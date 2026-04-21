---
title: Pull and recovery state machine
---

# Pull and recovery state machine

> **Stub.** Full state diagram + transition table lands in v0.4.0.

This page will cover:

- The TestRun lifecycle: queued → pre_smart → … → done | failed | aborted | interrupted
- The interrupt path: `interrupted_at_phase` set, `completed_at` left NULL
- Detection layers (in order):
  1. Hotplug REMOVE event flags `state.interrupted_serials`
  2. Pipeline failure path checks `_looks_like_pull(drive)` (v0.2.9 serial-rediscovery wins over device-path check)
  3. Daemon startup sweep (`_flag_dangling_runs_as_interrupted`) catches drives left open by daemon crashes / restarts
  4. Present-at-boot trigger (`_trigger_recovery_for_present_drives`) dispatches recovery for drives still inserted at startup
- Recovery dispatch: closes the interrupted run with `phase="interrupted"`, spawns `_run_recovery` task
- Per-transport state repair (SAS sg_format completion, SATA SAT-passthrough unlock+disable, NVMe no-op)
- Fresh pipeline start with the same quick-flag the original run had
- The `state.recovery_serials` set + amber-glow UI indicator (v0.2.5+)
