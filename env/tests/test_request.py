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
