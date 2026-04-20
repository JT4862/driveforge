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
  apt-get install -y --no-download --fix-broken || \
    warn "apt-get --fix-broken reported errors"
else
  apt-get update -qq
  apt-get install -y --no-install-recommends "${APT_PACKAGES[@]}" >/dev/null
fi
# Post-install sanity check — the specific failure we hit was `python3`
# missing even though setup claimed success, because errors were being
# silently swallowed. A loud check here fails the install at a useful spot
# instead of crashing later at `python3 -m venv`.
for bin in python3 apt-get useradd systemctl; do
  command -v "$bin" >/dev/null || die "required binary '$bin' not on PATH after apt install; check the apt/dpkg log above"
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
# Offline-bundle path: the ISO installer's late_command sets
# DRIVEFORGE_OFFLINE_BUNDLE so pip resolves from the bundled wheels cache
# instead of pypi.org.
PIP_OFFLINE_ARGS=()
if [[ -n "${DRIVEFORGE_OFFLINE_BUNDLE:-}" && -d "${DRIVEFORGE_OFFLINE_BUNDLE}/wheels" ]]; then
  log "Using offline wheel cache at ${DRIVEFORGE_OFFLINE_BUNDLE}/wheels"
  PIP_OFFLINE_ARGS=(--no-index --find-links "${DRIVEFORGE_OFFLINE_BUNDLE}/wheels")
fi
# On Linux we need the `linux` extra — it pulls in pyudev for the hotplug
# event monitor. Without this, the daemon boots fine but never sees drive
# add/remove events and the LED-blinker-on-reinsert feature silently
# no-ops. `--upgrade` so re-running install.sh after deps change actually
# refreshes them instead of pip concluding "driveforge X.Y.Z already
# present, skip" on an unchanged version string.
if [[ -f "$SRC_DIR/pyproject.toml" ]]; then
  pip install --quiet --upgrade "${PIP_OFFLINE_ARGS[@]}" "$SRC_DIR[linux]"
else
  pip install --quiet --upgrade "${PIP_OFFLINE_ARGS[@]}" "driveforge[linux]"
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
