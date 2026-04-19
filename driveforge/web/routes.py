"""Server-rendered web UI routes (HTMX + Jinja)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import joinedload

from driveforge.core import drive as drive_mod
from driveforge.core import enclosures
from driveforge.daemon.state import get_state
from driveforge.db import models as m

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


# Rough per-GB seconds coefficients per phase + media type. Good to one
# significant figure; refined with real-hardware telemetry later.
_ETA_BADBLOCKS_SEC_PER_GB = {"hdd": 16.0, "ssd": 5.0, "nvme": 2.0}
_ETA_ERASE_SEC_PER_GB = {"hdd": 6.0, "ssd": 0.5, "nvme": 0.1}
_ETA_LONG_TEST_SEC_PER_GB = {"hdd": 12.0, "ssd": 0.5, "nvme": 0.5}


def _media_kind(drive_row) -> str:
    if drive_row.transport == "nvme":
        return "nvme"
    # Without rotation info on the DB row, assume SAS/SATA spinning for now.
    # SSD detection can be refined later via the discovered drive metadata.
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


def _bay_card(
    state,
    session,
    bay_key: str,
    display_bay: int,
    *,
    installed_drive: "drive_mod.Drive | None" = None,
) -> dict:
    """Render a single bay card.

    Three visual states:
      - empty: slot has no drive installed
      - installed (idle): drive is physically in the slot but no test running
      - active: drive is under test (assigned in bay_assignments)
    """
    serial = state.bay_assignments.get(bay_key)
    if serial:
        drive = session.get(m.Drive, serial)
        if drive is not None:
            phase = state.active_phase.get(serial, "queued")
            # In-flight run: grab started_at for elapsed/eta
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
                "bay": display_bay,
                "state": "active",
                "key": bay_key,
                "serial": serial,
                "model": drive.model,
                "capacity_tb": round(drive.capacity_bytes / 1_000_000_000_000, 2),
                "phase": phase,
                "phase_class": _PHASE_CLASS.get(phase, "info"),
                "percent": state.active_percent.get(serial, 0.0),
                "elapsed_label": _format_duration(elapsed_sec),
                "eta_label": f"~{_format_duration(eta)}" if eta else None,
            }
    if installed_drive is not None:
        # Look up the most recent completed test for this drive so the card
        # shows "Grade B · tested 2026-04-19" or "✗ Failed" at a glance.
        last_run = (
            session.query(m.TestRun)
            .filter_by(drive_serial=installed_drive.serial)
            .filter(m.TestRun.completed_at.isnot(None))
            .order_by(m.TestRun.completed_at.desc())
            .first()
        )
        last_grade = last_run.grade if last_run else None
        last_tested = last_run.completed_at if last_run else None
        last_phase = last_run.phase if last_run else None
        last_error = None
        if last_run and last_run.error_message:
            # Extract the "[phase]" portion and a short summary for the card
            msg = last_run.error_message.strip().split("\n", 1)[0]
            last_error = msg[:80] + ("…" if len(msg) > 80 else "")
        return {
            "bay": display_bay,
            "state": "installed",
            "key": bay_key,
            "serial": installed_drive.serial,
            "model": installed_drive.model,
            "capacity_tb": installed_drive.capacity_tb,
            "last_grade": last_grade,
            "last_tested": last_tested,
            "last_phase": last_phase,
            "last_error": last_error,
        }
    return {"bay": display_bay, "state": "empty", "key": bay_key}


def _bay_view(state, session) -> dict:
    """Compose the grouped bay view from enclosure plan + orchestrator state."""
    plan = state.bay_plan
    # One-shot lsblk to get serials for every device currently present.
    # Used to show "installed but idle" bays.
    discovered = {d.device_path: d for d in drive_mod.discover()}

    enclosure_groups = []
    for enc_idx, enc in enumerate(plan.enclosures):
        bays = []
        for slot in enc.slots:
            key = f"e{enc_idx}:s{slot.slot_number}"
            installed = discovered.get(slot.device) if slot.device else None
            card = _bay_card(state, session, key, slot.slot_number + 1, installed_drive=installed)
            card["slot_number"] = slot.slot_number
            card["device"] = slot.device
            bays.append(card)
        enclosure_groups.append(
            {
                "label": enc.label,
                "vendor": enc.vendor,
                "sg_device": enc.sg_device,
                "slot_count": enc.slot_count,
                "populated_count": enc.populated_count,
                "bays": bays,
            }
        )
    # Partition discovered drives: bay-eligible (SATA / SAS) go into virtual
    # bays; NVMe / USB go to the unbayed section (they're never in a front
    # bay on this hardware class).
    in_slot_devices = {slot.device for enc in plan.enclosures for slot in enc.slots if slot.device}
    bay_eligible: list = []
    unbayed_eligible: list = []
    for d in discovered.values():
        if d.device_path in in_slot_devices:
            continue
        if d.transport in (drive_mod.Transport.NVME, drive_mod.Transport.USB):
            unbayed_eligible.append(d)
        else:
            bay_eligible.append(d)
    # Stable ordering so drives don't jump bays across refreshes
    bay_eligible.sort(key=lambda d: d.serial)

    virtual_bays = []
    if not plan.has_real_enclosures:
        # Build a stable serial → virtual-bay mapping for idle drives so the
        # dashboard shows them parked in bays instead of bucketed as unbayed.
        idle_serials_in_order = [
            d.serial for d in bay_eligible
            if d.serial not in state.bay_assignments.values()
        ]
        virtual_map: dict[int, "drive_mod.Drive"] = {}
        slot_idx = 0
        for d in bay_eligible:
            if d.serial in state.bay_assignments.values():
                continue  # active drive — bay_assignments already tracks it
            while slot_idx < plan.virtual_bay_count and f"v{slot_idx}" in state.bay_assignments:
                slot_idx += 1
            if slot_idx >= plan.virtual_bay_count:
                break
            virtual_map[slot_idx] = d
            slot_idx += 1
        for i in range(plan.virtual_bay_count):
            key = f"v{i}"
            installed = virtual_map.get(i)
            virtual_bays.append(_bay_card(state, session, key, i + 1, installed_drive=installed))
        # Any bay_eligible drives that didn't fit fall through to unbayed
        assigned_serials = {d.serial for d in virtual_map.values()}
        overflow = [d for d in bay_eligible if d.serial not in assigned_serials
                    and d.serial not in state.bay_assignments.values()]
    else:
        overflow = []

    # Unbayed section: NVMe / USB drives + overflow from virtual bays
    unbayed: list[dict] = []
    for d in unbayed_eligible + overflow:
        key = enclosures.unbayed_key(d.serial)
        card = _bay_card(state, session, key, 0, installed_drive=d)
        unbayed.append(card)
    return {
        "enclosures": enclosure_groups,
        "virtual_bays": virtual_bays,
        "unbayed": unbayed,
        "has_real_enclosures": plan.has_real_enclosures,
        "total_bays": plan.total_bays,
    }


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    state = get_state()
    with state.session_factory() as session:
        view = _bay_view(state, session)
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
        view = _bay_view(state, session)
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
    return templates.TemplateResponse(
        request, "new_batch.html", {"drives": drives, "err": err}
    )


@router.post("/batches/new")
async def new_batch_submit(request: Request) -> RedirectResponse:
    form = await request.form()
    source = form.get("source") or None
    selected = form.getlist("drive")
    quick = form.get("quick") == "on"
    confirm = (form.get("confirm") or "").strip().upper()
    if confirm != "ERASE":
        # Round-trip back to the form with an error banner
        return RedirectResponse(url="/batches/new?err=confirm", status_code=303)
    drives = [d for d in drive_mod.discover() if d.serial in selected]
    if not drives:
        drives = drive_mod.discover()
    orch = request.app.state.orchestrator
    await orch.start_batch(drives, source=source, quick=quick)
    return RedirectResponse(url="/", status_code=303)


@router.post("/abort-all")
async def abort_all_web(request: Request) -> RedirectResponse:
    orch = request.app.state.orchestrator
    await orch.abort_all()
    return RedirectResponse(url="/", status_code=303)


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
            }
            for r in runs
        ]
    return templates.TemplateResponse(
        request,
        "batch_detail.html",
        {"batch": batch_view, "runs": runs_view, "totals": totals},
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
    with state.session_factory() as session:
        approvals = session.query(m.FirmwareApproval).order_by(m.FirmwareApproval.approved_at.desc()).all()
        approvals_view = [
            {
                "id": a.id,
                "model": a.model,
                "transport": a.transport,
                "version": a.version,
                "signature_verified": a.signature_verified,
                "approved_at": a.approved_at,
            }
            for a in approvals
        ]
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": state.settings,
            "saved_panel": saved,
            "restart_required": restart == "1",
            "firmware_approvals": approvals_view,
        },
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
    i.firmware_db_url = (form.get("firmware_db_url") or "").strip() or None
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
    virtual_v = form.get("virtual_bays")
    if virtual_v is not None and str(virtual_v).strip() != "":
        d.virtual_bays = max(0, int(virtual_v))
        # Re-plan so the dashboard picks up the new count immediately (only
        # effective when no real enclosures are present)
        state.refresh_bay_plan()
    restart_needed = old_host != d.host or old_port != d.port
    await _save_settings_or_ignore(request)
    suffix = "&restart=1" if restart_needed else ""
    return RedirectResponse(url=f"/settings?saved=daemon{suffix}", status_code=303)


@router.post("/settings/firmware")
async def save_firmware(request: Request) -> RedirectResponse:
    state = get_state()
    form = await request.form()
    f = state.settings.firmware
    f.auto_apply = form.get("auto_apply") == "on"
    f.require_canary = form.get("require_canary") == "on"
    f.trust_pubkey = (form.get("trust_pubkey") or "").strip()
    await _save_settings_or_ignore(request)
    return RedirectResponse(url="/settings?saved=firmware", status_code=303)


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
