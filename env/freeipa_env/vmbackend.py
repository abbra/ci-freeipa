"""VM acquisition backends for the nested provider (design doc §3.9).

A VMBackend is the "get a VM" half of a nested provider: it turns a config
dict into provisioned VMs and hands back VMHandle objects (ssh targets)
that the nested provider then drives.  The "run the env in the VM" half
lives in nested_provider.py and composes a VMBackend with an inner
provider (default: podman).

Backends:
  * ``ssh``      -- a pool of pre-allocated user@host entries; the
                    "no real API" reference case.
  * ``command``  -- shell out to provision/deprovision scripts; the generic
                    cloud-API integration point (wrap any cloud in 2 scripts).
  * ``openstack``-- a concrete real-API example (openstack CLI); the
                    template for any other cloud.
  * ``qemu``     -- local VMs under qemu/KVM on the control node (qcow2
                    overlay + cloud-init seed; ssh over a forwarded port).

VMHandle is the currency between a backend and the nested provider: an ssh
target (user@host[:port]) with an optional private key and free-form meta
(the backend's own VM id, used for terminate).
"""

import glob
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml


class VMBackendError(Exception):
    pass


# --------------------------------------------------------------------- handle
@dataclass
class VMHandle:
    """An ssh-reachable provisioned VM."""
    host: str
    user: str = 'root'
    port: int = 22
    id: Optional[str] = None        # backend-specific id (used by terminate)
    key: Optional[str] = None       # private key path on the control node
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def target(self):
        return f'{self.user}@{self.host}'

    @classmethod
    def parse(cls, line: str) -> 'VMHandle':
        """Parse ``user@host``, ``user@host:port``, or a bare ``host``."""
        line = line.strip()
        user, sep, host = line.rpartition('@')
        if not sep:
            user, host = 'root', line
        port = 22
        if host.count(':') == 1 and not host.startswith('['):
            host, _, p = host.rpartition(':')
            if p.isdigit():
                port = int(p)
        return cls(host=host, user=user or 'root', port=port)

    def to_dict(self):
        return {
            'host': self.host, 'user': self.user, 'port': self.port,
            'id': self.id, 'key': self.key, 'meta': self.meta,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(host=d['host'], user=d.get('user', 'root'),
                   port=int(d.get('port', 22)), id=d.get('id'),
                   key=d.get('key'), meta=d.get('meta') or {})


# ----------------------------------------------------------------- ssh helpers
SSH_OPTS = [
    '-o', 'StrictHostKeyChecking=accept-new',
    '-o', 'ConnectTimeout=10',
    '-o', 'BatchMode=yes',
    '-o', 'ServerAliveInterval=30',
    '-o', 'ServerAliveCountMax=4',
]


def _ssh_command(handle: VMHandle):
    """The ssh argv without the target; shared by ssh and rsync."""
    args = ['ssh'] + SSH_OPTS
    if handle.meta.get('ephemeral_hostkey'):
        # per-VM host keys on reused localhost ports: don't pin them
        args += ['-o', 'UserKnownHostsFile=/dev/null', '-o', 'LogLevel=ERROR']
    if handle.port and handle.port != 22:
        args += ['-p', str(handle.port)]
    if handle.key:
        args += ['-i', handle.key]
    return args


def _ssh_prefix(handle: VMHandle):
    return _ssh_command(handle) + [handle.target]


def ssh_run(handle: VMHandle, cmd: str, check: bool = False,
            timeout: Optional[int] = None, stream: bool = False,
            log_path: Optional[str] = None):
    """Run a shell command on the VM. Returns (rc, stdout+stderr).

    When stream=True (and log_path is given) output is teed to the log and
    echoed to stdout line by line; otherwise it is captured and returned."""
    args = _ssh_prefix(handle) + [cmd]
    if stream and log_path:
        with open(log_path, 'wb') as lf:
            lf.write(f'+ {shlex.join(args)}\n'.encode())
            lf.flush()
            proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT)
            for line in proc.stdout:
                import sys
                sys.stdout.buffer.write(line)
                sys.stdout.flush()
                lf.write(line)
            rc = proc.wait()
            return rc, ''
    proc = subprocess.run(args, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=timeout)
    out = proc.stdout.decode(errors='replace')
    if log_path:
        with open(log_path, 'a') as lf:
            lf.write(out)
    if check and proc.returncode:
        raise VMBackendError(f'ssh {handle.target} failed '
                             f'({proc.returncode}): {cmd}\n{out[-2000:]}')
    return proc.returncode, out


def _rsync(handle: VMHandle, src: str, dst: str):
    ssh = shlex.join(_ssh_command(handle))
    subprocess.run(['rsync', '-az', '--partial', '-e', ssh, src, dst],
                   check=True)


def rsync_to(handle: VMHandle, local: str, remote: str):
    """rsync local -> VM:remote (remote path need not pre-exist)."""
    _rsync(handle, local, f'{handle.target}:{remote}')


def rsync_from(handle: VMHandle, remote: str, local: str):
    """rsync VM:remote -> local."""
    _rsync(handle, f'{handle.target}:{remote}', local)


# ------------------------------------------------------------------- backends
class VMBackend:
    name = 'base'

    def __init__(self, cfg: Optional[Dict] = None):
        self.cfg = cfg or {}

    def provision(self, count: int = 1) -> List[VMHandle]:
        raise NotImplementedError

    def terminate(self, handle: VMHandle) -> None:
        raise NotImplementedError

    def wait_ready(self, handle: VMHandle, timeout: int = 600,
                   interval: int = 5) -> None:
        """Poll until ssh reaches the VM (or timeout)."""
        deadline = time.time() + timeout
        last = ''
        while time.time() < deadline:
            rc, out = ssh_run(handle, 'true', check=False, timeout=30)
            if rc == 0:
                print(f'== vm {handle.target} is reachable')
                return
            last = out.strip().splitlines()[-1] if out.strip() else ''
            time.sleep(interval)
        raise VMBackendError(
            f'vm {handle.target} not ssh-reachable within {timeout}s'
            + (f' (last: {last})' if last else ''))

    def describe(self) -> str:
        return self.name


class SshBackend(VMBackend):
    """A pool of pre-allocated ``user@host[:port]`` entries.

    Config::

        vm:
          backend: ssh
          hosts: [root@10.0.0.5, root@10.0.0.6]
    """
    name = 'ssh'

    def __init__(self, cfg):
        super().__init__(cfg)
        hosts = self.cfg.get('hosts') or []
        self._pool = [VMHandle.parse(h) if isinstance(h, str)
                      else VMHandle.from_dict(h) for h in hosts]

    def provision(self, count=1):
        if not self._pool:
            raise VMBackendError('ssh backend: no hosts configured')
        if count > len(self._pool):
            raise VMBackendError(
                f'ssh backend: asked for {count} but the pool has '
                f'{len(self._pool)}')
        out, self._pool = self._pool[:count], self._pool[count:]
        return out

    def terminate(self, handle):
        # pre-allocated: leave it in place (it belongs to the pool)
        print(f'== vm {handle.target}: pre-allocated, leaving in place')


class CommandBackend(VMBackend):
    """Provision/deprovision by shelling out to user scripts.

    The generic cloud-API integration point: wrap any cloud in two scripts.

    Config::

        vm:
          backend: command
          provision_cmd: /opt/cloud/get-vms.sh     # $1 = count
          deprovision_cmd: /opt/cloud/drop-vms.sh  # args = handle ids/targets

    ``provision_cmd <count>`` must print one ``user@host[:port]`` per line
    (or a JSON list of {host, user, port, id, key}).  ``deprovision_cmd``
    receives one argument per VM being released (its id if present, else
    its user@host target)."""
    name = 'command'

    def _run_script(self, script: str, argv: List[str]) -> str:
        if not script:
            raise VMBackendError(f'command backend: no {script} configured')
        if isinstance(script, (list, tuple)):
            cmd = [str(x) for x in list(script) + argv]
        else:
            cmd = shlex.split(script) + [str(a) for a in argv]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=600)
        except FileNotFoundError:
            raise VMBackendError(f'command backend: script not found: '
                                 f'{shlex.join(cmd)}')
        out = proc.stdout.decode(errors='replace')
        if proc.returncode:
            raise VMBackendError(
                f'command backend: {shlex.join(cmd)} failed '
                f'({proc.returncode}):\n{out[-2000:]}')
        return out

    def provision(self, count=1):
        out = self._run_script(self.cfg.get('provision_cmd'), [count])
        handles: List[VMHandle] = []
        # try JSON first (a list of handle dicts), fall back to lines
        s = out.strip()
        if s.startswith('['):
            for i, d in enumerate(json.loads(s)):
                h = VMHandle.from_dict(d)
                h.id = h.id or f'vm{i}'
                handles.append(h)
            return handles
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            handles.append(VMHandle.parse(line))
        if not handles:
            raise VMBackendError(
                'command backend: provision_cmd printed no VM handles')
        return handles

    def terminate(self, handle):
        arg = handle.id or handle.target
        self._run_script(self.cfg.get('deprovision_cmd'), [arg])
        print(f'== vm {handle.target}: deprovisioned via command backend')


class OpenstackBackend(VMBackend):
    """A concrete real cloud-API example, built on the ``openstack`` CLI.

    This is the template for any other cloud: map provision to
    ``server create`` (+ floating IP), terminate to ``server delete``.

    Config::

        vm:
          backend: openstack
          cloud: prod          # `openstack --cloud prod` (or OS_* env)
          image: fedora-44
          flavor: m1.medium
          keypair: freeipa-ci # optional
          network: test-net   # optional fixed network
          floating_pool: public
    """
    name = 'openstack'

    def _os(self, args: List[str], parse_json: bool = True):
        cmd = ['openstack']
        cloud = self.cfg.get('cloud')
        if cloud:
            cmd += ['--cloud', cloud]
        cmd += args
        if parse_json and '--format' not in args:
            cmd += ['--format', 'json']
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=600)
        except FileNotFoundError:
            raise VMBackendError('openstack backend: `openstack` CLI not '
                                 'found on the control node')
        out = proc.stdout.decode(errors='replace')
        if proc.returncode:
            raise VMBackendError(
                f'openstack backend: {shlex.join(cmd)} failed '
                f'({proc.returncode}):\n{out[-2000:]}')
        return json.loads(out) if parse_json else out

    def provision(self, count=1):
        image = self.cfg.get('image')
        flavor = self.cfg.get('flavor')
        if not image or not flavor:
            raise VMBackendError('openstack backend: need `image` and '
                                 '`flavor`')
        handles = []
        for i in range(count):
            name = f'freeipa-ci-{uuid.uuid4().hex[:10]}'
            create = ['server', 'create', '--wait', '--image', image,
                      '--flavor', flavor, '-n', name]
            if self.cfg.get('keypair'):
                create += ['--keypair', self.cfg['keypair']]
            if self.cfg.get('network'):
                create += ['--network', self.cfg['network']]
            self._os(create, parse_json=False)
            self.wait_ready_ip(name)
            ip = self._ip_for(name)
            h = VMHandle(host=ip, user='root', id=name)
            h.meta['openstack'] = name
            handles.append(h)
            print(f'== openstack: provisioned {name} at {ip}')
        return handles

    def wait_ready_ip(self, name):
        # `server create --wait` already blocks until ACTIVE; poll the
        # address just in case the floating/fixed IP attaches late.
        deadline = time.time() + int(self.cfg.get('timeout', 300))
        while time.time() < deadline:
            ip = self._ip_for(name)
            if ip:
                return ip
            time.sleep(5)
        raise VMBackendError(f'openstack backend: no address for {name}')

    def _ip_for(self, name) -> Optional[str]:
        pool = self.cfg.get('floating_pool', 'public')
        # prefer a floating IP, else the first fixed address
        try:
            fips = self._os(['floating', 'ip', 'list', '--server', name,
                             '--format', 'json'])
            if fips:
                return fips[0]['IP']
        except VMBackendError:
            pass
        srv = self._os(['server', 'show', name, '--format', 'json'])
        addrs = srv.get('addresses') or {}
        for addrs_list in addrs.values():
            for a in addrs_list:
                if a.get('addr'):
                    return a['addr']
        return None

    def terminate(self, handle):
        name = handle.id
        if not name:
            print(f'== openstack: no server id for {handle.target}; '
                  f'skipping delete')
            return
        # release floating ip if present
        pool = self.cfg.get('floating_pool', 'public')
        try:
            fips = self._os(['floating', 'ip', 'list', '--server', name,
                             '--format', 'json'])
            for f in fips:
                self._os(['floating', 'ip', 'delete', f['IP']],
                         parse_json=False)
        except VMBackendError:
            pass
        self._os(['server', 'delete', name], parse_json=False)
        print(f'== openstack: deleted {name}')


PRIVATE_PREFIX = '192.168.100'


def _free_port(kind=socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

def _free_vnc_display() -> int:
    """First VNC display n (TCP 5900+n) not already bound on loopback."""
    for n in range(1, 200):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('127.0.0.1', 5900 + n))
            except OSError:
                continue
            return n
    raise VMBackendError('no free VNC display in 1..199')


def _usb_id(s) -> tuple:
    """'VVVV:PPPP' -> lowercase ('vvvv', 'pppp')."""
    vid, _, pid = str(s).lower().partition(':')
    try:
        if len(vid) == len(pid) == 4 and int(vid + pid, 16) >= 0:
            return vid, pid
    except ValueError:
        pass
    raise VMBackendError(f'qemu backend: usb id {s!r} must be VVVV:PPPP (hex)')


def _usb_devices() -> list:
    """(vid, pid, busnum, devnum) for each USB device in sysfs."""
    out = []
    for d in glob.glob('/sys/bus/usb/devices/*'):
        def rd(f):
            with open(os.path.join(d, f)) as fh:
                return fh.read().strip()
        try:
            out.append((rd('idVendor'), rd('idProduct'),
                        int(rd('busnum')), int(rd('devnum'))))
        except (OSError, ValueError):
            pass  # interface entries have no idVendor
    return out


def _usb_assign(cfgs):
    """Give each VM's `usb: [VVVV:PPPP]` entries a distinct host device (a
    device goes to one VM only). Sets c['usb_devices'] = [(bus, dev)]."""
    free = _usb_devices()
    for c in cfgs:
        c['usb_devices'] = []
        for want in c.get('usb') or []:
            vid, pid = _usb_id(want)
            hit = next((d for d in free if d[0] == vid and d[1] == pid), None)
            if hit is None:
                raise VMBackendError(f'qemu backend: no free USB device {vid}:{pid} '
                                     f'on this host')
            free.remove(hit)
            node = f'/dev/bus/usb/{hit[2]:03d}/{hit[3]:03d}'
            if not os.access(node, os.R_OK | os.W_OK):
                raise VMBackendError(f'qemu backend: USB {vid}:{pid} at {node} is '
                                     f'not read/write for this user')
            c['usb_devices'].append((hit[2], hit[3]))


def _usb_args(c) -> list:
    """xHCI controller plus usb-host devices for the VM's assigned devices."""
    if not c.get('usb_devices'):
        return []
    args = ['-device', 'qemu-xhci,id=xhci']
    for bus, dev in c['usb_devices']:
        args += ['-device', f'usb-host,bus=xhci.0,hostbus={bus},hostaddr={dev}']
    return args


def _mac() -> str:
    h = uuid.uuid4().hex[:6]
    return '52:54:00:' + ':'.join(h[i:i + 2] for i in (0, 2, 4))


def _qemu_pid(pidfile: str, vm_id: str) -> Optional[int]:
    """The live qemu pid for this VM, or None. qemu carries ``-name <vm_id>``
    on its cmdline, which guards against a recycled pid."""
    try:
        with open(pidfile) as f:
            pid = int(f.read().strip())
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            cmd = f.read()
    except (OSError, ValueError):
        return None
    return pid if vm_id.encode() in cmd else None


def live_qemu_vms(state_dir: str = '~/.cache/freeipa-ci/qemu') -> dict:
    """{vm id: pid} of the running qemu VMs under `state_dir`."""
    root = os.path.expanduser(state_dir)
    found = {}
    for vm_id in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        pid = _qemu_pid(os.path.join(root, vm_id, 'qemu.pid'), vm_id)
        if pid:
            found[vm_id] = pid
    return found


def stop_qemu(pid: int, grace: int = 30):
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + grace
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.5)
    os.kill(pid, signal.SIGKILL)


class QemuBackend(VMBackend):
    """Local VMs under qemu/KVM on the control node: no cloud API.

    Each VM is a qcow2 overlay on a cloud base image, booted with KVM and
    user-mode NAT networking. A cloud-init NoCloud seed authorizes the ssh
    public key for root; ssh reaches the guest on a host port forwarded to
    its port 22 on 127.0.0.1. Per-VM state lives in ``<state_dir>/<id>/``.

    Config::

        vm:
          backend: qemu
          image: ~/images/Fedora-Cloud-Base-Generic.x86_64.qcow2  # required
          cpus: 4                       # default 4
          memory: 8192                  # MiB, default 8192
          disk: 40G                     # overlay virtual size, default 40G
          ssh_key: ~/.ssh/id_ed25519    # default: every ~/.ssh/{id_ed25519,
                                        #   id_ecdsa,id_rsa}.pub is authorized
          state_dir: ~/.cache/freeipa-ci/qemu   # default
          qemu: qemu-system-x86_64              # default
          gpu: virgl                    # optional; virtio-gpu with host OpenGL
          render_node: /dev/dri/renderD128   # default, for gpu: virgl
          usb: [VVVV:PPPP]              # optional, per VM; host USB devices by
                                        #   VVVV:PPPP, one entry per device
          vms:                          # optional per-VM overrides, one per VM;
                                        #   vm.count must equal len(vms)
            - {name: master1, cpus: 8, memory: 16384}
            - {name: client1, image: ~/images/other.qcow2, disk: 20G}

    ``terminate`` kills the qemu process and removes the overlay and seed;
    ``console.log`` is kept for triage.
    """
    name = 'qemu'

    def _qemu(self):
        return self.cfg.get('qemu', 'qemu-system-x86_64')

    def _vm_cfgs(self, count):
        """Per-VM settings: top-level config, overridden by vms[i] if given."""
        base = {k: v for k, v in self.cfg.items() if k != 'vms'}
        over = self.cfg.get('vms')
        if over is None:
            return [dict(base) for _ in range(count)]
        if len(over) != count:
            raise VMBackendError(f'qemu backend: vm.vms lists {len(over)} VM(s) '
                                 f'but {count} requested; set vm.count to match')
        return [{**base, **(o or {})} for o in over]

    def _check_host(self, cfgs):
        for c in cfgs:
            image = c.get('image')
            if not image:
                raise VMBackendError('qemu backend: `vm.image` (cloud base image) '
                                     'is required, per VM or top-level')
            if not os.path.isfile(os.path.expanduser(image)):
                raise VMBackendError(f'qemu backend: base image not found: {image}')
        if not os.access('/dev/kvm', os.W_OK):
            raise VMBackendError('qemu backend: /dev/kvm is not writable '
                                 '(needs KVM and access to /dev/kvm)')
        for tool in (self._qemu(), 'qemu-img'):
            if not shutil.which(tool):
                raise VMBackendError(f'qemu backend: {tool} not found')

    def _pubkeys(self):
        """(authorized public key texts, private key for ssh or None)."""
        if self.cfg.get('ssh_key'):
            key = os.path.expanduser(self.cfg['ssh_key'])
            pubs = [key + '.pub']
        else:
            key = None
            pubs = [os.path.expanduser(f'~/.ssh/{n}.pub')
                    for n in ('id_ed25519', 'id_ecdsa', 'id_rsa')]
        pubs = [p for p in pubs if os.path.isfile(p)]
        if not pubs:
            raise VMBackendError('qemu backend: no ssh public key to authorize '
                                 '(set vm.ssh_key)')
        key = key or pubs[0][:-len('.pub')]     # private half of the first authorized key
        keys = []
        for p in pubs:
            with open(p) as f:
                keys.append(f.read().strip())
        return keys, key

    def _run(self, cmd, cwd=None):
        try:
            proc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=600)
        except FileNotFoundError:
            raise VMBackendError(f'qemu backend: {cmd[0]} not found')
        out = proc.stdout.decode(errors='replace')
        if proc.returncode:
            raise VMBackendError(f'qemu backend: {shlex.join(cmd)} failed '
                                 f'({proc.returncode}):\n{out[-2000:]}')

    def _seed(self, vmdir, vm_id, keys, net):
        tool = next((t for t in ('genisoimage', 'mkisofs', 'xorriso')
                     if shutil.which(t)), None)
        if not tool:
            raise VMBackendError('qemu backend: need genisoimage, mkisofs or '
                                 'xorriso to build the cloud-init seed')
        user = {'disable_root': False,
                'users': ['default',
                          {'name': 'root', 'ssh_authorized_keys': keys}],
                'write_files': [{'path': '/etc/hosts', 'append': True,
                                 'content': ''.join(f"{h['ip']} {h['name']}\n"
                                                    for h in net['hosts'])}]}
        netcfg = {'version': 2, 'ethernets': {
            'ssh': {'match': {'macaddress': net['mac_ssh']}, 'dhcp4': True},
            'priv': {'match': {'macaddress': net['mac_priv']},
                     'addresses': [f"{net['ip']}/24"]}}}
        with open(os.path.join(vmdir, 'user-data'), 'w') as f:
            f.write('#cloud-config\n' + yaml.safe_dump(user))
        with open(os.path.join(vmdir, 'meta-data'), 'w') as f:
            f.write(f"instance-id: {vm_id}\nlocal-hostname: {net['name']}\n")
        with open(os.path.join(vmdir, 'network-config'), 'w') as f:
            f.write(yaml.safe_dump(netcfg))
        args = ['-output', 'seed.iso', '-volid', 'cidata', '-joliet', '-rock',
                'user-data', 'meta-data', 'network-config']
        if tool == 'xorriso':
            args = ['-as', 'mkisofs'] + args
        self._run([tool] + args, cwd=vmdir)

    def _display_args(self, c, q):
        """No display, or virtio-gpu with host OpenGL (virgl) rendered on
        the control node's DRM render node via egl-headless. The guest screen
        is also served over VNC + websocket on loopback (q['vnc_websocket'])."""
        gpu = c.get('gpu')
        if not gpu:
            return ['-display', 'none']
        if gpu != 'virgl':
            raise VMBackendError(f'qemu backend: unknown gpu {gpu!r} '
                                 f'(supported: virgl)')
        node = os.path.expanduser(c.get('render_node', '/dev/dri/renderD128'))
        if not os.access(node, os.R_OK | os.W_OK):
            raise VMBackendError(f'qemu backend: gpu virgl needs read/write '
                                 f'access to {node}')
        disp = _free_vnc_display()
        q['vnc_websocket'] = _free_port()
        return ['-device', 'virtio-vga-gl',
                '-display', f'egl-headless,rendernode={node}',
                '-vnc', f'127.0.0.1:{disp},websocket={q["vnc_websocket"]}']

    def _start(self, keys, key, net, seg, c):
        vm_id = f'freeipa-ci-{uuid.uuid4().hex[:10]}'
        vmdir = os.path.join(os.path.expanduser(
            self.cfg.get('state_dir', '~/.cache/freeipa-ci/qemu')), vm_id)
        os.makedirs(vmdir)
        q = {'dir': vmdir, 'pidfile': os.path.join(vmdir, 'qemu.pid'),
             'console': os.path.join(vmdir, 'console.log')}
        disk = os.path.join(vmdir, 'disk.qcow2')
        seed = os.path.join(vmdir, 'seed.iso')
        port = _free_port()
        try:
            self._seed(vmdir, vm_id, keys, net)
            self._run(['qemu-img', 'create', '-q', '-f', 'qcow2', '-F', 'qcow2',
                       '-b', os.path.expanduser(c['image']), disk,
                       str(c.get('disk', '40G'))])
            self._run([self._qemu(), '-name', vm_id,
                       '-machine', 'q35,accel=kvm', '-cpu', 'host',
                       '-smp', str(c.get('cpus', 4)),
                       '-m', str(c.get('memory', 8192)),
                       *self._display_args(c, q), *_usb_args(c), '-daemonize',
                       '-pidfile', q['pidfile'],
                       '-serial', f'file:{q["console"]}',
                       '-drive', f'file={disk},if=virtio,format=qcow2',
                       '-drive', f'file={seed},if=virtio,media=cdrom,readonly=on',
                       # ssh NIC: NAT with a forwarded port on the control node
                       '-netdev', f'user,id=net0,hostfwd=tcp:127.0.0.1:{port}-:22',
                       '-device', f'virtio-net-pci,netdev=net0,mac={net["mac_ssh"]}',
                       # private NIC: multicast socket segment shared by this run
                       '-netdev', f'socket,id=priv,mcast=230.0.0.1:{seg}',
                       '-device', f'virtio-net-pci,netdev=priv,mac={net["mac_priv"]}'])
        except Exception:
            shutil.rmtree(vmdir, ignore_errors=True)
            raise
        if 'vnc_websocket' in q:
            print(f'== vm {vm_id}: VNC websocket 127.0.0.1:{q["vnc_websocket"]}')
        return VMHandle(host='127.0.0.1', port=port, id=vm_id, key=key,
                        meta={'ephemeral_hostkey': True, 'qemu': q,
                              'private': net})

    def provision(self, count=1):
        cfgs = self._vm_cfgs(count)
        self._check_host(cfgs)
        _usb_assign(cfgs)
        keys, key = self._pubkeys()
        # one private L2 segment per call: runs on the same host stay isolated
        seg = _free_port(socket.SOCK_DGRAM)
        names = [c.get('name', f'vm{i + 1}') for i, c in enumerate(cfgs)]
        if len(set(names)) != len(names):
            raise VMBackendError(f'qemu backend: duplicate VM names: {names}')
        nets = [{'name': n, 'ip': f'{PRIVATE_PREFIX}.{10 + i}',
                 'mac_ssh': _mac(), 'mac_priv': _mac()}
                for i, n in enumerate(names)]
        for n in nets:
            n['hosts'] = [{'name': m['name'], 'ip': m['ip']} for m in nets]
        handles = []
        try:
            for net, c in zip(nets, cfgs):
                handles.append(self._start(keys, key, net, seg, c))
        except Exception:
            for h in handles:
                try:
                    self.terminate(h)
                except VMBackendError as e:
                    print(f'== {e}')
            raise
        return handles

    def terminate(self, handle):
        q = handle.meta.get('qemu')
        if not q:
            raise VMBackendError(f'qemu backend: no qemu state for {handle.target}')
        pid = _qemu_pid(q['pidfile'], handle.id)
        if pid:
            stop_qemu(pid)
        for f in ('disk.qcow2', 'seed.iso', 'user-data', 'meta-data', 'qemu.pid'):
            try:
                os.remove(os.path.join(q['dir'], f))
            except FileNotFoundError:
                pass
        print(f'== vm {handle.target}: qemu {handle.id} stopped '
              f'(console: {q["console"]})')



BACKENDS = {
    'ssh': SshBackend,
    'command': CommandBackend,
    'openstack': OpenstackBackend,
    'qemu': QemuBackend,
}


def make_backend(cfg: Dict) -> VMBackend:
    cfg = cfg or {}
    name = cfg.get('backend')
    if not name:
        raise VMBackendError('vm backend: `vm.backend` is required')
    cls = BACKENDS.get(name)
    if cls is None:
        raise VMBackendError(
            f'unknown vm backend {name!r} (known: {sorted(BACKENDS)})')
    return cls(cfg)
