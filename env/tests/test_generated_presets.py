import os

from freeipa_env import blend, queue, runner

PRESET = 'presets/base-xmlrpc.yaml'
PROVIDER = 'presets/nested-qemu-external.yaml'


def use_cache(monkeypatch, path):
    for mod in (queue, blend, runner._queue):
        monkeypatch.setattr(mod, 'GENERATED', str(path))


def test_blend_writes_to_the_cache_and_a_queue_still_finds_its_presets(
        tmp_path, monkeypatch):
    use_cache(monkeypatch, tmp_path / 'gen')
    base_queue = tmp_path / 'base.yaml'
    base_queue.write_text(f'name: b\njobs:\n  - preset: {PRESET}\n')
    kind, out = blend.blend(str(base_queue), PROVIDER, 'vmq')
    assert kind == 'queue' and out == str(tmp_path / 'gen' / 'vmq.yaml')
    spec = queue.QueueSpec.from_file(out)
    job = spec.jobs[0]
    # the queue names the preset relative to env/, as the runners expect
    assert job.preset_rel.startswith('generated/vmq/')
    assert job.path.startswith(str(tmp_path / 'gen'))
    assert os.path.isfile(job.path)
    # nothing was written into the repository
    assert not os.path.exists(os.path.join(queue.ENVROOT, 'generated', 'vmq'))


def test_a_copy_under_env_wins_over_the_cache(tmp_path, monkeypatch):
    use_cache(monkeypatch, tmp_path / 'gen')
    (tmp_path / 'gen' / 'x').mkdir(parents=True)
    (tmp_path / 'gen' / 'x' / 'a.yaml').write_text('name: cached\n')
    monkeypatch.setattr(queue, 'ENVROOT', str(tmp_path / 'env'))
    assert queue.preset_path('generated/x/a.yaml') == str(
        tmp_path / 'gen' / 'x' / 'a.yaml')
    (tmp_path / 'env' / 'generated' / 'x').mkdir(parents=True)
    (tmp_path / 'env' / 'generated' / 'x' / 'a.yaml').write_text('x')
    assert queue.preset_path('generated/x/a.yaml') == str(
        tmp_path / 'env' / 'generated' / 'x' / 'a.yaml')


def test_a_runner_gets_the_generated_presets_in_its_copy_of_the_tree(
        tmp_path, monkeypatch):
    use_cache(monkeypatch, tmp_path / 'gen')
    (tmp_path / 'gen' / 'q').mkdir(parents=True)
    (tmp_path / 'gen' / 'q' / 'job.yaml').write_text('name: job\n')
    remote_ci = tmp_path / 'remote-ci'
    runner.LocalRunner().ship_generated(str(remote_ci))
    assert (remote_ci / 'env' / 'generated' / 'q' / 'job.yaml').read_text(
    ) == 'name: job\n'
