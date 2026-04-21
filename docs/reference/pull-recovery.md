---
title: Pull and recovery state machine
---

# Pull and recovery state machine

The technical reference for DriveForge's pull-and-recover system.
For the operator-facing walkthrough, see
[Operations → Pull and recover](../operations/pull-and-recover.md).

## TestRun lifecycle states

A `TestRun` row goes through these states, encoded as `phase` +
`completed_at` + `interrupted_at_phase` + `grade`:

| State | `phase` | `completed_at` | `interrupted_at_phase` | `grade` |
|-------|---------|----------------|------------------------|---------|
| Active | `pre_smart` / `short_test` / etc. | NULL | NULL | NULL |
| Done | `done` | (timestamp) | NULL | `A` / `B` / `C` |
| Failed | `failed` | (timestamp) | NULL | `fail` |
| Aborted | `aborted` | (timestamp) | NULL | NULL |
| Interrupted (open) | (whatever phase was running) | NULL | (the phase) | NULL |
| Interrupted (closed by recovery) | `interrupted` | (timestamp) | (the original phase) | NULL |

A run is "open" while `completed_at IS NULL`. Open runs are either
**actively running** (their asyncio task is alive) or **interrupted**
(the task ended without setting `completed_at`).

## State transitions

```
   ┌─────────┐     start_batch          ┌────────┐
   │ (no run)│ ───────────────────────> │ Active │
   └─────────┘                          └────────┘
                                            │
                            ┌───────────────┼──────────────────┐
                            │               │                  │
                            ▼               ▼                  ▼
                       ┌─────────┐    ┌─────────┐    ┌──────────────┐
                       │  Done   │    │ Failed  │    │  Aborted     │
                       │ A/B/C   │    │  fail   │    │ grade=NULL   │
                       └─────────┘    └─────────┘    └──────────────┘
                            │
                            │  pull mid-pipeline
                            ▼
                  ┌─────────────────────┐
                  │ Interrupted (open)  │ <── waiting for re-insert
                  │ phase=current       │
                  │ completed_at=NULL   │
                  │ interrupted_at_     │
                  │   phase=current     │
                  └─────────────────────┘
                            │
                            │  re-insert + recover_drive()
                            ▼
                  ┌──────────────────────┐
                  │ Interrupted (closed) │
                  │ phase=interrupted    │
                  │ completed_at=now()   │
                  └──────────────────────┘
                            │
                            │  _run_recovery → start_batch (fresh)
                            ▼
                       (new TestRun in Active state, with
                        same quick flag as the interrupted one)
```

## Detection layers

Three independent detection paths, all converging on the same
"interrupted" state. Listed in priority order (first-match wins):

### 1. Hotplug REMOVE event (the happy path)

`_handle_drive_removed` in `daemon/app.py`:

```python
serial = event.serial  # or fall back to device_basenames reverse-lookup
orch._cancel_blinker(serial)
interrupted_phase = state.active_phase.get(serial)
if interrupted_phase is not None:
    state.interrupted_serials.add(serial)
    # stamp the open TestRun with interrupted_at_phase=interrupted_phase
```

The orchestrator's pipeline task (running in parallel) hits an
error from its current subprocess (the device file is gone).
`_looks_like_pull(drive)` is consulted; the
`state.interrupted_serials` set has the serial, so it returns True.
The except branch goes to `_flag_interrupted(drive, phase=...)`
which idempotently sets `interrupted_at_phase` (a no-op if the
remove handler already set it).

### 2. `_looks_like_pull` device-existence + serial-rediscovery

```python
def _looks_like_pull(self, drive: Drive) -> bool:
    if drive.serial in self.state.interrupted_serials:
        return True
    try:
        present = drive_mod.discover()
    except Exception:
        present = None
    if present is not None:
        if any(d.serial == drive.serial for d in present):
            return False  # serial still findable → NOT a pull
        return True  # discovery succeeded + serial gone → confident pull
    # discovery errored — fall back to path check
    return not os.path.exists(drive.device_path)
```

Used when the udev REMOVE event hasn't arrived yet (rare) or
arrived without a serial (also rare). Also handles the edge case
where the kernel re-enumerates a drive (new `/dev/sdX` letter)
after certain errors — serial-rediscovery distinguishes "still
present, just renumbered" from "actually gone."

### 3. Daemon startup sweep

`_flag_dangling_runs_as_interrupted` runs once at daemon boot:

```python
open_dangling = (
    session.query(m.TestRun)
    .filter(m.TestRun.completed_at.is_(None))
    .filter(m.TestRun.interrupted_at_phase.is_(None))
    .all()
)
for run in open_dangling:
    run.interrupted_at_phase = run.phase
```

Catches the daemon-crash / `systemctl restart` mid-batch case where
the pipeline task vanished without going through its except handler.
Any open run without an `interrupted_at_phase` already set gets one
(= the phase it was in when killed). The drive becomes eligible for
recovery on its next re-insert (or via the next layer).

### 4. Present-at-boot recovery dispatch

`_trigger_recovery_for_present_drives` runs immediately after the
startup sweep:

```python
present = drive_mod.discover()
for d in present:
    if await orch.recover_drive(d):
        recovered += 1
```

For every drive currently inserted, tries to dispatch recovery. If
the drive has an open interrupted run, recovery fires immediately —
without waiting for a hotplug ADD event that's never coming (the
drive was already there before the daemon started).

This is the "upgrade-mid-batch" recovery path: the daemon restart
during install.sh kills the active runs, the startup sweep flags
them as interrupted, this layer dispatches recovery for the drives
still in their bays.

## Recovery dispatch (`recover_drive`)

```python
async def recover_drive(self, drive: Drive) -> bool:
    # Find an open run with interrupted_at_phase set
    run = (
        session.query(m.TestRun)
        .filter_by(drive_serial=drive.serial, completed_at=None)
        .filter(m.TestRun.interrupted_at_phase.isnot(None))
        .order_by(m.TestRun.started_at.desc())
        .first()
    )
    if run is None:
        return False

    interrupted_phase = run.interrupted_at_phase
    quick = bool(run.quick_mode)
    # Close the open run
    run.completed_at = datetime.now(UTC)
    run.phase = "interrupted"

    # Spawn recovery task
    asyncio.create_task(self._run_recovery(drive, interrupted_phase, quick))
    return True
```

Returns `True` if a recovery was dispatched, `False` if there was
nothing to recover (no matching open interrupted run).

`_run_recovery`:

1. Adds the serial to `state.recovery_serials` (drives the amber
   glow on the dashboard card)
2. Sets `state.active_phase[serial] = "recovering"` so the card
   shows the recovering state
3. Per-transport state repair (`_recover_secure_erase`)
4. Calls `start_batch([drive], source="auto-recovery after pull
   during <phase>", quick=quick)` to spawn a fresh pipeline
5. The fresh pipeline's `_run_drive` `finally` block clears
   `state.recovery_serials` when it exits, ending the amber glow

## Per-transport state repair

`_recover_secure_erase` in `daemon/orchestrator.py`. Per-transport:

### SAS

```python
self._log(serial, "recovery: running sg_format --format to completion ...")
await loop.run_in_executor(None, erase._sas_secure_erase, drive.device_path)
```

If the drive was in `sg_format` when pulled, the kernel sees it
in "Medium format corrupted" state — most I/O against it errors
with sense data indicating "MEDIUM ERROR". Re-issuing
`sg_format --format` to completion is the only documented recovery
path. Takes the full original duration (15+ min).

### SATA (v0.3.0+, via SAT passthrough)

```python
result = process.run(["hdparm", "-I", drive.device_path], timeout=10)
out = (result.stdout or "").lower()
is_frozen = "\tfrozen" in out and "not\tfrozen" not in out
is_locked = "\tlocked" in out and "not\tlocked" not in out

if is_frozen:
    raise RuntimeError("SATA security is frozen; cannot recover via software")
if is_locked:
    sat_passthru.security_unlock(drive.device_path, owner=serial)
    sat_passthru.security_disable_password(drive.device_path, owner=serial)
```

Three states the drive could be in:

- **Frozen** — BIOS issued SECURITY FREEZE LOCK. Can't recover
  without a power cycle on a BIOS that doesn't freeze. Recovery
  fails with a clear error; operator has to power-cycle.
- **Locked** — `_sata_secure_erase` had set the password before
  the pull. Unlock with the same password (`driveforge`), then
  disable to clear it.
- **Neither locked nor frozen** — pull happened before the
  password was ever set. No drive-side repair needed; fall
  through to the fresh pipeline.

`hdparm -I` is read-only (HDIO_GET_IDENTITY); it works on the
modern kernels where the legacy task ioctl was removed. Only the
unlock + disable use SAT passthrough.

### NVMe

```python
self._log(serial, "recovery: NVMe crypto-erase is atomic — nothing to recover")
```

`nvme format -s 1` is atomic. The drive is either fully erased
(format completed before the pull) or untouched (format hadn't
started). No intermediate state to repair.

## State tracker fields

`DaemonState` carries the recovery-relevant fields:

| Field | Type | Purpose |
|-------|------|---------|
| `interrupted_serials` | `set[str]` | Set by `_handle_drive_removed`; consulted by `_looks_like_pull` |
| `recovery_serials` | `set[str]` | Set during `_run_recovery`; drives the amber-glow CSS class |
| `active_phase` | `dict[str, str]` | Drive's current pipeline phase. Includes the synthetic `"recovering"` value during state repair. |

## What happens if recovery fails

`_run_recovery` wraps the per-transport repair in try/except. On
exception (e.g. SATA security frozen, sg_format errors out):

1. Log the failure
2. Pop `state.active_phase[serial]`, `active_percent[serial]`,
   etc. — clears the recovering state from the dashboard
3. `state.recovery_serials.discard(serial)` — clears the amber glow
4. The original interrupted run stays closed (`phase="interrupted"`,
   `completed_at` set)
5. No fresh pipeline spawns

The drive's card transitions back to a normal Installed card showing
`idle · never tested` (since the open interrupted run is closed and
no new run completed). The operator can manually retry via
**+ New Batch** which would skip recovery entirely and start fresh —
useful if the drive's stuck state has resolved on its own (e.g.,
through a power cycle that unfroze SATA security).

## Idempotency guarantees

Every recovery primitive is safe to call multiple times:

- `_flag_interrupted` only sets `interrupted_at_phase` if it's still
  NULL — won't overwrite a value already set by an earlier path
- `recover_drive` returns `False` and does nothing if there's no
  open interrupted run — safe to call on every hotplug ADD even
  for drives that have nothing to recover
- The startup sweep + present-at-boot dispatch both filter out
  runs that already have `interrupted_at_phase` set
- `_recover_secure_erase` per-transport: `hdparm -I` is idempotent
  read; `sg_format` to completion is idempotent (drive ends in
  same state regardless of starting state); SAT unlock + disable
  are idempotent for an already-cleared drive (return clean errors
  that get logged + ignored)

## Why the open-run-leaves-pipeline-half-done is correct

A pulled drive's pipeline task ends without setting `completed_at`.
That's intentional — the run is not complete (we don't know if the
phase succeeded), and the system needs to remember it's incomplete
so recovery can pick it up.

Marking it `failed` would lose that signal. Marking it `done` would
be wrong (no grade, no SMART snapshot, no badblocks data). Leaving
`completed_at` NULL with `interrupted_at_phase` set is the unambiguous
"this needs recovery" signal that every layer of the system reads.

The `_record_failure` path **doesn't** run for pulls — it would
write `completed_at`. That's why `_run_drive`'s except block
distinguishes "looks like pull → call `_flag_interrupted`" from
"genuine failure → call `_record_failure`".
