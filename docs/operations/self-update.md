---
title: In-app self-update
---

# In-app self-update

*Available since v0.3.1.*

DriveForge can update itself in place from the dashboard. **Settings →
About / Updates → Install update now**. One click, live log streaming
during install, automatic dashboard reconnect after the daemon
restart. No SSH required.

## How it works

1. **Click triggers `POST /settings/install-update`.** The handler
   refuses if any drive is currently in the test pipeline OR in
   recovery — the daemon restart at the end of the install would
   orphan their state. The dashboard surfaces the refusal with a
   plain-English banner explaining what to wait for.

2. **Daemon invokes `sudo systemctl start driveforge-update.service`.**
   That's the only privileged command the daemon user has sudo
   access for — see `/etc/sudoers.d/driveforge-update`. No general
   sudo grant; if an attacker compromises the daemon, the worst
   they can do is force a re-install from origin/main (which would
   require also compromising the GitHub repo to inject malicious
   code).

3. **`driveforge-update.service` runs `/usr/local/sbin/driveforge-update`,**
   which:
   - Locates the source tree (`/opt/driveforge-src` for ISO
     installs, `~driveforge/driveforge-src` for manual installs)
   - Refuses if the current branch isn't `main` or there are
     uncommitted local changes (operator clearly customized the
     install; manual update required)
   - `git fetch origin main` + `git merge --ff-only origin/main`
   - Reruns `scripts/install.sh`
   - install.sh restarts `driveforge-daemon.service` at its end

4. **Dashboard tails `/var/log/driveforge-update.log` live.** A
   panel appears below the Updates section showing the update's
   stdout/stderr in real time (HTMX-polled every 2s while the
   systemd unit reports `active`).

5. **Daemon disappears for ~10–15 seconds** during the restart.
   The page-level JS notices the `/api/health` poll starts failing,
   shows a "Daemon is restarting…" overlay, and reloads the
   Settings page once the new daemon answers — at which point the
   footer shows the new version.

## When it refuses

- **Drives under test.** Wait for them to finish or hit Abort on
  each. The refusal banner tells you how many.
- **Drive recovery in progress.** A pulled-mid-erase drive is being
  repaired and re-enrolled; wait for that to finish.
- **Already on the latest version.** No-op; the log will say "already
  at origin/main."
- **Uncommitted local changes** in the source tree, or a non-main
  branch checked out. Manual `git status` + cleanup needed.
- **sudoers rule missing** (visudo failed during install, or the
  rule was hand-removed). Banner shows the literal `sudo` error.

## Manual fallback

The pre-v0.3.1 copy-paste commands are still available — click
"Or update manually via SSH instead" on the Settings page to
expand them. Useful if the in-app flow is broken and you need to
update the box that fixes it.

## What's NOT done in v0.3.1

- **No partial / per-component updates.** It's all or nothing —
  full git pull + full install.sh.
- **No rollback button.** If an update breaks something, you SSH in
  and `cd /opt/driveforge-src && git checkout <prev-tag> &&
  sudo ./scripts/install.sh`.
- **No release-notes preview** before installing. The Updates panel
  shows what version is available and links to the GitHub release;
  click through to read notes before clicking Install.
- **No staged rollouts** across multiple boxes. Each box updates
  independently when its operator clicks the button.

## Security model

- The sudoers rule grants ONLY `systemctl start
  driveforge-update.service`. Not `systemctl restart`, not
  `systemctl stop`, not any other unit. Audit it any time with
  `cat /etc/sudoers.d/driveforge-update`.
- The dashboard has no auth. Anyone on your LAN with network
  access to port 8080 can hit Install. Treat your LAN
  appropriately. (TLS + auth are a future feature; not in v0.3.1.)
- DriveForge pulls from `https://github.com/JT4862/driveforge.git`.
  HTTPS guarantees the bytes match what's on GitHub at fetch
  time; we don't currently verify commit signatures locally
  before installing.
