"""FastAPI daemon entrypoint.

Boots DriveForge: loads config, initializes DB, mounts the REST API and
web UI, and serves on the configured host/port.
"""

from __future__ import annotations

import argparse
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from driveforge import config as cfg
from driveforge.daemon.api import router as api_router
from driveforge.daemon.orchestrator import Orchestrator
from driveforge.daemon.state import DaemonState, get_state, set_state
from driveforge.web.routes import router as web_router
from driveforge.web.routes import templates as web_templates
from driveforge.web.setup import router as setup_router

logger = logging.getLogger(__name__)

PACKAGE_ROOT = Path(__file__).parent.parent
STATIC_DIR = PACKAGE_ROOT / "web" / "static"


def make_app(settings: cfg.Settings) -> FastAPI:
    state = DaemonState.boot(settings)
    set_state(state)
    orch = Orchestrator(state)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        state.orchestrator = orch  # type: ignore[attr-defined]
        logger.info(
            "driveforge-daemon ready on %s:%d (dev_mode=%s)",
            settings.daemon.host,
            settings.daemon.port,
            settings.dev_mode,
        )
        yield
        logger.info("driveforge-daemon shutting down")

    app = FastAPI(
        title="DriveForge",
        description="In-house enterprise drive refurbishment pipeline",
        version="0.1.0",
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
