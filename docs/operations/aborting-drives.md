---
title: Aborting drives
---

# Aborting drives

Two ways to stop a running pipeline:

- **Per-drive Abort button** on the Active card (v0.2.2+) — stops one
  drive's pipeline.
- **Global `POST /abort-all`** endpoint — stops every active drive.
  No UI button (intentional safety friction); curl-only.

## Per-drive Abort button

Top-right corner of every Active card. One click; no second
confirmation prompt for non-erase phases (the pipeline is already
running and the operator already paid attention to find the right
card). Confirmation prompt **does** appear for non-erase phases
because asking "are you sure?" once feels right; the JS confirm
appears via the button's `onclick` handler.

### What it does

```python
async def abort_drive(self, serial: str) -> bool:
    task = self._tasks.get(serial)
    if task is None or task.done():
        return False
    killed = process.kill_owner(serial)
    task.cancel()
```

In order:

1. **`kill_owner(serial)`** — SIGTERM every subprocess registered
   with the drive's serial as owner (smartctl, hdparm, sg_format,
   nvme, badblocks). Wait 3 seconds. Then SIGKILL anything still
   alive. This is the load-bearing step — without it, asyncio task
   cancellation can leave sync subprocesses orphaned in the thread
   pool.
2. **`task.cancel()`** — cancels the asyncio pipeline task. The
   task hits `asyncio.CancelledError` at its next await boundary
   and falls into the `_record_failure(phase="aborted",
   detail="aborted by user")` branch.
3. **TestRun row** gets `phase="aborted"`, `completed_at=now()`,
   `grade=NULL` (since v0.2.6 — pre-v0.2.6 it was
   `grade="fail"`).
4. **Dashboard refresh** drops the drive from the Active section
   back into Installed, rendering as `idle · never tested`.

## Why Abort is disabled during secure_erase

Look at the disabled-button tooltip:

> Abort disabled during secure erase — the drive firmware handles
> this phase internally. Aborting SAS sg_format mid-flight leaves
> the drive with 'Medium format corrupted' and requires manual
> recovery.

The reasoning:

- **SAS `sg_format --format`** issues a single SCSI FORMAT UNIT
  command. The drive does the format internally over hours; the
  host process is just waiting. Killing the host process doesn't
  cancel the in-progress format. The drive can be left in
  "Medium format corrupted" state — needs another sg_format-to-
  completion to recover.
- **SATA SAT-passthrough secure erase** (v0.3.0+) is similar — the
  drive does the work in firmware after receiving the command;
  killing the host doesn't stop it.
- **NVMe `nvme format -s 1`** is near-instantaneous, so abort is
  moot.

Disabling the button across the board is simpler than per-transport
logic and costs the operator at most a few minutes of waiting (the
secure_erase ticker on the dashboard tells you how long is left).

The server side **still honors abort during secure_erase if you
really want it.** The button disable is a UX guardrail, not hard
enforcement. If you have a stuck `hdparm` or `sg_format` process
that needs killing:

```bash
curl -X POST http://<your-driveforge>:8080/drives/<serial>/abort
```

This kills the host process, leaves the drive in whatever state
the kill happened to catch it in, and marks the run aborted. Your
recovery responsibility from there.

## Global abort

```bash
curl -X POST http://<your-driveforge>:8080/abort-all
```

No UI button. Cancels every in-flight drive task, kills every
subprocess, clears `state.active_phase` / `active_percent` /
`active_sublabel` / `done_blinkers`. Returns the number of drives
aborted in the JSON response.

Useful for: emergency stop, daemon hung in an unusual state, end-of-
day "just stop everything," scripted batch shutdown.

## Aborted = untested (v0.2.6+)

Before v0.2.6, aborted runs were marked `grade="fail"` — same as
genuine pipeline failures. Result: the dashboard rendered them as
red Failed cards, the LED started doing the slow lighthouse fail
pattern, and auto-enroll wouldn't restart them.

**v0.2.6 made aborts a separate state from failures.** Aborted runs
get `grade=NULL` and the dashboard shows them as `idle · never
tested`. The LED stays dark. Re-inserting an aborted drive (with
auto-enroll on) triggers a fresh pipeline — see
[Auto-enroll](auto-enroll.md).

The semantics:

- **Failed** = pipeline ran, drive returned a verdict, that verdict
  was bad. Grade A/B/C/Fail are all "verdicts."
- **Aborted** = operator cancelled before a verdict was reached.
  Drive's actual condition is unknown; treat it as if the test
  never happened.

This distinction matters when:

- You aborted because the wrong drive was in the bay, you re-insert
  the right drive — and the right drive should auto-enroll cleanly.
- You aborted to free up a bay for another drive, then later
  re-insert the aborted drive — you want it tested fresh.
- You aborted because the pipeline phase was taking too long and
  you wanted to switch from Full to Quick mode — re-insert with
  Quick on, get a Quick run.

## Subprocess kill detail

The kill flow is in `driveforge/core/process.py:kill_owner`:

```python
def kill_owner(owner: str, *, grace_sec: float = 3.0) -> int:
    pids = active_pids(owner)
    for pid in pids:
        os.kill(pid, signal.SIGTERM)
    time.sleep(grace_sec)
    for pid in pids:
        if still_alive(pid):
            os.kill(pid, signal.SIGKILL)
    return len(pids)
```

Subprocesses register themselves as owned by a drive's serial via
the `owner=<serial>` keyword on `process.run()` calls. The
orchestrator passes `owner=drive.serial` for every subprocess in a
pipeline (smartctl, hdparm, sg_format, nvme, badblocks, etc.) so
kill_owner has them all to signal at once.

Bare `task.cancel()` without `kill_owner` would orphan any synchronous
subprocess running inside `asyncio.to_thread` or
`run_in_executor` — those don't honor asyncio cancellation. The
`kill_owner` step is what makes abort actually stop the work.
