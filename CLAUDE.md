# DriveForge — Claude session notes

## Read first
- **[BUILD.md](BUILD.md)** — the canonical project plan. Architecture, phases,
  decisions, UI design, telemetry scope, OSS distribution policy. Every
  substantive design choice lives there.
- User memory at `~/.claude/projects/-Users-jt/memory/` — JT's profile,
  homelab context, red lines.

## Project summary
DriveForge is a Debian + Python app that turns a Dell PowerEdge R720 into
an in-house drive refurbishment pipeline: SMART → secure-erase → burn-in →
long self-test → grade → print cert label. Commercial-refurbisher workflow
at homelab scale.

**Current status: pre-Phase-1.** BUILD.md is complete; no code exists yet.
Do not assume prior sessions wrote anything — check `git log` first.

## Workflow rules
- Branch: `main` only
- Commits: imperative subject, short body, Co-Authored-By trailer (see
  existing commits for the pattern)
- Tests: `pytest` with recorded SMART / nvme-cli / ipmitool fixtures —
  drive hardware is NOT required for dev or CI
- Python: 3.11+, `uv` or venv

## Appliance philosophy (non-negotiable, do not re-litigate)
- **No YAML hand-editing by users.** All configuration lives in the
  Settings UI. YAML on disk is daemon-written.
- **Self-contained by default.** No external service dependencies ship
  enabled. One opt-in outbound webhook is the only integration point;
  Twenty CRM, n8n, etc. are routed by the user downstream of the webhook.
- **Debian owns the OS** (network config via netplan, packages, systemd).
  DriveForge owns the app. Don't let DriveForge manage static IPs.
- **Brother QL printer family** driven by `brother_ql` (raster protocol).
  **Not** `python-escpos` — that's ESC/POS, wrong protocol. Past session
  hit this trap.

## Before writing code
See BUILD.md → Implementation Phases for current status. Phases 1-7 are
fixture-complete. Real-hardware iteration is now the active loop.

## Real-hardware deployment state (as of 2026-04-19, end of session)

- **Server**: R720 at `10.10.10.101` (hostname `driveforge`, mDNS alias
  `driveforge.local:8080`). Repo pushed private to
  <https://github.com/JT4862/driveforge>.
- **Login user = daemon user** named `driveforge` (JT's login AND the
  systemd service account). Not the cleanest separation; README tells
  OSS users to pick a different login name. install.sh warns when the
  existing `driveforge` looks like a login account.
- **Boot drive**: SK hynix 128GB SSD via USB-to-SATA (JMicron JMS578
  bridge). Excluded from discovery via findmnt root-device detection.
- **Backplane**: R720 LFF **direct-attach** variant — no SAS expander,
  no SES. Falls back to `virtual_bays=8`. Slot LED control not possible
  on this chassis.
- **HBA**: PERC H710 crossflashed to LSI 9207-8i IT mode ✓
- **Deploy workflow**: tar-over-ssh source sync → `sudo ./scripts/install.sh`
  (non-editable pip install + systemctl restart). ~30s per iteration.

## Scope decisions during this session

**Firmware auto-apply REMOVED.** Earlier design had signed community DB +
approval rows + canary + fail-closed gates. JT called it overkill for
homelab use, and reality of drive firmware distribution (no public
repo, vendor-gated, not legally redistributable) made the machinery
unhelpful. Stripped: `core/firmware.py`, `core/signing.py`,
`firmware_db.yaml`, `FirmwareApproval` + `FirmwareOperation` tables,
Settings Firmware panel, approve-button UI, /api/firmware/* endpoints,
cryptography dep. **Kept**: `drive.firmware_version` from lsblk `REV`
field, displayed on drive detail Hardware panel + logged in Phase 3
log. Drive firmware updates are now an explicit manual operation;
DriveForge just surfaces the current version for operator reference.

**Quick mode = provisional.** Quick-mode grades render with `*`
superscript + tooltip everywhere (dashboard cards, drive detail,
history, batch detail). Public cert page gets a yellow PROVISIONAL
banner explaining what was skipped and how to upgrade to a full cert.

## Real-hardware bugs found + fixed

1. `/etc/driveforge/` was root-owned — now chowned to driveforge user
2. `driveforge` user added to `disk` + `cdrom` groups for /dev/sdX access
3. SMART self-test "not supported" was misclassified as failure due to
   `bool(None) = False` coercion — now tri-state (True/False/None)
4. SAS self-test parsing used `entry.status` (doesn't exist) — fixed to
   `entry.result`; real JSON captured as test fixture
5. SATA drives on SAS HBAs report tran=sas from lsblk; sg_format fails
   with "Illegal request". Now `detect_true_transport()` re-probes via
   smartctl in the erase path only (not dashboard hot path — moving it
   out of discover() was the fix after concurrent smartctl pile-ups
   caused D-state process jams and dashboard unresponsiveness).
6. systemd `ProtectHome=true` + `pip install -e` from `/home` clashed.
   Decision: stay on non-editable install; ProtectHome stays on.
7. Abort cancelled asyncio tasks but orphaned subprocess children.
   `process.kill_owner(serial)` now SIGTERM + SIGKILL on abort. Note:
   mid-flight `sg_format` is fundamentally unabortable safely — once
   the SCSI FORMAT UNIT command is on the wire, killing the host
   process doesn't stop the drive's firmware.
8. Dashboard nested-anchor bug — report-icon `<a>` inside bay-card `<a>`
   broke layout. Fixed by splitting into sibling `<a>`s inside a `<div>`
   container with absolute-positioned badges.

## Known-risky SAS quirk (documented in UI)

**`sg_format` mid-flight abort corrupts the drive.** Once SCSI FORMAT
UNIT is on the wire, interrupting leaves the drive with "Medium format
corrupted" / capacity = 0 B. Recovery: manual `sudo sg_format --format
/dev/sdX` to completion (15-60 min on 300GB SAS). JT hit this once
during abort testing; drive recovered fine. New Batch form warns.

## Drives on hand for testing

- Seagate ST300MM0006 (S0K2BSJC) — 300GB SAS 10K, 71k POH; no short
  self-test support; recovered from sg_format corruption via manual
  format
- Seagate ST300MM0006 (S0K2BARS) — second unit of same model
- Intel SSDSC2BB120G4 (CVWL431600NS120LGN) — 120GB SATA SSD on SAS
  HBA; successfully quick-mode erased via hdparm → Grade B (1
  reallocated sector, 48k POH / ~5.5 years continuous)

## What's still not validated on real hardware

- Full-mode pipeline (badblocks + long self-test) — only quick-mode tested
- Thermal printer (no Brother QL connected yet)
- Multi-drive parallel batch (only 1-drive batches so far)
- Real failure grading (all live tests passed A or B, no real-world Fail yet)
- Outbound webhook to a real n8n endpoint

## README has screenshots

As of commit 9e0ea3d, the README has a "What it looks like" visual tour
section BEFORE install info. Screenshots live in `docs/screenshots/`
and are git-tracked. If the UI changes substantially, re-capture via
headless Chrome: see commit message for the incantation.
