from types import SimpleNamespace as NS

import pytest

from freeipa_env import guest_settings as gs

HOSTS = [NS(name='master1'), NS(name='replica1')]


def run_all(cfg):
    calls = []
    done = gs.apply(cfg, HOSTS, lambda h, c, t: calls.append((h.name, c)))
    return done, calls


def test_copr_is_enabled_before_the_packages_are_installed():
    _, calls = run_all({'copr': ['abbra/pqc'], 'guest_packages': ['gpu']})
    cmds = [c for h, c in calls if h == 'master1']
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


def test_script_lines_run_as_one_shell_with_errexit():
    _, calls = run_all({'script': ['a', 'b']})
    assert calls[0][1].startswith('bash -ec ')
