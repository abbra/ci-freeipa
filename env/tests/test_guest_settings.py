from types import SimpleNamespace as NS

import pytest

from freeipa_env import guest_settings as gs

HOSTS = [NS(name='master1'), NS(name='replica1')]


def run_all(cfg):
    calls = []
    done = gs.apply(cfg, HOSTS,
                    lambda h, c, t: calls.append((h.name, 'run', c)),
                    lambda h, local, remote: calls.append(
                        (h.name, 'put', remote)))
    return done, calls


def test_copr_is_enabled_before_the_packages_are_installed():
    _, calls = run_all({'copr': ['abbra/pqc'], 'guest_packages': ['gpu']})
    cmds = [c for h, _, c in calls if h == 'master1']
    assert 'copr enable abbra/pqc' in cmds[0]
    assert 'dnf -y install' in cmds[1]


def test_vms_entry_overrides_the_top_level_value_for_that_host_only():
    done, _ = run_all({
        'selinux': 'permissive',
        'vms': [{'name': 'replica1', 'selinux': 'enforcing'}]})
    assert done == {'master1': {'selinux': 'permissive'},
                    'replica1': {'selinux': 'enforcing'}}


def test_value_outside_the_allowed_list_names_the_host_and_setting():
    with pytest.raises(ValueError, match='selinux for master1'):
        run_all({'selinux': 'off'})


def test_vms_entry_with_a_short_name_matches_a_fully_qualified_host():
    hosts = [NS(name='master1.ipa.test'), NS(name='replica1.ipa.test')]
    done = gs.apply({'selinux': 'permissive',
                     'vms': [{'name': 'replica1', 'selinux': 'enforcing'}]},
                    hosts, lambda *a: None, lambda *a: None)
    assert done['replica1.ipa.test'] == {'selinux': 'enforcing'}
    assert done['master1.ipa.test'] == {'selinux': 'permissive'}


def test_script_lines_run_as_one_shell_with_errexit():
    _, calls = run_all({'script': ['a', 'b']})
    assert calls[0][2].startswith('bash -ec ')


def test_rpms_are_uploaded_before_they_are_installed(tmp_path):
    (tmp_path / 'a.rpm').write_text('x')
    (tmp_path / 'b.rpm').write_text('x')
    _, calls = run_all({'rpms': [str(tmp_path / '*.rpm')]})
    mine = [c for c in calls if c[0] == 'master1']
    assert [k for _, k, _ in mine] == ['run', 'put', 'put', 'run']
    assert mine[1][2] == f'{gs.UPLOADS}/rpms/a.rpm'
    install, reinstall = mine[3][2].split(' && ')
    assert install == f'dnf -y install {gs.UPLOADS}/rpms/*.rpm'
    assert reinstall == f'dnf -y reinstall {gs.UPLOADS}/rpms/*.rpm'


def test_a_file_setting_that_matches_nothing_fails_before_any_command(
        tmp_path):
    with pytest.raises(ValueError, match='rpms for master1: no file'):
        run_all({'rpms': [str(tmp_path / '*.rpm')]})
