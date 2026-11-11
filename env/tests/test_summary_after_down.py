from freeipa_env import cli

XML = ('<testsuite><testcase classname="a.B" name="ok"/>'
       '<testcase classname="a.B" name="bad"><failure>E  boom</failure>'
       '</testcase></testsuite>')


def test_summary_reads_the_report_that_down_moved_under_collected(
        tmp_path, capsys):
    host = tmp_path / 'logs' / 'collected' / 'master1.ipa.test'
    host.mkdir(parents=True)
    (host / 'nosetests.xml').write_text(XML)
    counts = cli._print_summary(str(tmp_path))
    out = capsys.readouterr().out
    assert counts['FAIL'] == 1 and counts['ok'] == 1
    assert 'results from logs/collected/master1.ipa.test/nosetests.xml' in out


def test_summary_without_any_report_returns_none(tmp_path):
    assert cli._print_summary(str(tmp_path)) is None
