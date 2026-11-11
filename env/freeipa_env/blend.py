"""`freeipa-env blend`: combine a base preset or queue, a provider setup and a
test selection into ordinary preset / queue files under
~/.cache/freeipa-ci/generated/.

The generated files are plain presets and queues, so the run is just
`freeipa-env run ~/.cache/freeipa-ci/generated/NAME.yaml` or
`freeipa-env queue run ~/.cache/freeipa-ci/generated/NAME.yaml --runner ...`.
A queue refers to its presets as generated/NAME/JOB.yaml, relative to env/;
local runs find them in the cache and the runners get a copy in env/generated.

Merge rule: the provider setup's top-level keys replace the base's keys
(provider, inner, vm, image, dist, domain, ...), except `name`, `run`,
`hosts` and `resources`, which always come from the base (the test).
`run` comes from the base, with `tests` replaced by --tests when given.
"""

import copy
import os

import yaml

from .envspec import EnvSpec, EnvSpecError
from . import queue as _queue
from .queue import ENVROOT, QueueSpec

# outputs, not sources: kept in the cache, shipped to runners (see queue.py)
GENERATED = _queue.GENERATED
PROVIDER_SKIP = ('name', 'run', 'hosts', 'resources')


def _doc(path):
    # env-relative (presets/...) or cwd-relative, like the other commands
    full = path if os.path.exists(path) else os.path.join(ENVROOT, path)
    with open(full) as f:
        d = yaml.safe_load(f)
    if not isinstance(d, dict):
        raise EnvSpecError(f'{path}: not a mapping')
    return d


def _blend_preset(base, provider, name, tests):
    doc = copy.deepcopy(base)
    doc.update(copy.deepcopy(
        {k: v for k, v in provider.items() if k not in PROVIDER_SKIP}))
    # the test's channel names a podman image; a result that runs in VMs or on
    # external hosts never reads it (unless the provider setup sets one)
    if 'image' not in provider and (
            doc.get('provider') == 'external'
            or (doc.get('provider') == 'nested'
                and doc.get('inner') == 'external')):
        doc.pop('image', None)
    doc['name'] = name
    run = dict(base.get('run') or {})
    if tests:
        run['tests'] = list(tests)
    doc['run'] = run
    EnvSpec.from_dict(copy.deepcopy(doc))  # fail before anything is written
    return doc


def _write(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        yaml.safe_dump(doc, f, sort_keys=False)


def blend(base_path, provider_path, name, tests=None):
    """Write the generated preset or queue; return (kind, path)."""
    base = _doc(base_path)
    provider = _doc(provider_path)
    out = os.path.join(GENERATED, f'{name}.yaml')
    if 'jobs' not in base:
        _write(out, _blend_preset(base, provider, name, tests))
        return 'preset', out
    q = QueueSpec.from_file(os.path.join(ENVROOT, base_path)
                            if not os.path.exists(base_path) else base_path)
    jobs = []
    for j in q.jobs:
        rel = os.path.join('generated', name, f'{j.key}.yaml')
        _write(os.path.join(GENERATED, name, f'{j.key}.yaml'),
               _blend_preset(_doc(j.path), provider, f'{name}-{j.key}',
                             tests))
        entry = {'preset': rel}
        if j.note:
            entry['note'] = j.note
        jobs.append(entry)
    _write(out, {'name': name, 'jobs': jobs})
    return 'queue', out
