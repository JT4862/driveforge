#!/usr/bin/env bash
# Build a self-contained offline-install bundle for DriveForge.
#
# Run on an internet-connected Debian 12 (bookworm) machine — produces
# `dist/driveforge-offline-<version>.tar.gz` containing:
#   - DriveForge source (git archive of HEAD)
#   - All required .deb packages + their transitive dependencies
#   - All required Python wheels for the daemon + its deps
#
# To install on an air-gapped target:
#   tar xzf driveforge-offline-<ver>.tar.gz
#   cd driveforge-offline-<ver>
#   sudo DRIVEFORGE_OFFLINE_BUNDLE="$(pwd)" ./scripts/install.sh
#
# The same bundle is what the ISO installer embeds — see scripts/build-iso.sh.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

[[ -f pyproject.toml ]] || { echo "must run from a DriveForge source tree" >&2; exit 1; }
[[ "$(. /etc/os-release && echo "$VERSION_ID")" == "12" ]] || \
  echo "warning: building on non-Debian-12 host — bundle may not install on Debian 12 targets" >&2

# Try to read the version out of pyproject.toml; fall back to a date stamp.
VERSION=$(grep -E '^version' pyproject.toml | head -1 | cut -d'"' -f2 || true)
VERSION=${VERSION:-$(date +%Y%m%d)}
BUNDLE_NAME="driveforge-offline-${VERSION}"
DIST="$ROOT/dist"
WORK="$DIST/$BUNDLE_NAME"

echo "==> Building bundle: $BUNDLE_NAME"
rm -rf "$WORK" "$DIST/${BUNDLE_NAME}.tar.gz"
mkdir -p "$WORK/debs" "$WORK/wheels"

# 1. Source archive (clean, no .git or local untracked junk).
echo "==> Archiving source..."
git archive --format=tar HEAD | tar -x -C "$WORK"

# 2. Apt packages + transitive deps. Same list as install.sh keeps in
# APT_PACKAGES — keep these in sync if you add/remove a runtime dep.
APT_PACKAGES=(
  python3 python3-venv python3-pip
  smartmontools hdparm sg3-utils nvme-cli e2fsprogs fio
  tmux lshw lsscsi ipmitool avahi-daemon avahi-utils
  fonts-dejavu-core
  curl ca-certificates
)
echo "==> Resolving apt deps (recursive)..."
# apt-cache depends gives us the recursive set. The lines we want are the
# package names — they sit at column 0 (no leading whitespace). Lines like
# "  Depends: foo" sit at column 2 and we skip them. Use a POSIX bracket
# class instead of `\w` so this works in both gawk (host) and mawk (the
# debian:12-slim Docker image).
#
# CRITICAL: we must NOT use --no-pre-depends here. `python3` Pre-Depends on
# `python3-minimal`, and if python3-minimal is absent from the bundle then
# dpkg -i silently fails for the python3 meta-package, leaving /usr/bin/python3
# un-created, which in turn kills install.sh at its `python3 -m venv` line.
# Hit this on 2026-04-20 during the ISO late_command pipeline. Pre-Depends
# are strict predecessors that MUST be installed before the dependent package,
# so we need them in the bundle even more than regular Depends.
DEPS=$(apt-cache depends --recurse --no-recommends --no-suggests \
  --no-conflicts --no-breaks --no-replaces --no-enhances \
  "${APT_PACKAGES[@]}" \
  | awk '/^[a-zA-Z0-9]/ {gsub(/[<>:]/, "", $1); print $1}' | sort -u)
DEP_COUNT=$(echo "$DEPS" | wc -l)
echo "==> Downloading $DEP_COUNT .deb packages → $WORK/debs"
# When running as root (typically inside a Docker container), apt-get download
# tries to drop privileges to the `_apt` user but fails if that user can't
# write to the current directory. Make the dir world-writable as a portable
# fallback — debian:12-slim has the `_apt` user but no `_apt` group, so a
# chown _apt:_apt fails. chmod 777 works regardless of group setup.
if [[ $EUID -eq 0 ]]; then
  chmod 777 "$WORK/debs"
fi
(cd "$WORK/debs" && apt-get download $DEPS 2>&1 | grep -v "^Get:" || true)
DEB_COUNT=$(ls "$WORK/debs" | wc -l)
echo "    $DEB_COUNT .deb files cached"
if [[ $DEB_COUNT -lt 10 ]]; then
  echo "✗ apt-get download produced only $DEB_COUNT debs — apt cache likely empty"
  echo "  (the Dockerfile must keep /var/lib/apt/lists/ — don't strip it)"
  exit 1
fi

# 2b. Generate an apt repo index for the downloaded .debs so the target
# can install via `apt-get install` (which does proper dep resolution,
# including virtual packages like `python3:any`) instead of iterative
# `dpkg -i` passes that can't converge on deep python dep chains.
# apt-ftparchive is in apt-utils (already installed in the build container).
echo "==> Generating apt repo index (Packages / Packages.gz)"
(
  cd "$WORK/debs"
  # Packages file lists every .deb in this dir with its metadata so apt
  # can resolve dependencies properly against our closed set.
  apt-ftparchive packages . > Packages
  # Compressed variant — apt prefers this when both exist.
  gzip -9 --keep --force Packages
)
echo "    index: $(wc -l < "$WORK/debs/Packages") metadata lines ($(stat -c%s "$WORK/debs/Packages.gz") bytes compressed)"

# 3. Python wheels. `pip wheel` is the right tool for an offline bundle:
#    a) Builds the DriveForge package itself into a .whl using its
#       build backend (hatchling). hatchling is only needed AT build
#       time — not on the target — which is critical because `pip
#       download` would bring an sdist with NO hatchling, and the
#       target's --no-index pip install would then fail trying to
#       resolve hatchling from PyPI it can't reach. Hit this on
#       2026-04-20 when the ISO install died at
#       "No matching distribution found for hatchling".
#    b) Downloads all RUNTIME deps (plus the `[linux]` extra → pyudev)
#       as wheels next to the DriveForge wheel, so the target can just
#       `pip install --no-index --find-links wheels/ driveforge[linux]`
#       with no build backend required.
# Use the same Python version the target will have so wheels are ABI-
# compatible (/opt/driveforge is a Python 3.11 venv on Debian 12).
echo "==> Building wheels..."
pip wheel --quiet --wheel-dir "$WORK/wheels" "$ROOT[linux]" 2>&1 | tail -5 || true
echo "    $(ls "$WORK/wheels" | wc -l) wheel files cached"

# 4. Inline install hint so a user who unpacks the tarball without reading
# README knows what to do.
cat > "$WORK/INSTALL.txt" <<'INSTRUCTIONS'
DriveForge offline install bundle
=================================

This tarball contains everything needed to install DriveForge on an
air-gapped Debian 12 system — no internet access required.

To install:
  cd driveforge-offline-<version>
  sudo DRIVEFORGE_OFFLINE_BUNDLE="$(pwd)" ./scripts/install.sh

When DRIVEFORGE_OFFLINE_BUNDLE is set, install.sh will:
  - Install .deb packages from ./debs/ via dpkg (no apt-get update)
  - Install Python deps from ./wheels/ via pip --no-index

Otherwise it falls back to apt-get + pip's normal network paths,
making the same script work for both online and offline installs.

After install, the daemon listens on http://<host>:8080.
See README.md for the full usage guide.
INSTRUCTIONS

# 5. Tarball.
echo "==> Tarballing..."
tar czf "$DIST/${BUNDLE_NAME}.tar.gz" -C "$DIST" "$BUNDLE_NAME"
SIZE=$(du -sh "$DIST/${BUNDLE_NAME}.tar.gz" | cut -f1)

echo
echo "✓ Bundle ready: dist/${BUNDLE_NAME}.tar.gz ($SIZE)"
echo
echo "  Air-gapped install on the target machine:"
echo "    tar xzf ${BUNDLE_NAME}.tar.gz"
echo "    cd ${BUNDLE_NAME}"
echo "    sudo DRIVEFORGE_OFFLINE_BUNDLE=\"\$(pwd)\" ./scripts/install.sh"
