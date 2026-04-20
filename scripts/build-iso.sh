#!/usr/bin/env bash
# Build the DriveForge installer ISO.
#
# Takes a vanilla Debian 12 amd64 netinst ISO, injects:
#   - iso/preseed.cfg  (unattended install configuration)
#   - the offline bundle (debs + wheels + source) at /driveforge-bundle/
#   - a custom isolinux/grub menu that auto-selects the preseeded install
# and repacks into a bootable hybrid ISO suitable for:
#   - dd onto a USB stick
#   - mounting via iDRAC / IPMI virtual media
#   - booting in a VM
#
# Run on a Debian 12 host (or any Linux with xorriso + isolinux + apt-utils).
# Output: dist/driveforge-installer-<version>-amd64.iso
#
# Usage:
#   sudo ./scripts/build-iso.sh
# Optional env vars:
#   DEBIAN_ISO_URL  — override the upstream Debian netinst URL
#   DEBIAN_ISO      — path to a pre-downloaded netinst ISO (skips download)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Inside a Docker container we're already root and don't need sudo.
# On a bare host the user invokes via `sudo`. Detect the context so we know
# how to run the offline-bundle step (apt-get download wants a non-root user
# on real systems but is fine inside a container sandbox).
IN_CONTAINER=0
[[ -f /.dockerenv ]] && IN_CONTAINER=1

if [[ $IN_CONTAINER -eq 0 && $EUID -ne 0 ]]; then
  echo "this script needs root on a bare host (loopback mount + xorriso) — re-run with sudo"
  exit 1
fi

# --- Tools --------------------------------------------------------------------
for tool in xorriso curl tar; do
  command -v "$tool" >/dev/null || { echo "missing: $tool — apt install xorriso curl tar"; exit 1; }
done

VERSION=$(grep -E '^version' pyproject.toml | head -1 | cut -d'"' -f2 || date +%Y%m%d)
DIST="$ROOT/dist"
WORK="$DIST/iso-build"
OUT="$DIST/driveforge-installer-${VERSION}-amd64.iso"

# Pinned Debian 12 netinst — DriveForge targets Bookworm specifically.
# Debian's "current" symlink moves to new majors (now points to 13/Trixie),
# so we pin against the dated archive path to stay on Bookworm. Bump
# DEBIAN_VERSION when a new 12.x point release ships:
#   https://cdimage.debian.org/cdimage/archive/?C=M;O=D
DEBIAN_VERSION="${DEBIAN_VERSION:-12.11.0}"
DEBIAN_ISO_URL="${DEBIAN_ISO_URL:-https://cdimage.debian.org/cdimage/archive/${DEBIAN_VERSION}/amd64/iso-cd/debian-${DEBIAN_VERSION}-amd64-netinst.iso}"
DEBIAN_SHA_URL="https://cdimage.debian.org/cdimage/archive/${DEBIAN_VERSION}/amd64/iso-cd/SHA256SUMS"
DEBIAN_ISO="${DEBIAN_ISO:-$DIST/$(basename "$DEBIAN_ISO_URL")}"

mkdir -p "$DIST"

# --- 1. Get base ISO ----------------------------------------------------------
if [[ ! -f "$DEBIAN_ISO" ]]; then
  echo "==> Downloading $DEBIAN_ISO_URL"
  curl -fL --output "$DEBIAN_ISO" "$DEBIAN_ISO_URL"
fi
# Verify the SHA256 against Debian's published checksums — protects against
# a corrupted download or a man-in-the-middle on this build host.
echo "==> Verifying SHA256"
EXPECTED_SHA=$(curl -fsSL "$DEBIAN_SHA_URL" | awk -v f="$(basename "$DEBIAN_ISO")" '$2==f {print $1}')
if [[ -z "$EXPECTED_SHA" ]]; then
  echo "✗ couldn't fetch expected SHA from $DEBIAN_SHA_URL — refusing to use unverified ISO"
  exit 1
fi
ACTUAL_SHA=$(sha256sum "$DEBIAN_ISO" | cut -d' ' -f1)
if [[ "$EXPECTED_SHA" != "$ACTUAL_SHA" ]]; then
  echo "✗ SHA256 mismatch on $DEBIAN_ISO"
  echo "    expected: $EXPECTED_SHA"
  echo "    actual:   $ACTUAL_SHA"
  echo "    Re-download (rm $DEBIAN_ISO) or check the network path."
  exit 1
fi
echo "==> Base ISO verified: $DEBIAN_ISO ($(du -sh "$DEBIAN_ISO" | cut -f1))"

# --- 2. Build offline bundle (if not already built) ---------------------------
BUNDLE_TGZ="$DIST/driveforge-offline-${VERSION}.tar.gz"
if [[ ! -f "$BUNDLE_TGZ" ]]; then
  if [[ $IN_CONTAINER -eq 1 ]]; then
    echo "==> Building offline bundle (in container, running as root)"
    "$ROOT/scripts/build-offline-bundle.sh"
  else
    echo "==> Building offline bundle (drop to ${SUDO_USER:-nobody} for apt-get download)"
    sudo -u "${SUDO_USER:-nobody}" "$ROOT/scripts/build-offline-bundle.sh"
  fi
fi
echo "==> Offline bundle: $BUNDLE_TGZ ($(du -sh "$BUNDLE_TGZ" | cut -f1))"

# --- 3. Extract ISO -----------------------------------------------------------
echo "==> Extracting base ISO into $WORK"
rm -rf "$WORK"
mkdir -p "$WORK/cd"
xorriso -osirrox on -indev "$DEBIAN_ISO" -extract / "$WORK/cd" >/dev/null 2>&1
chmod -R u+w "$WORK/cd"

# --- 4. Inject preseed --------------------------------------------------------
echo "==> Injecting preseed.cfg"
cp "$ROOT/iso/preseed.cfg" "$WORK/cd/preseed.cfg"

# --- 5. Inject offline bundle as /driveforge-bundle/ on the CD ---------------
echo "==> Injecting offline bundle (extracted) into ISO root"
mkdir -p "$WORK/cd/driveforge-bundle"
tar xzf "$BUNDLE_TGZ" -C "$WORK/cd" --strip-components=1 -C "$WORK/cd/driveforge-bundle" 2>/dev/null || \
  tar xzf "$BUNDLE_TGZ" -C "$WORK/cd/driveforge-bundle" --strip-components=1

# --- 6. Customize boot menu — auto-select preseeded install ------------------
# isolinux/BIOS boot
#
# Strategy: REPLACE Debian's whole isolinux.cfg with a single-entry menu
# that auto-fires our preseeded install. The default ISO has multiple
# config files (isolinux.cfg → menu.cfg → txt.cfg/gtk.cfg/spk.cfg) where
# the speech-synth path triggers automatically after 5 sec of boot-prompt
# inactivity, and the default-selected entry is "Graphical install" which
# has no preseed args. Stripping all that to one entry makes the wrong
# path impossible to hit.
if [[ -d "$WORK/cd/isolinux" ]]; then
  echo "==> Replacing isolinux boot menu (single entry, no speech-synth, auto-fire)"
  # Wipe Debian's includes so they don't override our config.
  rm -f "$WORK/cd/isolinux/menu.cfg" \
        "$WORK/cd/isolinux/txt.cfg" \
        "$WORK/cd/isolinux/gtk.cfg" \
        "$WORK/cd/isolinux/spk.cfg" \
        "$WORK/cd/isolinux/adtxt.cfg" \
        "$WORK/cd/isolinux/adgtk.cfg"
  # No `ui menu.c32` — that module isn't present in every netinst layout
  # (last build hit "Failed to load COM32 file menu.c32" loop). Without a UI
  # module, isolinux drops to a "boot:" prompt and auto-fires `default` after
  # `timeout`. For a single-entry installer that's exactly what we want.
  # `prompt 1` keeps the prompt visible for 5s so the operator can type
  # `rescue` to fall through to the manual path.
  cat > "$WORK/cd/isolinux/isolinux.cfg" <<'EOF'
# Single-entry boot config. After 5 seconds the DriveForge automated
# install fires automatically. Type `rescue` + Enter at the boot prompt
# to drop to a manual Debian installer instead.
default driveforge
prompt 1
timeout 50

say DriveForge installer — auto-firing in 5s. Type `rescue` for manual install.

label driveforge
    kernel /install.amd/vmlinuz
    append vga=788 initrd=/install.amd/initrd.gz auto=true priority=critical preseed/file=/cdrom/preseed.cfg --- quiet

label rescue
    kernel /install.amd/vmlinuz
    append vga=788 initrd=/install.amd/initrd.gz --- quiet
EOF
fi

# UEFI boot (grub)
if [[ -f "$WORK/cd/boot/grub/grub.cfg" ]]; then
  echo "==> Replacing grub boot menu (UEFI, single entry, auto-fire)"
  cat > "$WORK/cd/boot/grub/grub.cfg" <<'EOF'
set timeout=5
set default="0"

menuentry "DriveForge automated install (Debian 12)" {
    set background_color=black
    linux  /install.amd/vmlinuz auto=true priority=critical preseed/file=/cdrom/preseed.cfg quiet
    initrd /install.amd/initrd.gz
}

menuentry "Manual Debian install (no preseed)" {
    set background_color=black
    linux  /install.amd/vmlinuz quiet
    initrd /install.amd/initrd.gz
}
EOF
fi

# --- 7. Recompute md5sum.txt (Debian installer verifies it) ------------------
# Don't `-follow` symlinks — Debian's ISO has a `./debian` symlink that points
# to `.` for mirror-layout backwards compat, and following it sends find into
# an infinite loop. find without -follow processes symlinks as-is, which is
# what we want anyway (md5sum.txt should record the symlink targets, not
# duplicate every file twice through the symlink path).
echo "==> Refreshing md5sum.txt"
( cd "$WORK/cd" && find . -type f ! -name md5sum.txt -print0 \
  | xargs -0 md5sum > md5sum.txt )

# --- 8. Repack into a bootable hybrid ISO ------------------------------------
# Hybrid-MBR boot stub. Comes from the local isolinux package (not from the
# extracted ISO — Debian's netinst doesn't ship this file in iso/isolinux/).
ISOHDPFX="/usr/lib/ISOLINUX/isohdpfx.bin"
[[ -f "$ISOHDPFX" ]] || { echo "✗ missing $ISOHDPFX — install the isolinux package"; exit 1; }

echo "==> Repacking ISO → $OUT"
xorriso -as mkisofs \
  -o "$OUT" \
  -r -V "DriveForge ${VERSION}" \
  -J -joliet-long \
  -isohybrid-mbr "$ISOHDPFX" \
  -c isolinux/boot.cat \
  -b isolinux/isolinux.bin \
    -no-emul-boot -boot-load-size 4 -boot-info-table \
  -eltorito-alt-boot \
  -e boot/grub/efi.img \
    -no-emul-boot -isohybrid-gpt-basdat \
  "$WORK/cd" 2>&1 | tail -10

SIZE=$(du -sh "$OUT" | cut -f1)
echo
echo "✓ ISO built: $OUT ($SIZE)"
echo
echo "  Flash to a USB stick:"
echo "    sudo dd if=$OUT of=/dev/sdX bs=4M status=progress conv=fsync"
echo "  (replace /dev/sdX with your USB device — check 'lsblk' first)"
echo
echo "  Or boot via iDRAC virtual media."
