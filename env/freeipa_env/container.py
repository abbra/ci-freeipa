"""Running inside a container: what that changes on the control node.

A containerized control node (a toolbox/distrobox/dev container of a
workstation) often cannot run nested containers: any ``podman`` command
would talk to (or try to start) an outer store and may break the user's
environment, and rootless ``sudo dnf install`` is unavailable for the host
tools the qemu backend needs. The workarounds ci-freeipa applies when
``in_container()`` is true:

* ``podman`` is never executed: ``containerdisk.resolve`` falls back to
  ``skopeo``, which pulls the same image without touching any container
  store, and the podman provider is refused with a clear message (use the
  nested qemu backend, as the containerdisk flow already does).
* ``qemu-img`` and a ``mkisofs``-compatible tool (genisoimage/mkisofs/
  xorriso), when missing, are fetched as Fedora RPMs, extracted into a
  cache directory and exposed through wrapper scripts in ``~/.local/bin``
  (or ``$FREEIPA_CI_TOOL_BIN``). The next ``up`` then finds them on PATH.
  This needs ``dnf download`` (any user) and ``rpm2archive``/``cpio``.

The mcast private-NIC fallback for VM-to-VM traffic is in vmbackend.py: it
probes multicast, not the container (a VM host in a container without
multicast routing hits the same wall).
"""
import os
import shutil
import stat
import subprocess
import sys

# /run/.containerenv exists in (at least) podman and toolbox containers;
# /.dockerenv in docker ones. $container is set by toolbox.
_MARKERS = ('/run/.containerenv', '/.dockerenv')


def in_container():
    """True when the control node itself runs inside a container."""
    return (any(os.path.exists(m) for m in _MARKERS)
            or os.environ.get('container') is not None
            or 'container' in _pid1_env())


def _pid1_env():
    try:
        with open('/proc/1/environ', 'rb') as f:
            env = f.read().split(b'\0')
    except OSError:
        return {}
    out = {}
    for e in env:
        k, _, v = e.partition(b'=')
        if k:
            out[k.decode(errors='replace')] = v.decode(errors='replace')
    return out


def tool_bin_dir():
    """Where the wrapper scripts for fetched tools are put."""
    d = os.environ.get('FREEIPA_CI_TOOL_BIN') or os.path.expanduser(
        '~/.local/share/freeipa-ci/tool-bin')
    return d


def scratch_dir():
    """Where large temporary files (image pulls) are written.

    /tmp is often tmpfs: pulling a 1 GiB containerdisk there spends RAM.
    """
    d = os.environ.get('FREEIPA_CI_SCRATCH') or os.path.expanduser(
        '~/tmp/ci-freeipa-scratch')
    os.makedirs(d, exist_ok=True)
    return d


_EXTRACT_DIR = 'rpms-extracted'

# tool -> (rpm names to fetch, binary to wrap, extra rpm names for its libs)
# ordered: the first present on PATH wins in _seed_tool
_TOOLS = {
    'qemu-img': (['qemu-img'], 'qemu-img', []),
    'genisoimage': (['genisoimage'], 'genisoimage', ['libusal']),
    'mkisofs': (['genisoimage'], 'mkisofs', ['libusal']),   # a symlink
    'xorriso': (['xorriso'], 'xorriso', ['libisoburn', 'libisofs', 'libburn']),
}

_WRAPPER = """#!/bin/sh
# fetched by ci-freeipa: {tool} extracted from Fedora RPMs (no root needed)
export LD_LIBRARY_PATH="{libdir}${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}"
exec "{binary}" "$@"
"""


def _rpm_extract(rpm, dest):
    """Extract `rpm` into `dest` (rpm2archive | tar, no root, no cpio oddities)."""
    os.makedirs(dest, exist_ok=True)
    with open(rpm, 'rb') as f:
        rpm_data = f.read()
    p = subprocess.run(['rpm2archive', '-'], input=rpm_data,
                       stdout=subprocess.PIPE, check=False)
    if p.returncode:
        raise RuntimeError(f'rpm2archive failed on {rpm}')
    subprocess.run(['tar', '-xz', '-C', dest, '--delay-directory-restore'],
                   input=p.stdout, stdout=subprocess.DEVNULL, check=True)


def _dnf_download(pkgs, destdir):
    """`dnf download` the packages for this machine's arch into destdir."""
    os.makedirs(destdir, exist_ok=True)
    p = subprocess.run(['dnf', 'download', '--forcearch', _arch(),
                        *pkgs], cwd=destdir, capture_output=True, text=True,
                       check=False)
    if p.returncode:
        raise RuntimeError(f"dnf download {' '.join(pkgs)} failed: "
                           f'{(p.stderr or p.stdout).strip()[-500:]}')


def _arch():
    p = subprocess.run(['rpm', '--eval', '%{_arch}'], capture_output=True,
                       text=True, check=False)
    return p.stdout.strip() or 'x86_64'


def _wrapper_path(tool):
    return os.path.join(tool_bin_dir(), tool)


def _seed_tool(tool):
    """Fetch `tool` from the distro repos if it is missing.

    Returns the wrapper script path, or None when the tool is already on
    PATH (nothing to do) or cannot be fetched (missing dnf etc.).
    """
    if shutil.which(tool):
        return None
    if tool not in _TOOLS or not shutil.which('dnf'):
        return None
    rpms, binary, libs = _TOOLS[tool]
    extract = os.path.join(tool_bin_dir(), _EXTRACT_DIR)
    binary_path = os.path.join(extract, 'usr', 'bin', binary)
    if not os.path.isfile(binary_path):
        tmp = os.path.join(tool_bin_dir(), 'rpms-download')
        _dnf_download([*rpms, *libs], tmp)
        for rpm in _sorted_rpms(tmp, [*rpms, *libs]):
            _rpm_extract(rpm, extract)
        # some RPMs carry libs in usr/lib (32-bit multilib slots); a lib64
        # copy is what the 64-bit binary needs
        for libdir in ('lib', 'lib64'):
            src = os.path.join(extract, 'usr', libdir)
            dst = os.path.join(extract, 'usr', 'lib64')
            if libdir != 'lib64' and os.path.isdir(src):
                for f in os.listdir(src):
                    _move_if_new(os.path.join(src, f), os.path.join(dst, f))
        if not os.path.isfile(binary_path):
            raise RuntimeError(f'{tool}: {binary_path} not found after '
                               'extracting the RPMs')
    libdir = os.path.join(extract, 'usr', 'lib64')
    wrap = _wrapper_path(tool)
    if os.path.isfile(wrap):
        return wrap
    os.makedirs(tool_bin_dir(), exist_ok=True)
    with open(wrap, 'w') as f:
        f.write(_WRAPPER.format(tool=tool, libdir=libdir, binary=binary_path))
    os.chmod(wrap, os.stat(wrap).st_mode | stat.S_IXUSR | stat.S_IXGRP
             | stat.S_IXOTH)
    return wrap


def _sorted_rpms(dir_, pkgs):
    """The downloaded .rpm files for `pkgs`, any arch, in `pkgs` order."""
    out = []
    for pkg in pkgs:
        for f in sorted(os.listdir(dir_)):
            if f.startswith(pkg + '-') and f.endswith('.rpm'):
                out.append(os.path.join(dir_, f))
    return out


def _move_if_new(src, dst):
    if os.path.isfile(dst):
        return
    if os.path.isdir(src):
        # merge directories (.build-id and friends) instead of renaming
        for f in os.listdir(src):
            _move_if_new(os.path.join(src, f), os.path.join(dst, f))
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.rename(src, dst)


def prepare_tools():
    """Fetch qemu-img and an ISO tool when missing; prepend wrappers to PATH.

    Idempotent: present tools are left alone. Returns the names of the
    tools it fetched (for a message), so callers stay quiet by default.
    """
    if not in_container():
        return []
    fetched = []
    # mkisofs is a symlink to genisoimage: the genisoimage wrapper covers
    # vmbackend's _seed lookup, which tries genisoimage first anyway
    for tool in ('qemu-img', 'genisoimage', 'xorriso'):
        try:
            if _seed_tool(tool) and not fetched:
                fetched.append(tool)
        except RuntimeError as e:
            print(f'== container mode: cannot fetch {tool}: {e}',
                  file=sys.stderr)
    if fetched:
        bindir = tool_bin_dir()
        path = os.environ.get('PATH', os.defpath)
        if bindir not in path.split(os.pathsep):
            os.environ['PATH'] = f'{bindir}{os.pathsep}{path}'
    return fetched
