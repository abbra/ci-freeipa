"""Packages to install on nested VMs before the inner run.

`vm.guest_packages` in a nested spec lists set names (`gpu`, `ipa-distro`)
and/or plain package names; a `-name` entry removes that set's (or
package's) packages from the result. Works on Fedora, CentOS Stream, RHEL and the
EL clones (dnf); EL hosts get EPEL and CRB enabled first.
"""
import shlex

SETS = {
    # virgl check: GL driver + eglinfo (mesa-demos on F45 does not ship it)
    'gpu': ['mesa-dri-drivers', 'egl-utils'],
    # FreeIPA tests on the distro's own packages (no COPR, no local build)
    'ipa-distro': [
        'freeipa-server', 'freeipa-server-common', 'freeipa-server-dns',
        'freeipa-server-encrypted-dns', 'freeipa-server-trust-ad',
        'freeipa-client', 'freeipa-client-common', 'freeipa-client-encrypted-dns',
        'firewalld',
        'freeipa-client-samba', 'freeipa-client-epn', 'freeipa-common',
        'freeipa-python-compat', 'freeipa-selinux', 'freeipa-selinux-nfast',
        'freeipa-selinux-luna', 'python3-ipaserver', 'python3-ipaclient',
        'python3-ipalib', 'python3-ipatests', 'python3-samba', 'sudo',
        'python3-pip',
    ],
}

# pip packages not shipped as RPMs on F45
PIP = {'ipa-distro': ['pytest-html==4.2.0']}

# EL clones need EPEL and CRB for some of the above; Fedora needs nothing
EL_PREP = ('. /etc/os-release && case "$ID" in '
           'centos|rhel|rocky|almalinux) '
           'dnf -y install epel-release && '
           '{ dnf config-manager --set-enabled crb || '
           'dnf config-manager --set-enabled powertools; } ;; esac')


def install_cmd(names, copr=()):
    """Shell command enabling the COPR repos in `copr` and installing the
    sets and packages in `names`; packages named by a `-name` entry are
    dropped whatever their position."""
    pkgs, pip, drop = [], [], set()
    for n in names:
        if n.startswith('-'):
            drop.update(SETS.get(n[1:], [n[1:]]))
            continue
        pkgs += SETS.get(n, [n])
        pip += PIP.get(n, [])
    pkgs = [p for p in dict.fromkeys(pkgs) if p not in drop]
    steps = [EL_PREP]
    steps += [f'dnf -y copr enable {shlex.quote(r)}' for r in copr]
    if pkgs:
        steps.append('dnf -y install ' + ' '.join(shlex.quote(p) for p in pkgs))
    for p in dict.fromkeys(pip):
        q = shlex.quote(p)
        steps.append(f'python3 -m pip install {q} || '
                     f'python3 -m pip install --break-system-packages {q}')
    return ' && '.join(steps)


# shortcut: no check of the package list against the repos; a typo fails at
# dnf install time on the VM, not here
