import os

from freeipa_env import blend

ENV = os.path.join(os.path.dirname(__file__), '..')
BASE = blend._doc(os.path.join(ENV, 'presets/base-xmlrpc.yaml'))
VM = {'backend': 'qemu', 'image': '/x.qcow2'}


def test_a_result_that_runs_in_qemu_vms_does_not_carry_the_podman_image():
    assert BASE['image']          # the test names a channel
    doc = blend._blend_preset(
        BASE, {'provider': 'nested', 'inner': 'external', 'vm': VM}, 'x',
        None)
    assert 'image' not in doc


def test_a_podman_inner_result_keeps_the_image():
    doc = blend._blend_preset(
        BASE, {'provider': 'nested', 'inner': 'podman', 'vm': VM}, 'x', None)
    assert doc['image'] == BASE['image']


def test_an_image_the_provider_setup_names_is_kept():
    doc = blend._blend_preset(
        BASE, {'provider': 'nested', 'inner': 'external', 'vm': VM,
               'image': 'freeipa-next'}, 'x', None)
    assert doc['image'] == 'freeipa-next'
