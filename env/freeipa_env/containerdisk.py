"""`vm.image: containerdisk:REF`: a VM disk delivered by a container registry.

A containerdisk is a minimal image that holds one file, /disk/disk.qcow2:

    FROM scratch
    COPY disk.qcow2 /disk/disk.qcow2

(`images/vm/publish.sh` builds and pushes one.) Using it needs only podman,
no root and no conversion: the image is pulled, the disk copied out and kept
in the image cache, named after the image id so a moved tag is picked up and
an unchanged one is not copied again. REF is anything podman can pull,
including `containers-storage:...` and `oci-archive:...`.
"""
import os
import re
import shutil
import subprocess

PREFIX = 'containerdisk:'
CACHE = os.path.expanduser('~/.cache/freeipa-ci/images/containerdisk')
DISK_IN_IMAGE = '/disk/disk.qcow2'


class ContainerDiskError(Exception):
    pass


def _podman(args):
    try:
        p = subprocess.run(['podman'] + args, capture_output=True, text=True,
                           timeout=3600)
    except FileNotFoundError:
        raise ContainerDiskError('podman not found (needed for a '
                                 'containerdisk: image)')
    if p.returncode:
        raise ContainerDiskError(f'podman {" ".join(args)} failed: '
                                 f'{(p.stderr or p.stdout).strip()[-800:]}')
    return p.stdout.strip()


def resolve(image):
    """The local qcow2 for `image`: a plain path is returned as it is."""
    if not image.startswith(PREFIX):
        return image
    ref = image[len(PREFIX):]
    _podman(['pull', '-q', ref])
    image_id = _podman(['image', 'inspect', '--format', '{{.Id}}', ref])[:12]
    dest = os.path.join(
        CACHE, f'{re.sub(r"[^A-Za-z0-9._-]+", "_", ref)}-{image_id}.qcow2')
    if os.path.isfile(dest):
        return dest
    os.makedirs(CACHE, exist_ok=True)
    cid = _podman(['create', ref, DISK_IN_IMAGE])   # scratch: no command runs
    try:
        _podman(['cp', f'{cid}:{DISK_IN_IMAGE}', dest + '.partial'])
    except ContainerDiskError as e:
        raise ContainerDiskError(f'{ref} has no {DISK_IN_IMAGE}: {e}')
    finally:
        subprocess.run(['podman', 'rm', '-f', cid], capture_output=True)
    if not shutil.which('qemu-img'):
        raise ContainerDiskError('qemu-img not found')
    info = subprocess.run(['qemu-img', 'info', dest + '.partial'],
                          capture_output=True, text=True)
    if info.returncode or 'file format: qcow2' not in info.stdout:
        os.unlink(dest + '.partial')
        raise ContainerDiskError(f'{ref}: {DISK_IN_IMAGE} is not a qcow2 file')
    os.replace(dest + '.partial', dest)
    return dest
