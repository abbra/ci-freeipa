import shlex

from freeipa_env import guest_packages as gp


def dnf_pkgs(cmd):
    """Package names from the `dnf -y install` step, in order."""
    for step in cmd.split(' && '):
        if step.startswith('dnf -y install '):
            return shlex.split(step)[3:]
    return []


def test_set_expands_to_its_packages():
    assert dnf_pkgs(gp.install_cmd(['gpu'])) == gp.SETS['gpu']


def test_plain_name_is_added_next_to_a_set():
    pkgs = dnf_pkgs(gp.install_cmd(['ipa-distro', 'tmux', 'htop']))
    assert 'tmux' in pkgs and 'htop' in pkgs
    assert set(gp.SETS['ipa-distro']) <= set(pkgs)


def test_minus_name_drops_a_package_from_a_set():
    pkgs = dnf_pkgs(gp.install_cmd(['ipa-distro', '-firewalld']))
    assert 'firewalld' not in pkgs
    assert 'freeipa-server' in pkgs


def test_minus_entry_works_before_the_set_it_removes():
    pkgs = dnf_pkgs(gp.install_cmd(['-egl-utils', 'gpu']))
    assert pkgs == ['mesa-dri-drivers']


def test_minus_set_drops_every_package_of_that_set():
    assert dnf_pkgs(gp.install_cmd(['gpu', '-gpu'])) == []


def test_pip_step_is_kept_when_only_rpm_is_subtracted():
    cmd = gp.install_cmd(['ipa-distro', '-python3-pip'])
    assert 'pytest-html==4.2.0' in cmd


def test_no_dnf_step_when_everything_is_removed():
    cmd = gp.install_cmd(['gpu', '-mesa-dri-drivers', '-egl-utils'])
    assert dnf_pkgs(cmd) == []


def test_duplicate_packages_are_installed_once():
    pkgs = dnf_pkgs(gp.install_cmd(['gpu', 'mesa-dri-drivers']))
    assert pkgs.count('mesa-dri-drivers') == 1
