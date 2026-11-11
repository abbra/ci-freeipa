from freeipa_env.loganalyze import LogStore


def _host(root, with_xml=True):
    d = root / 'collected' / 'master1.ipa.test'
    d.mkdir(parents=True)
    if with_xml:
        (d / 'nosetests.xml').write_text('<testsuite/>')
    return d


def test_inner_tree_is_read_when_down_did_not_mirror_it(tmp_path):
    _host(tmp_path / 'inner' / 'logs')
    store = LogStore(str(tmp_path))
    assert store.hosts == ['master1.ipa.test']
    assert [p for p in store.xunit_paths()] == [
        str(tmp_path / 'inner' / 'logs' / 'collected' / 'master1.ipa.test'
            / 'nosetests.xml')]


def test_the_mirrored_logs_tree_wins_over_the_inner_one(tmp_path):
    _host(tmp_path / 'logs', with_xml=False)
    _host(tmp_path / 'inner' / 'logs')
    store = LogStore(str(tmp_path))
    assert store.logdir == str(tmp_path / 'logs')
    assert store.xunit_paths() == []
