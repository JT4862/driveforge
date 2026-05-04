#!/usr/bin/env bash
# Build the DriveForge Field-Check Live ISO from a Mac (or any non-Debian
# host) using Docker. Wraps scripts/build-fieldcheck-iso.sh in the
# iso-fieldcheck/Dockerfile container so you don't need a Debian VM
# locally.
#
# Usage:
#   ./scripts/build-fieldcheck-iso-docker.sh
#
# Output:
#   dist/driveforge-fieldcheck-<version>-amd64.iso  (host)
#
# Distinct from scripts/build-iso-docker.sh which produces the standard
# DriveForge installer ISO. Run both to produce both flavors locally.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

command -v docker >/dev/null || {
  echo "✗ docker CLI not found — install Docker Desktop / Colima / OrbStack first" >&2
  exit 1
}
docker info >/dev/null 2>&1 || {
  echo "✗ Docker daemon not running — start Docker Desktop / Colima / OrbStack" >&2
  exit 1
}

# Always target linux/amd64. live-build inside an arm64 container would
# bootstrap an arm64 Debian root — we want amd64 ISOs (x86 server target).
PLATFORM="${BUILD_PLATFORM:-linux/amd64}"
IMAGE="driveforge-fieldcheck-iso-builder"

echo "==> Building Docker image $IMAGE (platform=$PLATFORM)"
docker build --platform "$PLATFORM" -t "$IMAGE" "$ROOT/iso-fieldcheck"

mkdir -p "$ROOT/dist"

echo "==> Running Field-Check Live ISO build inside container"
echo "    (debootstraps Debian + pip-installs DriveForge; first run ~10 min)"
echo
# --privileged: live-build needs to mount loopback devices + chroot.
# These need real kernel access that bind mounts alone don't grant.
docker run --rm --privileged --platform "$PLATFORM" \
  -v "$ROOT:/src" \
  -w /src \
  "$IMAGE" -c './scripts/build-fieldcheck-iso.sh'

ISO_FILES=("$ROOT/dist"/driveforge-fieldcheck-*.iso)
if [[ ! -e "${ISO_FILES[0]}" ]]; then
  echo "✗ build reported success but no field-check ISO landed in dist/" >&2
  exit 1
fi

echo
echo "✓ Field-Check Live ISO build complete:"
ls -lh "$ROOT/dist"/driveforge-fieldcheck-*.iso
echo
echo "  Flash to a USB stick (replace /dev/diskN with your USB device):"
echo "    diskutil list"
echo "    sudo dd if=$ROOT/dist/driveforge-fieldcheck-*.iso of=/dev/rdiskN bs=4m status=progress"
