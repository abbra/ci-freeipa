import subprocess

import yaml

from freeipa_env import samba_ad
from freeipa_env.envspec import EnvSpec
from freeipa_env.external_provider import ExternalProvider

DOC = {
    'name': 'ad', 'domain': 'ipa.test', 'ad_domain': 'ad.example',
    'provider': 'external',
    'hosts': [
        {'role': 'master', 'name': 'master1.ipa.test', 'address': '10.0.0.1',
         'ip': '192.168.100.10'},
        {'role': 'ad', 'name': 'dc1.ad.example', 'address': '10.0.0.2',
         'ip': '192.168.100.11', 'os': 'samba'},
    ],
}


def test_an_ad_host_is_named_in_the_ad_domain():
    spec = EnvSpec.from_dict(dict(DOC, hosts=[
        {'role': 'master', 'name': 'master1'},
        {'role': 'ad', 'name': 'dc1', 'os': 'samba'}]))
    master, dc = spec.hosts
    assert spec.host_fqdn(master) == 'master1.ipa.test'
    assert spec.host_fqdn(dc) == 'dc1.ad.example'
    assert dc.is_ad and not master.is_ad


def test_the_config_puts_a_samba_dc_in_its_own_ad_domain(tmp_path):
    provider = ExternalProvider(EnvSpec.from_dict(DOC), str(tmp_path))
    provider._write_config()
    cfg = yaml.safe_load((tmp_path / 'ipa-test-config.yaml').read_text())
    ipa, ad = cfg['domains']
    assert (ipa['name'], ipa['type']) == ('ipa.test', 'IPA')
    assert [h['role'] for h in ipa['hosts']] == ['master']
    assert (ad['name'], ad['type']) == ('ad.example', 'AD')
    # the host_type makes the harness drive the DC with samba-tool
    assert ad['hosts'] == [{'name': 'dc1.ad.example', 'role': 'ad',
                            'ip': '192.168.100.11', 'host_type': 'samba'}]
    assert 'host_type' not in ipa['hosts'][0]


def test_a_windows_dc_gets_no_host_type(tmp_path):
    doc = dict(DOC)
    doc['hosts'] = [DOC['hosts'][0], dict(DOC['hosts'][1], os='windows')]
    ExternalProvider(EnvSpec.from_dict(doc), str(tmp_path))._write_config()
    cfg = yaml.safe_load((tmp_path / 'ipa-test-config.yaml').read_text())
    assert 'host_type' not in cfg['domains'][1]['hosts'][0]


def test_the_dc_script_is_valid_shell_and_keeps_a_posix_user_without_gid():
    script = samba_ad.script('ad.test', 'AD', '192.168.100.11', '8.8.8.8')
    subprocess.run(['bash', '-n'], input=script, text=True, check=True)
    lines = script.splitlines()
    create = next(l for l in lines if l.startswith(
        'samba-tool user create testuser1 '))
    # samba-tool refuses a uid without a gid: ldbmodify sets the attributes
    assert '--uid-number' not in create
    assert 'uidNumber: 10050' in script
    # a POSIX user with both gets them from samba-tool, with the NIS domain
    full = next(l for l in lines if l.startswith(
        'samba-tool user create testuser '))
    assert '--uid-number 10042' in full and '--gid-number 10047' in full
    assert '--nis-domain ad' in full
    assert 'bind interfaces only = yes' in script
