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
