"""Tests for v0.7.0's abort-button reliability + drive-detail button.

Pre-v0.7.0, `Orchestrator.abort_drive` returned bare `bool` and logged
NOTHING on the "serial not in _tasks" branch. Combined with the HTTP
route returning 303 to `/` with no flash, the operator got zero signal
after clicking abort — the silent-failure class that motivated this
release.

v0.7.0 changes:
  1. `abort_drive` returns a structured outcome dict:
     {status: "aborted"|"not_active"|"already_done", killed, phase, note}
  2. All branches log explicitly (INFO on non-aborts, WARNING on abort)
  3. `active_sublabel[serial]` is set to "aborting…" on success so the
     dashboard acknowledges the click immediately
  4. Web route redirects with `?aborted=<status>&abort_note=<text>` so
     base.html can render a flash banner
  5. Web route honors Referer so drive-detail clicks bounce back there
  6. Drive-detail page has its own Abort button (absent pre-v0.7.0)
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi.testclient import TestClient

from driveforge import config as cfg


def _bootstrap_app(tmp_path):
    from driveforge.daemon.app import make_app
    from driveforge.daemon.state import DaemonState

    settings = cfg.Settings()
    settings.daemon.state_dir = tmp_path
    settings.daemon.db_path = tmp_path / "driveforge.db"
    settings.daemon.pending_labels_dir = tmp_path / "pending-labels"
    settings.daemon.reports_dir = tmp_path / "reports"
    settings.setup_completed = True

    app = make_app(settings)
    DaemonState.boot(settings)
    return app


# -------------------------------------------------- orchestrator outcome dict


@pytest.mark.asyncio
async def test_abort_drive_returns_not_active_for_unknown_serial(tmp_path, caplog) -> None:
    """Most common failure mode pre-v0.7.0: operator clicks abort on a
    drive that's already completed (task was popped from _tasks) and
    nothing is logged. v0.7.0 logs at INFO + returns structured status."""
    from driveforge.daemon.state import DaemonState
    from driveforge.daemon.orchestrator import Orchestrator

    settings = cfg.Settings()
    settings.daemon.state_dir = tmp_path
    settings.daemon.db_path = tmp_path / "driveforge.db"
    settings.daemon.pending_labels_dir = tmp_path / "pending-labels"
    settings.daemon.reports_dir = tmp_path / "reports"
    state = DaemonState.boot(settings)
    orch = Orchestrator(state)

    caplog.set_level(logging.INFO, logger="driveforge.daemon.orchestrator")
    outcome = await orch.abort_drive("GHOST-SERIAL")

    assert outcome["status"] == "not_active"
    assert outcome["killed"] == 0
    # Explicit log line means operators debugging "why did my click
    # do nothing" can now see journal evidence on both paths.
    assert any(
        "not in _tasks" in rec.message for rec in caplog.records
    ), "abort_drive must log when serial isn't active (no more silent False)"


@pytest.mark.asyncio
async def test_abort_drive_returns_aborted_with_sublabel_update(tmp_path) -> None:
    """Live task → status="aborted", active_sublabel set to
    "aborting — waiting for pipeline to tear down". The sublabel
    update is the visible acknowledgment on the dashboard; without
    it, the card looks unchanged after abort click."""
    from driveforge.daemon.state import DaemonState
    from driveforge.daemon.orchestrator import Orchestrator

    settings = cfg.Settings()
    settings.daemon.state_dir = tmp_path
    settings.daemon.db_path = tmp_path / "driveforge.db"
    settings.daemon.pending_labels_dir = tmp_path / "pending-labels"
    settings.daemon.reports_dir = tmp_path / "reports"
    state = DaemonState.boot(settings)
    orch = Orchestrator(state)

    # Seed a fake live task. asyncio.sleep(3600) will get cancelled.
    async def fake_pipeline():
        await asyncio.sleep(3600)

    task = asyncio.create_task(fake_pipeline())
    orch._tasks["LIVE-SERIAL"] = task
    state.active_phase["LIVE-SERIAL"] = "badblocks"

    try:
        outcome = await orch.abort_drive("LIVE-SERIAL")
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    assert outcome["status"] == "aborted"
    assert outcome["phase"] == "badblocks"
    # Sublabel reflects the transition.
    assert "aborting" in state.active_sublabel.get("LIVE-SERIAL", "").lower()


@pytest.mark.asyncio
async def test_abort_drive_returns_already_done_for_done_task(tmp_path) -> None:
    """Edge: task completed but didn't get popped from _tasks (race
    between completion cleanup + operator click). v0.7.0 clears the
    stale entry + returns structured status instead of the silent
    False."""
    from driveforge.daemon.state import DaemonState
    from driveforge.daemon.orchestrator import Orchestrator

    settings = cfg.Settings()
    settings.daemon.state_dir = tmp_path
    settings.daemon.db_path = tmp_path / "driveforge.db"
    settings.daemon.pending_labels_dir = tmp_path / "pending-labels"
    settings.daemon.reports_dir = tmp_path / "reports"
    state = DaemonState.boot(settings)
    orch = Orchestrator(state)

    async def already_done():
        return None

    task = asyncio.create_task(already_done())
    await task  # let it complete
    orch._tasks["STALE-SERIAL"] = task

    outcome = await orch.abort_drive("STALE-SERIAL")
    assert outcome["status"] == "already_done"
    # Stale entry cleared as a side effect.
    assert "STALE-SERIAL" not in orch._tasks


# -------------------------------------------------------------- web routes


def test_web_abort_redirects_with_flash_params(tmp_path, monkeypatch) -> None:
    """POST /drives/{serial}/abort must redirect with ?aborted=<status>
    + ?abort_note=<text> so the base.html flash banner has something
    to render."""
    app = _bootstrap_app(tmp_path)

    # Stub the orchestrator call so we don't need a real task.
    async def fake_abort(serial):
        return {
            "status": "aborted",
            "killed": 2,
            "phase": "badblocks",
            "note": f"Abort signalled for {serial} in badblocks.",
        }

    app.state.orchestrator.abort_drive = fake_abort  # type: ignore[attr-defined]

    with TestClient(app, follow_redirects=False) as client:
        resp = client.post("/drives/SN-123/abort")

    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert "aborted=aborted" in loc
    assert "abort_note=" in loc
    assert "Abort+signalled" in loc or "Abort%20signalled" in loc


def test_web_abort_honors_referer_to_drive_detail(tmp_path) -> None:
    """When the click comes from a drive-detail page, the redirect
    should bounce back there so the flash banner lands in context.
    Pre-v0.7.0 everything always went to `/`."""
    app = _bootstrap_app(tmp_path)

    async def fake_abort(serial):
        return {"status": "aborted", "killed": 0, "phase": "pre_smart", "note": "ok"}

    app.state.orchestrator.abort_drive = fake_abort  # type: ignore[attr-defined]

    with TestClient(app, follow_redirects=False) as client:
        resp = client.post(
            "/drives/SN-7/abort",
            headers={"Referer": "http://localhost:8080/drives/SN-7"},
        )

    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert loc.startswith("/drives/SN-7?")


def test_web_abort_defaults_to_dashboard_when_no_referer(tmp_path) -> None:
    app = _bootstrap_app(tmp_path)

    async def fake_abort(serial):
        return {"status": "not_active", "killed": 0, "phase": None, "note": "not running"}

    app.state.orchestrator.abort_drive = fake_abort  # type: ignore[attr-defined]

    with TestClient(app, follow_redirects=False) as client:
        resp = client.post("/drives/GHOST/abort")

    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/?")


# ---------------------------------------------------------- drive-detail UI


def test_drive_detail_renders_abort_button_when_drive_active(tmp_path) -> None:
    """Drive-detail page gains its own Abort button when the drive
    is in state.active_phase. Matches the dashboard bay-card button's
    form action so both submit to the same route."""
    from driveforge.daemon.state import get_state
    from driveforge.db import models as m

    app = _bootstrap_app(tmp_path)
    state = get_state()

    # Seed a drive in the DB.
    with state.session_factory() as session:
        session.add(
            m.Drive(
                serial="ACTIVE-SERIAL",
                model="Test Drive",
                capacity_bytes=1_000_000_000,
                transport="sata",
            )
        )
        session.commit()

    # Mark the drive as active.
    state.active_phase["ACTIVE-SERIAL"] = "badblocks"
    state.active_sublabel["ACTIVE-SERIAL"] = "pass 2/8 · write 0xAA"

    with TestClient(app) as client:
        resp = client.get("/drives/ACTIVE-SERIAL")

    assert resp.status_code == 200
    body = resp.text
    # Pipeline-running panel + abort form present.
    assert "Pipeline running" in body
    assert 'action="/drives/ACTIVE-SERIAL/abort"' in body
    assert "Abort pipeline" in body
    # Sublabel surfaces the pass-label hint.
    assert "pass 2/8" in body


def test_drive_detail_hides_abort_during_secure_erase(tmp_path) -> None:
    """Secure-erase phase must show the same warning text as the
    dashboard — clicking abort during sg_format can corrupt the
    drive. Button must NOT render; informative paragraph renders
    in its place."""
    from driveforge.daemon.state import get_state
    from driveforge.db import models as m

    app = _bootstrap_app(tmp_path)
    state = get_state()
    with state.session_factory() as session:
        session.add(
            m.Drive(
                serial="SE-SERIAL",
                model="Test",
                capacity_bytes=1_000_000_000,
                transport="sata",
            )
        )
        session.commit()
    state.active_phase["SE-SERIAL"] = "secure_erase"

    with TestClient(app) as client:
        resp = client.get("/drives/SE-SERIAL")

    body = resp.text
    assert "Pipeline running" in body
    # Explicit no-abort message in place of the button.
    assert "disabled during secure erase" in body.lower()
    # No form submit button to the abort route.
    assert "Abort pipeline" not in body


def test_drive_detail_omits_pipeline_panel_when_drive_idle(tmp_path) -> None:
    """No active_phase → no Active Pipeline panel. The static
    'Latest test run' panel is the only thing rendered; no abort
    button appears out of nowhere."""
    from driveforge.daemon.state import get_state
    from driveforge.db import models as m

    app = _bootstrap_app(tmp_path)
    state = get_state()
    with state.session_factory() as session:
        session.add(
            m.Drive(
                serial="IDLE-SERIAL",
                model="Test",
                capacity_bytes=1_000_000_000,
                transport="sata",
            )
        )
        session.commit()
    # No active_phase entry = idle.

    with TestClient(app) as client:
        resp = client.get("/drives/IDLE-SERIAL")

    body = resp.text
    assert "Pipeline running" not in body
    assert "Abort pipeline" not in body


# ----------------------------------------------------- flash banner rendering


def test_base_html_renders_aborted_flash_on_any_page(tmp_path) -> None:
    """Flash banner lives in base.html chrome block so it renders on
    every page that extends base.html. Hitting any route with
    ?aborted=aborted must produce the green success banner."""
    app = _bootstrap_app(tmp_path)

    with TestClient(app) as client:
        resp = client.get("/?aborted=aborted&abort_note=Abort+signalled+for+SN-1.")

    assert resp.status_code == 200
    body = resp.text
    assert "abort-flash-banner--aborted" in body
    assert "Abort signalled" in body


def test_base_html_renders_not_active_flash(tmp_path) -> None:
    """The 'not_active' variant has a different tone (informational
    not success). Banner class differs so CSS can colorize it."""
    app = _bootstrap_app(tmp_path)

    with TestClient(app) as client:
        resp = client.get(
            "/?aborted=not_active&abort_note=GHOST+isn%27t+currently+running+a+pipeline."
        )

    body = resp.text
    assert "abort-flash-banner--not_active" in body
    assert "isn" in body  # the note text made it through URL-decoding


def test_base_html_hides_flash_banner_on_plain_pageload(tmp_path) -> None:
    """No ?aborted=... query param → no banner rendered."""
    app = _bootstrap_app(tmp_path)

    with TestClient(app) as client:
        resp = client.get("/")

    assert "abort-flash-banner" not in resp.text
