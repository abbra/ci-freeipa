from freeipa_env import testingfarm as tf
from freeipa_env.queue import QueueJob

CFG = {'repo_url': 'https://example.invalid/ci.git', 'ref': 'main',
       'plan': '/tmt/plans/qemu'}


def request(*presets):
    return tf.build_tf_request(CFG, [QueueJob(p) for p in presets], 600)


def test_a_nested_qemu_job_asks_for_kvm_and_the_memory_of_its_vms():
    env = request('presets/qemu-copr-customized_ds_config_install.yaml'
                  )['environments'][0]
    assert env['hardware']['virtualization'] == {'is-supported': True}
    # two hosts of 3300 MiB, and 4 GiB for the guest
    assert env['hardware']['memory'] == '>= 11 GiB'


def test_a_podman_job_asks_for_no_special_hardware():
    env = request('presets/base-xmlrpc.yaml')['environments'][0]
    assert 'hardware' not in env


def test_a_mixed_batch_gets_the_hardware_of_its_qemu_jobs():
    env = request('presets/base-xmlrpc.yaml',
                  'presets/qemu-copr-customized_ds_config_install.yaml'
                  )['environments'][0]
    assert env['hardware']['virtualization']['is-supported']
