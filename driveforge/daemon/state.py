"""Daemon runtime state.

Holds the shared state that the FastAPI app, orchestrator, and hotplug
monitor all need access to: config, DB session factory, printer backend,
in-flight batch tracking, and the async queue bridging events to the UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from driveforge.config import Settings
from driveforge.core.process import FixtureRunner, set_fixture_runner
from driveforge.db.session import make_engine, init_db, make_session_factory


@dataclass
class DaemonState:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker

    # In-flight: bay number -> active drive serial. Driven by the orchestrator.
    bay_assignments: dict[int, str] = field(default_factory=dict)
    # Latest phase per drive — for fast dashboard rendering
    active_phase: dict[str, str] = field(default_factory=dict)
    active_percent: dict[str, float] = field(default_factory=dict)

    @classmethod
    def boot(cls, settings: Settings) -> "DaemonState":
        engine = make_engine(settings.daemon.db_path)
        init_db(engine)
        sf = make_session_factory(engine)
        # Install the fixtures runner if dev mode is active
        if settings.dev_mode and settings.fixtures_dir:
            set_fixture_runner(FixtureRunner(settings.fixtures_dir))
        # Ensure runtime dirs exist
        for d in (settings.daemon.state_dir, settings.daemon.pending_labels_dir, settings.daemon.reports_dir):
            Path(d).mkdir(parents=True, exist_ok=True)
        return cls(settings=settings, engine=engine, session_factory=sf)


_STATE: DaemonState | None = None


def set_state(state: DaemonState) -> None:
    global _STATE
    _STATE = state


def get_state() -> DaemonState:
    if _STATE is None:
        raise RuntimeError("daemon state not initialized")
    return _STATE
