import json
import os

import pytest

from freeipa_env.envspec import EnvSpec
from freeipa_env.nested_provider import NestedError, NestedProvider

PRESET = os.path.join(os.path.dirname(__file__), '..', 'presets',
                      'nested-qemu-bootc.yaml')


def test_a_second_up_is_refused_instead_of_orphaning_the_first_vms(tmp_path):
    (tmp_path / 'nested-state.json').write_text(json.dumps(
        {'vms': [{'id': 'freeipa-ci-abc123'}], 'remote_workdir': '/x'}))
    prov = NestedProvider(EnvSpec.from_file(PRESET), str(tmp_path))
    with pytest.raises(NestedError, match='freeipa-ci-abc123.*down'):
        prov.up()
    # nothing was provisioned or overwritten
    assert json.loads((tmp_path / 'nested-state.json').read_text())[
        'vms'] == [{'id': 'freeipa-ci-abc123'}]
