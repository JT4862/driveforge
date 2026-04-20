#!/usr/bin/env bash
# Build the DriveForge installer ISO from a Mac (or any non-Debian host)
# using Docker. Wraps the Debian-only build pipeline (apt-get, xorriso) in
# a debian:12-slim container so you don't need a separate Debian VM.
#
# Usage:
#   ./scripts/build-iso-docker.sh
#
# Output:
#   dist/driveforge-installer-<version>-amd64.iso  (host)
#
# Requirements:
#   - Docker daemon running (Docker Desktop, Colima, OrbStack, or similar)
#   - ~2 GB free disk for image + Debian netinst cache + bundle staging

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# --- Sanity ------------------------------------------------------------------
command -v docker >/dev/null || {
  echo "✗ docker CLI not found — install Docker Desktop / Colima / OrbStack first" >&2
  exit 1
}
docker info >/dev/null 2>&1 || {
  echo "✗ Docker daemon not running — start Docker Desktop / Colima / OrbStack" >&2
  exit 1
}

# Always target linux/amd64 so the resulting ISO boots on x86 servers (the R720
# and most enterprise hardware). On Apple Silicon this runs via QEMU emulation
# under Rosetta — slower than native (~5-15 min total build) but produces the
# correct amd64 binary output.
PLATFORM="${BUILD_PLATFORM:-linux/amd64}"
IMAGE="driveforge-iso-builder"

echo "==> Building Docker image $IMAGE (platform=$PLATFORM)"
docker build --platform "$PLATFORM" -t "$IMAGE" "$ROOT/iso"

mkdir -p "$ROOT/dist"

echo "==> Running ISO build inside container"
echo "    (downloads Debian netinst + apt deps + wheels on first run; cached after)"
echo
docker run --rm --platform "$PLATFORM" \
  -v "$ROOT:/src" \
  -w /src \
  "$IMAGE" -c './scripts/build-iso.sh'

echo
echo "✓ ISO build complete:"
ls -lh "$ROOT/dist"/*.iso 2>/dev/null || echo "  (no .iso found in dist/ — check the build log above)"
echo
echo "  Flash to a USB stick (replace /dev/diskN with your USB device):"
echo "    diskutil list"
echo "    sudo dd if=$ROOT/dist/driveforge-installer-*.iso of=/dev/rdiskN bs=4m status=progress"
