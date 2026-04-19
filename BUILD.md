# DriveForge — Enterprise Drive Refurbishment Pipeline

## Purpose of this file

Context handoff for a Claude Code session building **DriveForge**, a Debian-based
Python application that turns a dedicated Dell PowerEdge R720 into an in-house
drive refurbishment workstation. Pulled enterprise drives go in, get secure-
erased + burned in + SMART-validated + graded, and come out with a printed
certification label ready for homelab deployment.

**Session owner**: JT (`jthompson4862@gmail.com`)
**License**: MIT
**Start a new Claude Code session pointed at this file**:

```
cd /Users/jt/Homelab/driveforge
# In Claude Code:
# Read @BUILD.md
```

---

## Project Vision

JT has a local source (a business that rips out old enterprise infrastructure)
that supplies pulled hard drives at near-zero cost. Commercial refurbishers
charge ~$18/TB retail — at scale that's thousands of dollars of markup for a
process you can run yourself in a corner of the homelab.

DriveForge is the **in-house refurbishment pipeline** that turns those pulled
drives into trusted, graded, certified drives ready for the homelab Ceph
cluster, NAS, or cold storage. Volume fluctuates: sometimes a one-off drive,
sometimes batches of thirty. No commercial pressure — timing doesn't matter,
but the process needs to run unattended and reliably.

**Workflow**: plug drives into the R720, tell DriveForge to process them, walk
away. A few days later each drive has a grade (A/B/C/fail), a printed adhesive
cert stuck to it, and a matching inventory record in Twenty CRM.

**Why an app, not a bootable OS**: an earlier version of this plan called for a
ShredOS-style bootable USB distro (Buildroot, custom kernel, minimal image).
That tradeoff only pays off if you want portability across unknown hardware.
DriveForge runs on one dedicated R720 forever, so the portability benefit
evaporates and the cost — slow Buildroot iteration, rebuild-ISO for any
change, painful integration with Twenty CRM / n8n / thermal printer — is pure
overhead. Debian + Python app is ~5x faster to build and trivially extensible.

---

## Target Hardware

- **Server**: Dell PowerEdge R720 (2U rackmount), dedicated to this project
- **PERC H710**: already crossflashed to IT mode (9207-8i equivalent) — SMART
  and SAS pass-through work natively
- **Drive bays**: 8x 3.5" LFF front bays for drives under test
- **Batch capacity**: 8 drives processed in parallel per round; larger batches
  (up to ~30) processed in multiple sequential rounds
- **Boot drive**: small SSD in an internal bay or rear slot — NOT one of the 8
  front bays, those are reserved for drives under test
- **Thermal printer**: Brother QL-820NWBc (USB + Ethernet + WiFi, 300 dpi,
  auto-cutter). Uses Brother DK-1209 die-cut labels (29×62mm / 1.1"×2.4",
  800/roll) — adhesive, stick directly to the drive, survive the inventory
  shelf. Driven over USB from the R720.
- **Network**: R720 on the homelab LAN; reachable from laptop/phone

---

## Architecture

### Base system
- **Debian 12 "Bookworm"** minimal install (no desktop environment)
- **Python 3.11+** for the application layer
- **systemd** for service management
- **SQLite** for local test history + in-flight state
- **Twenty CRM** as authoritative inventory after results sync

### Process layout

Three logical components, one codebase:

| Component | Role |
|---|---|
| `driveforge-daemon` | systemd service. Owns orchestration, DB, printer, CRM sync. Exposes REST API on `localhost:8080`. |
| `driveforge-tui` | Textual-based TUI. Talks to daemon via REST. For when you're at the crash cart or the web UI is down. |
| `driveforge-web` | FastAPI + HTMX web UI served by the daemon. Primary interface. Reachable at `http://driveforge.local` on the LAN. |

Separating daemon from UI means tests keep running whether or not anyone is
watching, and both UIs are thin clients over the same REST surface — no
duplicated logic.

### System package dependencies

| Package | Purpose |
|---|---|
| `smartmontools` | SMART reads + short/long self-tests |
| `hdparm` | ATA Secure Erase for SATA |
| `sg3-utils` | `sg_format` for SAS secure erase (hdparm doesn't cover SAS) |
| `nvme-cli` | NVMe format + firmware download/commit + health logs |
| `e2fsprogs` (badblocks) | Destructive surface scan |
| `fio` | Endurance / pattern testing (Phase 5+ workloads) |
| `tmux` | Per-drive parallel session management |
| `lshw`, `lsblk`, `lsscsi` | Hardware and block device discovery |

### Python stack

| Library | Purpose |
|---|---|
| `textual` | TUI framework (modern, async, better than urwid) |
| `fastapi` + `uvicorn` | Daemon REST API + web UI |
| `jinja2` | Web templates |
| `htmx` (client-side) | Dynamic web updates without an SPA |
| `httpx` | Async HTTP to Twenty CRM / n8n |
| `sqlalchemy` + `alembic` | DB ORM + migrations |
| `pydantic` | Type-safe models + config validation |
| `brother_ql` | Brother QL raster driver via raw USB (skips CUPS overhead) |
| `qrcode` | QR generation for cert labels |
| `pytest` | Testing |

---

## Test Workflow

Per-drive pipeline. Each drive runs inside a tmux session named
`driveforge-<serial>` so the daemon can attach/detach without killing work.
Every phase is idempotent and resumable across reboots.

```
Phase 1: Pre-test SMART snapshot (baseline)
Phase 2: SMART short self-test (~2 min)
Phase 3: Firmware check + (NVMe only for MVP) auto-update if newer known-good
Phase 4: Secure erase
           SATA: hdparm --security-erase
           SAS:  sg_format --format
           NVMe: nvme format -s 1
         Time: minutes (NVMe) to hours (large SATA)
Phase 5: badblocks destructive write/read
         Time: 24-48 hours per 8TB drive
Phase 6: SMART long self-test (10-20 hours)
Phase 7: Post-test SMART snapshot
Phase 8: Diff analysis → grade → print cert → Twenty CRM sync
```

Typical per-drive cycle: 3-5 days for 8TB+ HDDs, hours for NVMe.

---

## Grading System

Commercial refurbishers grade drives in tiers because reality isn't binary.
DriveForge assigns A/B/C/fail based on SMART attributes and test outcomes.
Thresholds live in `/etc/driveforge/grading.yaml` and are user-tunable.

| Grade | Description | Typical use |
|---|---|---|
| **A** | No reallocated sectors, no pending/offline-uncorrectable, all tests clean | Primary Ceph OSDs, TrueNAS main pool |
| **B** | Small number of stable reallocated sectors (≤8), no pending, all tests passed | Secondary OSDs, scratch pools, backup targets |
| **C** | More reallocated sectors but stable, all tests passed | Cold storage, test environments, heavy redundancy |
| **Fail** | Pending sectors, offline uncorrectable, test failures, or degradation between pre/post | Scrap / e-waste |

### Grading inputs

| Attribute | Grade A | Grade B | Grade C | Fail |
|---|---|---|---|---|
| Reallocated_Sector_Ct | 0 | ≤8 stable | ≤40 stable | >40 or increasing |
| Current_Pending_Sector | 0 | 0 | 0 | Any |
| Offline_Uncorrectable | 0 | 0 | 0 | Any |
| SMART short test | Pass | Pass | Pass | Fail |
| SMART long test | Pass | Pass | Pass | Fail |
| badblocks errors | 0 | 0 | 0 | Any |
| Any degradation pre→post | None | None | None | → Fail |
| Power_On_Hours delta | ≤ test duration + 1h | same | same | Wildly off → Fail |

Ceph's self-healing tolerates B/C drives cheaply, so the rig produces useful
output even from imperfect pulled stock.

---

## Certification Labels

Each completed drive gets a printed adhesive label stuck directly on it. The
Brother QL-820NWBc connects to the R720 via USB; DriveForge talks to it with
[`brother_ql`](https://github.com/pklaus/brother_ql) (raw USB, no CUPS).
Labels are Brother DK-1209 die-cut (29×62mm / 1.1"×2.4").

Example label layout:

```
DriveForge Certified
────────────────────
Model:    HGST HUS726T6TALE6L4
Capacity: 6.0 TB
Serial:   V8G6X4RL
Grade:    A
Tested:   2026-04-19
POH:      12,432 h

[QR code → Twenty CRM record]
```

The QR code encodes a URL to the drive's Twenty CRM `HardwareAsset` record,
with a fallback to the daemon's local report page if CRM is unreachable.

---

## Twenty CRM Integration

Each processed drive becomes a `HardwareAsset` record in Twenty CRM, synced in
Phase 8. Batches group drives processed together for inventory reporting.

`HardwareAsset` fields:
- `serial` (primary identifier)
- `model`
- `capacity_tb`
- `grade` (A/B/C/fail)
- `tested_at`
- `power_on_hours_at_test`
- `reallocated_sectors`
- `report_url`
- `batch_id` (FK to `RefurbBatch`)
- `source` (e.g., "LocalCo pull 2026-04-19")

`RefurbBatch` aggregates per-batch results: "batch of 15 pulled 2026-04-19 —
11 Grade A, 2 Grade B, 2 scrap." REST patterns are documented in
`~/.claude/projects/-Users-jt/memory/twenty-crm.md`; stick to them to avoid
re-deriving the response shapes.

---

## Firmware Updates

### MVP (Phase 1): NVMe only
NVMe firmware updates are standardized via the NVMe spec:
```
nvme fw-download -f firmware.bin /dev/nvmeN
nvme fw-commit -s <slot> -a <action> /dev/nvmeN
```

If DriveForge has a firmware blob matching the drive's model with a newer
known-good version, update it in Phase 3. Otherwise skip cleanly.

### Future (Phase 5-6): SATA/SAS best-effort
SATA and SAS firmware is vendor-specific and gated:
- Some vendors publish firmware (Seagate SeaChest, Intel/Solidigm, HGST via
  WDC's support site for older Ultrastars)
- Most Dell/HP-branded drives require support contracts
- Update mechanism: `sg_write_buffer` if the blob is available

Approach: build a lookup DB `model → known-good-firmware`, cache locally,
optionally community-contributed. Never update silently on errors. Phase 6+
stretch: publish the DB as a community resource alongside anonymized
drive-stats contributions.

---

## Repository Structure

```
driveforge/
├── README.md                        # User-facing docs
├── BUILD.md                         # This file — dev context
├── LICENSE                          # MIT
├── pyproject.toml                   # Python packaging
├── driveforge/
│   ├── __init__.py
│   ├── __main__.py                  # python -m driveforge
│   ├── cli.py                       # CLI entrypoints (Click)
│   ├── config.py                    # Pydantic config loader
│   ├── daemon/
│   │   ├── app.py                   # FastAPI daemon
│   │   ├── orchestrator.py          # Test pipeline driver
│   │   └── api.py                   # REST routes
│   ├── tui/
│   │   └── app.py                   # Textual TUI client
│   ├── web/
│   │   ├── routes.py                # HTMX-powered pages
│   │   ├── templates/
│   │   └── static/
│   ├── core/
│   │   ├── drive.py                 # Drive model + discovery
│   │   ├── smart.py                 # smartctl wrapper + parser
│   │   ├── erase.py                 # Secure erase (SATA/SAS/NVMe)
│   │   ├── badblocks.py             # badblocks wrapper
│   │   ├── grading.py               # A/B/C/fail logic
│   │   ├── firmware.py              # NVMe updates + lookup
│   │   ├── printer.py               # Brother QL raster cert printing
│   │   └── crm.py                   # Twenty CRM client
│   └── db/
│       ├── models.py                # SQLAlchemy schemas
│       └── migrations/              # Alembic
├── systemd/
│   ├── driveforge-daemon.service
│   └── driveforge-tui.service       # tty1 auto-launch
├── scripts/
│   ├── install.sh                   # Debian host bootstrap
│   └── r720-provision.yml           # Ansible playbook (optional)
├── config/
│   └── grading.yaml.example
├── docs/
│   ├── architecture.md
│   ├── hardware-compat.md
│   ├── printer-setup.md
│   ├── usage.md
│   └── troubleshooting.md
└── tests/
    ├── unit/                        # Against recorded SMART fixtures
    └── integration/
```

---

## MVP Milestones

| Phase | Goal | Duration |
|---|---|---|
| **1** | Daemon skeleton; drive discovery; runs smartctl + badblocks + erase on one drive; logs to `/var/log/driveforge` | 1 week |
| **2** | Textual TUI drives daemon via REST; 8-drive parallel orchestration via tmux | 1 week |
| **3** | Grading logic + pre/post SMART diff; writes reports; first real A/B/C verdicts | 1 week |
| **4** | Thermal printer + cert label design; auto-prints on completion | 3-4 days |
| **5** | Twenty CRM sync (HardwareAsset + RefurbBatch); n8n webhook on batch complete | 3-4 days |
| **6** | Web UI (FastAPI + HTMX) as primary interface; TUI becomes fallback | 1 week |
| **7+** | NVMe firmware auto-update; SATA/SAS firmware lookup DB; Cloudflare Tunnel for remote; public release | ongoing |

Total to usable MVP (Phases 1-5): ~4 weeks of focused time.

---

## Development Environment

Dev happens locally on macOS (this working directory), deploys to the R720 for
real drive testing.

**Local dev (macOS)**:
- Python 3.11+ via `pyenv` or Homebrew
- Dev dependencies via `uv` or pip in a virtualenv
- Unit tests run without drives using mocked smartctl / nvme output fixtures
- TUI developed against recorded drive-state fixtures

**Integration / real testing (R720)**:
- Debian 12 installed via iDRAC virtual media or USB installer
- `ansible/r720-provision.yml` installs system packages + systemd units
- Deploy via `rsync` from macOS or a GitHub Actions pipeline
- Real drives plugged into the 8 LFF bays; SMART output captured as new
  fixtures to expand the unit-test corpus

---

## Prior Art

- **[disk-burnin.sh](https://github.com/Spearfoot/disk-burnin-and-testing)** —
  the bash pipeline that's the direct inspiration for DriveForge's test logic.
  We reimplement in Python rather than vendor the shell script.
- **[Scrutiny](https://github.com/AnalogJ/scrutiny)** — web UI for SMART
  monitoring. Reference for UI patterns and SMART attribute visualization.
- **smartmontools** — the backbone. All drive testing funnels through
  `smartctl` eventually.
- **ShredOS** — our original reference for a bootable approach. Architecture
  diverged when we decided on a dedicated-server app instead of a portable
  ISO, but the test-flow inspiration still applies.

---

## Integration Ideas (stretch, post-MVP)

### Homelab (easy, Phase 7+)
- **n8n workflow** triggered on batch completion → summary email / Slack
- **Cloudflare Tunnel** so the web UI is reachable from anywhere
- No ArgoCD — DriveForge is inherently tied to physical R720 hardware, not
  a Kubernetes workload

### Community-facing (ambitious)
- **Public drive-stats DB** — anonymized SMART + failure data across
  contributors, Backblaze-style but crowdsourced from homelab refurbers
- **Firmware lookup service** — community-maintained DB of known-good SATA/SAS
  firmware blobs, filling the gap for drives whose vendors gate firmware
- **Public release** of DriveForge itself under MIT once MVP is stable

---

## Open Questions

1. **R720 boot drive**: repurpose an existing SSD or buy new? A 120GB
   Samsung/Kingston is ~$20 and gives headroom for logs + OS.
2. ~~**Thermal printer model**~~ — **Resolved**: Brother QL-820NWBc + DK-1209
   die-cut labels (29×62mm).
3. **Drive pool sizing**: How many Grade A drives to stockpile before starting
   the Ceph cluster build? Shapes how aggressively to process pulls.
4. **Public release timing**: open-source from day one (nothing to hide, MIT
   already chosen) or keep private until MVP is polished?

---

## Next Steps for New Session

Starting fresh:
1. Read this doc + memory files at `~/.claude/projects/-Users-jt/memory/`
2. Initialize Python project structure (`pyproject.toml`, `driveforge/`
   package, MIT LICENSE)
3. Begin Phase 1: daemon skeleton + single-drive test pipeline against
   recorded SMART fixtures

Not starting fresh — if prior work exists, check `git log` and existing code
before proposing next actions. This BUILD.md is a snapshot of the current
plan; reality may have moved past it.
