#!/bin/bash
# Run a FreeIPA env queue BATCH on a Testing Farm guest (one TF request).
#
# This script is part of the separate ci-freeipa repository; the plan in the
# FreeIPA repo under test discovers the test from this repo at a pinned ref.
# Invoked by the tmt test (tmt/tests/freeipa-env/main.fmf). The plan's
# prepare step (how: install) has already placed podman, curl and every
# BuildRequires of freeipa.spec.in on the guest, so the SRPM can be produced
# here without any pre-supplied artifact.
#
# A request carries a LIST of queue jobs (FREEIPA_JOBS, a JSON array) that all
# run in this one guest. The point: build freeipa's SRPM and the channel
# images ONCE and reuse them across every preset in the batch, instead of the
# old one-request-per-job model that rebuilt them for each job.
#
# Flow:
#   1. parse FREEIPA_JOBS -> the list of (key, preset) jobs;
#   2. obtain the IPA packages (download FREEIPA_SRPM_URL; REUSE the Koji
#      scratch build Packit already ran for a dist-git PR (KOJI_TASK_ID ->
#      koji-rpms.py downloads its RPMs); build an SRPM ON the guest -- from
#      a clone of FREEIPA_REPO_URL at FREEIPA_REPO_REF (the TF case, where
#      the fmf tree is only a plain file copy without .git/submodules), from
#      the dist-git checkout the plan ships (FREEIPA_DISTGIT, see
#      distgit-srpm.sh), or from the checkout named by FREEIPA_SRC (local
#      tmt runs)). Needed only if a channel image is missing, and not at
#      all when FREEIPA_IPA_FROM_COPR / FREEIPA_IPA_FROM_DISTRO make the
#      image bake install the IPA with dnf instead;
#   3. build the union of the batch's channel images from those RPMs/SRPMs,
#      each freshness-checked (a present freeipa-ci/full:<channel> is left
#      untouched);
#   4. run each preset's up -> run -> down recipe via tf-job.sh, which stages
#      its own per-preset results.yaml + artifacts under the test data dir;
#   5. reparent each preset's collected artifacts into artifacts/<key>/ so the
#      host maps each preset back to its own local workdir.
#
# The test is declared `result: custom` in main.fmf: tmt (1.77+) takes the
# outcome from $TMT_TEST_DATA/results.yaml alone (tmt never reads the
# per-preset <key>/results.yaml files). Each preset's stages are therefore
# MERGED into that single main file as namespaced entries /<key>/<stage>
# (rendered <test>/<key>/<stage>), appended after the batch '/' parent --
# see the merge loop near the end. The host report reads the per-key
# <key>/results.yaml directly. The staged machinery in stage-lib.sh records
# the stages with live logs; TF uploads the whole tmt workdir, so every
# stage and artifact shows up in results.xml.
#
set -uo pipefail

# The ci-freeipa tree root: this script lives in tmt/tests/freeipa-env/,
# three levels below it.
# (TMT_TREE points at the plan's tree -- the FreeIPA checkout, or this repo
# for its own tmt/plans/distro -- so it is never used to find this tree.)
CI="$(cd "$(dirname "$0")/../../.." && pwd)"
CLI="$CI/env/freeipa-env"
export CI CLI

# --- the batch: a JSON array of {key, preset_rel} (from build_tf_request) ---
# Backward compat: a request with the old single-preset vars (no FREEIPA_JOBS)
# becomes a one-job batch.
if [ -z "${FREEIPA_JOBS:-}" ]; then
    : "${FREEIPA_PRESET:?FREEIPA_PRESET or FREEIPA_JOBS is required}"
    LEGACY_KEY="${FREEIPA_JOB_KEY:-$(printf '%s' "$FREEIPA_PRESET" | tr '/' '-')}"
    FREEIPA_JOBS="$(printf '[{"key": "%s", "preset_rel": "%s"}]' \
        "$LEGACY_KEY" "$FREEIPA_PRESET")"
fi
export FREEIPA_JOBS

# Parse the job list into "key<TAB>preset" lines (tab- and newline-free fields).
command -v python3 >/dev/null 2>&1 \
    || { echo "FATAL: python3 not found on the guest" >&2; exit 1; }
JOB_LINES="$(python3 - "$FREEIPA_JOBS" <<'PY'
import json, sys
jobs = json.loads(sys.argv[1])
for j in jobs:
    key, preset = j['key'], j['preset_rel']
    for bad in ('\t', '\n'):
        key = key.replace(bad, '_')
        preset = preset.replace(bad, ' ')
    print(f'{key}\t{preset}')
PY
)" || die "could not parse FREEIPA_JOBS: $FREEIPA_JOBS"
[ -n "$JOB_LINES" ] || die "FREEIPA_JOBS parsed to an empty job list"

# A nested-qemu preset runs in qemu VMs, not in podman containers: it has no
# channel image to bake, and the guest needs KVM, an ssh key and the cloud
# images it names (qemu-prep.sh).
is_qemu_preset() {
    grep -qE '^provider:[[:space:]]*nested' "$1" \
        && grep -qE '^[[:space:]]+backend:[[:space:]]*qemu' "$1"
}
TOTAL_JOBS=0
QEMU_PRESETS=()
while IFS=$'\t' read -r _JK JPRESET; do
    [ -n "$JPRESET" ] || continue
    [ -f "$CI/env/$JPRESET" ] || die "preset not found: $CI/env/$JPRESET"
    TOTAL_JOBS=$((TOTAL_JOBS + 1))
    if is_qemu_preset "$CI/env/$JPRESET"; then QEMU_PRESETS+=("$CI/env/$JPRESET"); fi
done <<EOF
$JOB_LINES
EOF

# stage-lib.sh requires WORKDIR before sourcing. In the batch model the per
# preset workdir only exists inside its tf-job.sh child (it derives
# WORKDIR from JOB_WORKDIR), so the driver points its own namespace at the
# batch home: the driver's stage logs + the batch-parent results.yaml live
# under $STAGEDATA (the tmt test data dir) and the batch home doubles as the
# WORKDIR stage-lib's fallback needs (per-preset jobs use their own).
WORKDIR="$HOME/freeipa-jobs"
# staged results machinery (driver namespace: the tmt test data dir). The
# driver's own results.yaml is the batch parent; each preset child writes its
# own under <key>/ (see tf-job.sh).
source "$CI/tmt/tests/freeipa-env/stage-lib.sh"

# podman is installed by the plan's prepare step; keep a guard so a missing
# tool fails with a clear message instead of a cryptic `podman` not found.
# A batch of only nested-qemu presets does not use it.
if [ "${#QEMU_PRESETS[@]}" -lt "$TOTAL_JOBS" ]; then
    command -v podman >/dev/null 2>&1 || die "podman not found on the guest (prepare step should have installed it)"
    echo "podman: $(podman --version 2>/dev/null | head -1)"
    [ -d /run/systemd/system ] && echo "systemd: running" \
        || echo "WARN: systemd not detected; the podman provider may time out waiting for multi-user.target"
fi
# KVM, an ssh key and the cloud images for the qemu VMs
if [ "${#QEMU_PRESETS[@]}" -gt 0 ]; then
    bash "$CI/tmt/tests/freeipa-env/qemu-prep.sh" "${QEMU_PRESETS[@]}" \
        || die "qemu host preparation failed"
fi

SRPM_URL="${FREEIPA_SRPM_URL:-}"
REPO_URL="${FREEIPA_REPO_URL:-}"
REPO_REF="${FREEIPA_REPO_REF:-HEAD}"
# FREEIPA_SRC: an existing FreeIPA checkout to build the SRPM from (local
# tmt runs: tmt run -e FREEIPA_SRC=$PWD). The TF transport always sets
# FREEIPA_REPO_URL/REF instead; a checkout is never used when both are
# given, so a guest never touches a tree it does not own.
FREEIPA_SRC="${FREEIPA_SRC:-}"
GLOBAL_CHANNEL="${FREEIPA_CHANNEL:-}"
# FREEIPA_DISTGIT: build the SRPM from the dist-git checkout the plan ships
# (the Fedora dist-git CI case: the plan tree IS the dist-git repo -- spec +
# patches + `sources` hashes; the tarballs are fetched from the lookaside).
# FREEIPA_DISTGIT_DIR overrides the tree path (default: $TMT_TREE, which tmt
# exports to every test). The dist-git lane also passes --builddep to
# images/build.sh: the spec under test may carry BuildRequires the baked
# build image does not have, and the gate must build the package the way
# Fedora's own build system would.
DISTGIT="${FREEIPA_DISTGIT:-}"
DISTGIT_DIR="${FREEIPA_DISTGIT_DIR:-}"
# Additional COPR repos to enable on the channel-image bakes (space-joined
# OWNER/PROJECT list from the request's FREEIPA_COPR_REPOS variable). This
# customizes channel *creation* on the guest without touching any preset.
COPR_REPOS="${FREEIPA_COPR_REPOS:-}"
COPR_ARGS=()
for _r in $COPR_REPOS; do COPR_ARGS+=(--copr "$_r"); done
[ -n "$COPR_REPOS" ] && echo "== COPR repos for the channel-image bakes: $COPR_REPOS"
# Install IPA with dnf instead of building it from an SRPM: the SRPM
# download/build stage is skipped entirely and each channel image is baked
# with `dnf -y install <IPA packages>`.
#   FREEIPA_IPA_FROM_COPR    from the enabled COPR repos (build.sh
#                            --ipa-from-copr); wins over the next one, which
#                            plans/distro sets as its default;
#   FREEIPA_IPA_FROM_DISTRO  the packages the guest's own Fedora release
#                            ships (build.sh --ipa-from-distro); no COPR
#                            repo is allowed.
# Both bake for the guest's dist, so the request's compose picks the
# release under test.
# IPA_PACKAGES (optional) overrides the default dnf spec set (every IPA
# subpackage -- the full server incl. freeipa-server-dns, the client, the
# python3-* bits and python3-ipatests).
IPA_FROM=""
[ -n "${FREEIPA_IPA_FROM_DISTRO:-}" ] && IPA_FROM=distro
[ -n "${FREEIPA_IPA_FROM_COPR:-}" ] && IPA_FROM=copr
IPA_PACKAGES="${FREEIPA_IPA_PACKAGES:-}"
[ -n "$IPA_FROM" ] && echo "== IPA-from-$IPA_FROM mode: ${COPR_REPOS:-no COPR}${IPA_PACKAGES:+ ($IPA_PACKAGES)}"
# KOJI_TASK_ID: set by Packit on Fedora dist-git CI test requests -- the
# task id of the Koji scratch build Packit already ran for this PR. When it
# is present the batch REUSES that build: koji-rpms.py downloads the task's
# binary RPMs and the channel images are baked from them (build.sh --rpms)
# with no SRPM and no build image -- instead of paying for a second build.
KOJI_TASK="${KOJI_TASK_ID:-}"
[ -n "$KOJI_TASK" ] && echo "== scratch-build reuse: koji task $KOJI_TASK"
# The dist of the guest == the dist the dist-git PR targets (Packit picks
# the compose per dist-git branch). The koji lane's RPMs were built for
# exactly that dist and the dist-git lane's SRPM carries its %{dist}, so
# those lanes bake the base/channel images for the same dist; rawhide has
# no numeric toolbox tag yet, so it uses the rolling one.
guest_dist() {
    if grep -qi rawhide /etc/fedora-release /etc/os-release 2>/dev/null; then
        echo rawhide
        return
    fi
    rpm -E '%{dist}' 2>/dev/null | sed 's/^\.fc//'
}
BATCH_HOME="$HOME/freeipa-jobs"
SRPM_DIR="$BATCH_HOME/batch-srpm"

echo "== tf-runner: batch of $(printf '%s\n' "$JOB_LINES" | grep -c .) job(s); staged results -> $STAGEDATA"

# --- collect the batch's channel images (union across presets) --------------
# Only the three abstract channels are buildable (freeipa_env.image.is_channel);
# a concrete image reference or missing `image:` line skips the build and lets
# `up`/`resolve` report the usual "image not found; build it" error. The
# queue-level FREEIPA_CHANNEL overrides the per-preset channel for every job.
CHANNELS=""
add_channel() {
    case " $CHANNELS " in
        *" $1 "*) ;;
        *) CHANNELS="${CHANNELS:+$CHANNELS }$1" ;;
    esac
}
while IFS=$'\t' read -r JK JPRESET; do
    [ -n "$JPRESET" ] || continue
    [ -f "$CI/env/$JPRESET" ] || die "preset not found: $CI/env/$JPRESET"
    # a nested-qemu preset runs in VMs: there is no channel image to bake
    is_qemu_preset "$CI/env/$JPRESET" && continue
    if [ -n "$GLOBAL_CHANNEL" ]; then
        add_channel "$GLOBAL_CHANNEL"
    else
        image="$(sed -nE 's/^image:[[:space:]]*//p' "$CI/env/$JPRESET" | head -n1 | tr -d '[:space:]"' )"
        case "$image" in
            freeipa-current|freeipa-next|freeipa-previous) add_channel "${image#freeipa-}" ;;
        esac
    fi
done <<EOF
$JOB_LINES
EOF

# build_srpm_from_clone OUT_DIR
#   Build freeipa's SRPM DIRECTLY ON THE GUEST. The plan's prepare step
#   already installed the whole BuildRequires set + toolchain, so no build
#   image is needed. Mirrors makerpms.sh (autoreconf + configure with the
#   spec's rpm-equivalent flags) but only produces the SRPM (make srpms, no
#   binaries, no %check). The source is FREEIPA_SRC (an existing checkout,
#   built as-is) or a fresh clone of FREEIPA_REPO_URL at FREEIPA_REPO_REF;
#   without either there is no buildable source (a plain file copy has no
#   .git and no submodule contents).
build_srpm_from_clone() {
    outdir="$1"
    if [ -n "$FREEIPA_SRC" ]; then
        SRC="$FREEIPA_SRC"
        echo "==> using the FreeIPA checkout FREEIPA_SRC=$SRC"
        [ -d "$SRC/.git" ] || die "FREEIPA_SRC=$SRC is not a git checkout (submodules need .git)"
        cd "$SRC" || die "cannot cd to $SRC"
    elif [ -n "$REPO_URL" ]; then
        SRC="$BATCH_HOME/src"
        rm -rf "$SRC"
        echo "==> git clone --recursive $REPO_URL (ref: $REPO_REF) -> $SRC"
        git clone --recursive "$REPO_URL" "$SRC" \
            || die "git clone of $REPO_URL failed"
        cd "$SRC" || die "cannot cd to $SRC"
        [ "$REPO_REF" = "HEAD" ] || \
            git checkout "$REPO_REF" || die "git checkout $REPO_REF failed"
    else
        die "no SRPM source: set FREEIPA_SRC to a FreeIPA checkout or FREEIPA_REPO_URL to clone one"
    fi
    git submodule update --init --recursive \
        || die "git submodule update failed"
    git checkout po/*.po 2>/dev/null || true
    test -x configure && echo "configure: present" \
        || { echo "==> autoreconf -i"; autoreconf -i || die "autoreconf -i failed"; }
    echo "==> ./configure (spec-equivalent rpm flags)"
    ./configure --enable-silent-rules \
        --host="$(rpm -E %{_host})" \
        --build="$(rpm -E %{_build})" \
        --prefix="$(rpm -E %{_prefix})" \
        --exec-prefix="$(rpm -E %{_exec_prefix})" \
        --bindir="$(rpm -E %{_bindir})" \
        --sbindir="$(rpm -E %{_sbindir})" \
        --sysconfdir="$(rpm -E %{_sysconfdir})" \
        --datadir="$(rpm -E %{_datadir})" \
        --includedir="$(rpm -E %{_includedir})" \
        --libdir="$(rpm -E %{_libdir})" \
        --libexecdir="$(rpm -E %{_libexecdir})" \
        --localstatedir="$(rpm -E %{_localstatedir})" \
        --sharedstatedir="$(rpm -E %{_sharedstatedir})" \
        --mandir="$(rpm -E %{_mandir})" \
        --infodir="$(rpm -E %{_infodir})" || die "configure failed"

    echo "==> make srpms (on the guest)"
    make srpms || die "make srpms failed"
    ls "$SRC"/dist/srpms/

    cp "$SRC"/dist/srpms/*.src.rpm "$outdir/" \
        || die "make srpms produced no .src.rpm in dist/srpms"
    cd "$CI" || die "cannot cd back to $CI"
}

# --- ensure the IPA RPMs + every missing channel image ----------------------
# The IPA packages reach the channel image through one of five lanes, in this
# order: IPA-from-COPR or IPA-from-distro (explicit, installed with dnf), a
# pre-supplied SRPM URL, the dist-git
# scratch-build reuse (KOJI_TASK_ID: download the RPMs Packit already built),
# the dist-git tree SRPM build, or the upstream source build. The SRPM lanes
# only need the SRPM when at least one channel image is absent (a batch whose
# images are all pre-baked skips both the SRPM build and the bake).
# A batch with qemu presets, and a source of IPA in the request (not
# IPA-from-COPR/distro): build once, and the VMs install the RPMs.
QEMU_BUILD=0
if [ "${#QEMU_PRESETS[@]}" -gt 0 ] && [ -z "$IPA_FROM" ] \
        && [ -n "$SRPM_URL$KOJI_TASK$DISTGIT$DISTGIT_DIR$FREEIPA_SRC$REPO_URL" ]; then
    QEMU_BUILD=1
fi
MISSING=""
for c in $CHANNELS; do
    if ! podman image inspect "freeipa-ci/full:$c" >/dev/null 2>&1; then
        MISSING="${MISSING:+$MISSING }$c"
    fi
done
if [ -n "$MISSING" ] || [ "$QEMU_BUILD" = 1 ]; then
    if [ -n "$IPA_FROM" ]; then
        # dnf lanes: no SRPM download/build; bake each missing channel image
        # with the IPA installed from the enabled COPR repos, or (distro)
        # from the guest's own Fedora release.
        DNF_ARGS=("--ipa-from-$IPA_FROM" ${COPR_ARGS[@]+"${COPR_ARGS[@]}"}
                  --dist "$(guest_dist)")
        for c in $MISSING; do
            echo "== building channel $c image with IPA from $IPA_FROM"
            bash "$CI/images/build.sh" "${DNF_ARGS[@]}" --channel "$c" \
                --tool podman \
                ${IPA_PACKAGES:+--ipa-packages "$IPA_PACKAGES"} \
                || die "channel image build (IPA-from-$IPA_FROM) failed for $c"
            podman image inspect "freeipa-ci/full:$c" >/dev/null 2>&1 \
                || die "build succeeded but freeipa-ci/full:$c is still missing"
        done
    else
        BUILD_SRC=()     # --srpm <dir> or --rpms <dir>
        BUILD_EXTRA=()   # lane-specific: --builddep, --dist
        if [ -n "$SRPM_URL" ]; then
            mkdir -p "$SRPM_DIR"
            echo "== downloading SRPM $SRPM_URL -> $SRPM_DIR/"
            curl -fsSL -o "$SRPM_DIR/freeipa.src.rpm" "$SRPM_URL" \
                || die "failed to download SRPM from $SRPM_URL"
            BUILD_SRC=(--srpm "$SRPM_DIR")
        elif [ -n "$KOJI_TASK" ]; then
            # Scratch-build reuse: Packit's Koji build for this PR already
            # produced the RPMs; fetch them and bake the image from them --
            # no SRPM, no build image, no rpmbuild. The RPMs were built for
            # the guest's dist, so the images are baked for that dist too.
            RPMS_DIR="$BATCH_HOME/koji-rpms"
            echo "== reusing scratch build: fetching RPMs of koji task $KOJI_TASK -> $RPMS_DIR/"
            python3 "$CI/tmt/tests/freeipa-env/koji-rpms.py" "$KOJI_TASK" "$RPMS_DIR" \
                || die "koji scratch-build RPM fetch failed"
            BUILD_SRC=(--rpms "$RPMS_DIR")
            BUILD_EXTRA+=(--dist "$(guest_dist)")
        elif [ -n "$DISTGIT" ] || [ -n "$DISTGIT_DIR" ]; then
            mkdir -p "$SRPM_DIR"
            TREE="${DISTGIT_DIR:-$TMT_TREE}"
            [ -n "$TREE" ] && [ -d "$TREE" ] || \
                die "FREEIPA_DISTGIT needs the dist-git tree; TMT_TREE is unset or not a directory"
            echo "== dist-git lane: building the SRPM from the plan tree $TREE"
            bash "$CI/tmt/tests/freeipa-env/distgit-srpm.sh" "$TREE" "$SRPM_DIR" \
                || die "dist-git SRPM build failed"
            BUILD_SRC=(--srpm "$SRPM_DIR")
            BUILD_EXTRA+=(--builddep --dist "$(guest_dist)")
        else
            mkdir -p "$SRPM_DIR"
            echo "== full flow: no SRPM URL; building freeipa's SRPM from source"
            build_srpm_from_clone "$SRPM_DIR" || die "SRPM build on the guest failed"
            BUILD_SRC=(--srpm "$SRPM_DIR")
        fi
        for c in $MISSING; do
            echo "== building channel $c image"
            # build.sh builds the base (+ build, in the SRPM lanes) images
            # itself when absent, so this one call covers every lane; present
            # channel images were skipped above (build once, reuse).
            bash "$CI/images/build.sh" "${BUILD_SRC[@]}" --channel "$c" \
                --tool podman ${COPR_ARGS[@]+"${COPR_ARGS[@]}"} \
                ${BUILD_EXTRA[@]+"${BUILD_EXTRA[@]}"} \
                || die "channel image build failed for $c"
            podman image inspect "freeipa-ci/full:$c" >/dev/null 2>&1 \
                || die "build succeeded but freeipa-ci/full:$c is still missing"
        done
        if [ "$QEMU_BUILD" = 1 ]; then
            IPA_RPMS="$BATCH_HOME/ipa-rpms"
            # the VMs run the Fedora of their image, not the guest's: an RPM
            # built for another release does not install on them
            QEMU_DIST="$(python3 "$CI/tmt/tests/freeipa-env/adapt-preset.py" \
                --dist "${QEMU_PRESETS[@]}")" \
                || die "the qemu presets need VM images of one Fedora release"
            echo "== building the IPA RPMs for the qemu VMs${QEMU_DIST:+ (Fedora $QEMU_DIST)} -> $IPA_RPMS"
            bash "$CI/images/build.sh" "${BUILD_SRC[@]}" --build-only \
                --rpms-out "$IPA_RPMS" --tool podman \
                ${COPR_ARGS[@]+"${COPR_ARGS[@]}"} \
                ${BUILD_EXTRA[@]+"${BUILD_EXTRA[@]}"} \
                ${QEMU_DIST:+--dist "$QEMU_DIST"} \
                || die "building the IPA RPMs for the qemu VMs failed"
        fi
    fi
else
    if [ -n "$IPA_FROM" ]; then
        echo "== all channel image(s) present: $(echo $CHANNELS)"
    else
        echo "== all channel image(s) present: $(echo $CHANNELS); SRPM build skipped"
    fi
fi

# --- qemu presets get the IPA built above (vm.rpms) --------------------------
# A qemu preset installs its IPA on its own VMs from packages; when this batch
# builds FreeIPA (the guest cloned a branch, an SRPM or a Koji build came with
# the request) the VMs install those RPMs instead. Each qemu preset is
# copied to env/generated/tf-<key>.yaml with vm.rpms set and the job runs the
# copy.
if [ -n "${IPA_RPMS:-}" ]; then
    NEW_JOB_LINES=""
    while IFS=$'\t' read -r _K _P; do
        [ -n "$_K" ] || continue
        if is_qemu_preset "$CI/env/$_P"; then
            mkdir -p "$CI/env/generated"
            python3 "$CI/tmt/tests/freeipa-env/adapt-preset.py" \
                "$CI/env/$_P" "$CI/env/generated/tf-$_K.yaml" \
                --rpms "$IPA_RPMS" \
                || die "cannot give $_P the built RPMs"
            _P="generated/tf-$_K.yaml"
            echo "== $_K: qemu VMs install the RPMs built here ($IPA_RPMS)"
        fi
        NEW_JOB_LINES="${NEW_JOB_LINES:+$NEW_JOB_LINES
}$_K	$_P"
    done <<EOF
$JOB_LINES
EOF
    JOB_LINES="$NEW_JOB_LINES"
fi

# --- run each preset's recipe (per-preset staged results) -------------------
cd "$CI" || die "cannot cd to $CI"
WORST_RC=0
JOBS_NOTED=""
# Read the job list from fd 3, NOT fd 0. tf-job.sh (and its podman/ssh/test
# descendants) inherits the loop's stdin; if the list lived on fd 0 the
# first job's child would consume the remaining lines, so the outer read
# would hit EOF and every preset after the first would be silently skipped --
# while WORST_RC stayed 0 and the batch still reported as passed. A dedicated
# fd keeps the list out of reach of any child process. The child's own stdin
# is pinned to /dev/null (a batch job is non-interactive; never read fd 0).
while IFS=$'\t' read -r -u 3 JK JPRESET; do
    [ -n "$JK" ] || continue
    echo ""
    echo "########## job $JK: preset=$JPRESET ##########"
    export JOBKEY="$JK" ENFPRESET="$JPRESET" \
        JOB_WORKDIR="$BATCH_HOME/$JK"
    bash "$CI/tmt/tests/freeipa-env/tf-job.sh" </dev/null
    rc=$?
    [ "$rc" -gt "$WORST_RC" ] && WORST_RC=$rc
    JOBS_NOTED="${JOBS_NOTED:+$JOBS_NOTED, }$JK=rc$rc"
done 3<<EOF
$JOB_LINES
EOF

# --- reparent each preset's collected artifacts into artifacts/<key>/ -------
# tf-job.sh staged them under <key>/artifacts/ (so they never collided while
# running); the host's fetch_artifacts maps .../artifacts/<key>/<rel> back to
# the <key> job's local workdir/logs/<rel>.
mkdir -p "$STAGEDATA/artifacts"
for d in "$STAGEDATA"/*/; do
    key="$(basename "$d")"
    [ -d "$STAGEDATA/artifacts/$key" ] && continue
    if [ -d "$d/artifacts" ]; then
        mv "$d/artifacts" "$STAGEDATA/artifacts/$key" \
            && echo "== reparented $STAGEDATA/$key/artifacts -> $STAGEDATA/artifacts/$key/"
    fi
done

# --- batch parent results ---------------------------------------------------
[ "$WORST_RC" -eq 0 ] || OVERALL=fail
if [ -n "$FATAL_NOTE" ]; then
    :  # a die() set the parent note (and exited via the trap)
else
    # The batch summary is informational, not a fatal: a green batch must not
    # carry a "fatal:" note. Only a die() (a genuine fatal that exits) leaves
    # FATAL_NOTE set; on the normal completion path it is empty, so the
    # summary goes in PARENT_NOTE and is emitted without the fatal: prefix.
    PARENT_NOTE="batch: $(printf '%s\n' "$JOB_LINES" | grep -c .) job(s); $JOBS_NOTED"
fi
RUN_RC="$WORST_RC"
write_results

# --- merge each preset's namespaced stage fragment into the MAIN file ------
# tmt only reads $TMT_TEST_DATA/results.yaml (this file). write_results just
# wrote the batch '/' parent; now append every preset's stage entries, which
# tf-job.sh already finalized (via write_stage_fragment) in data-dir-relative
# form: names /<key>/<stage> -> tmt renders <test>/<key>/<stage>, and log:
# paths keyed by the post-reparent locations. Concatenating a YAML list onto
# a YAML list yields one valid list, so tmt/TF shows the per-preset +
# per-stage split. The host report reads the per-key <key>/results.yaml
# directly (unchanged), so this only feeds tmt/TF.
for f in "$STAGEDATA"/*/stages.yaml; do
    [ -f "$f" ] || continue
    cat "$f" >> "$RESULTS_FILE"
    echo "== merged stage fragment $f into $RESULTS_FILE"
done

rm -f "$STAGEDATA/.results-pending"
echo "== results: $RESULTS_FILE"
echo "== batch finished, worst job rc $WORST_RC"
exit "$WORST_RC"
