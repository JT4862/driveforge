"""Regression tests for the v0.2.7 auto-enroll-after-abort fix.

Three things the user hit in production that these tests guard:

  1. `Orchestrator.active_serials()` must NOT report drives whose pipeline
     task has completed. Prior to v0.2.7 a missing `_tasks.pop` in
     `_run_drive`'s finally meant every previously-run serial stayed in
     the "busy" set forever, and `start_batch([serial])` would raise
     `BatchRejected` for any auto-enroll of that serial afterwards.

  2. The hotplug-add auto-enroll filter must key off the LATEST TestRun
     per drive, not "any graded run within the last hour". An aborted
     re-test should supersede a prior pass/fail decision — otherwise a
     drive that passed 20 min ago, was aborted mid-retest, and then
     re-inserted would be silently blocked from auto-enrolling.

  3. `_hotplug_loop` must be resilient to handler-level exceptions. A
     single bad event (or a future regression in the handlers) cannot
     be allowed to kill the monitor task and silently starve the
     daemon of all further hotplug events.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from driveforge import config as cfg
from driveforge.core import drive as drive_mod
from driveforge.core.drive import Drive, Transport
from driveforge.core.hotplug import EventKind
from driveforge.daemon.app import _handle_drive_added, _hotplug_loop
from driveforge.daemon.orchestrator import BatchRejected, Orchestrator
from driveforge.daemon.state import DaemonState
from driveforge.db import models as m


# ---------------------------------------------------------------- fixtures


def _make_settings(tmp_path: Path, *, auto_enroll_mode: str = "quick") -> cfg.Settings:
    settings = cfg.Settings()
    settings.daemon.state_dir = tmp_path
    settings.daemon.db_path = tmp_path / "driveforge.db"
    settings.daemon.pending_labels_dir = tmp_path / "pending-labels"
    settings.daemon.reports_dir = tmp_path / "reports"
    settings.daemon.auto_enroll_mode = auto_enroll_mode
    return settings


def _make_drive(serial: str = "SN-TEST-1") -> Drive:
    return Drive(
        serial=serial,
        model="TESTDRIVE-1",
        capacity_bytes=1_000_000_000,
        transport=Transport.SATA,
        device_path="/dev/sdz",
        rotation_rate=0,
        firmware_version="TEST0001",
        manufacturer="Testco",
    )


def _make_event(drive: Drive):
    # EventKind.DRIVE_ADDED event shape mirrors what driveforge.core.hotplug
    # actually builds — a simple namespace with .kind/.serial/.device_node.
    return SimpleNamespace(
        kind=EventKind.DRIVE_ADDED,
        serial=drive.serial,
        device_node=drive.device_path,
    )


# ---------------------------------------------------------------- helpers


def _seed_aborted_run(state: DaemonState, drive: Drive, *, completed_minutes_ago: int) -> None:
    """Populate the DB with a Drive row + a completed 'aborted' TestRun
    (grade=None). Mirrors what the orchestrator's `_record_failure` writes
    on the user-abort path."""
    now = datetime.now(UTC)
    with state.session_factory() as session:
        session.add(
            m.Drive(
                serial=drive.serial,
                model=drive.model,
                manufacturer=drive.manufacturer,
                capacity_bytes=drive.capacity_bytes,
                transport=drive.transport.value,
            )
        )
        session.add(
            m.TestRun(
                drive_serial=drive.serial,
                phase="aborted",
                started_at=now - timedelta(minutes=completed_minutes_ago + 5),
                completed_at=now - timedelta(minutes=completed_minutes_ago),
                grade=None,
                quick_mode=False,
                error_message="[aborted] aborted by user",
            )
        )
        session.commit()


def _seed_pass_then_abort(state: DaemonState, drive: Drive) -> None:
    """Grade A run 20 min ago, then an aborted run 2 min ago.

    Exercises the latest-run filter change: the older Grade A would match
    the old `any graded run in the last hour` filter and block auto-enroll,
    but the latest run is aborted so v0.2.7 should fire auto-enroll.
    """
    now = datetime.now(UTC)
    with state.session_factory() as session:
        session.add(
            m.Drive(
                serial=drive.serial,
                model=drive.model,
                manufacturer=drive.manufacturer,
                capacity_bytes=drive.capacity_bytes,
                transport=drive.transport.value,
            )
        )
        session.add(
            m.TestRun(
                drive_serial=drive.serial,
                phase="done",
                started_at=now - timedelta(minutes=22),
                completed_at=now - timedelta(minutes=20),
                grade="A",
                quick_mode=True,
            )
        )
        session.add(
            m.TestRun(
                drive_serial=drive.serial,
                phase="aborted",
                started_at=now - timedelta(minutes=4),
                completed_at=now - timedelta(minutes=2),
                grade=None,
                quick_mode=True,
                error_message="[aborted] aborted by user",
            )
        )
        session.commit()


def _seed_recent_pass(state: DaemonState, drive: Drive) -> None:
    """Grade A run completed 10 min ago. Latest run IS graded + recent,
    so auto-enroll should be blocked — this is the "don't retest a freshly
    passed drive that got momentarily pulled" behavior we DON'T want to
    regress while fixing the aborted case."""
    now = datetime.now(UTC)
    with state.session_factory() as session:
        session.add(
            m.Drive(
                serial=drive.serial,
                model=drive.model,
                manufacturer=drive.manufacturer,
                capacity_bytes=drive.capacity_bytes,
                transport=drive.transport.value,
            )
        )
        session.add(
            m.TestRun(
                drive_serial=drive.serial,
                phase="done",
                started_at=now - timedelta(minutes=12),
                completed_at=now - timedelta(minutes=10),
                grade="A",
                quick_mode=True,
            )
        )
        session.commit()


class _SpyOrch:
    """Minimal Orchestrator stand-in for the auto-enroll decision test.

    We don't want `_handle_drive_added` spinning up real pipeline tasks
    (the fixture runner would try to execute smartctl / hdparm / etc.);
    this spy records whether recovery + auto-enroll fired, and defers to
    the real Orchestrator for `restore_blinker_for_drive` (which is a
    no-op for a drive whose latest row is phase="aborted")."""

    def __init__(self, real: Orchestrator) -> None:
        self._real = real
        self.recover_calls: list[Drive] = []
        self.start_batch_calls: list[tuple[list[Drive], str | None, bool]] = []
        self.recover_should_return = False
        self.start_batch_should_raise: Exception | None = None

    async def recover_drive(self, drive: Drive) -> bool:
        self.recover_calls.append(drive)
        return self.recover_should_return

    def restore_blinker_for_drive(self, drive: Drive) -> None:
        # Pass-through to the real one — it's safe (in-memory state flag).
        self._real.restore_blinker_for_drive(drive)

    async def start_batch(self, drives, *, source=None, quick=False) -> str:
        self.start_batch_calls.append((list(drives), source, bool(quick)))
        if self.start_batch_should_raise is not None:
            raise self.start_batch_should_raise
        return "spied-batch"


# ---------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_active_serials_excludes_done_tasks() -> None:
    """Regression: `active_serials()` must filter out completed tasks so
    a freshly-aborted drive isn't misreported as busy.

    Prior to v0.2.7 `active_serials()` returned `set(self._tasks)` —
    every serial that had ever been started, forever."""

    async def _done() -> None:
        return None

    # Minimal state — we only exercise the task tracker, not the DB
    state = SimpleNamespace(active_serials=lambda: set())
    orch = Orchestrator(state)  # type: ignore[arg-type]

    task = asyncio.create_task(_done())
    await task  # task is now done but still referenced in nothing
    orch._tasks["STALE-SERIAL"] = task

    assert task.done()
    assert "STALE-SERIAL" not in orch.active_serials()


@pytest.mark.asyncio
async def test_auto_enroll_fires_for_aborted_drive_on_reinsert(
    tmp_path, monkeypatch
) -> None:
    """End-to-end: an aborted drive gets auto-quick-run on re-insert."""
    settings = _make_settings(tmp_path, auto_enroll_mode="quick")
    state = DaemonState.boot(settings)
    orch = Orchestrator(state)
    drive = _make_drive()
    _seed_aborted_run(state, drive, completed_minutes_ago=2)

    # Match the hotplug event to a discovered drive.
    monkeypatch.setattr(drive_mod, "discover", lambda: [drive])

    spy = _SpyOrch(orch)
    await _handle_drive_added(state, spy, _make_event(drive))  # type: ignore[arg-type]

    assert spy.recover_calls == [drive], "recovery must be probed first"
    assert len(spy.start_batch_calls) == 1, "auto-enroll must fire"
    enrolled, source, quick = spy.start_batch_calls[0]
    assert [d.serial for d in enrolled] == [drive.serial]
    assert quick is True
    assert source and "auto-enroll" in source


@pytest.mark.asyncio
async def test_auto_enroll_fires_when_latest_run_is_abort_even_after_prior_pass(
    tmp_path, monkeypatch
) -> None:
    """The v0.2.7 filter change: a Grade A from 20 min ago must NOT
    block auto-enroll when the latest run is an abort."""
    settings = _make_settings(tmp_path, auto_enroll_mode="quick")
    state = DaemonState.boot(settings)
    orch = Orchestrator(state)
    drive = _make_drive()
    _seed_pass_then_abort(state, drive)

    monkeypatch.setattr(drive_mod, "discover", lambda: [drive])

    spy = _SpyOrch(orch)
    await _handle_drive_added(state, spy, _make_event(drive))  # type: ignore[arg-type]

    assert len(spy.start_batch_calls) == 1, (
        "stale Grade A must NOT block auto-enroll when latest run is an abort"
    )


@pytest.mark.asyncio
async def test_auto_enroll_skipped_when_latest_run_is_recent_pass(
    tmp_path, monkeypatch
) -> None:
    """Guardrail: a drive that passed 10 min ago and was pulled + re-inserted
    must NOT restart. This is the behavior we deliberately kept in v0.2.7 —
    preventing accidental retest churn."""
    settings = _make_settings(tmp_path, auto_enroll_mode="quick")
    state = DaemonState.boot(settings)
    orch = Orchestrator(state)
    drive = _make_drive()
    _seed_recent_pass(state, drive)

    monkeypatch.setattr(drive_mod, "discover", lambda: [drive])

    spy = _SpyOrch(orch)
    await _handle_drive_added(state, spy, _make_event(drive))  # type: ignore[arg-type]

    assert spy.start_batch_calls == [], (
        "recently-passed drive must not auto-re-enroll on momentary re-insert"
    )


@pytest.mark.asyncio
async def test_auto_enroll_swallows_batch_rejected(tmp_path, monkeypatch) -> None:
    """If start_batch raises BatchRejected (unexpected double-book race),
    the hotplug handler must log + continue, not propagate.

    Prior to v0.2.7 this exception would propagate up through
    `_hotplug_loop` and kill the monitor task silently."""
    settings = _make_settings(tmp_path, auto_enroll_mode="quick")
    state = DaemonState.boot(settings)
    orch = Orchestrator(state)
    drive = _make_drive()
    _seed_aborted_run(state, drive, completed_minutes_ago=2)

    monkeypatch.setattr(drive_mod, "discover", lambda: [drive])

    spy = _SpyOrch(orch)
    spy.start_batch_should_raise = BatchRejected("already busy")

    # Must NOT raise — the handler catches BatchRejected.
    await _handle_drive_added(state, spy, _make_event(drive))  # type: ignore[arg-type]
    assert len(spy.start_batch_calls) == 1


@pytest.mark.asyncio
async def test_hotplug_loop_survives_handler_exception(
    tmp_path, monkeypatch, caplog
) -> None:
    """Belt-and-suspenders: even if a handler throws an unexpected
    exception type the per-event try/except in `_hotplug_loop` must
    keep the monitor alive."""
    settings = _make_settings(tmp_path)
    state = DaemonState.boot(settings)
    orch = Orchestrator(state)

    # Fake monitor that emits two events and then finishes — the second
    # event should be processed even if the first handler blows up.
    events_seen: list[int] = []

    class _FakeMonitor:
        enabled = True

        def __init__(self) -> None:
            self._stopped = False

        async def events(self):
            yield SimpleNamespace(kind=EventKind.DRIVE_ADDED, serial="X", device_node="/dev/sdX")
            yield SimpleNamespace(kind=EventKind.DRIVE_REMOVED, serial="Y", device_node="/dev/sdY")

        def stop(self) -> None:
            self._stopped = True

    async def _boom(_state, _orch, event) -> None:
        events_seen.append(1)
        raise RuntimeError("simulated handler crash")

    def _remove_spy(_state, _orch, event) -> None:
        events_seen.append(2)

    monkeypatch.setattr("driveforge.daemon.app.HotplugMonitor", _FakeMonitor)
    monkeypatch.setattr("driveforge.daemon.app._handle_drive_added", _boom)
    monkeypatch.setattr("driveforge.daemon.app._handle_drive_removed", _remove_spy)

    await _hotplug_loop(state, orch)

    assert events_seen == [1, 2], (
        "second hotplug event must still be handled after first handler raises"
    )
