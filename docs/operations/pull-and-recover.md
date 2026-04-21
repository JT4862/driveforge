---
title: Pull and recover
---

# Pull and recover

You can pull a drive mid-pipeline. DriveForge handles it cleanly:
detects the pull, marks the run as interrupted (not failed), waits
for re-insert, repairs whatever drive-side state needs repair, and
restarts the pipeline. The dashboard shows a persistent amber glow
on the card for the entire recovery duration so you can see at a
glance which drives are "I'm retrying after a pull" vs "normal test
run."

This page is the operator-facing walkthrough. For the underlying
state machine + transition table, see
[Reference → Pull and recovery state machine](../reference/pull-recovery.md).

## What "pull" means

A drive disappears from the SAS / SATA / NVMe bus while a pipeline
task is running against it. Causes:

- Operator physically yanks the drive (most common)
- Cable came loose
- Backplane / HBA bus glitch (the LSI SAS2308 occasionally drops +
  re-adds links without anyone touching anything)
- Drive's own firmware crashes and the controller drops it

DriveForge treats all of these the same way at the recovery layer.
The detection layer distinguishes "real pull" from "transient bus
event" so the latter doesn't trigger unnecessary recovery — see
below.

## What happens when you yank a drive

In real-time:

1. Kernel `udev` fires a REMOVE event for the block device
2. DriveForge's hotplug monitor catches it, looks up the serial,
   adds it to `state.interrupted_serials`
3. A few hundred milliseconds later, the pipeline's current phase
   (smartctl, hdparm, sg_format, etc.) starts erroring out because
   its target device file is gone
4. The pipeline task hits its `except` handler. `_looks_like_pull`
   checks: did the hotplug monitor flag this serial? Is the device
   path actually missing? Is the serial findable under any other
   device path (kernel re-enumeration test, v0.2.9+)?
5. If yes-pull: TestRun row stays open, `interrupted_at_phase` set
   to whatever phase the pipeline was in. No `grade` written.
   Outcome is `None` — no LED blinker (drive is gone, nothing to
   blink).
6. If no-pull (genuine subprocess failure with the drive still
   present): TestRun closed as `grade="fail"`. LED starts the
   lighthouse fail pattern.

The dashboard removes the drive from the Active section on the next
poll (since it's gone).

## What happens when you re-insert

1. udev fires an ADD event
2. Hotplug handler runs decisions in priority order:
   1. **`recover_drive(drive)`** is called first. It looks for an
      open TestRun for this drive's serial with `interrupted_at_phase
      IS NOT NULL`. If found:
      - Closes the open run as `phase="interrupted"`,
        `completed_at=now()`
      - Removes the serial from `state.interrupted_serials`
      - Adds the serial to `state.recovery_serials` (this is what
        triggers the amber glow on the card)
      - Spawns `_run_recovery(drive, interrupted_phase, quick)` task
   2. If recovery didn't fire, `restore_blinker_for_drive` runs (for
      drives with previous pass/fail history)
   3. If no blinker either, auto-enroll evaluates (for fresh drives
      with auto-enroll on)

## What recovery actually does

`_run_recovery` shows a temporary `recovering` phase on the card
(with the `↻` icon) while it does its work, then dispatches a fresh
pipeline. The fresh pipeline runs under the same `quick` flag the
interrupted one had — so a recovery from an aborted Full run starts
a new Full run.

Per-transport drive-state repair:

### SAS

If the drive was in `sg_format` when pulled, it's now in "Medium
format corrupted" state. The kernel won't let any I/O succeed
against it.

Recovery: re-issue `sg_format --format` to completion. Same command
that originally ran; the drive picks up where it left off. Takes
15–60+ minutes depending on capacity.

The card shows `recovering` for this duration (with the amber glow),
then transitions into a fresh pipeline at `secure_erase` once the
format completes.

### SATA (v0.3.0+ via SAT passthrough)

If the drive was in security-erase when pulled, the security state
might be:

- **Frozen** — BIOS issued SECURITY FREEZE LOCK at boot. Can't
  unlock via software. Recovery fails cleanly with a clear error.
  Requires power-cycle on a BIOS that doesn't freeze.
- **Locked** — `_recover_secure_erase` runs SAT passthrough
  `SECURITY UNLOCK` with the throwaway password DriveForge set,
  followed by `SECURITY DISABLE PASSWORD` to clear the password
  entirely. Drive is now in a clean state for the next erase.
- **Neither** — pull happened before SECURITY SET PASSWORD ever
  succeeded. No recovery needed; fall through to the fresh
  pipeline.

Pre-v0.3.0, the unlock + disable used `hdparm`, which errored on the
NX-3200's LSI SAS2308 with `CONFIG_IDE_TASK_IOCTL`. Recovery from
SATA-on-SAS pulls effectively didn't work pre-v0.3.0; v0.3.0 fixed
both the original erase AND the recovery via SAT passthrough.

### NVMe

`nvme format -s 1` is atomic crypto-erase — completes in seconds
or fails outright. If you pull mid-format, the drive is either
done (the format completed before you pulled) or untouched (the
format hadn't started). Either way, no drive-side repair is
needed; recovery is a no-op and the fresh pipeline just re-runs
the format.

## The amber-glow indicator

CSS class `.recovery-mode` on the card. Driven by
`state.recovery_serials`. Pulses at a 2.8s cycle. Persists for the
**entire** recovery duration — drive-side repair (which can be 15+
minutes for SAS) plus the fresh pipeline that follows (hours for
Full).

Cleared in `_run_drive`'s `finally` block when the recovery-
triggered pipeline exits. Drive's card transitions back to a
normal Installed card with whatever grade the recovery pipeline
produced.

## Edge cases

### Daemon crash mid-pipeline

If the daemon crashes (or you `systemctl restart driveforge-daemon`)
while a pipeline is running, the pipeline task disappears with the
process. The TestRun is left open with `completed_at=NULL` but no
`interrupted_at_phase` (since `_record_failure` never ran).

On daemon startup, `_flag_dangling_runs_as_interrupted` sweeps for
exactly this case: any open TestRun without an `interrupted_at_phase`
gets one set (= the phase the run was in when it died). The drive
is then eligible for recovery on next re-insert.

### Drives present at boot

`_trigger_recovery_for_present_drives` runs at daemon start (after
the dangling-run sweep). For every drive currently inserted, if it
has an open interrupted run, dispatch recovery for it immediately —
without waiting for a hotplug ADD event that's never coming (the
drive is already there).

This is the "upgraded the daemon mid-batch" recovery path —
upgrades restart the daemon, the drives are still in their bays, and
recovery dispatches automatically.

### Bus glitch without a real pull (v0.2.9+)

LSI SAS2308 occasionally drops + re-adds a SATA drive's link without
the drive being physically pulled. Pre-v0.2.9, this falsely
classified as a pull because `_looks_like_pull` only checked
`os.path.exists(device_path)` — and during the brief re-enumeration
window, the device path didn't exist.

v0.2.9 added a serial-rediscovery check: if the drive's serial is
findable in `lsblk` under any device path, it's not a pull — it's a
genuine pipeline failure that should close cleanly as `grade="fail"`.
Eliminates the "stuck interrupted" zombie-run scenario where a bus
glitch left a drive in limbo waiting for a pull-and-reinsert that
would never come.

## What if recovery fails

If `_recover_secure_erase` raises (security frozen, sg_format errors,
etc.):

1. The `_run_recovery` task catches the exception, logs it, clears
   `state.active_phase` / `recovery_serials` for the drive
2. Card transitions back to an Installed card showing the drive as
   `idle · never tested` (the original interrupted run is closed,
   the new pipeline never started)
3. Operator can manually retry via **+ New Batch** (which would skip
   recovery and start a fresh pipeline directly — useful if the
   drive's stuck state has resolved on its own, e.g. a power cycle)
