#!/usr/bin/env bash
# Build the DriveForge Field-Check Live ISO.
#
# Produces a tmpfs-backed bootable image that boots into the field_check
# daemon role — for plug-into-an-unknown-server-and-inspect workflows
# where DriveForge MUST NOT touch the host's disks.
#
# Distinct from scripts/build-iso.sh which produces the standard
# DriveForge installer ISO (debian-installer + preseed). This script
# uses Debian's `live-build` to produce a live-bootable squashfs ISO
# that runs entirely from RAM after boot.
#
# Run on a Debian 12 host (typically inside the iso-fieldcheck/Dockerfile
# container — see scripts/build-fieldcheck-iso-docker.sh for the wrapper
# that handles the container plumbing on macOS / non-Debian hosts).
#
# Output: dist/driveforge-fieldcheck-<version>-amd64.iso

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Inside the build container we're already root and don't need sudo.
# On a bare host the user invokes via `sudo` (live-build needs root
# for chroot + loopback mount operations).
IN_CONTAINER=0
[[ -f /.dockerenv ]] && IN_CONTAINER=1

if [[ $IN_CONTAINER -eq 0 && $EUID -ne 0 ]]; then
  echo "this script needs root on a bare host (live-build chroot + loopback)"
  echo "re-run with sudo, or use ./scripts/build-fieldcheck-iso-docker.sh"
  exit 1
fi

for tool in lb xorriso debootstrap; do
  command -v "$tool" >/dev/null || {
    echo "missing: $tool — apt install live-build xorriso debootstrap"
    exit 1
  }
done

VERSION=$(grep -E '^version' pyproject.toml | head -1 | cut -d'"' -f2 || date +%Y%m%d)
DIST="$ROOT/dist"
# v1.1.1: build inside the container's own filesystem rather than the
# bind-mounted dist/. macOS Docker bind mounts are mounted `nodev`, which
# blocks debootstrap from `mknod`'ing /dev/null inside the chroot it's
# bootstrapping. /tmp inside the container is on overlayfs which permits
# device nodes. Linux CI runners don't have this restriction but using
# /tmp consistently gives one code path that works everywhere.
# After the build, the resulting ISO is moved out to $DIST/ where it
# can be picked up by the host or by CI's "upload artifact" step.
if [[ $IN_CONTAINER -eq 1 ]]; then
  WORK="/tmp/fieldcheck-iso-build"
else
  WORK="$DIST/fieldcheck-iso-build"
fi
OUT="$DIST/driveforge-fieldcheck-${VERSION}-amd64.iso"

echo "==> Building DriveForge Field-Check Live ISO v${VERSION}"
echo "    output: $OUT"

# Wipe any previous build state. live-build's `lb clean --purge` does the
# right thing inside the work dir.
mkdir -p "$WORK"
cd "$WORK"
if [[ -f config/binary ]]; then
  echo "==> Cleaning previous live-build state"
  lb clean --purge >/dev/null 2>&1 || true
fi

# Stage the live-build config tree from iso-fieldcheck/. We copy rather
# than symlink because live-build mutates files under config/ during
# `lb config` and we don't want those mutations leaking back to the
# repo.
echo "==> Staging live-build config from iso-fieldcheck/"
rsync -a --delete "$ROOT/iso-fieldcheck/" "$WORK/"

# Stage the DriveForge source into the chroot includes. The post-install
# hook (config/hooks/normal/9000-install-driveforge.hook.chroot) will
# `pip install` from this path inside the chroot. Excludes that don't
# need to land in the live image (saves ISO bytes + build time):
#   .git           — not needed at runtime; pip install reads pyproject.toml
#   dist/          — built ISOs from prior runs, would create a recursion
#   .venv/         — local dev virtualenv
#   tests/         — runtime doesn't need test fixtures (saves ~30 MB)
#   docs/          — Jekyll source for the GitHub Pages site
echo "==> Staging DriveForge source into chroot includes"
mkdir -p "$WORK/config/includes.chroot/opt/driveforge-src"
rsync -a \
    --exclude='.git/' \
    --exclude='dist/' \
    --exclude='.venv/' \
    --exclude='tests/' \
    --exclude='docs/' \
    --exclude='__pycache__/' \
    --exclude='*.egg-info/' \
    "$ROOT/" "$WORK/config/includes.chroot/opt/driveforge-src/"

# Generate the live-build config files from auto/config.
echo "==> Running lb config"
lb config

# Actually build the ISO. This pulls debian packages, runs the hook
# inside the chroot, builds the squashfs, lays out the hybrid ISO.
# Takes ~5-10 minutes on a fresh build, ~3-5 with apt cache warm.
echo "==> Running lb build (this takes 5-10 minutes)"
lb build

# Live-build's output filename is fixed: live-image-amd64.hybrid.iso.
# Rename to the DriveForge convention so it sits cleanly alongside
# the installer ISO in dist/.
LB_OUTPUT="$WORK/live-image-amd64.hybrid.iso"
if [[ ! -f "$LB_OUTPUT" ]]; then
  echo "✗ live-build did not produce live-image-amd64.hybrid.iso"
  ls -la "$WORK/" >&2
  exit 1
fi

mkdir -p "$DIST"
mv "$LB_OUTPUT" "$OUT"

echo
echo "✓ Field-Check Live ISO built:"
ls -lh "$OUT"
echo
echo "  Flash to a USB stick:"
echo "    sudo dd if=$OUT of=/dev/sdX bs=4M status=progress conv=fsync"
echo "  Then boot any server from the USB and hit http://<that-server-ip>:8080"
