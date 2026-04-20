#!/usr/bin/env bash
# DriveForge bootstrap installer.
#
# Intended usage:
#   curl -sSL https://raw.githubusercontent.com/JT4862/driveforge/main/scripts/install.sh | sudo bash
#
# Requires Debian 12 + root. Installs system deps, the driveforge Python
# package, systemd units, and a default config. No interactive prompts.

set -euo pipefail

RED=$'\033[0;31m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[0;33m'
BLUE=$'\033[0;34m'
RESET=$'\033[0m'

log() { echo "${BLUE}==>${RESET} $*"; }
ok()  { echo "${GREEN}✓${RESET} $*"; }
warn(){ echo "${YELLOW}⚠${RESET}  $*"; }
die() { echo "${RED}✗${RESET} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "must run as root (try: curl ... | sudo bash)"

# Debian installer's in-target chroot invokes us with a minimal PATH (often
# just /sbin:/bin), so /usr/bin/python3, /usr/sbin/useradd, etc. aren't
# resolvable. Prepend the standard Debian search path so the offline / ISO
# late_command path matches what a user's interactive sudo session would see.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin${PATH:+:$PATH}"

log "Checking Debian version..."
if ! grep -q '^VERSION_ID="12"' /etc/os-release 2>/dev/null; then
  warn "Not Debian 12. DriveForge targets Debian 12; other distros are unsupported."
fi

log "Installing system dependencies..."
export DEBIAN_FRONTEND=noninteractive
APT_PACKAGES=(
  python3 python3-venv python3-pip
  smartmontools hdparm sg3-utils nvme-cli e2fsprogs fio
  tmux lshw lsscsi ipmitool avahi-daemon avahi-utils
  fonts-dejavu-core
  curl ca-certificates
)
if [[ -n "${DRIVEFORGE_OFFLINE_BUNDLE:-}" && -d "${DRIVEFORGE_OFFLINE_BUNDLE}/debs" ]]; then
  # Air-gapped install — debs were pre-downloaded by build-offline-bundle.sh
  # on an internet-connected machine.
  log "Using offline .deb cache at ${DRIVEFORGE_OFFLINE_BUNDLE}/debs"
  # Iterate dpkg -i up to 3 passes — a single pass installs in shell-glob
  # alphabetical order, which can fail when a package's deps haven't been
  # processed yet (e.g. `python3` needs `python3-minimal` and `libpython3.11-*`
  # first). Subsequent passes pick up the ones that failed the previous time
  # now that their deps are satisfied. Three passes cover any normal dep
  # chain depth; we log the outcome of each so the install log actually
  # shows what happened instead of swallowing errors.
  for pass in 1 2 3; do
    log "dpkg -i pass $pass..."
    if dpkg -i "${DRIVEFORGE_OFFLINE_BUNDLE}"/debs/*.deb; then
      ok "all .debs installed on pass $pass"
      break
    fi
    if [[ $pass -eq 3 ]]; then
      warn "dpkg -i still reporting errors after 3 passes — check the log above"
    fi
  done
  # Fix-broken pass to resolve anything still hanging. Use cached debs only.
  # --no-remove is critical: without it, apt's broken-state resolver will
  # silently REMOVE packages (including the kernel!) if it can't otherwise
  # satisfy dependencies from the cached .debs. ISO #12 and #13 both hit
  # exactly this: udev version skew in the bundle caused apt to "fix" the
  # broken state by removing linux-image-amd64, initramfs-tools, and udev,
  # leaving /boot/vmlinuz missing and the VM booting to a grub> prompt.
  # Better to fail loud here than produce an unbootable install.
  apt-get install -y --no-download --fix-broken --no-remove || \
    die "apt-get --fix-broken failed — the offline bundle has a dependency conflict that apt refuses to resolve without removing packages. Rebuild the bundle with matching versions."
else
  apt-get update -qq
  apt-get install -y --no-install-recommends "${APT_PACKAGES[@]}" >/dev/null
fi
# Post-install sanity check — catches two classes of silent failure:
# missing binaries (python3 / apt-get / useradd / systemctl) from a busted
# apt step, and a missing kernel image from the fix-broken-removed-the-kernel
# scenario. Either way: fail now with a clear pointer to the log, instead
# of producing a system that only fails on first reboot.
for bin in python3 apt-get useradd systemctl; do
  command -v "$bin" >/dev/null || die "required binary '$bin' not on PATH after apt install; check the apt/dpkg log above"
done
if ! ls /boot/vmlinuz-* >/dev/null 2>&1 && ! ls /boot/vmlinuz >/dev/null 2>&1; then
  die "no kernel image in /boot/ — the apt step must have removed linux-image-amd64. Check the install log for 'Removing linux-image-*' lines and rebuild the offline bundle."
fi
ok "system packages installed"

log "Creating driveforge user and directories..."
if id -u driveforge >/dev/null 2>&1; then
  EXISTING_UID=$(id -u driveforge)
  EXISTING_SHELL=$(getent passwd driveforge | cut -d: -f7)
  if [[ $EXISTING_UID -ge 1000 ]] || [[ "$EXISTING_SHELL" != "/usr/sbin/nologin" && "$EXISTING_SHELL" != "/bin/false" ]]; then
    warn "The 'driveforge' user already exists as a login account (UID $EXISTING_UID, shell $EXISTING_SHELL)."
    warn "The daemon will run as this account. For a cleaner service/admin"
    warn "boundary, use a different login name and reinstall."
  fi
else
  useradd -r -s /usr/sbin/nologin -d /var/lib/driveforge driveforge
fi
# Grant access to raw block + SCSI-generic devices. `disk` covers /dev/sdX
# and /dev/nvme*; `cdrom` covers /dev/sg* on most Debian setups. The daemon
# needs these to open devices for smartctl, hdparm, sg_format, nvme-cli,
# and badblocks. CAP_SYS_RAWIO alone is not enough — open() is gated by
# filesystem permissions before capabilities apply.
usermod -a -G disk,cdrom driveforge
install -d -o driveforge -g driveforge -m 0755 /var/lib/driveforge /var/log/driveforge
# Daemon needs to write to /etc/driveforge/ when the user saves settings in
# the UI — owned by the driveforge user, not root.
install -d -o driveforge -g driveforge -m 0755 /etc/driveforge

# Safety: if a previous install left a DB in place, preserve it. A fresh
# install.sh re-run should never clobber test history.
if [[ -f /var/lib/driveforge/driveforge.db ]]; then
  warn "Existing DB found at /var/lib/driveforge/driveforge.db — preserving."
fi
ok "user + dirs ready"

log "Installing DriveForge Python package..."
python3 -m venv /opt/driveforge
# shellcheck disable=SC1091
source /opt/driveforge/bin/activate
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Two install modes:
#
#   Offline (ISO bundle path): DRIVEFORGE_OFFLINE_BUNDLE is set and contains
#     pre-built wheels (including a driveforge-*.whl produced by
#     build-offline-bundle.sh's `pip wheel` step). Install by PACKAGE NAME
#     from the wheels cache so pip picks the prebuilt wheel directly —
#     critically, never tries to rebuild from an sdist. A rebuild would
#     need hatchling (DriveForge's PEP 517 build backend), which isn't
#     in the bundle because build backends don't get packaged by
#     `pip wheel`. Hit this on 2026-04-20 with the "No matching
#     distribution found for hatchling" error.
#
#   Online (direct clone path): no offline bundle, internet available.
#     Install from the local source directory; pip resolves hatchling
#     from PyPI at build time.
if [[ -n "${DRIVEFORGE_OFFLINE_BUNDLE:-}" && -d "${DRIVEFORGE_OFFLINE_BUNDLE}/wheels" ]]; then
  log "Using offline wheel cache at ${DRIVEFORGE_OFFLINE_BUNDLE}/wheels"
  # By-name install from the wheels dir — pip finds driveforge-*.whl
  # and installs it directly, no build step, no backend needed.
  pip install --quiet --upgrade \
    --no-index --find-links "${DRIVEFORGE_OFFLINE_BUNDLE}/wheels" \
    "driveforge[linux]"
else
  # Online path. `--upgrade` so re-running install.sh on a drift-free
  # version string still refreshes deps that may have changed.
  pip install --quiet --upgrade "$SRC_DIR[linux]"
fi
deactivate
ln -sf /opt/driveforge/bin/driveforge-daemon /usr/bin/driveforge-daemon
ln -sf /opt/driveforge/bin/driveforge-tui /usr/bin/driveforge-tui
ln -sf /opt/driveforge/bin/driveforge /usr/bin/driveforge
ok "package installed"

log "Installing default config..."
[[ -f /etc/driveforge/grading.yaml ]] || install -m 0644 \
  "$(dirname "$0")/../config/grading.yaml.example" /etc/driveforge/grading.yaml
ok "defaults written to /etc/driveforge/"

log "Installing systemd units..."
install -m 0644 "$(dirname "$0")/../systemd/driveforge-daemon.service" /etc/systemd/system/
install -m 0644 "$(dirname "$0")/../systemd/driveforge-tui.service" /etc/systemd/system/
systemctl daemon-reload
# Avahi usually autostarts on Debian but enable explicitly so
# driveforge.local is reachable on first boot.
systemctl enable --now avahi-daemon.service >/dev/null 2>&1 || true
systemctl enable driveforge-daemon.service
# Restart (not just start) so re-running install.sh after a code update
# picks up the new package instead of keeping the old daemon in memory.
systemctl restart driveforge-daemon.service
ok "driveforge-daemon running"

# Detect primary IP + DHCP status for the access summary
SRV_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") print $(i+1)}')
SRV_IP=${SRV_IP:-$(hostname -I | awk '{print $1}')}
SRV_IP=${SRV_IP:-0.0.0.0}

DHCP_ACTIVE="no"
if command -v nmcli >/dev/null 2>&1; then
  if nmcli -t -f IP4.GATEWAY,IP4.DNS,IP4.DHCP4.OPTION device show 2>/dev/null | grep -q 'IP4.DHCP4'; then
    DHCP_ACTIVE="yes"
  fi
elif [[ -d /run/systemd/netif/leases ]] && compgen -G "/run/systemd/netif/leases/*" > /dev/null; then
  DHCP_ACTIVE="yes"
fi

echo
echo "${GREEN}✓${RESET} DriveForge installed and running."
echo
echo "Open the web UI at:"
echo "  → http://driveforge.local:8080     (mDNS, preferred)"
echo "  → http://${SRV_IP}:8080            (direct IP)"
if [[ "$DHCP_ACTIVE" == "yes" ]]; then
  echo
  echo "${YELLOW}⚠${RESET}  This server appears to be on DHCP — the IP may change on reboot."
  echo "   For a stable URL, set a static IP via Debian's netplan config."
fi
