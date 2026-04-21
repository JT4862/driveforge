"""Daemon runtime state.

Holds the shared state that the FastAPI app, orchestrator, and hotplug
monitor all need access to: config, DB session factory, printer backend,
in-flight batch tracking, and the async queue bridging events to the UI.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from driveforge.config import Settings
from driveforge.core import capabilities as capabilities_mod
from driveforge.core import enclosures
from driveforge.core.process import FixtureRunner, set_fixture_runner
from driveforge.db.session import make_engine, init_db, make_session_factory


# v0.6.5+: drive-command thread pool sizing. 16 workers covers the
# widest chassis we expect (24-bay JBOD expanders) plus a handful of
# concurrent telemetry samplers. If this cap is hit, drive subprocesses
# queue on the drive executor instead of starving HTTP request
# threads in FastAPI's default pool. The root cause today of "dashboard
# wedges when drives go D-state" is that FastAPI's default threadpool is
# shared with drive subprocess execution; isolating them fixes the
# cascade symptom even while individual drives stay stuck.
_DRIVE_COMMAND_EXECUTOR_WORKERS = 16


@dataclass
class DaemonState:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker

    # Per-drive active-phase state — the source of truth for "which drives
    # are currently under test". A drive serial is considered active iff
    # it has an entry here. The orchestrator sets/clears these three maps
    # atomically at phase transitions + run completion.
    active_phase: dict[str, str] = field(default_factory=dict)
    active_percent: dict[str, float] = field(default_factory=dict)
    # Optional phase-scoped sub-label shown on the drive card — e.g. which
    # of the 8 badblocks passes is running ("pass 3/8 · write 0xFF"). Cleared
    # on phase transition.
    active_sublabel: dict[str, str] = field(default_factory=dict)
    # Live per-drive I/O rate during active phases. Written by a periodic
    # /proc/diskstats poller in the daemon lifespan; read by the drive-card
    # renderer. Unit: MB/s (decimal megabytes). Cleared when a drive leaves
    # active_phase.
    active_io_rate: dict[str, dict[str, float]] = field(default_factory=dict)
    # Rolling history of recent I/O rates per drive — last 10 samples at
    # 3-second intervals = 30 s window. Used by the dashboard to render an
    # SVG sparkline during high-throughput phases (badblocks) so operators
    # can spot a stalling drive at a glance. Each entry: {"read": MB/s,
    # "write": MB/s}. Capped to len=10; older samples drop off the end.
    active_io_history: dict[str, list[dict[str, float]]] = field(default_factory=dict)
    # Serial → kernel device basename ("sda", "nvme0n1") for drives currently
    # in active_phase. The DB doesn't persist device_path (kernel letters
    # drift), so the I/O rate poller needs this to map its diskstats basename
    # rows back to the active drives. Populated by the orchestrator when a
    # drive starts running, cleared when it leaves active_phase.
    device_basenames: dict[str, str] = field(default_factory=dict)
    # Latest drive temperature (°C) for drives currently in the pipeline.
    # Populated by `_record_telemetry()` in the orchestrator on each SMART
    # snapshot (pre/post + periodic polls). Drives that never report a
    # readable temperature are simply absent from this dict; callers
    # should handle `.get(serial)` → None cleanly. Cleared on pipeline exit.
    active_drive_temp: dict[str, int] = field(default_factory=dict)
    # Ring buffer of recent log lines per in-flight drive (last ~40 lines)
    active_log: dict[str, list[str]] = field(default_factory=dict)
    # Wall-clock (monotonic seconds) when each drive's phase last transitioned.
    # Used by the dashboard to briefly pulse the card border when the phase
    # changes — a visual cue that progress is happening, without having to
    # watch the phase label closely.
    phase_change_ts: dict[str, float] = field(default_factory=dict)
    # Wall-clock (monotonic) when each drive's pipeline last completed.
    # Used to flash a card's "just completed" state for a short window
    # after it transitions from Active → Installed.
    just_completed_ts: dict[str, float] = field(default_factory=dict)
    # Serials known to have been pulled mid-pipeline. Set by the hotplug
    # remove handler right before the pipeline's failure path fires, so
    # _run_drive's except block can leave the TestRun row "open"
    # (interrupted_at_phase set, completed_at NULL) for recovery instead
    # of closing it as a permanent failure. Cleared when recovery completes
    # or the user dismisses the interrupted state.
    interrupted_serials: set[str] = field(default_factory=set)
    # Serials currently running a recovery-triggered pipeline. Populated
    # by `_run_recovery` when a pulled drive is re-inserted and cleared
    # in `_run_drive`'s finally when that pipeline exits. The dashboard
    # uses this to draw a persistent amber glow around the card for the
    # full duration of recovery — including the brief drive-state repair
    # step AND the fresh pipeline that follows — so an operator can see
    # at a glance which drives are "I'm retrying after a pull" vs.
    # "normal test run".
    recovery_serials: set[str] = field(default_factory=set)
    # v0.5.5+ — Serials of drives whose last quick-pass triaged as Fail
    # and whose operator has opted into the "prompt" mode via
    # settings.daemon.quick_pass_fail_action. The dashboard renders a
    # banner on these cards offering to run a full pipeline; operator
    # clicks Yes (triggers start_batch + removes from set) or Dismiss
    # (just removes). Not persisted \u2014 a daemon restart clears the set,
    # which is acceptable: the triage badge on the drive row still
    # surfaces the state, and operators can re-trigger via New Batch.
    promote_prompts: set[str] = field(default_factory=set)
    # Post-pipeline "safe to pull" activity-LED blinkers, keyed by serial.
    # Populated when a run completes, cancelled on drive pull / abort / new batch.
    done_blinkers: dict[str, asyncio.Task] = field(default_factory=dict)
    # Operator-triggered identify blinkers, keyed by serial. Stored
    # separately from done_blinkers so the dashboard can toggle the
    # Ident button per-drive without losing the pass/fail LED pattern
    # that was running before identify took over (restored when
    # identify exits). Click Ident → add entry. Click Stop → cancel +
    # remove. Auto-removes on task exit (5-minute deadline, drive
    # pull, or natural cancellation).
    identify_blinkers: dict[str, asyncio.Task] = field(default_factory=dict)

    # Cached enclosure discovery. Refreshed on boot + udev events. Kept for
    # internal LED targeting (sg_ses needs the slot's element_index) and as
    # informational metadata; the dashboard itself no longer renders drives
    # by enclosure/slot — it's a flat drive-centric list.
    bay_plan: enclosures.BayPlan = field(
        default_factory=lambda: enclosures.BayPlan(enclosures=[])
    )
    # Cached hardware capabilities — led control, chassis power, chassis temp.
    # Refreshed alongside bay_plan because led_control is derived from it.
    # Used by the Setup Wizard Step 2 "capabilities" panel + Settings page.
    capabilities: capabilities_mod.HardwareCapabilities = field(
        default_factory=lambda: capabilities_mod.HardwareCapabilities(
            led_control=False, chassis_power=False, chassis_temperature=False
        )
    )
    # v0.6.5+ dedicated thread pool for drive-subprocess execution
    # (sg_raw, smartctl, hdparm, badblocks, sg_format, nvme). Kept
    # SEPARATE from FastAPI's default anyio threadpool so a drive
    # stuck in kernel D-state can't starve HTTP request handlers.
    # Without this split, 2-3 stuck drives consume 2-3 of the ~40
    # default pool slots, dashboard polling fills the rest, and the
    # UI wedges. With this split, the orchestrator's executor has
    # its own 16 workers and the dashboard's responsiveness is
    # independent of drive state.
    drive_command_executor: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(
            max_workers=_DRIVE_COMMAND_EXECUTOR_WORKERS,
            thread_name_prefix="drive-cmd",
        )
    )

    def refresh_bay_plan(self) -> enclosures.BayPlan:
        """Re-discover enclosures + capabilities. Called on daemon start
        and on udev add/remove events."""
        self.bay_plan = enclosures.build_bay_plan(
            sys_root=self.settings.daemon.sysfs_root,
        )
        self.capabilities = capabilities_mod.detect(plan=self.bay_plan)
        return self.bay_plan

    def active_serials(self) -> set[str]:
        """Serials currently in the test pipeline. Single source of truth.

        v0.6.5+: snapshot via list() before building the set — orchestrator
        tasks mutate active_phase concurrently, and `set(dict.keys())`
        iterates internally while building the set. Under the 8-drive
        concurrent scenario this raced and raised "dictionary changed
        size during iteration." list() snapshots atomically under GIL.
        """
        return set(list(self.active_phase))

    @classmethod
    def boot(cls, settings: Settings) -> "DaemonState":
        engine = make_engine(settings.daemon.db_path)
        init_db(engine)
        sf = make_session_factory(engine)
        # Install the fixtures runner if dev mode is active
        if settings.dev_mode and settings.fixtures_dir:
            set_fixture_runner(FixtureRunner(settings.fixtures_dir))
            # Dev mode also points sysfs at a synthetic tree under the
            # fixtures dir if one exists
            synthetic_sys = settings.fixtures_dir / "sys"
            if synthetic_sys.exists():
                settings.daemon.sysfs_root = settings.fixtures_dir
        # Ensure runtime dirs exist
        for d in (settings.daemon.state_dir, settings.daemon.pending_labels_dir, settings.daemon.reports_dir):
            Path(d).mkdir(parents=True, exist_ok=True)
        instance = cls(settings=settings, engine=engine, session_factory=sf)
        instance.refresh_bay_plan()
        return instance


_STATE: DaemonState | None = None


def set_state(state: DaemonState) -> None:
    global _STATE
    _STATE = state


def get_state() -> DaemonState:
    if _STATE is None:
        raise RuntimeError("daemon state not initialized")
    return _STATE
