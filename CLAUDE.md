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

## Real-hardware deployment state (as of 2026-04-19)

- **Server**: R720 at `10.10.10.101` (hostname `driveforge`, mDNS alias
  `driveforge.local:8080`)
- **Login user = daemon user** named `driveforge` (both JT's login and
  the systemd service account). Not the cleanest separation; OSS docs
  tell users to pick a different login name, but JT's setup reuses one.
- **Boot drive**: SK hynix 128GB SSD via USB-to-SATA (JMicron JMS578
  bridge). Excluded from drive discovery via findmnt.
- **Backplane**: R720 LFF **direct-attach** variant — no SAS expander,
  no SES. DriveForge falls back to `virtual_bays=8`. Slot LED control
  (sg_ses) is not available on this chassis.
- **HBA**: PERC H710 crossflashed to LSI 9207-8i IT mode ✓
- **Deploy workflow**: `rsync` source from dev Mac → `~/driveforge/` on
  server via tar-over-ssh; then `sudo ./scripts/install.sh` reinstalls
  non-editable + restarts daemon. ~30s per iteration.

## Real-hardware bugs found + fixed today

1. `/etc/driveforge/` was root-owned — now owned by driveforge user
2. `driveforge` user added to `disk` + `cdrom` groups for /dev/sdX access
3. SMART self-test "not supported" was misclassified as failure due to
   `bool(None) = False` coercion — now tri-state (True/False/None)
4. SAS self-test parsing used `entry.status` (doesn't exist) — fixed to
   `entry.result`; captured real JSON as fixture
5. SATA drives on SAS HBAs reported as tran=sas by lsblk; erase dispatch
   picked sg_format → "Illegal request". Now `detect_true_transport()`
   probes smartctl in the erase path only (not dashboard hot path).
6. systemd `ProtectHome=true` + `pip install -e` from `/home` clashed —
   reverted to non-editable pip install; ProtectHome stays on.
7. Abort cancelled asyncio tasks but orphaned subprocess children —
   `process.kill_owner(serial)` now SIGTERMs + SIGKILLs on abort.

## Known-risky SAS quirk (documented in UI)

**`sg_format` is NOT safely abortable mid-flight** on SAS drives. Once
the SCSI FORMAT UNIT command is on the wire, interrupting it leaves the
drive with "Medium format corrupted" and requires a manual `sudo
sg_format --format /dev/sdX` (15-60 min) to recover. The New Batch form
warns about this explicitly.

## Drives being used for testing

- Seagate ST300MM0006 (S0K2BSJC) — 300GB SAS 10K, 71k POH; doesn't
  support short self-test; needed sg_format recovery after JT's first
  abort test
- Seagate ST300MM0006 (S0K2BARS) — second unit of same model
- Intel SSDSC2BB120G4 (CVWL431600NS120LGN) — 120GB SATA SSD via SAS HBA;
  successfully erased via hdparm → Grade B (1 reallocated sector)

## What's *not* yet validated on real hardware

- Full-mode pipeline (badblocks + long self-test) — only quick-mode tested
- Thermal printer integration (no printer physically connected)
- Grading with real failure cases (all tested drives graded A or B)
- Webhook dispatch against a real n8n / endpoint
- Multi-drive parallel batch (only single-drive batches so far)
