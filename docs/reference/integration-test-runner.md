---
title: Integration test runner setup
---

# Real-hardware integration test runner

*Available since v0.5.0.*

DriveForge's unit test suite runs fine on any machine, but it can only
test what it can mock. Real-hardware bugs — the v0.4.1–v0.4.4 pattern
where unit tests passed but the real daemon or drive broke — are
invisible to monkeypatched subprocess tests.

**v0.5.0 adds a real-hardware integration test suite** that runs
against a dedicated "test drive" on a self-hosted GitHub Actions
runner. The suite runs on every PR and wipes that drive as part of
exercising the real `secure_erase` code path end-to-end.

This page covers the one-time setup to register such a runner.

## What you need

- **A Linux host with DriveForge installed** — the R720 itself works
  well; it's already set up with an IT-mode HBA and drive bays
- **A dedicated test drive** — a small, cheap, KNOWN-ERASABLE SATA
  SSD (120-500 GB). The suite will wipe it on every PR. This is NOT
  a drive you want to keep data on.
- **GitHub repo admin access** (to register a self-hosted runner)

A 2 TB hard cap is enforced in the test harness itself; anything
larger will be skipped. Use an SSD so each PR runs in minutes, not
hours.

## 1. Register the runner on your host

From the GitHub UI: **Repo → Settings → Actions → Runners → New
self-hosted runner** → Linux x64.

GitHub gives you a copy-paste shell script. Run it on your
DriveForge host as a non-root user with sudo privileges. It'll:

1. Download the GitHub Actions runner binary
2. Register it with your repo using a one-time token
3. Offer to install it as a systemd unit

Accept the systemd install (`sudo ./svc.sh install && sudo ./svc.sh
start`) so the runner auto-starts on boot.

### Label the runner

When GitHub's setup script asks for runner labels, add:

```
self-hosted,linux,driveforge-integration
```

The `.github/workflows/integration-test.yml` workflow looks for all
three labels to avoid accidentally matching other runners.

## 2. Configure the test-drive env vars

Find the drive's serial number:

```sh
sudo smartctl -i /dev/sdX | grep "Serial Number"
```

The runner needs two env vars set persistently:

- `DRIVEFORGE_INTEGRATION_DEVICE` — the `/dev/sdX` path of the test
  drive
- `DRIVEFORGE_INTEGRATION_ALLOWED_SERIALS` — the exact serial(s),
  comma-separated if multiple drives

These go into the runner's environment config. Edit `.env` in the
runner's install directory (typically `~/actions-runner/.env`):

```
DRIVEFORGE_INTEGRATION_DEVICE=/dev/sdh
DRIVEFORGE_INTEGRATION_ALLOWED_SERIALS=WD-WX...your-serial-here
```

Restart the runner to pick up the new env:

```sh
cd ~/actions-runner
sudo ./svc.sh stop
sudo ./svc.sh start
```

## 3. Configure sudo for the integration tests

The integration tests need root access to issue `sg_raw` /
`hdparm` against the test drive. Add a sudoers rule for the runner
user:

```sh
sudo visudo -f /etc/sudoers.d/driveforge-integration-runner
```

Add this line (substitute the runner user for `runner` and the venv
path for your checkout):

```
runner ALL=(root) NOPASSWD: /home/runner/actions-runner/_work/driveforge/driveforge/.venv-ci/bin/pytest
```

Tighter than generic sudo — the runner can only invoke the test
suite binary, not arbitrary commands.

## 4. Safety gates (already in the test code)

The integration test harness has four layers of "don't wipe the
wrong drive":

1. **Env var required** — absence → tests skip, PR passes
2. **Device must exist** — absence → skip with clear error
3. **Serial allow-list match** — the drive's serial must be in
   `DRIVEFORGE_INTEGRATION_ALLOWED_SERIALS`. A drive swap mid-CI
   (or typo in the env var) means skipping rather than wiping the
   wrong drive.
4. **Boot-drive check** — if the target device is any parent of `/`
   or `/boot`, the test fails loudly and refuses to touch it.

Plus a 2 TB capacity cap so a huge drive doesn't block CI for
hours.

## 5. Verify it works

Create a PR with any small change. Check the PR's Actions tab. You
should see the **Real-hardware integration tests** workflow run on
the self-hosted runner. In the test output, look for:

- `preflight completed in X.XXs`
- `erase: X.XXs`
- `postcheck: X.XXs`

If the env vars aren't set or the drive isn't present, you'll see
`SKIPPED [reason]` — the PR still passes. That's the "degraded but
honest" mode: CI doesn't block on hardware availability, but it
also doesn't silently give you green checks when the hardware test
didn't actually run.

## Troubleshooting

### Runner shows offline in GitHub UI

```sh
cd ~/actions-runner
sudo ./svc.sh status
sudo journalctl -u 'actions.runner.*' -n 50
```

Usually one of: runner token expired (re-register), network/DNS
issue reaching GitHub, or the systemd unit failed and needs
restart.

### Tests always skip, never run

Check the env vars are actually reaching the test process:

```sh
sudo ./svc.sh stop
cd ~/actions-runner
cat .env
# should show DRIVEFORGE_INTEGRATION_DEVICE=...
sudo ./svc.sh start
```

Re-run the workflow. The "Show environment" step logs what the
runner sees.

### Tests fail on `sudo -E pytest`

The sudoers rule needs the EXACT binary path to match. Check the
runner's `.venv-ci/bin/pytest` path and update
`/etc/sudoers.d/driveforge-integration-runner`. The wildcards some
people try don't work with sudo.
