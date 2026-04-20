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
            if event.kind == EventKind.DRIVE_ADDED:
                await _handle_drive_added(state, orch, event)
            elif event.kind == EventKind.DRIVE_REMOVED:
                _handle_drive_removed(state, orch, event)
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
    # `match` is a freshly-discovered Pydantic Drive with current device_path;
    # the restore helper reads that directly instead of round-tripping through
    # the DB (which doesn't store device_path — kernel letters drift across
    # reboots).
    orch.restore_blinker_for_drive(match)


def _handle_drive_removed(state: DaemonState, orch: Orchestrator, event) -> None:
    """Cancel any active blinker for the removed drive.

    If the REMOVE event carries a serial, we use that directly. If it
    doesn't (rare — udev usually has cached info even at unplug), we
    don't try to reverse-lookup via the DB: `m.Drive` doesn't persist
    `device_path` (kernel letters drift across reboots), and falling
    back to `filter_by(device_path=...)` would AttributeError on the
    SQLAlchemy row.

    Missing the explicit cancel is safe — the blinker's own read loop
    exits on the next OSError (EIO, ENOENT) when it tries to touch the
    gone device. We just lose the ~0-2 second window between unplug
    and that read catching up.
    """
    if event.serial:
        orch._cancel_blinker(event.serial)  # type: ignore[attr-defined]


async def _poll_io_rates(state: DaemonState) -> None:
    """Background task: sample /proc/diskstats every few seconds and update
    state.active_io_rate for every drive currently assigned to a bay.

    Runs for the daemon's entire lifetime. On non-Linux dev laptops the
    diskstats file doesn't exist and the tracker returns empty — the task
    still runs, it's just a no-op.
    """
    tracker = diskstats.IoRateTracker()
    while True:
        try:
            rates = tracker.poll()
            if rates and state.bay_assignments:
                # Map device basenames back to serials via the cache the
                # orchestrator populates when a drive enters bay_assignments.
                # We *can't* read device_path from the DB — `m.Drive` doesn't
                # persist it, since kernel letters drift across reboots and
                # hotplug shuffles.
                fresh: dict[str, dict[str, float]] = {}
                for serial in list(state.bay_assignments.values()):
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
                # Replace wholesale so serials that just left bay_assignments
                # drop out of the display instead of showing stale rates.
                state.active_io_rate = fresh
            elif not state.bay_assignments:
                state.active_io_rate.clear()
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
