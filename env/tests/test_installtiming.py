import sqlite3

from freeipa_env import installtiming as it

LOG = """\
2026-11-11T10:00:00Z DEBUG Logging to /var/log/ipaserver-install.log
2026-11-11T10:00:00Z DEBUG IPA version 4.14.0
2026-11-11T10:00:00Z DEBUG Configuring directory server (dirsrv). Estimated time: 30 seconds
2026-11-11T10:00:00Z DEBUG   [1/2]: creating instance
2026-11-11T10:00:20Z DEBUG step duration: dirsrv __create 20.00 sec
2026-11-11T10:00:20Z DEBUG   [2/2]: starting
2026-11-11T10:01:20Z DEBUG step duration: dirsrv __start 60.00 sec
2026-11-11T10:01:20Z DEBUG Done configuring directory server (dirsrv).
2026-11-11T10:01:20Z INFO The ipa-server-install command was successful
2026-11-11T10:09:00Z DEBUG unrelated line appended long after the run
2026-11-11T10:10:00Z DEBUG Logging to /var/log/ipaserver-install.log
2026-11-11T10:10:00Z DEBUG Configuring Kerberos KDC (krb5kdc)
2026-11-11T10:10:00Z DEBUG   [1/1]: starting
  traceback line without a timestamp
2026-11-11T10:10:05Z DEBUG   [error] RuntimeError: boom
2026-11-11T10:10:05Z ERROR The ipa-server-install command failed.
"""


def parsed(tmp_path, **kw):
    p = tmp_path / 'ipaserver-install.log'
    p.write_text(LOG)
    return [it.rate_all(i, **kw) for i in it.parse(str(p))], str(p)


def test_every_invocation_in_an_appended_log_is_its_own_run(tmp_path):
    runs, _ = parsed(tmp_path)
    assert [r['outcome'] for r in runs] == ['ok', 'failed']
    # the line appended long after the first run does not stretch it
    assert runs[0]['wall'] == 80


def test_service_is_rated_against_its_estimate_and_steps_against_it_too(
        tmp_path):
    ok = parsed(tmp_path)[0][0]
    svc = ok['services'][0]
    assert svc['estimate'] == 30 and round(svc['ratio'], 1) == 2.7
    assert svc['rag'] == 'R'
    # 20 s is over half the 30 s estimate (amber), 60 s over all of it (red)
    assert [s['rag'] for s in svc['steps']] == ['A', 'R']


def test_failed_run_is_red_and_its_failed_step_is_listed(tmp_path):
    bad = parsed(tmp_path)[0][1]
    assert bad['overall'] == 'R' and bad['services'][0]['failed']
    assert any(o['kind'] == 'step-error' and 'boom' in o['text']
               for o in bad['oddities'])


def test_parse_estimate_reads_the_installers_wording():
    assert it.parse_estimate('1 minute 30 seconds') == 90
    assert it.parse_estimate('3 minutes') == 180


def test_storing_the_same_run_twice_keeps_one_row(tmp_path):
    runs, path = parsed(tmp_path)
    db = str(tmp_path / 't.sqlite')
    for _ in range(2):
        for r in runs:
            r.update(host='h', rel='x/ipaserver-install.log')
            it.ingest(db, 'env', r)
    con = sqlite3.connect(db)
    assert con.execute('select count(*) from runs').fetchone() == (2,)
    assert con.execute('select count(*) from steps').fetchone() == (3,)
