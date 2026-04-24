---
title: Auto-enroll
---

# Auto-enroll

Auto-enroll is the "drop a drive in a bay and walk away" feature. With
it on, every new drive you insert gets a pipeline started automatically
— no need to click **+ New Batch** for each one.

## The three modes

Toggle at the dashboard header pill. Daemon reads it live; no restart
needed when changing.

- **Standalone** installs: stored in `daemon.auto_enroll_mode` in
  `/etc/driveforge/driveforge.yaml` on this box.
- **Operator** installs (v0.10.9+): same — but the operator's value
  is the **fleet-wide** setting. Clicking Quick / Full propagates to
  every connected agent via the fleet WebSocket within seconds; agents
  ignore their own local config when in agent mode and use the
  operator's cached value. Agents that are offline when you click pick
  up the new value on reconnect via the handshake.
- **Agent** installs: the pill is not rendered on agents (they have
  no web GUI in v0.11+). They receive the value from their operator.
  Fail-closed: an agent with no cached operator value defaults to
  "Off" — pipelines never auto-start without the operator's green
  light.

| Mode | Pipeline run on insert |
|------|------------------------|
| **Off** | Nothing. Manually start batches via **+ New Batch**. |
| **Quick** | pre-SMART → short test → firmware check → secure erase → post-SMART → grade. **Skips badblocks + long self-test.** Useful for triage / quick certification on drives you've already burn-in tested. ~30 min for an SSD, ~1–2h for an HDD. |
| **Full** | Same as Quick + 8-pass badblocks + SMART long self-test. **The full certification pipeline.** Days per drive on multi-TB HDDs (8-pass × capacity-bound). |

## When auto-enroll fires

**Only on hotplug ADD events.** A udev `add` event for a block device
triggers `_handle_drive_added` in the daemon, which is where the
auto-enroll decision lives.

What this means in practice:

- **Drive already in the bay at daemon start** → no auto-enroll. The
  daemon doesn't sweep present drives at boot to start pipelines for
  them. (Pull + re-insert if you want to trigger one.)
- **Drive inserted while daemon is running** → auto-enroll evaluated.
- **Mode toggled from Off → Quick/Full while drives are sitting
  installed** → no retroactive enrollment. They stay idle until you
  pull and re-insert.

## What blocks auto-enroll

The hotplug ADD handler runs decisions in priority order:

1. **Recovery first.** If the drive has an open `interrupted_at_phase`
   TestRun (was pulled mid-pipeline), recovery dispatches first. After
   recovery completes, a fresh pipeline starts automatically as part
   of the recovery flow — using the same `quick`/`full` mode the
   interrupted run had.
2. **Blinker restore.** If the drive has a completed pass/fail run,
   its post-run LED blinker (heartbeat for pass, lighthouse for fail)
   restarts.
3. **Auto-enroll evaluation.** Last in the priority chain.

Auto-enroll is then **skipped** when:

- **Mode is Off.**
- **Drive is already in `state.active_phase`** (race protection — same
  drive can't be in two pipelines).
- **Drive has a graded most-recent run** (A/B/C/Fail). Indefinite
  skip — see below.

## The "graded drives are sticky" rule (v0.2.9+)

A drive that's already been tested with a real grade does NOT
auto-retest on re-insert, **regardless of how long ago the test
was**. The verdict is durable.

Pre-v0.2.9 had a 1-hour cutoff: re-inserting a Grade A drive 8 hours
later would auto-retest it. Operators reported this caused retest
churn on shelves of pre-tested drives. v0.2.9 dropped the cutoff.

To re-test a graded drive, click **+ New Batch** and select it
manually. That's the explicit operator-intent signal.

## The aborted-drive case (v0.2.7+)

Aborted runs (`phase="aborted"`, `grade=NULL`) **do** trigger
auto-enroll on re-insert. The abort was a cancellation, not a
verdict — re-inserting the drive is the operator's signal to give
it another shot.

Combined with the latest-run filter: a drive that passed Grade A
20 minutes ago and was aborted mid-retest 2 minutes ago will
correctly auto-enroll on re-insert (the abort is the latest row,
and aborts don't block).

## Why hotplug-only?

A periodic "scan present drives + auto-enroll" sweep was considered
and rejected:

- Operators sometimes leave drives idle in bays intentionally
  (waiting for a label printer, batching for end-of-day, etc.)
  — auto-running them on a timer would surprise.
- The hotplug signal is unambiguous: "this drive just appeared."
  Anything else needs operator confirmation.

If you want a previously-installed drive to auto-run, the workflow
is: pull it, re-insert it. Hotplug fires, decision happens.

## Recovery interaction

Recovery is the special case where auto-enroll fires even for a
drive that has an open interrupted run (not a graded one). The full
flow:

1. Drive pulled mid-`secure_erase` → TestRun left open with
   `interrupted_at_phase="secure_erase"`, `state.interrupted_serials`
   gets the serial
2. Drive re-inserted → hotplug ADD fires
3. `recover_drive()` finds the open interrupted run, closes it as
   `phase="interrupted"`, dispatches `_run_recovery` task
4. `_run_recovery` repairs drive state per-transport (SAS:
   complete the interrupted sg_format; SATA: SAT-passthrough unlock
   + disable; NVMe: no-op)
5. Recovery then calls `start_batch` with the same `quick` flag the
   interrupted run had — kicking off a fresh pipeline
6. Card on the dashboard shows a persistent amber-glow border for
   the entire recovery + restart duration

So even with auto-enroll **Off**, recovery + re-test happens on
re-insert of a pulled-mid-erase drive. That's intentional: leaving
a drive in a half-erased state is unsafe.

## Debugging "why didn't this drive auto-run?"

```bash
journalctl -u driveforge-daemon -f
```

Hotplug add events log at INFO level. Look for:

- `hotplug add: no matching drive for event` — udev fired but
  `lsblk` hadn't settled yet. Retried 5×0.4s; if all retries miss,
  the drive's first appearance to the daemon is via the next dashboard
  poll (not auto-enroll).
- `hotplug add: drive X entered recovery flow` — recovery took
  precedence; auto-enroll wasn't evaluated.
- `hotplug add: drive X has a graded completed run (A, ...);
  skipping auto-enroll` — the v0.2.9 sticky-graded rule.
- `hotplug add: auto-enrolling drive X (quick mode)` — fired as
  expected.

If you don't see ANY of these for a drive that's clearly been
inserted, the hotplug subsystem itself isn't seeing the event —
check `udevadm monitor --kernel` to confirm udev is observing the
ADD.
