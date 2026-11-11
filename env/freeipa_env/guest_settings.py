"""Settings applied to every nested VM before the inner run.

Each `vm:` key below is set at the top level of the nested spec and may be
overridden for one VM in the `vm.vms` entry named after its host. To add a
setting, add one row to SETTINGS: the rows run in table order, so a row may
rely on the ones above it (`copr` before `guest_packages`).
"""
import shlex
from collections import namedtuple

from . import guest_packages

# values: allowed values (None: any); command: value -> shell command;
# timeout: seconds for that command on one VM
Setting = namedtuple('Setting', 'name values command timeout')


def _selinux(mode):
    perm = mode == 'permissive'
    # persisted for reboots; -DB also logs dontaudit denials
    return (f"sed -i 's/^SELINUX=.*/SELINUX={mode}/' /etc/selinux/config && "
            f"setenforce {int(not perm)} && semodule -{'DB' if perm else 'B'}")


def _copr(repos):
    return ' && '.join([guest_packages.EL_PREP] + [
        f'dnf -y copr enable {shlex.quote(r)}' for r in repos])


def _script(lines):
    text = lines if isinstance(lines, str) else '\n'.join(lines)
    return 'bash -ec ' + shlex.quote(text)


SETTINGS = [
    Setting('selinux', ('permissive', 'enforcing'), _selinux, 300),
    Setting('copr', None, _copr, 600),
    Setting('guest_packages', None, guest_packages.install_cmd, 3600),
    Setting('script', None, _script, 3600),
]


def apply(cfg, hosts, run):
    """Run every configured setting on each of `hosts` through
    `run(host, command, timeout)`; returns {host name: {setting: value}}."""
    over = {v.get('name'): v for v in cfg.get('vms') or []}
    done = {}
    for h in hosts:
        done[h.name] = {}
        for s in SETTINGS:
            value = over.get(h.name, {}).get(s.name, cfg.get(s.name))
            if not value:
                continue
            if s.values and value not in s.values:
                raise ValueError(f'{s.name} for {h.name} is {value!r}; '
                                 f'use one of {", ".join(s.values)}')
            run(h, s.command(value), s.timeout)
            done[h.name][s.name] = value
    return done
