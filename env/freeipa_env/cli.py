"""freeipa-env CLI: up / run / down / show / logs over an env.yaml.

`logs` (D1) categorizes the collected artifacts and retrieves them per
category for CI issue triage; see loganalyze.py."""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

from .envspec import EnvSpec, EnvSpecError, RunSpec
from .podman_provider import PodmanProvider, PodmanError
from .external_provider import ExternalProvider, ExternalError
from .vmbackend import VMBackendError
from .nested_provider import NestedProvider, NestedError
from .loganalyze import (LogStore, CATEGORIES, CATEGORY_NAMES,
                         render_list, render_json, show_categories,
                         DEFAULT_LINES, DEFAULT_TAIL)
from .htmlreport import render_html
from . import summary as xsummary

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'scripts')
RUN_SCRIPT = os.path.join(SCRIPTS_DIR, 'run-base-tests.sh')
CONTAINER_LOGSDIR = '/root/ipa-env/logs'
CONFIG_IN_CONTAINER = '/root/.ipa/ipa-test-config.yaml'


def state_home():
    return (os.environ.get('XDG_STATE_HOME')
            or os.path.join(os.path.expanduser('~'), '.local', 'state'))


def default_workdir(spec):
    """Per-user state, not the current directory: logs and keys of an
    environment must not land in whichever repository `up` ran from."""
    return os.path.join(state_home(), 'freeipa-env', spec.name)


def load_spec(path):
    try:
        return EnvSpec.from_file(path)
    except EnvSpecError as e:
        print(f'error: {e}', file=sys.stderr)
        sys.exit(2)


def _emit_html(store, html_arg, meta=None):
    """Write the self-contained results.html (default <workdir>/logs/results.html)
    and print the path it was written to.

    The report sits in the logdir (not the workdir root) so that its
    relative links to the collected log tree (also under <workdir>/logs)
    resolve identically on the TF artifact server and after a local
    ``tf-logs`` fetch.
    """
    out = (html_arg if html_arg not in (None, 'auto')
           else os.path.join(store.logdir, 'results.html'))
    p = render_html(store, out, meta)
    print(f'== wrote report: {p}')
    return p


def _emit_batch_index(workdir, entries, meta=None):
    """Write the batch index at <workdir>/results.html and print its path.

    ``entries`` is a list of ``(key, subdir)`` tuples, one per preset (the
    ``''`` key is the top level). The index links to each preset's
    ``<preset>/logs/results.html``; the parent itself is not a single-job
    report.
    """
    from .htmlreport import render_batch_index
    out = os.path.join(workdir, 'results.html')
    p = render_batch_index(workdir, entries, out, meta)
    print(f'== wrote batch index: {p}')
    return p


def cmd_report(args):
    """Render a self-contained results.html for a workdir's artifacts.

    No network needed: it reuses the same LogStore / loganalyze machinery
    as `logs` and inlines all CSS, so the file opens offline straight from
    the (fetched) workdir. The default output is
    <workdir>/logs/results.html (the logdir, so its relative links to the
    collected log tree resolve on the artifact server and locally).
    """
    workdir = args.workdir
    if not workdir:
        if not args.env:
            print('error: give an env file (to derive the default workdir) '
                  'or --workdir DIR', file=sys.stderr)
            return 2
        workdir = default_workdir(load_spec(args.env))
    if not os.path.isdir(workdir):
        print(f'error: no artifacts in {workdir} (run `up`/`run`/`down` '
              'first, or `tf-logs` to fetch a TF request)', file=sys.stderr)
        return 2
    _emit_html(LogStore(workdir), args.out,
               meta={'request_id': getattr(args, 'request_id', None)})
    return 0


def make_provider(spec, workdir, args):
    if spec.provider == 'podman':
        return PodmanProvider(spec, workdir, tool=args.tool,
                              seccomp=args.seccomp,
                              copr=getattr(args, 'copr', None) or None,
                              ipa_from=getattr(args, 'ipa_from', None),
                              ipa_packages=getattr(args, 'ipa_packages',
                                                   None),
                              ipa_add=getattr(args, 'add_ipa_package',
                                              None) or None)
    if spec.provider == 'nested':
        return NestedProvider(spec, workdir, args)
    return ExternalProvider(spec, workdir, strict=args.strict,
                         ssh_key=args.ssh_key)


def build_run_env(spec):
    run = spec.run or RunSpec()
    env = {
        'IPA_TESTS_DOMAIN': spec.domain,
        'IPA_TESTS_REALM': spec.realm,
        'IPA_TESTS_LOGSDIR': CONTAINER_LOGSDIR,
        # shlex-quoted: the values expand through `bash -c` in the runner
        # scripts (and ipa-run-tests re-spawns pytest through a shell), so
        # multi-word elements (e.g. -k "not test_dns_soa") must survive.
        'IPA_TESTS_TO_RUN': ' '.join(shlex.quote(t) for t in run.tests),
        'IPA_TESTS_TO_IGNORE': ' '.join(f'--ignore {shlex.quote(x)}'
                                        for x in run.ignore),
        'IPA_TESTS_TO_DESELECT': ' '.join(f'--deselect {shlex.quote(x)}'
                                          for x in run.deselect),
        'IPA_TESTS_ARGS': ' '.join(shlex.quote(a) for a in run.args),
        'IPA_NETWORK_INTERNAL': str(run.network_internal).lower(),
        'SETUP_KRA': '1' if run.setup_kra else '0',
    }
    fwd = run.forwarder or spec.dns_forwarder
    if fwd and not run.network_internal:
        env['SERVER_FORWARDER'] = fwd
    return env


def cmd_up(args):
    spec = load_spec(args.env)
    workdir = args.workdir or default_workdir(spec)
    os.makedirs(workdir, exist_ok=True)
    prov = make_provider(spec, workdir, args)
    prov.up()
    return 0


def cmd_down(args):
    spec = load_spec(args.env)
    workdir = args.workdir or default_workdir(spec)
    prov = make_provider(spec, workdir, args)
    prov.down()
    return 0


def cmd_show(args):
    spec = load_spec(args.env)
    workdir = args.workdir or default_workdir(spec)
    prov = make_provider(spec, workdir, args)
    prov.show()
    return 0


def cmd_resolve(args):
    """Resolve the preset's logical image references to the concrete
    local images — a provider task (presets never pin a build).
    Prints one line per image: `<ref> <concrete> <image_id>`. Exit 0 iff
    every reference resolves."""
    spec = load_spec(args.env)
    workdir = args.workdir or default_workdir(spec)
    prov = make_provider(spec, workdir, args)
    resolved = prov.resolve_images()
    if not resolved:
        print('no image references to resolve locally (the provider '
              'resolves at `up` time, e.g. a nested provider resolves on '
              'the provisioned VM)')
        return 0
    for ref, (concrete, img_id) in resolved.items():
        print(f'{ref} {concrete} {img_id}')
    return 0


def cmd_ensure(args):
    """Build the preset's channel images ourselves (design §3.10) when they
    are missing on this host — a provider task, the build-side counterpart
    of `resolve`. Consumes the spec's `build.srpm` (the SRPM the control node
    produced with scripts/make-srpms.sh); absent channels are built in the
    dedicated build image and baked into the channel `full` image. Present
    channels are left alone unless `--build` (or `build.force`) is set.
    Prints one line per channel: `<ref> <concrete> <image_id>`."""
    spec = load_spec(args.env)
    workdir = args.workdir or default_workdir(spec)
    os.makedirs(workdir, exist_ok=True)
    prov = make_provider(spec, workdir, args)
    force = getattr(args, 'build', False) or None
    ensured = prov.ensure_images(force=force)
    if not ensured:
        print('no channel images to build locally (the env names no '
              'channel, or the provider builds on the provisioned host)')
        return 0
    for ref, (concrete, img_id) in ensured.items():
        print(f'{ref} {concrete} {img_id}')
    return 0


def cmd_check(args):
    """Parse + validate preset files without spawning anything (CI gating).

    Checks every preset (default: all under env/presets) parses as an
    EnvSpec and passes the provider's read-only sanity checks. Exit 0 iff
    every preset is sound and no given path was missing."""
    from .checker import check_paths
    results, missing = check_paths(args.paths or None)
    n_ok = sum(1 for _p, errs in results if not errs)
    n_bad = sum(1 for _p, errs in results if errs)
    for path, errs in results:
        for e in errs:
            print(f'FAIL {path}: {e}')
    for m in missing:
        print(f'FAIL {m}: file not found')
    total = len(results)
    print(f'\nchecked {total} presets: {n_ok} ok, {n_bad} failed, '
          f'{len(missing)} missing')
    return 1 if (n_bad or missing) else 0


def cmd_migrate(args):
    """Generate freeipa-env presets from a PRCI definition file."""
    from .prci import migrate
    if args.out:
        outdir = args.out
    else:
        stem = os.path.splitext(os.path.basename(args.definition))[0]
        outdir = os.path.join('presets', 'prci', stem.replace('_', '-'))
    written, skipped = migrate(args.definition, outdir, only=args.jobs)
    for path, preset, notes in written:
        ad = any('AD host' in n for n in notes)
        flag = '  [AD: edit external placeholders]' if ad else ''
        print(f'== {path}  [{preset["run"]["mode"]}{flag}]')
        for n in notes:
            print(f'   note: {n}')
    for jn, reason in skipped:
        print(f'== skipped {jn}: {reason}')
    print(f'\n{len(written)} preset(s) generated in {outdir}, '
          f'{len(skipped)} skipped; report: '
          f'{os.path.join(outdir, "MIGRATED.md")}')
    return 0


def _show_files(workdir, args):
    """The last --lines of every collected file matching --file, restricted
    to tests whose log directory contains --test and to --host. The
    collected tree is logs/collected/<host>/ipa-env/logs/<test>/<host>/...,
    which nobody should have to type."""
    import fnmatch
    root = os.path.join(workdir, 'logs', 'collected')
    hits = []
    for base, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(base, name)
            rel = os.path.relpath(path, root)
            if not fnmatch.fnmatch(name, args.file):
                continue
            if args.test and args.test not in rel:
                continue
            if args.host and args.host not in rel:
                continue
            hits.append((rel, path))
    if not hits:
        print(f'no collected file matches {args.file!r} under {root}',
              file=sys.stderr)
        return 1
    for rel, path in sorted(hits):
        print(f'== {rel}')
        with open(path, errors='replace') as f:
            lines = f.read().splitlines()
        print('\n'.join(lines[-args.lines:]))
    return 0


def cmd_logs(args):
    workdir = args.workdir
    spec = None
    if not workdir:
        if not args.env:
            print('error: give an env file (to derive the default workdir) '
                  'or --workdir DIR', file=sys.stderr)
            return 2
        spec = load_spec(args.env)
        workdir = default_workdir(spec)
    if not os.path.isdir(workdir):
        print(f'error: no artifacts in {workdir} (run `up`/`run`/`down` first)',
              file=sys.stderr)
        return 2
    if args.refresh:
        if not args.env:
            print('error: --refresh requires the env file', file=sys.stderr)
            return 2
        spec = spec or load_spec(args.env)
        prov = make_provider(spec, workdir, args)
        print('== collecting fresh logs from the environment')
        try:
            prov.collect_logs()
        except (PodmanError, ExternalError) as e:
            print(f'warning: log collection failed: {e}', file=sys.stderr)
    if args.file:
        return _show_files(workdir, args)
    if args.pattern:
        try:
            args.pattern_compiled = re.compile(args.pattern)
        except re.error as e:
            print(f'error: invalid --pattern: {e}', file=sys.stderr)
            return 2
    else:
        args.pattern_compiled = None
    store = LogStore(workdir)
    if args.html:
        _emit_html(store, args.html)
        return 0
    cats = args.category or []
    if args.list or not cats:
        if args.json:
            render_json(store)
        else:
            render_list(store)
    if cats:
        rc = show_categories(store, cats, args)
        return rc or 0
    return 0


def cmd_tf_logs(args):
    """Fetch a finished Testing Farm request's job artifacts into a local
    workdir and parse them with the same ``logs`` machinery a local/ssh job
    uses (categories, pattern, json, per-host)."""
    from .loganalyze import (LogStore, CATEGORIES, CATEGORY_NAMES,
                             render_list, render_json, show_categories,
                             DEFAULT_LINES, DEFAULT_TAIL)
    from .testingfarm import fetch_artifacts, TfApiError

    # Fetching a finished request's artifacts needs no auth: the request
    # state (GET) and the artifact store are public. A token is only used if
    # one happens to be set (e.g. to match a non-public API deployment).
    token = args.tf_token or os.environ.get('TESTING_FARM_API_TOKEN')
    workdir = args.workdir
    if not workdir:
        stem = re.sub(r'[^A-Za-z0-9._-]+', '-', args.request_id).strip('-')
        workdir = os.path.join(os.getcwd(), f'tf-{stem}.env')
    try:
        per_key = fetch_artifacts(
            args.request_id, workdir, token,
            url=args.tf_url or 'https://api.testing-farm.io',
            include_consoles=not args.no_consoles,
            log=lambda l: print(l, file=sys.stderr))
    except TfApiError as e:
        print(f'error: {e}', file=sys.stderr)
        return 1
    if not per_key or not any(per_key.values()):
        print(f'error: no job artifacts in request {args.request_id} '
              '(the job may have died before `down`, or has not finished)',
              file=sys.stderr)
        return 1
    total = sum(per_key.values())
    print(f'== fetched {total} artifact file(s) into {workdir}',
          file=sys.stderr)
    # A batch request (M13) fetched one subtree per preset; each preset is a
    # reportable workdir of its own. A legacy single-job request maps the ''
    # key to the workdir itself.
    subdirs = []
    for key in sorted(per_key):
        d = workdir if key == '' else os.path.join(workdir, key)
        if per_key[key]:
            subdirs.append((key, d))
    for key, d in subdirs:
        label = f'{key}: {workdir}' if key == '' else f'{key}: {d}'
        print(f'== workdir {label} ({per_key[key]} file(s))', file=sys.stderr)
        print(f'   report: freeipa-env report {d} '
              f'(writes {d}/logs/results.html)', file=sys.stderr)
    if any(key != '' for key, _ in subdirs):
        print(f'== batch request: one report per preset above',
              file=sys.stderr)

    # reuse the `logs` parsing path exactly
    if args.pattern:
        try:
            args.pattern_compiled = re.compile(args.pattern)
        except re.error as e:
            print(f'error: invalid --pattern: {e}', file=sys.stderr)
            return 2
    else:
        args.pattern_compiled = None
    store = LogStore(workdir)
    if args.html:
        if any(k for k, _ in subdirs):
            # A batch request: one report per preset, plus an index at the
            # parent linking to each (the parent is not a single-job report).
            # The '' entry is the parent workdir (its own results.yaml), not a
            # preset — it is the index itself, not a row.
            presets = [(k, d) for k, d in subdirs if k]
            for _key, d in presets:
                _emit_html(LogStore(d), 'auto',
                           meta={'request_id': args.request_id})
            _emit_batch_index(workdir, presets,
                              meta={'request_id': args.request_id})
        else:
            # A legacy single-job request: one report for the workdir itself.
            _emit_html(LogStore(workdir), args.html,
                       meta={'request_id': args.request_id})
        return 0
    cats = args.category or []
    if args.list or not cats:
        if args.json:
            render_json(store)
        else:
            render_list(store)
    if cats:
        rc = show_categories(store, cats, args)
        return rc or 0
    return 0


def _podman_only(prov, what):
    if not isinstance(prov, PodmanProvider):
        raise PodmanError(f'{what} needs the podman provider')


def _require_up(prov):
    """A run needs the master: say so, rather than fail inside podman."""
    if isinstance(prov, PodmanProvider):
        name = prov.spec.container_name(prov.spec.master)
        state = prov._state(name)
        if state != 'running':
            raise PodmanError(f'{name} is {state}: run `up` for this env '
                              f'file first')


def _build_command(spec, run, prov):
    """(shell command, environment) that runs the spec's tests in the master,
    or (None, env) for an unknown mode."""
    env = build_run_env(spec)
    if run.mode == 'base':
        prov.copy_to_controller(RUN_SCRIPT, '/root/run-base-tests.sh')
        return 'bash /root/run-base-tests.sh', env
    if run.mode != 'integration':
        return None, env
    argv = ['ipa-run-tests', '--logging-level=debug', '--verbose', '-ra',
            '--with-xunit', f'--logfile-dir={CONTAINER_LOGSDIR}',
            f'--html={CONTAINER_LOGSDIR}/report.html',
            '--self-contained-html']
    # --logfile-dir makes the framework collect its per-test log trees
    # (one dir per test nodeid, with the per-host install/uninstall logs
    # + journal) into /root/ipa-env/logs instead of a throwaway tempdir,
    # so `down` collects them and the report's "Per-test logs" section has
    # content. The framework creates the dir itself (os.makedirs), and the
    # container's /root is writable (base mode's run-base-tests.sh writes
    # to the same path), so no pre-creation is needed.
    # --html/--self-contained-html (pytest-html) writes the in-container
    # per-test captured stdout/stderr + traceback report to
    # /root/ipa-env/logs/report.html; it rides the same /root/ipa-env cp
    # as the per-test trees and is parsed by the report's Per-test logs
    # section. pytest-html must be installed in the image (images/full).
    for x in run.ignore:
        argv += ['--ignore', x]
    for x in run.deselect:
        argv += ['--deselect', x]
    argv += run.args
    argv += run.tests
    env['IPATEST_YAML_CONFIG'] = CONFIG_IN_CONTAINER
    return shlex.join([a for a in argv if a]), env


def _clear_stale_report(prov):
    """A report left by an earlier run would be fetched and summarised as
    this run's when this one writes none (e.g. a collection error)."""
    prov._exec_rc(prov.spec.container_name(prov.spec.master),
                  'rm -f /root/nosetests.xml '
                  '/root/ipa-env/logs/nosetests.xml')
    stale = os.path.join(prov.workdir, 'nosetests.xml')
    if os.path.exists(stale):
        os.remove(stale)


def _print_summary(workdir, tail=6):
    path = os.path.join(workdir, 'nosetests.xml')
    if not os.path.exists(path):
        return None
    overlay = 0
    try:
        with open(os.path.join(workdir, 'overlay.json')) as f:
            overlay = len(json.load(f))
    except (OSError, ValueError):
        pass
    text, counts = xsummary.render(path, tail, overlay)
    print(text)
    return counts


def _finish_run(prov, workdir, rc):
    # pull the xunit report next to the workdir logs (podman only)
    if isinstance(prov, PodmanProvider) and rc is not None:
        _fetch_xunit(prov, workdir)
        _print_summary(workdir)
    print(f'== run finished with exit code {rc}')
    return rc if rc is not None else 1


def cmd_run(args):
    spec = load_spec(args.env)
    workdir = args.workdir or default_workdir(spec)
    prov = make_provider(spec, workdir, args)
    run = spec.run or RunSpec()
    if args.tests:
        run.tests = list(args.tests)
        spec.run = run
    if args.detach:
        _podman_only(prov, '--detach')
    _require_up(prov)
    cmd, env = _build_command(spec, run, prov)
    if cmd is None:
        print(f'error: unknown run mode {run.mode!r}', file=sys.stderr)
        return 2
    if isinstance(prov, PodmanProvider):
        _clear_stale_report(prov)
    if args.detach:
        prov.start_detached(cmd, env)
        print('== run started in the background: `status` follows it, '
              '`wait` collects its result')
        return 0
    rc = prov.run_in_controller(cmd, env=env, log='run.log')
    return _finish_run(prov, workdir, rc)


def cmd_status(args):
    spec = load_spec(args.env)
    prov = make_provider(spec, args.workdir or default_workdir(spec), args)
    _podman_only(prov, 'status')
    state, rc = prov.detached_state()
    print(f'== detached run: {state}'
          + (f' (exit code {rc})' if rc is not None else ''))
    if state != 'none':
        print(prov.detached_tail(args.lines), end='')
    return 0 if state in ('running', 'finished') else 1


def cmd_wait(args):
    spec = load_spec(args.env)
    workdir = args.workdir or default_workdir(spec)
    prov = make_provider(spec, workdir, args)
    _podman_only(prov, 'wait')
    deadline = time.time() + args.timeout if args.timeout else None
    while True:
        state, rc = prov.detached_state()
        if state == 'finished':
            break
        if state in ('none', 'lost'):
            print(f'error: no detached run to wait for ({state})',
                  file=sys.stderr)
            return 1
        if deadline and time.time() > deadline:
            print('== still running')
            return 3
        time.sleep(args.interval)
    prov.fetch_detached_log()
    return _finish_run(prov, workdir, rc)


def cmd_summary(args):
    spec = load_spec(args.env)
    workdir = args.workdir or default_workdir(spec)
    if _print_summary(workdir, args.tail) is None:
        print(f'error: no nosetests.xml in {workdir} (`run` or `wait` '
              f'fetches it)', file=sys.stderr)
        return 1
    return 0


def cmd_overlay(args):
    spec = load_spec(args.env)
    prov = make_provider(spec, args.workdir or default_workdir(spec), args)
    _podman_only(prov, 'overlay')
    pairs = []
    for item in args.pairs:
        src, sep, dst = item.partition(':')
        if not sep or not dst.startswith('/'):
            print(f'error: {item!r}: expected SRC:/absolute/DST',
                  file=sys.stderr)
            return 2
        pairs.append((src, dst))
    prov.overlay(pairs, mode=args.mode)
    return 0


def cmd_reset(args):
    """Fresh containers on the same spec, the previous logs kept aside."""
    spec = load_spec(args.env)
    workdir = args.workdir or default_workdir(spec)
    prov = make_provider(spec, workdir, args)
    _podman_only(prov, 'reset')
    prov.down()
    if os.path.isdir(prov.logdir):
        os.rename(prov.logdir,
                  f'{prov.logdir}.{time.strftime("%Y%m%d-%H%M%S")}')
    for stale in ('overlay.json', 'nosetests.xml'):
        path = os.path.join(workdir, stale)
        if os.path.exists(path):
            os.remove(path)
    prov.up()
    return 0


def _podman_lines(tool, argv):
    p = subprocess.run([tool] + argv, capture_output=True, text=True)
    return p.stdout.splitlines() if p.returncode == 0 else []


def _environments(tool):
    """{env name: [(container, status)]} for every env-* podman network, so
    environments whose workdir is gone (or never was here) are listed too."""
    envs = {}
    for net in _podman_lines(tool, ['network', 'ls', '--format',
                                    '{{.Name}}']):
        if not net.startswith('env-'):
            continue
        rows = [ln.split('|', 1) for ln in _podman_lines(
            tool, ['ps', '-a', '--filter', f'network={net}', '--format',
                   '{{.Names}}|{{.Status}}'])]
        envs[net[len('env-'):]] = rows
    return envs


def _state_dir():
    return os.path.join(state_home(), 'freeipa-env')


def cmd_list(args):
    envs = _environments(args.tool)
    workdirs = (set(os.listdir(_state_dir()))
                if os.path.isdir(_state_dir()) else set())
    for name in sorted(set(envs) | workdirs):
        rows = envs.get(name)
        if rows is None:
            state = 'no containers'
        else:
            up = sum(1 for _n, s in rows if s.startswith('Up'))
            state = f'{up}/{len(rows)} running'
        wd = os.path.join(_state_dir(), name) if name in workdirs else '-'
        print(f'{name:28s} {state:16s} {wd}')
    if not envs and not workdirs:
        print('no environments')
    return 0


def cmd_prune(args):
    """Remove the containers and network of environments with nothing running
    (and, with --workdirs, their workdirs). Dry run unless --yes."""
    envs = _environments(args.tool)
    idle = {n: r for n, r in envs.items()
            if not any(s.startswith('Up') for _n, s in r)}
    workdirs = (set(os.listdir(_state_dir()))
                if os.path.isdir(_state_dir()) else set())
    victims = []
    for name in sorted(idle):
        print(f'environment {name}: {len(idle[name])} stopped container(s) '
              f'and its network')
        victims.append(name)
    gone = sorted(workdirs - {n for n, r in envs.items()
                              if any(s.startswith('Up') for _n, s in r)})
    if args.workdirs:
        for name in gone:
            print(f'workdir {os.path.join(_state_dir(), name)}')
    if not args.yes:
        print('== dry run: nothing removed (--yes to remove)')
        return 0
    for name in victims:
        for cname, _status in idle[name]:
            subprocess.run([args.tool, 'rm', '-f', cname],
                           capture_output=True)
        subprocess.run([args.tool, 'network', 'rm', f'env-{name}'],
                       capture_output=True)
    if args.workdirs:
        for name in gone:
            shutil.rmtree(os.path.join(_state_dir(), name),
                          ignore_errors=True)
    return 0


def _fetch_xunit(prov, workdir):
    try:
        # ipa-run-tests writes $PWD/nosetests.xml (PWD at invocation;
        # run-base-tests.sh pushd's into $IPA_TESTS_LOGSDIR, like Azure).
        # probe LOGSDIR, /root, /, and the pkg dir. The probe uses
        # double-quoted python literals only, so it can be wrapped in
        # single quotes for the container bash safely. When no report
        # exists (e.g. the install failed and the tests never ran) the
        # probe yields an empty string, which we treat as a clean no-op
        # below rather than a crash.
        probe = ('import ipatests, os; '
                 'c = ["/root/ipa-env/logs/nosetests.xml", '
                 '"/root/nosetests.xml", "/nosetests.xml", '
                 'os.path.join(os.path.dirname(ipatests.__file__), '
                 '"nosetests.xml")]; '
                 'print(next((p for p in c if os.path.isfile(p)), ""))')
        _rc, out = prov._exec(
            prov.spec.container_name(prov.spec.master),
            "python3 -c '%s'" % probe)
        # the probe prints exactly one line (the path, or empty when no
        # report exists); there is no multi-line output to worry about.
        src = out.strip()
        if not src:
            # no report written: expected when the install failed and the
            # test phase was skipped, or when no tests matched. Not an error.
            print('== no xunit report found '
                  '(tests produced no nosetests.xml; expected when the '
                  'install failed)', file=sys.stderr)
            return
        prov._podman(['cp', f'{prov.spec.container_name(prov.spec.master)}:'
                            f'{src}',
                      os.path.join(workdir, 'nosetests.xml')])
        print(f'== xunit report: {os.path.join(workdir, "nosetests.xml")}')
    except (PodmanError, IndexError):
        print('== no xunit report found', file=sys.stderr)


def cmd_queue(args):
    if args.queue_cmd == 'generate':
        from .queue import generate_prci_queue
        text = generate_prci_queue(args.definition)
        if args.out:
            d = os.path.dirname(os.path.abspath(args.out))
            os.makedirs(d, exist_ok=True)
            with open(args.out, 'w') as f:
                f.write(text)
            print(f'wrote {args.out}')
        else:
            sys.stdout.write(text)
        return 0

    from .queue import QueueSpec
    from .supervisor import Supervisor, SupervisorError
    from .runner import make_runner, RunnerTransportError
    try:
        q = QueueSpec.from_file(args.queue_file)
    except (ValueError, OSError) as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    jobs = q.jobs
    if args.jobs:
        jobs = [j for j in jobs
                if any(o.lower() in j.key.lower() for o in args.jobs)]
    if args.limit:
        jobs = jobs[:args.limit]
    if not jobs:
        print('error: no jobs left after filtering', file=sys.stderr)
        return 2
    q.jobs = jobs
    # No source to build named: install with dnf, from the COPR repos when
    # any is given, else the packages the distribution ships. A testing-farm
    # run that names a FreeIPA repo keeps building that repo on the guest.
    if not (args.ipa_from or args.srpm or (
            any(s.strip() == 'testing-farm' for s in args.runner)
            and (args.freeipa_src or args.tf_repo_url))):
        args.ipa_from = 'copr' if args.copr else 'distro'
    tf_cfg = _tf_config(args)
    if tf_cfg is None:
        return 2
    if args.dry_run:
        print(f'queue {q.name}: {len(jobs)} job(s) over '
              f'{len(args.runner)} runner(s): '
              f'{"; ".join(args.runner)}')
        for i, j in enumerate(jobs, 1):
            note = f'  # {j.note}' if j.note else ''
            missing = '' if os.path.isfile(j.path) else '  [MISSING PRESET]'
            print(f'{i:3d}. {j.key:45s} {j.preset_rel}{note}{missing}')
        if any(s.strip() == 'testing-farm' for s in args.runner):
            from .testingfarm import build_tf_request
            if not tf_cfg.get('token'):
                print('(testing-farm: no token configured; the request '
                      'JSON below is what would be submitted)')
            srpm = getattr(args, 'srpm', None)
            copr = getattr(args, 'copr', None) or None
            ipa_from = getattr(args, 'ipa_from', None)
            ipa_packages = getattr(args, 'ipa_packages', None)
            # The supervisor dequeues round-robin over the runners; a TF
            # runner submits its whole subsequence as ONE batched request
            # (build the SRPM/channel images once, reuse across presets).
            # With a single TF runner that is the entire queue. Preview each
            # TF runner's subsequence as one request (deterministic
            # round-robin; exact for the single-runner case).
            runner_list = list(args.runner)
            tf_idx = [i for i, s in enumerate(runner_list)
                      if s.strip() == 'testing-farm']
            per_runner = {i: [] for i in tf_idx}
            for n, j in enumerate(jobs):
                r = n % len(runner_list)
                if r in per_runner:
                    per_runner[r].append(j)
            for i in tf_idx:
                sub = per_runner[i]
                print(f'\n== testing-farm batch request '
                      f'({len(sub)} job(s): '
                      + ', '.join(j.key for j in sub) + ') ==')
                print(json.dumps(
                    build_tf_request(tf_cfg, sub, args.job_timeout,
                                     srpm=srpm, copr=copr,
                                     ipa_from=ipa_from,
                                     ipa_packages=ipa_packages),
                    indent=2))
        return 0
    outdir = args.outdir or os.path.join(os.getcwd(), f'{q.name}.queue')
    try:
        runners = [make_runner(s, ssh_key=args.ssh_key, tf_cfg=tf_cfg)
                   for s in args.runner]
    except RunnerTransportError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    srpm = getattr(args, 'srpm', None)
    copr = getattr(args, 'copr', None) or None
    ipa_from = getattr(args, 'ipa_from', None)
    ipa_packages = getattr(args, 'ipa_packages', None)
    if ipa_from == 'copr' and not copr:
        print('error: --ipa-from-copr requires at least one --copr repo',
              file=sys.stderr)
        return 2
    if ipa_from == 'distro' and copr:
        print('error: --ipa-from-distro excludes --copr (use '
              '--ipa-from-copr)', file=sys.stderr)
        return 2
    if ipa_from and srpm:
        print(f'error: --ipa-from-{ipa_from} is mutually exclusive with '
              '--srpm', file=sys.stderr)
        return 2
    has_tf = any(s.strip() == 'testing-farm' for s in args.runner)
    if srpm and not has_tf and not os.path.exists(srpm):
        print(f'error: --srpm path does not exist: {srpm}', file=sys.stderr)
        return 2
    srpm_dir = getattr(args, 'srpm_dir', None) or os.path.join(
        os.path.dirname(args.remote_ci.rstrip('/')), 'srpm')
    sup = Supervisor(q, runners, outdir, job_timeout=args.job_timeout,
                     keep_on_failure=args.keep_on_failure,
                     remote_ci=args.remote_ci, jobs_dir=args.jobs_dir,
                     bootstrap=not args.no_bootstrap, srpm=srpm,
                     srpm_dir=srpm_dir,
                     image_timeout=getattr(args, 'image_timeout', 9000),
                     copr=copr, ipa_from=ipa_from, ipa_packages=ipa_packages)
    try:
        return sup.run()
    except SupervisorError as e:
        print(f'error: {e}', file=sys.stderr)
        return 1


def _git_value(args, fallback=None, cwd=None):
    """git plumbing value for a checkout (best effort; default: current)."""
    import subprocess
    try:
        p = subprocess.run(['git'] + args, capture_output=True, text=True,
                           timeout=10, cwd=cwd)
        return p.stdout.strip() or fallback
    except (OSError, subprocess.TimeoutExpired):
        return fallback


def _default_tf_ref(cwd=None):
    """Default git ref for the TF guest: prefer the current branch NAME over
    the HEAD SHA. TF distinguishes a branch from a raw ref and a bare SHA is
    not an advertised ref on most remotes (e.g. GitHub), so it fails with
    ``Failed to fetch ref <sha>: retries exhausted``; a branch name is always
    fetchable. On a detached HEAD there is no branch, so fall back to the SHA.
    """
    branch = _git_value(['symbolic-ref', '--short', 'HEAD'], cwd=cwd)
    if branch:
        return branch
    return _git_value(['rev-parse', 'HEAD'], cwd=cwd) or 'HEAD'


def _tf_config(args):
    """The TF cfg dict for `queue run` ({} when no TF runner is requested;
    None after printing an error)."""
    if not any(s.strip() == 'testing-farm' for s in args.runner):
        return {}
    token = getattr(args, 'tf_token', None) or \
        os.environ.get('TESTING_FARM_API_TOKEN')
    if not token:
        print('error: testing-farm runner: no API token (pass --tf-token '
              'or set TESTING_FARM_API_TOKEN)', file=sys.stderr)
        return None
    # The repo under test is a freeipa checkout, never this ci-freeipa one:
    # its URL/ref default from --freeipa-src, or must be given explicitly.
    # Except with --ipa-from-distro: nothing is built, so no FreeIPA checkout
    # is involved and the request runs this repo's own plan from this
    # checkout's origin and branch.
    src = getattr(args, 'freeipa_src', None)
    plan = getattr(args, 'tf_plan', None)
    # (--ipa-from-copr with an explicit --tf-repo-url keeps that repo's plan)
    ipa_from = getattr(args, 'ipa_from', None)
    if not src and (ipa_from == 'distro' or (
            ipa_from == 'copr' and not getattr(args, 'tf_repo_url', None))):
        from .runner import REPO_CI_DIR
        from .testingfarm import DISTRO_PLAN
        src = REPO_CI_DIR
        plan = plan or DISTRO_PLAN
    repo_url = getattr(args, 'tf_repo_url', None)
    if not repo_url:
        if not src:
            print('error: testing-farm runner: the repo under test is a '
                  'freeipa checkout, not this ci-freeipa one; pass '
                  '--freeipa-src DIR (its origin remote and branch are '
                  'used as defaults) or --tf-repo-url', file=sys.stderr)
            return None
        repo_url = _git_value(['config', 'remote.origin.url'], cwd=src)
        if not repo_url:
            print(f'error: testing-farm runner: {src} has no origin '
                  'remote (pass --tf-repo-url)', file=sys.stderr)
            return None
    ref = getattr(args, 'tf_ref', None)
    if not ref:
        if not src:
            print('error: testing-farm runner: no ref; pass --freeipa-src '
                  'DIR or --tf-ref', file=sys.stderr)
            return None
        ref = _default_tf_ref(cwd=src)
    cfg = {
        'token': token,
        'url': getattr(args, 'tf_url', None),
        'repo_url': repo_url,
        'ref': ref,
        'arch': getattr(args, 'tf_arch', None),
        'compose': getattr(args, 'tf_compose', None),
        'plan': plan,
    }
    for kv in getattr(args, 'tf_variable', []):
        k, _, v = kv.partition('=')
        if not k:
            print(f'error: --tf-variable needs KEY=VALUE: {kv!r}',
                  file=sys.stderr)
            return None
        cfg.setdefault('extra_variables', {})[k] = v
    return cfg


def main(argv=None):
    p = argparse.ArgumentParser(
        prog='freeipa-env',
        description='Provision and drive FreeIPA CI environments '
                    '(design: doc/designs/ci_modernization.md in the '
                    'freeipa repository).')
    p.add_argument('--tool', default='podman',
                   help='container tool (default: podman)')
    p.add_argument('--seccomp', default=None,
                   help='path to a seccomp profile json (podman provider)')
    p.add_argument('--strict', action='store_true',
                   help='external provider: fail on unreachable ports')
    sub = p.add_subparsers(dest='cmd', required=True)

    def add_ipa_from(sp):
        """Install the IPA with dnf instead of building it: args.ipa_from is
        'copr', 'distro' or None. None becomes 'distro' when no SRPM, COPR
        repo or (testing-farm) FreeIPA repo is named."""
        g = sp.add_mutually_exclusive_group()
        g.add_argument('--ipa-from-copr', dest='ipa_from',
                       action='store_const', const='copr',
                       help='install the IPA packages from the enabled COPR '
                            'repos (--copr) instead of building them from an '
                            'SRPM/RPM set (build.sh --ipa-from-copr); '
                            'requires at least one --copr')
        g.add_argument('--ipa-from-distro', dest='ipa_from',
                       action='store_const', const='distro',
                       help='install the IPA packages the Fedora release '
                            'itself ships: no build, no COPR repo (build.sh '
                            '--ipa-from-distro); excludes --copr and --srpm. '
                            'The release is the env `dist:` (up/ensure), 44 '
                            'on a host runner, the guest compose on the '
                            'testing-farm runner. This is the default when '
                            'no --copr, SRPM or FreeIPA repo is named; '
                            '--copr alone means --ipa-from-copr')

    def add_common(sp):
        sp.add_argument('env', help='env.yaml (or preset) path')
        sp.add_argument('--workdir', default=None,
                        help='state/logs dir (default '
                             '$XDG_STATE_HOME/freeipa-env/<env-name>, '
                             'i.e. ~/.local/state/freeipa-env/<env-name>)')
        sp.add_argument('--ssh-key', default=None,
                        help='external provider: private key for root SSH '
                             '(default: ~/.ssh/id_rsa)')
        sp.add_argument('--copr', action='append', metavar='OWNER/PROJECT',
                        default=[],
                        help='additional COPR repo to enable on the channel-'
                             'image builds (repeatable); customizes channel '
                             '*creation* without touching the preset. '
                             'Used by up/ensure with the podman provider.')
        add_ipa_from(sp)
        sp.add_argument('--ipa-packages', default=None, metavar='SPECS',
                        help='space-separated dnf specs to install in '
                             '--ipa-from-copr/--ipa-from-distro mode '
                             '(default: all IPA subpackages -- the full '
                             'server incl. freeipa-server-dns, the client, '
                             'the python3-* bits and python3-ipatests)')
        sp.add_argument('--add-ipa-package', action='append',
                        metavar='SPEC', default=[],
                        help='with --ipa-from-copr/--ipa-from-distro: extra '
                             'dnf spec added to '
                             'the default package list (repeatable), e.g. '
                             'kuipa; unlike --ipa-packages it never needs '
                             'the default list copied')

    sp = sub.add_parser('up', help='create (or attach) the environment')
    add_common(sp)
    sp.set_defaults(fn=cmd_up)

    sp = sub.add_parser('down', help='collect logs and tear down')
    add_common(sp)
    sp.set_defaults(fn=cmd_down)

    sp = sub.add_parser('show', help='show environment state')
    add_common(sp)
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser(
        'resolve',
        help="resolve the preset's image references to concrete local "
             "images (a provider task; prints '<ref> <concrete> <id>')")
    add_common(sp)
    sp.set_defaults(fn=cmd_resolve)

    sp = sub.add_parser(
        'ensure',
        help="build the preset's channel images from build.srpm on this "
             "host when missing (a provider task; the build-side "
             "counterpart of resolve)")
    add_common(sp)
    sp.add_argument('--build', action='store_true',
                    help='rebuild the channel images even if they are '
                         'already present (else only absent ones are built)')
    sp.set_defaults(fn=cmd_ensure)

    sp = sub.add_parser('run', help='run the test workflow in the env')
    add_common(sp)
    sp.add_argument('--tests', action='append', metavar='TEST',
                    help='run these tests instead of the spec\'s run.tests '
                         '(repeatable): a file or file::Class[::test]')
    sp.add_argument('--detach', action='store_true',
                    help='start the tests in the master and return; the run '
                         'survives this command (`status`, `wait`)')
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser('status', help='state and output of a detached run')
    add_common(sp)
    sp.add_argument('--lines', type=int, default=20,
                    help='output lines to show (default 20)')
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser(
        'wait', help='wait for a detached run, then fetch its report')
    add_common(sp)
    sp.add_argument('--timeout', type=int, default=0, metavar='SECONDS',
                    help='give up waiting after this long (exit 3); the '
                         'run itself keeps going. Default: wait for it')
    sp.add_argument('--interval', type=int, default=15, metavar='SECONDS',
                    help='seconds between checks (default 15)')
    sp.set_defaults(fn=cmd_wait)

    sp = sub.add_parser(
        'summary', help='per-test results of the last run, with the cause '
                        'of each failure')
    add_common(sp)
    sp.add_argument('--tail', type=int, default=6,
                    help='cause lines per failed test (default 6)')
    sp.set_defaults(fn=cmd_summary)

    sp = sub.add_parser(
        'overlay', help='copy local files into every container, verify '
                        'them by hash and mark the run as an overlay run')
    add_common(sp)
    sp.add_argument('pairs', nargs='+', metavar='SRC:/DST',
                    help='a file to the destination path, or a directory to '
                         'the destination directory')
    sp.add_argument('--mode', default=None, metavar='MODE',
                    help='chmod the copies, e.g. 755')
    sp.set_defaults(fn=cmd_overlay)

    sp = sub.add_parser(
        'reset', help='recreate the containers on the same spec, keeping '
                      'the previous logs as logs.<timestamp>')
    add_common(sp)
    sp.set_defaults(fn=cmd_reset)

    sp = sub.add_parser(
        'list', help='environments on this host: containers and workdir')
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser(
        'prune', help='remove the containers and network of environments '
                      'with nothing running (dry run unless --yes)')
    sp.add_argument('--yes', action='store_true', help='really remove')
    sp.add_argument('--workdirs', action='store_true',
                    help='also remove the workdirs of environments that are '
                         'not running (their logs go with them)')
    sp.set_defaults(fn=cmd_prune)

    sp = sub.add_parser(
        'logs',
        help='categorize and retrieve collected logs for troubleshooting')
    sp.add_argument('env', nargs='?', default=None,
                    help='env.yaml (or preset) path')
    sp.add_argument('--workdir', default=None,
                    help='state/logs dir to analyze (default '
                         '$XDG_STATE_HOME/freeipa-env/<env-name>)')
    sp.add_argument('--ssh-key', default=None,
                    help='external provider: private key for root SSH '
                         '(default: ~/.ssh/id_rsa); used with --refresh')
    sp.add_argument('--list', action='store_true',
                    help='per-category summary (default when no --category)')
    sp.add_argument('--category', action='append', metavar='NAME',
                    help=f'category to print, repeatable; "all" for '
                         f'everything; names: {", ".join(CATEGORY_NAMES)}')
    sp.add_argument('--host', default=None,
                    help='only this host (default: all)')
    sp.add_argument('--pattern', default=None,
                    help='line-match regex, overrides the category default')
    sp.add_argument('--lines', type=int, default=DEFAULT_LINES,
                    help=f'max matched lines per file (default {DEFAULT_LINES})')
    sp.add_argument('--tail', type=int, default=DEFAULT_TAIL,
                         help=f'tail lines to show per file (default {DEFAULT_TAIL}, '
                         f'0 disables)')
    sp.add_argument('--all', action='store_true',
                    help='print whole files unfiltered')
    sp.add_argument('--json', action='store_true',
                    help='machine-readable summary (with --list)')
    sp.add_argument('--refresh', action='store_true',
                    help='re-collect logs from a live environment first')
    sp.add_argument('--test', default=None, metavar='TEXT',
                    help='with --file: only logs of tests whose name '
                         'contains TEXT')
    sp.add_argument('--file', default=None, metavar='NAME',
                    help='print the last --lines lines of every collected '
                         'file called NAME (a glob, e.g. ipaserver-install.'
                         'log), per test and host, instead of the category '
                         'view')
    sp.add_argument('--html', nargs='?', const='auto', default=None,
                    metavar='OUT',
                    help='instead of the terminal view, write a '
                         'self-contained results.html and exit (default '
                         'output: <workdir>/logs/results.html)')
    sp.set_defaults(fn=cmd_logs)

    sp = sub.add_parser(
        'tf-logs',
        help='fetch a Testing Farm request\'s job artifacts and parse them '
             'like `logs` (no env file needed)')
    sp.add_argument('request_id',
                    help='Testing Farm request id (from a `queue run` '
                         'transcript or tf_list)')
    sp.add_argument('--workdir', default=None,
                    help='where to store the artifacts (default '
                         './tf-<request-id>.env; reuse it to skip the '
                         're-download)')
    sp.add_argument('--tf-token', default=None,
                    help='Testing Farm API token; not needed to fetch a '
                         'finished request (the request state and the '
                         'artifact store are public) — default: env '
                         'TESTING_FARM_API_TOKEN, or none')
    sp.add_argument('--tf-url', default=None,
                    help='Testing Farm API base URL '
                         '(default https://api.testing-farm.io)')
    sp.add_argument('--no-consoles', action='store_true',
                    help='do not fetch the per-stage console logs '
                         '(only the job workdir artifacts under logs/)')
    # the same parsing options as `logs`
    sp.add_argument('--list', action='store_true',
                    help='per-category summary (default when no --category)')
    sp.add_argument('--category', action='append', metavar='NAME',
                    help=f'category to print, repeatable; "all" for '
                         f'everything; names: {", ".join(CATEGORY_NAMES)}')
    sp.add_argument('--host', default=None,
                    help='only this host (default: all)')
    sp.add_argument('--pattern', default=None,
                    help='line-match regex, overrides the category default')
    sp.add_argument('--lines', type=int, default=DEFAULT_LINES,
                    help=f'max matched lines per file (default {DEFAULT_LINES})')
    sp.add_argument('--tail', type=int, default=DEFAULT_TAIL,
                    help=f'tail lines to show per file (default {DEFAULT_TAIL}, '
                         f'0 disables)')
    sp.add_argument('--all', action='store_true',
                    help='print whole files unfiltered')
    sp.add_argument('--json', action='store_true',
                    help='machine-readable summary (with --list)')
    sp.add_argument('--html', nargs='?', const='auto', default=None,
                    metavar='OUT',
                    help='after fetching, write a self-contained '
                         'results.html and exit (default output: '
                         '<workdir>/logs/results.html)')
    sp.set_defaults(fn=cmd_tf_logs)

    sp = sub.add_parser(
        'report',
        help='render a self-contained results.html from a workdir\'s '
             'collected artifacts (offline; no env file needed)')
    sp.add_argument('env', nargs='?', default=None,
                    help='env.yaml (or preset) path (only to derive the '
                         'default workdir)')
    sp.add_argument('--workdir', default=None,
                    help='state/logs dir to render (default '
                         './<env-name>.env)')
    sp.add_argument('-o', '--out', default=None,
                    help='output file (default <workdir>/logs/results.html)')
    sp.set_defaults(fn=cmd_report)

    sp = sub.add_parser(
        'migrate',
        help='generate presets from a PRCI definition file '
             '(ipatests/prci_definitions/*.yaml)')
    sp.add_argument('definition', help='PRCI definition YAML file')
    sp.add_argument('-o', '--out', default=None,
                    help='output dir (default: presets/prci/<definition>)')
    sp.add_argument('--jobs', action='append', metavar='NAME',
                    help='only migrate jobs whose name contains NAME '
                         '(repeatable)')
    sp.set_defaults(fn=cmd_migrate)

    sp = sub.add_parser(
        'check',
        help='parse + validate preset files without spawning containers '
             '(CI gating)')
    sp.add_argument('paths', nargs='*', default=None,
                    help='preset files or directories '
                         '(default: presets/ under env)')
    sp.set_defaults(fn=cmd_check)

    spq = sub.add_parser(
        'queue', help='ordered preset queues + the queue supervisor')
    qsub = spq.add_subparsers(dest='queue_cmd', required=True)
    qrun = qsub.add_parser(
        'run', help='run a queue on pre-allocated runners '
                    '(ssh + podman + systemd)')
    qrun.add_argument('queue_file', help='queue YAML (queues/*.yaml)')
    qrun.add_argument('--runner', action='append', required=True,
                      metavar='RUNNER',
                      help='runner (repeatable; jobs dequeued in queue '
                           'order). Spec: user@host[:port] = ssh runner '
                           '(one job at a time); "local" = the control '
                           'node (no ssh); "testing-farm" = the runner\'s '
                           'whole subsequence becomes ONE TF request (build '
                           'the SRPM/channel images once, reuse across '
                           'presets)')
    qrun.add_argument('--jobs', action='append', metavar='NAME',
                      help='substring filter on the job key (repeatable)')
    qrun.add_argument('--limit', type=int, default=None,
                      help='run at most N jobs')
    qrun.add_argument('--dry-run', action='store_true',
                      help='print the planned queue (and, for the '
                           'testing-farm runner, the batched request JSON '
                           'for each runner\'s subsequence) and exit')
    qrun.add_argument('--keep-on-failure', action='store_true',
                      help='skip `down` when a job fails (env stays on the '
                           'runner for triage)')
    qrun.add_argument('--job-timeout', type=int, default=14400,
                      help='per-job remote timeout in seconds '
                           '(default 14400 = 4h)')
    qrun.add_argument('--outdir', default=None,
                      help='local artifacts dir (default ./<queue>.queue)')
    qrun.add_argument('--remote-ci', default='~/freeipa-ci/ci',
                      help='ci-freeipa tree location on each runner; ~ expands '
                           'to the runner user\'s home (default '
                           '~/freeipa-ci/ci)')
    qrun.add_argument('--jobs-dir', default='~/jobs',
                      help='per-job workdir parent on each runner; ~ '
                           'expands to the runner user\'s home (default '
                           '~/jobs)')
    qrun.add_argument('--ssh-key', default=None, help='ssh identity file')
    qrun.add_argument('--no-bootstrap', action='store_true',
                      help='do not sync the ci-freeipa tree to the runners '
                           '(the testing-farm runner always clones the '
                           'repo itself)')
    qrun.add_argument('--srpm', default=None, metavar='PATH',
                      help='SRPM (or its dist/srpms/ dir) produced by '
                           'scripts/make-srpms.sh; shipped to each runner '
                           'and used to build the queue channel images that '
                           'are missing there (design §3.10, "build the '
                           'IPA RPMs ourselves"). With the testing-farm '
                           'runner this must be an HTTP(S) URL: the guest '
                           'downloads it at job time.')
    qrun.add_argument('--srpm-dir', default=None, metavar='PATH',
                      help='remote dir on each runner to ship the SRPM into '
                           '(default: <dirname --remote-ci>/srpm)')
    qrun.add_argument('--copr', action='append', metavar='OWNER/PROJECT',
                      default=[],
                      help='additional COPR repo to enable on the queue '
                           'channel-image builds (repeatable); customizes '
                           'channel *creation* without touching any preset. '
                           'Host runners pass it to build.sh --copr; the '
                           'testing-farm runner emits it as the guest '
                           'FREEIPA_COPR_REPOS request variable.')
    add_ipa_from(qrun)
    qrun.add_argument('--ipa-packages', default=None, metavar='SPECS',
                      help='space-separated dnf specs to install in '
                           '--ipa-from-copr/--ipa-from-distro mode '
                           '(default: all IPA subpackages -- the full '
                           'server incl. freeipa-server-dns, the client, '
                           'the python3-* bits and python3-ipatests)')
    qrun.add_argument('--image-timeout', type=int, default=9000,
                      help='per-channel-image remote build timeout in '
                           'seconds (default 9000 = 2.5h)')
    # Testing Farm options (only used with --runner testing-farm)
    qrun.add_argument('--freeipa-src', default=None, metavar='DIR',
                      help='FreeIPA checkout under test; --tf-repo-url and '
                           '--tf-ref default to its origin remote and '
                           'branch (this ci-freeipa checkout is never the '
                           'repo under test, except with --ipa-from-distro, '
                           'where it is the default and nothing is built)')
    qrun.add_argument('--tf-token', default=None,
                      help='Testing Farm API token (default: env '
                           'TESTING_FARM_API_TOKEN)')
    qrun.add_argument('--tf-url', default='https://api.testing-farm.io',
                      help='Testing Farm API base URL')
    qrun.add_argument('--tf-repo-url', default=None,
                      help='git URL of the FreeIPA repo under test for the '
                           'TF guest to clone (default: the origin remote '
                           'of --freeipa-src)')
    qrun.add_argument('--tf-ref', default=None,
                      help='git ref for the TF guest (default: the current '
                           'branch of --freeipa-src, falling back to its '
                           'HEAD SHA on a detached HEAD)')
    qrun.add_argument('--tf-arch', default='x86_64',
                      help='TF guest architecture (default x86_64)')
    qrun.add_argument('--tf-compose', default='Fedora-44',
                      help='TF guest OS compose (default Fedora-44)')
    qrun.add_argument('--tf-plan', default=None,
                      help='tmt plan (node) name (default '
                           '/ci/tmt/plans/freeipa-env in the FreeIPA repo '
                           'under test, which pins this repo; with '
                           '--ipa-from-distro /tmt/plans/distro in this one)')
    qrun.add_argument('--tf-variable', action='append', default=[],
                      metavar='KEY=VALUE',
                      help='extra environment variable for the TF guest '
                           '(repeatable)')
    qrun.set_defaults(fn=cmd_queue)
    qgen = qsub.add_parser(
        'generate', help='generate a queue from a PRCI definition '
                         '(in PRCI job order)')
    qgen.add_argument('definition', help='PRCI definition YAML file')
    qgen.add_argument('-o', '--out', default=None,
                      help='output queue file (default: print to stdout)')
    qgen.set_defaults(fn=cmd_queue)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (PodmanError, ExternalError, EnvSpecError, VMBackendError,
            NestedError) as e:
        print(f'error: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
