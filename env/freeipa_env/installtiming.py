"""Timing and oddities of the FreeIPA installer logs.

ipaserver/install/service.py logs, for every service it configures, the
estimate it prints (`Configuring X (svc). Estimated time: 3 minutes`), each
step (`  [3/46]: message`), the time of every step (`step duration: svc
method 4.52 sec`) and, in newer versions, of the whole service (`service
duration: svc 23.1 sec`). This module reads those lines, rates what it finds
red / amber / green, lists what else is odd (warnings, errors, stalls,
retries, a run that did not end), and stores the result in a SQLite database
so runs can be compared and retrieved later.
"""
import contextlib
import datetime
import hashlib
import json
import os
import re
import sqlite3
from collections import OrderedDict

LINE = re.compile(r'^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)Z (\w+) (.*)$')
STEP = re.compile(r'^\s+\[(\d+)/(\d+)\]: (.*)$')
STEP_ERROR = re.compile(r'^\s+\[error\] (.*)$')
STEP_TIME = re.compile(r'^step duration: (\S+) (\S+) ([\d.]+) sec$')
SERVICE_TIME = re.compile(r'^service duration: (\S+) ([\d.]+) sec$')
ESTIMATE = '. Estimated time: '
OUTCOME = re.compile(r'^The ipa-\S+ command (was successful|failed)')
RETRY = re.compile(r'\bRetrying\b|\bretry (in|after)\b|\bTimed out\b|'
                   r'\btimeout (expired|exceeded)\b', re.I)
UNITS = {'hour': 3600, 'minute': 60, 'second': 1}

# defaults of the rating, all overridable
AMBER_RATIO, RED_RATIO = 1.0, 1.5      # service wall time / its estimate
AMBER_STEP, RED_STEP = 10.0, 60.0      # seconds for one step
GAP = 30.0                             # seconds without a log line


def parse_estimate(text):
    """'1 minute 30 seconds' -> 90.0"""
    return float(sum(int(n) * UNITS[u.rstrip('s')]
                     for n, u in re.findall(r'(\d+) (\w+)', text)
                     if u.rstrip('s') in UNITS))


def _epoch(stamp):
    return datetime.datetime.fromisoformat(stamp).replace(
        tzinfo=datetime.timezone.utc).timestamp()


def _shape(text):
    """A message with its numbers and addresses blanked, to group repeats."""
    return re.sub(r'[0-9a-f:]{8,}|\d+', '#', text)[:200]


def rate(value, amber, red):
    return 'G' if value < amber else 'A' if value < red else 'R'


def _parse(path, lines, start, gap):
    """The services, steps and oddities of one installer invocation, `lines`
    being its lines and `start` the number of the first."""
    svcs, odd, cur, pending = [], OrderedDict(), None, None
    info = {'path': path, 'ipa_version': None, 'outcome': 'unknown'}
    first = prev_t = prev_text = None
    last = None

    def oddity(kind, level, lineno, stamp, text):
        key = (kind, level, _shape(text))
        o = odd.setdefault(key, {'kind': kind, 'level': level, 'line': lineno,
                                 'ts': stamp, 'text': text, 'count': 0})
        o['count'] += 1

    with contextlib.nullcontext(lines) as f:
        for lineno, raw in enumerate(f, start):
            m = LINE.match(raw.rstrip('\n'))
            if not m:
                continue                     # a continuation line
            stamp, level, text = m.groups()
            t = _epoch(stamp)
            first = stamp if first is None else first
            last = stamp
            if prev_t is not None and t - prev_t >= gap:
                running = pending is not None and pending['sec'] is None
                where = pending['message'] if running else prev_text
                oddity('stall', 'R' if t - prev_t >= 4 * gap else 'A', lineno,
                       stamp, f'{t - prev_t:.0f}s without a log line after: '
                              f'{where[:100]}')
            prev_t, prev_text = t, text
            if level in ('WARNING', 'ERROR'):
                oddity('log', 'A' if level == 'WARNING' else 'R', lineno,
                       stamp, f'{level} {text}')
            if RETRY.search(text) and level != 'DEBUG':
                oddity('retry', 'A', lineno, stamp, text)
            if text.startswith('IPA version '):
                info['ipa_version'] = text[len('IPA version '):]
                continue
            om = OUTCOME.match(text)
            if om:
                info['outcome'] = 'ok' if om.group(1) == 'was successful' \
                    else 'failed'
                break                        # what follows is not this run
            if text.startswith('Configuring '):
                body, _sep, est = text.partition(ESTIMATE)
                if cur is not None and not cur['steps']:
                    svcs.remove(cur)
                cur = {'desc': body[len('Configuring '):].rstrip('.'),
                       'name': None,
                       'estimate': parse_estimate(est) if est else None,
                       'start': t, 'end': None, 'duration': None,
                       'steps': [], 'failed': False}
                svcs.append(cur)
                continue
            if cur is None:
                continue
            sm = STEP.match(text)
            if sm:
                pending = {'n': int(sm.group(1)), 'of': int(sm.group(2)),
                           'message': sm.group(3), 'method': None,
                           'sec': None, 'line': lineno, 'ts': stamp}
                cur['steps'].append(pending)
                continue
            em = STEP_ERROR.match(text)
            if em:
                cur['failed'] = True
                oddity('step-error', 'R', lineno, stamp,
                       f'{cur["desc"]}: {em.group(1)}')
                continue
            tm = STEP_TIME.match(text)
            if tm:
                cur['name'] = cur['name'] or tm.group(1)
                if pending is not None and pending['sec'] is None:
                    pending['method'] = tm.group(2)
                    pending['sec'] = float(tm.group(3))
                continue
            sv = SERVICE_TIME.match(text)
            if sv:
                cur['name'] = cur['name'] or sv.group(1)
                cur['duration'] = float(sv.group(2))
                continue
            if text.startswith('Done configuring') or text == 'Done.':
                cur['end'] = t
                cur = None
    for s in svcs:
        s['incomplete'] = s['end'] is None
        end = s['end'] if s['end'] is not None else (
            _epoch(last) if last else s['start'])
        s['wall'] = (s['duration'] if s['duration'] is not None
                     else end - s['start'])
    svcs = [s for s in svcs if s['steps']]
    info.update(started=first, ended=last, services=svcs,
                oddities=list(odd.values()),
                wall=(_epoch(last) - _epoch(first)) if first else 0.0)
    if info['outcome'] == 'unknown' and any(s['incomplete'] for s in svcs):
        info['outcome'] = 'failed'
    return info


def parse(path, gap=GAP):
    """One info per installer invocation in the log: the installers append
    to their log, and every invocation starts with `Logging to <file>`."""
    with open(path, errors='replace') as f:
        lines = f.read().split('\n')
    starts = [i for i, ln in enumerate(lines)
              if (m := LINE.match(ln)) and m.group(3).startswith('Logging to ')]
    if not starts or starts[0] != 0:
        starts.insert(0, 0)
    infos = []
    for a, b in zip(starts, starts[1:] + [len(lines)]):
        info = _parse(path, lines[a:b], a + 1, gap)
        if info['started']:
            infos.append(info)
    return infos


def rate_all(info, amber_ratio=AMBER_RATIO, red_ratio=RED_RATIO,
             amber_step=AMBER_STEP, red_step=RED_STEP):
    """Add the ratings. A service is rated against its estimate when it gave
    one. A step is rated against fixed seconds, raised to half of its
    service's estimate (amber) and the whole estimate (red): a step the
    installer expected to be long is not an oddity. `overall` is the worst
    rating, red when the run failed; logged warnings and errors count at
    most amber in a run that succeeded, and stalls are shown but not counted
    (the step that stalled is already rated)."""
    ok = info['outcome'] == 'ok'
    worst = 'R' if info['outcome'] == 'failed' else 'G'

    def up(rag):
        nonlocal worst
        worst = max(worst, rag, key='GAR'.index)

    for s in info['services']:
        est = s['estimate']
        s['ratio'] = s['wall'] / est if est else None
        s['rag'] = ('-' if s['ratio'] is None
                    else 'G' if s['ratio'] <= amber_ratio
                    else 'A' if s['ratio'] <= red_ratio else 'R')
        if s['failed'] or s['incomplete']:
            s['rag'] = 'R'
        a = max(amber_step, est / 2) if est else amber_step
        r = max(red_step, est) if est else red_step
        for st in s['steps']:
            st['rag'] = ('-' if st['sec'] is None
                         else rate(st['sec'], a, r))
            if st['rag'] in 'AR':
                up(st['rag'])
        if s['rag'] in 'AR':
            up(s['rag'])
    for o in info['oddities']:
        if o['kind'] != 'stall':
            up('A' if ok and o['level'] == 'R' and o['kind'] in
               ('log', 'retry') else o['level'])
    info['overall'] = worst
    return info


def _dur(sec):
    sec = int(round(sec))
    return (f'{sec // 60}m{sec % 60:02d}s' if sec >= 60 else f'{sec}s')


def render(info, top=10):
    """The text report of one parsed log."""
    out = [f'== {info.get("host", "?")}: {info.get("rel", info["path"])}'
           + (f'  (ipa {info["ipa_version"]})' if info['ipa_version'] else ''),
           f'   {info["started"]} .. {info["ended"]}  total '
           f'{_dur(info["wall"])}  run: {info["outcome"]}  '
           f'overall: {info["overall"]}']
    if info['services']:
        out += ['', '   service                                  estimate'
                    '   actual  ratio  RAG']
        for s in info['services']:
            name = f'{s["desc"]}'[:40]
            est = _dur(s['estimate']) if s['estimate'] else '-'
            ratio = f'{s["ratio"]:.2f}' if s['ratio'] is not None else '-'
            mark = ' (did not finish)' if s['incomplete'] else ''
            out.append(f'   {name:<40} {est:>8} {_dur(s["wall"]):>8} '
                       f'{ratio:>6}  {s["rag"]}{mark}')
        steps = [(st, s) for s in info['services']
                 for st in s['steps'] if st['sec'] is not None]
        steps.sort(key=lambda p: -p[0]['sec'])
        out += ['', f'   slowest steps (top {top})']
        for st, s in steps[:top]:
            out.append(f'   {st["rag"]}  {st["sec"]:>7.1f}s  '
                       f'{s["name"] or "?":<14} {st["method"]}  '
                       f'[{st["n"]}/{st["of"]}] {st["message"][:50]}')
    if info['oddities']:
        out += ['', '   oddities']
        for o in sorted(info['oddities'], key=lambda o: (
                -'GAR'.index(o['level']), o['line'])):
            n = f' x{o["count"]}' if o['count'] > 1 else ''
            out.append(f'   {o["level"]}  line {o["line"]}{n}  '
                       f'{o["kind"]}: {o["text"][:110]}')
    return '\n'.join(out)


def installer_logs(store):
    """(host, relative path, absolute path) of every installer log found
    for the hosts of a LogStore, one per distinct content."""
    names = re.compile(r'(^|/)var/log/ipa(server|replica|client)-'
                       r'(install|uninstall)\.log$|(^|/)var/log/'
                       r'ipaupgrade\.log$')
    class _All:                          # a category that excludes nothing
        workflow = False
    found = []
    for host in store.hosts:
        files, owner = [], {}
        ext = store.extracted_dir(host)
        for root in store.search_roots(_All, host):
            # a per-test dir is named after the VM whose logs it holds
            vm = host if root == ext else os.path.basename(root)
            for base, _d, fnames in os.walk(root):
                for n in fnames:
                    full = os.path.join(base, n)
                    if names.search(os.path.relpath(full, root)):
                        files.append(full)
                        owner[full] = vm
        for f in store._dedupe_by_content(sorted(files)):
            found.append((owner[f], os.path.relpath(f, store.logdir), f))
    return found


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, env TEXT, host TEXT, source TEXT, sha256 TEXT,
  ipa_version TEXT, started TEXT, ended TEXT, wall_sec REAL, outcome TEXT,
  overall TEXT, ingested_at TEXT, UNIQUE (env, host, source, started));
CREATE TABLE IF NOT EXISTS services (
  run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE, name TEXT, descr TEXT,
  estimate_sec REAL, wall_sec REAL, ratio REAL, rag TEXT, incomplete INTEGER);
CREATE TABLE IF NOT EXISTS steps (
  run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE, service TEXT,
  n INTEGER, of INTEGER, message TEXT, method TEXT, sec REAL, rag TEXT,
  line INTEGER);
CREATE TABLE IF NOT EXISTS oddities (
  run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE, kind TEXT,
  level TEXT, line INTEGER, ts TEXT, text TEXT, count INTEGER);
"""


def ingest(db_path, env, info):
    """Write one parsed, rated log into the database, replacing an earlier
    ingest of the same (env, host, source)."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    with open(info['path'], 'rb') as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    db = sqlite3.connect(db_path)
    try:
        db.execute('PRAGMA foreign_keys = ON')
        db.executescript(SCHEMA)
        db.execute('DELETE FROM runs WHERE env=? AND host=? AND source=? '
                   'AND started=?',
                   (env, info['host'], info['rel'], info['started']))
        cur = db.execute(
            'INSERT INTO runs (env, host, source, sha256, ipa_version, '
            'started, ended, wall_sec, outcome, overall, ingested_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (env, info['host'], info['rel'], sha, info['ipa_version'],
             info['started'], info['ended'], info['wall'], info['outcome'],
             info['overall'],
             datetime.datetime.now(datetime.timezone.utc).isoformat()))
        rid = cur.lastrowid
        for s in info['services']:
            db.execute('INSERT INTO services VALUES (?,?,?,?,?,?,?,?)',
                       (rid, s['name'], s['desc'], s['estimate'], s['wall'],
                        s['ratio'], s['rag'], int(s['incomplete'])))
            db.executemany(
                'INSERT INTO steps VALUES (?,?,?,?,?,?,?,?,?)',
                [(rid, s['name'], st['n'], st['of'], st['message'],
                  st['method'], st['sec'], st['rag'], st['line'])
                 for st in s['steps']])
        db.executemany(
            'INSERT INTO oddities VALUES (?,?,?,?,?,?,?)',
            [(rid, o['kind'], o['level'], o['line'], o['ts'], o['text'],
              o['count']) for o in info['oddities']])
        db.commit()
    finally:
        db.close()


def analyze(store_, gap=GAP, **thresholds):
    """Parsed and rated infos for every installer invocation in a LogStore's
    logs. The framework keeps a snapshot of the same log per test, so an
    invocation seen in several files is reported once: the longest copy."""
    best = {}
    for host, rel, path in installer_logs(store_):
        for info in parse(path, gap):
            info.update(host=host, rel=rel)
            key = (host, os.path.basename(path), info['started'])
            if key not in best or info['wall'] > best[key]['wall'] or (
                    info['wall'] == best[key]['wall']
                    and len(info['services']) > len(best[key]['services'])):
                best[key] = info
    return [rate_all(i, **thresholds)
            for _k, i in sorted(best.items(), key=lambda kv: (
                kv[0][0], kv[0][2] or '', kv[0][1]))]


def to_json(infos):
    return json.dumps(infos, indent=2)
