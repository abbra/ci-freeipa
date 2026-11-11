import re

from freeipa_env import loganalyze as la

PAT = re.compile('ERR')


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return str(path)


def test_scan_file_matches_and_counts_total(tmp_path):
    f = write(tmp_path / 'errors', 'ok\nERR - bad\nfine\n')
    total, matched, tail = la.scan_file(f, PAT)
    assert total == 3
    assert matched == ['ERR - bad']
    assert tail[-1] == 'fine'


def test_matched_lines_dedupes_identical_lines_per_host(tmp_path):
    store = la.LogStore(str(tmp_path))
    store.hosts = ['master1', 'replica1']
    cat = la.Category('ds', 'test', match=PAT)
    # two per-test snapshots on master1 share one line; replica1 has it too
    snaps = {
        'master1': [write(tmp_path / 'a', '[ts] ERR - x\n[ts2] ERR - y\n'),
                    write(tmp_path / 'b', '[ts] ERR - x\n')],
        'replica1': [write(tmp_path / 'c', '[ts] ERR - x\n')],
    }
    store.category_files = lambda c, h: snaps[h]
    lines = [l for _f, l in store.matched_lines(cat, PAT)]
    assert lines.count('[ts] ERR - x') == 2  # once per host, not per snapshot
    assert lines.count('[ts2] ERR - y') == 1


def test_matched_lines_keeps_same_text_with_different_timestamps(tmp_path):
    store = la.LogStore(str(tmp_path))
    store.hosts = ['master1']
    cat = la.Category('ds', 'test', match=PAT)
    store.category_files = lambda c, h: [
        write(tmp_path / 'a', '[t1] ERR - x\n[t2] ERR - x\n')]
    assert len(store.matched_lines(cat, PAT)) == 2


def test_scan_cache_hit_then_invalidated_on_change(tmp_path):
    f = write(tmp_path / 'errors', 'ERR one\nok\n')
    cache = la.ScanCache(str(tmp_path))
    assert la.scan_file(f, PAT, cache=cache)[1] == ['ERR one']
    # same size and mtime: the cached answer is served, not the file
    st = (tmp_path / 'errors').stat()
    cache.db.execute('update scan set matched=?', ('["cached"]',))
    cache.db.commit()
    assert la.scan_file(f, PAT, cache=cache)[1] == ['cached']
    # a rewritten file (new mtime) is rescanned
    (tmp_path / 'errors').write_text('ERR two\nok\n')
    import os
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert la.scan_file(f, PAT, cache=cache)[1] == ['ERR two']


def test_scan_cache_delete_drops_only_given_keys(tmp_path):
    cache = la.ScanCache(str(tmp_path))
    f = write(tmp_path / 'errors', 'ERR a\n')
    other = re.compile('WARN')
    la.scan_file(f, PAT, cache=cache)
    la.scan_file(f, other, cache=cache)
    keys = {k for k, _n, _c in cache.summary()}
    assert keys == {la.cache_prefix(PAT) + '8', la.cache_prefix(other) + '8'}
    cache.delete([la.cache_prefix(PAT) + '8'])
    assert {k for k, _n, _c in cache.summary()} == {la.cache_prefix(other) + '8'}
