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

[[ $EUID -eq 0 ]] || { echo "this script needs root (loopback mount + xorriso)"; exit 1; }

# --- Tools --------------------------------------------------------------------
for tool in xorriso curl tar; do
  command -v "$tool" >/dev/null || { echo "missing: $tool — apt install xorriso curl tar"; exit 1; }
done

VERSION=$(grep -E '^version' pyproject.toml | head -1 | cut -d'"' -f2 || date +%Y%m%d)
DIST="$ROOT/dist"
WORK="$DIST/iso-build"
OUT="$DIST/driveforge-installer-${VERSION}-amd64.iso"

# Pinned Debian netinst version. Bump on Debian point releases; see
# https://www.debian.org/CD/netinst/ for current.
DEBIAN_ISO_URL="${DEBIAN_ISO_URL:-https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/debian-12.7.0-amd64-netinst.iso}"
DEBIAN_ISO="${DEBIAN_ISO:-$DIST/$(basename "$DEBIAN_ISO_URL")}"

mkdir -p "$DIST"

# --- 1. Get base ISO ----------------------------------------------------------
if [[ ! -f "$DEBIAN_ISO" ]]; then
  echo "==> Downloading $DEBIAN_ISO_URL"
  curl -fL --output "$DEBIAN_ISO" "$DEBIAN_ISO_URL"
fi
echo "==> Base ISO: $DEBIAN_ISO ($(du -sh "$DEBIAN_ISO" | cut -f1))"

# --- 2. Build offline bundle (if not already built) ---------------------------
BUNDLE_TGZ="$DIST/driveforge-offline-${VERSION}.tar.gz"
if [[ ! -f "$BUNDLE_TGZ" ]]; then
  echo "==> Building offline bundle (run as your normal user — apt-get download needs cache)"
  sudo -u "${SUDO_USER:-nobody}" "$ROOT/scripts/build-offline-bundle.sh"
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
if [[ -f "$WORK/cd/isolinux/isolinux.cfg" ]]; then
  echo "==> Patching isolinux boot menu"
  cat > "$WORK/cd/isolinux/txt.cfg" <<'EOF'
default driveforge
label driveforge
        menu label ^DriveForge automated install (recommended)
        kernel /install.amd/vmlinuz
        append vga=788 initrd=/install.amd/initrd.gz auto=true priority=critical preseed/file=/cdrom/preseed.cfg --- quiet
label install
        menu label ^Manual Debian install
        kernel /install.amd/vmlinuz
        append vga=788 initrd=/install.amd/initrd.gz --- quiet
EOF
  # Force auto-select after 5 sec
  sed -i 's/^timeout .*/timeout 50/' "$WORK/cd/isolinux/isolinux.cfg" 2>/dev/null || true
fi

# UEFI boot (grub)
if [[ -f "$WORK/cd/boot/grub/grub.cfg" ]]; then
  echo "==> Patching grub boot menu (UEFI)"
  cat > "$WORK/cd/boot/grub/grub.cfg" <<'EOF'
set timeout=5
set default="0"

menuentry "DriveForge automated install (recommended)" {
    set background_color=black
    linux  /install.amd/vmlinuz auto=true priority=critical preseed/file=/cdrom/preseed.cfg quiet
    initrd /install.amd/initrd.gz
}
menuentry "Manual Debian install" {
    set background_color=black
    linux  /install.amd/vmlinuz quiet
    initrd /install.amd/initrd.gz
}
EOF
fi

# --- 7. Recompute md5sum.txt (Debian installer verifies it) ------------------
echo "==> Refreshing md5sum.txt"
( cd "$WORK/cd" && find . -follow -type f ! -name md5sum.txt -print0 \
  | xargs -0 md5sum > md5sum.txt )

# --- 8. Repack into a bootable hybrid ISO ------------------------------------
echo "==> Repacking ISO → $OUT"
xorriso -as mkisofs \
  -o "$OUT" \
  -r -V "DriveForge ${VERSION}" \
  -J -joliet-long \
  -isohybrid-mbr "$WORK/cd/isolinux/isohdpfx.bin" \
  -c isolinux/boot.cat \
  -b isolinux/isolinux.bin \
    -no-emul-boot -boot-load-size 4 -boot-info-table \
  -eltorito-alt-boot \
  -e boot/grub/efi.img \
    -no-emul-boot -isohybrid-gpt-basdat \
  "$WORK/cd" 2>&1 | tail -5

SIZE=$(du -sh "$OUT" | cut -f1)
echo
echo "✓ ISO built: $OUT ($SIZE)"
echo
echo "  Flash to a USB stick:"
echo "    sudo dd if=$OUT of=/dev/sdX bs=4M status=progress conv=fsync"
echo "  (replace /dev/sdX with your USB device — check 'lsblk' first)"
echo
echo "  Or boot via iDRAC virtual media."
