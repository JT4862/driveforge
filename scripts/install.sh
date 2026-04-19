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

log "Checking Debian version..."
if ! grep -q '^VERSION_ID="12"' /etc/os-release 2>/dev/null; then
  warn "Not Debian 12. DriveForge targets Debian 12; other distros are unsupported."
fi

log "Installing system dependencies..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip \
  smartmontools hdparm sg3-utils nvme-cli e2fsprogs fio \
  tmux lshw lsscsi ipmitool avahi-daemon avahi-utils \
  curl ca-certificates >/dev/null
ok "system packages installed"

log "Creating driveforge user and directories..."
id -u driveforge >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin -d /var/lib/driveforge driveforge
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
# In a real release this grabs the wheel/.deb from a GitHub release. For now,
# the installer assumes the package is checked out locally adjacent to this
# script, or that `driveforge` is already on PYTHONPATH.
python3 -m venv /opt/driveforge
# shellcheck disable=SC1091
source /opt/driveforge/bin/activate
if [[ -f "$(dirname "$0")/../pyproject.toml" ]]; then
  pip install --quiet "$(dirname "$0")/.."
else
  pip install --quiet driveforge
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
systemctl enable --now driveforge-daemon.service
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
