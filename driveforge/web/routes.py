"""Server-rendered web UI routes (HTMX + Jinja)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import io

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import joinedload

from driveforge.core import drive as drive_mod
from driveforge.daemon.state import get_state
from driveforge.db import models as m

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=TEMPLATES_DIR)
router = APIRouter()


_PHASE_CLASS = {
    "queued": "info",
    "pre_smart": "info",
    "short_test": "info",
    "firmware_check": "info",
    "post_smart": "info",
    "grading": "info",
    "secure_erase": "erase",
    "badblocks": "burn",
    "long_test": "long",
    "done": "done",
    "failed": "fail",
    "aborted": "fail",
}


def _format_duration(seconds: float | int) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    h = s // 3600
    m = (s % 3600) // 60
    return f"{h}h {m}m"


# Rough per-GB seconds coefficients per phase + media type. Calibrated
# against real-hardware measurements on an R720 with -b 1MiB -c 32 badblocks
# against an Intel SSDSC2BB120G4 (SATA SSD on LSI 9207-8i SAS HBA):
#   - badblocks: 8 passes × ~14 min = 112 min on 120 GB → ~56 sec/GB
#   - secure erase (hdparm SECURITY ERASE UNIT): ~73 sec / 120 GB → 0.6 sec/GB
# Numbers are pessimistic by design — an ETA that overshoots is more helpful
# than one that lies about a drive being nearly done.
_ETA_BADBLOCKS_SEC_PER_GB = {"hdd": 30.0, "ssd": 56.0, "nvme": 4.0}
_ETA_ERASE_SEC_PER_GB = {"hdd": 6.0, "ssd": 0.7, "nvme": 0.1}
_ETA_LONG_TEST_SEC_PER_GB = {"hdd": 12.0, "ssd": 1.0, "nvme": 0.5}


def _media_kind(drive_row) -> str:
    if drive_row.transport == "nvme":
        return "nvme"
    # Prefer the DB's rotational flag (populated from lsblk ROTA at enrollment).
    # Legacy rows without the flag fall back to transport-based heuristic: SAS
    # is usually spinning, SATA could be either — assume HDD to be conservative.
    rotational = getattr(drive_row, "rotational", None)
    if rotational is True:
        return "hdd"
    if rotational is False:
        return "ssd"
    return "hdd"


def _eta_seconds(phase: str, drive_row) -> int | None:
    gb = drive_row.capacity_bytes / 1_000_000_000
    media = _media_kind(drive_row)
    if phase == "badblocks":
        return int(gb * _ETA_BADBLOCKS_SEC_PER_GB[media])
    if phase == "secure_erase":
        return int(max(60, gb * _ETA_ERASE_SEC_PER_GB[media]))
    if phase == "long_test":
        return int(gb * _ETA_LONG_TEST_SEC_PER_GB[media])
    return None


def _active_card(state, session, serial: str) -> dict | None:
    """Render an Active-section card for a drive currently in the pipeline."""
    drive = session.get(m.Drive, serial)
    if drive is None:
        return None
    phase = state.active_phase.get(serial, "queued")
    run = (
        session.query(m.TestRun)
        .filter_by(drive_serial=serial, completed_at=None)
        .order_by(m.TestRun.started_at.desc())
        .first()
    )
    elapsed_sec = 0
    if run and run.started_at:
        delta = datetime.now(UTC) - (
            run.started_at if run.started_at.tzinfo else run.started_at.replace(tzinfo=UTC)
        )
        elapsed_sec = int(delta.total_seconds())
    eta = _eta_seconds(phase, drive)
    return {
        "state": "active",
        "key": serial,
        "serial": serial,
        "model": drive.model,
        "manufacturer": drive.manufacturer,
        "capacity_tb": round(drive.capacity_bytes / 1_000_000_000_000, 2),
        "phase": phase,
        "phase_class": _PHASE_CLASS.get(phase, "info"),
        "percent": state.active_percent.get(serial, 0.0),
        "sublabel": state.active_sublabel.get(serial),
        "io_rate": state.active_io_rate.get(serial),
        "elapsed_label": _format_duration(elapsed_sec),
        "eta_label": f"~{_format_duration(eta)}" if eta else None,
    }


def _installed_card(state, session, drive: "drive_mod.Drive") -> dict:
    """Render an Installed-section card for a drive that is present but
    not currently in the test pipeline. Shows last-grade info pulled from
    the most recent completed TestRun, when there is one.
    """
    last_run = (
        session.query(m.TestRun)
        .filter_by(drive_serial=drive.serial)
        .filter(m.TestRun.completed_at.isnot(None))
        .order_by(m.TestRun.completed_at.desc())
        .first()
    )
    last_grade = last_run.grade if last_run else None
    last_tested = last_run.completed_at if last_run else None
    last_phase = last_run.phase if last_run else None
    last_quick = bool(last_run.quick_mode) if last_run else False
    last_error = None
    if last_run and last_run.error_message:
        msg = last_run.error_message.strip().split("\n", 1)[0]
        last_error = msg[:80] + ("…" if len(msg) > 80 else "")
    # Prefer live discover-time manufacturer detection (catches newly-added
    # OEM rules); fall back to the DB row written at last enrollment.
    db_drive = session.get(m.Drive, drive.serial)
    mfr = drive.manufacturer or (db_drive.manufacturer if db_drive else None)
    # Prefer the DB's transport over the lsblk-based live value: lsblk
    # reports `tran=sas` for SATA drives on a SAS HBA, but the orchestrator
    # refines this at enrollment via smartctl and writes the true wire
    # protocol to the Drive row. Drives never enrolled fall through to
    # the live lsblk value.
    transport = (db_drive.transport if db_drive and db_drive.transport else None) or drive.transport.value
    return {
        "state": "installed",
        "key": drive.serial,
        "serial": drive.serial,
        "model": drive.model,
        "manufacturer": mfr,
        "capacity_tb": drive.capacity_tb,
        "transport": transport,
        "last_grade": last_grade,
        "last_tested": last_tested,
        "last_phase": last_phase,
        "last_quick": last_quick,
        "last_error": last_error,
    }


def _drive_view(state, session) -> dict:
    """Compose the drive-centric dashboard view.

    Returns two flat lists:
      - active: one card per drive currently in the test pipeline (serial
        is in state.active_phase). Ordered by insertion into the pipeline.
      - installed: one card per drive currently present on the host that
        is NOT active. Ordered by serial.

    No enclosures, no slot groupings, no empty placeholders. Drives that
    are pulled disappear from the view automatically on the next refresh.
    """
    # One-shot lsblk to get the currently-present drives.
    discovered = {d.serial: d for d in drive_mod.discover()}

    # Active section: preserve orchestrator insertion order (dict iteration).
    active_cards: list[dict] = []
    for serial in state.active_phase.keys():
        card = _active_card(state, session, serial)
        if card is not None:
            active_cards.append(card)
    active_serials = {c["serial"] for c in active_cards}

    # Installed section: every currently-present drive that isn't active.
    installed_cards: list[dict] = []
    for serial in sorted(discovered.keys()):
        if serial in active_serials:
            continue
        installed_cards.append(_installed_card(state, session, discovered[serial]))

    return {
        "active": active_cards,
        "installed": installed_cards,
        "total_present": len(discovered),
    }


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    state = get_state()
    with state.session_factory() as session:
        view = _drive_view(state, session)
        batch_count = session.query(m.Batch).count()
        drive_count = session.query(m.Drive).count()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"view": view, "batch_count": batch_count, "drive_count": drive_count},
    )


@router.get("/_partials/bays", response_class=HTMLResponse)
def bays_partial(request: Request) -> HTMLResponse:
    """HTMX polling endpoint for live dashboard refresh."""
    state = get_state()
    with state.session_factory() as session:
        view = _drive_view(state, session)
    return templates.TemplateResponse(request, "_bays.html", {"view": view})


SUGGESTED_USE = {
    "A": "Primary Ceph OSD, TrueNAS main pool — no reservations",
    "B": "Secondary OSD, scratch pool, backup target",
    "C": "Cold storage, test environment, heavy-redundancy array",
    "fail": "Scrap / e-waste — do not deploy",
}


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
        max_temp = None
        avg_temp = None
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
            temps = [t.drive_temp_c for t in telemetry_pts if t.drive_temp_c is not None]
            if temps:
                max_temp = max(temps)
                avg_temp = round(sum(temps) / len(temps), 1)
    duration_sec = None
    if latest and latest.started_at and latest.completed_at:
        started = latest.started_at if latest.started_at.tzinfo else latest.started_at.replace(tzinfo=UTC)
        completed = latest.completed_at if latest.completed_at.tzinfo else latest.completed_at.replace(tzinfo=UTC)
        duration_sec = int((completed - started).total_seconds())
    live_log = "\n".join(state.active_log.get(serial, []))
    log_tail = live_log or (latest.log_tail if latest else "") or ""
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
            "log_tail": log_tail,
            "log_is_live": bool(live_log),
            "duration_label": _format_duration(duration_sec) if duration_sec is not None else None,
            "max_temp": max_temp,
            "avg_temp": avg_temp,
            "suggested_use": SUGGESTED_USE.get(latest.grade) if latest and latest.grade else None,
            "label_roll": state.settings.printer.label_roll or "DK-1209",
        },
    )


@router.get("/batches", response_class=HTMLResponse)
def batches(request: Request) -> HTMLResponse:
    state = get_state()
    with state.session_factory() as session:
        rows = (
            session.query(m.Batch)
            .options(joinedload(m.Batch.test_runs))
            .order_by(m.Batch.started_at.desc())
            .all()
        )
        batches_view = []
        for b in rows:
            totals = {"A": 0, "B": 0, "C": 0, "fail": 0}
            for run in b.test_runs:
                if run.grade in totals:
                    totals[run.grade] += 1
            batches_view.append(
                {
                    "id": b.id,
                    "source": b.source,
                    "started_at": b.started_at,
                    "completed_at": b.completed_at,
                    "totals": totals,
                    "count": len(b.test_runs),
                }
            )
    return templates.TemplateResponse(request, "batches.html", {"batches": batches_view})


@router.get("/batches/new", response_class=HTMLResponse)
def new_batch_form(request: Request) -> HTMLResponse:
    drives = drive_mod.discover()
    err = request.query_params.get("err")
    orch = request.app.state.orchestrator
    busy = orch.active_serials()
    drives_view = [
        {
            "serial": d.serial,
            "model": d.model,
            "capacity_tb": d.capacity_tb,
            "transport": d.transport,
            "active": d.serial in busy,
        }
        for d in drives
    ]
    return templates.TemplateResponse(
        request, "new_batch.html", {"drives": drives_view, "err": err}
    )


@router.post("/batches/new")
async def new_batch_submit(request: Request) -> RedirectResponse:
    # Imported locally so this module doesn't pull in the orchestrator on
    # collection-time — keeps test import graph shallow.
    from driveforge.daemon.orchestrator import BatchRejected

    form = await request.form()
    source = form.get("source") or None
    selected = form.getlist("drive")
    quick = form.get("quick") == "on"
    confirm = (form.get("confirm") or "").strip().upper()
    if confirm != "ERASE":
        return RedirectResponse(url="/batches/new?err=confirm", status_code=303)
    drives = [d for d in drive_mod.discover() if d.serial in selected]
    if not drives:
        drives = drive_mod.discover()
    orch = request.app.state.orchestrator
    try:
        await orch.start_batch(drives, source=source, quick=quick)
    except BatchRejected:
        return RedirectResponse(url="/batches/new?err=active", status_code=303)
    return RedirectResponse(url="/", status_code=303)


@router.post("/abort-all")
async def abort_all_web(request: Request) -> RedirectResponse:
    orch = request.app.state.orchestrator
    await orch.abort_all()
    return RedirectResponse(url="/", status_code=303)

# --- Manual cert label printing ---------------------------------------------
# Labels are printed on-demand, not automatically, so the operator can inspect
# each cert before committing a sticker. See BUILD.md → Certification Labels.

_BROTHER_QL_BACKENDS = {
    # PrinterConfig.connection → brother_ql backend id
    "usb": "pyusb",
    "network": "network",
    "bluetooth": "linux_kernel",
    "file": "file",  # dev mode: save PNG + raster to /tmp, no hardware needed
}


def _public_report_url(request: Request, state, serial: str) -> str:
    """Build the URL that goes into the QR code on the printed label.

    Prefer the Cloudflare Tunnel hostname from Settings so the QR resolves
    from outside the homelab LAN; fall back to the request's own host.
    """
    tun = state.settings.integrations.cloudflare_tunnel_hostname
    if tun:
        if not tun.startswith(("http://", "https://")):
            tun = f"https://{tun}"
        return f"{tun.rstrip('/')}/reports/{serial}"
    return f"{request.url.scheme}://{request.url.netloc}/reports/{serial}"


def _cert_label_data_for(request: Request, state, drive, run):
    """Build CertLabelData for a given drive + run. Shared by preview + print."""
    # Lazy import — the printer module pulls in qrcode + pillow which aren't
    # always available in the macOS dev environment.
    from driveforge.core import printer as printer_mod

    return printer_mod.CertLabelData(
        model=drive.model,
        serial=drive.serial,
        capacity_tb=round(drive.capacity_bytes / 1_000_000_000_000, 2),
        grade=run.grade or "—",
        tested_date=(run.completed_at or run.started_at or datetime.now(UTC)).date(),
        power_on_hours=run.power_on_hours_at_test or 0,
        report_url=_public_report_url(request, state, drive.serial),
        quick_mode=bool(run.quick_mode),
    )


def _print_label_for_run(request: Request, state, drive, run) -> tuple[bool, str]:
    """Render + dispatch a single cert label. Returns (ok, message)."""
    pc = state.settings.printer
    if not pc.model:
        return False, "no printer configured (Settings → Printer)"
    from driveforge.core import printer as printer_mod

    backend = _BROTHER_QL_BACKENDS.get(pc.connection, "file")
    data = _cert_label_data_for(request, state, drive, run)
    try:
        img = printer_mod.render_label(data, roll=pc.label_roll or "DK-1209")
    except Exception as exc:  # noqa: BLE001
        logger.exception("label render failed for %s", drive.serial)
        return False, f"render failed: {exc}"
    ok = printer_mod.print_label(
        img, model=pc.model, backend=backend, identifier=pc.backend_identifier
    )
    if not ok:
        return False, "printer dispatch failed (check Settings → Printer and device connection)"
    return True, f"printed label for {drive.serial}"


def _latest_printable_run(session, serial: str):
    """Return the drive's most recent completed run that produced a grade."""
    return (
        session.query(m.TestRun)
        .filter_by(drive_serial=serial)
        .filter(m.TestRun.completed_at.isnot(None))
        .filter(m.TestRun.grade.isnot(None))
        .order_by(m.TestRun.completed_at.desc())
        .first()
    )


@router.get("/drives/{serial}/label-preview.png")
def drive_label_preview(request: Request, serial: str) -> Response:
    """Render the cert label as PNG without dispatching to a printer.

    Used by the in-browser preview modal on drive detail / batch detail.
    Works even when no printer is configured — pulls the label_roll from
    settings (default DK-1209) just for sizing.
    """
    state = get_state()
    with state.session_factory() as session:
        drive = session.get(m.Drive, serial)
        if drive is None:
            raise HTTPException(status_code=404, detail="drive not found")
        run = _latest_printable_run(session, serial)
        if run is None:
            raise HTTPException(status_code=404, detail="no completed run to preview")
        from driveforge.core import printer as printer_mod

        data = _cert_label_data_for(request, state, drive, run)
        try:
            img = printer_mod.render_label(
                data, roll=state.settings.printer.label_roll or "DK-1209"
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("label preview render failed for %s", serial)
            raise HTTPException(status_code=500, detail=f"render failed: {exc}") from exc
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/drives/{serial}/print-label")
def print_drive_label(request: Request, serial: str) -> RedirectResponse:
    state = get_state()
    with state.session_factory() as session:
        drive = session.get(m.Drive, serial)
        if drive is None:
            raise HTTPException(status_code=404, detail="drive not found")
        run = _latest_printable_run(session, serial)
        if run is None:
            return RedirectResponse(
                url=f"/drives/{serial}?flash=err&msg=no+completed+run+to+print",
                status_code=303,
            )
        ok, msg = _print_label_for_run(request, state, drive, run)
    status = "ok" if ok else "err"
    from urllib.parse import quote

    return RedirectResponse(
        url=f"/drives/{serial}?flash={status}&msg={quote(msg)}",
        status_code=303,
    )


@router.post("/batches/{batch_id}/print-labels")
def print_batch_labels(request: Request, batch_id: str) -> RedirectResponse:
    state = get_state()
    with state.session_factory() as session:
        batch = session.get(m.Batch, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="batch not found")
        # Only print labels for runs that actually graded (skip fails + incomplete)
        runs = (
            session.query(m.TestRun)
            .filter_by(batch_id=batch_id)
            .filter(m.TestRun.completed_at.isnot(None))
            .filter(m.TestRun.grade.isnot(None))
            .filter(m.TestRun.grade != "fail")
            .all()
        )
        printed = 0
        failures: list[str] = []
        for run in runs:
            drive = session.get(m.Drive, run.drive_serial)
            if drive is None:
                continue
            ok, msg = _print_label_for_run(request, state, drive, run)
            if ok:
                printed += 1
            else:
                failures.append(f"{run.drive_serial}: {msg}")
                # If the very first label fails because of config, abort early
                # rather than spam identical errors for every drive.
                if "no printer configured" in msg or "printer dispatch failed" in msg:
                    break
    from urllib.parse import quote

    if failures and printed == 0:
        return RedirectResponse(
            url=f"/batches/{batch_id}?flash=err&msg={quote(failures[0])}",
            status_code=303,
        )
    summary = f"printed {printed}/{len(runs)} labels"
    if failures:
        summary += f" · {len(failures)} failed"
    return RedirectResponse(
        url=f"/batches/{batch_id}?flash=ok&msg={quote(summary)}",
        status_code=303,
    )


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
        batch_view = {
            "id": batch.id,
            "source": batch.source,
            "started_at": batch.started_at,
            "completed_at": batch.completed_at,
        }
        runs_view = [
            {
                "drive_serial": r.drive_serial,
                "bay": r.bay,
                "phase": r.phase,
                "grade": r.grade,
                "report_url": r.report_url,
                "error_message": r.error_message,
                "quick_mode": bool(r.quick_mode),
                "printable": bool(
                    r.completed_at and r.grade and r.grade != "fail"
                ),
            }
            for r in runs
        ]
        printable_count = sum(1 for r in runs_view if r["printable"])
    return templates.TemplateResponse(
        request,
        "batch_detail.html",
        {
            "batch": batch_view,
            "runs": runs_view,
            "totals": totals,
            "printable_count": printable_count,
        },
    )


@router.get("/history", response_class=HTMLResponse)
def history(request: Request) -> HTMLResponse:
    state = get_state()
    with state.session_factory() as session:
        runs = (
            session.query(m.TestRun)
            .options(joinedload(m.TestRun.drive))
            .filter(m.TestRun.completed_at.isnot(None))
            .order_by(m.TestRun.completed_at.desc())
            .limit(500)
            .all()
        )
        rows = []
        for r in runs:
            duration = None
            if r.started_at and r.completed_at:
                started = r.started_at if r.started_at.tzinfo else r.started_at.replace(tzinfo=UTC)
                completed = r.completed_at if r.completed_at.tzinfo else r.completed_at.replace(tzinfo=UTC)
                duration = _format_duration(int((completed - started).total_seconds()))
            capacity_tb = round(r.drive.capacity_bytes / 1_000_000_000_000, 2) if r.drive else None
            rows.append(
                {
                    "completed_at": r.completed_at,
                    "drive_serial": r.drive_serial,
                    "model": r.drive.model if r.drive else "—",
                    "capacity_tb": capacity_tb,
                    "grade": r.grade,
                    "power_on_hours": r.power_on_hours_at_test,
                    "batch_id": r.batch_id,
                    "quick_mode": bool(r.quick_mode),
                    "duration": duration,
                    "has_report": bool(r.report_url),
                }
            )
    return templates.TemplateResponse(request, "history.html", {"rows": rows})


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    state = get_state()
    saved = request.query_params.get("saved")
    restart = request.query_params.get("restart")
    from driveforge.core import telemetry
    from driveforge.core import updates as updates_mod

    # Live-sample chassis telemetry for the Hardware panel. Skipped silently
    # when the underlying capability is False (no BMC / no /dev/ipmi0 perms).
    chassis_power = telemetry.read_chassis_power() if state.capabilities.chassis_power else None
    chassis_temps = telemetry.read_chassis_temperatures() if state.capabilities.chassis_temperature else {}

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": state.settings,
            "saved_panel": saved,
            "restart_required": restart == "1",
            "update_info": updates_mod.cached(),
            "update_command": updates_mod.update_command(),
            "current_version": updates_mod.CURRENT_VERSION,
            "capabilities": state.capabilities,
            "chassis_power": chassis_power,
            "chassis_temps": chassis_temps,
        },
    )


@router.post("/settings/check-updates")
def check_updates(request: Request) -> RedirectResponse:
    """Manual-trigger update check. Hits the GitHub Releases API once,
    caches the result for an hour. Never installs anything — surfaces a
    copy-paste command for the operator to run via SSH."""
    from driveforge.core import updates as updates_mod

    info = updates_mod.check_for_updates(force=True)
    return RedirectResponse(
        url=f"/settings?saved=updates&update_status={info.status}",
        status_code=303,
    )


async def _save_settings_or_ignore(request: Request) -> None:
    from driveforge import config as cfg

    try:
        cfg.save(request.app.state.orchestrator.state.settings)
    except PermissionError:
        pass


@router.post("/settings/grading")
async def save_grading(request: Request) -> RedirectResponse:
    state = get_state()
    form = await request.form()
    g = state.settings.grading
    for key in (
        "grade_a_reallocated_max",
        "grade_b_reallocated_max",
        "grade_c_reallocated_max",
    ):
        v = form.get(key)
        if v is not None and str(v).strip() != "":
            setattr(g, key, int(v))
    g.fail_on_pending_sectors = form.get("fail_on_pending_sectors") == "on"
    g.fail_on_offline_uncorrectable = form.get("fail_on_offline_uncorrectable") == "on"
    temp = (form.get("thermal_excursion_c") or "").strip()
    g.thermal_excursion_c = int(temp) if temp else None
    await _save_settings_or_ignore(request)
    return RedirectResponse(url="/settings?saved=grading", status_code=303)


@router.post("/settings/printer")
async def save_printer(request: Request) -> RedirectResponse:
    state = get_state()
    form = await request.form()
    p = state.settings.printer
    p.model = (form.get("model") or "").strip() or None
    p.connection = (form.get("connection") or "usb").strip()
    p.label_roll = (form.get("label_roll") or "").strip() or None
    await _save_settings_or_ignore(request)
    return RedirectResponse(url="/settings?saved=printer", status_code=303)


@router.post("/settings/integrations")
async def save_integrations(request: Request) -> RedirectResponse:
    state = get_state()
    form = await request.form()
    i = state.settings.integrations
    i.webhook_url = (form.get("webhook_url") or "").strip() or None
    i.cloudflare_tunnel_hostname = (
        (form.get("cloudflare_tunnel_hostname") or "").strip() or None
    )
    await _save_settings_or_ignore(request)
    return RedirectResponse(url="/settings?saved=integrations", status_code=303)


@router.post("/settings/daemon")
async def save_daemon(request: Request) -> RedirectResponse:
    state = get_state()
    form = await request.form()
    d = state.settings.daemon
    old_host = d.host
    old_port = d.port
    d.host = (form.get("host") or d.host).strip()
    port_v = form.get("port")
    if port_v:
        d.port = int(port_v)
    restart_needed = old_host != d.host or old_port != d.port
    await _save_settings_or_ignore(request)
    suffix = "&restart=1" if restart_needed else ""
    return RedirectResponse(url=f"/settings?saved=daemon{suffix}", status_code=303)


@router.post("/settings/wizard-replay")
async def replay_wizard(request: Request) -> RedirectResponse:
    state = get_state()
    state.settings.setup_completed = False
    await _save_settings_or_ignore(request)
    return RedirectResponse(url="/setup/1", status_code=303)


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
    temps = [t.drive_temp_c for t in telemetry_pts if t.drive_temp_c is not None]
    max_temp = max(temps) if temps else None
    avg_temp = round(sum(temps) / len(temps), 1) if temps else None
    return templates.TemplateResponse(
        request,
        "report.html",
        {
            "drive": drive,
            "run": latest,
            "telemetry": telemetry_pts,
            "capacity_tb": round(drive.capacity_bytes / 1_000_000_000_000, 2),
            "max_temp": max_temp,
            "avg_temp": avg_temp,
            "generated_at": datetime.utcnow(),
        },
    )
