"""Text summary of an xunit report: one line per test, the cause of each
failure under it, so a run can be judged without opening the XML."""

import xml.etree.ElementTree as ET

STATES = ('FAIL', 'ERROR', 'SKIP', 'ok')


def _cause(element, tail):
    """The assertion lines of a failure ('E   ...'), else its last lines."""
    text = (element.text or '').strip('\n')
    lines = [ln for ln in text.splitlines() if ln.strip()]
    asserted = [ln for ln in lines if ln.startswith('E ')]
    return (asserted or lines)[-tail:]


def read(path, tail=6):
    """[(state, 'Class::test', [cause lines])] in report order."""
    rows = []
    for tc in ET.parse(path).getroot().iter('testcase'):
        name = '%s::%s' % (tc.get('classname', '').split('.')[-1],
                           tc.get('name'))
        failure = tc.find('failure')
        error = tc.find('error')
        if failure is not None:
            rows.append(('FAIL', name, _cause(failure, tail)))
        elif error is not None:
            rows.append(('ERROR', name, _cause(error, tail)))
        elif tc.find('skipped') is not None:
            rows.append(('SKIP', name, []))
        else:
            rows.append(('ok', name, []))
    return rows


def render(path, tail=6, overlay=None):
    """The summary text; returns (text, counts)."""
    rows = read(path, tail)
    counts = {s: sum(1 for r in rows if r[0] == s) for s in STATES}
    out = []
    for state, name, cause in rows:
        out.append('%-5s %s' % (state, name))
        out.extend('        ' + ln[:200] for ln in cause)
    joined = ', '.join('%d %s' % (counts[s], s) for s in STATES if counts[s])
    out.append('== ' + (joined or 'no tests'))
    if overlay:
        out.append('== OVERLAY: %d file copies applied; this is not a '
                   'packaged run' % overlay)
    return '\n'.join(out), counts
