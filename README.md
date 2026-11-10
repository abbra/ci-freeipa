# ci-freeipa — FreeIPA CI tooling, separate from the branch under test

This repository holds the FreeIPA CI implementation: the `freeipa-env`
provisioner (spawns or attaches FreeIPA test environments from a single
declarative file and drives the install → test → uninstall workflow), the
image pipeline, the preset/queue catalogs and the tmt test that runs a job
batch on a Testing Farm guest. The freeipa source tree no longer carries
any of this code — it keeps only a pinned tmt plan that runs this repo's
test against the checkout under test (see **Repository split** below). The
design is documented in the freeipa repository as
`doc/designs/ci_modernization.md`.

For agent-assisted investigation and testing of a current FreeIPA worktree,
see [ci-freeipa-worktree](.agents/skills/ci-freeipa-worktree/SKILL.md).
To test the packages a Fedora release already ships, with no checkout, build
or COPR repository, see **Test the packages Fedora ships** below and
[ci-freeipa-distro-packages](.agents/skills/ci-freeipa-distro-packages/SKILL.md).

## Repository split

CI code and the branch under test are versioned independently. The only CI
artifact inside a freeipa checkout is the pin — the plan
`ci/tmt/plans/freeipa-env.fmf`, which discovers the test in this repo
(`tmt/tests/freeipa-env/`) from a git URL at a pinned ref, and whose
`prepare` step installs the BuildRequires of **that branch's**
`freeipa.spec.in` (build dependencies belong to the code being built).
Adopting newer CI tooling is therefore a one-line `ref:` bump in the plan;
changing a branch's build deps never requires touching this repo.

Running the test from a freeipa checkout (the plan's `discover.url` must be
a git URL reachable where tmt runs — a local path works for local runs):

```
tmt run plan --name /ci/tmt/plans/freeipa-env -e FREEIPA_SRC=$PWD
```

`FREEIPA_SRC` points the test at this checkout for the on-guest SRPM build;
the Testing Farm transport sets `FREEIPA_REPO_URL`/`FREEIPA_REPO_REF`
instead (the guest clones the branch under test itself, since TF syncs the
fmf tree as a plain file copy without `.git` or submodules). Submitting a
TF batch from here: `freeipa-env queue run QUEUE --runner testing-farm
--freeipa-src ~/freeipa` — the repo under test defaults to that checkout's
origin and branch.

```
freeipa-env up   ENV.YAML    create (or attach) the environment
freeipa-env run  ENV.YAML    run the test workflow (install, tests, uninstall)
freeipa-env down ENV.YAML    collect journal/logs, then tear down
freeipa-env show ENV.YAML    environment state, image labels, installed IPA/kuipa/ahdapa/akamu builds
freeipa-env logs ENV.YAML    categorize and retrieve collected logs
freeipa-env logs ENV.YAML --test T --file NAME   last lines of a collected file, per test and host
freeipa-env run  ENV.YAML --tests X   run these tests instead of the env's run.tests
freeipa-env run  ENV.YAML --detach    start the tests and return (survives this command)
freeipa-env status ENV.YAML            state and output of a detached run
freeipa-env wait ENV.YAML              wait for a detached run, fetch report, print summary
freeipa-env summary ENV.YAML           per-test results of the last run with failure causes
freeipa-env overlay ENV.YAML SRC:/DST  copy files into every container, hash-verified
freeipa-env reset ENV.YAML             fresh containers on the same spec, old logs kept aside
freeipa-env list                       environments on this host
freeipa-env prune [--yes] [--workdirs] remove environments with nothing running
freeipa-env report [ENV.YAML]  render a self-contained results.html from the artifacts
freeipa-env resolve ENV.YAML   resolve image refs against the local store (read-only)
freeipa-env ensure ENV.YAML    build the env's missing channel images (Fedora's packages, or build.srpm)
freeipa-env migrate PRCI.YAML  generate presets from a PRCI definition
freeipa-env queue run QUEUE.YAML --runner USER@HOST [...]   # run a queue
freeipa-env queue generate PRCI.YAML -o QUEUE.YAML          # PRCI-order queue
freeipa-env check [PRESETS ...]                             # validate presets (no containers)
```

Common options: `--workdir DIR` (state/logs dir, default
`$XDG_STATE_HOME/freeipa-env/<env-name>`, i.e.
`~/.local/state/freeipa-env/<env-name>`, never the current directory),
`--tool podman`, `--seccomp FILE` (podman), `--strict` (external: fail on
unreachable ports), `--ssh-key FILE` (external: root SSH key).

### Long runs, repeatable runs

A backup/restore class takes 30 minutes or more. `run` in the foreground is
tied to the calling process: a wrapper timeout or a sleeping laptop ends the
command while the tests may keep going inside the master. Use
`run --detach`; `status` shows the state and the output tail, and `wait`
(optionally `--timeout`) fetches `nosetests.xml` and prints a per-test summary
with the cause of each failure. `run --tests X` selects tests without editing
the env file.

Several classes in one `run` share the containers, so state an uninstall
leaves behind reaches the next class; that is a real finding, not noise. For
an independent result use `reset` (fresh containers, previous logs kept as
`logs.<timestamp>`).

`up` picks a free podman subnet when the env names none, and fails with the
owning network's name when a named one is taken. It warns when the
`dns_forwarder` does not answer from the master: an install with DNS fails
its forwarder check on a resolver that is merely down.

`overlay SRC:/DST ...` copies files into every container and verifies each by
SHA-256. It records them in `<workdir>/overlay.json`; `show` and the run
summary then say the run is not a packaged one. Sources must be final files,
not `*.in` templates.

`list` shows every environment on the host (from its podman network, so
orphans whose workdir is gone appear too); `prune` removes the stopped ones
(dry run unless `--yes`).

## Env file

```yaml
name: base-xmlrpc
provider: podman            # or: external (pre-created), nested (get a VM via API)
domain: ipa.test
dist: 44
image: freeipa-current      # build channel; the provider resolves the
                            # concrete image (per-host image: overrides)
hosts:
  - {role: master, name: master1}
  - {role: client, name: client1}
resources:                  # defaults mirror Azure variables-fedora.yml
  master: {memory: 1800m, memory-swap: 2500m}
  client: {memory: 512m, memory-swap: 1024m}
run:
  mode: base                # or: integration
  setup_dns: true
  setup_kra: true
  forwarder: 8.8.8.8        # explicit; else --auto-forwarders
  tests: [test_xmlrpc]
  ignore: [test_xmlrpc/test_dns_plugin.py]
```

External (pre-created) systems: set `provider: external` (or just give a
host an `address:`). Hosts then need `name`/`role`/`address` (plus optional
`user`, `port`, `image`, `os`). `up` only validates reachability/SSH/
hostname/distribution/IPA state and never mutates; `run` is unchanged; `down`
collects logs without destroying anything. Authenticate with `--ssh-key`
(default `~/.ssh/id_rsa`).

## Nested provider (get a VM, run the env in it)

For cloud deployments where the test VMs exist only **through an API**, set
`provider: nested`. A nested provider composes an **outer VM backend**
("get a VM") with an **inner provider** (default `podman`, "run the env in
the VM"). The inner environment is this same spec with `provider` set to
`inner` and the `vm:` block stripped — the `hosts:` become containers in the
provisioned VM, and the whole podman flow runs there. It is the supervisor's
pre-allocated runner generalized: the runner is acquired by the backend on
demand and released on `down`.

```yaml
name: nested-example
provider: nested
image: freeipa-current
inner: podman              # inner provider (default podman)
vm:
  backend: command         # ssh | command | openstack | qemu
  count: 1                 # VMs to provision (podman inner uses the 1st)
  provision_cmd: examples/nested/get-vm.sh      # $1 = count; print user@host[:port] / line
  deprovision_cmd: examples/nested/drop-vm.sh   # $1 = vm id/target
hosts:
  - {role: master, name: master1}
  - {role: replica, name: replica1}
run: {mode: integration, tests: [test_integration/test_simple_replication.py]}
```

**VM backends** (`freeipa_env/vmbackend.py`):

* `ssh` — a pool of pre-allocated `user@host[:port]` entries (`vm.hosts`);
  `terminate` is a no-op. The "no real API" reference case.
* `command` — shell out to `vm.provision_cmd <count>` (stdout: one
  `user@host[:port]` per line, or a JSON list of handles) and
  `vm.deprovision_cmd <handle>`. The **generic cloud-API integration
  point**: wrap any cloud in two scripts. See `examples/nested/`.
* `openstack` — a concrete real-API example on the `openstack` CLI
  (`server create`/`show`/`delete`, floating IPs); the template for any
  other cloud.
* `qemu` — local VMs under qemu/KVM on the control node (no cloud API): a
  qcow2 overlay on `vm.image` (a cloud base image, e.g. Fedora Cloud Base),
  cloud-init seeds the control node's ssh key for root, ssh over a forwarded
  localhost port. Each VM also gets a private NIC on `192.168.100.0/24`
  (`vm1` = `.10`, `vm2` = `.11`, …) with peers in `/etc/hosts`, so VMs reach
  each other by name. `vm.vms` overrides `name`, `image`, `cpus`, `memory`,
  `disk` per VM; `vm.count` must equal its length. `terminate` kills the qemu
  process and removes the overlay; `console.log` is kept under `vm.state_dir`.
  Needs `/dev/kvm` and the host packages listed below.
  `vm.gpu: virgl` gives a VM virtio-gpu with OpenGL rendered on the control
  node's GPU (`vm.render_node`, default `/dev/dri/renderD128`, must be
  read/write for the user). It needs the `qemu-device-display-virtio-gpu-gl`
  and `qemu-ui-egl-headless` packages. Guest check: `EGL_PLATFORM=surfaceless
  eglinfo` should report the `virtio_gpu` driver and a `virgl` renderer.
  Each such VM also serves its screen as VNC + websocket on loopback; `up`
  prints the port. View it in a browser with noVNC (`novnc` package): open
  `vnc.html?host=127.0.0.1&port=<ws>`. From another machine, tunnel with
  `ssh -L <ws>:127.0.0.1:<ws>`.

  `vm.usb: [VVVV:PPPP, ...]` (per VM in `vm.vms[i]`) passes host USB devices to
  that VM through qemu `usb-host` on an xHCI controller. Each entry claims one
  free host device with that vendor:product id; a device goes to one VM only.
  Attached at boot only: no hot-plug. The device disappears from the host while
  the VM runs. The user must be able to read/write `/dev/bus/usb/BBB/DDD` for
  the device, or `up` fails with that path; a udev rule is the usual fix.

  **Host packages (qemu backend, Fedora).** Required:
  ```
  sudo dnf install qemu-system-x86-core qemu-img genisoimage rsync openssh-clients
  ```
  `genisoimage` may be replaced by `xorriso` or `mkisofs`. Add for `vm.gpu: virgl`:
  ```
  sudo dnf install qemu-device-display-virtio-gpu-gl qemu-ui-egl-headless virglrenderer
  ```
  Optional, to view the VM screen in a browser: `sudo dnf install novnc`.
  Guest-side GL (`mesa-dri-drivers`, `egl-utils`) is installed inside the VM, not on the host.
  The user must be able to read `/dev/kvm` and, for virgl, read/write `render_node`.

**Lifecycle.** `up`: provision → wait for ssh → rsync the ci-freeipa tree + the
inner spec to the VM → run the inner `freeipa-env up` there → save the VM
handles to `<workdir>/nested-state.json` (so a later `down` knows what to
tear down). `down`: run the inner `freeipa-env down` (collects logs), rsync
the remote workdir back (so `logs` and the xunit work locally), release the
VMs, clear state. `run` drives the inner `freeipa-env run` on the primary VM.
`resolve` is deferred — the VM does not exist yet, so image resolution
happens on the VM at `up` time (a provisioned VM is expected to carry the
`freeipa-ci` image, or set `vm.image_ref` to `podman pull` it).

With `inner: external` nothing runs in containers: each host runs directly on
the VM it names with `hosts[].vm` (a VM from the backend's list; for qemu,
`vm.vms[].name`). Every host needs a `vm:`, no VM may serve two hosts, and
`vm.count` must equal the host count. `up` provisions the VMs, maps each host
to its VM's forwarded ssh port (`address`/`port`) and private address (`ip`),
writes the inner external spec, and attaches through `ExternalProvider` from the
control node. `run` and `show` use the same provider. No ci-tree bootstrap runs
on the VMs. Example: `env/presets/nested-qemu-external.yaml`.

The nested provider runs on the **control node** (a CI machine with the
cloud API creds + ssh). A queue of `provider: nested` presets runs under the
existing `freeipa-env queue` supervisor on a control-node `--runner`; each
job provisions its own VM(s).

## Artifacts

`run` writes:

- `<workdir>/nosetests.xml` — JUnit XML from `ipa-run-tests --with-xunit`
- `<workdir>/logs/` — per-host journal + `/var/log/ipa*` tarballs, install
  and test run logs
- `IPA_TESTS_LOGSDIR` (in-container) — per-test logs from the framework
  (`--logfile-dir`); `down`/`collect_logs` also fetch this controller tree
  into `<workdir>/logs/collected/<master>/ipa-env/logs` so `logs` can surface it
  (see the `per-test` category below)

## Log analysis (`logs`)

`freeipa-env logs` turns the collected artifacts into a triage view for
resolving CI issues. With no `--category` it prints a per-category summary
(source count + interesting-line count per category);
`--category NAME` (repeatable, or `all`) prints the matching lines plus a
tail per source file:

```
freeipa-env logs ENV.YAML                        # category summary
freeipa-env logs ENV.YAML --category tests       # failed tests, grouped by module
freeipa-env logs ENV.YAML --category ds --category httpd --category kra  # several at once
freeipa-env logs ENV.YAML --category tests --json  # machine-readable summary
freeipa-env logs --workdir DIR --category system --pattern 'OOM|segfault'
```

Categories (each with a sensible default line filter, overridable via
`--pattern REX`):

| category    | source                                   |
|-------------|------------------------------------------|
| `tests`     | `nosetests.xml` (JUnit): totals + failed/errored tests with messages and traceback tails, grouped by module |
| `per-test`  | the framework's per-test log tree (one dir per test nodeid): per-host install/uninstall logs + journal, with the xunit status joined in |
| `run`       | `logs/run.log` — the streamed install/test/uninstall console (FAILED/ERROR/summary lines) |
| `install`   | `ipaserver-install.log` (ERROR/WARNING) |
| `uninstall` | `ipaserver-uninstall.log` (ERROR/WARNING) |
| `ds`        | 389-ds `errors` log (`- ERR -`/`- CRIT -` lines) |
| `httpd`     | `error_log` — IPA framework errors + Python tracebacks (debug noise excluded) |
| `kra` / `ca`| pki instance logs (`pki-tomcat/{kra,ca}` + spawn logs) |
| `kdc`       | `krb5kdc.log` (error/crit/fatal) |
| `samba`     | `/var/log/samba` (AD trust) |
| `dns`       | BIND runtime logs (`named.run`) |
| `ipa`       | per-operation logs under `/var/log/ipa/` (ERROR/CRITICAL) |
| `system`    | boot journal: failed units, OOM, segfaults, watchdogs |

Useful flags: `--pattern REX` (override a category's line filter),
`--lines N` (matched lines per file, default 30), `--tail N` (0 disables),
`--all` (print whole files unfiltered), `--host NAME`, `--json` (summary
for scripting), `--refresh` (re-collect logs from a still-running
environment before analyzing; a failed collection never clobbers existing
artifacts). Daemon tarballs are extracted selectively into
`<workdir>/logs/extracted/<host>/` and cached by mtime+size, so repeated
calls are instant.

### Self-contained HTML report (`report`, `--html`)

`freeipa-env report` renders the collected artifacts into a single
self-contained `results.html` (default `<workdir>/results.html`) for sharing
or pasting into a ticket. It reuses the same `LogStore` / `loganalyze`
machinery as `logs`, so it is **offline** (no network, no env file needed for
an already-collected workdir) and **dependency-free** to view: all CSS is
inlined, there is no JavaScript and no external asset, and sections collapse
with the native `<details>` element. Each failed/errored/skipped test row
carries its failure message and a traceback tail; the per-category log views
are collapsed.

```
freeipa-env report --workdir DIR                 # <workdir>/results.html
freeipa-env report --workdir DIR -o /tmp/r.html  # explicit output
freeipa-env logs ENV.YAML --html                 # same report, as a logs variant
freeipa-env tf-logs <request-id> --html          # fetch a TF request, then render
```

The report is intentionally coarser than PRCI's in-image pytest-html `report.html`:
it shows per-test status + duration from the xunit plus the shared per-category
logs and the framework's per-test log trees (a `Per-test logs` section, one
collapsed block per test nodeid with the xunit status joined in), but not
per-test captured stdout (PRCI gets that from pytest-html running in the image,
which this one does not add). When a per-stage `results.yaml` is present in the
workdir (the TF
transport uploads it with the request; see below) a `Stages` table shows each
stage's result, duration and note.

## Presets

- `presets/base-xmlrpc.yaml` — the Azure **xmlrpc** job (1 master,
  DNS+KRA, `test_xmlrpc` minus `test_dns_plugin.py`).
- `presets/external-smoke.yaml` — external-provider smoke template.
- `presets/azure/` — one preset per Azure CI job (the full recreation of
  `ipatests/azure/azure_definitions/{base,gating}-fedora.yml`):

| preset (`presets/azure/`) | Azure job | hosts | suite |
|---|---|---|---|
| `base.yaml` | base | master | unit tests + `test_xmlrpc/test_dns_plugin.py` (`-k 'not test_dns_soa'`) |
| `kerberos-flags.yaml` | kerberos_flags | master, client | `test_kerberos_flags.py` |
| `forced-client-reenrollment.yaml` | forced_client_reenrollment | master, replica, client | `test_forced_client_reenrollment.py` |
| `external-ca-install.yaml` | external_ca_ExternalCAInstall | master | `test_external_ca.py::TestExternalCAInstall` |
| `simple-replication.yaml` | simple_replication | master, replica | `test_simple_replication.py` |
| `service-permissions.yaml` | service_permissions | master | `test_service_permissions.py` |
| `sudo.yaml` | sudo | master, client | `test_sudo.py` (deselects `TestSudo_Functional`) |
| `external-ca.yaml` | external_ca_ExternalCA | master, replica, client | `test_external_ca.py::TestExternalCA` |
| `topologies-testconfig.yaml` | topologies_and_testconfig | master | `test_topologies.py`, `test_testconfig.py` (no installs) |
| `external-ca-constraints.yaml` | external_ca_ExternalCAConstraints | master, client | `test_external_ca.py::TestExternalCAConstraints` |
| `membermanager.yaml` | membermanager | master | `test_membermanager.py` |
| `netgroup.yaml` | netgroup | master | `test_netgroup.py` |
| `external-ca-profile-scenarios.yaml` | external_ca_ExternalCAProfileScenarios | master | `test_external_ca.py::TestExternalCAProfileScenarios` |
| `caless-to-ca-full.yaml` | ServerReplicaCALessToCAFull | master, replica | `test_caless.py::TestServerReplicaCALessToCAFull` |
| `external-ca-self-external-self.yaml` | external_ca_SelfExternalSelf | master | `test_external_ca.py::TestSelfExternalSelf` |

The `xmlrpc` job is `presets/base-xmlrpc.yaml`. Base-pipeline jobs run in
`mode: base` (run-base-tests.sh: install → test → uninstall); gating jobs
run in `mode: integration`, where the framework performs the per-class
install/uninstall from the multihost config. Per-host memory limits mirror
the Azure definitions.

## Migrated PRCI definitions (`migrate`)

`freeipa-env migrate ipatests/prci_definitions/<def>.yaml` converts a PRCI
definition into freeipa-env presets (one per job) under
`presets/prci/<def>/`, with a `MIGRATED.md` report. Regenerate after PRCI
definition changes; options: `-o DIR`, `--jobs NAME` (substring filter).

`presets/prci/` currently contains all 11 PRCI definitions
(1301 presets):

dir | PRCI file | presets
---|---|---
`gating/` | `gating.yaml` | 31
`nightly-latest/` | `nightly_latest.yaml` | 185
`nightly-latest-389ds/` | `nightly_latest_389ds.yaml` | 48
`nightly-latest-pki/` | `nightly_latest_pki.yaml` | 91
`nightly-latest-selinux/` | `nightly_latest_selinux.yaml` | 185
`nightly-latest-sssd/` | `nightly_latest_sssd.yaml` | 20
`nightly-latest-testing/` | `nightly_latest_testing.yaml` | 185
`nightly-latest-testing-selinux/` | `nightly_latest_testing_selinux.yaml` | 185
`nightly-previous/` | `nightly_previous.yaml` | 185
`nightly-rawhide/` | `nightly_rawhide.yaml` | 185
`temp-commit/` | `temp_commit.yaml` | 1

Mapping rules:

- **topology → hosts**: `ipaserver` → 1 master (base mode);
  `master[_Nrepl]_Mclient` → master/replica/client hosts (integration
  mode when >1 IPA host); AD tokens `ad` / `adroot_adchild_adtree` add
  external AD hosts with roles `ad`, `ad_subdomain`, `ad_treedomain`.
- **topology memory → per-role limits**, split evenly (rounded to 100 MiB,
  floor 512 MiB) — PRCI numbers are per-topology totals.
- **test_suite → `run.tests`** (space-separated list preserved).
- **class Build** → skipped (the `freeipa-ci` image pipeline replaces it);
  **ipa_ipa_trust** topologies → skipped (two IPA domains; the preset
  model supports one IPA domain plus AD domains).
- **RunWebuiTests** → `mode: base` with a NOTE: PRCI provisions a browser
  + selenium; `freeipa-ci/full` does not include them, so those presets
  will not pass as-is.

**AD hosts are external.** PRCI provisions the AD DCs itself; here each AD
host is an `address` placeholder (RFC 5737) plus a placeholder FQDN — edit
both before `freeipa-env up` (the preset footer lists every EDIT ME). The
IPA containers resolve the AD FQDNs via `/etc/hosts` entries written at
`up`; trust setup itself (AD admin credentials, DNS) is done by the tests
via `ad_admin_name`/`ad_admin_password` (defaults `Administrator`/
`Secret123`). A Samba AD DC (or Windows) reachable over SSH for log
collection works; Windows-only DCs need `user` adjusted.

## Run queues and the supervisor (`queue`)

Queues run ordered preset lists on a pool of **pre-allocated runners**
(hosts with ssh + podman + systemd, e.g. the lab VMs) — the replacement
for PRCI's per-PR job scheduling. Queue files live in `queues/`:

* `azure.yaml` — the 16 Azure CI jobs (Azure ran them in parallel; the
  queue order is conventional);
* one per PRCI definition, named like the preset dirs
  (`gating.yaml`, `nightly-latest.yaml`, …) — each in the PRCI
  definition's job order, generated with `freeipa-env queue generate`
  (regenerate after PRCI definition changes).

Queue format: an ordered `jobs:` list of preset paths (relative to
`env/`), a directory entry (trailing `/`) expanding to all presets in
it sorted, or per-job dicts (`preset`/`dir` + `note`).

**Image references are build channels, never builds.** A preset pins
no build — it names a *channel*: `image: freeipa-current` (the current
release's newest build), `freeipa-next` (the next release, PRCI
`fedora-rawhide`) or `freeipa-previous` (the previous release). This
mirrors PRCI, where each definition file references one build generation
and every job in it uses that one build. `images/build.sh --channel
NAME` tags each build with the channel tag (`freeipa-ci/full:<channel>`)
alongside the immutable provenance tag (`freeipa-ci/full:<dist>-<sha>`).

Resolving a channel to a concrete image on a given host is a **provider
task**: the podman provider maps the channel to its podman tag and
resolves it against the local image store at `up` time (and
`freeipa-env resolve ENV.YAML` does just that one thing, printing
`<ref> <concrete> <image_id>`); the external provider ignores it —
external hosts run their own IPA. A preset may also name an explicit
image reference (passed through unchanged) if you need to pin a build.

```
freeipa-env queue run queues/gating.yaml \
    --runner root@192.168.122.215 --runner root@192.168.122.216 \
    [--jobs NAME ...] [--limit N] [--dry-run] [--keep-on-failure] \
    [--job-timeout SECS] [--outdir DIR] [--no-bootstrap] [--ssh-key FILE]
```

The supervisor, per run:

1. validates every preset (parse + existence),
2. checks each runner (ssh + podman) and rsyncs the local ci-freeipa tree to
   `--remote-ci` (default `/root/freeipa-ci/ci`),
3. asks each runner's provider to resolve every distinct preset's image
   references (remote `freeipa-env resolve`; fail fast on a missing
   image, before any job starts; resolutions are recorded per runner in
   `summary.md`),
4. schedules: each runner pulls the next job from the front of the list,
   one job at a time — `up` → `run` → `down`, under a per-job remote
   `timeout` (default 4 h), workdir `/root/jobs/<job-key>` per job.
5. writes `summary.tsv` / `summary.md` + per-job transcripts to `--outdir`
   (default `./<queue>.queue`); exit code 0 only when every job passed.

Ordering semantics: with one runner the jobs run exactly in queue order;
with N runners each runner's subsequence preserves queue order (FIFO
dequeue) — the same shape as a PRCI run queue spread over a pool of VMs.
`--keep-on-failure` skips `down` on a failed job so the environment stays
on the runner for `freeipa-env logs --refresh`-style triage (remember to
`down` manually before reusing the runner's capacity).

## Runner transports (`--runner`)

A *runner* is a host that can execute a queued job (podman + systemd + a
copy of the ci-freeipa tree). Each `--runner SPEC` names one transport:

| spec | transport | how jobs run |
|---|---|---|
| `user@host[:port]` (default) | **ssh** | a pre-allocated runner reached over key-based ssh; the ci-freeipa tree is rsynced there and `up`/`run`/`down` execute remotely |
| `local` | **local** | the control node is the runner; no ssh, the same job flow runs as local subprocesses (needs `podman` on the control node) |
| `testing-farm` | **TF** | no ssh, no pre-allocated host: a runner's queued jobs run as **one Testing Farm** request whose guest provisions once, builds the SRPM + channel images once, and runs the presets in sequence |

All three share one job recipe (`up` → `run` → `down`, under a per-job
timeout; `down` is unbounded and must complete), so queue semantics and the
`summary.tsv`/`summary.md` + per-job transcript shapes are transport-neutral.

**Runner-style paths are de-rooted.** Remote/local job paths are written
`~/...` (unquoted), so the *remote* shell expands them against the ssh user's
home — for a non-root user that keeps the tree out of `/` (the default
`--remote-ci ~/freeipa-ci/ci`, `--jobs-dir ~/freeipa-jobs` work unchanged for
any user). The local runner expands `~/...` against the local user's home.

### Testing Farm transport

`--runner testing-farm` sends each runner's queued jobs to the public
[Testing Farm](https://docs.testing-farm.io/) API as **one request** (one
request per runner subsequence). There is no pre-allocated host and no root
on any machine you control: TF provisions a fresh Fedora guest, checks out
the **FreeIPA repo under test** itself, and runs the pinned plan from that
checkout — the plan discovers this repo's test at the pinned ref. The guest
builds the SRPM and every needed channel image **once**, then runs the presets in
sequence, reusing the baked images. A preset **PASSes** only when the request
completes and that preset's own `results.yaml` grades `passed`; the request
itself fails if any preset's run step fails.

Required: an API token — pass `--tf-token TOKEN` or set the
`TESTING_FARM_API_TOKEN` environment variable. The repo the guest clones is
`--tf-repo-url` (default: the `remote.origin.url` of `--freeipa-src`) at
`--tf-ref` (default: the current branch of `--freeipa-src`); both should point at a
**publicly pushed** commit, since TF clones over the network. Other knobs:
`--tf-url` (API base), `--tf-arch` (default `x86_64`), `--tf-compose`
(default `Fedora-44`), `--tf-plan` (the plan node in the FreeIPA repo, default
`/ci/tmt/plans/freeipa-env`), and `--tf-variable KEY=VALUE` (repeatable, extra
variables for the request).

**The guest builds the SRPM itself — no pre-supplied SRPM.** The tmt plan's
`prepare` step (`how: install`) places `git`, `podman`, the autotools
toolchain and every `BuildRequires` of the branch's `freeipa.spec.in` on the guest (the
[synta](https://pagure.io/synta) pattern). TF's pipeline only syncs the fmf
tree to the guest as a plain file copy (no `.git`, no submodule contents),
so the tmt test clones the repo under test itself first — `git clone --recursive` +
`git checkout` of `--tf-repo-url`/`--tf-ref` — then produces `freeipa`'s
SRPM on the guest: `autoreconf -i`, `./configure` with the spec's
rpm-equivalent flags, and `make srpms`. It bakes each of the presets'
channel images from that SRPM with `images/build.sh --srpm` **once each**
(a channel already baked is reused), then runs every preset in sequence —
each a fresh `freeipa-env up` → `run` → `down` under its own `--job-timeout`
— and the request fails if any run step fails. Pass `--srpm <URL>` only to
override with a prebuilt SRPM (it must be an HTTP(S) URL the guest can fetch).

**Per-stage results + downloadable job artifacts.** The tmt test declares
`result: custom`, so each preset is reported with one result per stage
(`srpm-build`, `image-build`, `env-up`, `test-run`, `env-down`) plus a parent
result, each with its own console log; the batch as a whole carries a parent
result that is `fail` when any preset's run step fails. Each preset's workdir
artifacts (run console, `nosetests.xml`, collected per-host logs) are
reparented under `.../data/artifacts/<preset-key>/` and its `results.yaml`
under `.../data/<preset-key>/`, so every preset is downloadable and gradable
on its own.

**Analyze a TF request's artifacts with `logs`.** `freeipa-env tf-logs
<request-id>` downloads a finished request's artifacts into a local workdir in
the same layout a local/ssh job uses, then runs the usual `logs` machinery —
no env file needed. A batched request stores **one workdir per preset**
(`<workdir>/<preset-key>/` with its own `logs/`, `stages/`, `results.yaml`)
and a single `report` per preset (`--html` writes one `results.html` per
preset); a legacy single-preset request stores one flat workdir. The request
id is the one printed in the `queue run` transcript (or from the TF API).
`--category`/`--pattern`/`--json`/`--host` work exactly as for `logs`;
`--workdir DIR` picks where the artifacts are stored (default
`./tf-<request-id>.env`) and re-running against the same dir reuses the
downloaded files.

A batched request was validated end-to-end: the guest built the SRPM and
channel image once, ran each queued preset in sequence, and `tf-logs --html`
produced one `results.html` per preset alongside the batch parent.

```
freeipa-env tf-logs <request-id>
freeipa-env tf-logs <request-id> --category tests --category run --lines 20
freeipa-env tf-logs <request-id> --html          # fetch, then results.html
```

`tf-logs` also fetches the job's per-stage `results.yaml` (the tmt test's
`result: custom` outcome, uploaded with the request) into the workdir, so the
`report`/`--html` output includes a `Stages` table (per-stage result,
duration, note) alongside the per-test xunit table and the collapsed
log categories.

Fetching a finished request needs no token: both the request state
(`GET /v0.1/requests/<id>`) and the artifact store are public. (A token is
only needed to *submit* a request via `queue run --runner testing-farm`.)

```
freeipa-env queue run queues/gating.yaml \
    --runner testing-farm \
    --tf-token $TESTING_FARM_API_TOKEN \
    --tf-repo-url https://github.com/abbra/freeipa.git \
    --tf-ref modrnize-ci \
    --job-timeout 21600        # one build + several runs (default 4 h/preset)
```

The supervisor polls the single request to a terminal state under a batch
deadline (`--job-timeout`, default 14400 = 4 h per preset, plus a build
buffer) and cancels it if the deadline is reached; a full on-guest build
(SRPM + binary RPMs + image) happens once and is shared by every preset in
the request. `--dry-run` with a `testing-farm` runner prints the exact batch
request JSON that would be submitted (a token is still required, but nothing
is actually sent).

**Released packages, no FreeIPA checkout (the default).** Without
`--freeipa-src`, `--tf-repo-url`, `--srpm` and `--copr` the packages under
test are the ones Fedora ships: nothing is built and no
FreeIPA repo is involved, so the request names **this** repository and its
own plan `/tmt/plans/distro` (`tmt/plans/distro.fmf`: `prepare` installs
only `podman` and `python3-pyyaml`, the test comes from the same ref, so
there is no pin to bump), at this
checkout's origin and current branch, which must be pushed. The guest bakes
the channel images for its own release, so the compose picks the Fedora
under test:

```
freeipa-env queue run queues/azure.yaml \
    --runner testing-farm --tf-compose Fedora-45
```

To run that plan from another clone of this repository, name it with
`--tf-repo-url`/`--tf-ref` and pass `--ipa-from-distro` explicitly (a bare
`--tf-repo-url` means a FreeIPA repo to build).

**Packages from a COPR repository.** Add `--copr OWNER/PROJECT` (repeatable)
to the same command: the same plan runs, and the guest installs the IPA
packages from the COPR and the distribution together, for its own release
(the COPR must publish builds for that Fedora).

## Fedora dist-git lane (packaging PR CI)

Fedora dist-git CI (Packit) runs a **Koji scratch build** for every dist-git
PR before it runs the tests, and passes its task id to the test request as
`KOJI_TASK_ID`. The test REUSES that build: `tmt/tests/freeipa-env/
koji-rpms.py` reads the task's `buildArch` subtask results and downloads
the binary RPMs (x86_64 + noarch, debuginfo/debugsource excluded) from
kojipkgs; the channel image is baked from them with `build.sh --rpms` --
no SRPM, no build image, no rpmbuild. The scratch build is for the PR's
target dist, so this lane bakes the base/channel images for the guest's
dist (`images/build.sh --dist`, `base/Dockerfile` takes `ARG DIST`;
rawhide guests map to the rolling `rawhide` toolbox tag).

Fallback when no scratch build was made (manual runs, a CI without Packit):
with `FREEIPA_DISTGIT=1` the test builds the SRPM from the dist-git
checkout the plan itself ships -- a dist-git repo carries the spec, the
patches, and a `sources` file with the tarball hashes, no configure.ac.
`tmt/tests/freeipa-env/distgit-srpm.sh` takes the plan tree (`$TMT_TREE`,
which the CI syncs to the guest), fetches every `sources` entry from the
Fedora lookaside, and runs `rpmbuild -bs`; the channel image is then baked
from that SRPM with `--builddep` (the build image tops up any BuildRequires
the PR's spec adds beyond the baked list) and the guest's dist. Overrides:
`FREEIPA_DISTGIT_DIR` (tree path, default `$TMT_TREE`),
`FREEIPA_DISTGIT_SPEC` (spec file name), `FREEIPA_DISTGIT_PKG` (lookaside
package name, default the spec's `Name:`), `FREEIPA_LOOKASIDE_URL`
(lookaside base).

The dist-git-side plan is the same pin as the upstream one, with a smaller
`prepare` (the binary build happens inside the build image, so the guest
needs no BuildRequires):

```yaml
discover:
  how: fmf
  url: https://github.com/abbra/ci-freeipa.git
  ref: <pinned tag>
  test: "^/tmt/tests/"
prepare:
  how: install
  package: [podman, curl, git, rpm-build]
execute:
  how: tmt
environment:
  # fallback source when the request carries no KOJI_TASK_ID
  FREEIPA_DISTGIT: "1"
  FREEIPA_JOBS: '[{"key": "base-unit", "preset_rel": "presets/azure/base.yaml"},
                  {"key": "base-xmlrpc", "preset_rel": "presets/base-xmlrpc.yaml"}]'
```

The selected suites are whatever `FREEIPA_JOBS` lists (presets from this
repo). Source precedence on the guest: `FREEIPA_SRPM_URL` > `KOJI_TASK_ID`
(scratch-build reuse) > `FREEIPA_DISTGIT` > the upstream clone/SRPM build.

## Test the packages Fedora ships (the default)

When no other source is named (no `--srpm`/`build.srpm`, no `--rpms`, no
`--copr`; `--copr` alone installs from that COPR, see the end of this
section) the image is baked from the distribution's own packages, to test
a Fedora release as released: `images/build.sh` runs `dnf -y install` of
every IPA
subpackage (the server with DNS and trust-ad, the client, the `python3-*`
bits and `python3-ipatests`) against the repositories of `--dist` as the
`fedora-toolbox` image enables them (`fedora` and `updates`, not
`updates-testing`). The tests that run are the ones that release's
`python3-ipatests` installs, so they always match the packages. The image
carries the label `source: distro`; `freeipa-env show` prints it next to the
installed builds. `--ipa-from-distro` states the choice explicitly and is
refused together with `--copr` or `--srpm` (with a
COPR enabled dnf would prefer its builds and the result would no longer be
the distribution's).

The image is a snapshot of the repositories at bake time: rebake to pick up
newer updates. `--add-ipa-package SPEC` adds a package to the default
list, `--ipa-packages 'SPECS'` replaces the list.

- **Single machine, a dedicated image** — the safe choice on a host that
  also holds a worktree or COPR build, because a channel image that is
  already present is never rebuilt and `up` would then test that one:

  ```
  images/build.sh --dist 45 --tag freeipa-distro
  ```

  The env file names it directly, `image: freeipa-distro/full:45` with
  `dist: 45`. To run a catalog preset against it, copy the preset and change
  those two lines.

- **Single machine, the channel image** — `freeipa-env up ENV.YAML` (or
  `ensure`) bakes the env's missing channel image for the env's `dist:`;
  `ensure --build` rebakes a present one, **replacing** whatever build the
  channel tag held.

- **Queue** — `freeipa-env queue run QUEUE --runner local` (or `USER@HOST`)
  bakes the missing channel images on each runner before any job starts.
  Host runners bake for dist 44; for another release bake the channel first
  (`images/build.sh --dist N`).

- **Testing Farm** — `--runner testing-farm --tf-compose Fedora-N`; see
  **Testing Farm transport** above.

Builds published in a COPR repository use the same bake: add
`--copr OWNER/PROJECT` (repeatable) to any of the commands above
(`--ipa-from-copr` states it explicitly). dnf then resolves the
IPA packages against the COPR and the distribution together.

## Build the IPA RPMs ourselves

By default `freeipa-env` consumes IPA RPMs that some build farm produced and
baked into a `freeipa-ci/full` image, which you then distribute. A
delivered RPM's distro dependencies (e.g. samba) can outlive its snapshot: a
newer samba lands in the repo, the old IPA RPM's `Requires` no longer
resolves, and the test image build breaks for reasons unrelated to the IPA
change under test. The self-build lane removes that failure class: the IPA
binary RPMs are compiled **from the same git snapshot** inside a dedicated
build image on the same host that bakes the test image, so build and test
share one distro repo snapshot (design §3.10).

The lane, end to end:

1. **Control node** — `scripts/make-srpms.sh` autogens + configures the
   checkout and runs `make srpms`, producing
   `dist/srpms/freeipa-<ver>.src.rpm`. This is the only step that needs the
   IPA source tree; it mirrors `makerpms.sh` (same configure triplet), so
   the SRPM records the same host/layout a local `make rpms` would.
2. **Ship** — the SRPM reaches the host that runs the environment: locally
   (single machine) or rsynced by the supervisor to each runner (queues).
3. **Build + bake** — `images/build.sh --srpm <path>` compiles the SRPM
   inside the dedicated `freeipa-ci/build:<dist>` image (the base image plus
   every `BuildRequires` from the spec, baked once per dist — not `dnf
   builddep` per build) with `rpmbuild --nocheck --rebuild` (`--nocheck`
   skips the spec's `%check` unit-test suite, which CI runs in its own
   `run` lane), collects the fresh binary RPMs, bakes
   `freeipa-ci/full:<dist>` from them, and tags it with the channel.

Three entry points, one underlying command:

- **Single machine** — put a `build:` block on the env file; `up` builds any
  missing channel image before it resolves (and `freeipa-env ensure` does
  just that half):

  ```yaml
  name: base-xmlrpc
  provider: podman
  image: freeipa-current
  build:
    srpm: dist/srpms/freeipa-16.0.0-0.20260101gitabc1234.src.rpm
    # force: true    # rebuild even if the channel image is already present
  hosts:
    - {role: master, name: master1}
  ```

  ```
  freeipa-env ensure ENV.YAML          # build the missing channel images
  freeipa-env ensure ENV.YAML --build  # force a rebuild
  ```

- **Queue** — pass the SRPM (a file or the `dist/srpms/` dir) to `queue run`;
  the supervisor ships it to each runner and builds any channel image the
  runner is missing, before any job starts:

  ```
  freeipa-env queue run queues/gating.yaml \
      --runner root@192.168.122.215 \
      --srpm dist/srpms \
      [--srpm-dir /root/freeipa-ci/srpm] [--image-timeout 9000]
  ```

**Freshness model.** A channel image already present on the host is left
untouched; only missing channels are built (a full IPA build + image bake
can take a while). Force a rebuild with `build.force` in the env file,
`freeipa-env ensure --build`, or by removing the channel image. Without an
SRPM (`--srpm`, or a `build:` block on the env file) a missing channel image
is instead baked with dnf from the distribution's packages, or from the
`--copr` repositories when any is given (see above).

## Preset validation (`check`)

`freeipa-env check [PRESETS ...]` validates preset files **without**
spawning containers or touching the network — the CI gate for the 1300+
presets (migrated PRCI, Azure, nested). With no arguments it checks every
`*.yaml` under `env/presets/` (plus the top-level presets); pass explicit
files or directories to narrow the scope. Each preset must parse as an
`EnvSpec` and pass the provider's read-only sanity checks:

* `provider: podman` — every containerized host resolves an image, and a
  channel-style name is a *known* build channel (`freeipa-current/next/
  previous`); a bare name that is neither a channel nor an image reference
  is flagged as a likely typo.
* `provider: nested` — the `vm:` block builds a known VM backend and
  `inner` names a known provider; with `inner: external` every host has a `vm:`.
* `provider: external` — every host carries an address.

One line is printed per failing preset, then a summary; the exit code is 0
iff every preset is sound and no path is missing. Wire it into the PR gate
so a preset edit can't silently break a queue:

```
freeipa-env check   # from the repo root; checks all presets, exits non-zero on any problem
```

## Notes

- The podman provider pins one network per env (10.89.0.0/24 +
  2001:db8:1::/64); one env per host in v1. A preset replaces those with
  `subnet_v4:` / `subnet_v6:` when the defaults collide with a podman
  network another project already created on the same host.
- `run` in base mode copies `scripts/run-base-tests.sh` into the master
  container and runs it there; the JUnit report is fetched back afterwards.
- Uninstall is part of the workflow (install → test → uninstall); the
  container is destroyed only by `down`, after uninstall completes.
