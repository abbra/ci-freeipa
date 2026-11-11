"""Settings applied to every nested VM before the inner run.

Each `vm:` key below is set at the top level of the nested spec and may be
overridden for one VM in the `vm.vms` entry named after its host. To add a
setting, add one row to SETTINGS: the rows run in table order, so a row may
rely on the ones above it (`copr` before `guest_packages`).
"""
import glob
import os
import shlex
from collections import namedtuple

from . import guest_packages

# values: allowed values (None: any); command: value -> shell command;
# timeout: seconds for that command on one VM; files: the value is a list of
# local paths (globs allowed) copied to the VM first, and `command` gets the
# directory they landed in
Setting = namedtuple('Setting', 'name values command timeout files')

UPLOADS = '/root/ipa-local'


def _selinux(mode):
    perm = mode == 'permissive'
    # persisted for reboots; -DB also logs dontaudit denials. A fresh VM may
    # still hold the policy lock (cloud-init, a package trigger), so semodule
    # is retried for up to three minutes.
    sem = f"semodule -{'DB' if perm else 'B'}"
    return (f"sed -i 's/^SELINUX=.*/SELINUX={mode}/' /etc/selinux/config && "
            f"setenforce {int(not perm)} && "
            f"for i in $(seq 36); do {sem} && break; "
            f"[ $i = 36 ] && exit 1; sleep 5; done")


def _copr(repos):
    return ' && '.join([guest_packages.EL_PREP] + [
        f'dnf -y copr enable {shlex.quote(r)}' for r in repos])


def _script(lines):
    text = lines if isinstance(lines, str) else '\n'.join(lines)
    return 'bash -ec ' + shlex.quote(text)


def _rpms(d):
    # install, then reinstall: dnf skips a file whose NEVRA is already
    # installed, which would keep the old build of a rebuilt package
    return f'dnf -y install {d}/*.rpm && dnf -y reinstall {d}/*.rpm'


def _srpms(d):
    # one SRPM at a time, in name order, installing what each builds so the
    # next one can build against it
    return _script([
        'dnf -y install rpm-build dnf-plugins-core',
        f'for s in {d}/*.src.rpm; do',
        '  dnf -y builddep "$s"',
        '  out=$(rpmbuild --rebuild "$s" | sed -n "s/^Wrote: //p" '
        '| grep -v "[.]src[.]rpm$")',
        '  dnf -y install $out && dnf -y reinstall $out',
        'done'])


SETTINGS = [
    Setting('selinux', ('permissive', 'enforcing'), _selinux, 300, False),
    Setting('copr', None, _copr, 600, False),
    Setting('guest_packages', None, guest_packages.install_cmd, 3600, False),
    Setting('rpms', None, _rpms, 3600, True),
    Setting('srpms', None, _srpms, 7200, True),
    Setting('script', None, _script, 3600, False),
]


def _local_files(name, host, value):
    paths = [value] if isinstance(value, str) else value
    found = [f for p in paths
             for f in sorted(glob.glob(os.path.expanduser(p)))]
    if not found:
        raise ValueError(f'{name} for {host}: no file matches {paths}')
    return found


def apply(cfg, hosts, run, put):
    """Run every configured setting on each of `hosts` through
    `run(host, command, timeout)`, copying a file setting's paths with
    `put(host, local, remote)` first; returns
    {host name: {setting: value}}."""
    over = {v.get('name'): v for v in cfg.get('vms') or []}
    done = {}
    for h in hosts:
        done[h.name] = {}
        # a vms entry is named after the host, in short or full form
        mine = over.get(h.name) or over.get(h.name.split('.')[0]) or {}
        for s in SETTINGS:
            value = mine.get(s.name, cfg.get(s.name))
            if not value:
                continue
            if s.values and value not in s.values:
                raise ValueError(f'{s.name} for {h.name} is {value!r}; '
                                 f'use one of {", ".join(s.values)}')
            arg = value
            if s.files:
                files = _local_files(s.name, h.name, value)
                arg = f'{UPLOADS}/{s.name}'
                run(h, f'rm -rf {arg} && mkdir -p {arg}', 60)
                for f in files:
                    put(h, f, f'{arg}/{os.path.basename(f)}')
            run(h, s.command(arg), s.timeout)
            done[h.name][s.name] = value
    return done
