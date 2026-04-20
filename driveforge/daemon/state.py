"""Daemon runtime state.

Holds the shared state that the FastAPI app, orchestrator, and hotplug
monitor all need access to: config, DB session factory, printer backend,
in-flight batch tracking, and the async queue bridging events to the UI.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from driveforge.config import Settings
from driveforge.core import enclosures
from driveforge.core.process import FixtureRunner, set_fixture_runner
from driveforge.db.session import make_engine, init_db, make_session_factory


@dataclass
class DaemonState:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker

    # In-flight: bay_key -> active drive serial. Driven by the orchestrator.
    bay_assignments: dict[str, str] = field(default_factory=dict)
    # Latest phase per drive — for fast dashboard rendering
    active_phase: dict[str, str] = field(default_factory=dict)
    active_percent: dict[str, float] = field(default_factory=dict)
    # Optional phase-scoped sub-label shown on the bay card — e.g. which of
    # the 8 badblocks passes is running ("pass 3/8 · write 0xFF"). Cleared on
    # phase transition.
    active_sublabel: dict[str, str] = field(default_factory=dict)
    # Live per-drive I/O rate during active phases. Written by a periodic
    # /proc/diskstats poller in the daemon lifespan; read by the bay-card
    # renderer. Unit: MB/s (decimal megabytes). Cleared when a drive leaves
    # bay_assignments.
    active_io_rate: dict[str, dict[str, float]] = field(default_factory=dict)
    # Serial → kernel device basename ("sda", "nvme0n1") for drives currently
    # in bay_assignments. The DB doesn't persist device_path (kernel letters
    # drift), so the I/O rate poller needs this to map its diskstats basename
    # rows back to the active drives. Populated by the orchestrator when a
    # batch starts, cleared when a drive leaves bay_assignments.
    device_basenames: dict[str, str] = field(default_factory=dict)
    # Ring buffer of recent log lines per in-flight drive (last ~40 lines)
    active_log: dict[str, list[str]] = field(default_factory=dict)
    # Post-pipeline "safe to pull" activity-LED blinkers, keyed by serial.
    # Populated when a run completes, cancelled on drive pull / abort / new batch.
    done_blinkers: dict[str, asyncio.Task] = field(default_factory=dict)

    # Cached enclosure discovery. Refreshed on boot + udev events.
    bay_plan: enclosures.BayPlan = field(
        default_factory=lambda: enclosures.BayPlan(enclosures=[], virtual_bay_count=0, total_bays=0)
    )

    def refresh_bay_plan(self) -> enclosures.BayPlan:
        """Re-discover enclosures. Called on daemon start and udev add/remove."""
        self.bay_plan = enclosures.build_bay_plan(
            sys_root=self.settings.daemon.sysfs_root,
            virtual_bays_fallback=self.settings.daemon.virtual_bays,
        )
        return self.bay_plan

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
