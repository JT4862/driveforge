---
title: Known issues
---

# Known issues

A live-maintained list of hardware quirks DriveForge has hit in the
field, organized by status (fixed vs. still open vs. waiting on
hardware).

## Fixed

### `CONFIG_IDE_TASK_IOCTL` on SATA-on-SAS — fixed in v0.3.0

**Symptom (pre-v0.3.0):** SATA drives behind a SAS HBA failed at
secure_erase with:

```
The running kernel lacks CONFIG_IDE_TASK_IOCTL support for this device.
SECURITY_ERASE: Invalid argument
```

**Hardware affected:** anything modern. Most visible on the NX-3200's
LSI SAS2308 + Debian 12 kernel; the R720's older LSI 9207-8i
happened to still work because of a different code path in `hdparm`'s
fallback chain.

**Root cause:** `hdparm --security-erase` issues the ATA SECURITY
ERASE UNIT command via the legacy `HDIO_DRIVE_TASKFILE` ioctl. Modern
Linux kernels removed that ioctl for SATA drives carried by SAS-
family HBAs.

**Fix:** v0.3.0 replaces the hdparm path with SAT passthrough —
the same ATA command wrapped in a SCSI ATA-PASS-THROUGH(16) CDB,
submitted via `sg_raw`. Universally supported by SAT-3 conformant
HBAs.

**If you see this:** upgrade to v0.3.1 or later (in-app updater
in v0.3.1+ makes this one click).

### Stuck `interrupted_at_phase` runs from bus glitches — fixed in v0.2.9

**Symptom (pre-v0.2.9):** A SATA drive on the NX-3200 would sit
forever in "idle · never tested" state on the dashboard with no
auto-enroll firing, even with auto-enroll on. Manual investigation
showed an open TestRun with `interrupted_at_phase="secure_erase"`
and `completed_at=NULL`.

**Root cause:** Pre-v0.2.9, `_looks_like_pull` only checked
`os.path.exists(drive.device_path)`. The CONFIG_IDE_TASK_IOCTL
hdparm failure caused brief kernel re-enumeration of the SATA drive
(new `/dev/sdX` letter assigned). In the tiny window after the
failure, the original device path didn't exist — so the orchestrator
classified the failure as a pull. The drive was never actually
pulled, so no hotplug ADD event ever fired to trigger recovery, and
the run stayed stuck.

**Fix:** v0.2.9 added serial-rediscovery: if the drive's serial is
findable in `lsblk` under any current device path, it's NOT a pull —
it's a genuine pipeline failure that closes cleanly as
`grade="fail"`.

### Re-test churn from re-inserting graded drives — fixed in v0.2.9

**Symptom (pre-v0.2.9):** Re-inserting a Grade A drive 8 hours after
its test, with auto-enroll on, would re-test it for no reason. Same
for Grade B/C drives; same for Failed drives.

**Root cause:** Pre-v0.2.9 had a 1-hour cutoff on the auto-enroll
"recently graded" filter — runs older than that didn't block. The
cutoff was meant to prevent re-testing immediately after pull-and-
reinsert; in practice it caused churn on shelves of pre-tested
drives.

**Fix:** v0.2.9 made the graded-drives skip indefinite. A drive
with any real grade (A/B/C/Fail) does NOT auto-retest, regardless
of when the test happened. Manual `+ New Batch` to retest.

### `driveforge.local` collision on multi-box LANs — fixed in v0.2.8

**Symptom:** Two DriveForge boxes on one LAN both tried to claim
`driveforge.local`. avahi auto-suffixed the loser as
`driveforge-2.local` non-deterministically.

**Fix:** v0.2.8 added Settings → Hostname for runtime rename.
See [Hostname rename](../operations/hostname-rename.md).

### `driveforge-issue.service` exit=2 cosmetic noise — fixed in v0.2.9

**Symptom:** `systemctl status driveforge-issue.service` showed the
unit failed (red) even though the `/etc/issue` banner was correctly
written.

**Root cause:** The script ran with `set -euo pipefail`. On hosts
where `ip route get 1.1.1.1` returned non-zero (no default route at
boot, etc.), the failed command short-circuited the script even
though the fallback IP detection still produced a usable value.

**Fix:** v0.2.9 dropped `set -e` from the script (its whole structure
is fallback-chain) and added explicit `exit 0` at the end.

## Open

### Brother QL printer hardware test — waiting on hardware

The label-printing code path (`brother_ql` raster protocol, USB /
network / file backends) is wired but no Brother QL printer has
been tested on hand. The `file://` backend writes a PNG to /tmp
which renders correctly; real-printer round-trip is unverified.

**Status:** waiting on a Brother QL-800 / 810W / 820NWBc / 1100 /
1110NWBc to test against.

### Amber fault LED via SES — waiting on hardware

`ledctl fault=/dev/sdX` is wired into the post-run blinker for
failed drives. Works on chassis with SES-capable backplanes that
implement the IBPI fault LED.

R720 LFF and NX-3200 expander-only backplanes both **don't** have
this — neither lights an amber fault LED in response to ledctl.
The activity-LED lighthouse pattern is the universal fallback.

**Status:** waiting on a Dell MD1200-class JBOD or upgraded NX-3200
SKU with the SES-capable backplane revision to test the amber LED
end-to-end.

### Real Fail-tier drive grading — waiting on hardware

Every drive tested through the pipeline so far has graded A/B/C —
no organic Fail-tier drive has come through. The Fail-state UI
(red badge, fail cert PDF, fail label) is implemented but
unexercised against a real failing drive.

**Status:** waiting for a real failing drive to come through the
refurb pipeline. Any drive with growing reallocation count, pending
sectors, or SMART-status-failing should hit it.

### Outbound webhook to live n8n — waiting on hardware

The webhook payload is built from the TestRun + Batch DB rows and
POSTed to the configured URL. The code path is unit-tested against
a stub server; never fired against a real n8n endpoint.

**Status:** waiting on a real n8n instance to point at and confirm
the payload renders correctly in n8n's webhook node.

## Watch list (one-time observations)

### Hung `hdparm -B254` D-state during secure_erase (fixed v0.6.9)

**First observed** on the R720 during the 2026-04-19 8-drive batch:
two `hdparm -B254 /dev/sde` processes stuck in uninterruptible
D-state for hours. Initially assumed to be stale subprocesses from
an earlier orchestrator code path and filed as a watch-list item.

**Reproduced** on the R720 v0.6.6 on 2026-04-21: during an active
SECURITY ERASE UNIT on sdf (WDC WD1000CHTZ), two `hdparm -B254
/dev/sdf` processes appeared alongside the legitimate `sg_raw`
erase. Both had PPID=1 (reparented to init after their udev-worker
parent exited) — the telltale sign of a udev-RUN helper.

**Root cause** (not a DriveForge code bug): Debian's stock
`/lib/udev/rules.d/85-hdparm.rules` fires `/lib/udev/hdparm` on every
`ACTION=="add"` event for sd[a-z] devices. That helper runs
`/usr/lib/pm-utils/power.d/95hdparm-apm`, which issues `hdparm -B<N>`
to set APM per the hdparm-functions defaults — even with an empty
`/etc/hdparm.conf`. On a drive mid-SECURITY-ERASE-UNIT, the drive
refuses APM commands (it's busy doing the erase), so the `hdparm
-B254` subprocess goes D-state waiting on a response it will never
receive. Udev re-enumeration transitions during the erase can fire
the rule multiple times, stacking D-state subprocesses on the same
drive.

Combined with the daemon's own `sg_raw` + `smartctl` against the
same drive, the mpt2sas HBA's SG queue fills and
`kworker/*+fw_event_mpt2sas*` goes D-state processing events it
can no longer drain. At that point, newly-inserted drives on the
same HBA never get `/dev` nodes — the discovery pipeline is
wedged until the D-state pileup clears (either by drive pull or
by the original erase finishing).

**Fix (v0.6.9):** `install.sh` symlinks
`/etc/udev/rules.d/85-hdparm.rules` to `/dev/null`. Files in
`/etc/udev/rules.d` take precedence over same-named files in
`/lib/udev/rules.d`, and a `/dev/null` symlink is the standard
Debian idiom for shadowing a vendor-shipped rule. DriveForge
manages drive power state explicitly per pipeline phase and has
no use for Debian's boot-time APM helper.

**Existing installs** get the mask the next time the in-app
"Install update" path runs (which re-invokes install.sh). To
mitigate immediately without waiting for the update, run:

```
sudo ln -sf /dev/null /etc/udev/rules.d/85-hdparm.rules
sudo udevadm control --reload-rules
```

**Status:** fixed v0.6.9. Leaving this entry in place as
background / post-mortem for operators on older releases.

## Reporting hardware-specific issues

If you hit a quirk on hardware not listed here, file an
[issue](https://github.com/JT4862/driveforge/issues) with:

- Chassis model + HBA (`lspci -v` excerpt for the SAS / RAID line)
- Drive that triggered it (model + firmware version from
  `smartctl -a`)
- DriveForge version (`Settings → About`)
- The dashboard's failed-card error message (or `journalctl -u
  driveforge-daemon -n 100` if no card was rendered)

Hardware-specific quirks tend to need code changes in the relevant
transport / erase / detection module — fixes typically ship as a
`.x` patch release within a day or two of confirmed reproduction.
