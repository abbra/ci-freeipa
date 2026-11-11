#!/bin/bash
# Make a Testing Farm guest able to run nested-qemu presets (the qemu VM
# backend of freeipa-env): KVM, an ssh key for the VMs, and the cloud images
# the presets name. Usage: qemu-prep.sh PRESET.yaml...
set -euo pipefail

[ -w /dev/kvm ] || {
    echo "FATAL: /dev/kvm is not writable: the request needs" \
         "hardware.virtualization.is-supported" >&2
    exit 1
}
for tool in qemu-system-x86_64 qemu-img rsync curl; do
    command -v "$tool" >/dev/null 2>&1 \
        || { echo "FATAL: $tool not found (qemu plan prepare step)" >&2; exit 1; }
done
command -v xorriso >/dev/null 2>&1 || command -v genisoimage >/dev/null 2>&1 \
    || { echo "FATAL: need xorriso or genisoimage for the cloud-init seed" >&2; exit 1; }

# the qemu backend authorizes ~/.ssh/id_{ed25519,ecdsa,rsa}.pub on the VMs;
# generate one only when the guest has none of them
if [ ! -e ~/.ssh/id_ed25519 ] && [ ! -e ~/.ssh/id_ecdsa ] \
        && [ ! -e ~/.ssh/id_rsa ]; then
    mkdir -p ~/.ssh && chmod 700 ~/.ssh
    ssh-keygen -q -t ed25519 -N '' -f ~/.ssh/id_ed25519
    echo "== qemu-prep: generated ~/.ssh/id_ed25519"
fi

# every image the presets name: vm.image and each vm.vms[].image
IMAGES="$(python3 - "$@" <<'PY'
import os, sys, yaml
seen = []
for path in sys.argv[1:]:
    vm = (yaml.safe_load(open(path)) or {}).get('vm') or {}
    for img in [vm.get('image')] + [v.get('image') for v in vm.get('vms') or []]:
        if img and os.path.expanduser(img) not in seen:
            seen.append(os.path.expanduser(img))
print('\n'.join(seen))
PY
)"

while IFS= read -r img; do
    [ -n "$img" ] || continue
    [ -s "$img" ] && { echo "== qemu-prep: have $img"; continue; }
    name="$(basename "$img")"
    rel="$(printf '%s' "$name" | sed -nE 's/^Fedora-Cloud-Base-Generic-([0-9]+)-.*/\1/p')"
    ver="$(printf '%s' "$name" | sed -nE 's/^Fedora-Cloud-Base-Generic-[0-9]+-([0-9.]+)\.x86_64\.qcow2$/\1/p')"
    [ -n "$rel" ] && [ -n "$ver" ] \
        || { echo "FATAL: cannot derive a download URL for $img" >&2; exit 1; }
    base="https://download.fedoraproject.org/pub/fedora/linux/releases/$rel/Cloud/x86_64/images"
    sum="$(curl -fsSL --retry 3 "$base/Fedora-Cloud-$rel-$ver-x86_64-CHECKSUM" \
           | sed -n "s/^SHA256 ($name) = //p")"
    [ -n "$sum" ] || { echo "FATAL: no checksum for $name" >&2; exit 1; }
    mkdir -p "$(dirname "$img")"
    echo "== qemu-prep: downloading $name"
    curl -fL --retry 3 -o "$img.partial" "$base/$name"
    echo "$sum  $img.partial" | sha256sum -c - >/dev/null \
        || { echo "FATAL: checksum mismatch for $name" >&2; rm -f "$img.partial"; exit 1; }
    mv "$img.partial" "$img"
done <<EOF
$IMAGES
EOF
echo "== qemu-prep: ready"
