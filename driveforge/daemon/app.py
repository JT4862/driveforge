"""FastAPI daemon entrypoint.

Boots DriveForge: loads config, initializes DB, mounts the REST API and
web UI, and serves on the configured host/port.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from driveforge import config as cfg
from driveforge.core import diskstats, drive as drive_mod
from driveforge.core.hotplug import EventKind, Monitor as HotplugMonitor
from driveforge.daemon.api import router as api_router
from driveforge.daemon.orchestrator import Orchestrator
from driveforge.daemon.state import DaemonState, get_state, set_state
from driveforge.db import models as m
from driveforge.web.routes import router as web_router
from driveforge.web.routes import templates as web_templates
from driveforge.web.setup import router as setup_router

logger = logging.getLogger(__name__)

PACKAGE_ROOT = Path(__file__).parent.parent
STATIC_DIR = PACKAGE_ROOT / "web" / "static"

# Cadence for the /proc/diskstats live-rate poller. 3 s is short enough that
# a stalled drive is obvious on the dashboard within one refresh cycle, and
# long enough that rounding/quantization doesn't dominate the rate display.
IO_RATE_POLL_INTERVAL_SEC = 3.0


def _flag_dangling_runs_as_interrupted(state: DaemonState) -> None:
    """Flag TestRuns left open (completed_at IS NULL) as interrupted so
    the re-insert handler picks up recovery automatically.

    Catches daemon-crash / systemd-restart / package-upgrade scenarios
    where the pipeline task was killed mid-run without going through its
    except clause. The in-flight failure path in `_run_drive` itself uses
    a device-existence check to flag pulls — so we don't need a
    heuristic sweep of closed-as-failed runs at startup; the live check
    handles that class of pull deterministically.

    Never overwrites an already-set `interrupted_at_phase`.
    """
    from driveforge.db import models as m

    with state.session_factory() as session:
        open_dangling = (
            session.query(m.TestRun)
            .filter(m.TestRun.completed_at.is_(None))
            .filter(m.TestRun.interrupted_at_phase.is_(None))
            .all()
        )
        for run in open_dangling:
            run.interrupted_at_phase = run.phase
            logger.warning(
                "daemon startup: flagging open dangling run %d (drive=%s, phase=%s) as interrupted",
                run.id, run.drive_serial, run.phase,
            )
        if open_dangling:
            session.commit()
            logger.warning(
                "daemon startup: %d dangling run(s) flagged for pull-recovery",
                len(open_dangling),
            )


async def _trigger_recovery_for_present_drives(state: DaemonState, orch: Orchestrator) -> None:
    """After `_flag_dangling_runs_as_interrupted`, any drives physically
    present at daemon startup that have an open interrupted TestRun
    should have recovery kicked off immediately — the hotplug `add` event
    path that normally triggers recovery won't fire for drives that were
    inserted before the daemon started.

    Covers the common upgrade scenario: `install.sh` restarts the daemon
    mid-batch, the new daemon's startup sweep flags the in-progress runs
    as interrupted, then we need to actively dispatch recovery instead of
    waiting for a re-insert that will never happen.
    """
    try:
        present = drive_mod.discover()
    except Exception:  # noqa: BLE001
        logger.info("startup recovery sweep: drive discovery unavailable, skipping")
        return
    recovered = 0
    for d in present:
        try:
            if await orch.recover_drive(d):
                recovered += 1
        except Exception:  # noqa: BLE001
            logger.exception("startup recovery sweep: failed for drive %s", d.serial)
    if recovered:
        logger.warning("startup recovery sweep: dispatched recovery for %d drive(s)", recovered)


def _restore_blinkers_on_startup(state: DaemonState, orch: Orchestrator) -> None:
    """Re-spawn done-blinkers for drives that are physically present and
    have a clear last-run verdict. Called once at daemon boot.

    Silent no-op if drive discovery fails (non-Linux dev environment,
    missing lsblk, etc.) — the feature is best-effort, not load-bearing.
    """
    try:
        present = drive_mod.discover()
    except Exception:  # noqa: BLE001
        logger.info("startup blinker restore: drive discovery unavailable, skipping")
        return
    restored = 0
    for d in present:
        before = len(state.done_blinkers)
        orch.restore_blinker_for_drive(d)
        if len(state.done_blinkers) > before:
            restored += 1
    if restored:
        logger.info("startup blinker restore: spawned %d blinker(s)", restored)


async def _hotplug_loop(state: DaemonState, orch: Orchestrator) -> None:
    """Consume hotplug events. On DRIVE_ADDED, re-enroll the drive in the
    DB (fresh device_path / serial mapping) and restore its post-run
    blinker if it has a completed test history. On DRIVE_REMOVED, cancel
    any active blinker for that drive (the blinker self-exits on OSError
    already, but cancelling is cleaner).

    Linux-only in practice. On macOS / BSD the Monitor is a no-op that
    idles forever.
    """
    monitor = HotplugMonitor()
    if not monitor.enabled:
        logger.info("hotplug loop: monitor disabled on this platform, idling")
        # Still need to sleep-forever so the lifespan cancellation works;
        # the monitor's own events() does the same, so just call it.
    try:
        async for event in monitor.events():
            # Per-event try/except so one handler blowing up doesn't
            # starve the daemon of all subsequent hotplug events. The
            # inner handlers already do their own defensive handling,
            # but this is the load-bearing guarantee that the monitor
            # task survives.
            try:
                if event.kind == EventKind.DRIVE_ADDED:
                    await _handle_drive_added(state, orch, event)
                elif event.kind == EventKind.DRIVE_REMOVED:
                    _handle_drive_removed(state, orch, event)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception(
                    "hotplug handler for event %s crashed; continuing",
                    event,
                )
    except asyncio.CancelledError:
        monitor.stop()
        raise


async def _handle_drive_added(state: DaemonState, orch: Orchestrator, event) -> None:
    """Re-discover the inserted drive so we have a fresh device_path,
    update the DB, then restore the blinker."""
    # Re-walk lsblk — hotplug events can arrive before the block device
    # is fully settled; a short retry burst handles races cleanly.
    present = []
    for _attempt in range(5):
        try:
            present = drive_mod.discover()
        except Exception:  # noqa: BLE001
            present = []
        if any(d.serial == event.serial for d in present) or any(
            d.device_path == event.device_node for d in present
        ):
            break
        await asyncio.sleep(0.4)
    match = None
    for d in present:
        if event.serial and d.serial == event.serial:
            match = d
            break
        if event.device_node and d.device_path == event.device_node:
            match = d
            break
    if match is None:
        logger.debug("hotplug add: no matching drive for event %s", event)
        return
    # Priority order on drive-insert:
    #   1. Recovery — drive was pulled mid-pipeline and has an open
    #      interrupted run. Takes precedence over everything: the drive is
    #      in an ambiguous state (SAS: "Medium format corrupted"; SATA:
    #      security-locked) and needs repair before we do anything with it.
    #   2. Blinker restore — drive with a completed test history gets its
    #      pass/fail activity-LED cadence restarted.
    #   3. Auto-enroll (future) — start a fresh pipeline if the user has
    #      opted in to "auto-test on insert".
    if await orch.recover_drive(match):
        logger.info("hotplug add: drive %s entered recovery flow", match.serial)
        return
    orch.restore_blinker_for_drive(match)

    # Auto-enroll — optional, operator opts in via the dashboard toggle.
    # Priority-lower than recovery + blinker restore. Skipped silently when:
    #   - auto_enroll_mode is "off" (default)
    #   - drive is already running in this daemon
    #   - drive's LATEST completed run has a real grade (A/B/C/fail),
    #     regardless of age. Graded drives are "done"; a pull + re-insert
    #     must not start a re-test on its own. The operator can re-run
    #     manually via New Batch if they want to. Re-test churn on a
    #     shelf full of pre-tested drives was the v0.2.9 bug this
    #     closes.
    #
    # The "latest-run" framing is the v0.2.7 key insight: we key the
    # decision off the single most recent completed run. Aborted runs
    # stamp grade=None (v0.2.6) so they still let auto-enroll fire on
    # re-insert — the "retest after an abort" case is explicitly
    # supported because the operator's earlier click was a cancel, not
    # a verdict.
    #
    # Prior to v0.2.9 this had a 1-hour cutoff instead of indefinite —
    # a drive that graded A in the morning would re-run itself on
    # afternoon shelf-check. That's the opposite of what operators
    # want.
    mode = (state.settings.daemon.auto_enroll_mode or "off").lower()
    if mode not in ("quick", "full"):
        return
    if match.serial in state.active_phase:
        return
    from datetime import UTC
    from driveforge.db import models as m
    from driveforge.daemon.orchestrator import BatchRejected
    with state.session_factory() as session:
        latest = (
            session.query(m.TestRun)
            .filter_by(drive_serial=match.serial)
            .filter(m.TestRun.completed_at.isnot(None))
            .order_by(m.TestRun.completed_at.desc())
            .first()
        )
    # SQLite stores DateTime(timezone=True) columns as naive strings and
    # SQLAlchemy surfaces them as naive Python datetimes. Normalize to
    # UTC-aware for logging. (Kept even though we no longer compare to
    # a cutoff — the isoformat() string is friendlier with tz info.)
    latest_completed = latest.completed_at if latest is not None else None
    if latest_completed is not None and latest_completed.tzinfo is None:
        latest_completed = latest_completed.replace(tzinfo=UTC)
    # v0.5.1+ auto-enroll stickiness: A/B/C/F are VERDICTS ABOUT THE
    # DRIVE and stick indefinitely. "error" is a verdict about the
    # SOFTWARE (pipeline broke) — we WANT auto-retest on re-insert
    # because the error might have been transient (transient bus
    # glitch, temporary daemon issue, etc.). Aborted (grade=NULL) also
    # auto-retests since the abort wasn't a verdict.
    #
    # Legacy "fail" rows (pre-v0.5.1) are sticky too — treat them like
    # F, since we can't retroactively tell real-fail from pipeline-error.
    # Operator can manually retest via New Batch.
    STICKY_GRADES = ("A", "B", "C", "F", "fail")
    if latest is not None and latest.grade in STICKY_GRADES:
        logger.info(
            "hotplug add: drive %s has a sticky graded run (%s, %s); "
            "skipping auto-enroll",
            match.serial,
            latest.grade,
            latest_completed.isoformat() if latest_completed else "?",
        )
        return
    if latest is not None and latest.grade == "error":
        logger.info(
            "hotplug add: drive %s's latest run was a pipeline error "
            "(%s); firing auto-enroll retry",
            match.serial,
            latest_completed.isoformat() if latest_completed else "?",
        )
    logger.info("hotplug add: auto-enrolling drive %s (%s mode)", match.serial, mode)
    try:
        await orch.start_batch(
            [match],
            source=f"auto-enroll ({mode})",
            quick=(mode == "quick"),
        )
    except BatchRejected as exc:
        # Defensive: the drive is already busy in another batch somehow.
        # Log and move on instead of letting the exception kill the
        # hotplug loop — prior to v0.2.7 a stale self._tasks entry could
        # cause this path to fire for a perfectly-idle drive.
        logger.warning(
            "hotplug add: auto-enroll rejected for %s: %s",
            match.serial,
            exc,
        )
    except Exception:  # noqa: BLE001
        # Same defensive posture for any other orchestrator misbehaviour.
        # Hotplug loop MUST keep running; a single failed auto-enroll
        # must not starve the rest of the daemon's insert handling.
        logger.exception(
            "hotplug add: auto-enroll crashed for %s; hotplug loop continues",
            match.serial,
        )


def _handle_drive_removed(state: DaemonState, orch: Orchestrator, event) -> None:
    """Cancel any active blinker for the removed drive + flag pull-interrupted
    pipelines for recovery on re-insert.

    If the REMOVE event carries a serial, we use that directly. If it
    doesn't (rare — udev usually has cached info even at unplug), we
    don't try to reverse-lookup via the DB: `m.Drive` doesn't persist
    `device_path` (kernel letters drift across reboots), and falling
    back to `filter_by(device_path=...)` would AttributeError on the
    SQLAlchemy row.

    Interrupt detection: if the removed drive is currently in
    `state.active_phase`, mark its open TestRun with `interrupted_at_phase`
    so the re-insert handler can find it and kick off a recovery flow.
    This runs BEFORE the subprocess-failure path fires (the hdparm /
    sg_format call inside the pipeline task will error out a moment
    later when its I/O starts failing against the gone device).
    """
    # Resolve serial — udev's remove event usually carries it, but
    # sometimes it's been cleared by the time we get the event. Fall back
    # to reverse-lookup via device_basenames (populated by the orchestrator
    # when the drive entered active_phase).
    serial = event.serial
    if not serial and event.device_node:
        basename = event.device_node.rsplit("/", 1)[-1]
        for s, bn in state.device_basenames.items():
            if bn == basename:
                serial = s
                logger.info(
                    "hotplug remove: event had no serial; recovered %s from device_basenames[%s]",
                    serial, basename,
                )
                break
    if not serial:
        return
    orch._cancel_blinker(serial)  # type: ignore[attr-defined]
    interrupted_phase = state.active_phase.get(serial)
    if interrupted_phase is not None:
        state.interrupted_serials.add(serial)
        # Stamp the open TestRun so recovery can find it after daemon restart
        # or page refresh. We look for the most recent non-completed run for
        # this serial — the pipeline creates exactly one such row per batch.
        from driveforge.db import models as m
        with state.session_factory() as session:
            run = (
                session.query(m.TestRun)
                .filter_by(drive_serial=serial, completed_at=None)
                .order_by(m.TestRun.started_at.desc())
                .first()
            )
            if run is not None:
                run.interrupted_at_phase = interrupted_phase
                session.commit()
        logger.warning(
            "drive %s pulled during phase=%s — flagged for recovery on re-insert",
            serial,
            interrupted_phase,
        )


async def _poll_io_rates(state: DaemonState) -> None:
    """Background task: sample /proc/diskstats every few seconds and update
    state.active_io_rate for every drive currently assigned to a bay.

    Runs for the daemon's entire lifetime. On non-Linux dev laptops the
    diskstats file doesn't exist and the tracker returns empty — the task
    still runs, it's just a no-op.
    """
    tracker = diskstats.IoRateTracker()
    # 10 samples at 3-second polling = a 30-second sparkline window.
    HISTORY_MAX = 10
    while True:
        try:
            rates = tracker.poll()
            active = state.active_serials()
            if rates and active:
                # Map device basenames back to serials via the cache the
                # orchestrator populates when a drive enters active_phase.
                # We *can't* read device_path from the DB — `m.Drive` doesn't
                # persist it, since kernel letters drift across reboots and
                # hotplug shuffles.
                fresh: dict[str, dict[str, float]] = {}
                for serial in active:
                    dev_name = state.device_basenames.get(serial)
                    if not dev_name:
                        continue
                    rate = rates.get(dev_name)
                    if rate is None:
                        continue
                    fresh[serial] = {
                        "read_mbps": round(rate.read_mbps, 1),
                        "write_mbps": round(rate.write_mbps, 1),
                    }
                    # Append to rolling history for the sparkline. Store
                    # total throughput (read + write) as a single series so
                    # the sparkline is one polyline rather than two.
                    history = state.active_io_history.setdefault(serial, [])
                    history.append({
                        "read": round(rate.read_mbps, 1),
                        "write": round(rate.write_mbps, 1),
                    })
                    if len(history) > HISTORY_MAX:
                        del history[: len(history) - HISTORY_MAX]
                # Replace wholesale so serials that just left active_phase
                # drop out of the display instead of showing stale rates.
                state.active_io_rate = fresh
                # Trim history for serials no longer active.
                for stale in list(state.active_io_history.keys() - active):
                    state.active_io_history.pop(stale, None)
            elif not active:
                state.active_io_rate.clear()
                state.active_io_history.clear()
        except Exception:  # noqa: BLE001
            # Never let a transient error kill the poller — the dashboard
            # losing a rate display is fine; a crashed background task is not.
            logger.exception("io-rate poll iteration failed")
        await asyncio.sleep(IO_RATE_POLL_INTERVAL_SEC)


def make_app(settings: cfg.Settings) -> FastAPI:
    state = DaemonState.boot(settings)
    set_state(state)
    orch = Orchestrator(state)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        state.orchestrator = orch  # type: ignore[attr-defined]
        io_task = asyncio.create_task(_poll_io_rates(state))
        # One-shot: for drives already present at startup, upsert them into
        # the DB and restore their post-run blinker based on last grade.
        # Covers the "daemon restarted while a previously-tested drive was
        # sitting in a bay" case — without this, restart would silence the
        # LED pattern until the next batch.
        try:
            _restore_blinkers_on_startup(state, orch)
        except Exception:  # noqa: BLE001
            logger.exception("startup blinker restore failed (non-fatal)")
        # Pick up any TestRuns left dangling by a crash, restart, or the
        # pre-v0.2.2 case where a drive was pulled while the old daemon
        # couldn't stamp the interrupt.
        try:
            _flag_dangling_runs_as_interrupted(state)
        except Exception:  # noqa: BLE001
            logger.exception("startup dangling-run flag failed (non-fatal)")
        # For drives that are physically present at startup AND now carry
        # a freshly-flagged interrupted run, dispatch recovery NOW — the
        # hotplug `add` path would never fire for drives that were there
        # before the daemon started (daemon-restart-mid-batch scenario).
        try:
            await _trigger_recovery_for_present_drives(state, orch)
        except Exception:  # noqa: BLE001
            logger.exception("startup recovery dispatch failed (non-fatal)")
        hotplug_task = asyncio.create_task(_hotplug_loop(state, orch))
        logger.info(
            "driveforge-daemon ready on %s:%d (dev_mode=%s)",
            settings.daemon.host,
            settings.daemon.port,
            settings.dev_mode,
        )
        yield
        for task in (io_task, hotplug_task):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        logger.info("driveforge-daemon shutting down")

    from driveforge.version import __version__ as DRIVEFORGE_VERSION

    app = FastAPI(
        title="DriveForge",
        description="In-house enterprise drive refurbishment pipeline",
        version=DRIVEFORGE_VERSION,
        lifespan=lifespan,
    )
    @app.middleware("http")
    async def setup_gate(request: Request, call_next):
        """Force users through the setup wizard until it completes."""
        s = get_state().settings
        path = request.url.path
        if not s.setup_completed and not (
            path.startswith("/setup")
            or path.startswith("/static")
            or path.startswith("/api")
        ):
            return RedirectResponse(url="/setup/1", status_code=303)
        return await call_next(request)

    app.include_router(api_router)
    app.include_router(setup_router)
    app.include_router(web_router)
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    # Expose templates for web routes
    app.state.templates = web_templates  # type: ignore[attr-defined]
    app.state.orchestrator = orch  # type: ignore[attr-defined]
    # Make __version__ available to every Jinja template (used by base.html
    # footer + the About panel) so version bumps only touch version.py.
    web_templates.env.globals["driveforge_version"] = DRIVEFORGE_VERSION
    return app


def main() -> None:
    parser = argparse.ArgumentParser(prog="driveforge-daemon")
    parser.add_argument("--config", type=Path, help="path to driveforge.yaml")
    parser.add_argument("--dev", action="store_true", help="dev mode (no real hardware)")
    parser.add_argument("--fixtures", type=Path, help="fixtures directory (implies --dev)")
    parser.add_argument("--host", help="override bind host")
    parser.add_argument("--port", type=int, help="override bind port")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    settings = cfg.load(args.config)
    if args.dev or args.fixtures:
        settings.dev_mode = True
    if args.fixtures:
        settings.fixtures_dir = args.fixtures
        # In dev mode, use a local SQLite DB + reports dir so we don't touch /var
        local_state = Path.cwd() / ".driveforge-dev"
        settings.daemon.state_dir = local_state
        settings.daemon.db_path = local_state / "driveforge.db"
        settings.daemon.pending_labels_dir = local_state / "pending-labels"
        settings.daemon.reports_dir = local_state / "reports"
    if args.host:
        settings.daemon.host = args.host
    if args.port:
        settings.daemon.port = args.port

    app = make_app(settings)
    uvicorn.run(app, host=settings.daemon.host, port=settings.daemon.port, log_level="info")


if __name__ == "__main__":
    main()
