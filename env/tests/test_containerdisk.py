import hashlib
import os
import shutil
import subprocess
import uuid

import pytest

from freeipa_env import container as container_mode
from freeipa_env import containerdisk as cd

ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
PUBLISH = os.path.join(ROOT, 'images', 'vm', 'publish.sh')
needs_podman = pytest.mark.skipif(
    not (shutil.which('podman') and shutil.which('qemu-img')
         and not (container_mode.in_container()
                  and subprocess.run(
                      ['podman', 'info'], capture_output=True
                  ).returncode != 0)),
    reason='a working podman and qemu-img needed (a control node that is '
           'a container has no podman; see freeipa_env.container)')


def sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cd, 'CACHE', str(tmp_path / 'cache'))
    tags = []
    yield tags
    for t in tags:
        subprocess.run(['podman', 'rmi', '-f', t], capture_output=True)


def local_ref(tags):
    ref = f'localhost/containerdisk-test-{uuid.uuid4().hex[:8]}:1'
    tags.append(ref)
    return ref


def test_a_plain_path_is_left_alone():
    assert cd.resolve('/x/disk.qcow2') == '/x/disk.qcow2'


@needs_podman
def test_a_published_disk_comes_back_byte_for_byte_and_is_cached(
        tmp_path, cache):
    disk = tmp_path / 'disk.qcow2'
    subprocess.run(['qemu-img', 'create', '-q', '-f', 'qcow2', str(disk),
                    '8M'], check=True)
    ref = local_ref(cache)
    subprocess.run([PUBLISH, str(disk), ref], check=True,
                   capture_output=True)
    first = cd.resolve(f'containerdisk:containers-storage:{ref}')
    assert sha(first) == sha(disk)
    stamp = os.stat(first).st_mtime_ns
    again = cd.resolve(f'containerdisk:containers-storage:{ref}')
    assert again == first and os.stat(again).st_mtime_ns == stamp


@needs_podman
def test_an_image_whose_disk_is_not_a_qcow2_is_rejected(tmp_path, cache):
    (tmp_path / 'disk.qcow2').write_text('not a disk')
    (tmp_path / 'Containerfile').write_text(
        'FROM scratch\nCOPY disk.qcow2 /disk/disk.qcow2\n')
    ref = local_ref(cache)
    subprocess.run(['podman', 'build', '-q', '-t', ref, str(tmp_path)],
                   check=True, capture_output=True)
    with pytest.raises(cd.ContainerDiskError, match='not a qcow2'):
        cd.resolve(f'containerdisk:containers-storage:{ref}')
    assert not os.listdir(cd.CACHE)       # no partial file is left


@needs_podman
def test_an_image_without_the_disk_says_so(tmp_path, cache):
    (tmp_path / 'other').write_text('x')
    (tmp_path / 'Containerfile').write_text(
        'FROM scratch\nCOPY other /other\n')
    ref = local_ref(cache)
    subprocess.run(['podman', 'build', '-q', '-t', ref, str(tmp_path)],
                   check=True, capture_output=True)
    with pytest.raises(cd.ContainerDiskError, match='has no /disk/disk.qcow2'):
        cd.resolve(f'containerdisk:containers-storage:{ref}')
