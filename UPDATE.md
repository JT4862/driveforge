# Updating DriveForge

Once DriveForge is installed, there are four lanes for keeping it
current. Pick the one that matches your situation and risk tolerance.

| Lane | When to use | Downtime | Effort |
|---|---|---|---|
| [**1. In-app update checker**](#lane-1-in-app-update-checker) | You just want a notification when a new release is out | — (no update happens) | One click |
| [**2. `git clone` + `install.sh`**](#lane-2-git-clone--installsh-in-place-update) | Incremental updates on a system that already has DriveForge | ~30 s (daemon restart) | 1 minute SSH |
| [**3. Release tarball + `install.sh`**](#lane-3-release-tarball--installsh-no-git) | Same as lane 2, but without `git` installed | ~30 s | 2 minutes |
| [**4. Fresh ISO install**](#lane-4-fresh-iso-install-major-version-only) | Major version jump, or OS itself needs a refresh | ~20 min | 15 minutes + operator attention |

**Your data is preserved across lanes 2 + 3.** `install.sh` is idempotent
and explicitly preserves:

- `/var/lib/driveforge/driveforge.db` — test history, drive records,
  batch history, telemetry samples
- `/etc/driveforge/*.yaml` — grading thresholds, printer config,
  integrations, daemon settings
- `/var/log/driveforge/` — phase logs

Lane 4 (fresh ISO) wipes everything unless you've copied those off
first — see [Backup before updating](#backup-before-updating).

---

## Before you update

### Stop in-flight batches

`install.sh` restarts the daemon at the end, which will abort any
batches still running. Not destructive to drives (badblocks aborts
cleanly), but the aborted drives lose their grading verdict.

Either wait for the current batch to finish, or click **Abort All** on
the dashboard and let it finalize (~2 seconds) before starting the
update.

### Check what version you're on

```bash
# From an SSH session on the installed server:
/opt/driveforge/bin/driveforge --version

# Or via the API from your Mac:
curl -s http://driveforge.local:8080/api/health
```

### Read the release notes

Check the [Releases page](https://github.com/JT4862/driveforge/releases)
for the target version. Look for:

- **Breaking changes** — config file format changes, schema migrations,
  etc. (Usually called out with `BREAKING:` prefix.)
- **Known issues** — if a release has a known bug on your hardware
  class, it's documented there.
- **Minimum-version-to-update-from** — occasionally a release requires
  stepping through an intermediate version.

---

## Lane 1: In-app update checker

**Does not update anything.** Tells you that an update is available and
shows the shell commands to apply it via one of the other lanes.

- Open the dashboard → **Settings** → **About / Updates**
- Click **Check for updates**
- If a newer release exists, you'll see the version number + copy-paste
  command

The check hits `https://api.github.com/repos/JT4862/driveforge/releases/latest`
and caches the result for 1 hour. No personal data is sent.

### Why isn't this a one-click button?

Fully-automatic updates on a drive-destructive appliance would be
irresponsible — a bad update could interrupt an active batch and
corrupt the SQLite DB. The "automatic" version is on the backlog but
gated behind polkit, batch-refusal guards, log streaming, and
auto-reconnect. Until that's built, the update checker is
notification-only.

---

## Lane 2: `git clone` + `install.sh` (in-place update)

The standard lane for anyone who installed via Path B (direct Debian
install) or wants incremental updates on an ISO-installed system.

### First time — clone a source tree

If you don't already have a working clone on the server:

```bash
ssh forge@driveforge.local     # or your admin user
sudo apt-get install -y git    # if git isn't already there
git clone https://github.com/JT4862/driveforge.git ~/driveforge
cd ~/driveforge
sudo ./scripts/install.sh
```

### Subsequent updates

```bash
ssh forge@driveforge.local
cd ~/driveforge
git fetch --tags
git checkout v0.x.y            # or `git pull origin main` for bleeding edge
sudo ./scripts/install.sh
```

`install.sh` will:

1. Re-install any system packages that have changed
2. Upgrade the Python venv in-place (`pip install --upgrade .[linux]`)
3. Reload + restart the systemd unit
4. Print the access URLs

Your DB and config survive untouched.

### If you installed via the ISO

The ISO doesn't leave a git clone — it drops an extracted source tree
at `/usr/local/share/driveforge-bundle/`. That tree is **not a git
repo**, so `git pull` inside it won't work. For in-place updates from
an ISO install, either:

- Follow "First time" above to create a fresh clone in your home
  directory, then use it for all future updates, OR
- Use [Lane 3 (tarball)](#lane-3-release-tarball--installsh-no-git)

---

## Lane 3: Release tarball + `install.sh` (no git)

Same end result as lane 2 but doesn't require `git` on the server.
Useful for air-gapped updates or restricted environments.

```bash
ssh forge@driveforge.local

# Get the tarball of the release you want
VERSION="v0.x.y"                # adjust to target release
curl -fL -o /tmp/driveforge-${VERSION}.tar.gz \
  https://github.com/JT4862/driveforge/archive/refs/tags/${VERSION}.tar.gz

# Extract + install
tar xzf /tmp/driveforge-${VERSION}.tar.gz -C /tmp
sudo /tmp/driveforge-*/scripts/install.sh

# Clean up
rm -rf /tmp/driveforge-*
```

This uses GitHub's auto-generated source tarballs, which are equivalent
to `git archive HEAD` at that tag. Works exactly like lane 2 for the
`install.sh` step.

### Air-gapped variant

If the server has no internet, download the release's **offline
bundle** (`driveforge-offline-<version>.tar.gz`) from the release page
on an internet-connected machine, `scp` it over, then:

```bash
scp driveforge-offline-<version>.tar.gz forge@driveforge.local:/tmp/
ssh forge@driveforge.local
tar xzf /tmp/driveforge-offline-<version>.tar.gz -C /tmp
sudo DRIVEFORGE_OFFLINE_BUNDLE=/tmp/driveforge-offline-<version> \
  /tmp/driveforge-offline-<version>/scripts/install.sh
```

The `DRIVEFORGE_OFFLINE_BUNDLE` env var tells `install.sh` to use the
bundled .deb and .whl files instead of hitting apt mirrors / PyPI.

---

## Lane 4: Fresh ISO install (major version only)

Use only when:

- The release notes explicitly recommend a clean install (e.g. major
  OS version bump from Bookworm → Trixie in the future)
- Your existing install has drifted so far from clean that other lanes
  misbehave
- You're rebuilding the hardware from scratch anyway

### Backup before updating

**This lane WILL wipe the OS disk.** If you want to preserve history
and config, copy them off first:

```bash
ssh forge@driveforge.local

# Grab the DB + config + phase logs
sudo tar czf /tmp/driveforge-backup-$(date +%Y%m%d).tar.gz \
  /var/lib/driveforge/driveforge.db \
  /etc/driveforge/ \
  /var/log/driveforge/

# Copy off to your workstation
scp forge@driveforge.local:/tmp/driveforge-backup-*.tar.gz ~/
```

### Do the reinstall

1. Follow [Path A in INSTALL.md](INSTALL.md#path-a-iso-installer-recommended)
   with the new ISO
2. After first boot, walk through the setup wizard with defaults (or
   skip to step 3 if restoring)

### Restore DB + config (optional)

```bash
# Copy the backup back
scp ~/driveforge-backup-*.tar.gz forge@driveforge.local:/tmp/

ssh forge@driveforge.local
sudo systemctl stop driveforge-daemon

# Extract over the fresh install
sudo tar xzf /tmp/driveforge-backup-*.tar.gz -C /

# Fix ownership in case tar changed it
sudo chown -R driveforge:driveforge /var/lib/driveforge /var/log/driveforge /etc/driveforge

sudo systemctl start driveforge-daemon
```

Your test history and configuration are back. Re-enter any secrets the
backup didn't include (rare — most settings are plain YAML).

---

## Rollback

If an update breaks something, roll back to the previous version via
the same lane you used to update.

### Lane 2 rollback

```bash
cd ~/driveforge
git log --oneline -5           # find the previous version's commit
git checkout v0.x.<y-1>        # or the SHA
sudo ./scripts/install.sh
```

### Lane 3 rollback

Same commands as the forward path but with the older `VERSION` tag.

### Lane 4 rollback

Flash the previous release's ISO and reinstall from scratch. Slow but
guaranteed-clean.

### DB compatibility

The DB uses SQLAlchemy auto-migration on daemon start. Generally:
- **Patch versions (v0.1.0 → v0.1.1)**: DB is forward-compatible and
  backward-compatible
- **Minor / major versions**: check release notes — occasionally a
  migration is one-way

If in doubt, keep a DB backup from before the update (see
[Backup before updating](#backup-before-updating)) and restore it if
the rollback hits a schema mismatch.

---

## Troubleshooting

### `install.sh` fails partway through

Run with verbose output to see which step broke:

```bash
sudo bash -x ./scripts/install.sh 2>&1 | tee /tmp/install-debug.log
```

Share the last 50-ish lines in a
[bug report](https://github.com/JT4862/driveforge/issues/new?template=bug_report.md).

### After update, daemon won't start

```bash
sudo journalctl -u driveforge-daemon -n 100 --no-pager
```

Look for import errors (venv out of sync — rebuild with `sudo rm -rf
/opt/driveforge && sudo ./scripts/install.sh`), schema errors (DB
migration — check release notes for the target version), or
permission errors (ownership drift — `sudo chown -R
driveforge:driveforge /var/lib/driveforge /etc/driveforge`).

### Dashboard shows old version after update

Hard-refresh the browser (Cmd+Shift+R on Mac, Ctrl+Shift+R on Linux /
Windows) — the SPA caches its bundle aggressively. If it persists,
check the actual daemon version:

```bash
curl -s http://driveforge.local:8080/api/health
/opt/driveforge/bin/driveforge --version
```

### Lost DB after lane 4

You're out of luck unless you took a backup. This is why the lane-4
instructions open with the backup step in bold.
