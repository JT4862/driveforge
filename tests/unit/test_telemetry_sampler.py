"""Tests for the v0.5.5 periodic telemetry sampler.

Pre-v0.5.5 telemetry was only written at SMART-snapshot phase boundaries
(pre + post), producing 2-sample charts on multi-hour runs \u2014 the bug
observed on a 5-hour R720 run (Dell 300 GB SAS, grade A). The sampler
fixes this by running a background asyncio task per active pipeline
that emits a TelemetrySample row every
`settings.daemon.telemetry_sample_interval_s` seconds.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from driveforge import config as cfg
from driveforge.core.drive import Drive, Transport
from driveforge.daemon.orchestrator import Orchestrator
from driveforge.daemon.state import DaemonState
from driveforge.db import models as m


def _make_settings(tmp_path: Path) -> cfg.Settings:
    settings = cfg.Settings()
    settings.daemon.state_dir = tmp_path
    settings.daemon.db_path = tmp_path / "driveforge.db"
    settings.daemon.pending_labels_dir = tmp_path / "pending-labels"
    settings.daemon.reports_dir = tmp_path / "reports"
    # Low interval for fast-running tests \u2014 clamped to 5 inside the
    # sampler, which is still too slow for a unit test; the sampler
    # tests use monkey-patched asyncio.sleep.
    settings.daemon.telemetry_sample_interval_s = 30
    return settings


def _make_drive(serial: str = "SN-TELE-1") -> Drive:
    return Drive(
        serial=serial,
        model="TESTDRIVE-TELE",
        capacity_bytes=1_000_000_000,
        transport=Transport.SATA,
        device_path="/dev/sdz",
        rotation_rate=0,
        firmware_version="TEST0001",
        manufacturer="Testco",
    )


def _seed_run(state: DaemonState, drive: Drive) -> int:
    """Create a TestRun row so the sampler has a run_id to attach samples to."""
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
        run = m.TestRun(drive_serial=drive.serial, phase="pre_smart", quick_mode=False)
        session.add(run)
        session.commit()
        session.refresh(run)
        return run.id


def test_sample_drive_temp_quietly_returns_none_on_smartctl_failure(tmp_path) -> None:
    """A transient smartctl failure must not crash the sampler \u2014 return None
    and let the sampler loop continue. Temperature gaps are acceptable;
    dead samplers are not."""
    settings = _make_settings(tmp_path)
    state = DaemonState.boot(settings)
    orch = Orchestrator(state)
    drive = _make_drive()

    with patch("driveforge.daemon.orchestrator.smart.snapshot", side_effect=RuntimeError("smartctl exploded")):
        temp = orch._sample_drive_temp_quietly(drive)

    assert temp is None


def test_sample_drive_temp_quietly_returns_temp_on_success(tmp_path) -> None:
    settings = _make_settings(tmp_path)
    state = DaemonState.boot(settings)
    orch = Orchestrator(state)
    drive = _make_drive()

    fake_snap = SimpleNamespace(temperature_c=42)
    with patch("driveforge.daemon.orchestrator.smart.snapshot", return_value=fake_snap):
        temp = orch._sample_drive_temp_quietly(drive)

    assert temp == 42


@pytest.mark.asyncio
async def test_sampler_writes_samples_to_db(tmp_path) -> None:
    """End-to-end: start the sampler, let it tick a few times, cancel it.
    The DB should contain TelemetrySample rows \u2014 more than the 2 samples
    that the pre-v0.5.5 two-phase-boundary-only code would produce.
    """
    settings = _make_settings(tmp_path)
    # Force a fast interval that passes the min(5) clamp.
    settings.daemon.telemetry_sample_interval_s = 5
    state = DaemonState.boot(settings)
    orch = Orchestrator(state)
    drive = _make_drive()
    run_id = _seed_run(state, drive)
    state.active_phase[drive.serial] = "badblocks"

    fake_snap = SimpleNamespace(temperature_c=35)
    sleep_calls: list[float] = []

    _real_sleep = asyncio.sleep

    async def fast_sleep(duration: float) -> None:
        sleep_calls.append(duration)
        # Let the loop make progress in real time without actually sleeping.
        # Use the saved reference to avoid recursing into our own patch.
        await _real_sleep(0)

    with patch("driveforge.daemon.orchestrator.smart.snapshot", return_value=fake_snap), \
         patch("driveforge.daemon.orchestrator.telemetry.read_chassis_power", return_value=180.0), \
         patch("driveforge.daemon.orchestrator.asyncio.sleep", side_effect=fast_sleep):
        task = asyncio.create_task(orch._telemetry_sampler_loop(run_id, drive))
        # Let several sampling iterations run. Use the saved real-sleep
        # reference so we don't trigger the orchestrator module's patched
        # sleep from inside this test.
        for _ in range(10):
            await _real_sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    with state.session_factory() as session:
        samples = session.query(m.TelemetrySample).filter_by(test_run_id=run_id).all()

    assert len(samples) > 2, (
        f"sampler must record more than the 2-boundary pre-v0.5.5 baseline; "
        f"got {len(samples)} samples"
    )
    # Each recorded sample should carry the active phase + the mocked temp.
    for s in samples:
        assert s.phase == "badblocks"
        assert s.drive_temp_c == 35
        assert s.chassis_power_w == 180.0


@pytest.mark.asyncio
async def test_sampler_survives_transient_smartctl_errors(tmp_path) -> None:
    """If smartctl blows up mid-run, the sampler must log + continue \u2014
    NOT die silently, leaving the telemetry chart blank for the rest of
    the run. Simulated by alternating success / RuntimeError from
    smart.snapshot."""
    settings = _make_settings(tmp_path)
    settings.daemon.telemetry_sample_interval_s = 5
    state = DaemonState.boot(settings)
    orch = Orchestrator(state)
    drive = _make_drive()
    run_id = _seed_run(state, drive)
    state.active_phase[drive.serial] = "badblocks"

    call_count = {"n": 0}

    def flaky_snapshot(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] % 2 == 0:
            raise RuntimeError("smartctl transient error")
        return SimpleNamespace(temperature_c=30 + call_count["n"])

    _real_sleep = asyncio.sleep

    async def fast_sleep(duration: float) -> None:
        await _real_sleep(0)

    with patch("driveforge.daemon.orchestrator.smart.snapshot", side_effect=flaky_snapshot), \
         patch("driveforge.daemon.orchestrator.telemetry.read_chassis_power", return_value=200.0), \
         patch("driveforge.daemon.orchestrator.asyncio.sleep", side_effect=fast_sleep):
        task = asyncio.create_task(orch._telemetry_sampler_loop(run_id, drive))
        for _ in range(10):
            await _real_sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    with state.session_factory() as session:
        samples = session.query(m.TelemetrySample).filter_by(test_run_id=run_id).all()

    # Some samples will have temp=None (the transient failure ticks) and
    # others will have the fake temp. The sampler must keep going through
    # both, so we should see multiple of each.
    none_temp_count = sum(1 for s in samples if s.drive_temp_c is None)
    int_temp_count = sum(1 for s in samples if s.drive_temp_c is not None)
    assert none_temp_count > 0, "sampler never hit a smartctl-failure tick"
    assert int_temp_count > 0, "sampler never hit a smartctl-success tick"


@pytest.mark.asyncio
async def test_sampler_cancels_cleanly(tmp_path) -> None:
    """Cancellation must propagate without hanging the orchestrator's
    pipeline cleanup. The sampler's cancellation is what stops telemetry
    collection at pipeline end; if it doesn't respect cancel, the task
    leaks."""
    settings = _make_settings(tmp_path)
    settings.daemon.telemetry_sample_interval_s = 5
    state = DaemonState.boot(settings)
    orch = Orchestrator(state)
    drive = _make_drive()
    run_id = _seed_run(state, drive)

    fake_snap = SimpleNamespace(temperature_c=40)

    with patch("driveforge.daemon.orchestrator.smart.snapshot", return_value=fake_snap), \
         patch("driveforge.daemon.orchestrator.telemetry.read_chassis_power", return_value=175.0):
        task = asyncio.create_task(orch._telemetry_sampler_loop(run_id, drive))
        await asyncio.sleep(0.05)  # give it a moment to enter the loop
        task.cancel()
        await asyncio.wait_for(task, timeout=1.0)  # must complete within 1s

    assert task.cancelled() or task.done()
