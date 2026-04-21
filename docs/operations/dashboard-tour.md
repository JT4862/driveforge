---
title: Dashboard tour
---

# Dashboard tour

DriveForge's dashboard is intentionally a single screen. Drives that
are physically present appear; drives that aren't, don't. No virtual
slots, no empty placeholders, no "rack diagram" view. The LED on the
chassis is the physical pointer; the serial in the UI is the logical
identity.

This page walks through every visible element top-to-bottom.

## Header strip

Across the top, left-to-right:

- **DriveForge brand mark + word** — links back to `/`
- **Dashboard / Batches / History / Settings** tabs
- **Chassis telemetry strip** (right-aligned) — only renders on
  hardware that exposes BMC or sensor data:
  - **Power** in watts (from `ipmitool dcmi power reading`)
  - **Inlet temperature** in °C, color-banded
  - **Exhaust temperature** in °C, color-banded
  - Bands: **cool** < 30°C (blue), **normal** 30–45°C (green),
    **warm** 45–55°C (amber), **hot** ≥ 55°C (red)
- **+ New Batch** button — opens the batch-creation page
- **Auto-enroll mode pill** (Off / Quick / Full) — segmented control;
  click any pill to switch modes immediately. See
  [Auto-enroll](auto-enroll.md).

If your hardware has no BMC and no IPMI access, the chassis strip is
hidden — the dashboard adapts to the smaller header.

## Active section

Per-drive cards for every drive currently in a test pipeline. One
card per drive. Insertion order matches when the drive started its
run.

### Card layout (active)

```
┌─────────────────────────────────────────────────┐
│  Western Digital                       [Abort]  │  ← manufacturer (small, muted)
│  WDC WD1000CHTZ-04JCPV0                         │  ← drive model
│                                                 │
│  SN: WD-WXF1E62EJYE5 · 1.0 TB · 22°C            │  ← meta row
│  ⚡ secure erase                                 │  ← phase + icon
│  sg_format --format                             │  ← sublabel (mechanism)
│                                                 │
│  ████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  11%   │  ← progress bar
│  24m 19s · ETA ~1h 40m                          │  ← elapsed + ETA
└─────────────────────────────────────────────────┘
```

Elements:

- **Manufacturer + model** (stacked since v0.2.5 to avoid Abort-button
  collision)
- **Serial · capacity · live drive temperature** (temp pulled fresh
  from SMART each polling cycle; color-banded same as chassis)
- **Phase** with icon: `⚕` pre-SMART, `◐` short test, `⚙` firmware
  check, `⚡` secure erase, `🔥` badblocks, `⧖` long test, `★`
  grading, `↻` recovering, `✓` done
- **Sublabel** — phase-specific extra info. For erase: which mechanism
  (`hdparm --security-erase`, `sg_format --format`, `nvme format -s 1`).
  For badblocks: which pass (`pass 3/8 · write 0xFF`).
- **Throughput sparkline** — 30-second rolling I/O rate during
  high-throughput phases (badblocks). Hidden during phases that
  don't generate kernel-visible block I/O (secure erase, long test).
- **Progress bar** — phase-colored (info=blue, erase=amber,
  badblocks=purple, long_test=cyan, done=green, fail=red), animated
  with a continuous shimmer.
- **Elapsed + ETA** — wall-clock since phase start, plus calibrated
  per-phase ETA from capacity.
- **Per-card Abort button** — top-right corner. Disabled during
  `secure_erase` (see [Aborting drives](aborting-drives.md)).

### Visual states

- **Phase pulse** — 1.4-second one-shot border animation when a card
  transitions to a new phase. Glance-cue that the pipeline is
  progressing.
- **Recovery-mode amber glow** — persistent pulsating border (2.8s
  cycle) on cards whose serial is in `state.recovery_serials`.
  Indicates the drive is in a recovery-triggered pipeline (was pulled
  mid-run, recovered + restarted). Clears when the recovery pipeline
  exits.
- **Just-completed flash** — 1.8s glow when a card transitions out of
  Active back to Installed (green for pass, red for fail). Also a
  one-shot.

## Installed section

Per-drive cards for every drive that's physically present but **not**
currently in a pipeline. Sorted by serial.

### Card layout (installed)

```
┌─────────────────────────────────────────────────┐
│  Seagate                              [📄] [A]  │  ← grade badge if tested
│  ST3000DM001-1CH166                             │
│                                                 │
│  SN: Z1F248SL · 3.0 TB · SAS · 5.2y             │  ← meta + drive age
│  Grade A · tested 2026-04-19                    │  ← last-test summary
│                                                 │
│                                       [Ident]   │  ← LED ident toggle
└─────────────────────────────────────────────────┘
```

Elements:

- **Manufacturer + model** (same stacking as active cards)
- **Serial · capacity · transport · drive age** (age = power-on
  hours from last test; "5.2y" or "45k POH" depending on magnitude)
- **Last-test summary**:
  - Graded drives: `Grade X · tested YYYY-MM-DD`
  - Failed drives: `✗ Failed YYYY-MM-DD · <phase>` + first line of
    error message in muted text
  - Untested / aborted: `idle · never tested`
- **Grade badge** (top-right) — colored A/B/C/Fail pill. `*` superscript
  on quick-mode runs to indicate "burn-in skipped, not a full
  certification."
- **Cert PDF link** (📄, left of grade) — opens `/reports/<serial>`
- **Ident button** (bottom-right) — toggles the LED locate pattern.
  See [Identify LED](identify-led.md). Button label flips to **Stop**
  while ident is active.

## Drive detail page (`/drives/<serial>`)

Click any card → drive detail page. Shows:

- **Latest test run** — full phase log, timestamps, error message,
  SMART deltas
- **Hardware** — model, serial, capacity, transport, firmware,
  first-seen date
- **Phase log** — last ~40 lines of the in-flight log buffer
- **Telemetry** — drive temperature + chassis power as
  Chart.js line charts over the run's duration. Bucketed to ~1-min
  intervals for runs longer than 1 hour.
- **Full test history** — every prior TestRun for this drive's serial,
  with grade + completion timestamp + batch link

## Polling cadence

The dashboard auto-refreshes via HTMX every 2 seconds. Specific
elements:

- `/_partials/bays` — full drive grid, every 2s
- `/_partials/update-log` — only when an in-app update is in flight,
  every 2s, stops when systemd unit reports inactive

There's no WebSocket or SSE — HTMX polling is intentional for
simplicity and easy debugging via curl.

## What you don't see

Things deliberately absent from the dashboard:

- **Slot numbers / bay numbers.** The drive's serial is the identity;
  physical location is found via the LED ident button, not a UI
  diagram.
- **Empty bay placeholders.** A bay with no drive in it doesn't
  appear at all.
- **Enclosure groupings.** Drives are a flat list. Backplane
  topology lives in `Settings → Hardware` for reference, but doesn't
  drive UI grouping.
- **Drive-vendor logos / icons.** Manufacturer name is text-only.
- **Per-drive performance graphs in the dashboard.** Live throughput
  sparkline is intentionally compact; deeper telemetry is on the
  drive detail page.
