---
title: Manual install
---

# Manual install (existing Debian 12 host)

Use this path if you already have a Debian 12 box and don't want to
reinstall the OS just for DriveForge — typical for homelab operators
who already have a server provisioned the way they like it.

The end state is identical to the [ISO install](iso-install.md):
`driveforge-daemon` running as the `driveforge` user, dashboard at
`http://<host>.local:8080`. The only difference is the OS install +
preseed wizard pieces are skipped.

## Prerequisites

- **Debian 12 (bookworm)**. Not Debian 11; not Ubuntu (would probably
  work but isn't tested). The version check in `install.sh` warns on
  non-Debian-12 hosts but doesn't refuse.
- **Root access** (or `sudo`).
- **Internet egress** for `apt` and `pip` to fetch dependencies. For
  air-gapped hosts, see the [air-gapped install](air-gapped.md).
- **An IT-mode SAS HBA** (if you're using one — direct motherboard
  SATA also works). See [supported HBAs](../hardware/supported-hbas.md).
- **A separate boot drive** that's NOT in any front bay you'll test.
  Booting from a test bay is unsafe — the front bays are
  drive-erasure territory.

## The install command

One line, run on the target host as root:

```bash
curl -sSL https://raw.githubusercontent.com/JT4862/driveforge/main/scripts/install.sh | sudo bash
```

That fetches and executes the latest `install.sh` from `main`. If you
want a specific tagged release instead of `main`'s latest:

```bash
git clone --branch v0.3.1 https://github.com/JT4862/driveforge.git /opt/driveforge-src
cd /opt/driveforge-src
sudo ./scripts/install.sh
```

The git-clone form leaves the source tree at `/opt/driveforge-src`,
which is where the in-app updater (v0.3.1+) expects it. The `curl |
bash` form does the same clone internally.

## What `install.sh` does

In order:

1. **Checks Debian version.** Warns on non-12 hosts, doesn't abort.
2. **Cleans up apt sources** — strips any stale `cdrom:` entries that
   would otherwise break `apt-get update` post-ISO.
3. **Installs system packages:** `python3`, `python3-venv`,
   `python3-pip`, `smartmontools`, `hdparm`, `sg3-utils`, `nvme-cli`,
   `e2fsprogs`, `fio`, `tmux`, `lshw`, `lsscsi`, `ipmitool`,
   `avahi-daemon`, `avahi-utils`, `ledmon`, `fonts-dejavu-core`,
   `curl`, `ca-certificates`. Plus their transitive deps.
4. **Loads kernel modules:** `ses`, `ipmi_si`, `ipmi_devintf` via
   `/etc/modules-load.d/driveforge.conf` (so DriveForge can read
   chassis temps + drive ident LEDs without root).
5. **Creates the `driveforge` system user.** Locked password,
   `/var/lib/driveforge` home, no shell — daemon process isolation
   only.
6. **Installs udev rules:** `KERNEL=="ipmi[0-9]*", MODE="0660",
   GROUP="driveforge"` so the daemon can read BMC chassis power +
   temperature.
7. **Builds a Python venv at `/opt/driveforge`** and pip-installs the
   DriveForge package + its deps from PyPI.
8. **Installs systemd units:**
   - `driveforge-daemon.service` — the main FastAPI daemon
   - `driveforge-tui.service` — TTY dashboard (optional, console-only)
   - `driveforge-issue.service` — login banner refresh
   - `driveforge-update.service` — one-click update target (v0.3.1+)
9. **Installs the sudoers rule** at `/etc/sudoers.d/driveforge-update`
   that grants the daemon user permission to start (and only start)
   the update service.
10. **Writes default config** to `/etc/driveforge/driveforge.yaml` and
    `/etc/driveforge/grading.yaml` (only if those files don't already
    exist — re-running install.sh preserves your customizations).
11. **Enables + starts the daemon.** `systemctl enable --now
    driveforge-daemon.service`.
12. **Prints access URLs** at the end.

## Where things live post-install

| Path | Purpose |
|------|---------|
| `/opt/driveforge-src/` | DriveForge source tree (git-cloned) |
| `/opt/driveforge/` | Python venv |
| `/etc/driveforge/driveforge.yaml` | User-editable config (Settings UI writes here) |
| `/etc/driveforge/grading.yaml` | Grading thresholds |
| `/etc/sudoers.d/driveforge-update` | Sudoers rule for v0.3.1 in-app update |
| `/etc/systemd/system/driveforge-*.service` | systemd units |
| `/etc/modules-load.d/driveforge.conf` | Kernel modules to autoload |
| `/var/lib/driveforge/driveforge.db` | SQLite state (drives, batches, runs, telemetry) |
| `/var/lib/driveforge/pending-labels/` | Cert label PNGs awaiting print |
| `/var/lib/driveforge/reports/` | Generated cert PDFs |
| `/var/log/driveforge/` | Daemon logs (also via journalctl) |
| `/var/log/driveforge-install.log` | install.sh output |
| `/var/log/driveforge-update.log` | v0.3.1 in-app update output |

## Verify the daemon is running

```bash
systemctl status driveforge-daemon
# should show: Active: active (running)

journalctl -u driveforge-daemon -n 20
# last 20 log lines

curl http://localhost:8080/api/health
# {"status":"ok","dev_mode":false,"active_serials":[]}
```

If the daemon isn't running, the log will tell you why — common
causes: port conflict on 8080 (something else binding it), missing
package in the install (rare; rerun install.sh), Python version too
old (need 3.11+; Debian 12 ships 3.11 by default).

## Re-running install.sh to update

Pre-v0.3.1 (or as a manual fallback after v0.3.1):

```bash
cd /opt/driveforge-src
sudo git pull
sudo ./scripts/install.sh
```

`install.sh` is idempotent — it skips packages that are already
installed, preserves existing config files, and restarts the daemon
at the end. Re-running on every code change is the supported pattern.

For v0.3.1+, the [in-app self-update](../operations/self-update.md)
button does this automatically.

## Differences from the ISO install

The ISO install adds these on top:

- **Preseed-driven Debian install** (you don't see Debian's installer)
- **Hostname defaults to `driveforge`** (manual install keeps your
  existing hostname; rename via [Settings → Hostname](../operations/hostname-rename.md)
  if needed)
- **`forge` user is created** as the admin (manual install doesn't
  touch existing users)
- **`/etc/issue` login banner** is configured
- **avahi enable + module autoload** are part of the preseed flow
  (manual install does these too via install.sh)

End state is the same. The ISO is just a faster path for fresh
hardware.

## Next steps

- [Dashboard tour](../operations/dashboard-tour.md)
- [Auto-enroll](../operations/auto-enroll.md)
- [In-app self-update](../operations/self-update.md)
