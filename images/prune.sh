#!/bin/bash
# prune.sh — remove what image builds leave behind.
#
# Every rebuild of a channel tag orphans the previous image (a dangling
# <none> image), and an interrupted build leaves a buildah "working
# container" that keeps its base image from being removed. Neither is used by
# any environment: this removes both and nothing else (no tagged image, no
# container of an environment created by freeipa-env).
#
# Usage: images/prune.sh [--tool podman|docker] [--dry-run]
set -euo pipefail

TOOL=podman
DRY=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --tool) TOOL="$2"; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

run() { if [[ $DRY == 1 ]]; then echo "would run: $*"; else "$@"; fi; }

# buildah working containers are the ones podman lists only with --external;
# one that is up belongs to a build in progress and stays.
if [[ "$TOOL" == podman ]]; then
    while IFS='|' read -r id name status; do
        [[ -n "$id" ]] || continue
        [[ "$status" == Up* ]] && { echo "==> $name is running, left alone"; continue; }
        echo "==> working container $name ($id)"
        run "$TOOL" rm -f "$id"
    done < <("$TOOL" ps -a --external --format '{{.ID}}|{{.Names}}|{{.Status}}' \
             2>/dev/null | grep -E '\|[^|]*working-container' || true)
fi

dangling=$("$TOOL" images --filter dangling=true -q)
if [[ -z "$dangling" ]]; then
    echo "==> no dangling images"
else
    echo "==> dangling images: $(echo "$dangling" | wc -l)"
    # shellcheck disable=SC2086
    run "$TOOL" rmi $dangling || echo "some images are still in use; left in place"
fi
