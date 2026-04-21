"""Tests for v0.6.9's async-subprocess migration.

The v0.6.5/6 hot-path sync-in-async fix used
`run_in_executor(drive_command_executor, ...)` to isolate blocking
subprocess calls from the event loop. v0.6.9 goes the next step: the
three hottest sites (the telemetry sampler, `_capture_smart`, and
`_record_telemetry`'s chassis-power read) now use
`asyncio.create_subprocess_exec` directly via new `*_async` twins on
`core.smart` and `core.telemetry`.

These tests cover:
  1. The new async twins exist and return the same shapes as the
     sync originals.
  2. The orchestrator's `_capture_smart` path now routes through
     `smart.snapshot_async` — i.e. the thread-pool offload is gone
     on this path.
  3. `run_async`'s timeout → .kill() contract on a runaway child.
     This is the D-state risk surface the backlog called out.
     We can't simulate a real D-state process in a unit test (it
     needs actual kernel uninterruptible sleep), but we can verify
     that the cancellation path fires `.kill()` on a child that
     doesn't exit on its own — enough to catch regressions where
     someone accidentally removes the `proc.kill()` call.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from driveforge.core import process as process_mod
from driveforge.core import smart as smart_mod
from driveforge.core import telemetry as telemetry_mod

# ----------------------------------------------------------- async twins


@pytest.mark.asyncio
async def test_smart_snapshot_async_exists_and_routes_through_run_async() -> None:
    """Sanity: `smart.snapshot_async` is a real coroutine function, not
    just a sync-with-async-wrapper. Must go through
    `process.run_async` so the subprocess lifecycle is owned by
    asyncio, not a thread pool."""
    assert asyncio.iscoroutinefunction(smart_mod.snapshot_async), (
        "snapshot_async must be a coroutine function"
    )

    async def fake_run_async(argv, **kw):
        # Return the JSON that smart.parse() expects.
        return process_mod.ProcessResult(
            argv=argv,
            returncode=0,
            stdout='{"model_name": "FAKE", "serial_number": "SN-1", "user_capacity": {"bytes": 0}}',
            stderr="",
        )

    # smart.py did `from ...process import run_async` so the name lives
    # in smart's namespace — patch there, not on process_mod.
    with patch("driveforge.core.smart.run_async", side_effect=fake_run_async) as mock_run:
        snap = await smart_mod.snapshot_async("/dev/sdz")

    assert mock_run.call_count == 1
    argv = mock_run.call_args.args[0]
    assert argv[0] == "smartctl"
    assert "--json" in argv
    assert "--all" in argv
    assert snap.model == "FAKE"
    assert snap.serial == "SN-1"


@pytest.mark.asyncio
async def test_smart_snapshot_async_raises_runtime_on_empty_stdout() -> None:
    """Same contract as sync: empty stdout → RuntimeError. Without
    this, a silently-failing smartctl would return None from parse()
    and callers would crash on attribute access downstream."""

    async def fake_run_async(argv, **kw):
        return process_mod.ProcessResult(argv=argv, returncode=0, stdout="", stderr="whoops")

    with patch("driveforge.core.smart.run_async", side_effect=fake_run_async):
        with pytest.raises(RuntimeError, match="no output"):
            await smart_mod.snapshot_async("/dev/sdz")


@pytest.mark.asyncio
async def test_read_chassis_power_async_returns_watts_on_success() -> None:
    """Parser behavior matches the sync twin. Regex lives on the
    module level so both variants share it; we just want to confirm
    the async path still dispatches to it correctly."""

    async def fake_run_async(argv, **kw):
        return process_mod.ProcessResult(
            argv=argv,
            returncode=0,
            stdout="Instantaneous power reading : 172 Watts\n",
            stderr="",
        )

    with patch("driveforge.core.telemetry.run_async", side_effect=fake_run_async):
        watts = await telemetry_mod.read_chassis_power_async()

    assert watts == 172.0


@pytest.mark.asyncio
async def test_read_chassis_power_async_returns_none_on_nonzero_rc() -> None:
    """BMC unreachable → ipmitool exits non-zero → we return None
    rather than raising. Same contract as sync."""

    async def fake_run_async(argv, **kw):
        return process_mod.ProcessResult(argv=argv, returncode=1, stdout="", stderr="timeout")

    with patch("driveforge.core.telemetry.run_async", side_effect=fake_run_async):
        watts = await telemetry_mod.read_chassis_power_async()

    assert watts is None


@pytest.mark.asyncio
async def test_read_chassis_power_async_returns_none_on_missing_ipmitool() -> None:
    """On dev boxes without ipmitool, FileNotFoundError surfaces
    from run_async. Telemetry is best-effort; returning None lets
    the sampler keep going instead of crashing on startup."""

    async def fake_run_async(*args, **kw):
        raise FileNotFoundError("ipmitool")

    with patch("driveforge.core.telemetry.run_async", side_effect=fake_run_async):
        watts = await telemetry_mod.read_chassis_power_async()

    assert watts is None


# ----------------------------------------------------- timeout → .kill()


@pytest.mark.asyncio
async def test_run_async_kills_child_on_timeout() -> None:
    """D-state risk surface. The backlog called this out explicitly:
    when `.communicate()` hits its timeout, the timeout path must
    call `proc.kill()` so the asyncio child handle is released. A
    D-state kernel task won't exit (SIGKILL doesn't dent it; the
    kernel's the one holding it), but the asyncio SubprocessProtocol
    has to be told to stop waiting so the event loop isn't wedged.

    We simulate a hanging child by mocking `asyncio.create_subprocess_exec`
    to return a stub whose .communicate() times out. Verify:
      1. `asyncio.TimeoutError` is raised (propagated to caller).
      2. `proc.kill()` was called before the raise.
      3. `proc.wait()` was awaited (so the SubprocessProtocol gets
         its final close).
    """
    kill_called = []
    wait_called = []

    class _StubProc:
        pid = 99999
        returncode = None

        async def communicate(self):
            # Never-completing coroutine that also respects cancellation.
            await asyncio.sleep(3600)
            return (b"", b"")

        def kill(self):
            kill_called.append(True)

        async def wait(self):
            wait_called.append(True)

    async def fake_create_subprocess_exec(*argv, **kw):
        return _StubProc()

    with patch.object(asyncio, "create_subprocess_exec", side_effect=fake_create_subprocess_exec):
        with pytest.raises(asyncio.TimeoutError):
            await process_mod.run_async(["fake-hang"], timeout=0.05)

    assert kill_called, "proc.kill() must be called on timeout"
    assert wait_called, "proc.wait() must be awaited after kill"


@pytest.mark.asyncio
async def test_run_async_unregisters_pid_on_timeout() -> None:
    """Owner-registered subprocesses must be unregistered on timeout
    too — not just on normal completion. Otherwise kill_owner() on a
    later abort would try to signal a PID that's already been killed
    by the timeout path, which is harmless but noisy."""
    from driveforge.core import process as pm

    class _StubProc:
        pid = 77777
        returncode = None

        async def communicate(self):
            await asyncio.sleep(3600)

        def kill(self):
            pass

        async def wait(self):
            pass

    async def fake_exec(*argv, **kw):
        return _StubProc()

    with patch.object(asyncio, "create_subprocess_exec", side_effect=fake_exec):
        with pytest.raises(asyncio.TimeoutError):
            await pm.run_async(["fake"], timeout=0.02, owner="DRIVE-TEST")

    assert pm.active_pids("DRIVE-TEST") == [], (
        "owner-registered PID must be unregistered after timeout"
    )


# ------------------------------------------------- orchestrator integration


@pytest.mark.asyncio
async def test_capture_smart_uses_snapshot_async_not_executor(tmp_path) -> None:
    """Regression guard for the v0.6.9 migration. `_capture_smart`
    must call `smart.snapshot_async` (async path) — NOT
    `smart.snapshot` via `run_in_executor`. Patching both, the async
    twin must receive the call and the sync twin must stay
    untouched."""
    from driveforge import config as cfg
    from driveforge.daemon.orchestrator import Orchestrator
    from driveforge.daemon.state import DaemonState
    from driveforge.db import models as m

    settings = cfg.Settings()
    settings.daemon.state_dir = tmp_path
    settings.daemon.db_path = tmp_path / "driveforge.db"
    settings.daemon.pending_labels_dir = tmp_path / "pending-labels"
    settings.daemon.reports_dir = tmp_path / "reports"

    state = DaemonState.boot(settings)
    orch = Orchestrator(state)

    # Seed a drive + run row so the DB writes succeed.
    with state.session_factory() as session:
        session.add(
            m.Drive(
                serial="CAP-SMART-1",
                model="FAKE",
                capacity_bytes=1_000_000_000,
                transport="sata",
            )
        )
        run_row = m.TestRun(drive_serial="CAP-SMART-1", phase="pre_smart", quick_mode=False)
        session.add(run_row)
        session.commit()
        session.refresh(run_row)
        run_id = run_row.id

    fake_snap = SimpleNamespace(
        temperature_c=40,
        reallocated_sectors=0,
        current_pending_sector=0,
        captured_at=None,
        model_dump=lambda mode: {},
    )

    sync_called = MagicMock()
    async_called = AsyncMock(return_value=fake_snap)

    from driveforge.core import drive as drive_mod

    fake_drive = SimpleNamespace(
        serial="CAP-SMART-1",
        device_path="/dev/sdz",
        transport=drive_mod.Transport("sata"),
        model="FAKE",
        capacity_bytes=1_000_000_000,
    )

    # Need _record_telemetry to not actually hit ipmitool.
    async def fake_record_telemetry(*args, **kwargs):
        return None

    with patch(
        "driveforge.daemon.orchestrator.smart.snapshot",
        side_effect=sync_called,
    ), patch(
        "driveforge.daemon.orchestrator.smart.snapshot_async",
        new=async_called,
    ), patch.object(orch, "_record_telemetry", side_effect=fake_record_telemetry):
        await orch._capture_smart(run_id, fake_drive, kind="pre")

    assert async_called.call_count == 1, "snapshot_async must be called"
    assert sync_called.call_count == 0, (
        "snapshot (sync) must NOT be called — v0.6.9 migrated this site"
    )


@pytest.mark.asyncio
async def test_record_telemetry_is_async_and_uses_read_chassis_power_async(tmp_path) -> None:
    """Regression guard: `_record_telemetry` was sync + called
    `telemetry.read_chassis_power()` synchronously from `_capture_smart`,
    which was an unaudited sync-in-async site in v0.6.6. v0.6.9
    converted it to async and routes chassis-power through the async
    twin. Confirm both."""
    from driveforge import config as cfg
    from driveforge.daemon.orchestrator import Orchestrator
    from driveforge.daemon.state import DaemonState
    from driveforge.db import models as m

    settings = cfg.Settings()
    settings.daemon.state_dir = tmp_path
    settings.daemon.db_path = tmp_path / "driveforge.db"
    settings.daemon.pending_labels_dir = tmp_path / "pending-labels"
    settings.daemon.reports_dir = tmp_path / "reports"

    state = DaemonState.boot(settings)
    orch = Orchestrator(state)

    with state.session_factory() as session:
        session.add(
            m.Drive(
                serial="REC-TEL-1",
                model="FAKE",
                capacity_bytes=1_000_000_000,
                transport="sata",
            )
        )
        run_row = m.TestRun(drive_serial="REC-TEL-1", phase="pre_smart", quick_mode=False)
        session.add(run_row)
        session.commit()
        session.refresh(run_row)
        run_id = run_row.id

    assert asyncio.iscoroutinefunction(orch._record_telemetry), (
        "_record_telemetry must be async in v0.6.9+"
    )

    async_power = AsyncMock(return_value=145.0)
    sync_power = MagicMock()

    with patch(
        "driveforge.daemon.orchestrator.telemetry.read_chassis_power",
        side_effect=sync_power,
    ), patch(
        "driveforge.daemon.orchestrator.telemetry.read_chassis_power_async",
        new=async_power,
    ):
        await orch._record_telemetry(
            run_id,
            "REC-TEL-1",
            phase="pre_smart",
            drive_temp_c=42,
        )

    assert async_power.call_count == 1
    assert sync_power.call_count == 0

    # The DB write happened synchronously on the event loop thread —
    # fine for SQLite — and carries both the drive temp and the
    # async-fetched chassis power.
    with state.session_factory() as session:
        samples = session.query(m.TelemetrySample).filter_by(test_run_id=run_id).all()
    assert len(samples) == 1
    assert samples[0].drive_temp_c == 42
    assert samples[0].chassis_power_w == 145.0
