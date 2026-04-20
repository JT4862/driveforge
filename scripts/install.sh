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
# Normal path uses Debian's mirrors. Air-gapped path (DRIVEFORGE_OFFLINE_BUNDLE
# set) points apt at the local .debs repo pre-built by build-offline-bundle.sh —
# see INSTALL.md Path C for the full flow.
if [[ -n "${DRIVEFORGE_OFFLINE_BUNDLE:-}" && -d "${DRIVEFORGE_OFFLINE_BUNDLE}/debs" ]]; then
  log "Using offline .deb cache at ${DRIVEFORGE_OFFLINE_BUNDLE}/debs"
  cat > /etc/apt/sources.list.d/driveforge-offline.list <<EOF
deb [trusted=yes] file://${DRIVEFORGE_OFFLINE_BUNDLE}/debs ./
EOF
  # Update apt's index from ONLY our local repo — override the default
  # sourcelist + sourceparts so apt doesn't try Debian mirrors.
  apt-get -o Dir::Etc::sourceparts=- \
          -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/driveforge-offline.list \
          -o APT::Get::List-Cleanup=0 \
          update 2>&1 | tail -3
  apt-get install -y --no-install-recommends "${APT_PACKAGES[@]}"
  rm -f /etc/apt/sources.list.d/driveforge-offline.list
else
  apt-get update -qq
  apt-get install -y --no-install-recommends "${APT_PACKAGES[@]}"
fi
# Post-install sanity check — fail loud if a critical binary is missing
# after apt instead of crashing two sections later at e.g. `python3 -m venv`.
for bin in python3 apt-get useradd systemctl; do
  command -v "$bin" >/dev/null || die "required binary '$bin' not on PATH after apt install; check the apt log above"
done
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
# Normal path: install from the local source tree + PyPI for deps
# (hatchling build backend resolves from PyPI automatically). Air-gap
# path (DRIVEFORGE_OFFLINE_BUNDLE set): install the pre-built
# driveforge wheel from the bundle's wheels/ dir by name so pip picks
# the .whl directly and doesn't try to rebuild (which would need
# hatchling from PyPI we can't reach). `[linux]` extra pulls pyudev
# for the hotplug monitor.
if [[ -n "${DRIVEFORGE_OFFLINE_BUNDLE:-}" && -d "${DRIVEFORGE_OFFLINE_BUNDLE}/wheels" ]]; then
  pip install --quiet --upgrade \
    --no-index --find-links "${DRIVEFORGE_OFFLINE_BUNDLE}/wheels" \
    "driveforge[linux]"
else
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
install -m 0644 "$(dirname "$0")/../systemd/driveforge-issue.service" /etc/systemd/system/
install -m 0755 "$(dirname "$0")/../scripts/driveforge-update-issue.sh" /usr/local/sbin/driveforge-update-issue
systemctl daemon-reload
# Avahi usually autostarts on Debian but enable explicitly so
# driveforge.local is reachable on first boot.
systemctl enable --now avahi-daemon.service >/dev/null 2>&1 || true
systemctl enable driveforge-daemon.service
# Refresh /etc/issue on every boot with the current IP + dashboard URL so
# the TTY login banner shows where to point a browser (Proxmox-style).
systemctl enable driveforge-issue.service
# Run it once now so the banner is right on first login BEFORE a reboot.
/usr/local/sbin/driveforge-update-issue || true
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
