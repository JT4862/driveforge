# DriveForge — Enterprise Drive Refurbishment Pipeline

## About this document

Canonical design document for **DriveForge** — a Debian-based Python
application that turns a dedicated Dell PowerEdge R720 (or comparable
hardware) into an in-house drive refurbishment workstation. Pulled
enterprise drives go in, get secure-erased + burned in + SMART-validated
+ graded, and come out with a printed certification label ready for
homelab deployment.

Read alongside the code when making non-trivial changes. User-facing
install instructions live in [README.md](README.md); this file covers
architecture, phase plan, and the *why* behind each decision.

- **License**: MIT
- **Repository**: <https://github.com/JT4862/driveforge>

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
cert stuck to it, a matching record in the local DB, and a report page
served at `http://driveforge.local/reports/<serial>`.

**Why an app, not a bootable OS**: an earlier version of this plan called for a
ShredOS-style bootable USB distro (Buildroot, custom kernel, minimal image).
That tradeoff only pays off if you want portability across unknown hardware.
DriveForge runs on one dedicated R720 forever, so the portability benefit
evaporates and the cost — slow Buildroot iteration, rebuild-ISO for any
change, painful integration with the thermal printer and outbound webhooks — is pure
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
- **SQLite** as authoritative local store for drives, batches, telemetry
- **Generated HTML reports** served locally at `http://driveforge.local/reports/<serial>` — no external inventory system required

### Process layout

Three logical components, one codebase:

| Component | Role |
|---|---|
| `driveforge-daemon` | systemd service. Owns orchestration, DB, printer, outbound webhook dispatch. Exposes REST API on `localhost:8080`. |
| `driveforge-tui` | Textual-based TUI. Talks to daemon via REST. For when you're at the crash cart or the web UI is down. |
| `driveforge-web` | FastAPI + HTMX web UI served by the daemon. Primary interface. Reachable at `http://driveforge.local:8080` on the LAN (see Network & Access). |

Separating daemon from UI means tests keep running whether or not anyone is
watching, and both UIs are thin clients over the same REST surface — no
duplicated logic.

### Hotplug & Hardware Events

The daemon owns a udev subscription (via `pyudev`) that reacts to kernel
hardware events in real time. One monitor serves every hotplug use case:

| Event | Daemon response |
|---|---|
| USB device added, VID `0x04F9` (Brother) | Identify model via PID, auto-configure printer, push UI notification ("Printer QL-810W connected"), flush any pending label queue |
| USB device removed (printer) | Mark printer unavailable; pending cert labels queue to `/var/lib/driveforge/pending-labels/` |
| Block device added (`/dev/sdX`) | Offer to add drive to the active batch; update dashboard bay card |
| Block device removed mid-test | Abort the drive's pipeline cleanly, mark as "pulled during test" |

Design implication: **no feature requires a config file restart**. Plug in
a printer after first-run setup → it's usable within a second. Swap to a
different Brother QL model → it's auto-detected and reconfigured.

### Enclosure & Bay Detection

Server-class hardware (R720 backplane, MD1200, most storage shelves)
exposes a SAS enclosure over **SES (SCSI Enclosure Services)**. The
Linux kernel surfaces this at `/sys/class/enclosure/<name>/`:

```
/sys/class/enclosure/0:0:32:0/
├── device/{vendor,model,scsi_generic/sg3}
├── id                          # logical WWN
├── Slot_00/
│   ├── slot                    # "0"
│   ├── status                  # "OK" | "Unknown"
│   └── device/block/sda/       # populated slot → block device
├── Slot_01/ …
└── Slot_07/
```

`driveforge.core.enclosures` enumerates this tree on daemon boot and
whenever a udev event reshuffles drives. Each physical slot becomes a
tracked bay identified by a stable `bay_key`:

| Format | Meaning |
|---|---|
| `e<enc_idx>:s<slot>` | Real enclosure slot (e.g. R720 slot 3 → `e0:s3`) |
| `v<n>` | Virtual bay — used only when no SES enclosure was found |
| `u:<serial>` | Unbayed drive — NVMe or direct-attached SATA not in any enclosure |

**Virtual bay fallback**: on consumer PCs or NVMe-only rigs where no SES
enclosure exists, DriveForge generates `virtual_bays` (default 8, editable
in Settings → Daemon) slots that drives are assigned to in insertion
order. Mixing real + virtual is **not** supported — any single real
enclosure disables the virtual-bay fallback entirely.

**Multi-enclosure**: plug in an MD1200 via SAS cable → next `refresh_bay_plan()`
picks it up, dashboard now renders two enclosure sections totaling
R720 slots + MD1200 slots. No user action required.

**Unbayed section**: drives without an enclosure association (NVMe, USB,
direct SATA on a consumer motherboard) render in their own section below
the enclosure grid so they're visible but clearly flagged as outside the
bay-slot semantics.

**Slot LED control** (Phase 7+ stretch): SES enclosures support setting
ident / fault LEDs via `sg_ses --set=FAIL,1 --index=0,<slot>`. When a
drive fails grading, DriveForge can light its fault LED so the operator
knows exactly which bay to pull from a 20+ bay rack. Not wired in the
MVP — noted for the first real-hardware shakedown.

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
| `avahi-daemon` | mDNS — advertises `driveforge.local` on the LAN |
| `ipmitool` | Chassis telemetry (watts, temps) via local iDRAC |

### Python stack

| Library | Purpose |
|---|---|
| `textual` | TUI framework (modern, async, better than urwid) |
| `fastapi` + `uvicorn` | Daemon REST API + web UI |
| `jinja2` | Web templates |
| `htmx` (client-side) | Dynamic web updates without an SPA |
| `httpx` | Async HTTP for outbound webhook dispatch |
| `sqlalchemy` + `alembic` | DB ORM + migrations |
| `pydantic` | Type-safe models + config validation |
| `brother_ql` | Brother QL raster driver via raw USB (skips CUPS overhead) |
| `pyudev` | libudev bindings — hotplug detection (Linux extra; no-op on macOS) |
| `cryptography` | Ed25519 signature verification for firmware blobs |
| `python-multipart` | FastAPI form-body parser |
| `qrcode` + `pillow` | QR generation + label image composition |
| `pytest` | Testing |

---

## User Interface

### Guiding principle

DriveForge is a **start-it-and-walk-away** tool: a batch runs for days, humans
glance at it once or twice a day. The primary screen is therefore a live
dashboard of bay state, not a menu tree. Both the TUI (Textual) and web UI
(HTMX) are thin clients over the same daemon REST surface, so screen flows
are shared.

### Navigation (tabs / left rail)

1. **Dashboard** — live state of the 8 bays (home)
2. **Batches** — active + historical batch list
3. **History** — all drives ever processed, searchable
4. **Settings** — grading thresholds, printer, outbound webhook, Cloudflare Tunnel
5. **System** — daemon health, logs, R720 vitals, printer status

### Dashboard

Eight bay cards in a grid. Empty bays grayed out with a "Scan for drive"
affordance. A primary **[+ New Batch]** button at the top, a corner
**[Abort All]** with confirm modal for emergencies. Per-card content:

```
┌─ Bay 3 ──────────────────┐
│ HGST HUS726T6TALE6L4     │
│ SN: V8G6X4RL  • 6.0 TB   │
│ Phase 5: badblocks (2/4) │
│ ████████░░░░ 68%  ETA 9h │
│ SMART: ● clean           │
└──────────────────────────┘
```

### Drive detail (click a card)

- Full SMART table: pre-test vs current, deltas highlighted
- Live log tail from the tmux session
- Phase timeline — done / running / queued
- Live telemetry charts (temperature over test run, see Telemetry below)
- Actions: **Abort**, **Pause**, **Restart current phase**, **Attach to
  tmux** (TUI only), **Print label** (if graded), **Override grade** (with
  required note, Phase 6+ only)

### Batches / History / Settings / System

- **Batches**: list with A/B/C/F breakdown per batch; drill-in shows drives;
  actions: **Export CSV**, **Re-fire webhook**, **Reprint any label**
- **History**: flat searchable drive table (serial/model/grade/date/batch);
  read-only detail view with prominent **Reprint label** button
- **Settings**: three panels — Grading (in-app threshold editor), Printer
  (model dropdown, label-roll picker, test print, template preview,
  paper-out diagnostic), Integrations (outbound webhook URL, Cloudflare
  Tunnel, firmware DB source). **All config lives in the UI**; YAML files
  on disk are written by the daemon and not intended for hand-editing.
- **System**: daemon status, DB size, printer status, R720 vitals, last 50
  errors with drive context

### Keybinds (TUI and web)

`d` dashboard · `b` batches · `h` history · `,` settings · `?` help · `Esc` back

### First-Run Setup Wizard

On initial daemon start (no existing config), opening the web UI lands on
a setup flow instead of the dashboard. Every step has sensible defaults
and a **Skip** option, and the wizard auto-detects wherever it can rather
than ask:

1. **Welcome** — brief explanation of what happens next
2. **Hardware & network discovery** — lists detected bays from `lsscsi` /
   `lsblk`, confirms the HBA mode, reports iDRAC/IPMI availability, shows
   current IP / hostname / static-or-DHCP status; warns if on DHCP because
   the access URL may change
3. **Printer** — USB scan via udev for Brother VID `0x04F9`; pre-fills
   the model and loaded label roll if detected; inline **Test print**
   button. Skippable — plug in a printer later and the hotplug monitor
   auto-configures it.
4. **Grading** — defaults preselected from shipped `grading.yaml.example`;
   inline editor with "Reset to defaults"
5. **Optional integrations** — outbound webhook (for n8n / Zapier / custom
   endpoints) and Cloudflare Tunnel. Each has a **Test** button and is
   individually skippable.
6. **Done** — lands on the dashboard, ready to run a batch

### Printer Compatibility

Any printer supported by the [`brother_ql`](https://github.com/pklaus/brother_ql)
library works. Users pick from a dropdown in Settings — no file editing.

| Tier | Model | Connectivity | Notes | ~Price |
|---|---|---|---|---|
| Budget | QL-800 | USB | 300 dpi, auto-cutter | $80 |
| Budget+ | QL-810W | USB + WiFi | 300 dpi, auto-cutter | $120 |
| **Mid (recommended)** | **QL-820NWBc** | USB + Ethernet + WiFi + BT | 300 dpi, LCD, label sensor | $130 |
| Wide format | QL-1100 | USB | Up to 4" rolls | $170 |
| Wide format+ | QL-1110NWBc | USB + Ethernet + WiFi + BT | Up to 4" rolls | $300 |

Older `brother_ql`-compatible models (QL-500/550/700/710W/720NW/1050/1060N)
appear in the dropdown as "untested — YMMV".

### Label Rolls

Brother QL-820/810/1100 printers have a **label sensor** that reports which
DK roll is loaded. The daemon reads this and picks the matching cert
template automatically — the user just loads a roll and prints.

| Roll | Size | Template | Use case |
|---|---|---|---|
| **DK-1209** | 29×62mm die-cut | Standard (default) | 3.5" HDDs (recommended) |
| DK-1208 | 38×90mm die-cut | Large (adds thermal chart thumbnail + POH) | Max info on 3.5" drives |
| DK-1201 | 29×90mm die-cut | Longer | Extra text fields |
| DK-1221 | 23×23mm square | Compact (QR + serial only) | 2.5" SSD faces |
| DK-22210 | 29mm continuous | Standard, cut to template length | Power users, DIY sizing |

Printers without a label sensor (QL-800, QL-1100) fall back to a manual
dropdown in Settings. Anything outside this list falls back to a
"best-effort" render with a warning in the UI.

### Explicitly out of MVP

- **Grade override**: invites gaming the rubric. Added only when a genuine
  false-grade case shows up.
- **Tmux attach**: TUI-only feature; painful to implement in web, not worth it.

---

## Telemetry

Per-drive and chassis-level signals collected across a test run. Stored in
SQLite as a simple time-series table (one row per drive per poll, ~30s
interval) and exposed as line charts in the drive detail + dashboard views.

| Signal | Source | Granularity | Purpose |
|---|---|---|---|
| Drive temperature (°C) | SMART attrs 190/194 via `smartctl` | Per drive, 30s | Spot overheating drives mid-run |
| Drive airflow temp (°C) | SMART attr 190 where available | Per drive, 30s | Cross-check inlet vs drive temp |
| Chassis power (W) | `ipmitool` against local iDRAC | Server-wide, 30s | Batch-level power cost reporting |
| Derived: power-hours per drive | chassis_watts apportioned across active drives × duration | Per drive, per phase | Rough per-drive energy attribution |

True per-bay wattage is not measurable on stock R720 hardware (no per-bay
instrumentation on the backplane) and is explicitly out of scope. If ever
needed, a shelf of inline USB/SATA power meters could be added as a Phase
8+ stretch.

Telemetry data feeds:
- **Drive detail page**: temp + derived power-hours chart for that drive
- **Dashboard**: small sparkline per bay card (current temp trend)
- **Batch complete report**: total kWh drawn, peak/avg chassis power
- **Grading**: optional thermal-excursion flag if any drive exceeded a
  configured temp ceiling during test (threshold in `grading.yaml`)

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
Phase 8: Diff analysis → grade → print cert → fire outbound webhook (if configured)
```

Typical per-drive cycle: 3-5 days for 8TB+ HDDs, hours for NVMe.

---

## Grading System

Commercial refurbishers grade drives in tiers because reality isn't binary.
DriveForge assigns A/B/C/fail based on SMART attributes and test outcomes.
Thresholds are tunable in-app via Settings → Grading.

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

Each completed drive gets a printed adhesive label stuck directly on it.
DriveForge talks to the printer via [`brother_ql`](https://github.com/pklaus/brother_ql)
(raw USB, no CUPS). Supported printer models and label rolls are listed
under User Interface → Printer Compatibility / Label Rolls. The reference
configuration is a Brother QL-820NWBc with DK-1209 die-cut rolls
(29×62mm / 1.1"×2.4").

### Pending-label queue

Because tests run unattended for days, the printer may be offline or
disconnected when a drive finishes grading. Rather than fail, the daemon:

1. Renders the label to PNG and saves it to `/var/lib/driveforge/pending-labels/<serial>.png`
2. Shows a pending-labels badge in the UI ("3 labels waiting to print")
3. On printer reconnect (udev event) → automatically flushes the queue
4. Settings → Printer also exposes a manual **[Print All Pending]** button
   and per-label reprint from drive history

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

[QR code → local report page]
```

The QR code encodes a URL to the drive's **public report landing page**
served by DriveForge itself (see Phase 6). No external service required.
The landing page is a read-only view of the drive's cert: model, serial,
grade, test date, key SMART attributes, and thermal chart from the test
run. Served from the local daemon at `http://driveforge.local/reports/<serial>`
and optionally exposed externally via Cloudflare Tunnel so anyone with the
label can scan and verify.

---

## Inventory & External Integration

DriveForge is **self-contained by default**. Drive records, batch history,
and grading results live in the local SQLite DB and are served by the web
UI at `http://driveforge.local`. There is no dependence on any external
inventory system, CRM, or hosted service.

For users who want results pushed elsewhere (CRM, warehouse software,
Notion, Airtable, Slack, email), the daemon fires a single outbound
webhook on batch completion. Routing the payload to any specific system
is the user's responsibility — typically via n8n, Zapier, or a small
script. DriveForge itself is payload-agnostic.

Sample webhook payload:

```json
{
  "event": "batch.complete",
  "batch_id": "01HT...",
  "source": "LocalCo pull 2026-04-19",
  "totals": {"A": 11, "B": 2, "C": 0, "fail": 2},
  "drives": [
    {
      "serial": "V8G6X4RL",
      "model": "HGST HUS726T6TALE6L4",
      "capacity_tb": 6.0,
      "grade": "A",
      "tested_at": "2026-04-19T14:32:11Z",
      "power_on_hours": 12432,
      "reallocated_sectors": 0,
      "report_url": "http://driveforge.local/reports/V8G6X4RL"
    }
  ]
}
```

### Local DB schema (canonical)

The SQLite DB is the source of truth. Relevant records:

- **Drive**: serial (PK), model, capacity_tb, first_seen_at
- **TestRun**: drive_serial (FK), batch_id (FK), started_at, completed_at,
  grade, power_on_hours_at_test, reallocated_sectors, report_url
- **Batch**: id (PK), source, started_at, completed_at, notes
- **TelemetrySample**: drive_serial (FK), phase, ts, temp_c, chassis_w

Every webhook payload is derived from these tables; the webhook is a
projection of local state, never authoritative.

---

## Firmware Updates

**Scoped to display-only.** DriveForge reads the drive's current firmware
version via lsblk's `REV` field on discovery and logs it during Phase 3 of
the pipeline. It does not look up newer versions, download blobs, or
apply firmware. Updates are an explicit manual step by the operator using
vendor tools when they have the blob.

### Why not auto-update?

An earlier design had a signed community firmware DB + per-pair approval
rows + canary drives + fail-closed safety gates. We ripped it out
mid-session. Reasons:

- **There is no public "apt repo" for drive firmware.** Every vendor
  distributes separately; most enterprise drives are gated behind support
  contracts; redistributing blobs is legally murky.
- **DriveForge's target scale is homelab** (~30 drives at a time, one
  operator). The defense-in-depth designed to stop a poisoned community
  DB from bricking fleets was overkill.
- **Existing tooling handles it fine** — when an operator has a firmware
  blob in hand, Seagate SeaChest / Intel `isdct` / `nvme-cli` / `hdparm
  --fwdownload` already do the job reliably. DriveForge duplicating that
  with a thin wrapper added little.

### What DriveForge does surface

- Current firmware version in Drive Detail → Hardware panel
- A log line during Phase 3: `firmware: <version> (manual updates only)`
- Nothing else — no "update available" prompts, no download UI, no
  approvals

### If firmware needs updating

Operator runs the vendor tool out-of-band, reboots or re-inserts the
drive, and re-runs a batch. The new firmware version will appear in the
next test run's record automatically.

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
│   │   ├── app.py                   # FastAPI daemon + lifespan + middleware
│   │   ├── orchestrator.py          # Test pipeline driver
│   │   ├── state.py                 # Shared DaemonState + BayPlan cache
│   │   └── api.py                   # REST routes under /api
│   ├── tui/
│   │   └── app.py                   # Textual TUI client
│   ├── web/
│   │   ├── routes.py                # HTMX-powered pages
│   │   ├── setup.py                 # First-run setup wizard (5 steps)
│   │   ├── templates/               # Jinja templates + setup/ subdir
│   │   └── static/                  # app.css, static assets
│   ├── core/
│   │   ├── drive.py                 # Drive model + lsblk discovery
│   │   ├── smart.py                 # smartctl wrapper + JSON parser
│   │   ├── erase.py                 # Secure erase (SATA/SAS/NVMe)
│   │   ├── badblocks.py             # badblocks wrapper
│   │   ├── grading.py               # A/B/C/fail logic + rationale
│   │   ├── firmware.py              # Lookup DB + apply + decide_apply
│   │   ├── signing.py               # Ed25519 verify for firmware blobs
│   │   ├── enclosures.py            # SES / sysfs enclosure detection
│   │   ├── printer.py               # Brother QL raster cert printing
│   │   ├── hotplug.py               # pyudev monitor (printer + drive events)
│   │   ├── telemetry.py             # ipmitool chassis power sampling
│   │   ├── reports.py               # Report payload model
│   │   ├── process.py               # Subprocess runner with fixture mode
│   │   └── webhook.py               # Outbound JSON webhook dispatch
│   ├── data/
│   │   └── firmware_db.yaml         # Bundled firmware lookup DB
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
    ├── fixtures/                    # Canned smartctl / lsblk / ipmitool output + synthetic sysfs
    ├── unit/                        # Against recorded fixtures — no hardware required
    └── integration/                 # For real Debian-VM runs
```

---

## Implementation Phases

| Phase | Goal | Status |
|---|---|---|
| **1** | Daemon skeleton; drive discovery; smartctl + badblocks + erase wrappers; logs to `/var/log/driveforge` | ✅ Real-hardware quick-mode validated on R720 |
| **2** | Textual TUI drives daemon via REST; parallel orchestration | ✅ Fixture-complete (real-hardware parallel batches untested) |
| **3** | Grading logic + pre/post SMART diff; telemetry collection (drive temp + chassis power); writes reports; real A/B/C verdicts | ✅ Working on real hardware (quick-mode Intel SSD → Grade B) |
| **4** | Thermal printer + cert label design; auto-prints on completion | ✅ Code + Pillow rendering complete; physical print pending printer purchase |
| **5** | Outbound webhook on batch complete (JSON POST to configured URL); local report page | ✅ Complete |
| **6** | Web UI (FastAPI + HTMX) — live dashboard, telemetry charts, public QR landing page, first-run setup wizard, editable Settings, quick-mode provisional marking | ✅ Complete; validated on R720 |
| **7** | ~~NVMe firmware auto-apply~~ | ❌ **Removed** — scope decision to keep firmware as manual-only. See Firmware Updates section. |
| **8+** | Slot LED control via sg_ses (requires SES-capable backplane); public OSS release | ongoing |

**Current status** (2026-04-19, end of session): 31 tests passing (down
from 44 after firmware rip-out). Real-hardware quick-mode validated on
R720 with Intel SSD (Grade B). Full-mode run on real hardware still
pending (takes 6+ hours on a 300GB SAS drive).

---

## Development Environment

Three tiers of environment, used for different work:

### Tier 1 — macOS (primary dev loop)

Day-to-day coding happens here. FastAPI + HTMX + Jinja + Textual are all
pure Python and platform-portable; no VM or container needed for the UI
iteration loop.

- Python 3.11+ via Homebrew
- `uv` for venv + dependency management (`brew install uv`)
- Unit tests run without drives against recorded `smartctl` /
  `nvme-cli` / `ipmitool` fixtures
- TUI developed against the same recorded fixtures

**Dev mode**: the daemon accepts a `--dev --fixtures <dir>` flag that
serves canned drive/batch/telemetry state instead of running real
orchestration. Web UI iteration loop:

```bash
uv venv && uv pip install -e .
driveforge-daemon --dev --fixtures tests/fixtures/
# open http://localhost:8080 in a browser; edit templates, refresh
```

**What to mock on macOS** (all Linux-only, all orchestration plumbing,
none of it blocks UI work):

| Thing | Reason | Mock approach |
|---|---|---|
| `pyudev` | Linux kernel udev only | Fake event fixtures |
| `brother_ql` USB | Needs physical printer | Use its file backend → PNG on disk |
| `smartctl` against `/dev/sdX` | No drives on Mac | Recorded output fixtures |
| `ipmitool` | Needs iDRAC | Recorded output fixtures |

### Tier 2 — Debian VM (integration testing)

When the work touches udev / systemd / real networking, run a local
Debian VM instead of going to the R720. **[Lima](https://github.com/lima-vm/lima)**
is the recommended tool (`brew install lima`) — lightweight Debian VMs
on macOS with automatic port forwarding to `127.0.0.1`. Multipass is an
acceptable alternative.

**Not Docker.** systemd in containers requires privileged mode, udev
doesn't work cleanly, and drive device access is absent. VMs are
purpose-built for this; containers fight you.

```bash
limactl start --name=driveforge-dev template://debian-12
limactl shell driveforge-dev
# inside: sudo apt install <system deps>; rsync code in; systemctl start driveforge-daemon
# browse http://127.0.0.1:8080 from macOS
```

### Tier 3 — R720 (real hardware)

For anything involving actual drives, SMART reads against physical disks,
or the thermal printer:

- Debian 12 installed via iDRAC virtual media or USB installer
- `scripts/r720-provision.yml` (Ansible) installs system packages + systemd units
- Deploy via `rsync` from macOS or GitHub Actions pipeline
- Real drives plugged into the 8 LFF bays; SMART output captured as new
  fixtures to expand the unit-test corpus

### Tier boundary rule

If a change can be validated with fixtures, do it in Tier 1. Only escalate
to Tier 2 or 3 when Linux-specific behavior or real hardware is actually
required. Most web UI iteration never leaves Tier 1.

---

## OSS Distribution

DriveForge is designed so any Debian 12 x86_64 server can run it with zero
dependence on JT's homelab infrastructure. Fresh installs work end to end
with local-only features; every external integration is opt-in.

### Default-empty integrations

| Setting | Default | Effect when empty |
|---|---|---|
| Outbound webhook URL | empty | No notifications fire; results stay local |
| Cloudflare Tunnel | not configured | QR landing page serves on local LAN only |
| Firmware lookup DB URL | bundled with app | Works out of the box; remote updates optional |

A fresh install prints cert labels, saves reports locally, and serves a
read-only report page on the LAN. Everything else is toggled on in Settings.

### Network & Access

DriveForge's web UI is reachable on the LAN via two URLs out of the box:

- **`http://driveforge.local:8080`** — advertised via mDNS (Avahi). Works on
  macOS, iOS, modern Linux, and Windows with Bonjour installed. Preferred.
- **`http://<server-ip>:8080`** — the raw LAN IP. Always works.

The daemon binds to `0.0.0.0:8080` by default. Port and bind address are
configurable in Settings → System (change requires a daemon restart; the
UI warns about the URL change before applying).

**Network config is not DriveForge's responsibility.** Static IP / DHCP /
DNS is handled at the Debian layer via `netplan`. The install script
detects DHCP and prints a friendly warning so users know the URL could
drift after a reboot. A Phase 7+ `driveforge network` CLI may later wrap
netplan for users who want a Proxmox-style static-IP wizard, but it is
explicitly out of MVP scope.

HTTPS: MVP is HTTP-only. Phase 6+ adds optional self-signed HTTPS on
port 8443, with Let's Encrypt via Cloudflare Tunnel as the path to a
trusted public cert for the QR landing page.

### End-user install flow

1. User installs Debian 12 on their own server hardware (OS out of scope)
2. SSH in and run one bootstrap command:
   ```bash
   curl -sSL https://raw.githubusercontent.com/JT4862/driveforge/main/scripts/install.sh | sudo bash
   ```
3. Script: `apt install` system deps, downloads latest `.deb` from GitHub
   Releases, installs it, writes default configs under `/etc/driveforge/`,
   enables and starts `driveforge-daemon.service`
4. Connect thermal printer via USB (optional — can be plugged in later;
   udev hotplug monitor auto-configures it)
5. Install script finishes with a Proxmox-style access summary:
   ```
   ✓ DriveForge installed and running.

   Open the web UI at:
     → http://driveforge.local:8080     (mDNS, preferred)
     → http://192.168.1.42:8080         (direct IP)

   ⚠  This server is on DHCP — the IP may change on reboot. For a stable
      URL, set a static IP via Debian's netplan config.
   ```
6. Opening either URL lands on the **First-Run Setup Wizard**, which
   auto-detects hardware and walks through optional integrations. No
   files to edit by hand, ever.

### What we are explicitly NOT shipping

- **Custom ISO / bootable image** — scope creep. Debian handles the OS.
- **Docker container** — drive testing needs direct `/dev/sdX` access and
  host-level tmux; container is the wrong abstraction.

### README contents (at public release)

- Hardware compatibility notes (HBA in IT mode, SATA/SAS/NVMe all supported,
  thermal printer optional)
- Supported printer models (see User Interface → Printer Compatibility)
- Link to Debian 12 download + install guide
- The one-line bootstrap install command
- Screenshots of the first-run setup wizard + dashboard
- Note that all configuration happens in the web UI (no file editing)
- **Firmware update limitations** — prominent section listing drive
  categories DriveForge cannot auto-update (OEM-branded Dell/HP/NetApp,
  Windows-only vendor tool drives, drives requiring physical power cycle,
  drives gated behind vendor support contracts). Mirror the Known
  Limitations list from the Firmware Updates section.

---

## Prior Art

### Open-source tools and references
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

### Commercial refurbishment pipelines

DriveForge's phase ordering mirrors what commercial hard-drive refurbishers
(Server Parts Deals, Water Panther, Bargain Hardware, etc.) actually run.
Direct mapping:

| DriveForge phase | Commercial equivalent |
|---|---|
| Pre-test SMART + short self-test | Intake triage / initial health screen |
| Firmware check/update | Same (most shops do it) |
| Secure erase (hdparm/sg_format/nvme format) | NIST 800-88 sanitization — same underlying commands |
| badblocks destructive scan | "Burn-in" / surface scan |
| Long self-test | Same |
| Pre/post SMART diff | Degradation check post-burn-in |
| Grading (A/B/C/Fail) | Tiered sorting — Recertified / Used Tested / Scrap |
| Cert label + QR | Barcode label + inventory record |

**What commercial shops do that DriveForge intentionally skips** — relevant
at industrial scale, noise at homelab scale:
- Robotic test benches for high-throughput automation
- Signed NIST 800-88 sanitization certificates with audit trail
- Warranty attachment (1–5 year commercial warranties)
- Vendor proprietary diagnostics (SeaChest, WD DLGDiag, HGST WinDFT)
- Thermal / vibration chamber testing
- Power-cycle aging protocols

**What DriveForge does better than commercial shops**:
- **Open, tunable grading rubric** — commercial rubrics are proprietary
- **Public QR cert pages** — anyone with the label verifies the grade, no login
- **Community drive-stats contribution** (Phase 8+) — anonymized failure
  data, Backblaze-style but crowdsourced from homelab refurbers

**What DriveForge will never do**: SMART counter reset / POH fakery. That
is gray-market fraud, not refurbishment.

---

## Integration Ideas (stretch, post-MVP)

### Homelab (easy, Phase 7+)
- **Route the outbound webhook to anything** — n8n workflow, Zapier, a
  Discord bot, a Python script that writes to a CRM or inventory system.
  The webhook ships in Phase 5; downstream routing is a user concern, not
  a DriveForge concern.
- **Remote admin access** via Cloudflare Tunnel — the public QR landing page
  ships in Phase 6; full admin UI exposure is a separate Phase 7+ decision
- **`driveforge network` CLI** — Proxmox-style static-IP wizard that wraps
  `netplan` with a preview-and-confirm flow. Phase 7+ nice-to-have; users
  who want it today can edit netplan directly.
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
4. ~~**Public release timing**~~ — **Resolved**: private repo during
   pre-alpha shakedown; flip to public after the first successful
   real-hardware batch run.

---

## Next Steps

Code is feature-complete for dev-mode. The remaining milestones are
hardware-dependent and sequenced as:

1. **Debian VM dry run** via Lima or Proxmox — prove `install.sh` works
   end to end on a clean Debian 12 install
2. **First R720 install** — pulls drives, plugs them in, runs a real
   batch. Capture any SMART / SES / printer-integration snags and fix.
3. **First printed label + first QR scan** — prove the cert artifact
   lifecycle is complete
4. **Wire firmware auto-apply** — orchestrator → `apply_nvme_firmware()`,
   gated on canary success. One approved entry at a time, carefully.
5. **Flip repo public** — once the above is done, when there's a story
   to tell
6. **Phase 8+** — SATA/SAS firmware lookup DB, slot LED control,
   community drive-stats contributions

This BUILD.md is a snapshot of the current design; when reality moves
past it, update it.
