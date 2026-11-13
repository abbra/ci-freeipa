"""The container-detection and fallbacks of freeipa_env.container,
and the skopeo containerdisk path that uses them."""
import os
import shutil
import stat
import subprocess
import tarfile
from io import BytesIO

import pytest

from freeipa_env import container as cm
from freeipa_env import containerdisk as cd

needs_dnf = pytest.mark.skipif(not shutil.which('dnf'),
                               reason='dnf download needed')
needs_skopeo = pytest.mark.skipif(not shutil.which('skopeo'),
                                  reason='skopeo needed')


def test_markers_are_detected(monkeypatch, tmp_path):
    monkeypatch.delenv('container', raising=False)
    monkeypatch.setattr(cm, '_pid1_env', lambda: {})
    monkeypatch.setattr(cm, '_MARKERS', ())
    assert not cm.in_container()
    monkeypatch.setattr(cm, '_MARKERS', (str(tmp_path / 'x'),))
    assert not cm.in_container()
    (tmp_path / 'x').write_text('')
    assert cm.in_container()
    monkeypatch.setattr(cm, '_MARKERS', ())
    monkeypatch.setenv('container', 'toolbox')
    assert cm.in_container()


def test_a_fetched_tool_runs_and_is_cached(tmp_path, monkeypatch):
    """_seed_tool wraps a real Fedora package: qemu-img, or any other."""
    if not shutil.which('dnf'):
        pytest.skip('dnf download needed')
    monkeypatch.setenv('FREEIPA_CI_TOOL_BIN', str(tmp_path))
    monkeypatch.setattr(cm, 'shutil', cm.shutil)   # keep the real module
    real_which = shutil.which
    monkeypatch.setattr(
        cm.shutil, 'which',
        lambda t: None if t == 'qemu-img' else real_which(t))
    got = cm._seed_tool('qemu-img')
    assert got == str(tmp_path / 'qemu-img')
    # the wrapper is executable and runs the real binary
    assert os.stat(got).st_mode & stat.S_IXUSR
    out = subprocess.run([got, '--version'], capture_output=True, text=True,
                         check=False)
    assert out.returncode == 0 and 'qemu-img version' in out.stdout
    # a second call does not re-fetch: the wrapper is already there
    mtime = os.stat(got).st_mtime_ns
    assert cm._seed_tool('qemu-img') == got
    assert os.stat(got).st_mtime_ns == mtime


@needs_skopeo
def test_the_skopeo_path_extracts_the_disk(tmp_path, monkeypatch):
    """A containerdisk resolved while in a container never runs podman."""
    monkeypatch.setenv('FREEIPA_CI_TOOL_BIN', str(tmp_path / 'bin'))
    monkeypatch.setattr(cd, 'CACHE', str(tmp_path / 'cache'))
    monkeypatch.setattr(cm, 'in_container', lambda: True)
    monkeypatch.setattr(cm, 'prepare_tools', list)

    def no_podman(args):
        raise AssertionError('podman must not run inside a container')

    monkeypatch.setattr(cd, '_podman', no_podman)
    ref = 'ghcr.io/freeipa/ci-freeipa-vm-images/fedora-45:latest'
    disk = cd.resolve(f'containerdisk:{ref}')
    assert disk.startswith(str(tmp_path / 'cache'))
    # qcow2 magic
    with open(disk, 'rb') as f:
        assert f.read(4) == b'QFI\xfb'
    # a second resolve is a cache hit with the same name
    again = cd.resolve(f'containerdisk:{ref}')
    assert again == disk


def test_skopeo_disk_rejects_a_layer_without_the_disk(tmp_path):
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as t:
        data = b'other'
        ti = tarfile.TarInfo('other')
        ti.size = len(data)
        t.addfile(ti, BytesIO(data))
    layer = tmp_path / 'layer.tar.gz'
    layer.write_bytes(buf.getvalue())
    with pytest.raises(cd.ContainerDiskError, match='has no /disk/disk.qcow2'):
        cd._skopeo_disk(str(layer), str(tmp_path / 'out'))
