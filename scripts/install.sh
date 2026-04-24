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

# Defensive: strip any `cdrom:` / `deb cdrom:` entries from apt sources
# before updating. Debian's netinst installer adds the install disc as
# an apt source by default, and in our ISO flow that disc is gone by
# the time install.sh runs — so `apt-get update` errors with "does not
# have a Release file" and `set -euo pipefail` aborts the whole install
# before a single runtime dep lands. The preseed has
# `apt-setup/disable-cdrom-entries boolean true` which prevents this at
# install-time; this is a belt-and-suspenders for re-runs or older ISOs.
if grep -qE '^\s*deb\s+cdrom:' /etc/apt/sources.list 2>/dev/null; then
  warn "Removing stale 'cdrom:' entries from /etc/apt/sources.list"
  sed -i -E '/^\s*deb\s+cdrom:/d' /etc/apt/sources.list
fi
for f in /etc/apt/sources.list.d/*.list; do
  [[ -f "$f" ]] || continue
  if grep -qE '^\s*deb\s+cdrom:' "$f"; then
    warn "Removing stale 'cdrom:' entries from ${f}"
    sed -i -E '/^\s*deb\s+cdrom:/d' "$f"
  fi
done

APT_PACKAGES=(
  python3 python3-venv python3-pip
  smartmontools hdparm sg3-utils nvme-cli e2fsprogs fio
  tmux lshw lsscsi ipmitool avahi-daemon avahi-utils
  # ledmon provides `ledctl` for SGPIO/IBPI LED control on SES-capable
  # backplanes. Expander-only backplanes (e.g. some NX-3200 SKUs) fall
  # back silently — see driveforge/core/blinker.py _try_ledctl().
  ledmon
  fonts-dejavu-core
  # v0.6.1+: policykit-1 is required for v0.6.0's polkit-mediated
  # in-app update path. It is NOT installed by default on Debian 12
  # netinst — a fact that bit v0.6.0. Without it, install.sh's rule
  # drop to /etc/polkit-1/rules.d/ is silently skipped and the
  # in-app update button fails with "Interactive authentication
  # required." Pin it explicitly so every DriveForge host has it.
  policykit-1
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

# Auto-load kernel modules at boot that DriveForge relies on but which
# Debian doesn't always trigger via udev on a fresh install:
#   ses             - binds to SES target devices so /sys/class/enclosure/
#                     populates. Skipped here → SAS-layer fallback, but with
#                     less info and no LED control.
#   ipmi_si         - detects the local BMC via KCS/SMIC/BT interface.
#   ipmi_devintf    - creates /dev/ipmi[0-9]* for userspace tools (ipmitool).
# Harmless no-ops on hardware that doesn't have SES or IPMI.
install -d /etc/modules-load.d
cat > /etc/modules-load.d/driveforge.conf <<'EOF'
# Managed by DriveForge install.sh — regenerated on each run.
ses
ipmi_si
ipmi_devintf
EOF
# Load them now so we don't need a reboot before first discover.
for m in ses ipmi_si ipmi_devintf; do
  modprobe "$m" 2>/dev/null || true
done

# The daemon runs as the unprivileged `driveforge` user, but /dev/ipmi0 is
# created mode 0600 root:root by default — so ipmitool fails for the daemon
# ("could not reach the local BMC"). Install a udev rule that makes
# /dev/ipmi[0-9]* group-readable by the driveforge group, then chmod the
# existing device (if any) so it takes effect without waiting for a reboot.
install -d /etc/udev/rules.d
cat > /etc/udev/rules.d/90-driveforge-ipmi.rules <<'EOF'
# Allow the driveforge daemon to read chassis power / sensor data via IPMI.
KERNEL=="ipmi[0-9]*", MODE="0660", GROUP="driveforge"
EOF
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger --subsystem-match=ipmi 2>/dev/null || true
# Immediate effect for a /dev/ipmi0 that already exists from earlier boot.
for dev in /dev/ipmi[0-9]*; do
  [[ -e "$dev" ]] || continue
  chgrp driveforge "$dev" 2>/dev/null || true
  chmod 0660 "$dev" 2>/dev/null || true
done

# v0.6.1+: Brother QL-series USB label printers (VID 0x04f9) are
# created root:lp mode 0664 by default, and the daemon isn't in the
# `lp` group (nor should it be — that'd grant access to all lp
# devices indiscriminately). This rule grants the `driveforge` group
# access to the one VID we care about so brother_ql's pyusb backend
# can send raster data without the daemon running as root.
cat > /etc/udev/rules.d/90-driveforge-brother-usb.rules <<'EOF'
# Brother QL-series label printers — access for the driveforge daemon.
SUBSYSTEM=="usb", ATTRS{idVendor}=="04f9", MODE="0660", GROUP="driveforge"
EOF
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger --subsystem-match=usb --attr-match=idVendor=04f9 2>/dev/null || true

# v0.6.9+: mask Debian's stock /lib/udev/rules.d/85-hdparm.rules.
#
# Debian's `hdparm` package ships a udev rule that fires
# /lib/udev/hdparm on every ACTION=="add" for sd[a-z] block devices.
# That helper runs /usr/lib/pm-utils/power.d/95hdparm-apm which issues
# `hdparm -B<N>` per the hdparm-functions defaults — even when
# /etc/hdparm.conf has no per-drive overrides.
#
# On a DriveForge appliance that's a hostile interaction: when the
# target drive is mid-SECURITY-ERASE-UNIT it cannot answer APM
# commands, so the `hdparm -B254` issued by the Debian rule goes
# D-state waiting on a response the drive will never give. Udev
# re-enumeration transitions during an erase can fire the rule
# repeatedly, stacking multiple D-state `hdparm -B254` processes on
# the same drive. Combined with the daemon's own `sg_raw` + `smartctl`
# against the same drive, the HBA's SG queue fills and the
# `kworker/*+fw_event_mpt2sas*` kthread goes D-state processing
# events it can no longer drain — at which point NEW drive insertions
# on the same HBA never get `/dev` nodes (observed on R720 v0.6.6
# with an active WDC WD1000CHTZ secure_erase, 2026-04-21).
#
# Fix: symlink /etc/udev/rules.d/85-hdparm.rules to /dev/null. Udev
# reads files in /etc/udev/rules.d with the same name as files in
# /lib/udev/rules.d AT HIGHER PRIORITY; a /dev/null symlink at that
# path is the standard Debian idiom for "disable this vendor-shipped
# rule." DriveForge never relies on boot-time APM/spindown anyway;
# it manages drives explicitly per pipeline phase.
if [[ ! -L /etc/udev/rules.d/85-hdparm.rules ]]; then
  ln -sf /dev/null /etc/udev/rules.d/85-hdparm.rules
  ok "masked Debian hdparm udev rule (prevents hdparm -B254 pileup during secure_erase)"
fi
udevadm control --reload-rules 2>/dev/null || true

# `ledmon` ships with a systemd daemon that auto-starts and polls for
# RAID rebuild events to drive LEDs. DriveForge calls `ledctl` directly
# on state transitions (pass/fail/blinker), so the daemon is redundant
# and just adds background CPU. Disable if installed.
if systemctl list-unit-files ledmon.service >/dev/null 2>&1; then
  systemctl disable --now ledmon.service >/dev/null 2>&1 || true
fi

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

# v0.11.0+ — ISO boot menu can seed the initial role via kernel
# cmdline. The "DriveForge Agent" boot entry passes
# `driveforge.initial_role=candidate` so install.sh writes a
# minimal driveforge.yaml skipping the setup wizard, and the
# daemon boots directly into candidate mode (advertises itself
# via mDNS, waits for operator adoption).
#
# Only applies on first install (no existing yaml); in-app updates
# leave the existing config untouched.
if [[ ! -f /etc/driveforge/driveforge.yaml ]]; then
  initial_role=""
  if [[ -r /proc/cmdline ]]; then
    for tok in $(cat /proc/cmdline); do
      case "$tok" in
        driveforge.initial_role=*) initial_role="${tok#driveforge.initial_role=}" ;;
      esac
    done
  fi
  if [[ "$initial_role" == "candidate" ]]; then
    log "kernel cmdline set driveforge.initial_role=candidate — seeding candidate config"
    cat > /etc/driveforge/driveforge.yaml <<'YAMLEOF'
# Seeded by install.sh from ISO boot menu (DriveForge Agent entry).
# This box will advertise itself via mDNS until an operator on the
# LAN adopts it via Settings → Agents → Discovered.
setup_completed: true
fleet:
  role: candidate
YAMLEOF
    chown driveforge:driveforge /etc/driveforge/driveforge.yaml
    chmod 0644 /etc/driveforge/driveforge.yaml
    ok "candidate config seeded"
  fi
fi

ok "defaults written to /etc/driveforge/"

# ---------------------------------------------------------------- hostname
# v0.10.0+: uniquify the hostname on first install so multiple DriveForge
# boxes on the same LAN don't all claim "driveforge.local" via mDNS. avahi
# has runtime collision detection but auto-suffixes with no operator
# visibility; better to pick a stable, collision-free name up front.
#
# Strategy:
#   - Only run when the hostname is still the preseed default
#     ("driveforge") AND we haven't done this before (marker file).
#   - Derive a 6-hex suffix from the primary NIC's MAC address. Stable
#     across reinstalls of the same hardware (same MAC), unique across
#     boxes (different MAC).
#   - Update /etc/hostname, /etc/hosts, and run `hostnamectl` so
#     avahi picks up the change immediately.
#   - Operator can always override via Settings → Hostname UI later;
#     the marker file ensures we don't undo their rename on future
#     install.sh re-runs (in-app updates call install.sh).
HOSTNAME_UNIQ_MARKER=/var/lib/driveforge/.hostname-uniquified
# v0.11.0+ — read /etc/hostname directly instead of calling the
# `hostname` command. Inside Debian installer's in-target chroot,
# `hostname` returns the installer-environment name (usually
# localhost or the d-i runtime's name), NOT the target's configured
# hostname, so the guard check never matched and the uniquifier
# never fired at install time. Only subsequent in-app updates
# (running in the live system) would catch it, which explained
# why fresh ISO installs were shipping out as `driveforge.local`
# despite this block existing since v0.10.0.
preseed_hostname="$(cat /etc/hostname 2>/dev/null | tr -d '[:space:]')"
if [[ "$preseed_hostname" == "driveforge" ]] && [[ ! -f "$HOSTNAME_UNIQ_MARKER" ]]; then
  log "Uniquifying hostname (avoids driveforge.local mDNS collisions)..."
  # Pick the iface on the default route. Fallback to first /sys entry that
  # isn't lo or a virtual bridge if `ip route` yields nothing yet
  # (install.sh may run before first DHCP lease on some images).
  primary_iface=""
  if command -v ip >/dev/null 2>&1; then
    primary_iface="$(ip -o route get 1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="dev") print $(i+1); exit}')"
  fi
  if [[ -z "$primary_iface" ]]; then
    for iface in /sys/class/net/*; do
      name="$(basename "$iface")"
      [[ "$name" == "lo" ]] && continue
      [[ -d "$iface/bridge" ]] && continue
      [[ -d "$iface/bonding" ]] && continue
      primary_iface="$name"
      break
    done
  fi
  mac_suffix=""
  if [[ -n "$primary_iface" && -r "/sys/class/net/$primary_iface/address" ]]; then
    mac_suffix="$(tr -d ':' < "/sys/class/net/$primary_iface/address" | tail -c 7 | head -c 6)"
  fi
  if [[ -n "$mac_suffix" ]]; then
    new_hostname="driveforge-${mac_suffix}"
    log "  → ${new_hostname}"
    # v0.11.0+ — write /etc/hostname directly as the source of
    # truth. `hostnamectl` may not work inside the Debian installer's
    # in-target chroot (no running systemd-hostnamed on the installer
    # side); direct-write is authoritative on first boot regardless.
    # Try hostnamectl for live-system paths (in-app update) where
    # it DOES work and pokes the kernel hostname immediately.
    echo "$new_hostname" > /etc/hostname
    hostnamectl set-hostname "$new_hostname" 2>/dev/null || true
    # Patch /etc/hosts so local resolution matches. Debian's preseed usually
    # installs `127.0.1.1 driveforge`; replace that exact line.
    if grep -qE '^127\.0\.1\.1\s+driveforge\b' /etc/hosts; then
      sed -i -E "s/^(127\.0\.1\.1\s+)driveforge\b/\1${new_hostname}/" /etc/hosts
    elif ! grep -qE "^127\.0\.1\.1\s+${new_hostname}\b" /etc/hosts; then
      echo "127.0.1.1	${new_hostname}" >> /etc/hosts
    fi
    # avahi caches the system hostname at daemon start. `reload`
    # sends SIGHUP which re-reads avahi-daemon.conf but NOT the
    # kernel hostname — so the advertised name stays stale. v0.11.0
    # uses `restart` unconditionally. Fire-and-forget: if avahi
    # isn't running yet (pre-boot, install-time chroot), we skip
    # cleanly and the first boot picks up the right name naturally.
    if systemctl is-active --quiet avahi-daemon 2>/dev/null; then
      systemctl restart avahi-daemon 2>/dev/null || true
    fi
    touch "$HOSTNAME_UNIQ_MARKER"
    ok "hostname set to ${new_hostname}"
  else
    warn "couldn't derive MAC suffix — leaving hostname as 'driveforge'"
    warn "rename it in Settings → Hostname if you're running more than one DriveForge box on this LAN"
  fi
fi

# v0.10.8+ — /etc/hosts canonicalization. Runs on every install.sh
# invocation (install AND in-app update). The daemon's Python self-heal
# can't fix drifted 127.0.1.1 lines because /etc/hosts is root-owned
# 644 and the daemon runs as `driveforge`; systemd ReadWritePaths
# grants namespace access, not DAC override. install.sh runs as root
# so the fix lands cleanly here.
#
# Drift sources observed: v0.10.0's uniquifier sed left extra tokens
# on boxes that already had `.local` aliases appended by some earlier
# rename flow (NX-3200 ended up with
# `127.0.1.1 driveforge-44242c.local driveforge` which breaks sudo).
current_hostname="$(hostname 2>/dev/null || echo "")"
if [[ -n "$current_hostname" ]] && [[ -w /etc/hosts ]]; then
  # Canonical Debian form: single token on the 127.0.1.1 line matching
  # the current short hostname. avahi handles the `.local` alias via
  # mDNS; it does NOT belong in /etc/hosts.
  canonical_re="^127\.0\.1\.1[[:space:]]+${current_hostname}[[:space:]]*\$"
  if ! grep -qE "$canonical_re" /etc/hosts; then
    log "canonicalizing /etc/hosts 127.0.1.1 entry to '${current_hostname}'"
    # Remove any existing 127.0.1.1 line(s), then append the canonical
    # one. Preserves every other line.
    sed -i -E '/^127\.0\.1\.1[[:space:]]/d' /etc/hosts
    printf "127.0.1.1\t%s\n" "$current_hostname" >> /etc/hosts
    ok "/etc/hosts canonicalized"
  fi
fi

log "Installing systemd units..."
install -m 0644 "$(dirname "$0")/../systemd/driveforge-daemon.service" /etc/systemd/system/
install -m 0644 "$(dirname "$0")/../systemd/driveforge-tui.service" /etc/systemd/system/
install -m 0644 "$(dirname "$0")/../systemd/driveforge-issue.service" /etc/systemd/system/
install -m 0644 "$(dirname "$0")/../systemd/driveforge-update.service" /etc/systemd/system/
# v0.6.9+: on-demand systemd-udevd restart unit. Paired with the
# polkit rule installed below; triggered by the dashboard's
# "Restart udev" button when the udev-health detector reports
# the pipeline is stalled. See driveforge/core/udev_health.py.
install -m 0644 "$(dirname "$0")/../systemd/driveforge-udev-restart.service" /etc/systemd/system/
install -m 0755 "$(dirname "$0")/../scripts/driveforge-update-issue.sh" /usr/local/sbin/driveforge-update-issue
install -m 0755 "$(dirname "$0")/../scripts/driveforge-update" /usr/local/sbin/driveforge-update

# Pre-create the update log file so the daemon (running as user
# `driveforge`) can read it for live-tail streaming on the dashboard.
# The systemd unit appends as root; we just need read perms for the
# daemon group. Group-readable; no world-readable since the log can
# include git URLs / package versions some operators consider noisy
# but not secret.
touch /var/log/driveforge-update.log
chgrp driveforge /var/log/driveforge-update.log 2>/dev/null || true
chmod 0640 /var/log/driveforge-update.log

# v0.6.0+: install the polkit rule that grants the `driveforge`
# daemon user permission to call StartUnit on driveforge-update.service
# via systemd's D-Bus interface. Replaces the pre-v0.6.0 sudoers rule,
# which suffered from occasional 10-second PAM/reverse-DNS timeouts.
# See config/driveforge-update.polkit-rules for the scope + reasoning.
#
# Policykit-1 is installed by default on every Debian 12 netinst that
# boots via systemd, so we can rely on /etc/polkit-1/rules.d/ existing.
# The directory check is defensive for offline-bundle paths that
# deselected polkit explicitly.
if [[ -d /etc/polkit-1/rules.d ]]; then
  install -m 0644 "$(dirname "$0")/../config/driveforge-update.polkit-rules" \
    /etc/polkit-1/rules.d/50-driveforge-update.rules
  ok "polkit rule installed (allows daemon → start update unit only)"
  # v0.6.9+: second polkit rule for the udev-restart helper unit.
  # Narrowly scoped to StartUnit on driveforge-udev-restart.service
  # for the daemon user. See config/driveforge-udev-restart.polkit-rules
  # for the scope + reasoning.
  install -m 0644 "$(dirname "$0")/../config/driveforge-udev-restart.polkit-rules" \
    /etc/polkit-1/rules.d/51-driveforge-udev-restart.rules
  ok "polkit rule installed (allows daemon → start udev-restart unit only)"
  # polkit re-reads rules on file change via inotify, so no explicit
  # reload is required. systemctl --user daemon-reload is NOT what
  # reloads polkit rules.
else
  warn "/etc/polkit-1/rules.d not found — is polkit (policykit-1) installed?"
  warn "Skipping polkit rule install; in-app update will be unavailable until fixed"
fi

# Upgrade hygiene: if an older install left the sudoers rule in place,
# remove it. The new polkit path makes it redundant, and keeping a
# stale sudoers rule around just muddles the audit trail for anyone
# inspecting /etc/sudoers.d/ on an upgraded host.
if [[ -f /etc/sudoers.d/driveforge-update ]]; then
  rm -f /etc/sudoers.d/driveforge-update
  ok "removed legacy /etc/sudoers.d/driveforge-update (superseded by polkit)"
fi

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
