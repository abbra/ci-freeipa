# ci-freeipa: FreeIPA CI tools

This repository contains the FreeIPA CI tools. It has these parts:

- `freeipa-env`: a tool that creates or attaches FreeIPA test environments. It reads one YAML file and runs the install, test, and uninstall steps.
- The image pipeline, the preset catalog, and the queue catalog.
- A tmt test that runs a job batch on a Testing Farm guest.

The FreeIPA source tree does not contain this code. It contains only a pinned tmt plan. That plan runs the test from this repository against the checkout under test. See [Repository split](#repository-split). The design is in the FreeIPA repository, in `doc/designs/ci_modernization.md`.

Use these guides for specific tasks:

- To test a FreeIPA worktree with an agent, read [ci-freeipa-worktree](.agents/skills/ci-freeipa-worktree/SKILL.md).
- To test the packages that a Fedora release ships, read [Test the packages Fedora ships](#test-the-packages-fedora-ships-the-default) and [ci-freeipa-distro-packages](.agents/skills/ci-freeipa-distro-packages/SKILL.md). You do not need a checkout, a build, or a COPR repository.

## Contents

- [Repository split](#repository-split)
- [Commands](#commands)
- [Env file](#env-file)
- [Nested provider](#nested-provider-get-a-vm-run-the-env-in-it)
- [Artifacts](#artifacts)
- [Log analysis](#log-analysis-logs)
- [Presets](#presets)
- [Migrated PRCI definitions](#migrated-prci-definitions-migrate)
- [Run queues](#run-queues-and-the-supervisor-queue)
- [Runner transports](#runner-transports---runner)
- [Fedora dist-git lane](#fedora-dist-git-lane-packaging-pr-ci)
- [Test the packages Fedora ships](#test-the-packages-fedora-ships-the-default)
- [Build the IPA RPMs](#build-the-ipa-rpms-ourselves)
- [Blend a preset](#blend-a-preset-or-queue-with-a-provider-setup-blend)
- [Preset validation](#preset-validation-check)
- [Notes](#notes)

## Repository split

CI code and the branch under test have separate versions. The only CI file in a FreeIPA checkout is the pin: the plan `ci/tmt/plans/freeipa-env.fmf`.

The pin does three things:

- It finds the test in this repository (`tmt/tests/freeipa-env/`). It uses a git URL and a pinned ref.
- Its `prepare` step installs the BuildRequires of the branch's `freeipa.spec.in`. Build dependencies belong to the code that is built.

To use newer CI tools, change the `ref:` line in the plan. You do not change this repository when a branch changes its build dependencies.

To run the test from a FreeIPA checkout, use this command. The plan's `discover.url` must be a git URL that tmt can reach. A local path works for local runs.

```
tmt run plan --name /ci/tmt/plans/freeipa-env -e FREEIPA_SRC=$PWD
```

`FREEIPA_SRC` gives the path of the checkout. The test builds the SRPM from it on the guest.

Testing Farm sets `FREEIPA_REPO_URL` and `FREEIPA_REPO_REF` instead. The guest clones the branch under test. Testing Farm copies the fmf tree as plain files. The copy has no `.git` directory and no submodules.

To submit a Testing Farm batch from here, use this command. The repository under test defaults to the origin and branch of that checkout.

```
freeipa-env queue run QUEUE --runner testing-farm --freeipa-src ~/freeipa
```

## Commands

```
freeipa-env up   ENV.YAML    Create or attach the environment.
freeipa-env run  ENV.YAML    Run the test workflow: install, tests, uninstall.
freeipa-env down ENV.YAML    Collect journal and logs, then remove the environment.
freeipa-env show ENV.YAML    Show the state, image labels, and installed IPA, kuipa, ahdapa, and akamu builds.
freeipa-env logs ENV.YAML    Sort and retrieve the collected logs.
freeipa-env logs ENV.YAML --test T --file NAME   Show the last lines of a collected file, for one test and host.
freeipa-env run  ENV.YAML --tests X   Run these tests instead of the run.tests in the env file.
freeipa-env run  ENV.YAML --detach    Start the tests and return. The run continues after this command ends.
freeipa-env status ENV.YAML           Show the state and output of a detached run.
freeipa-env wait ENV.YAML             Wait for a detached run, get the report, and print a summary.
freeipa-env summary ENV.YAML          Show the result of each test in the last run, with the cause of each failure.
freeipa-env overlay ENV.YAML SRC:/DST Copy files into every container. Each copy is checked with a hash.
freeipa-env reset ENV.YAML            Create fresh containers from the same spec. Keep the old logs.
freeipa-env list                      List the environments on this host.
freeipa-env prune [--yes] [--workdirs] Remove the environments that have nothing running.
freeipa-env report [ENV.YAML]         Make a self-contained results.html from the artifacts.
freeipa-env resolve ENV.YAML          Find the image references in the local image store. Read only.
freeipa-env ensure ENV.YAML           Build the missing channel images for the env. Use Fedora packages, or build.srpm.
freeipa-env migrate PRCI.YAML         Make presets from a PRCI definition.
freeipa-env queue run QUEUE.YAML --runner USER@HOST [...]   Run a queue.
freeipa-env queue generate PRCI.YAML -o QUEUE.YAML          Make a queue in PRCI order.
freeipa-env blend BASE PROVIDER --name NAME [--tests T ...] Make a preset or queue from a base and a provider.
freeipa-env check [PRESETS ...]       Check presets. No containers are started.
```

### Common options

- `--workdir DIR`: the state and log directory. The default is `$XDG_STATE_HOME/freeipa-env/<env-name>`, which is `~/.local/state/freeipa-env/<env-name>`. It is never the current directory.
- `--tool podman`: use podman.
- `--seccomp FILE`: use a seccomp file. Podman only.
- `--strict`: with the external provider, fail when a port cannot be reached.
- `--ssh-key FILE`: with the external provider, use this root SSH key.

### Long runs and repeated runs

A backup or restore test class can take 30 minutes or more. A foreground `run` depends on the calling process. If a wrapper timeout ends the command, or a laptop sleeps, the command stops. The tests can keep running in the master.

Use `run --detach` for long runs:

- `status` shows the state and the last lines of output.
- `wait` gets `nosetests.xml` and prints a summary for each test. It also gives the cause of each failure. Use `--timeout` to set a limit.
- `run --tests X` chooses tests without an edit to the env file.

Several classes in one `run` share the containers. If an uninstall leaves state behind, the next class gets that state. That is a real result, not noise. For an independent result, use `reset`. It creates fresh containers and keeps the old logs as `logs.<timestamp>`.

`up` chooses a free podman subnet when the env file names none. If a named subnet is in use, `up` fails and gives the name of the network that uses it.

`up` warns when the `dns_forwarder` does not answer from the master. An install with DNS fails its forwarder check when the resolver is down.

`overlay SRC:/DST` copies files into every container. It checks each file with SHA-256. It writes the copies to `<workdir>/overlay.json`. After that, `show` and the run summary say that the run is not a packaged run. Sources must be final files, not `*.in` templates.

`list` shows every environment on the host. It reads the podman networks, so it also shows orphans whose workdir is gone. `prune` removes the stopped environments. It is a dry run unless you add `--yes`.

## Env file

```yaml
name: base-xmlrpc
provider: podman            # or: external (pre-created), nested (get a VM through an API)
domain: ipa.test
dist: 44
image: freeipa-current      # build channel. The provider finds the image. Per-host image: overrides it.
hosts:
  - {role: master, name: master1}
  - {role: client, name: client1}
resources:                  # defaults match the Azure variables-fedora.yml file
  master: {memory: 1800m, memory-swap: 2500m}
  client: {memory: 512m, memory-swap: 1024m}
run:
  mode: base                # or: integration
  setup_dns: true
  setup_kra: true
  forwarder: 8.8.8.8        # set it here, or use --auto-forwarders
  tests: [test_xmlrpc]
  ignore: [test_xmlrpc/test_dns_plugin.py]
```

### External systems

External systems already exist. To use one, set `provider: external`. You can also give a host an `address:`.

Each host needs `name`, `role`, and `address`. It can also have `user`, `port`, `image`, and `os`.

- `up` checks reachability, SSH, the hostname, the distribution, and the IPA state. It does not change the system.
- `run` works the same as before.
- `down` collects logs. It does not delete anything.

Use `--ssh-key` to set the key. The default is `~/.ssh/id_rsa`.

## Nested provider: get a VM, run the env in it

Use `provider: nested` when the test VMs exist only through an API. A nested provider has two parts:

- An **outer VM backend** gets a VM.
- An **inner provider** runs the env in the VM. The default inner provider is `podman`.

The inner environment uses the same spec. Its `provider` is `inner`, and the `vm:` block is removed. The `hosts:` become containers in the VM. The full podman flow then runs in the VM.

The backend gets a runner when it is needed. It releases the runner at `down`. This is the supervisor's pre-allocated runner, generalized.

```yaml
name: nested-example
provider: nested
image: freeipa-current
inner: podman              # inner provider. The default is podman.
vm:
  backend: command         # ssh | command | openstack | qemu
  count: 1                 # number of VMs. The podman inner provider uses the first one.
  provision_cmd: examples/nested/get-vm.sh      # $1 is the count. Print user@host[:port], one per line.
  deprovision_cmd: examples/nested/drop-vm.sh   # $1 is the VM id or target.
hosts:
  - {role: master, name: master1}
  - {role: replica, name: replica1}
run: {mode: integration, tests: [test_integration/test_simple_replication.py]}
```

### VM backends

The backends are in `freeipa_env/vmbackend.py`.

- **`ssh`**: a pool of VMs that already exist. Each entry is `user@host[:port]` in `vm.hosts`. `terminate` does nothing. Use this as a simple reference.
- **`command`**: runs your scripts. `vm.provision_cmd <count>` prints the VM handles. Print one `user@host[:port]` per line, or a JSON list. `vm.deprovision_cmd <handle>` removes a VM. This is the general integration point for a cloud API. Wrap any cloud in two scripts. See `examples/nested/`.
- **`openstack`**: an example that uses the `openstack` CLI. It runs `server create`, `show`, and `delete`, and it uses floating IPs. Use it as a template for other clouds.
- **`qemu`**: local VMs under qemu and KVM on the control node. There is no cloud API.

#### The qemu backend

- Each VM is a qcow2 overlay on `vm.image`. Use a cloud base image, such as Fedora Cloud Base.
- cloud-init adds the control node's SSH key for root. SSH uses a forwarded localhost port.
- Each VM has a private NIC on `192.168.100.0/24`. `vm1` gets `.10`, `vm2` gets `.11`, and so on. The VMs have each other in `/etc/hosts`, so they can reach each other by name.
- `vm.vms` sets `name`, `image`, `cpus`, `memory`, and `disk` for each VM. `vm.count` must equal the length of `vm.vms`. With `inner: external`, `memory` comes from each host's `resources` entry instead, and a `memory` key in the vm block is an error.
- `terminate` stops the qemu process and removes the overlay. The `console.log` file stays in `vm.state_dir`.
- You need `/dev/kvm` and the host packages below.

#### GPU: virgl

Set `vm.gpu: virgl` to give a VM virtio-gpu with OpenGL. The control node's GPU renders the output.

- `vm.render_node` sets the render node. The default is `/dev/dri/renderD128`. The user must be able to read and write it.
- You need the packages `qemu-device-display-virtio-gpu-gl` and `qemu-ui-egl-headless`.
- To check the guest, run `EGL_PLATFORM=surfaceless eglinfo`. The output must show the `virtio_gpu` driver and a `virgl` renderer.

Each VM with virgl also sends its screen over VNC and websocket, on the loopback address. `up` prints the port.

To see the screen in a browser, install noVNC (the `novnc` package). Open `vnc.html?host=127.0.0.1&port=<ws>`. To view it from another machine, make a tunnel: `ssh -L <ws>:127.0.0.1:<ws>`.

#### USB passthrough

Set `vm.usb: [VVVV:PPPP, ...]` in `vm.vms[i]` to give host USB devices to that VM. Each entry claims one free host device with that vendor and product ID. One device can go to one VM only.

- The devices attach at boot only. There is no hot-plug.
- While the VM runs, the device is not available on the host.
- The user must be able to read and write `/dev/bus/usb/BBB/DDD` for the device. If not, `up` fails and prints that path. A udev rule is the usual fix.

#### Host packages for the qemu backend (Fedora)

These packages are required:

```
sudo dnf install qemu-system-x86-core qemu-img genisoimage rsync openssh-clients
```

You can use `xorriso` or `mkisofs` instead of `genisoimage`.

For `vm.gpu: virgl`, also install:

```
sudo dnf install qemu-device-display-virtio-gpu-gl qemu-ui-egl-headless virglrenderer
```

To see the VM screen in a browser, also install `novnc`.

The guest GL packages (`mesa-dri-drivers`, `egl-utils`) go inside the VM, not on the host.

The user must be able to read `/dev/kvm`. For virgl, the user must also read and write `render_node`.

#### Guest packages

Set `vm.guest_packages` in the nested spec. Each entry is a set name or a plain package name. `up` installs the packages with `dnf` on every VM, before the inner `up`.

- `gpu`: installs `mesa-dri-drivers` and `egl-utils`. Use it for the virgl `eglinfo` check.
- `ipa-distro`: installs the FreeIPA server, client, and Python packages from the distribution, plus `firewalld`, which the FreeIPA test suite expects. It does not use COPR or a local build. It also installs `pytest-html==4.2.0` with pip.

```yaml
vm:
  guest_packages: [gpu, ipa-distro]
  gpu: virgl
```

- On Fedora, `dnf` installs from the Fedora repositories.
- On CentOS Stream, RHEL, Rocky Linux, and AlmaLinux, the install first enables EPEL and CRB. If CRB is not available, it uses `powertools`.
- Plain names also work. For example: `guest_packages: [tmux]`.
- If a package name is wrong, `up` fails and prints the `dnf` output.
- Only Fedora 45 is tested. The EL clones are not tested.
- There are no guest desktop packages in any set yet.

### Lifecycle

`up` runs these steps:

1. Provision the VMs.
2. Wait for SSH.
3. Copy the ci-freeipa tree and the inner spec to the VM.
4. Run the inner `freeipa-env up` on the VM.
5. Save the VM handles to `<workdir>/nested-state.json`. A later `down` uses this file to find the VMs.

`down` runs these steps:

1. Run the inner `freeipa-env down`. This collects the logs.
2. Copy the remote workdir back. Then `logs` and the xunit output work on the control node.
3. Release the VMs.
4. Clear the state.

`run` starts the inner `freeipa-env run` on the first VM.

`resolve` does not work for nested environments. The VM does not exist yet. The image is found on the VM during `up`. The VM must have the `freeipa-ci` image. Or set `vm.image_ref` so that the VM runs `podman pull`.

### Inner provider: external

With `inner: external`, nothing runs in containers. Each host runs directly on a VM named after the host. For the qemu backend, a `vm.vms` entry with the host's name sets that VM's `cpus` and other options.

These rules apply:

- `vm.count` must equal the number of hosts.

`up` does these steps:

1. Provisions the VMs.
2. Maps each host to the forwarded SSH port (`address` and `port`) and to the private address (`ip`) of its VM.
3. Writes the inner external spec.
4. Attaches with `ExternalProvider` from the control node.

`run` and `show` use the same provider. The ci-freeipa tree is not copied to the VMs. The example is `env/presets/nested-qemu-external.yaml`.

### Framework SSH key

The framework runs on the master and SSHes to every host as root. `up` generates that key on the master at `/root/.ipa/framework_key` and authorizes its public key on each host. It also writes `/etc/hosts` entries for every host on every host.

Set `framework_key` in the env file to choose the key. `type` is passed to `ssh-keygen -t` unchanged, so any type the master's OpenSSH supports works (`ed25519`, `rsa`, `ecdsa`, `ecdsa-sk`, ...):

```yaml
framework_key: {type: rsa, bits: 3072}   # optional bits: passed as ssh-keygen -b
```

- Without `framework_key`, the default is ed25519. With `fips: true`, the default is rsa.
- The key is created only if `/root/.ipa/framework_key` is absent. Changing `type` on an existing master does not regenerate it. Remove the file on the master first.
- A type the host does not support fails in `ssh-keygen` on the master, during `up`.


### bootc VMs

For a bootc host, set `vm.bootc: true`. `up` then runs `bootc usroverlay` on each VM. It runs this before `guest_packages`, so `dnf` can install into `/usr`.

The overlay is removed at reboot. After a reboot, run `up` again.

The example is `env/presets/nested-qemu-bootc.yaml`. It uses `guest_packages: [ipa-distro]` and one `master1` VM. FreeIPA runs directly on the VM, without containers.

### Base disk for bootc

ci-freeipa does not build the base disk. The disk was built with these steps. The steps run in a guest VM, because the conversion needs root.

```sh
podman pull quay.io/fedora/fedora-bootc:44
truncate -s 20G /root/output/disk.raw
podman run --rm --privileged --pid=host -v /dev:/dev \
  -v /var/lib/containers:/var/lib/containers -v /root/output:/output \
  --security-opt label=type:unconfined_t quay.io/fedora/fedora-bootc:44 \
  bootc install to-disk --via-loopback --generic-image --filesystem ext4 \
  --root-ssh-authorized-keys /output/authorized_keys /output/disk.raw
qemu-img convert -O qcow2 /root/output/disk.raw /root/output/disk.qcow2
```

Copy the qcow2 file to `~/.cache/freeipa-ci/bootc/fedora-bootc-44.qcow2`.

Test status:

- Tested: the bootc VM starts. `usroverlay` and `dnf install` work. The `ipa-distro` packages install. The freeipa-server version is 4.13.4.
- Not tested: `ipa-server-install` on the bootc VM. FreeIPA does not support the bootc server side yet. This is the failure to investigate.

### Where nested runs happen

The nested provider runs on the **control node**. The control node is a CI machine. It has the cloud API credentials and SSH access.

A queue of `provider: nested` presets runs under the `freeipa-env queue` supervisor. It runs on a control-node `--runner`. Each job provisions its own VMs.

## Artifacts

`run` writes these files:

- `<workdir>/nosetests.xml`: JUnit XML from `ipa-run-tests --with-xunit`.
- `<workdir>/logs/`: the per-host journal, the `/var/log/ipa*` tarballs, and the install and test run logs.
- `IPA_TESTS_LOGSDIR` (inside the container): per-test logs from the framework (`--logfile-dir`). `down` and `collect_logs` also copy this tree to `<workdir>/logs/collected/<master>/ipa-env/logs`. Then `logs` can show it. See the `per-test` category below.

## Log analysis (`logs`)

`freeipa-env logs` turns the collected files into a triage view.

- With no `--category`, it prints a summary for each category. The summary shows the number of sources and the number of interesting lines.
- With `--category NAME`, it prints the matching lines and the tail of each source file. Use `--category all` for every category. You can repeat the option.

```
freeipa-env logs ENV.YAML                        # category summary
freeipa-env logs ENV.YAML --category tests       # failed tests, grouped by module
freeipa-env logs ENV.YAML --category ds --category httpd --category kra  # several at once
freeipa-env logs ENV.YAML --category tests --json  # summary in JSON
freeipa-env logs --workdir DIR --category system --pattern 'OOM|segfault'
```

Each category has a default line filter. You can replace it with `--pattern REX`.

| Category | Source |
|---|---|
| `tests` | `nosetests.xml` (JUnit). Totals, failed and errored tests with messages and traceback tails, grouped by module. |
| `per-test` | The framework's per-test log tree. One directory for each test ID. It has the install and uninstall logs and the journal for each host. The xunit status is added. |
| `run` | `logs/run.log`. The console output of install, test, and uninstall. FAILED, ERROR, and summary lines. |
| `install` | `ipaserver-install.log`. ERROR and WARNING lines. |
| `uninstall` | `ipaserver-uninstall.log`. ERROR and WARNING lines. |
| `ds` | The 389-ds `errors` log. `- ERR -` and `- CRIT -` lines. |
| `httpd` | `error_log`. IPA framework errors and Python tracebacks. Debug messages are removed. |
| `kra`, `ca` | The pki instance logs (`pki-tomcat/{kra,ca}`) and the spawn logs. |
| `kdc` | `krb5kdc.log`. error, crit, and fatal lines. |
| `samba` | `/var/log/samba`. AD trust. |
| `dns` | The BIND runtime logs (`named.run`). |
| `ipa` | The per-operation logs in `/var/log/ipa/`. ERROR and CRITICAL lines. |
| `system` | The boot journal. Failed units, OOM events, segfaults, and watchdogs. |

Useful options:

- `--pattern REX`: replace the line filter of a category.
- `--lines N`: the number of matched lines for each file. The default is 30.
- `--tail N`: the number of tail lines. Use 0 to turn it off.
- `--all`: print each whole file, with no filter.
- `--host NAME`: show one host only.
- `--json`: print the summary for scripts.
- `--refresh`: collect the logs again from an environment that still runs, before you analyze them. If the collection fails, the existing files are kept.

`logs` extracts the daemon tarballs into `<workdir>/logs/extracted/<host>/`. It extracts only the needed files. It checks the mtime and size of each file. Repeated calls are fast.

### Self-contained HTML report (`report`)

`freeipa-env report` makes one file, `results.html`. The default location is `<workdir>/results.html`. Share this file or paste it into a ticket.

The report uses the same code as `logs`. It works offline. It needs no environment file for a workdir that is already collected. It has no JavaScript and no external files. All CSS is inside the file. Sections collapse with the HTML `<details>` element.

Each failed, errored, or skipped test row has its failure message and traceback tail.

```
freeipa-env report --workdir DIR                 # writes <workdir>/results.html
freeipa-env report --workdir DIR -o /tmp/r.html  # choose the output file
freeipa-env logs ENV.YAML --html                 # the same report, from logs
freeipa-env tf-logs <request-id> --html          # download a TF request, then make the report
```

The report is less detailed than the `report.html` from PRCI. PRCI uses pytest-html inside the image. This report has these parts:

- Per-test status and duration from the xunit file.
- The shared log categories.
- The per-test log trees. Each test has its own collapsed block, with its xunit status.

It does not show the captured stdout of each test. The image in this repository does not run pytest-html.

If the workdir has a `results.yaml` for each stage, the report adds a `Stages` table. The table shows the result, duration, and note of each stage. The Testing Farm transport uploads this file.

## Presets

- `presets/base-xmlrpc.yaml`: the Azure **xmlrpc** job. One master, DNS and KRA, and `test_xmlrpc` without `test_dns_plugin.py`.
- `presets/external-smoke.yaml`: a template for external provider smoke tests.
- `presets/azure/`: one preset for each Azure CI job. These presets recreate `ipatests/azure/azure_definitions/{base,gating}-fedora.yml`.

| Preset (`presets/azure/`) | Azure job | Hosts | Suite |
|---|---|---|---|
| `base.yaml` | base | master | Unit tests and `test_xmlrpc/test_dns_plugin.py` (`-k 'not test_dns_soa'`) |
| `kerberos-flags.yaml` | kerberos_flags | master, client | `test_kerberos_flags.py` |
| `forced-client-reenrollment.yaml` | forced_client_reenrollment | master, replica, client | `test_forced_client_reenrollment.py` |
| `external-ca-install.yaml` | external_ca_ExternalCAInstall | master | `test_external_ca.py::TestExternalCAInstall` |
| `simple-replication.yaml` | simple_replication | master, replica | `test_simple_replication.py` |
| `service-permissions.yaml` | service_permissions | master | `test_service_permissions.py` |
| `sudo.yaml` | sudo | master, client | `test_sudo.py`. Deselects `TestSudo_Functional`. |
| `external-ca.yaml` | external_ca_ExternalCA | master, replica, client | `test_external_ca.py::TestExternalCA` |
| `topologies-testconfig.yaml` | topologies_and_testconfig | master | `test_topologies.py`, `test_testconfig.py`. No installs. |
| `external-ca-constraints.yaml` | external_ca_ExternalCAConstraints | master, client | `test_external_ca.py::TestExternalCAConstraints` |
| `membermanager.yaml` | membermanager | master | `test_membermanager.py` |
| `netgroup.yaml` | netgroup | master | `test_netgroup.py` |
| `external-ca-profile-scenarios.yaml` | external_ca_ExternalCAProfileScenarios | master | `test_external_ca.py::TestExternalCAProfileScenarios` |
| `caless-to-ca-full.yaml` | ServerReplicaCALessToCAFull | master, replica | `test_caless.py::TestServerReplicaCALessToCAFull` |
| `external-ca-self-external-self.yaml` | external_ca_SelfExternalSelf | master | `test_external_ca.py::TestSelfExternalSelf` |

Run modes:

- Base pipeline jobs use `mode: base`. The steps are install, test, and uninstall. The script is `run-base-tests.sh`.
- Gating jobs use `mode: integration`. The framework does the install and uninstall for each class. It reads the multihost config.

Each host gets the same memory limits as in the Azure definitions.

## Migrated PRCI definitions (`migrate`)

`freeipa-env migrate ipatests/prci_definitions/<def>.yaml` converts a PRCI definition. It writes one preset for each job under `presets/prci/<def>/`. It also writes a `MIGRATED.md` report.

Run `migrate` again after you change a PRCI definition. Options:

- `-o DIR`: the output directory.
- `--jobs NAME`: keep only the jobs whose name contains NAME.

`presets/prci/` has all 11 PRCI definitions. It has 1301 presets in total.

| Directory | PRCI file | Presets |
|---|---|---|
| `gating/` | `gating.yaml` | 31 |
| `nightly-latest/` | `nightly_latest.yaml` | 185 |
| `nightly-latest-389ds/` | `nightly_latest_389ds.yaml` | 48 |
| `nightly-latest-pki/` | `nightly_latest_pki.yaml` | 91 |
| `nightly-latest-selinux/` | `nightly_latest_selinux.yaml` | 185 |
| `nightly-latest-sssd/` | `nightly_latest_sssd.yaml` | 20 |
| `nightly-latest-testing/` | `nightly_latest_testing.yaml` | 185 |
| `nightly-latest-testing-selinux/` | `nightly_latest_testing_selinux.yaml` | 185 |
| `nightly-previous/` | `nightly_previous.yaml` | 185 |
| `nightly-rawhide/` | `nightly_rawhide.yaml` | 185 |
| `temp-commit/` | `temp_commit.yaml` | 1 |

### Mapping rules

- **Topology to hosts.** `ipaserver` gives one master, in base mode. `master[_Nrepl]_Mclient` gives master, replica, and client hosts. These use integration mode when there is more than one IPA host. The AD tokens `ad` and `adroot_adchild_adtree` add external AD hosts. Their roles are `ad`, `ad_subdomain`, and `ad_treedomain`.
- **Topology memory to limits.** PRCI gives a total for each topology. `migrate` divides it evenly between the roles. It rounds to 100 MiB. The minimum is 512 MiB.
- **test_suite to `run.tests`.** The space-separated list is kept.
- **class Build.** Skipped. The `freeipa-ci` image pipeline replaces it.
- **ipa_ipa_trust topologies.** Skipped. These need two IPA domains. The preset model supports one IPA domain and AD domains.
- **RunWebuiTests.** Uses `mode: base`. A note is added. PRCI installs a browser and selenium. The `freeipa-ci/full` image does not have them. These presets fail as they are.

### AD hosts are external

PRCI creates its own AD domain controllers. This repository does not. Each AD host is an `address` placeholder from RFC 5737, with a placeholder FQDN. Edit both before you run `freeipa-env up`. The footer of each preset lists every line marked EDIT ME.

`up` writes the AD FQDNs into `/etc/hosts` on the IPA containers. The tests set up the trust. They use `ad_admin_name` and `ad_admin_password`. The defaults are `Administrator` and `Secret123`.

A Samba AD DC works if SSH can reach it for log collection. Windows-only DCs need the `user` value changed.

## Run queues and the supervisor (`queue`)

A queue runs an ordered list of presets. It uses a pool of **pre-allocated runners**. A runner is a host with SSH, podman, and systemd. The lab VMs are runners. Queues replace the per-PR job scheduling of PRCI.

Queue files are in `queues/`:

- `azure.yaml`: the 16 Azure CI jobs. Azure ran them in parallel. The order in the queue is the usual order.
- One file for each PRCI definition. The names match the preset directories, for example `gating.yaml` and `nightly-latest.yaml`. Each file keeps the job order of its PRCI definition. Make them with `freeipa-env queue generate`. Run it again after a PRCI definition changes.

### Queue format

A queue file has a `jobs:` list. Each entry is one of these:

- A preset path, relative to `env/`.
- A directory path that ends with `/`. It expands to all presets in that directory, sorted.
- A dict with `preset` or `dir`, and an optional `note`.

### Image references are channels

A preset does not name a build. It names a **channel**:

- `image: freeipa-current`: the newest build of the current release.
- `image: freeipa-next`: the next release. PRCI calls it `fedora-rawhide`.
- `image: freeipa-previous`: the previous release.

PRCI uses one build generation for each definition file. Every job in the file uses that build. The channels do the same thing here.

`images/build.sh --channel NAME` adds the channel tag, `freeipa-ci/full:<channel>`. It also adds the fixed provenance tag, `freeipa-ci/full:<dist>-<sha>`.

The provider finds the image for a channel on each host. This is a provider task.

- The podman provider maps the channel to a podman tag. It checks the tag in the local image store during `up`.
- `freeipa-env resolve ENV.YAML` does only this step. It prints `<ref> <concrete> <image_id>`.
- The external provider ignores the channel. External hosts run their own IPA.

A preset can also name an explicit image reference. The reference is used as it is. Use this to pin a build.

### Run a queue

```
freeipa-env queue run queues/gating.yaml \
    --runner root@192.168.122.215 --runner root@192.168.122.216 \
    [--jobs NAME ...] [--limit N] [--dry-run] [--keep-on-failure] \
    [--job-timeout SECS] [--outdir DIR] [--no-bootstrap] [--ssh-key FILE]
```

For each run, the supervisor does these steps:

1. Checks every preset. It parses each file and checks that it exists.
2. Checks each runner for SSH and podman. Copies the local ci-freeipa tree to `--remote-ci`. The default is `/root/freeipa-ci/ci`.
3. Asks each runner to find the images for each preset. This runs `freeipa-env resolve` on the runner. If an image is missing, the run stops before any job starts. The results are saved per runner in `summary.md`.
4. Schedules the jobs. Each runner takes the next job from the front of the list. It runs one job at a time. Each job runs `up`, then `run`, then `down`. Each job has a remote timeout. The default is 4 hours. Each job uses the workdir `/root/jobs/<job-key>`.
5. Writes `summary.tsv`, `summary.md`, and a transcript for each job, in `--outdir`. The default is `./<queue>.queue`. The exit code is 0 only when every job passes.

### Job order

- With one runner, the jobs run in the queue order.
- With N runners, each runner gets its own part of the queue. Each part keeps the queue order. The jobs leave the queue first in, first out.

`--keep-on-failure` skips `down` for a failed job. The environment stays on the runner. Use `freeipa-env logs --refresh` to look at it. Run `down` yourself before you reuse the runner.

## Runner transports (`--runner`)

A runner is a host that runs queued jobs. It needs podman, systemd, and a copy of the ci-freeipa tree. Each `--runner SPEC` names one transport.

| Spec | Transport | How the jobs run |
|---|---|---|
| `user@host[:port]` (default) | **SSH** | A runner that you set up. The ci-freeipa tree is copied to it. `up`, `run`, and `down` run on the runner. |
| `local` | **Local** | The control node is the runner. No SSH is used. The same steps run as local processes. You need podman on the control node. |
| `testing-farm` | **TF** | No SSH and no host you set up. The jobs of one runner run as **one Testing Farm request**. The guest is set up once. It builds the SRPM and the channel images once. Then it runs the presets in order. |

All three transports use the same job steps: `up`, `run`, and `down`. Each job has a timeout. `down` has no timeout and must finish. The output files (`summary.tsv`, `summary.md`, and the transcripts) have the same form for each transport.

### Paths on runners

The remote and local job paths start with `~/`. The remote shell expands them to the home directory of the SSH user. A non-root user keeps the tree out of `/`. The defaults `--remote-ci ~/freeipa-ci/ci` and `--jobs-dir ~/freeipa-jobs` work for any user. The local runner expands `~/` to the home directory of the local user.

### Testing Farm transport

Use `--runner testing-farm` to send the jobs to the public [Testing Farm](https://docs.testing-farm.io/) API. Each runner's jobs are one request.

Testing Farm does these things:

- It creates a new Fedora guest. You do not need a host or root access on your machines.
- It clones the **FreeIPA repository under test**.
- It runs the pinned plan from that checkout. The plan finds this repository's test at the pinned ref.

The guest builds the SRPM and each channel image once. Then it runs the presets in order, and it reuses the images.

A preset passes only when these two things are true:

- The request completes.
- The preset's own `results.yaml` shows `passed`.

The request fails when the run step of any preset fails.

You need an API token. Pass `--tf-token TOKEN`, or set the `TESTING_FARM_API_TOKEN` environment variable.

Other options:

- `--tf-repo-url`: the repository that the guest clones. The default is the `remote.origin.url` of `--freeipa-src`.
- `--tf-ref`: the ref to check out. The default is the current branch of `--freeipa-src`.
- `--tf-url`: the API base URL.
- `--tf-arch`: the architecture. The default is `x86_64`.
- `--tf-compose`: the compose. The default is `Fedora-44`.
- `--tf-plan`: the plan in the FreeIPA repository. The default is `/ci/tmt/plans/freeipa-env`.
- `--tf-variable KEY=VALUE`: an extra request variable. You can repeat this option.

Testing Farm clones over the network. Use a commit that is **pushed to a public repository**.

#### The guest builds the SRPM

You do not supply an SRPM. The tmt plan `prepare` step installs these packages on the guest:

- `git`, `podman`, and the autotools toolchain.
- Every BuildRequires of the branch's `freeipa.spec.in`.

This follows the [synta](https://pagure.io/synta) pattern.

Testing Farm copies the fmf tree as plain files. So the tmt test clones the repository first. It runs `git clone --recursive` and then `git checkout`, using `--tf-repo-url` and `--tf-ref`. Then it builds the SRPM:

1. `autoreconf -i`
2. `./configure` with the flags that match the spec.
3. `make srpms`

For each preset, the guest bakes the channel image from the SRPM with `images/build.sh --srpm`. It bakes each channel once. A channel that is already baked is reused.

Then the guest runs each preset in order. Each preset runs `up`, `run`, and `down` with its own `--job-timeout`. The request fails if any run step fails.

To use a prebuilt SRPM, pass `--srpm <URL>`. The URL must be HTTP or HTTPS, and the guest must be able to fetch it.

#### Results and artifacts

The tmt test uses `result: custom`. Each preset reports one result for each stage:

- `srpm-build`
- `image-build`
- `env-up`
- `test-run`
- `env-down`

It also reports a parent result for the preset. Each result has its own console log. The batch has a parent result. It is `fail` when the run step of any preset fails.

The artifacts of each preset go to `.../data/artifacts/<preset-key>/`. These are the run console, `nosetests.xml`, and the collected per-host logs. The `results.yaml` file goes to `.../data/<preset-key>/`. You can download and check each preset separately.

#### Analyze a TF request with `logs`

`freeipa-env tf-logs <request-id>` downloads the artifacts of a finished request. It puts them in a local workdir. The layout is the same as for a local or SSH job. Then it runs `logs`. You do not need an environment file.

A batched request has one workdir for each preset. The path is `<workdir>/<preset-key>/`. Each one has its own `logs/`, `stages/`, and `results.yaml`. `--html` writes one `results.html` for each preset. A request with one preset has one flat workdir.

The request ID is printed in the `queue run` transcript. You can also get it from the TF API. `--category`, `--pattern`, `--json`, and `--host` work as they do for `logs`. `--workdir DIR` sets where the artifacts go. The default is `./tf-<request-id>.env`. If you run the command again on the same directory, it reuses the files that are already downloaded.

```
freeipa-env tf-logs <request-id>
freeipa-env tf-logs <request-id> --category tests --category run --lines 20
freeipa-env tf-logs <request-id> --html          # download, then make results.html
```

`tf-logs` also downloads the `results.yaml` of each stage. The `report` then shows the `Stages` table.

You do not need a token to download a finished request. The request state (`GET /v0.1/requests/<id>`) and the artifact store are public. You need a token only to submit a request with `queue run --runner testing-farm`.

#### Example

```
freeipa-env queue run queues/gating.yaml \
    --runner testing-farm \
    --tf-token $TESTING_FARM_API_TOKEN \
    --tf-repo-url https://github.com/abbra/freeipa.git \
    --tf-ref modrnize-ci \
    --job-timeout 21600        # one build and several runs. The default is 4 hours for each preset.
```

The supervisor checks the request until it ends. The batch deadline is `--job-timeout`. The default is 14400 seconds (4 hours) for each preset, plus a buffer for the build. If the deadline passes, the supervisor cancels the request.

The full build (SRPM, binary RPMs, and the image) runs once. Every preset in the request uses the result.

`--dry-run` with the `testing-farm` runner prints the exact batch request JSON. It does not send it. You still need a token.

#### Released packages, no FreeIPA checkout (the default)

If you do not pass `--freeipa-src`, `--tf-repo-url`, `--srpm`, or `--copr`, the tests use the packages that Fedora ships. Nothing is built. This repository is not a FreeIPA repository, but the request names it. The request uses its plan `/tmt/plans/distro` (`tmt/plans/distro.fmf`).

- `prepare` installs only `podman` and `python3-pyyaml`.
- The test comes from the same ref, so you do not change a pin.
- The request uses the origin and current branch of this checkout. You must push the branch first.

The guest builds the channel images for its own release. The compose chooses the Fedora version:

```
freeipa-env queue run queues/azure.yaml \
    --runner testing-farm --tf-compose Fedora-45
```

To run that plan from another clone of this repository, use `--tf-repo-url` and `--tf-ref`. Also pass `--ipa-from-distro`. If you pass only `--tf-repo-url`, the tool expects a FreeIPA repository to build.

#### Packages from a COPR repository

Add `--copr OWNER/PROJECT` to the same command. You can repeat this option. The same plan runs. The guest installs the IPA packages from the COPR and from the distribution together. The COPR must have builds for that Fedora version.

## Fedora dist-git lane (packaging PR CI)

Fedora dist-git CI (Packit) runs a **Koji scratch build** for each dist-git PR. It runs the scratch build before the tests. It passes the task ID to the test request as `KOJI_TASK_ID`.

The test uses that build. The file `tmt/tests/freeipa-env/koji-rpms.py` reads the `buildArch` subtask results of the task. It downloads the binary RPMs from kojipkgs. It downloads x86_64 and noarch RPMs. It leaves out debuginfo and debugsource.

The channel image is made from these RPMs with `build.sh --rpms`. No SRPM is needed. No build image is needed. No rpmbuild runs.

The scratch build targets the PR's Fedora dist. So this lane makes the base and channel images for the dist of the guest. It uses `images/build.sh --dist`. `base/Dockerfile` uses `ARG DIST`. A rawhide guest uses the rolling `rawhide` toolbox tag.

### Fallback: no scratch build

Use this fallback for manual runs, or for a CI system without Packit. Set `FREEIPA_DISTGIT=1`. The test then builds the SRPM from the dist-git checkout in the plan. A dist-git repository has the spec, the patches, and a `sources` file with the tarball hashes. It does not have `configure.ac`.

`tmt/tests/freeipa-env/distgit-srpm.sh` does these steps:

1. Reads the plan tree at `$TMT_TREE`. The CI copies this tree to the guest.
2. Downloads each entry of `sources` from the Fedora lookaside.
3. Runs `rpmbuild -bs`.

Then the channel image is made from the SRPM with `--builddep`. The build image adds any BuildRequires that the PR's spec adds. The image is for the guest's dist.

Override settings:

- `FREEIPA_DISTGIT_DIR`: the tree path. The default is `$TMT_TREE`.
- `FREEIPA_DISTGIT_SPEC`: the spec file name.
- `FREEIPA_DISTGIT_PKG`: the package name in the lookaside. The default is the `Name:` of the spec.
- `FREEIPA_LOOKASIDE_URL`: the base URL of the lookaside.

### Dist-git plan

The dist-git plan uses the same pin as the upstream plan. Its `prepare` step is smaller. The binary build runs in the build image, so the guest needs no BuildRequires.

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
  # fallback source when the request has no KOJI_TASK_ID
  FREEIPA_DISTGIT: "1"
  FREEIPA_JOBS: '[{"key": "base-unit", "preset_rel": "presets/azure/base.yaml"},
                  {"key": "base-xmlrpc", "preset_rel": "presets/base-xmlrpc.yaml"}]'
```

`FREEIPA_JOBS` lists the suites that run. The presets come from this repository.

The guest chooses the source in this order:

1. `FREEIPA_SRPM_URL`
2. `KOJI_TASK_ID` (reuses the scratch build)
3. `FREEIPA_DISTGIT`
4. The upstream clone and SRPM build

## Test the packages Fedora ships (the default)

If you do not name a source, the image uses the packages from the distribution. A source is named by `--srpm` or `build.srpm`, by `--rpms`, or by `--copr`. `--copr` alone installs from that COPR. See the end of this section.

The image is made from the distribution's own packages. This tests a Fedora release as it was released.

`images/build.sh` runs `dnf -y install` for each IPA subpackage. It installs these packages:

- The server, with DNS and trust-ad.
- The client.
- The `python3-*` packages.
- `python3-ipatests`.

The packages come from the repositories of `--dist`. These are the repositories that `fedora-toolbox` enables: `fedora` and `updates`. `updates-testing` is not used.

The tests that run are the tests that `python3-ipatests` of that release installs. So the tests always match the packages.

The image has the label `source: distro`. `freeipa-env show` prints this label with the installed builds.

`--ipa-from-distro` states this choice. It cannot be used with `--copr` or `--srpm`. A COPR gives dnf its own builds. The result would then not be the distribution's.

The image is a snapshot of the repositories at bake time. To get newer updates, bake the image again.

- `--add-ipa-package SPEC` adds a package to the default list.
- `--ipa-packages 'SPECS'` replaces the list.

### Ways to use it

- **One machine, a dedicated image.** Use this on a host that also has a worktree or a COPR build. A channel image that is already present is never built again. So `up` would test that image. Use this command:

  ```
  images/build.sh --dist 45 --tag freeipa-distro
  ```

  In the env file, set `image: freeipa-distro/full:45` and `dist: 45`. To run a catalog preset with this image, copy the preset. Change those two lines.

- **One machine, the channel image.** `freeipa-env up ENV.YAML` or `ensure` makes the missing channel image for the `dist:` of the env. `ensure --build` makes a new image. It **replaces** the build that the channel tag points to.

- **Queue.** `freeipa-env queue run QUEUE --runner local` (or `USER@HOST`) makes the missing channel images on each runner. This happens before any job starts. Host runners make images for dist 44. For another release, make the channel first with `images/build.sh --dist N`.

- **Testing Farm.** Use `--runner testing-farm --tf-compose Fedora-N`. See [Testing Farm transport](#testing-farm-transport).

To use builds from a COPR repository, add `--copr OWNER/PROJECT` to any command above. You can repeat it. `--ipa-from-copr` states this choice. dnf then finds the IPA packages in the COPR and in the distribution.

## Build the IPA RPMs ourselves

By default, `freeipa-env` uses IPA RPMs. A build farm makes them. The RPMs go into a `freeipa-ci/full` image, and you distribute that image.

A problem can happen with these RPMs. Their distro dependencies can change after the snapshot. For example, a newer samba enters the repository. The old IPA RPM requires the old samba, which no longer resolves. The image build then fails. The cause is not the IPA change under test.

The self-build lane prevents this problem. It compiles the IPA binary RPMs from the **same git snapshot**. It does this in a build image on the same host that makes the test image. The build and the test use one snapshot of the distro repository (design §3.10).

### Steps

1. **Control node.** `scripts/make-srpms.sh` runs autogen and configure on the checkout. Then it runs `make srpms`. The result is `dist/srpms/freeipa-<ver>.src.rpm`. This is the only step that needs the IPA source tree. The script uses the same configure options as `makerpms.sh`. So the SRPM has the same host and layout as a local `make rpms`.
2. **Ship.** The SRPM goes to the host that runs the environment. For one machine, it is already there. For a queue, the supervisor copies it to each runner with rsync.
3. **Build and bake.** `images/build.sh --srpm <path>` compiles the SRPM. It does this in the `freeipa-ci/build:<dist>` image. That image has the base image and every BuildRequires of the spec. It is made once for each dist. The build does not run `dnf builddep` for each build.

   The build runs `rpmbuild --nocheck --rebuild`. `--nocheck` skips the `%check` unit tests of the spec. CI runs those tests in its own `run` step. The build then collects the new binary RPMs. It makes `freeipa-ci/full:<dist>` from them. It tags the image with the channel.

### Three ways to start it

- **One machine.** Add a `build:` block to the env file. `up` builds any missing channel image before it finds the image. `freeipa-env ensure` does only this step.

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

- **Queue.** Pass the SRPM to `queue run`. You can pass a file or the `dist/srpms/` directory. The supervisor copies it to each runner. It builds any channel image that the runner does not have. This happens before any job starts.

  ```
  freeipa-env queue run queues/gating.yaml \
      --runner root@192.168.122.215 \
      --srpm dist/srpms \
      [--srpm-dir /root/freeipa-ci/srpm] [--image-timeout 9000]
  ```

### When images are rebuilt

A channel image that is already on the host is not changed. Only missing channel images are built. A full IPA build and image bake can take a long time.

To force a rebuild, use one of these:

- Set `build.force` in the env file.
- Run `freeipa-env ensure --build`.
- Remove the channel image.

Without an SRPM (`--srpm`, or a `build:` block), a missing channel image is made with dnf. It uses the distribution packages. If you pass `--copr`, it uses the COPR repositories.

## Blend a preset or queue with a provider setup (`blend`)

`freeipa-env blend BASE PROVIDER --name NAME [--tests T ...]` writes a preset or queue that is ready to run. The file is `env/generated/NAME.yaml`. You then run it with `run` or `queue run`. You do not repeat any flags.

- BASE is a preset or a queue (a file with `jobs:`). For a queue, each job is blended. Each one is written to `env/generated/NAME/<job>.yaml`.
- PROVIDER is a setup file. It has keys such as `provider`, `inner`, `vm`, `image`, `dist`, and `domain`. Its top-level keys replace the keys of BASE. `hosts` and `resources` always come from BASE, so the test's topology and memory stay. Its `name` and `run` are ignored.
- `--tests` replaces `run.tests` in each generated preset.

```
freeipa-env blend presets/base-xmlrpc.yaml presets/nested-qemu-external.yaml \
    --name xmlrpc-vm --tests test_xmlrpc
freeipa-env run env/generated/xmlrpc-vm.yaml
```

`blend` checks each generated file before it writes it. A bad merge fails in `blend`, not during the run.

## Preset validation (`check`)

`freeipa-env check [PRESETS ...]` checks preset files. It does not start containers and it does not use the network. Use it as the CI check for the 1300 or more presets.

With no arguments, it checks every `*.yaml` under `env/presets/`, and the top-level presets. To check fewer files, pass files or directories.

Each preset must parse as an `EnvSpec`. It must also pass the checks of its provider:

- **`provider: podman`.** Each containerized host must find an image. A channel name must be a known channel: `freeipa-current`, `freeipa-next`, or `freeipa-previous`. A name that is neither a channel nor an image reference is reported as a likely typo.
- **`provider: nested`.** The `vm:` block must use a known VM backend. `inner` must name a known provider.
- **`provider: external`.** Each host must have an address.

`check` prints one line for each failing preset, then a summary. The exit code is 0 only when every preset is valid and no path is missing.

Add the check to your pull request gate. A preset change then cannot break a queue without notice.

```
freeipa-env check   # run from the repository root. It checks all presets and exits non-zero on any problem.
```

### Example: master on Fedora 44, replica on Fedora 45

This runs the PRCI `customized_ds_config_install` test on two VMs from two Fedora releases. The test asks for `master1` and `replica1`. `nested-qemu-mixed.yaml` puts `master1` on the F44 cloud image (`vm.vms`). Every other host gets the default F45 image. Both VMs get the `ipa-distro` packages from their own release, including `firewalld`.

Download the two images first. Verify them against the `CHECKSUM` file in the same directory:

```
mkdir -p ~/.cache/freeipa-ci/images
# Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2 and ...-45-1.4.x86_64.qcow2
# from https://dl.fedoraproject.org/pub/fedora/linux/releases/{44,45}/Cloud/x86_64/images/
```

```
freeipa-env blend prci/nightly-latest-testing/customized_ds_config_install.yaml \
    presets/nested-qemu-mixed.yaml --name mixed-cds
freeipa-env up  env/generated/mixed-cds.yaml
freeipa-env run env/generated/mixed-cds.yaml
freeipa-env down env/generated/mixed-cds.yaml
```

Check the result: `master1` must report `freeipa-server-*.fc44`, and `replica1` must report `freeipa-server-*.fc45`. The FreeIPA server build must match its release in both VMs. If not, the VM image is wrong.

## Notes

- The podman provider uses one network for each env: `10.89.0.0/24` and `2001:db8:1::/64`. Only one env can run on each host in version 1. If these defaults conflict with a podman network that another project made, set `subnet_v4:` and `subnet_v6:` in the preset.
- In base mode, `run` copies `scripts/run-base-tests.sh` into the master container and runs it there. The JUnit report is copied back afterward.
- Uninstall is part of the workflow: install, test, uninstall. The container is removed only by `down`, after the uninstall finishes.
