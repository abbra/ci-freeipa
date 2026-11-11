"""Run the tests that a contributor asks for in a FreeIPA checkout.

A "temp commit" is a commit that adds `.ci-freeipa.yaml` at the root of the
FreeIPA tree, like PRCI's `.freeipa-pr-ci.yaml` symlink. The file names the
presets, or a queue, to run:

    build: true                 # default: build FreeIPA from this checkout
    presets:
      - presets/qemu-smoke.yaml
      - preset: presets/prci/gating/test_adtrust_install.yaml
        tests: [test_integration/test_adtrust_install.py]
    queue: queues/qemu-copr.yaml

A path is a file of the checkout when the checkout has one (a contributor can
add a preset in the same commit), else a file of this repository, relative
to `env/` (a queue: to the repository root). `freeipa-env request FILE
--freeipa-src DIR` runs them one after the other on this machine; the GitHub
workflow of the FreeIPA tree calls it on the runner of the contributor's fork.
"""
import copy
import glob
import os
import shlex
import subprocess
import sys

import yaml

from . import queue

KEYS = {'build', 'build_image', 'presets', 'queue'}
CI_ROOT = os.path.dirname(queue.ENVROOT)
PLAN = 'ci/tmt/plans/freeipa-env.fmf'


class RequestError(Exception):
    pass


def load(path):
    """The request in `path`, checked"""
    with open(path) as f:
        doc = yaml.safe_load(f) or {}
    if not isinstance(doc, dict):
        raise RequestError(f'{path}: not a mapping')
    unknown = set(doc) - KEYS
    if unknown:
        raise RequestError(f'{path}: unknown keys {sorted(unknown)}')
    if not doc.get('presets') and not doc.get('queue'):
        raise RequestError(f'{path}: name `presets` or a `queue` to run')
    if not isinstance(doc.get('presets') or [], list):
        raise RequestError(f'{path}: presets must be a list')
    doc.setdefault('build', True)
    return doc


def _find(rel, src, base):
    """`rel` in the checkout if it has it, else under `base`"""
    for root in (src, base):
        if root and os.path.isfile(os.path.join(root, rel)):
            return os.path.join(root, rel)
    raise RequestError(f'{rel}: not in the FreeIPA checkout or in ci-freeipa')


def jobs(req, src):
    """[(key, preset file, tests or None)] in the order they run"""
    out = []
    if req.get('queue'):
        for job in queue.QueueSpec.from_file(
                _find(req['queue'], src, CI_ROOT)).jobs:
            out.append((job.key, job.path, None))
    for entry in req.get('presets') or []:
        rel, tests = (entry, None) if isinstance(entry, str) else (
            entry.get('preset'), entry.get('tests'))
        if not rel:
            raise RequestError(f'preset entry without `preset`: {entry!r}')
        out.append((os.path.splitext(os.path.basename(rel))[0],
                    _find(rel, src, queue.ENVROOT), tests))
    seen = set()
    for key, _path, _tests in out:
        if key in seen:
            raise RequestError(f'{key}: more than one preset of that name')
        seen.add(key)
    return out


def adapt(doc, srpm_dir=None, tests=None, rpms_dir=None):
    """A copy of a preset that runs the given tests and, unless the preset
    names its own build, installs the FreeIPA built from the checkout: the
    RPMs in `rpms_dir` on the VMs of a nested preset (built once, not on
    every VM), the SRPM for a podman preset"""
    doc = copy.deepcopy(doc)
    if tests:
        doc.setdefault('run', {})['tests'] = list(tests)
    provider = doc.get('provider', 'podman')
    if provider == 'nested' and (rpms_dir or srpm_dir):
        vm = doc.setdefault('vm', {})
        if not (vm.get('srpms') or vm.get('rpms')):
            if rpms_dir:
                vm['rpms'] = [os.path.join(rpms_dir, '*.rpm')]
            else:
                vm['srpms'] = [os.path.join(srpm_dir, '*.src.rpm')]
    elif provider == 'podman' and srpm_dir and not doc.get('build'):
        doc['build'] = {'srpm': srpm_dir}
    # an external preset tests its own IPA
    return doc


def plan_packages(src):
    """The BuildRequires that the pin plan of the checkout installs"""
    path = os.path.join(src, PLAN)
    try:
        with open(path) as f:
            return yaml.safe_load(f)['prepare']['package']
    except (OSError, KeyError, TypeError) as e:
        raise RequestError(f'{path}: no prepare/package list ({e})') from e


def build_srpm(src, image):
    """`make srpms` of the checkout in a Fedora container: the runner may
    not be a Fedora, and the plan's package list is what the branch needs"""
    pkgs = ' '.join(shlex.quote(p) for p in plan_packages(src))
    script = (f'dnf -y install {pkgs} && cd /ci && '
              f'./scripts/make-srpms.sh /src')
    print(f'== building the SRPM of {src} in {image}', flush=True)
    rc = subprocess.run(
        ['podman', 'run', '--rm', '-v', f'{src}:/src:Z',
         '-v', f'{CI_ROOT}:/ci:ro,Z', image, 'bash', '-ec', script]).returncode
    found = sorted(glob.glob(os.path.join(src, 'dist', 'srpms', '*.src.rpm')))
    if rc or not found:
        raise RequestError(f'building the SRPM failed (exit {rc})')
    return os.path.join(src, 'dist', 'srpms')


def _step(verb, preset, workdir):
    argv = [os.path.join(queue.ENVROOT, 'freeipa-env'), verb, preset,
            '--workdir', workdir]
    print('== ' + ' '.join(shlex.quote(a) for a in argv), flush=True)
    return subprocess.run(argv).returncode


IMAGE = 'registry.fedoraproject.org/fedora:latest'


def _is_nested(path):
    with open(path) as f:
        return (yaml.safe_load(f) or {}).get('provider') == 'nested'


def build_rpms(srpm_dir, out):
    """The binary RPMs of the SRPM, built in podman (images/build.sh)"""
    print(f'== building the RPMs of {srpm_dir} in podman', flush=True)
    rc = subprocess.run([
        os.path.join(CI_ROOT, 'images', 'build.sh'), '--srpm', srpm_dir,
        '--build-only', '--rpms-out', out, '--tool', 'podman']).returncode
    if rc or not glob.glob(os.path.join(out, '*.rpm')):
        raise RequestError(f'building the RPMs failed (exit {rc})')
    return out


def testing_farm(req, todo, workdir, src, repo_url, ref, dry_run):
    """Submit the request to Testing Farm: `queue run --runner testing-farm`.

    The guest clones the FreeIPA repository under test, builds it, and runs
    the pinned plan of that repository, which finds the presets in the
    ci-freeipa it pins. So only presets of ci-freeipa run there, with the
    tests they name, built from the pushed commit. A qemu preset runs on the
    same guest (the request asks for virtualization): tf-runner.sh builds
    the RPMs in podman and the VMs install them (vm.rpms).
    """
    if not req['build']:
        raise RequestError('build: false is not supported on Testing Farm')
    if not (repo_url and ref):
        raise RequestError('Testing Farm needs --tf-repo-url and --tf-ref: '
                           'the guest clones the commit under test')
    for key, path, tests in todo:
        if os.path.commonpath([path, queue.ENVROOT]) != queue.ENVROOT:
            raise RequestError(
                f'{key}: Testing Farm runs the presets of ci-freeipa; '
                f'{path} is a file of the checkout')
        if tests:
            raise RequestError(f'{key}: `tests` is not supported on '
                               f'Testing Farm')
    os.makedirs(workdir, exist_ok=True)
    qfile = os.path.join(workdir, 'request-queue.yaml')
    with open(qfile, 'w') as f:
        yaml.safe_dump({'name': 'request', 'jobs': [
            {'preset': os.path.relpath(path, queue.ENVROOT)}
            for _k, path, _t in todo]}, f, sort_keys=False)
    argv = [os.path.join(queue.ENVROOT, 'freeipa-env'), 'queue', 'run',
            qfile, '--runner', 'testing-farm', '--freeipa-src', src,
            '--tf-repo-url', repo_url, '--tf-ref', ref,
            '--outdir', os.path.join(workdir, 'queue')]
    if dry_run:
        argv.append('--dry-run')
    print('== ' + ' '.join(shlex.quote(a) for a in argv), flush=True)
    return subprocess.run(argv).returncode


def run(request_file, src, workdir, image=IMAGE, dry_run=False,
        runner='local', tf_repo_url=None, tf_ref=None):
    """Run the request; the exit status is 0 when every preset passed"""
    src = os.path.abspath(src)
    req = load(request_file)
    todo = jobs(req, src)
    if runner == 'testing-farm':
        return testing_farm(req, todo, workdir, src, tf_repo_url, tf_ref,
                            dry_run)
    if dry_run:
        for key, path, tests in todo:
            print(f'{key}: {path}' + (f' tests={tests}' if tests else ''))
        return 0
    os.makedirs(workdir, exist_ok=True)
    srpm_dir = build_srpm(src, req.get('build_image') or image) \
        if req['build'] else None
    # the VMs of a nested preset install RPMs: build them once in podman
    # rather than on every VM (it needs more memory than a test VM has)
    rpms_dir = None
    if srpm_dir and any(_is_nested(p) for _k, p, _t in todo):
        rpms_dir = build_rpms(srpm_dir, os.path.join(workdir, 'ipa-rpms'))
    results = []
    for key, path, tests in todo:
        gen = os.path.join(workdir, f'{key}.preset.yaml')
        with open(path) as f:
            doc = yaml.safe_load(f)
        with open(gen, 'w') as f:
            yaml.safe_dump(adapt(doc, srpm_dir, tests, rpms_dir), f,
                           sort_keys=False)
        wd = os.path.join(workdir, key)
        rc = _step('up', gen, wd)
        if rc == 0:
            rc = _step('run', gen, wd)
        _step('summary', gen, wd)
        _step('down', gen, wd)
        results.append((key, rc))
    print('\n== request finished')
    lines = ['| preset | result |', '|---|---|'] + [
        f'| `{k}` | {"passed" if rc == 0 else f"failed ({rc})"} |'
        for k, rc in results]
    print('\n'.join(lines))
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as f:
            f.write('\n'.join(lines) + '\n')
    return 0 if all(rc == 0 for _k, rc in results) else 1


def main_args(args):
    try:
        return run(args.request, args.freeipa_src, args.workdir,
                   dry_run=args.dry_run, runner=args.runner,
                   tf_repo_url=args.tf_repo_url, tf_ref=args.tf_ref)
    except RequestError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
