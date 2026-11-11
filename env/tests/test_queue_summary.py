import os

from freeipa_env import cli
from freeipa_env.envspec import EnvSpec

ENV = os.path.join(os.path.dirname(__file__), '..')
JOBS = ['presets/base-xmlrpc.yaml', 'presets/nested-qemu-bootc.yaml']
XML = ('<testsuite><testcase classname="a.B" name="ok"/>'
       '<testcase classname="a.B" name="bad"><failure>E  boom</failure>'
       '</testcase></testsuite>')


def test_queue_summary_adds_up_the_jobs_that_left_a_report(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    queue = tmp_path / 'q.yaml'
    queue.write_text('name: q\njobs:\n' + ''.join(
        f'  - preset: {os.path.join(ENV, j)}\n' for j in JOBS))
    # job 1 ran and was torn down: its report sits under logs/collected
    wd = cli.default_workdir(EnvSpec.from_file(os.path.join(ENV, JOBS[0])))
    host = os.path.join(wd, 'logs', 'collected', 'h')
    os.makedirs(host)
    with open(os.path.join(host, 'nosetests.xml'), 'w') as f:
        f.write(XML)
    rc = cli._queue_summary(str(queue), 6)
    out = capsys.readouterr().out
    assert rc == 0
    assert '== total: 1 FAIL, 1 ok; 1 job(s) without a report' in out
    assert out.count('no nosetests.xml') == 1
