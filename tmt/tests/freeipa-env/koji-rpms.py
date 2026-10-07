#!/usr/bin/env python3
"""Download the binary RPMs of a Koji build task (scratch-build reuse lane).

Usage: koji-rpms.py TASK_ID OUT_DIR [--arch x86_64,noarch] [--hub URL]
                                   [--file-url URL]

Fedora dist-git CI (Packit) runs a Koji scratch build for every dist-git PR
and passes its task id to the test request as KOJI_TASK_ID. That task already
built the exact RPMs the PR produces, so the CI reuses them instead of paying
for a second build: this script collects the binary RPMs of the task's
buildArch subtasks (default arches x86_64 + noarch; debuginfo/debugsource
excluded) from kojipkgs into OUT_DIR, where images/build.sh --rpms bakes the
channel image from them.

The RPM paths come from each subtask's stored result ("rpms = tasks/<...>.rpm"),
which is authoritative; files are served under the koji file URL.
"""
import argparse
import os
import sys
import urllib.request
import xmlrpc.client


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('task_id', type=int, help='Koji task id (build/scratch-build parent)')
    ap.add_argument('out_dir', help='directory the RPMs are written to')
    ap.add_argument('--arch', default='x86_64,noarch',
                    help='comma-separated arches to keep (default: x86_64,noarch)')
    ap.add_argument('--hub', default='https://koji.fedoraproject.org/kojihub')
    # Task result paths ("tasks/<...>.rpm") are relative to koji's task
    # output tree, served under the file server's /work prefix.
    ap.add_argument('--file-url', default='https://kojipkgs.fedoraproject.org/work')
    args = ap.parse_args()

    def die(msg):
        print(f'koji-rpms: {msg}', file=sys.stderr)
        sys.exit(1)

    hub = xmlrpc.client.ServerProxy(args.hub, allow_none=True)
    if not hub.getTaskInfo(args.task_id):
        die(f'no such koji task: {args.task_id}')

    # A build (scratch or tagged) keeps its RPMs in the buildArch subtasks'
    # results; include the parent too in case it aggregates them.
    task_ids = [args.task_id]
    for child in hub.getTaskChildren(args.task_id) or []:
        task_ids.append(child['id'])

    arches = {a.strip() for a in args.arch.split(',') if a.strip()}
    os.makedirs(args.out_dir, exist_ok=True)
    seen = set()
    got = 0
    for tid in task_ids:
        result = hub.getTaskResult(tid) or {}
        for rel in result.get('rpms') or []:
            name = rel.rsplit('/', 1)[-1]
            if not name.endswith('.rpm'):
                continue
            parts = name.split('.')
            arch = parts[-2] if len(parts) >= 2 else ''
            if arch not in arches:
                continue
            if 'debuginfo' in name or 'debugsource' in name:
                continue
            if name in seen:
                continue
            seen.add(name)
            url = f"{args.file_url.rstrip('/')}/{rel}"
            print(f'==> fetching {name}')
            req = urllib.request.Request(url, headers={'User-Agent': 'freeipa-ci'})
            with urllib.request.urlopen(req) as r, \
                    open(os.path.join(args.out_dir, name), 'wb') as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            got += 1

    if got == 0:
        die(f'no {"/".join(sorted(arches))} binary RPMs in task {args.task_id} '
            '(is it a build task?)')
    print(f'koji-rpms: {got} RPMs from task {args.task_id} -> {args.out_dir}')


if __name__ == '__main__':
    main()
