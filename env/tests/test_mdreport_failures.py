from freeipa_env.loganalyze import LogStore
from freeipa_env.mdreport import build_markdown

XML = ('<testsuite tests="2" failures="1">'
       '<testcase classname="a.B" name="ok" time="1"/>'
       '<testcase classname="a.B" name="bad|pipe" time="2">'
       '<failure message="assert 1 == 2">E   assert 1 == 2\n'
       'with ``` inside</failure></testcase></testsuite>')


def test_a_failed_test_gets_its_message_and_traceback_in_a_safe_fence(
        tmp_path):
    host = tmp_path / 'logs' / 'collected' / 'master1.ipa.test'
    host.mkdir(parents=True)
    (host / 'nosetests.xml').write_text(XML)
    md = build_markdown(LogStore(str(tmp_path)))
    assert '| tests: 2' not in md and '- tests: 2 · 1 failed' in md
    assert '### Failures' in md and '#### `a.B::bad|pipe`' in md
    assert 'assert 1 == 2' in md
    # the traceback holds ``` so its fence must be longer
    assert '````\nE   assert 1 == 2' in md
    # the pipe in the test name must not split the table row
    assert '`bad\\|pipe`' in md
