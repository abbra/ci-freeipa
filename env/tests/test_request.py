import os

import pytest
import yaml

from freeipa_env import request


def write(tmp_path, name, text):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return str(p)


def test_a_request_must_name_something_to_run(tmp_path):
    with pytest.raises(request.RequestError, match='presets'):
        request.load(write(tmp_path, 'r.yaml', 'build: false\n'))


def test_an_unknown_key_is_an_error_not_ignored(tmp_path):
    with pytest.raises(request.RequestError, match=r"unknown keys \['preset'\]"):
        request.load(write(tmp_path, 'r.yaml', 'preset: x.yaml\n'))


def test_the_checkout_is_built_unless_the_request_says_not(tmp_path):
    req = request.load(write(tmp_path, 'r.yaml', 'presets: [a.yaml]\n'))
    assert req['build'] is True
    req = request.load(write(tmp_path, 'r.yaml',
                             'build: false\npresets: [a.yaml]\n'))
    assert req['build'] is False


def test_a_preset_in_the_checkout_wins_over_the_one_of_ci_freeipa(tmp_path):
    src = tmp_path / 'src'
    write(src, 'presets/qemu-smoke.yaml', 'name: mine\n')
    req = {'presets': ['presets/qemu-smoke.yaml']}
    [(key, path, tests)] = request.jobs(req, str(src))
    assert path == str(src / 'presets/qemu-smoke.yaml')
    assert (key, tests) == ('qemu-smoke', None)


def test_a_preset_of_ci_freeipa_is_found_relative_to_env(tmp_path):
    req = {'presets': [{'preset': 'presets/qemu-smoke.yaml',
                        'tests': ['test_x.py::T']}]}
    [(key, path, tests)] = request.jobs(req, str(tmp_path))
    assert path.endswith(os.path.join('env', 'presets', 'qemu-smoke.yaml'))
    assert tests == ['test_x.py::T']


def test_a_preset_that_exists_nowhere_is_an_error(tmp_path):
    with pytest.raises(request.RequestError, match='nope.yaml: not in'):
        request.jobs({'presets': ['nope.yaml']}, str(tmp_path))


def test_two_presets_with_one_name_would_share_a_workdir(tmp_path):
    src = tmp_path
    write(src, 'a/p.yaml', 'name: a\n')
    write(src, 'b/p.yaml', 'name: b\n')
    with pytest.raises(request.RequestError, match='more than one'):
        request.jobs({'presets': ['a/p.yaml', 'b/p.yaml']}, str(src))


def test_a_queue_is_expanded_into_its_presets(tmp_path):
    req = {'queue': 'queues/qemu-copr.yaml'}
    keys = [k for k, _p, _t in request.jobs(req, str(tmp_path))]
    assert keys == ['qemu-copr-customized_ds_config_install']


def test_nested_preset_gets_the_srpm_of_the_checkout():
    doc = {'provider': 'nested', 'vm': {'backend': 'qemu'}}
    out = request.adapt(doc, '/src/dist/srpms')
    assert out['vm']['srpms'] == ['/src/dist/srpms/*.src.rpm']
    assert 'srpms' not in doc['vm']          # the preset itself is not changed


def test_nested_preset_gets_the_rpms_built_once_in_podman():
    doc = {'provider': 'nested', 'vm': {'backend': 'qemu'}}
    out = request.adapt(doc, '/src/dist/srpms', rpms_dir='/work/ipa-rpms')
    assert out['vm']['rpms'] == ['/work/ipa-rpms/*.rpm']
    assert 'srpms' not in out['vm']


def test_rpms_alone_are_enough_for_a_nested_preset():
    # tf-runner.sh has the RPMs and no SRPM directory to give
    out = request.adapt({'provider': 'nested', 'vm': {}}, rpms_dir='/r')
    assert out['vm']['rpms'] == ['/r/*.rpm']


def test_a_podman_preset_ignores_the_rpms():
    assert 'build' not in request.adapt({'name': 'x'}, rpms_dir='/r')


def test_testing_farm_accepts_a_qemu_preset_it_builds_for(
        tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(request.subprocess, 'run', lambda argv: (
        seen.append(argv), type('R', (), {'returncode': 0})())[1])
    assert _tf(tmp_path, {'presets': ['presets/qemu-smoke.yaml']}) == 0
    assert seen


def test_podman_preset_gets_a_build_block():
    assert request.adapt({'name': 'x'}, '/s')['build'] == {'srpm': '/s'}


def test_a_preset_that_names_its_own_build_keeps_it():
    nested = {'provider': 'nested', 'vm': {'rpms': ['/mine/*.rpm']}}
    assert request.adapt(nested, '/s')['vm'] == {'rpms': ['/mine/*.rpm']}
    podman = {'build': {'srpm': '/theirs'}}
    assert request.adapt(podman, '/s')['build'] == {'srpm': '/theirs'}


def test_an_external_preset_tests_its_own_ipa():
    doc = {'provider': 'external', 'hosts': []}
    assert request.adapt(doc, '/s') == doc


def test_tests_replace_the_run_selection():
    out = request.adapt({'run': {'tests': ['a'], 'mode': 'integration'}},
                        None, ['b::C'])
    assert out['run'] == {'tests': ['b::C'], 'mode': 'integration'}


def test_the_build_packages_come_from_the_plan_of_the_checkout(tmp_path):
    write(tmp_path, request.PLAN, yaml.safe_dump(
        {'prepare': {'how': 'install', 'package': ['make', 'gcc']}}))
    assert request.plan_packages(str(tmp_path)) == ['make', 'gcc']
    with pytest.raises(request.RequestError, match='no prepare/package'):
        request.plan_packages(str(tmp_path / 'elsewhere'))


def _tf(tmp_path, req, **kw):
    todo = request.jobs(req, str(tmp_path))
    args = dict(repo_url='https://github.com/x/freeipa', ref='a' * 40,
                dry_run=True)
    args.update(kw)
    return request.testing_farm(
        dict({'build': True}, **req), todo, str(tmp_path / 'w'),
        str(tmp_path), args['repo_url'], args['ref'], args['dry_run'])


def test_testing_farm_gets_a_queue_of_the_presets_and_the_commit(
        tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(request.subprocess, 'run', lambda argv: (
        seen.append(argv), type('R', (), {'returncode': 0})())[1])
    req = {'presets': ['presets/prci/gating/test_adtrust_install.yaml']}
    assert _tf(tmp_path, req) == 0
    argv = seen[0]
    assert argv[1:4] == ['queue', 'run', str(tmp_path / 'w/request-queue.yaml')]
    assert argv[argv.index('--tf-ref') + 1] == 'a' * 40
    assert argv[argv.index('--tf-repo-url') + 1] == (
        'https://github.com/x/freeipa')
    assert '--dry-run' in argv
    queue_doc = yaml.safe_load((tmp_path / 'w/request-queue.yaml').read_text())
    assert queue_doc['jobs'] == [
        {'preset': 'presets/prci/gating/test_adtrust_install.yaml'}]


@pytest.mark.parametrize('req, kw, message', [
    ({'presets': [{'preset': 'presets/prci/gating/test_adtrust_install.yaml',
                   'tests': ['x']}]}, {}, '`tests` is not supported'),
    ({'presets': ['presets/prci/gating/test_adtrust_install.yaml']},
     {'ref': None}, '--tf-ref'),
    ({'presets': ['presets/prci/gating/test_adtrust_install.yaml'],
      'build': False}, {}, 'build: false'),
])
def test_testing_farm_refuses_what_it_cannot_run(tmp_path, req, kw, message):
    with pytest.raises(request.RequestError, match=message):
        _tf(tmp_path, req, **kw)


def test_testing_farm_refuses_a_preset_of_the_checkout(tmp_path):
    write(tmp_path, 'mine.yaml', 'name: mine\n')
    with pytest.raises(request.RequestError, match='file of the checkout'):
        _tf(tmp_path, {'presets': ['mine.yaml']})


def test_the_rpms_are_built_for_the_fedora_of_the_vm_image(tmp_path):
    a = write(tmp_path, 'a.yaml', yaml.safe_dump(
        {'vm': {'image': 'containerdisk:ghcr.io/x/fedora-45:latest'}}))
    b = write(tmp_path, 'b.yaml', yaml.safe_dump(
        {'vm': {'image': '~/images/Fedora-Cloud-Base-Generic-45-1.4.qcow2'}}))
    assert request.vm_dist([a, b]) == '45'


def test_vm_images_of_two_releases_need_two_builds(tmp_path):
    a = write(tmp_path, 'a.yaml', yaml.safe_dump(
        {'vm': {'image': 'containerdisk:x/fedora-45:latest'}}))
    b = write(tmp_path, 'b.yaml', yaml.safe_dump(
        {'vm': {'image': '~/Fedora-Cloud-Base-Generic-44-1.7.qcow2'}}))
    with pytest.raises(request.RequestError, match="one release"):
        request.vm_dist([a, b])


def test_an_image_name_without_a_release_gives_none(tmp_path):
    p = write(tmp_path, 'a.yaml', yaml.safe_dump(
        {'vm': {'image': '~/images/some.qcow2'}}))
    assert request.vm_dist([p]) is None


def test_the_summary_has_a_table_and_a_section_per_preset():
    text = request.compose_summary(
        [('a', 0), ('b', 1)], {'a': '# A report\n', 'b': None})
    assert '| `a` | passed |' in text and '| `b` | failed (1) |' in text
    # a failed preset is open on the run page, a passed one is folded
    assert '<details><summary><b>a</b>: passed' in text
    assert '<details open><summary><b>b</b>: failed (1)' in text
    assert '# A report' in text
    assert 'no report' in text


def test_a_report_over_the_limit_is_cut_at_a_line_and_its_fence_closed():
    report = '```\n' + 'x' * 50 + '\n' + 'y' * 50 + '\n```\n'
    cut = request._clip(report, 70)
    assert cut.startswith('```\nxxxx')
    assert 'y' * 10 not in cut
    assert cut.count('```') == 2 and 'report cut' in cut


def test_the_summary_stays_under_the_cap_with_several_big_reports():
    reports = {k: 'line\n' * 100_000 for k in 'abc'}
    text = request.compose_summary(
        [(k, 1) for k in 'abc'], reports, cap=50_000)
    assert len(text) < 50_000
    assert text.count('report cut') == 3
