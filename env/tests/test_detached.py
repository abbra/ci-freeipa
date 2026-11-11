import time

import pytest

from freeipa_env.detached import LocalDetached


def wait_finished(det, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        state = det.detached_state()
        if state[0] == 'finished':
            return state
        time.sleep(0.05)
    raise AssertionError('did not finish')


def test_exit_status_and_output_outlive_the_starter(tmp_path):
    det = LocalDetached(str(tmp_path))
    assert det.detached_state() == ('none', None)
    det.start(['sh', '-c', 'echo hello; exit 3'])
    assert wait_finished(det) == ('finished', 3)
    assert det.detached_tail(5) == 'hello\n'


def test_a_second_start_is_refused_while_one_runs(tmp_path):
    det = LocalDetached(str(tmp_path))
    det.start(['sleep', '5'])
    try:
        assert det.detached_state()[0] == 'running'
        with pytest.raises(RuntimeError, match='already in progress'):
            det.start(['true'])
    finally:
        pid = int((tmp_path / 'detached' / 'pid').read_text())
        import os
        import signal
        os.kill(pid, signal.SIGKILL)
