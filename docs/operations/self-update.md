---
title: In-app self-update
---

# In-app self-update

*Available since v0.3.1. Authorization refactored to polkit in v0.6.0.*

DriveForge can update itself in place from the dashboard. **Settings →
About / Updates → Install update now**. One click, live log streaming
during install, automatic dashboard reconnect after the daemon
restart. No SSH required. v0.6.0+ also shows you the **release notes
BEFORE you commit to the update**, so you see what's changing rather
than finding out after the restart.

## How it works

1. **Click triggers `POST /settings/install-update`.** The handler
   refuses if any drive is currently in the test pipeline OR in
   recovery — the daemon restart at the end of the install would
   orphan their state. The dashboard surfaces the refusal with a
   plain-English banner explaining what to wait for.

2. **Daemon invokes `systemctl start driveforge-update.service`.**
   No `sudo` in the argv (v0.6.0+). Instead, the unprivileged daemon
   user `driveforge` is authorized to call `StartUnit` on that one
   specific unit via a polkit rule at
   `/etc/polkit-1/rules.d/50-driveforge-update.rules`. systemctl
   speaks systemd's D-Bus interface under the hood; polkit
   mediates — so the net effect is a scoped, PAM-free authorization
   for that one action. No general elevation; if an attacker
   compromises the daemon, the worst they can do is force a
   re-install from origin/main (which would require also
   compromising the GitHub repo to inject malicious code).

   *Pre-v0.6.0 hosts used a sudoers rule instead. Upgrading to
   v0.6.0 via install.sh removes the stale
   `/etc/sudoers.d/driveforge-update` file automatically.*

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
- **Polkit rule missing or mis-installed** (v0.6.0+). Banner shows
  systemctl's stderr verbatim — typically `Failed to start
  driveforge-update.service: Interactive authentication required.`
  Fix: rerun `sudo scripts/install.sh` from the source tree, which
  re-installs the rule to `/etc/polkit-1/rules.d/`.

## Manual fallback

The pre-v0.3.1 copy-paste commands are still available — click
"Or update manually via SSH instead" on the Settings page to
expand them. Useful if the in-app flow is broken and you need to
update the box that fixes it.

## Updating a fleet (v0.11.4+)

When the daemon is running as a fleet operator, the Install update
button **also pushes the update to every connected agent** in
parallel before triggering its own update. v0.11.6+ adds verified
delivery: the operator queues an `UpdateCmd` on each agent's
outbound WebSocket queue, waits up to 5 seconds per agent for an
ACK (`CommandResultMsg`), then fires its own update. The resulting
redirect URL carries `fleet_pushed=N&fleet_acked=M&fleet_failed=X,Y`
so the Settings page can render a per-agent failure banner with
manual recovery commands.

Failed/timed-out agents do NOT block the operator's own update;
they just appear in the failed list with the SSH command needed
to retry by hand. See [Fleet mode → Upgrading the fleet](fleet.md#upgrading-the-fleet)
for the full flow.

## When the button itself was broken

v0.11.8 fixed a class of dead-button bugs caused by browsers
silently blocking `window.confirm()` after repeated use — the form's
`onsubmit` handler returned undefined, the form never submitted,
and the operator was stuck with a button that did nothing. The fix
dropped the JS confirm dialog (the green "update available" panel
above the button + the "Restarts the daemon" subtitle below
already explain what's about to happen). v0.11.10 swept the same
pattern from every other form across the app.

If you're on a pre-v0.11.8 install whose Install button does
nothing when clicked, the SSH fallback at the bottom of the
Updates panel is the bootstrap path — use it once to get to
v0.11.8+, then the button works for everything after that.

## Still not done

- **No partial / per-component updates.** It's all or nothing —
  full git pull + full install.sh.
- **No rollback button.** If an update breaks something, you SSH in
  and `cd /opt/driveforge-src && git checkout <prev-tag> &&
  sudo ./scripts/install.sh`.
- **No staged rollouts.** Fleet update is parallel-all-at-once, not
  canary-then-rollout. Operators who want canary-style update can
  pre-revoke specific agents from the operator's Agents page,
  upgrade the rest via the button, then re-enroll the canary
  manually after validation.

## Security model

- The polkit rule (v0.6.0+) grants ONLY
  `action.id = "org.freedesktop.systemd1.manage-units"` with
  `action.lookup("unit") == "driveforge-update.service"` AND
  `action.lookup("verb") == "start"` for `subject.user == "driveforge"`.
  Every other unit, every other verb, every other user falls
  through and hits the default polkit policy (interactive auth
  required). Audit the rule any time with
  `cat /etc/polkit-1/rules.d/50-driveforge-update.rules`.
- The dashboard has no auth. Anyone on your LAN with network
  access to port 8080 can hit Install. Treat your LAN
  appropriately. (TLS + auth are a future feature.)
- DriveForge pulls from `https://github.com/JT4862/driveforge.git`.
  HTTPS guarantees the bytes match what's on GitHub at fetch
  time; we don't currently verify commit signatures locally
  before installing.
