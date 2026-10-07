#!/bin/bash
# Build the package SRPM from a dist-git checkout (the Fedora dist-git lane).
#
# A Fedora dist-git repository tracks the spec, the patches, and a `sources`
# file carrying the hashes of the upstream tarballs; the tarballs themselves
# live on the lookaside server. Fedora CI (and any tmt run of a dist-git
# plan) puts exactly that tree on the guest as the plan tree (TMT_TREE, a
# plain file copy without .git), so the SRPM of the package *as the PR would
# build it* is produced here: fetch the lookaside sources, then `rpmbuild -bs`.
#
# This is the dist-git counterpart of tf-runner.sh's build_srpm_from_clone
# (which builds from an upstream source tree via autoreconf/configure/make
# srpms): a dist-git checkout has no configure.ac, only the spec.
#
# Usage: distgit-srpm.sh TREE OUT_DIR
#   TREE     dist-git checkout directory (spec + `sources` + patches)
#   OUT_DIR  directory the produced *.src.rpm is placed in
#
# Overrides:
#   FREEIPA_DISTGIT_SPEC   spec file name inside TREE
#                          (default: the single *.spec found there)
#   FREEIPA_DISTGIT_PKG    lookaside package (repository) name
#                          (default: the spec's Name:)
#   FREEIPA_LOOKASIDE_URL  lookaside base URL
#                          (default: https://src.fedoraproject.org/repo/pkgs)
set -uo pipefail

TREE="${1:?usage: distgit-srpm.sh TREE OUT_DIR}"
OUT="${2:?usage: distgit-srpm.sh TREE OUT_DIR}"
LOOKASIDE="${FREEIPA_LOOKASIDE_URL:-https://src.fedoraproject.org/repo/pkgs}"

die() { echo "distgit-srpm: $*" >&2; exit 1; }

[ -d "$TREE" ] || die "dist-git tree not found: $TREE"
[ -f "$TREE/sources" ] || die "no \`sources\` file in $TREE (not a dist-git checkout?)"

# Work on a private copy: the plan tree is read-only CI input and the
# lookaside sources must land next to the spec for rpmbuild's %_sourcedir.
WORK="$(mktemp -d "${TMPDIR:-/tmp}/distgit-srpm.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
cp -a "$TREE"/. "$WORK"/ 2>/dev/null || die "cannot copy $TREE"
rm -rf "$WORK/.git"

if [ -n "${FREEIPA_DISTGIT_SPEC:-}" ]; then
    SPEC="$WORK/$FREEIPA_DISTGIT_SPEC"
    [ -f "$SPEC" ] || die "FREEIPA_DISTGIT_SPEC=$FREEIPA_DISTGIT_SPEC not in the tree"
else
    mapfile -t SPECS < <(find "$WORK" -maxdepth 1 -name '*.spec' | sort)
    [ "${#SPECS[@]}" -eq 1 ] || die \
        "expected exactly one *.spec in the tree, found ${#SPECS[@]} (set FREEIPA_DISTGIT_SPEC)"
    SPEC="${SPECS[0]}"
fi
echo "==> spec: $(basename "$SPEC")"

# The lookaside path is keyed by the dist-git repository name; the spec's
# Name: is the working default (it matches for ordinary packages).
PKG="${FREEIPA_DISTGIT_PKG:-$(rpmspec -q --srpm --queryformat '%{NAME}\n' "$SPEC" 2>/dev/null | head -n1)}"
[ -n "$PKG" ] || die "could not determine the package name from $SPEC (set FREEIPA_DISTGIT_PKG)"
echo "==> lookaside package: $PKG"

# `sources` lines: `SHA512 (freeipa-4.13.4.tar.gz) = <hex>` (older repos use
# MD5/SHA256; the lookaside URL uses the lowercased algorithm name).
nsrc=0
while read -r algo file _eq hash; do
    [ -n "$algo" ] || continue
    file="${file#(}"; file="${file%)}"
    [ -n "$file" ] && [ -n "$hash" ] || die "unparsed \`sources\` line: $algo $file"
    url="$LOOKASIDE/$PKG/$file/$(printf '%s' "$algo" | tr 'A-Z' 'a-z')/$hash/$file"
    echo "==> fetching $file"
    curl -fsSL -o "$WORK/$file" "$url" || die "download failed: $url"
    nsrc=$((nsrc + 1))
done < "$WORK/sources"
[ "$nsrc" -gt 0 ] || die "no sources listed in $WORK/sources"

# -bs only bundles spec + sources into the SRPM (no %prep, no BuildRequires
# evaluation): the binary build happens later in the freeipa-ci build image.
mkdir -p "$WORK/rpmbuild"/{BUILD,RPMS,SPECS,SRPMS}
echo "==> rpmbuild -bs"
rpmbuild --define "_sourcedir $WORK" \
         --define "_specdir $WORK" \
         --define "_topdir $WORK/rpmbuild" \
         -bs "$SPEC" || die "rpmbuild -bs failed"

mapfile -t SRPMS < <(find "$WORK/rpmbuild/SRPMS" -name '*.src.rpm')
[ "${#SRPMS[@]}" -ge 1 ] || die "rpmbuild -bs produced no .src.rpm"
mkdir -p "$OUT"
for s in "${SRPMS[@]}"; do
    cp "$s" "$OUT/" || die "cannot copy $s to $OUT"
    echo "==> SRPM: $OUT/$(basename "$s")"
done
