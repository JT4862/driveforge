"""Server-rendered web UI routes (HTMX + Jinja)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from driveforge.daemon.state import get_state
from driveforge.db import models as m

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=TEMPLATES_DIR)
router = APIRouter()


def _bay_view(state, session) -> list[dict]:
    """Compose the 8-bay dashboard view from current orchestrator + DB state."""
    bays: list[dict] = []
    for bay_num in range(1, 9):
        serial = state.bay_assignments.get(bay_num)
        if serial is None:
            bays.append({"bay": bay_num, "empty": True})
            continue
        drive = session.get(m.Drive, serial)
        if drive is None:
            bays.append({"bay": bay_num, "empty": True})
            continue
        bays.append(
            {
                "bay": bay_num,
                "empty": False,
                "serial": serial,
                "model": drive.model,
                "capacity_tb": round(drive.capacity_bytes / 1_000_000_000_000, 2),
                "phase": state.active_phase.get(serial, "queued"),
                "percent": state.active_percent.get(serial, 0.0),
            }
        )
    return bays


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    state = get_state()
    with state.session_factory() as session:
        bays = _bay_view(state, session)
        batch_count = session.query(m.Batch).count()
        drive_count = session.query(m.Drive).count()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"bays": bays, "batch_count": batch_count, "drive_count": drive_count},
    )


@router.get("/_partials/bays", response_class=HTMLResponse)
def bays_partial(request: Request) -> HTMLResponse:
    """HTMX polling endpoint for live dashboard refresh."""
    state = get_state()
    with state.session_factory() as session:
        bays = _bay_view(state, session)
    return templates.TemplateResponse(request, "_bays.html", {"bays": bays})


@router.get("/drives/{serial}", response_class=HTMLResponse)
def drive_detail(request: Request, serial: str) -> HTMLResponse:
    state = get_state()
    with state.session_factory() as session:
        drive = session.get(m.Drive, serial)
        if drive is None:
            raise HTTPException(status_code=404, detail="drive not found")
        runs = (
            session.query(m.TestRun)
            .filter_by(drive_serial=serial)
            .order_by(m.TestRun.started_at.desc())
            .all()
        )
        latest = runs[0] if runs else None
        snapshots = []
        telemetry_pts = []
        if latest:
            snapshots = (
                session.query(m.SmartSnapshot)
                .filter_by(test_run_id=latest.id)
                .order_by(m.SmartSnapshot.captured_at.asc())
                .all()
            )
            telemetry_pts = (
                session.query(m.TelemetrySample)
                .filter_by(test_run_id=latest.id)
                .order_by(m.TelemetrySample.ts.asc())
                .all()
            )
    return templates.TemplateResponse(
        request,
        "drive_detail.html",
        {
            "drive": drive,
            "runs": runs,
            "latest": latest,
            "snapshots": snapshots,
            "telemetry": telemetry_pts,
            "capacity_tb": round(drive.capacity_bytes / 1_000_000_000_000, 2),
        },
    )


@router.get("/batches", response_class=HTMLResponse)
def batches(request: Request) -> HTMLResponse:
    state = get_state()
    with state.session_factory() as session:
        rows = session.query(m.Batch).order_by(m.Batch.started_at.desc()).all()
        batches_view = []
        for b in rows:
            totals = {"A": 0, "B": 0, "C": 0, "fail": 0}
            for run in b.test_runs:
                if run.grade in totals:
                    totals[run.grade] += 1
            batches_view.append({"batch": b, "totals": totals, "count": len(b.test_runs)})
    return templates.TemplateResponse(request, "batches.html", {"batches": batches_view})


@router.get("/batches/{batch_id}", response_class=HTMLResponse)
def batch_detail(request: Request, batch_id: str) -> HTMLResponse:
    state = get_state()
    with state.session_factory() as session:
        batch = session.get(m.Batch, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="batch not found")
        runs = session.query(m.TestRun).filter_by(batch_id=batch_id).all()
        totals = {"A": 0, "B": 0, "C": 0, "fail": 0}
        for r in runs:
            if r.grade in totals:
                totals[r.grade] += 1
    return templates.TemplateResponse(
        request,
        "batch_detail.html",
        {"batch": batch, "runs": runs, "totals": totals},
    )


@router.get("/history", response_class=HTMLResponse)
def history(request: Request) -> HTMLResponse:
    state = get_state()
    with state.session_factory() as session:
        runs = (
            session.query(m.TestRun)
            .filter(m.TestRun.completed_at.isnot(None))
            .order_by(m.TestRun.completed_at.desc())
            .limit(500)
            .all()
        )
    return templates.TemplateResponse(request, "history.html", {"runs": runs})


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    state = get_state()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {"settings": state.settings},
    )


@router.get("/reports/{serial}", response_class=HTMLResponse)
def public_report(request: Request, serial: str) -> HTMLResponse:
    """Public-facing, read-only drive cert page — target of the QR code."""
    state = get_state()
    with state.session_factory() as session:
        drive = session.get(m.Drive, serial)
        if drive is None:
            raise HTTPException(status_code=404, detail="drive not found")
        latest = (
            session.query(m.TestRun)
            .filter_by(drive_serial=serial)
            .filter(m.TestRun.completed_at.isnot(None))
            .order_by(m.TestRun.completed_at.desc())
            .first()
        )
        if latest is None:
            raise HTTPException(status_code=404, detail="no completed test run for this drive")
        telemetry_pts = (
            session.query(m.TelemetrySample)
            .filter_by(test_run_id=latest.id)
            .order_by(m.TelemetrySample.ts.asc())
            .all()
        )
    return templates.TemplateResponse(
        request,
        "report.html",
        {
            "drive": drive,
            "run": latest,
            "telemetry": telemetry_pts,
            "capacity_tb": round(drive.capacity_bytes / 1_000_000_000_000, 2),
            "generated_at": datetime.utcnow(),
        },
    )
