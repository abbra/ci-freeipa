"""Markdown job report for a parsed workdir: the same facts as the HTML
report (overall status, stages, tests with failure detail, matched log
lines per category), as text that reads in a terminal, a PR comment or an
issue. The per-test log blocks of the HTML report are not repeated here.
"""
import datetime
import os
import re

from .htmlreport import _clip_line, _rel, _xunit_testrows, _xunit_totals
from .loganalyze import CATEGORIES, load_results, overall_status, scan_file

# matched lines shown per source file; the HTML report keeps more
_MD_MATCH_CAP = 50


def _cell(s):
    """One table cell: no pipe or newline may end the cell early."""
    return str(s).replace('|', '\\|').replace('\n', ' ') or '—'


def _fence(text):
    """`text` in a code fence longer than any backtick run inside it."""
    longest = max((len(m) for m in re.findall(r'`+', text)), default=0)
    bar = '`' * max(3, longest + 1)
    return f'{bar}\n{text.rstrip()}\n{bar}'


def _stages(stages):
    if not stages:
        return ['_No `results.yaml` in the workdir._']
    out = ['| stage | result | duration | started | note |',
           '|---|---|---:|---:|---|']
    for s in stages:
        start = s['start'].replace('T', ' ').rstrip('Z').replace('.000000', '')
        out.append(f'| `{_cell(s["name"] or "/")}` | '
                   f'{_cell(s["result"] or "unknown")} | '
                   f'{_cell(s["duration"] or "")} | {_cell(start)} | '
                   f'{_cell(" ; ".join(s["note"]))} |')
    return out


def _tests(rows):
    if not rows:
        return ['_No xunit test cases found (`nosetests.xml` absent or '
                'empty)._']
    out = ['| module | test | result | time |', '|---|---|---|---:|']
    details = []
    for tc in rows:
        out.append(f'| `{_cell(tc["classname"])}` | `{_cell(tc["name"])}` | '
                   f'{_cell(tc["status"])} | {tc["time"]:.3f}s |')
        if tc['status'] in ('fail', 'error'):
            body = [f'#### `{tc["classname"]}::{tc["name"]}`']
            if tc['message']:
                body.append(_clip_line(tc['message']))
            if tc['text']:
                body.append(_fence(tc['text']))
            details.append('\n\n'.join(body))
    if details:
        out += ['', '### Failures', ''] + ['\n\n'.join(details)]
    return out


def _category(cat, store):
    sources = store.category_sources(cat)
    lines = [f'### {cat.name}', '', f'{cat.desc} — {len(sources)} source(s)']
    for f in sources:
        total, matched, _tail = scan_file(f, cat.match, cache=store.scan_cache)
        if not matched:
            continue
        shown = [_clip_line(m) for m in matched[:_MD_MATCH_CAP]]
        more = len(matched) - len(shown)
        lines += ['', f'`{_rel(store, f)}` ({len(matched)}/{total} lines)',
                  '', _fence('\n'.join(shown))]
        if more:
            lines.append(f'_… {more} more matching lines_')
    return lines


def build_markdown(store, meta=None):
    meta = meta or {}
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        '%Y-%m-%d %H:%M:%S UTC')
    tot = _xunit_totals(store)
    stages = load_results(store.workdir)
    status, detail = overall_status(store, stages)
    out = [f'# FreeIPA CI job report — {meta.get("request_id") or "local"}',
           '',
           f'- workdir: `{store.workdir}`',
           f'- hosts: {", ".join(store.hosts) or "(none collected)"}',
           f'- generated: {now}',
           f'- status: **{detail or status}**']
    if tot['tests']:
        out.append(f'- tests: {tot["tests"]} · {tot["failures"]} failed · '
                   f'{tot["errors"]} errors · {tot["skipped"]} skipped · '
                   f'{tot["time"]:.1f}s')
    out += ['', '## Stages', ''] + _stages(stages)
    out += ['', '## Tests', ''] + _tests(_xunit_testrows(store))
    out += ['', '## Log categories', '']
    for cat in CATEGORIES:
        if not cat.xunit and not cat.ptest:
            out += _category(cat, store) + ['']
    return '\n'.join(out).rstrip() + '\n'


def render_markdown(store, out_path, meta=None):
    """Write results.md for a parsed workdir; returns the path."""
    d = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(d, exist_ok=True)
    with open(out_path, 'w') as f:
        f.write(build_markdown(store, meta))
    return out_path
