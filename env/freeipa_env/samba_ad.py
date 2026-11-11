"""A Samba AD domain controller on a VM, for the AD-trust integration tests.

`script()` returns the shell script that turns a Fedora VM into the DC of
`realm`: provision, keep the DC on the private network only (the NAT NIC
every VM has would otherwise be in its DNS and collide with the IPA VM's own
NAT address), and create the objects the tests expect
(github.com/freeipa/freeipa-pr-ci, ansible/roles/windows/ipa-ad-data). The
ipatests harness drives such a host through `host_type: samba`
(ipatests/pytest_ipa/integration/host.py, SambaADHost).

Not created yet, so the tests that need them fail on Samba: the
cannotchangepwduser and expiredaduser properties, UPN suffix users, primary
groups other than Domain Users, and a child or tree domain.
"""
import shlex

# name, gid-number
GROUPS = [
    ('testgroup', 10047), ('mytestgroup', 10055), ('group@group', 10048),
    ('testgroup1', None),
]

# name, given, surname, uid, gid, flags. flags: disabled; mustchange
USERS = [
    ('testuser', 'Test', 'User', 10042, 10047, ()),
    ('testuser1', 'Test1', 'User1', 10050, None, ()),
    ('testuser2', 'Test2', 'User2', 10060, 10049, ()),
    ('nonposixuser', 'Nonposix', 'User', None, None, ()),
    ('nonposixuser1', 'Nonposix1', 'User1', None, None, ()),
    ('disabledaduser', 'Disabledad', 'User', 10043, 10047, ('disabled',)),
    ('mytestuser', 'Test', 'User', 10055, 10055, ()),
    ('changepwdatlogonuser', 'ChangePwdAtLogon', 'User', 10062, 10047,
     ('mustchange',)),
]


def _user_cmd(name, given, sur, uid, gid, flags, password, nis):
    q = shlex.quote
    cmd = ['samba-tool', 'user', 'create', name, password,
           '--given-name', given, '--surname', sur, '--use-username-as-cn']
    if uid is not None and gid is not None:
        cmd += ['--uid-number', str(uid), '--gid-number', str(gid),
                '--nis-domain', nis, '--login-shell', '/bin/sh',
                '--unix-home', f'/home/{name}', '--gecos', f'{given} {sur}']
    if 'mustchange' in flags:
        cmd += ['--must-change-at-next-login']
    lines = [' '.join(q(c) for c in cmd)]
    if uid is not None and gid is None:
        # samba-tool wants a gid with a uid; this user has none on purpose
        lines.append(
            'ldbmodify -H /var/lib/samba/private/sam.ldb <<EOF\n'
            f'dn: CN={name},CN=Users,$basedn\n'
            'changetype: modify\n'
            f'replace: uidNumber\nuidNumber: {uid}\n-\n'
            'replace: loginShell\nloginShell: /bin/sh\n-\n'
            f'replace: unixHomeDirectory\nunixHomeDirectory: /home/{name}\n'
            f'-\nreplace: gecos\ngecos: {given} {sur}\n'
            'EOF')
    if 'mustchange' not in flags:
        lines.append(f'samba-tool user setexpiry {q(name)} --noexpiry')
    if 'disabled' in flags:
        lines.append(f'samba-tool user disable {q(name)}')
    return lines


def script(realm, netbios, ip, forwarder, password='Secret123'):
    """Shell script (run as root) that makes this VM the DC of `realm`"""
    q = shlex.quote
    basedn = ','.join('DC=' + p for p in realm.split('.'))
    lines = [
        'set -ex',
        f'basedn={q(basedn)}',
        'dnf -y install samba-dc samba-dc-provision krb5-workstation '
        'bind-utils ldb-tools',
        # samba's DNS server needs port 53
        'systemctl disable --now systemd-resolved || true',
        'rm -f /etc/resolv.conf /etc/samba/smb.conf',
        f'echo "nameserver {forwarder}" > /etc/resolv.conf',
        f'nic=$(ip -o -4 addr show | awk -v ip={ip} '
        "'index($4, ip \"/\") == 1 {print $2}')",
        '[ -n "$nic" ]',
        f'samba-tool domain provision --realm={q(realm.upper())} '
        f'--domain={q(netbios)} --adminpass={q(password)} '
        '--server-role=dc --dns-backend=SAMBA_INTERNAL --use-rfc2307 '
        f'--option="dns forwarder={forwarder}"',
        # only the private NIC: the NAT NIC is the same 10.0.2.15 on every VM
        r'sed -i "/^\[global\]/a\\\tinterfaces = lo $nic\n'
        r'\tbind interfaces only = yes" /etc/samba/smb.conf',
        'cp /var/lib/samba/private/krb5.conf /etc/krb5.conf',
        f'echo "nameserver {ip}" > /etc/resolv.conf',
        'systemctl enable --now samba',
        'for i in $(seq 30); do samba-tool domain level show '
        '>/dev/null 2>&1 && break; sleep 2; done',
        # the tests sync the IPA hosts' clocks with the DC (tasks.sync_time)
        'dnf -y install chrony',
        'printf "allow\\nlocal stratum 10\\n" >> /etc/chrony.conf',
        'systemctl enable chronyd && systemctl restart chronyd',
        'dc=$(hostname -s)',
        # drop every address of the DC that is not the private one
        f'for i in $(seq 10); do samba-tool dns query 127.0.0.1 '
        f'{q(realm.lower())} "$dc" ALL -U administrator%{q(password)} '
        '>/tmp/dc-dns 2>/dev/null && break; sleep 3; done',
        'for t in A AAAA; do for a in $(awk -v t="$t:" \'$1 == t {print $2}\' '
        '/tmp/dc-dns); do '
        f'[ "$a" = {ip} ] || samba-tool dns delete 127.0.0.1 '
        f'{q(realm.lower())} "$dc" $t "$a" -U administrator%{q(password)}; '
        'done; done',
    ]
    nis = netbios.lower()
    for name, gid in GROUPS:
        opt = f' --gid-number {gid} --nis-domain {q(nis)}' if gid else ''
        lines.append(f'samba-tool group add {q(name)}{opt}')
    for u in USERS:
        lines += _user_cmd(*u, password, nis)
    lines.append('echo SAMBA-AD-READY')
    return '\n'.join(lines) + '\n'
