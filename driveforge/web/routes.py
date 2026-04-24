"""Server-rendered web UI routes (HTMX + Jinja)."""

from __future__ import annotations

import asyncio
import functools
import io
import logging
from datetime import UTC, datetime
from pathlib import Path

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
    "recovering": "recover",
    "done": "done",
    "failed": "fail",
    "aborted": "fail",
    "interrupted": "fail",
}

# Single-glyph cues per phase so the card reads at a glance. Unicode
# symbols + a couple of emoji; kept monochrome-ish so they don't
# compete with the phase-colored progress bar. Intentionally minimal —
# not meant to replace the text label, just reinforce it.
_PHASE_ICONS = {
    "queued": "\u22EF",           # ⋯ horizontal ellipsis
    "pre_smart": "\u2695",        # ⚕ medical staff
    "short_test": "\u25D0",       # ◐ half-filled circle
    "firmware_check": "\u2699",   # ⚙ gear
    "secure_erase": "\u26A1",     # ⚡ high voltage
    "badblocks": "\U0001F525",    # 🔥 fire
    "long_test": "\u29D6",        # ⧖ hourglass
    "post_smart": "\u2695",       # ⚕ medical staff
    "grading": "\u2605",          # ★ black star
    "recovering": "\u21BB",       # ↻ clockwise open circle arrow
    "done": "\u2713",             # ✓ check
    "failed": "\u2717",           # ✗ ballot x
    "aborted": "\u2298",          # ⊘ circled division slash
    "interrupted": "\u2298",      # ⊘ same
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
    import time as _time
    # Short window during which the card's border pulses to signal a phase
    # change. 2.5 s is long enough to survive one HTMX refresh cycle
    # (3 s polling) but short enough that the animation feels snappy.
    phase_changed_at = state.phase_change_ts.get(serial)
    phase_just_changed = (
        phase_changed_at is not None
        and (_time.monotonic() - phase_changed_at) < 2.5
    )
    drive_temp = state.active_drive_temp.get(serial)
    # Render a sparkline of recent total throughput during high-I/O phases.
    # Only show when there's meaningful flow (peak >= 1 MB/s in the window)
    # — idle phases (secure_erase, smart tests) would just draw a flat line
    # and add noise.
    history = state.active_io_history.get(serial, [])
    spark_points: list[float] | None = None
    spark_peak: float | None = None
    if history:
        totals = [(h["read"] + h["write"]) for h in history]
        peak = max(totals)
        if peak >= 1.0:
            spark_points = totals
            spark_peak = peak
    return {
        "state": "active",
        "key": serial,
        "serial": serial,
        "model": drive.model,
        "manufacturer": drive.manufacturer,
        "capacity_tb": round(drive.capacity_bytes / 1_000_000_000_000, 2),
        "phase": phase,
        "phase_class": _PHASE_CLASS.get(phase, "info"),
        "phase_icon": _PHASE_ICONS.get(phase, ""),
        "drive_temp_c": drive_temp,
        "drive_temp_band": _temp_band(drive_temp),
        "spark_points": spark_points,
        "spark_peak": spark_peak,
        "phase_just_changed": phase_just_changed,
        "recovery_mode": serial in state.recovery_serials,
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
    last_poh = last_run.power_on_hours_at_test if last_run else None
    # v0.5.5: triage verdict for quick-pass runs (grade is NULL in that case).
    last_triage = last_run.triage_result if last_run else None
    # v0.5.5: healing delta (post - pre reallocations). Only meaningful
    # when both snapshots are populated; NULL on legacy pre-v0.5.5 rows.
    remapped_during_run: int | None = None
    if (
        last_run is not None
        and last_run.reallocated_sectors is not None
        and last_run.pre_reallocated_sectors is not None
    ):
        remapped_during_run = (
            last_run.reallocated_sectors - last_run.pre_reallocated_sectors
        )
    # Compact age label: "45k POH" or "5.2y" when POH is meaningful.
    # Hours → years at 24*365.25 = 8766 h/y. Skipped for drives we've
    # never tested (no POH captured) or drives still showing near-zero.
    drive_age_label: str | None = None
    if last_poh and last_poh >= 100:
        years = last_poh / 8766.0
        if years >= 0.9:
            drive_age_label = f"{years:.1f}y"
        else:
            drive_age_label = f"{int(last_poh / 1000)}k POH" if last_poh >= 1000 else f"{last_poh} POH"
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
    import time as _time
    # Briefly flash this card when the drive has just completed a pipeline
    # (pass/fail). Window is 3.5 s — long enough that one HTMX refresh cycle
    # lands inside it, short enough to feel transient. Aborted runs don't
    # flash (stamped only on clean completions in _run_drive's finally).
    completed_at = state.just_completed_ts.get(drive.serial)
    just_completed = (
        completed_at is not None
        and (_time.monotonic() - completed_at) < 3.5
    )
    # Is the operator currently identifying this drive via the LED strobe?
    # The template uses this to flip the Ident button label → Stop.
    orch = getattr(state, "orchestrator", None)
    identifying = bool(orch and orch.is_identifying(drive.serial))
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
        "last_triage": last_triage,
        "remapped_during_run": remapped_during_run,
        "last_error": last_error,
        "drive_age_label": drive_age_label,
        "just_completed": just_completed,
        "identifying": identifying,
        # v0.5.5+ — True when the operator has opted into the "prompt"
        # mode for quick-pass triage=fail (settings.daemon.quick_pass_fail_action="prompt")
        # AND the drive's latest quick-pass triaged as fail. The card
        # template renders a banner with Yes / Dismiss buttons.
        "promote_prompt": drive.serial in state.promote_prompts,
    }


def _remote_active_card(agent_state, drive_state) -> dict:
    """v0.10.1+ — render a card for a remote agent's active drive.

    Mirrors `_active_card()` shape so the dashboard template can
    iterate `view.active` without branching on local-vs-remote. The
    `host_id` + `host_display` fields are what trigger the host-badge
    render in the card template.
    """
    import time as _time
    phase = drive_state.phase or "queued"
    phase_changed_at = drive_state.phase_change_ts_epoch
    phase_just_changed = (
        phase_changed_at is not None
        and (_time.monotonic() - phase_changed_at) < 2.5
    )
    return {
        "state": "active",
        "key": f"{agent_state.agent_id}:{drive_state.serial}",
        "serial": drive_state.serial,
        "model": drive_state.model,
        "manufacturer": drive_state.manufacturer,
        "capacity_tb": round(drive_state.capacity_bytes / 1_000_000_000_000, 2) if drive_state.capacity_bytes else 0.0,
        "phase": phase,
        "phase_class": _PHASE_CLASS.get(phase, "info"),
        "phase_icon": _PHASE_ICONS.get(phase, ""),
        "drive_temp_c": drive_state.drive_temp_c,
        "drive_temp_band": _temp_band(drive_state.drive_temp_c),
        "spark_points": None,  # remote drives don't forward history yet
        "spark_peak": None,
        "phase_just_changed": phase_just_changed,
        "recovery_mode": False,  # remote recovery state not forwarded in v0.10.1
        "percent": drive_state.percent or 0.0,
        "sublabel": drive_state.sublabel,
        "io_rate": drive_state.io_rate,
        "elapsed_label": "",  # operator doesn't know run start time for remote runs yet
        "eta_label": None,
        # v0.10.1+ host identity
        "host_id": agent_state.agent_id,
        "host_display": agent_state.display_name,
        "host_offline": not agent_state.is_online(_time.monotonic()),
    }


def _remote_installed_card(agent_state, drive_state, session=None) -> dict:
    """v0.10.1+ — installed-section card for a remote agent's idle drive.

    v0.11.9+: now queries the operator's DB for the most recent TestRun
    on this serial originating from this agent (host_id=agent_id) and
    populates last_grade / last_tested / last_phase / triage just like
    the local `_installed_card` does. Pre-v0.11.9 these fields were
    hardcoded to None with a TODO comment ("those arrive with v0.10.3
    cert forwarding"); the forwarding shipped, the render didn't catch
    up — so the operator's dashboard always showed the agent's drive
    as "idle · never tested" even after a real run had completed and
    been ingested into the DB.

    `session` is optional for backwards compatibility with the small
    number of internal callers that don't have one handy; when None,
    the card renders without history (same shape as v0.10.1 → v0.11.8).
    The dashboard view layer (`_drive_view`) always passes a session.
    """
    import time as _time
    last_grade = None
    last_tested = None
    last_phase = None
    last_quick = False
    last_triage = None
    last_error = None
    drive_age_label = None
    remapped_during_run: int | None = None
    if session is not None:
        last_run = (
            session.query(m.TestRun)
            .filter_by(drive_serial=drive_state.serial, host_id=agent_state.agent_id)
            .filter(m.TestRun.completed_at.isnot(None))
            .order_by(m.TestRun.completed_at.desc())
            .first()
        )
        if last_run is not None:
            last_grade = last_run.grade
            last_tested = last_run.completed_at
            last_phase = last_run.phase
            last_quick = bool(last_run.quick_mode)
            last_triage = last_run.triage_result
            if last_run.error_message:
                msg = last_run.error_message.strip().split("\n", 1)[0]
                last_error = msg[:80] + ("…" if len(msg) > 80 else "")
            if (
                last_run.reallocated_sectors is not None
                and last_run.pre_reallocated_sectors is not None
            ):
                remapped_during_run = (
                    last_run.reallocated_sectors - last_run.pre_reallocated_sectors
                )
            last_poh = last_run.power_on_hours_at_test
            if last_poh and last_poh >= 100:
                years = last_poh / 8766.0
                if years >= 0.9:
                    drive_age_label = f"{years:.1f}y"
                else:
                    drive_age_label = (
                        f"{int(last_poh / 1000)}k POH" if last_poh >= 1000
                        else f"{last_poh} POH"
                    )
    return {
        "state": "installed",
        "key": f"{agent_state.agent_id}:{drive_state.serial}",
        "serial": drive_state.serial,
        "model": drive_state.model,
        "manufacturer": drive_state.manufacturer,
        "capacity_tb": round(drive_state.capacity_bytes / 1_000_000_000_000, 2) if drive_state.capacity_bytes else 0.0,
        "transport": drive_state.transport,
        "last_grade": last_grade,
        "last_tested": last_tested,
        "last_phase": last_phase,
        "last_quick": last_quick,
        "last_triage": last_triage,
        "remapped_during_run": remapped_during_run,
        "last_error": last_error,
        "drive_age_label": drive_age_label,
        "just_completed": False,
        # v0.10.2+ — protocol now carries `identifying` per-drive so
        # the operator's toggle button reflects agent reality.
        "identifying": bool(getattr(drive_state, "identifying", False)),
        "promote_prompt": False,
        "host_id": agent_state.agent_id,
        "host_display": agent_state.display_name,
        "host_offline": not agent_state.is_online(_time.monotonic()),
    }


def _drive_view(state, session, *, host_filter: str | None = None) -> dict:
    """Compose the drive-centric dashboard view.

    Returns two flat lists:
      - active: one card per drive currently in the test pipeline (serial
        is in state.active_phase). Ordered by insertion into the pipeline.
      - installed: one card per drive currently present on the host that
        is NOT active. Ordered by serial.

    v0.10.1+ operator role: remote-agent drives are merged into both
    lists after local drives. `host_filter` restricts the view to one
    host_id ("local" for the operator's own drives, an agent_id for
    a specific agent, or None for everything).

    No enclosures, no slot groupings, no empty placeholders. Drives that
    are pulled disappear from the view automatically on the next refresh.
    """
    # One-shot lsblk to get the currently-present drives.
    # v0.10.1+: agent-role daemons skip local discovery if they're
    # serving nothing — but the standalone / operator default is to
    # always include the operator's own local drives, because an
    # operator is also a pipeline runner.
    show_local = host_filter in (None, "local")

    # Active section: preserve orchestrator insertion order (dict iteration).
    # v0.6.5+: snapshot the keys into a list before iterating — orchestrator
    # tasks mutate active_phase as drives transition between pipeline phases,
    # and under high concurrency (8+ drives rapid-fire) a live iteration
    # races with the write and raises "dictionary changed size during
    # iteration", 500'ing the dashboard request. list() takes the snapshot
    # atomically under the GIL.
    active_cards: list[dict] = []
    discovered: dict = {}
    if show_local:
        discovered = {d.serial: d for d in drive_mod.discover()}
        for serial in list(state.active_phase):
            card = _active_card(state, session, serial)
            if card is not None:
                active_cards.append(card)
    active_serials = {c["serial"] for c in active_cards}

    # Installed section: every currently-present local drive that isn't active.
    installed_cards: list[dict] = []
    if show_local:
        for serial in sorted(discovered.keys()):
            if serial in active_serials:
                continue
            installed_cards.append(_installed_card(state, session, discovered[serial]))

    # v0.10.1+ fleet aggregation — remote agent drives.
    # Operator renders each agent's drives inline with its own. The
    # host badge on the card lets operators visually separate them.
    remote_count = 0
    if state.settings.fleet.role == "operator":
        from driveforge.daemon import fleet_server
        for ra in fleet_server.all_known_agents(state):
            if host_filter not in (None, ra.agent_id):
                continue
            for drive_state in ra.drives.values():
                remote_count += 1
                if drive_state.phase:
                    active_cards.append(_remote_active_card(ra, drive_state))
                else:
                    # v0.11.9+: pass session so the card render can pull
                    # last-grade / last-tested / triage from the operator's
                    # DB (where agent completions are ingested as TestRun
                    # rows tagged with host_id=agent_id).
                    installed_cards.append(
                        _remote_installed_card(ra, drive_state, session=session)
                    )

    return {
        "active": active_cards,
        "installed": installed_cards,
        "total_present": len(discovered) + remote_count,
        # v0.10.1+ host filter state — templates render a dropdown /
        # chip when the fleet has any remote agents.
        "host_filter": host_filter,
        "fleet_role": state.settings.fleet.role,
    }


def _temp_band(temp_c: int | None) -> str:
    """Bucket a temperature reading into a CSS-friendly band label so the
    dashboard can color-code hot vs. cool. Bands are tuned for chassis
    ambient (inlet ~15-35 °C, exhaust ~20-45 °C normal, higher = hot)."""
    if temp_c is None:
        return "unknown"
    if temp_c < 30:
        return "cool"
    if temp_c < 45:
        return "normal"
    if temp_c < 55:
        return "warm"
    return "hot"


def _chassis_snapshot(state) -> dict | None:
    """Live chassis readings for the dashboard header strip. Returns None
    when nothing is available (keeps the header clean on consumer PCs)."""
    from driveforge.core import telemetry

    caps = state.capabilities
    if not (caps.chassis_power or caps.chassis_temperature):
        return None
    power = telemetry.read_chassis_power() if caps.chassis_power else None
    temps = telemetry.read_chassis_temperatures() if caps.chassis_temperature else {}
    inlet = temps.get("Inlet Temp")
    exhaust = temps.get("Exhaust Temp")
    if power is None and inlet is None and exhaust is None:
        return None
    return {
        "power_w": power,
        "inlet_c": inlet,
        "exhaust_c": exhaust,
        "inlet_band": _temp_band(inlet),
        "exhaust_band": _temp_band(exhaust),
    }


def _render_agent_status(request: Request, state) -> HTMLResponse:
    """v0.10.7+ agent-mode landing page.

    Intentionally sparse: operator's dashboard is where drive
    management happens. This page confirms the daemon is alive,
    shows connection state + pending-forward count, and links to
    the operator. No batch UI, no drive cards.
    """
    client = getattr(state, "fleet_client", None)
    client_status = client.status if client is not None else None
    pending_forward_count = 0
    with state.session_factory() as session:
        pending_forward_count = (
            session.query(m.TestRun)
            .filter(m.TestRun.pending_fleet_forward.is_(True))
            .count()
        )
    return templates.TemplateResponse(
        request,
        "agent_status.html",
        {
            "settings": state.settings,
            "client_status": client_status,
            "pending_forward_count": pending_forward_count,
            "operator_url": state.settings.fleet.operator_url,
        },
    )


def _available_hosts(state) -> list[dict]:
    """v0.10.1+ — list of hosts the dashboard's filter chip can
    switch between. Empty on standalone/agent roles (no fleet UI).
    On operators with no enrolled agents this returns a single
    "local" entry which the template collapses to a no-op.

    v0.11.3+ — the "this operator" count now reflects total drives
    (active + installed) that would render under the local filter,
    not just `len(state.active_phase)`. Pre-v0.11.3 a JT-screenshot
    bug: NX-3200 had 1 installed-but-idle drive and 1 R720 remote
    drive; pill row read `[All hosts 2] [this operator 0] [r720 1]`
    even though clicking "this operator" rendered the local INTEL
    drive correctly. Counts have to match the rendered view or
    operators distrust the chip.
    """
    if state.settings.fleet.role != "operator":
        return []
    from driveforge.daemon import fleet_server
    # v0.11.3+ — local drive count = lsblk-discovered ∪ active_phase.
    # Mirrors the presence rule the snapshot builder uses on agents
    # so the operator's "this operator" pill matches both its
    # locally-rendered cards AND what an agent would self-report.
    try:
        local_serials = {d.serial for d in drive_mod.discover()}
    except Exception:  # noqa: BLE001
        # discover() shouldn't ever raise (catches lsblk errors
        # internally) but if it does, don't crash the whole
        # dashboard — fall back to active count only.
        local_serials = set()
    local_serials |= set(state.active_phase.keys())
    entries: list[dict] = [{
        "id": "local",
        "display": state.settings.fleet.display_name or "this operator",
        "drives": len(local_serials),
        "online": True,
    }]
    import time as _time
    now = _time.monotonic()
    for ra in fleet_server.all_known_agents(state):
        entries.append({
            "id": ra.agent_id,
            "display": ra.display_name,
            "drives": len(ra.drives),
            "online": ra.is_online(now),
        })
    return entries


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    state = get_state()
    # v0.10.7+ — agent role renders a minimal read-only status page
    # instead of the full dashboard. Operator is the canonical
    # surface; local UI just confirms "yes I'm running, yes I'm
    # connected, here's how to reach the operator."
    if state.settings.fleet.role == "agent":
        return _render_agent_status(request, state)
    host_filter = request.query_params.get("host") or None
    with state.session_factory() as session:
        view = _drive_view(state, session, host_filter=host_filter)
    chassis = _chassis_snapshot(state)
    # v0.10.2+ — drain any failed remote-command results so the
    # dashboard shows a banner once, then forgets. Typical causes:
    # agent refused an abort mid-secure-erase, identify hit the
    # "drive not present" path, regrade had no prior A/B/C run.
    fleet_errors: list[dict] = []
    if state.settings.fleet.role == "operator":
        from driveforge.daemon import fleet_server
        for ra, result in fleet_server.drain_command_failures(state):
            fleet_errors.append({
                "host": ra.display_name,
                "command": result.command,
                "detail": result.detail or "(no detail)",
            })
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "view": view,
            "chassis": chassis,
            "settings": state.settings,
            "available_hosts": _available_hosts(state),
            "current_host_filter": host_filter,
            "fleet_errors": fleet_errors,
        },
    )


@router.post("/settings/auto-enroll")
async def set_auto_enroll(request: Request) -> RedirectResponse:
    """Toggle auto-enrollment mode from the dashboard segmented control.

    v0.10.9+: when this daemon is the fleet operator, the mode is
    broadcast to every connected agent via ConfigUpdateMsg so the
    toggle applies fleet-wide. Agents update their cached operator-
    mode value; the NEXT hotplug event on the agent honors it.
    Pre-v0.10.9 each agent had its own invisible toggle that
    stayed stale after an operator click.
    """
    state = get_state()
    form = await request.form()
    mode = (form.get("mode") or "off").strip().lower()
    if mode not in ("off", "quick", "full"):
        mode = "off"
    state.settings.daemon.auto_enroll_mode = mode
    await _save_settings_or_ignore(request)

    # v0.10.9+ fleet broadcast. Enqueue on every connected agent's
    # outbound queue; agents with no active session miss this tick
    # but pick up the new value on their next reconnect via
    # HelloAckMsg.
    if state.settings.fleet.role == "operator":
        from driveforge.core import fleet_protocol as proto
        from driveforge.daemon import fleet_server
        update_msg = proto.ConfigUpdateMsg(auto_enroll_mode=mode)
        for agent_id in list(state.remote_agents.keys()):
            try:
                await fleet_server.send_command_to_agent(
                    state, agent_id, update_msg,
                )
            except fleet_server.CommandDispatchError:
                # Agent offline — no-op; they'll get the new value
                # from HelloAckMsg on reconnect.
                pass

    return RedirectResponse(url="/", status_code=303)


@router.get("/_partials/bays", response_class=HTMLResponse)
def bays_partial(request: Request) -> HTMLResponse:
    """HTMX polling endpoint for live dashboard refresh."""
    state = get_state()
    host_filter = request.query_params.get("host") or None
    with state.session_factory() as session:
        view = _drive_view(state, session, host_filter=host_filter)
    return templates.TemplateResponse(request, "_bays.html", {"view": view})


SUGGESTED_USE = {
    "A": "Primary Ceph OSD, TrueNAS main pool — no reservations",
    "B": "Secondary OSD, scratch pool, backup target",
    "C": "Cold storage, test environment, heavy-redundancy array",
    "F": "Scrap / e-waste — drive failed grading rules, do not deploy",
    "error": "Retry — pipeline errored; drive's actual health is unknown",
    # Legacy "fail" rows (pre-v0.5.1) — before the F/error split existed.
    # Render with a generic "not-a-grade" message until operator re-runs.
    "fail": "Legacy fail from pre-v0.5.1 code — re-test to get a real verdict (F or A/B/C)",
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
            # v0.6.9+ frozen-SSD remediation state. None when this drive
            # isn't currently flagged as frozen; populated when the
            # orchestrator registered it after a libata-freeze pattern.
            "frozen_remediation_state": state.frozen_remediation.get(serial),
            # v0.9.0+ password-locked remediation state. None when this
            # drive isn't currently flagged as security-locked; populated
            # when secure_erase preflight failed with the locked pattern
            # AND the vendor-factory-master auto-recovery also failed.
            "password_locked_state": state.password_locked.get(serial),
            # v0.7.0+ active-pipeline phase for this drive. None when
            # the drive isn't in _tasks (most common case — the drive
            # was tested earlier, the card is informational). The
            # template renders an inline Abort button when non-None
            # so operators don't have to go back to the dashboard to
            # abort from here.
            "active_phase": state.active_phase.get(serial),
            "active_sublabel": state.active_sublabel.get(serial),
            # v0.8.0+: expose Settings so the buyer-report template can
            # reference rated_tbw_* thresholds when rendering the "X% of
            # rated TBW" inline context on the Wear & lifetime I/O
            # section. Also powers the class-dependent rated-TB lookup.
            "settings": state.settings,
            # v0.8.0+: the set of serials currently present on the HBA
            # that AREN'T in active_phase. The Regrade button renders
            # only for drives in this set (must be physically present +
            # not currently running a pipeline).
            "installed_serials": {
                s for s in state.device_basenames
                if s not in state.active_phase
            },
            # v0.8.0+: "Report generated: <ts>" line on the print-only
            # header. Fresh on every pageload so the printed sheet
            # carries a real timestamp.
            "report_generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
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
            totals = {"A": 0, "B": 0, "C": 0, "F": 0, "error": 0, "fail": 0}
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
    """Batch creation form.

    v0.11.7+ — when the daemon is running as a fleet operator, the
    drive list merges:
      - the operator's own locally-discovered drives, and
      - every drive reported by every connected agent
        (`state.remote_agents[*].drives`).

    The POST handler (`new_batch_submit`) already routes per-serial
    via `fleet_server.find_agent_for_serial`, so the form just needs
    to expose the agent drives so they can be ticked. Each row carries
    a `host_display` so the operator can tell at a glance which box
    a drive lives on. Remote drives that are mid-pipeline render with
    a disabled checkbox, mirroring the local-busy behavior.
    """
    state = get_state()
    err = request.query_params.get("err")
    orch = request.app.state.orchestrator
    busy = orch.active_serials()
    fleet_role = state.settings.fleet.role
    is_operator = fleet_role == "operator"

    drives_view: list[dict] = []
    # Local drives — operators + standalone include their own chassis.
    # An agent-role daemon never serves the dashboard (the lockdown
    # middleware blocks /batches/new entirely), so we don't have to
    # special-case it here, but the `is_operator` flag controls whether
    # we render the host badge for local rows so single-host setups
    # don't get visual clutter.
    for d in drive_mod.discover():
        drives_view.append({
            "serial": d.serial,
            "model": d.model,
            "capacity_tb": d.capacity_tb,
            "transport": d.transport.value if hasattr(d.transport, "value") else str(d.transport),
            "active": d.serial in busy,
            "host_display": "this operator" if is_operator else None,
            "host_offline": False,
        })

    # v0.11.7+ — operator-mode fleet drives. Iterate every known agent
    # (online or offline; offline agents still surface their last-seen
    # drives so the operator can tell what's missing). Each agent's
    # `drives` dict is the most-recent DriveSnapshotMsg the operator
    # received — `phase` is None for idle drives, set for active ones.
    if is_operator:
        from driveforge.daemon import fleet_server
        import time as _time
        now = _time.monotonic()
        for ra in fleet_server.all_known_agents(state):
            offline = not ra.is_online(now)
            for ds in ra.drives.values():
                drives_view.append({
                    "serial": ds.serial,
                    "model": ds.model,
                    "capacity_tb": (
                        round(ds.capacity_bytes / 1_000_000_000_000, 2)
                        if ds.capacity_bytes else 0.0
                    ),
                    "transport": (ds.transport or "").upper() or "UNKNOWN",
                    # A remote drive is "active" (uncheckable) if its
                    # most-recent snapshot showed it mid-pipeline OR if
                    # the agent is offline (we can't dispatch to it
                    # right now).
                    "active": (ds.phase is not None) or offline,
                    "host_display": ra.display_name,
                    "host_offline": offline,
                })

    return templates.TemplateResponse(
        request, "new_batch.html",
        {"drives": drives_view, "err": err, "is_operator": is_operator},
    )


def _primary_lan_ip() -> str | None:
    """v0.11.3+ — best-effort primary IPv4 detection. Used when
    composing the operator URL for adoption packages so agents get
    a hostname-resolution-free address.

    Strategy: open a UDP socket to a non-routable test target;
    inspect the local socket's bound address. No packet leaves the
    box (UDP socket bound but never sent on). Reliable on DHCP
    networks; preserves Linux's default-route choice when there are
    multiple interfaces.
    """
    import socket as _socket
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.settimeout(0.5)
        # 192.0.2.1 is in TEST-NET-1 (RFC 5737) — guaranteed non-
        # routable, won't actually generate a packet on connect()
        # for a UDP socket.
        s.connect(("192.0.2.1", 1))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return None


def _new_cmd_id() -> str:
    """Short random id for correlating CommandResultMsg replies with
    the POST handler that dispatched the command. Not security-
    sensitive — just a log-friendly identifier."""
    import secrets
    return secrets.token_hex(6)


async def _forward_start_pipeline_to_agent(
    state, agent_id: str, serial: str, *, quick: bool, source: str | None,
    batch_id: str | None = None,
) -> None:
    """Enqueue a StartPipelineCmd on the target agent's outbound
    queue. Fire-and-forget; the drive's state appears in the next
    snapshot (≤3s) if the agent accepts, else the CommandResultMsg
    reply lands in `recent_command_results` for the dashboard flash
    area.

    Raises `fleet_server.CommandDispatchError` if the agent has no
    active session. Callers catch + surface to the operator.

    v0.11.9+: `batch_id` carries the operator-minted batch id so the
    agent's TestRun row joins back to the operator's Batch row on
    completion ingestion.
    """
    from driveforge.core import fleet_protocol as proto
    from driveforge.daemon import fleet_server
    cmd = proto.StartPipelineCmd(
        cmd_id=_new_cmd_id(), serial=serial, quick_mode=quick, source=source,
        batch_id=batch_id,
    )
    await fleet_server.send_command_to_agent(state, agent_id, cmd)


async def _forward_abort_to_agent(state, agent_id: str, serial: str) -> None:
    from driveforge.core import fleet_protocol as proto
    from driveforge.daemon import fleet_server
    cmd = proto.AbortCmd(cmd_id=_new_cmd_id(), serial=serial)
    await fleet_server.send_command_to_agent(state, agent_id, cmd)


async def _forward_identify_to_agent(state, agent_id: str, serial: str, *, on: bool) -> None:
    from driveforge.core import fleet_protocol as proto
    from driveforge.daemon import fleet_server
    cmd = proto.IdentifyCmd(cmd_id=_new_cmd_id(), serial=serial, on=on)
    await fleet_server.send_command_to_agent(state, agent_id, cmd)


async def _forward_regrade_to_agent(state, agent_id: str, serial: str) -> None:
    from driveforge.core import fleet_protocol as proto
    from driveforge.daemon import fleet_server
    cmd = proto.RegradeCmd(cmd_id=_new_cmd_id(), serial=serial)
    await fleet_server.send_command_to_agent(state, agent_id, cmd)


@router.post("/batches/new")
async def new_batch_submit(request: Request) -> RedirectResponse:
    # Imported locally so this module doesn't pull in the orchestrator on
    # collection-time — keeps test import graph shallow.
    from driveforge.daemon.orchestrator import BatchRejected
    from driveforge.daemon import fleet_server

    state = get_state()
    form = await request.form()
    source = form.get("source") or None
    selected = form.getlist("drive")
    quick = form.get("quick") == "on"
    confirm = (form.get("confirm") or "").strip().upper()
    if confirm != "ERASE":
        return RedirectResponse(url="/batches/new?err=confirm", status_code=303)

    # v0.10.2+ fan-out: for each selected serial, decide whether it
    # lives locally (send to local orchestrator) or on a remote agent
    # (forward StartPipelineCmd over the fleet socket). Serials absent
    # from both paths are dropped silently — matches pre-v0.10.2
    # behavior where unknown serials just didn't run.
    local_drives = []
    remote_dispatch: list[tuple[str, str]] = []  # (agent_id, serial)
    local_by_serial = {d.serial: d for d in drive_mod.discover()}
    for serial in selected:
        if serial in local_by_serial:
            local_drives.append(local_by_serial[serial])
            continue
        agent_id = fleet_server.find_agent_for_serial(state, serial)
        if agent_id is not None:
            remote_dispatch.append((agent_id, serial))

    # Empty selection → fall back to "all present local drives" (original
    # behavior). Operator clicking New Batch with no drives ticked means
    # they want the whole local chassis.
    if not local_drives and not remote_dispatch:
        local_drives = drive_mod.discover()

    # v0.11.9+: pre-mint the batch_id at the operator level so both the
    # local orchestrator AND every agent in the fan-out tag their TestRun
    # rows under the same id. Pre-v0.11.9 the agent minted its own id and
    # the operator's ingestion dropped it (`batch_id=None` hardcode in
    # fleet_server), making remote runs invisible from the batch detail
    # page. With a shared id, the operator's batch view shows the full
    # roster of drives — local + every agent — under one batch click.
    import uuid as _uuid
    from datetime import UTC, datetime
    from driveforge.db import models as m
    batch_id = _uuid.uuid4().hex[:12]
    # Create the Batch row up front so agent ingestion has a Batch row
    # to FK against, even when there are no local drives in this batch
    # (pure-remote dispatch). Idempotent via INSERT IGNORE-style guard
    # below in case start_batch beats us to the row.
    with state.session_factory() as session:
        if session.get(m.Batch, batch_id) is None:
            session.add(m.Batch(id=batch_id, source=source, started_at=datetime.now(UTC)))
            session.commit()

    orch = request.app.state.orchestrator
    if local_drives:
        try:
            await orch.start_batch(
                local_drives, source=source, quick=quick, batch_id=batch_id,
            )
        except BatchRejected:
            # Only error if there's NO remote dispatch either; otherwise
            # the remote path is still valid and we continue.
            if not remote_dispatch:
                return RedirectResponse(url="/batches/new?err=active", status_code=303)
    for agent_id, serial in remote_dispatch:
        try:
            await _forward_start_pipeline_to_agent(
                state, agent_id, serial, quick=quick, source=source,
                batch_id=batch_id,
            )
        except fleet_server.CommandDispatchError as exc:
            logger.warning("fleet: start_batch forward to %s failed: %s", agent_id, exc)
    return RedirectResponse(url="/", status_code=303)


@router.post("/abort-all")
async def abort_all_web(request: Request) -> RedirectResponse:
    """Global abort — kept for emergency-via-curl, not wired to the UI anymore.

    The dashboard uses per-drive abort buttons on each active card now
    (see POST /drives/{serial}/abort). This endpoint still works if you
    POST to it from the terminal, but it's no longer one click away.
    """
    orch = request.app.state.orchestrator
    await orch.abort_all()
    return RedirectResponse(url="/", status_code=303)


@router.post("/drives/{serial}/identify")
async def identify_drive_web(serial: str, request: Request) -> RedirectResponse:
    """Toggle the identify-LED strobe for a present drive.

    Click once: 5-minute rapid-flash ident so the operator can find the
    drive in the rack. Click again while it's running: stop the strobe
    and restore whatever pass/fail LED pattern was showing before.

    Refuses cleanly if the drive is currently under test (the pipeline
    is already lighting the activity LED) or if the drive is no longer
    physically present.

    v0.10.2+: routes to the owning agent when the drive lives on a
    remote node. Toggle state is read from the most recent snapshot's
    `identifying` bit rather than the operator's orchestrator.
    """
    from driveforge.daemon import fleet_server

    orch = request.app.state.orchestrator
    state = get_state()

    # v0.10.2+ remote routing
    remote_agent_id = fleet_server.find_agent_for_serial(state, serial)
    if remote_agent_id is not None:
        ra = state.remote_agents[remote_agent_id]
        drive_state = ra.drives.get(serial)
        currently_on = bool(getattr(drive_state, "identifying", False))
        try:
            await _forward_identify_to_agent(
                state, remote_agent_id, serial, on=not currently_on,
            )
        except fleet_server.CommandDispatchError as exc:
            logger.warning("fleet: identify forward to %s failed: %s", remote_agent_id, exc)
        return RedirectResponse(url="/", status_code=303)

    # Local path — unchanged from pre-v0.10.2
    if orch.is_identifying(serial):
        orch.stop_identify(serial)
        return RedirectResponse(url="/", status_code=303)
    discovered = {d.serial: d for d in drive_mod.discover()}
    drive = discovered.get(serial)
    if drive is None:
        return RedirectResponse(url="/", status_code=303)
    await orch.identify_drive(drive)
    return RedirectResponse(url="/", status_code=303)


@router.post("/drives/{serial}/abort")
async def abort_drive_web(serial: str, request: Request) -> RedirectResponse:
    """Abort a single drive's in-flight pipeline.

    UI safety layer (in `_bays.html`) disables the Abort button while a
    drive is in `secure_erase`, because killing the host process there
    doesn't stop the drive's internal format. For SAS `sg_format` that
    leaves the drive in "Medium format corrupted" state. Treating all
    erase phases as abort-disabled is simpler than per-transport logic
    and costs the operator at most a few minutes of waiting.

    Server-side we still honor the abort — the button disable is a UX
    guardrail, not a hard enforcement. If you really want to terminate
    a stuck erase process (e.g. hdparm hung past its timeout) you can
    still POST to this endpoint via curl.

    v0.7.0+: redirect carries the abort outcome via query params so
    the dashboard / drive-detail page can render an explicit banner
    ("Abort signalled for X" / "X isn't currently running"). Pre-v0.7.0
    this redirected to `/` with no flash, so operators saw no feedback
    either way and had to infer from journal absence that the click
    had landed. Redirect target follows Referer when it points back at
    a drive-detail page so the flash lands where the operator clicked
    from; falls through to `/` otherwise (dashboard bay-card click).
    """
    from urllib.parse import quote, urlparse
    from driveforge.daemon import fleet_server

    state = get_state()

    # Resolve Referer redirect target once; both paths use it.
    referer = request.headers.get("Referer", "")
    dest = "/"
    if referer:
        try:
            parsed = urlparse(referer)
            if parsed.path == f"/drives/{serial}":
                dest = f"/drives/{serial}"
        except Exception:  # noqa: BLE001
            dest = "/"

    # v0.10.2+ remote routing
    remote_agent_id = fleet_server.find_agent_for_serial(state, serial)
    if remote_agent_id is not None:
        try:
            await _forward_abort_to_agent(state, remote_agent_id, serial)
            status, note = "forwarded", f"abort sent to agent {state.remote_agents[remote_agent_id].display_name}"
        except fleet_server.CommandDispatchError as exc:
            status, note = "dispatch_error", str(exc)
        sep = "&" if "?" in dest else "?"
        params = (
            f"aborted={status}"
            f"&abort_serial={quote(serial)}"
            f"&abort_note={quote(note)}"
        )
        return RedirectResponse(url=f"{dest}{sep}{params}", status_code=303)

    # Local path
    orch = request.app.state.orchestrator
    outcome = await orch.abort_drive(serial)
    sep = "&" if "?" in dest else "?"
    params = (
        f"aborted={outcome['status']}"
        f"&abort_serial={quote(serial)}"
        f"&abort_note={quote(str(outcome['note']))}"
    )
    return RedirectResponse(url=f"{dest}{sep}{params}", status_code=303)


@router.post("/drives/{serial}/promote-to-full")
async def promote_to_full_web(serial: str, request: Request) -> RedirectResponse:
    """v0.5.5+ \u2014 operator confirmed the quick-pass fail prompt.

    Starts a full-pipeline batch on this drive and clears the prompt.
    Only meaningful when the drive's latest run was a quick-pass with
    triage=fail AND the prompt was surfaced by the
    `quick_pass_fail_action="prompt"` setting path.
    """
    state = get_state()
    state.promote_prompts.discard(serial)
    orch = request.app.state.orchestrator
    # Re-discover the drive to get a current Drive object with live
    # device path (kernel letters can drift across reboots).
    from driveforge.core import drive as drive_mod
    drives = {d.serial: d for d in drive_mod.discover()}
    match = drives.get(serial)
    if match is None:
        # Drive was pulled between the prompt rendering and the click.
        # Nothing to do; fall through to dashboard.
        return RedirectResponse(url="/", status_code=303)
    try:
        await orch.start_batch(
            [match],
            source="operator-promoted after quick-pass triage=fail",
            quick=False,
        )
    except Exception:  # noqa: BLE001
        logger.exception("promote-to-full failed for %s", serial)
    return RedirectResponse(url="/", status_code=303)


@router.post("/drives/{serial}/frozen/retry")
async def frozen_remediation_retry(serial: str, request: Request) -> RedirectResponse:
    """v0.6.9+: operator clicked "I tried something, retest" in the
    frozen-SSD remediation panel. Marks the entry as retried (the
    orchestrator will bump retry_count + promote status on the NEXT
    failed run) and triggers a fresh pipeline on the current physical
    device.

    Does not clear the remediation entry — that happens automatically
    when the next pipeline either succeeds (via `_finalize_run`'s
    clear call) or fails with the freeze signature again (which will
    re-register via `register_freeze`'s retry-bump branch).

    If the drive is no longer present (pulled between panel view and
    click), we fall through to the dashboard quietly. The next
    insertion will re-fire the pipeline via normal hotplug.
    """
    from driveforge.core import drive as drive_mod
    from driveforge.core import frozen_remediation

    state = get_state()
    orch = request.app.state.orchestrator
    frozen_remediation.mark_retried(state.frozen_remediation, serial)

    drives = {d.serial: d for d in drive_mod.discover()}
    match = drives.get(serial)
    if match is None:
        return RedirectResponse(url=f"/drives/{serial}?frozen=retry-queued", status_code=303)
    try:
        await orch.start_batch(
            [match],
            source="operator-retry after frozen-SSD remediation",
            quick=False,
        )
    except Exception:  # noqa: BLE001
        logger.exception("frozen-remediation retry failed for %s", serial)
        return RedirectResponse(
            url=f"/drives/{serial}?frozen=retry-failed",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/drives/{serial}?frozen=retry-started",
        status_code=303,
    )


@router.post("/drives/{serial}/frozen/mark-unrecoverable")
async def frozen_remediation_mark_unrecoverable(
    serial: str, request: Request
) -> RedirectResponse:
    """v0.6.9+: operator clicked "Mark as unrecoverable" in the
    frozen-SSD remediation panel. Stamps an explicit F grade on the
    latest TestRun for this serial (with a `fail_reason` explaining
    the origin) so that auto-enroll's F-is-sticky logic skips this
    drive on future inserts.

    Clears the in-memory remediation entry in the same call — the F
    grade is the persistent marker going forward, the remediation
    panel's job is done.

    If there is NO latest TestRun for this serial (operator marked
    an enrolled-but-never-tested drive), create a minimal TestRun
    with grade=F + phase="frozen_unrecoverable" so the DB carries
    the sticky marker. Phase value is a v0.6.9+ addition; older
    reports UI tolerates unknown phase strings.
    """
    from datetime import UTC, datetime
    from driveforge.core import frozen_remediation

    state = get_state()
    with state.session_factory() as session:
        last_run = (
            session.query(m.TestRun)
            .filter(m.TestRun.drive_serial == serial)
            .order_by(m.TestRun.completed_at.desc().nulls_last())
            .first()
        )
        error_msg = (
            "frozen by libata, no remediation worked — marked "
            "unrecoverable by operator"
        )
        if last_run is None:
            new_run = m.TestRun(
                drive_serial=serial,
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                phase="frozen_unrecoverable",
                grade="F",
                error_message=error_msg,
            )
            session.add(new_run)
        else:
            last_run.grade = "F"
            last_run.phase = "frozen_unrecoverable"
            last_run.completed_at = last_run.completed_at or datetime.now(UTC)
            last_run.error_message = error_msg
        session.commit()

    frozen_remediation.clear(state.frozen_remediation, serial)

    # v0.11.9+ — fire a physical UNRECOVERABLE label print so the
    # operator's hand has a sticker to slap on the drive going into
    # the destroy bin. Best-effort; print failure does NOT undo the
    # F-grade stamp (DB is the source of truth, sticker is the
    # physical bridge to that decision). Failure surfaces in the
    # redirect's `print` query param so the drive-detail flash area
    # can show what happened.
    print_status = ""
    try:
        from driveforge.core import printer as printer_mod
        with state.session_factory() as session:
            drive_row = session.get(m.Drive, serial)
        if drive_row is not None:
            ok, msg = printer_mod.auto_print_unrecoverable_for_drive(
                state, drive_row, reason=error_msg,
            )
            print_status = "ok" if ok else f"err:{msg[:80]}"
        else:
            print_status = "err:no-drive-row"
    except Exception as exc:  # noqa: BLE001
        logger.exception("frozen mark-unrecoverable: print failed for %s", serial)
        print_status = f"err:{exc}"

    from urllib.parse import quote as _q
    return RedirectResponse(
        url=f"/drives/{serial}?frozen=marked-unrecoverable&print={_q(print_status)}",
        status_code=303,
    )


# ------------------------------------------ v0.9.0 password-locked routes


@router.post("/drives/{serial}/password-locked/try-unlock")
async def password_locked_try_unlock(
    serial: str, request: Request
) -> RedirectResponse:
    """v0.9.0+: operator entered a manual password in the remediation
    panel and clicked Try unlock. Runs `hdparm --security-disable
    <password> /dev/sdX` under the hood. Success → clears the
    remediation state + re-dispatches the drive through the pipeline.
    Failure → bumps manual_attempts counter + surfaces the reason in
    the panel's "last attempt" line.

    Bounded blast radius: we don't retry on our own. Each click =
    exactly one `hdparm --security-disable` attempt. Operators can
    see their remaining strikes in the panel
    (`attempts_remaining_estimate`) and decide whether to keep
    guessing vs. mark unrecoverable vs. destroy.
    """
    from urllib.parse import quote
    from driveforge.core import password_locked_remediation as pwd_lock
    from driveforge.core.process import run as run_sync

    state = get_state()
    form = await request.form()
    password = (form.get("password") or "").strip()
    if not password:
        return RedirectResponse(
            url=f"/drives/{serial}?pwd_error=" + quote("password field was empty"),
            status_code=303,
        )

    # Look up device path. Drive must be currently present.
    device_basename = state.device_basenames.get(serial)
    if not device_basename:
        return RedirectResponse(
            url=f"/drives/{serial}?pwd_error=" + quote(
                "drive not currently plugged in — reinsert to try unlock"
            ),
            status_code=303,
        )
    device_path = f"/dev/{device_basename}"

    # Run `hdparm --security-disable <pw> <device>`. This DISABLES the
    # security state (doesn't erase data) — if the password is right,
    # the drive comes back to CLEAN + accepts normal I/O. Pipeline will
    # then re-run secure_erase through the normal path.
    try:
        result = run_sync(
            [
                "hdparm",
                "--user-master", "u",
                "--security-disable", password,
                device_path,
            ],
            timeout=30,
            owner=serial,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("password-locked try-unlock crashed for %s", serial)
        pwd_lock.record_manual_attempt(
            state.password_locked, serial,
            ok=False, note=f"hdparm crashed: {exc}",
        )
        return RedirectResponse(
            url=f"/drives/{serial}?pwd_error=" + quote(f"hdparm crashed: {exc}"),
            status_code=303,
        )

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        note = f"hdparm rc={result.returncode}: {stderr[:120]}"
        pwd_lock.record_manual_attempt(
            state.password_locked, serial, ok=False, note=note,
        )
        logger.info(
            "password-locked manual attempt failed for %s: %s", serial, note,
        )
        return RedirectResponse(
            url=f"/drives/{serial}?pwd_error=" + quote(
                f"unlock failed ({stderr[:60] or 'wrong password'}). "
                f"Drive's internal counter has decremented — watch for lockout."
            ),
            status_code=303,
        )

    # Success — drive is unlocked. Clear remediation state + kick a
    # fresh pipeline run. Operator sees green banner on return.
    pwd_lock.record_manual_attempt(
        state.password_locked, serial,
        ok=True,
        note="manual unlock succeeded; re-enrolling drive for full pipeline",
    )
    pwd_lock.clear(state.password_locked, serial)
    logger.warning(
        "password-locked manual unlock SUCCEEDED on %s — re-enrolling for pipeline",
        serial,
    )

    # Re-enroll: fresh discovery + pipeline kick. Same pattern as the
    # frozen-remediation retry route.
    try:
        from driveforge.core import drive as drive_mod
        drives = {d.serial: d for d in drive_mod.discover()}
        match = drives.get(serial)
        if match is not None:
            orch = request.app.state.orchestrator
            await orch.start_batch(
                [match],
                source="password-locked manual unlock success",
                quick=False,
            )
    except Exception:  # noqa: BLE001
        logger.exception("password-locked post-unlock pipeline kick failed for %s", serial)

    return RedirectResponse(
        url=f"/drives/{serial}?pwd_ok=" + quote(
            "drive unlocked and re-enrolled for pipeline"
        ),
        status_code=303,
    )


@router.post("/drives/{serial}/password-locked/mark-unrecoverable")
async def password_locked_mark_unrecoverable(
    serial: str, request: Request
) -> RedirectResponse:
    """v0.9.0+: operator clicked Mark as unrecoverable in the
    password-locked remediation panel. Same mechanics as the
    frozen-remediation equivalent:
      - Stamp F grade on the latest TestRun (or create a minimal
        one if none exists)
      - Clear the remediation state entry
      - Redirect with a confirmation flash
    Use phase="password_locked_unrecoverable" to distinguish this
    failure mode from `frozen_unrecoverable` in the history view
    + reports.
    """
    from urllib.parse import quote
    from datetime import UTC, datetime as dt
    from driveforge.core import password_locked_remediation as pwd_lock

    state = get_state()
    with state.session_factory() as session:
        last_run = (
            session.query(m.TestRun)
            .filter(m.TestRun.drive_serial == serial)
            .order_by(m.TestRun.completed_at.desc().nulls_last())
            .first()
        )
        error_msg = (
            "security-locked by unknown password, no remediation worked — "
            "marked unrecoverable by operator"
        )
        if last_run is None:
            new_run = m.TestRun(
                drive_serial=serial,
                started_at=dt.now(UTC),
                completed_at=dt.now(UTC),
                phase="password_locked_unrecoverable",
                grade="F",
                error_message=error_msg,
            )
            session.add(new_run)
        else:
            last_run.grade = "F"
            last_run.phase = "password_locked_unrecoverable"
            last_run.completed_at = last_run.completed_at or dt.now(UTC)
            last_run.error_message = error_msg
        session.commit()

    pwd_lock.clear(state.password_locked, serial)

    # v0.11.9+ — physical UNRECOVERABLE label print, same shape as the
    # frozen-SSD path above. Best-effort; print failure doesn't roll
    # back the F-grade stamp.
    print_status = ""
    try:
        from driveforge.core import printer as printer_mod
        with state.session_factory() as session:
            drive_row = session.get(m.Drive, serial)
        if drive_row is not None:
            ok, msg = printer_mod.auto_print_unrecoverable_for_drive(
                state, drive_row, reason=error_msg,
            )
            print_status = "ok" if ok else f"err:{msg[:80]}"
        else:
            print_status = "err:no-drive-row"
    except Exception as exc:  # noqa: BLE001
        logger.exception("password-locked mark-unrecoverable: print failed for %s", serial)
        print_status = f"err:{exc}"

    return RedirectResponse(
        url=f"/drives/{serial}?pwd_ok=" + quote(
            "drive marked unrecoverable; F grade stamped, auto-enroll will skip this serial"
        ) + f"&print={quote(print_status)}",
        status_code=303,
    )


@router.post("/drives/{serial}/regrade")
async def regrade_drive(serial: str, request: Request) -> RedirectResponse:
    """v0.8.0+: re-apply current grading rules to a drive that's
    already been through a full pipeline.

    Non-destructive — reads fresh SMART and reuses the source TestRun's
    preserved pipeline outputs (badblocks errors, throughput stats,
    self-test results). Rules with ceiling semantics (POH / workload /
    SSD wear) see updated counters; grade can drop (drive has aged
    past a threshold) or stay the same (still within ceilings).
    Never promotes — a drive originally graded B can end up B or C,
    never A, unless the thresholds themselves have loosened.

    Creates a new `TestRun(phase="regrade")` with `regrade_of_run_id`
    pointing at the source, so the history column reflects the
    transition. Auto-prints if enabled.

    Refuses if:
      - drive is not currently present (no device path to read from)
      - drive is in `state.active_phase` (a pipeline is running;
        regrade would stomp its SMART-read queue)
      - no prior A/B/C TestRun exists (nothing to regrade from;
        operator needs to run a full pipeline first)

    Flash banner surfaces all three refusal modes explicitly.
    """
    from urllib.parse import quote
    from driveforge.core import drive_class as drive_class_mod
    from driveforge.core import drive as drive_mod
    from driveforge.core import grading, smart as smart_mod
    from driveforge.daemon import fleet_server

    state = get_state()

    # v0.10.2+ remote routing. Regrade forwards to the owning agent
    # so the re-grading runs locally where the drive lives (fresh
    # SMART requires actual device access). Result surfaces via
    # CommandResultMsg; the new run appears in the agent's next
    # snapshot.
    remote_agent_id = fleet_server.find_agent_for_serial(state, serial)
    if remote_agent_id is not None:
        try:
            await _forward_regrade_to_agent(state, remote_agent_id, serial)
            return RedirectResponse(
                url=(f"/drives/{serial}?regrade_forwarded="
                     + quote(f"sent to agent {state.remote_agents[remote_agent_id].display_name}")),
                status_code=303,
            )
        except fleet_server.CommandDispatchError as exc:
            return RedirectResponse(
                url=(f"/drives/{serial}?regrade_error="
                     + quote(f"agent unreachable: {exc}")),
                status_code=303,
            )

    # Refusal 1: drive actively running
    if serial in state.active_phase:
        return RedirectResponse(
            url=(f"/drives/{serial}?regrade_error="
                 + quote("drive is currently running a pipeline; abort or wait for it to finish")),
            status_code=303,
        )

    # Refusal 2: drive not physically present
    device_basename = state.device_basenames.get(serial)
    if not device_basename:
        return RedirectResponse(
            url=(f"/drives/{serial}?regrade_error="
                 + quote("drive is not currently plugged in — re-insert to regrade")),
            status_code=303,
        )
    device_path = f"/dev/{device_basename}"

    # Refusal 3: no prior completed A/B/C run to regrade from
    with state.session_factory() as session:
        source_run = (
            session.query(m.TestRun)
            .filter_by(drive_serial=serial)
            .filter(m.TestRun.grade.in_(["A", "B", "C"]))
            .filter(m.TestRun.completed_at.isnot(None))
            .order_by(m.TestRun.completed_at.desc())
            .first()
        )
        if source_run is None:
            return RedirectResponse(
                url=(f"/drives/{serial}?regrade_error="
                     + quote("no prior A/B/C pipeline run to regrade from — run a full pipeline first")),
                status_code=303,
            )
        source_run_id = source_run.id

        # Also pull the Drive row so the classifier has its fields.
        drive_row = session.get(m.Drive, serial)
        if drive_row is None:
            return RedirectResponse(
                url=(f"/drives/{serial}?regrade_error="
                     + quote("drive missing from DB unexpectedly — re-enroll via a full pipeline")),
                status_code=303,
            )

    # Capture fresh SMART (async, ~5 s — v0.6.9 migration gave us this)
    try:
        post_snap = await smart_mod.snapshot_async(device_path)
    except Exception as exc:  # noqa: BLE001
        logger.exception("regrade: SMART snapshot failed for %s", serial)
        return RedirectResponse(
            url=(f"/drives/{serial}?regrade_error="
                 + quote(f"failed to read fresh SMART: {exc}")),
            status_code=303,
        )

    # Re-classify — operator may have added an override since original
    # grading. Cheap (pure Python + YAML read).
    transport = (
        drive_row.transport.value
        if hasattr(drive_row.transport, "value")
        else str(drive_row.transport)
    )
    dclass = drive_class_mod.classify(
        model=drive_row.model,
        transport=transport,
        rotation_rate=getattr(drive_row, "rotation_rate", None),
        overrides_path=Path("/etc/driveforge/drive_class_overrides.yaml"),
    )

    # Build `pre` snapshot from the source run. We only need the fields
    # that degradation-detection rules read — reconstruct a minimal
    # SmartSnapshot with the source's post-SMART counters as the "pre"
    # baseline for THIS regrade. Semantically: "did counters get worse
    # since the original pipeline finished?"
    pre_snap = smart_mod.SmartSnapshot(
        device=device_path,
        captured_at=source_run.completed_at or datetime.now(UTC),
        reallocated_sectors=source_run.reallocated_sectors,
        current_pending_sector=source_run.current_pending_sector,
        offline_uncorrectable=source_run.offline_uncorrectable,
        power_on_hours=source_run.power_on_hours_at_test,
    )

    # Reconstruct ThroughputStats from the source (grading reads it to
    # apply within-pass-variance / pass-to-pass rules — we want those
    # to still fire consistently based on the original burn-in, not
    # falsely "pass" just because we're not running badblocks now).
    throughput = None
    if source_run.throughput_mean_mbps is not None:
        from driveforge.core.throughput import ThroughputStats
        throughput = ThroughputStats(
            mean_mbps=source_run.throughput_mean_mbps,
            p5_mbps=source_run.throughput_p5_mbps or 0,
            p95_mbps=source_run.throughput_p95_mbps or 0,
            per_pass_means=list(source_run.throughput_pass_means or []),
        )

    # Grade with the composite (source pipeline results + fresh SMART)
    result = grading.grade_drive(
        pre=pre_snap,
        post=post_snap,
        config=state.settings.grading,
        short_test_passed=True,  # source already passed; else it wouldn't be A/B/C
        long_test_passed=True,
        badblocks_errors=(0, 0, 0),  # source passed — not re-running badblocks
        max_temperature_c=None,
        throughput=throughput,
        drive_class=dclass,
    )

    # Persist new TestRun
    now = datetime.now(UTC)
    with state.session_factory() as session:
        new_run = m.TestRun(
            drive_serial=serial,
            batch_id=None,
            phase="regrade",
            started_at=now,
            completed_at=now,
            grade=result.grade.value,
            rules=[r.model_dump() for r in result.rules],
            report_url=f"/reports/{serial}",
            power_on_hours_at_test=post_snap.power_on_hours,
            reallocated_sectors=post_snap.reallocated_sectors,
            current_pending_sector=post_snap.current_pending_sector,
            offline_uncorrectable=post_snap.offline_uncorrectable,
            smart_status_passed=post_snap.smart_status_passed,
            # Preserved from the source pipeline — regrade doesn't re-run these
            throughput_mean_mbps=source_run.throughput_mean_mbps,
            throughput_p5_mbps=source_run.throughput_p5_mbps,
            throughput_p95_mbps=source_run.throughput_p95_mbps,
            throughput_pass_means=source_run.throughput_pass_means,
            sanitization_method=source_run.sanitization_method,
            # v0.8.0 buyer-transparency fields from the fresh snapshot
            lifetime_host_reads_bytes=post_snap.lifetime_host_reads_bytes,
            lifetime_host_writes_bytes=post_snap.lifetime_host_writes_bytes,
            wear_pct_used=post_snap.wear_pct_used,
            available_spare_pct=post_snap.available_spare_pct,
            end_to_end_error_count=post_snap.end_to_end_error_count,
            command_timeout_count=post_snap.command_timeout_count,
            reallocation_event_count=post_snap.reallocation_event_count,
            nvme_critical_warning=post_snap.nvme_critical_warning,
            nvme_media_errors=post_snap.nvme_media_errors,
            self_test_has_past_failure=post_snap.self_test_has_past_failure,
            drive_class=dclass,
            regrade_of_run_id=source_run_id,
        )
        session.add(new_run)
        session.commit()
        session.refresh(new_run)

    # Auto-print the new cert if configured. Failure is not fatal —
    # the DB row is already committed with the new grade.
    try:
        from driveforge.core import printer as printer_mod
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            state.drive_command_executor,
            functools.partial(
                printer_mod.auto_print_cert_for_run,
                state,
                drive_row,
                new_run,
            ),
        )
    except Exception:  # noqa: BLE001
        logger.exception("regrade: auto-print failed for %s (non-fatal)", serial)

    return RedirectResponse(
        url=(f"/drives/{serial}?regrade_ok="
             + quote(f"regraded to {result.grade.value} (was {source_run.grade})")),
        status_code=303,
    )


@router.post("/regrade-all-idle")
async def regrade_all_idle(request: Request) -> RedirectResponse:
    """v0.8.0+: batch regrade every installed-and-idle drive. Saves
    operators from clicking through N drive-detail pages one at a
    time. Dispatches to the same regrade path per drive; serializes
    (not parallel) to keep the HBA SG queue sane during the bulk
    SMART reads. Failures per-drive are logged but don't abort the
    batch."""
    state = get_state()
    idle_serials = [
        s for s in list(state.device_basenames)
        if s not in state.active_phase and s not in state.recovery_serials
    ]
    count = 0
    for serial in idle_serials:
        # Check prereq: a completed A/B/C run must exist
        with state.session_factory() as session:
            has_prior = (
                session.query(m.TestRun)
                .filter_by(drive_serial=serial)
                .filter(m.TestRun.grade.in_(["A", "B", "C"]))
                .filter(m.TestRun.completed_at.isnot(None))
                .first()
            )
        if has_prior is None:
            continue

        # Synthesize a request object-like shim for the internal call.
        # Easier: just call the route function directly — it returns a
        # RedirectResponse we can ignore. All per-drive error handling
        # is internal to `regrade_drive`.
        try:
            await regrade_drive(serial, request)
            count += 1
        except Exception:  # noqa: BLE001
            logger.exception("batch regrade: failed for %s", serial)

    from urllib.parse import quote
    return RedirectResponse(
        url=("/?regrade_batch_ok="
             + quote(f"regraded {count} drive(s)")),
        status_code=303,
    )


@router.post("/drives/{serial}/dismiss-promote-prompt")
async def dismiss_promote_prompt_web(serial: str, request: Request) -> RedirectResponse:
    """v0.5.5+ \u2014 operator dismissed the quick-pass fail prompt.

    Removes the drive from state.promote_prompts. The triage-fail badge
    stays on the card; only the inline prompt banner goes away. Operator
    can still run a full pipeline manually via New Batch.
    """
    state = get_state()
    state.promote_prompts.discard(serial)
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
    """Build CertLabelData for a given drive + run. Shared by preview +
    manual print. v0.6.4+ delegates to the request-free shared helper
    in core/printer — only difference here is we derive the QR-code
    URL from the incoming request (so the sticker QR resolves to the
    same host the operator is using to view the dashboard)."""
    from driveforge.core import printer as printer_mod

    report_url = _public_report_url(request, state, drive.serial)
    return printer_mod.build_cert_label_data_from_run(
        drive, run, report_url=report_url,
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
    # v0.6.1+: print_label now returns (ok, message). The message
    # carries the specific failure reason (unknown model, no USB
    # printer detected, pyusb error string, wrong-roll rejection)
    # so the banner can surface it verbatim instead of the old
    # generic "dispatch failed." The `roll` parameter (also v0.6.1+)
    # gets translated to brother_ql's label identifier so the raster's
    # label-type metadata matches the physical roll loaded.
    ok, print_msg = printer_mod.print_label(
        img,
        model=pc.model,
        backend=backend,
        identifier=pc.backend_identifier,
        roll=pc.label_roll,
    )
    if not ok:
        return False, print_msg
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
        # Print labels for every run that reached a real verdict —
        # A/B/C (pass tiers) AND F (drive-fail). The F sticker is
        # what lets the operator identify the bad drive in the scrap
        # pile; not printing it was a v0.5.1 bug, corrected here.
        #
        # Skip:
        #   - incomplete runs (completed_at NULL)
        #   - aborted runs (grade NULL)
        #   - pipeline-error runs ("error") — no verdict reached; the
        #     drive's actual health is unknown, there's nothing to
        #     certify OR to scrap. Printing a sticker for these would
        #     mislead the operator.
        #   - legacy "fail" (pre-v0.5.1 rows) — can't retroactively
        #     tell if it was real or pipeline-error; operator should
        #     retest for a real verdict.
        runs = (
            session.query(m.TestRun)
            .filter_by(batch_id=batch_id)
            .filter(m.TestRun.completed_at.isnot(None))
            .filter(m.TestRun.grade.in_(("A", "B", "C", "F")))
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
        totals = {"A": 0, "B": 0, "C": 0, "F": 0, "error": 0, "fail": 0}
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
                # Printable: A/B/C (cert) + F (bad-drive sticker).
                # Corrected v0.5.2 — v0.5.1 erroneously excluded F
                # from the printable set, contradicting the design
                # intent of "F prints, ERR doesn't."
                "printable": bool(
                    r.completed_at and r.grade in ("A", "B", "C", "F")
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
    """v0.9.0+: history page gained a serial search. Operators
    typically know a drive by the last 4-5 chars of its serial ("the
    one ending 2452"). The `?q=<substring>` query param filters the
    result set with a case-insensitive substring match against
    `Drive.serial` — so suffix search, prefix search, and arbitrary
    substring search all work with one input.

    Preserves existing behavior:
      - Reverse-chronological sort (most recent first)
      - 500-row limit (applied to the filtered result set)
      - Only completed runs (completed_at IS NOT NULL)

    URL state via `?q=` so operators can bookmark a filter and
    browser-refresh keeps it active.
    """
    state = get_state()
    query = (request.query_params.get("q") or "").strip()
    with state.session_factory() as session:
        base = (
            session.query(m.TestRun)
            .options(joinedload(m.TestRun.drive))
            .filter(m.TestRun.completed_at.isnot(None))
        )
        if query:
            # Case-insensitive substring match. SQLite's `LIKE` is
            # case-insensitive by default for ASCII (which every drive
            # serial we've ever seen fits within), so a plain ilike
            # equivalent via func.lower would be redundant. Keep it
            # simple: `%<q>%` LIKE against drive_serial.
            base = base.filter(
                m.TestRun.drive_serial.ilike(f"%{query}%")
            )
        runs = (
            base.order_by(m.TestRun.completed_at.desc())
            .limit(500)
            .all()
        )
        # v0.10.3+ host column. Build agent_id → display_name map
        # in one query so rendering doesn't fan out per row. Local
        # runs (host_id IS NULL) render as "local".
        agent_display_by_id = {
            a.id: a.display_name
            for a in session.query(m.Agent).all()
        }
        rows = []
        for r in runs:
            duration = None
            if r.started_at and r.completed_at:
                started = r.started_at if r.started_at.tzinfo else r.started_at.replace(tzinfo=UTC)
                completed = r.completed_at if r.completed_at.tzinfo else r.completed_at.replace(tzinfo=UTC)
                duration = _format_duration(int((completed - started).total_seconds()))
            capacity_tb = round(r.drive.capacity_bytes / 1_000_000_000_000, 2) if r.drive else None
            host_display = (
                agent_display_by_id.get(r.host_id, r.host_id)
                if r.host_id
                else None  # None = local / standalone run
            )
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
                    # v0.10.3+ — which node executed this run.
                    # None = local (this operator / standalone).
                    "host_id": r.host_id,
                    "host_display": host_display,
                }
            )
    # Render the host column only when at least one row has a non-NULL
    # host_id (standalone installs with no fleet history shouldn't see
    # a dead column).
    show_host_column = any(row["host_id"] for row in rows)
    return templates.TemplateResponse(
        request, "history.html",
        {
            "rows": rows, "search_query": query,
            "show_host_column": show_host_column,
        },
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    state = get_state()
    saved = request.query_params.get("saved")
    restart = request.query_params.get("restart")
    hostname_error = request.query_params.get("hostname_error")
    install_error = request.query_params.get("install_error")
    install_started = request.query_params.get("install_started") == "1"
    # v0.11.4+ how many agents got the fleet-wide UpdateCmd push.
    # 0 = operator-only (standalone install OR no agents online).
    try:
        fleet_pushed = int(request.query_params.get("fleet_pushed") or 0)
    except ValueError:
        fleet_pushed = 0
    # v0.11.6+ verified delivery — `fleet_acked` is the count of
    # agents that returned a successful CommandResultMsg before the
    # operator's own update fired. `fleet_failed` is a comma-
    # separated list of agent display names that errored or timed
    # out, surfaced as a warn banner.
    try:
        fleet_acked = int(request.query_params.get("fleet_acked") or 0)
    except ValueError:
        fleet_acked = 0
    fleet_failed_raw = (request.query_params.get("fleet_failed") or "").strip()
    fleet_failed = [s for s in fleet_failed_raw.split(",") if s]
    # v0.6.1+: test-print flow uses `saved=test_print` on success and
    # `test_print_error=<msg>` on failure so the template can render a
    # green "sent to printer" pill or a warn banner with the specific
    # reason (same pattern as install-update's error handling).
    test_print_error = request.query_params.get("test_print_error")
    from driveforge.core import hostname as hostname_mod
    from driveforge.core import telemetry
    from driveforge.core import updates as updates_mod

    # Live-sample chassis telemetry for the Hardware panel. Skipped silently
    # when the underlying capability is False (no BMC / no /dev/ipmi0 perms).
    chassis_power = telemetry.read_chassis_power() if state.capabilities.chassis_power else None
    chassis_temps = telemetry.read_chassis_temperatures() if state.capabilities.chassis_temperature else {}

    cached_update_info = updates_mod.cached()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": state.settings,
            "saved_panel": saved,
            "restart_required": restart == "1",
            "update_info": cached_update_info,
            # v0.6.0+: release-notes-before-Install preview. Rendered
            # server-side from the cached markdown body so the template
            # can drop it straight into a `|safe` block without any
            # client-side markdown parser. Returns None when there's
            # nothing to render (no cache, or body is empty) — template
            # hides the preview card entirely in that case.
            "update_notes_html": updates_mod.render_release_notes_html(cached_update_info),
            "update_command": updates_mod.update_command(),
            "ssh_update_command": updates_mod.ssh_update_command(),
            "current_version": updates_mod.CURRENT_VERSION,
            "capabilities": state.capabilities,
            "chassis_power": chassis_power,
            "chassis_temps": chassis_temps,
            "current_hostname": hostname_mod.current_hostname() or "driveforge",
            "hostname_error": hostname_error,
            "install_error": install_error,
            "install_started": install_started,
            "fleet_pushed": fleet_pushed,
            "fleet_acked": fleet_acked,
            "fleet_failed": fleet_failed,
            "test_print_error": test_print_error,
            # v0.7.0+ Safety-gate UX. Hand the template the live
            # active-drive + recovery counts so it can render the
            # Install Update button as disabled with a clear reason
            # rather than letting the operator click and discover the
            # server-side refusal. The server-side gate in
            # `/settings/install-update` stays as belt-and-suspenders.
            "active_phase_count": len(state.active_phase),
            "recovery_count": len(state.recovery_serials),
        },
    )


@router.post("/settings/hostname")
async def save_hostname(request: Request) -> RedirectResponse:
    """Rename the host: /etc/hostname + hostnamectl + /etc/hosts patch +
    avahi-daemon restart. Hostname is OS-level state, not driveforge.yaml,
    so this does NOT go through `_save_settings_or_ignore`."""
    from driveforge.core import hostname as hostname_mod
    from urllib.parse import quote

    state = get_state()
    form = await request.form()
    raw = (form.get("hostname") or "").strip()
    try:
        hostname_mod.apply_hostname(raw, dev_mode=state.settings.dev_mode)
    except hostname_mod.HostnameError as exc:
        return RedirectResponse(
            url=f"/settings?hostname_error={quote(str(exc))}",
            status_code=303,
        )
    return RedirectResponse(url="/settings?saved=hostname", status_code=303)


@router.post("/settings/install-update")
async def install_update(request: Request) -> RedirectResponse:
    """One-click in-app update — fires `systemctl start
    driveforge-update.service` (polkit-authorized as of v0.6.0; no
    sudo), which git-pulls + reruns install.sh + restarts the daemon.

    Refusal preconditions checked here (the underlying primitive in
    `updates.trigger_in_app_update()` doesn't enforce them):

      - No drive currently in `state.active_phase`. The daemon restart
        at the end of install.sh would orphan any in-flight pipeline,
        and we lose the test-run state. Operator must wait or abort
        the active drives first.
      - No drive currently in `state.recovery_serials`. Same reason —
        recovery dispatches a fresh pipeline that would also be killed.

    Surfaces refusals via `?install_error=...` so the Settings page
    can render a clear banner instead of silently no-op'ing.
    """
    from urllib.parse import quote
    from driveforge.core import updates as updates_mod

    state = get_state()
    if state.active_phase:
        active_n = len(state.active_phase)
        return RedirectResponse(
            url=(
                "/settings?install_error="
                + quote(
                    f"{active_n} drive(s) currently under test — wait for them to "
                    f"finish or abort them, then try again. Updating now would "
                    f"interrupt their pipelines."
                )
            ),
            status_code=303,
        )
    if state.recovery_serials:
        return RedirectResponse(
            url=(
                "/settings?install_error="
                + quote(
                    "Drive recovery in progress — wait for it to complete before updating."
                )
            ),
            status_code=303,
        )

    # v0.11.4+ — when this daemon is the fleet operator, push the
    # update to every connected agent FIRST, then update ourselves.
    # JT's design intent: one button updates the entire fleet, no
    # version skew between operator + agents.
    #
    # v0.11.6+ verified delivery: pre-v0.11.6 this was "fire and
    # hope" — queue UpdateCmd, immediately fire operator's own
    # update. The operator's daemon-restart could SIGTERM the
    # WebSocket sender_loop before queued bytes flushed, silently
    # dropping the broadcast (race we hit during the v0.11.4
    # walkthrough). Now we:
    #
    #   1. Queue UpdateCmd on each online agent
    #   2. Wait briefly for the sender_loop to drain the queues
    #      (asyncio.sleep yields control to the event loop;
    #      sender's `await ws.send_text` runs)
    #   3. Wait up to ACK_TIMEOUT for each agent's CommandResultMsg
    #      to arrive in `recent_command_results`
    #   4. Surface failures (timeouts + ack errors) to the URL so
    #      the post-redirect banner can warn about partial-fleet
    #      updates
    #   5. Then trigger the operator's own update
    #
    # Trade-off: adds ~ACK_TIMEOUT seconds to the install-update
    # POST latency in the worst case. Worth it — silent fleet
    # version skew is the bigger sin.
    fleet_pushed: list[str] = []      # cmd_ids successfully queued
    fleet_acked: list[str] = []       # cmd_ids confirmed delivered
    fleet_failed: list[str] = []      # agent display names that failed
    if state.settings.fleet.role == "operator":
        import asyncio as _asyncio
        from driveforge.core import fleet_protocol as proto
        from driveforge.daemon import fleet_server

        cmd_to_agent: dict[str, str] = {}  # cmd_id → agent_id

        for agent_id in list(state.remote_agents.keys()):
            ra = state.remote_agents[agent_id]
            if ra.outbound_queue is None:
                continue  # agent offline; skip
            cmd = proto.UpdateCmd(cmd_id=_new_cmd_id())
            try:
                await fleet_server.send_command_to_agent(state, agent_id, cmd)
                fleet_pushed.append(cmd.cmd_id)
                cmd_to_agent[cmd.cmd_id] = agent_id
            except fleet_server.CommandDispatchError:
                logger.warning(
                    "fleet update: failed to push UpdateCmd to %s", agent_id,
                )
                fleet_failed.append(ra.display_name)

        # Wait briefly for the sender_loops to drain. Empty 250ms
        # tick gives the asyncio scheduler a chance to actually
        # run each session's _sender_loop coroutine.
        if fleet_pushed:
            await _asyncio.sleep(0.25)

        # Now poll for CommandResultMsg arrivals up to ACK_TIMEOUT.
        # Operators with N agents on a busy LAN typically see acks
        # back well under 1 second; 5s is a generous worst-case cap.
        ACK_TIMEOUT = 5.0
        POLL_INTERVAL = 0.1
        deadline = _asyncio.get_event_loop().time() + ACK_TIMEOUT
        pending = set(fleet_pushed)
        while pending and _asyncio.get_event_loop().time() < deadline:
            for agent_id in list(state.remote_agents.keys()):
                ra = state.remote_agents.get(agent_id)
                if ra is None:
                    continue
                for result in ra.recent_command_results:
                    if result.cmd_id in pending:
                        pending.discard(result.cmd_id)
                        if result.success:
                            fleet_acked.append(result.cmd_id)
                        else:
                            fleet_failed.append(
                                f"{ra.display_name} ({result.detail or 'no detail'})"
                            )
            if pending:
                await _asyncio.sleep(POLL_INTERVAL)

        # Anything still pending = timed out before ack. Note as
        # such (agent might still be processing the update — the
        # actual update could complete after the operator restarts).
        for cmd_id in pending:
            agent_id = cmd_to_agent.get(cmd_id, "?")
            ra = state.remote_agents.get(agent_id)
            display = ra.display_name if ra else agent_id
            fleet_failed.append(f"{display} (no ack within {ACK_TIMEOUT:.0f}s)")

    ok, message = updates_mod.trigger_in_app_update()
    if not ok:
        return RedirectResponse(
            url="/settings?install_error=" + quote(message),
            status_code=303,
        )

    # URL params surface fleet-update outcome to the post-redirect
    # banner. fleet_pushed = N agents the broadcast was queued for;
    # fleet_acked = N agents that confirmed receipt; fleet_failed
    # = comma-separated list of display names that errored or timed
    # out (those agents need manual intervention).
    parts = [f"install_started=1"]
    if fleet_pushed:
        parts.append(f"fleet_pushed={len(fleet_pushed)}")
        parts.append(f"fleet_acked={len(fleet_acked)}")
    if fleet_failed:
        parts.append(f"fleet_failed={quote(','.join(fleet_failed))}")
    return RedirectResponse(
        url="/settings?" + "&".join(parts), status_code=303,
    )


@router.post("/settings/restart-udev")
async def restart_udev(request: Request) -> RedirectResponse:
    """v0.6.9+: trigger a polkit-authorized systemd-udevd restart via
    `driveforge-udev-restart.service`. Wired to the "Restart udev"
    button that appears in base.html when
    `udev_health.needs_operator_action` is True.

    Unlike `install-update`, this one is SAFE to fire with active
    pipelines. Restarting systemd-udevd doesn't touch already-mounted
    filesystems or in-flight drive subprocesses — those are owned by
    the daemon, not by udev workers. The restart just gives udev a
    fresh worker pool that can process NEW hotplug events. Operators
    can click the button whenever they notice drives not enumerating.

    After the restart (fires async via `systemctl start --no-block`)
    the daemon's `_udev_health_loop` will re-probe on its next tick,
    catch the recovery, and the banner will disappear. No manual
    state clearing needed.

    Source page comes back via Referer so the redirect lands the
    operator where they clicked from (dashboard, Settings, etc).
    """
    from urllib.parse import quote
    from driveforge.core import udev_health as udev_health_mod

    ok, message = udev_health_mod.trigger_udev_restart()
    back = request.headers.get("Referer", "/")
    # Avoid referer-based open-redirect: only redirect to same-origin
    # paths. Anything external → home.
    if not back.startswith("/"):
        try:
            from urllib.parse import urlparse
            parsed = urlparse(back)
            host = request.url.hostname or ""
            back = parsed.path or "/" if parsed.hostname in (host, "", None) else "/"
        except Exception:  # noqa: BLE001
            back = "/"

    sep = "&" if "?" in back else "?"
    if ok:
        return RedirectResponse(url=f"{back}{sep}udev_restart=ok", status_code=303)
    return RedirectResponse(
        url=f"{back}{sep}udev_restart_error=" + quote(message),
        status_code=303,
    )


@router.get("/_partials/update-log", response_class=HTMLResponse)
def update_log_partial(request: Request) -> HTMLResponse:
    """HTMX-polled live tail of /var/log/driveforge-update.log + the
    unambiguous update-state classification (v0.5.0+).

    Combines two signals:
      - `systemctl is-active driveforge-update.service` — tells us
        whether the systemd unit is currently running
      - Explicit markers in the log (`=== DRIVEFORGE_UPDATE_START ===`
        / `_SUCCESS` / `_FAILED: <reason>`) emitted by the update
        script itself at known transition points

    Neither signal alone is sufficient: the unit can be `inactive`
    because it succeeded OR because it died silently without cleanup;
    the log can lack a SUCCESS marker because the update really
    failed OR because it's still in progress. Combined, the
    classification is unambiguous.

    HTMX only polls while state is `running`. On `succeeded` or
    `failed` the partial renders without `hx-trigger`, which stops
    the polling loop — at which point the page-level JS watches
    /api/health for the daemon coming back under the new version
    (succeeded) or gives the operator a clear failure banner with
    the reason + log tail (failed).
    """
    from driveforge.core import updates as updates_mod

    log = updates_mod.update_log_tail(max_lines=400)
    service_state = updates_mod.update_service_state()
    update_state, failure_detail = updates_mod.classify_update_state(
        log, service_state,
    )
    is_running = update_state == updates_mod.UpdateState.RUNNING
    return templates.TemplateResponse(
        request,
        "_update_log.html",
        {
            "log": log,
            "service_state": service_state,
            "update_state": update_state,
            "failure_detail": failure_detail,
            "is_running": is_running,
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

    # Helper to read an optional int field. Blank or missing → leave
    # the stored value alone (form submission doesn't carry the value
    # we want to preserve); empty-string on a nullable field → None.
    def _int_or_none(key: str) -> int | None:
        v = (form.get(key) or "").strip()
        return int(v) if v else None

    def _int_keep(key: str) -> None:
        """Parse a required int from `form[key]`; leave g unchanged on
        blank/missing. Avoids smashing legitimate values with 0 when the
        browser omits a field."""
        v = form.get(key)
        if v is not None and str(v).strip() != "":
            setattr(g, key, int(v))

    # Existing (pre-v0.8.0) fields
    for k in ("grade_a_reallocated_max", "grade_b_reallocated_max", "grade_c_reallocated_max"):
        _int_keep(k)
    g.fail_on_pending_sectors = form.get("fail_on_pending_sectors") == "on"
    g.fail_on_offline_uncorrectable = form.get("fail_on_offline_uncorrectable") == "on"
    g.thermal_excursion_c = _int_or_none("thermal_excursion_c")

    # v0.8.0+ age ceilings
    g.age_ceiling_enabled = form.get("age_ceiling_enabled") == "on"
    _int_keep("poh_a_ceiling_hours")
    _int_keep("poh_b_ceiling_hours")
    g.poh_fail_hours = _int_or_none("poh_fail_hours")

    # v0.8.0+ workload ceilings + rated-TBW table
    g.workload_ceiling_enabled = form.get("workload_ceiling_enabled") == "on"
    for k in (
        "workload_a_ceiling_pct",
        "workload_b_ceiling_pct",
        "workload_fail_pct",
        "rated_tbw_enterprise_hdd",
        "rated_tbw_enterprise_ssd",
        "rated_tbw_consumer_hdd",
        "rated_tbw_consumer_ssd",
    ):
        _int_keep(k)

    # v0.8.0+ SSD wear ceilings
    g.ssd_wear_ceiling_enabled = form.get("ssd_wear_ceiling_enabled") == "on"
    for k in ("ssd_wear_a_ceiling_pct", "ssd_wear_b_ceiling_pct", "ssd_wear_fail_pct"):
        _int_keep(k)
    g.fail_on_low_nvme_spare = form.get("fail_on_low_nvme_spare") == "on"

    # v0.8.0+ error-class rules
    g.error_rules_enabled = form.get("error_rules_enabled") == "on"
    g.fail_on_end_to_end_error = form.get("fail_on_end_to_end_error") == "on"
    g.fail_on_nvme_critical_warning = form.get("fail_on_nvme_critical_warning") == "on"
    g.cap_c_on_nvme_media_errors = form.get("cap_c_on_nvme_media_errors") == "on"
    _int_keep("command_timeout_b_ceiling")
    g.cap_c_on_past_self_test_failure = form.get("cap_c_on_past_self_test_failure") == "on"

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
    # v0.6.4+: checkbox values are absent from the form submission when
    # unchecked, present when checked. Presence-check = enabled.
    p.auto_print = "auto_print" in form

    # v0.7.0+: network-printer config. Always parse both fields so
    # switching connection=usb → network → usb doesn't lose the
    # host/port the operator already typed. Only synthesize
    # backend_identifier from them when connection=network; for USB
    # we leave backend_identifier as-is (auto-discovered at print
    # time when empty).
    p.network_host = (form.get("network_host") or "").strip() or None
    try:
        port_raw = (form.get("network_port") or "").strip()
        p.network_port = int(port_raw) if port_raw else 9100
    except ValueError:
        # Operator typed non-numeric — fall back to default rather
        # than hard-failing the save. UI validation catches the
        # typo on the next render.
        p.network_port = 9100
    if p.connection == "network" and p.network_host:
        p.backend_identifier = f"tcp://{p.network_host}:{p.network_port}"
    elif p.connection != "network":
        # Switching back to USB: clear any stale tcp:// identifier
        # so pyusb's auto-discover path (in core/printer.py:print_label)
        # fires correctly. USB operators who manually filled in a
        # usb://VID:PID identifier aren't affected because we only
        # clear tcp:// values.
        if p.backend_identifier and p.backend_identifier.startswith("tcp://"):
            p.backend_identifier = None

    await _save_settings_or_ignore(request)
    return RedirectResponse(url="/settings?saved=printer", status_code=303)


@router.post("/settings/test-print")
async def test_print(request: Request) -> RedirectResponse:
    """Fire a single sentinel label to the configured printer. v0.6.1+.

    Purpose: confirm the printer is wired up and the backend can
    dispatch raster bytes, without having to wait for a completed
    drive run. Also the mechanism for v1.0's Brother QL hardware-test
    validation gate. Refuses cleanly when no model is saved so the
    button can't be hit before the operator has saved the printer
    panel (though the template hides it in that case too).

    Redirects back to /settings with either ``?saved=test_print`` on
    success (green pill) or ``?test_print_error=<msg>`` on failure
    (warn banner with the specific error — unknown model, no USB
    printer detected, wrong label roll, etc.).
    """
    from urllib.parse import quote
    from driveforge.core import printer as printer_mod

    state = get_state()
    pc = state.settings.printer
    if not pc.model:
        return RedirectResponse(
            url="/settings?test_print_error=" + quote("no printer configured — pick a model + connection and Save first"),
            status_code=303,
        )
    backend = _BROTHER_QL_BACKENDS.get(pc.connection, "file")
    try:
        img = printer_mod.render_test_label(roll=pc.label_roll or "DK-1209")
    except Exception as exc:  # noqa: BLE001
        logger.exception("test-print render failed")
        return RedirectResponse(
            url="/settings?test_print_error=" + quote(f"render failed: {exc}"),
            status_code=303,
        )
    ok, msg = printer_mod.print_label(
        img,
        model=pc.model,
        backend=backend,
        identifier=pc.backend_identifier,
        roll=pc.label_roll,
    )
    if not ok:
        return RedirectResponse(
            url="/settings?test_print_error=" + quote(msg),
            status_code=303,
        )
    return RedirectResponse(url="/settings?saved=test_print", status_code=303)


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
    # v0.5.5+ \u2014 quick-pass fail action + telemetry interval.
    action = form.get("quick_pass_fail_action")
    if action in ("badge_only", "prompt", "auto_promote"):
        d.quick_pass_fail_action = action
    interval_v = form.get("telemetry_sample_interval_s")
    if interval_v:
        try:
            interval_i = int(interval_v)
        except ValueError:
            interval_i = d.telemetry_sample_interval_s
        # Bounds match the input's min/max; defensive clamp in case
        # someone POSTs outside the form UI.
        d.telemetry_sample_interval_s = max(5, min(600, interval_i))
    restart_needed = old_host != d.host or old_port != d.port
    await _save_settings_or_ignore(request)
    suffix = "&restart=1" if restart_needed else ""
    return RedirectResponse(url=f"/settings?saved=daemon{suffix}", status_code=303)


# ---------------------------------------------------------------- fleet
#
# v0.10.0 fleet management UI. Lives under /settings/agents. Three
# handlers:
#   GET  /settings/agents              — list enrolled agents + token form
#   POST /settings/agents/new-token    — mint a fresh one-shot token, render
#                                        it to the operator once
#   POST /settings/agents/<id>/revoke  — stamp revoked_at on an agent row
# Role-gate is rendered in the template itself so standalone + agent
# operators still get a friendly "fleet mode not enabled here" page
# instead of a 404, which makes the Settings layout predictable.


@router.get("/settings/agents", response_class=HTMLResponse)
def agents_page(request: Request) -> HTMLResponse:
    state = get_state()
    new_token = request.query_params.get("new_token")
    rotated = request.query_params.get("rotated")
    agents: list[m.Agent] = []
    # v0.11.0+ — discovered candidates with filter for ignored ones.
    discovered: list = []
    # v0.11.0+ — flash-back query params for enroll success/failure.
    enroll_error = request.query_params.get("enroll_error")
    enrolled_host = request.query_params.get("enrolled")
    # v0.10.4+: live status per agent_id for the Agents page.
    #   connected: bool — is there an active WS session right now
    #   online: bool — has the agent sent a frame in the is_online
    #                   window (2 min default)
    #   last_seen_monotonic: float | None — operator-local cache
    #   drives: int — count of drives the agent reported on last snapshot
    live_status: dict[str, dict] = {}
    refusals: list[dict] = []
    if state.settings.fleet.role == "operator":
        from driveforge.core import fleet as fleet_mod
        import time as _time
        with state.session_factory() as session:
            agents = fleet_mod.list_agents(session)
        now_m = _time.monotonic()
        for aid, ra in state.remote_agents.items():
            live_status[aid] = {
                "connected": ra.ws is not None,
                "online": ra.is_online(now_m),
                "drives": len(ra.drives),
                "agent_version": ra.agent_version,
                "protocol_version": ra.protocol_version,
            }
        # Copy the refusal buffer (newest first) — don't drain it;
        # operator may refresh the page to recheck.
        refusals = list(reversed(state.fleet_refusals))
        # v0.11.0+ — candidate list, excluding ignored rows. Newest
        # first so the operator sees freshly-booted boxes at the top.
        # v0.11.3+ — also exclude candidates whose hostname matches
        # an already-enrolled (non-revoked) agent. Pre-v0.11.3 a
        # candidate that finished adoption but whose mDNS broadcast
        # was still cached on the operator (or who was misconfigured
        # and never restarted out of candidate mode, like the JT-walk-
        # through R720 bug) would appear in BOTH the Discovered AND
        # Enrolled tables, inviting double-enrollment which would
        # mint a second agent row and orphan the first credential.
        # Match by hostname since that's what's stable across the
        # candidate→agent role flip; install_id correlation is
        # available too but hostname is more obvious to operators
        # eyeballing the table.
        enrolled_hostnames = {
            a.hostname for a in agents
            if a.revoked_at is None and a.hostname
        }
        discovered = sorted(
            (
                c for c in state.discovered_candidates.values()
                if not c.ignored
                and c.hostname not in enrolled_hostnames
            ),
            key=lambda c: c.last_seen_monotonic,
            reverse=True,
        )
    # Synthesize the operator URL for the token-display command. Prefer
    # the configured integrations.cloudflare_tunnel_hostname if set
    # (public hostname), else fall back to <hostname>.local:<port>.
    # Agents dial this from the same LAN, so .local is usually right.
    from driveforge.core import hostname as hostname_mod
    host = (
        state.settings.integrations.cloudflare_tunnel_hostname
        or f"{hostname_mod.current_hostname() or 'driveforge'}.local"
    )
    scheme = "https" if state.settings.integrations.cloudflare_tunnel_hostname else "http"
    port_suffix = "" if scheme == "https" else f":{state.settings.daemon.port}"
    operator_url = f"{scheme}://{host}{port_suffix}"
    return templates.TemplateResponse(
        request,
        "settings_agents.html",
        {
            "settings": state.settings,
            "agents": agents,
            "new_token": new_token,
            "rotated": rotated,
            "operator_url": operator_url,
            "token_ttl_minutes": state.settings.fleet.enrollment_token_ttl_seconds // 60,
            # v0.10.4+ live status map + refusals buffer
            "live_status": live_status,
            "refusals": refusals,
            # v0.11.0+ Discovered panel
            "discovered": discovered,
            "enroll_error": enroll_error,
            "enrolled_host": enrolled_host,
        },
    )


@router.post("/settings/fleet-role")
async def save_fleet_role(request: Request) -> RedirectResponse:
    """v0.10.5+ — flip the daemon's fleet role between standalone and
    operator from the Settings UI.

    Agent mode is NOT settable here. An agent is born by consuming an
    enrollment token from an operator via
    `sudo driveforge fleet join <operator_url> <token>` — the CLI is
    what writes the role AND the long-lived credential to disk. A
    web-UI path would need somewhere to paste the token first, which
    duplicates the CLI and muddies the security model (the token
    briefly lives in a browser-cached URL).

    Operators detaching an agent back to standalone should use
    `sudo driveforge fleet leave` on the agent's console.

    Flipping standalone ↔ operator requires a daemon restart because
    the lifespan hooks (`fleet_client` task + mounted routes'
    role-gate behavior) are evaluated once at boot. Restart banner
    renders on the Settings page after save so the operator knows to
    apply the change.
    """
    state = get_state()
    form = await request.form()
    new_role = (form.get("role") or "").strip()
    if new_role not in ("standalone", "operator"):
        return RedirectResponse(
            url="/settings?saved=fleet_role_invalid",
            status_code=303,
        )
    if state.settings.fleet.role == new_role:
        # No-op click; don't advertise a restart that isn't needed.
        return RedirectResponse(url="/settings?saved=fleet_role", status_code=303)
    state.settings.fleet.role = new_role
    # When flipping OUT of agent mode (shouldn't happen via this form
    # since we don't accept agent as an option, but defensive),
    # clear agent-only config so a future flip to agent doesn't
    # inherit stale operator_url.
    if new_role != "agent":
        # Intentionally leave operator_url / api_token_path as-is so a
        # round-trip standalone → operator → standalone doesn't erase
        # history. Only wipe on explicit `driveforge fleet leave`.
        pass
    await _save_settings_or_ignore(request)
    # v0.11.2+ — auto-restart so the new role's lifespan tasks
    # (operator discovery, candidate mDNS publish, fleet client)
    # actually spawn. Pre-v0.11.2 the user had to SSH in or click
    # Install Update; silent failure mode where nothing seemed wrong
    # but fleet features didn't work.
    from driveforge.core import self_restart
    self_restart.schedule_self_restart(
        reason=f"settings: fleet role → {new_role}",
    )
    return RedirectResponse(
        url="/settings?saved=fleet_role&restart=1", status_code=303,
    )


@router.post("/settings/agents/new-token")
def agents_new_token(request: Request) -> RedirectResponse:
    """Generate a one-shot enrollment token and redirect back to the
    Agents page with the raw token in the query string so the template
    can display it exactly once.

    The token is presented in the URL — not ideal from a
    browser-history-leak standpoint, but:
      (a) the token is one-shot, so a leaked-history copy is useless
          once the agent has consumed it
      (b) the token TTL is 15 minutes by default
      (c) the alternative (session storage) adds infra for marginal
          benefit at homelab scale.
    Operator is expected to consume the token immediately on the agent
    console."""
    from urllib.parse import quote
    state = get_state()
    if state.settings.fleet.role != "operator":
        raise HTTPException(status_code=400, detail="fleet role is not operator")
    from driveforge.core import fleet as fleet_mod
    with state.session_factory() as session:
        issue = fleet_mod.issue_enrollment_token(
            session,
            ttl_seconds=state.settings.fleet.enrollment_token_ttl_seconds,
        )
    return RedirectResponse(
        url=f"/settings/agents?new_token={quote(issue.raw_token)}",
        status_code=303,
    )


@router.post("/settings/agents/discovered/{install_id}/enroll")
async def agents_enroll_discovered(install_id: str, request: Request) -> RedirectResponse:
    """v0.11.0+ — one-click Enroll for a candidate the operator sees
    on Settings → Agents → Discovered.

    Flow:
      1. Look up the DiscoveredCandidate by install_id; 404 if unknown.
      2. Mint a fresh Agent row + long-lived token (same primitives
         as the v0.10.0 enrollment path, but server-side — no
         user-visible token).
      3. POST /api/fleet/adopt on the candidate with the full
         package (operator_url + agent_token + display_name +
         install_id).
      4. Candidate writes the token, flips role, restarts. Operator
         sees it as a new online agent on its WebSocket within
         ~10 s.

    No user-pasted token anywhere. No shared secret on the wire that
    the operator didn't generate seconds earlier.
    """
    from urllib.parse import quote
    import httpx
    from driveforge.core import fleet as fleet_mod

    state = get_state()
    if state.settings.fleet.role != "operator":
        raise HTTPException(status_code=400, detail="fleet role is not operator")

    candidate = state.discovered_candidates.get(install_id)
    if candidate is None:
        return RedirectResponse(
            url=f"/settings/agents?enroll_error={quote('candidate no longer on network')}",
            status_code=303,
        )

    # Resolve the candidate's URL. Prefer the IP we saw in avahi over
    # the hostname — operators on a weird DNS setup might not resolve
    # .local reliably, but mDNS gave us a working IP.
    candidate_url = f"http://{candidate.address}:{candidate.port}"

    # Mint a fresh long-lived agent token. This is the same two-step
    # dance the v0.10.0 CLI path does (issue enrollment token, then
    # consume it) — we just do both sides server-side so the token
    # never leaves the operator machine before it hits the candidate.
    with state.session_factory() as session:
        issue = fleet_mod.issue_enrollment_token(session, ttl_seconds=60)
    with state.session_factory() as session:
        result = fleet_mod.consume_enrollment_token(
            session,
            composite_token=issue.raw_token,
            display_name=candidate.hostname,
            hostname=candidate.hostname,
            version=candidate.version,
        )
    agent_token = result.api_token

    # Compose the operator URL that the candidate will dial into.
    # v0.11.4+ — store the operator's mDNS hostname (`.local`) by
    # default. Reasoning evolved across the release series:
    #
    #   v0.11.0 stored .local — failed because libnss-mdns wasn't
    #     installed by install.sh, so agents couldn't resolve it.
    #   v0.11.3 worked around by storing the IP — fixed the
    #     resolution problem but introduced a worse one: an IP
    #     becomes stale the moment the operator's DHCP lease
    #     renews to a different address.
    #   v0.11.4 returns to .local because (a) v0.11.3 also fixed
    #     install.sh to ship libnss-mdns, so resolution works
    #     reliably, and (b) mDNS dynamically re-resolves on every
    #     reconnect, so DHCP changes are transparent. This is the
    #     "survives a router reboot" behavior the homelab needs.
    #
    # Cloudflare tunnel hostname still wins when configured —
    # operator explicitly chose a public-routable name + we don't
    # want mDNS pointing at a LAN IP for a fleet that's also
    # tunneled.
    from driveforge.core import hostname as hostname_mod
    if state.settings.integrations.cloudflare_tunnel_hostname:
        operator_url = f"https://{state.settings.integrations.cloudflare_tunnel_hostname}"
    else:
        op_host = hostname_mod.current_hostname() or "driveforge"
        operator_url = f"http://{op_host}.local:{state.settings.daemon.port}"

    # POST the adoption package to the candidate. Short timeout
    # because the candidate's adoption handler is designed to reply
    # fast and restart; if we hang we should back off rather than
    # block the operator's Settings UI.
    logger.info(
        "fleet: adopting candidate install_id=%s host=%s ip=%s",
        install_id, candidate.hostname, candidate.address,
    )
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{candidate_url}/api/fleet/adopt",
                json={
                    "operator_url": operator_url,
                    "agent_token": agent_token,
                    "display_name": candidate.hostname,
                    "install_id": install_id,
                },
            )
    except httpx.RequestError as exc:
        logger.warning("fleet: adoption POST failed: %s", exc)
        # Roll back the Agent row so a retry doesn't create a
        # duplicate. The token we minted is never used by anyone.
        with state.session_factory() as session:
            from driveforge.db import models as m
            row = session.get(m.Agent, result.agent_id)
            if row is not None:
                session.delete(row)
                session.commit()
        return RedirectResponse(
            url=f"/settings/agents?enroll_error={quote(f'candidate unreachable: {exc}')}",
            status_code=303,
        )
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:  # noqa: BLE001
            detail = resp.text
        with state.session_factory() as session:
            from driveforge.db import models as m
            row = session.get(m.Agent, result.agent_id)
            if row is not None:
                session.delete(row)
                session.commit()
        return RedirectResponse(
            url=f"/settings/agents?enroll_error={quote(f'candidate rejected: {detail}')}",
            status_code=303,
        )

    # Success — remove from discovered cache so the Enroll button
    # doesn't offer the same box again while it's restarting.
    state.discovered_candidates.pop(install_id, None)
    return RedirectResponse(
        url=f"/settings/agents?enrolled={quote(candidate.hostname)}",
        status_code=303,
    )


@router.post("/settings/agents/discovered/{install_id}/ignore")
def agents_ignore_discovered(install_id: str) -> RedirectResponse:
    """Hide a candidate from the Discovered panel without enrolling.
    Useful when an operator sees a neighbor's DriveForge on a shared
    VLAN or an old candidate that hasn't cleaned up its mDNS entry."""
    state = get_state()
    cand = state.discovered_candidates.get(install_id)
    if cand is not None:
        cand.ignored = True
    return RedirectResponse(url="/settings/agents", status_code=303)


@router.post("/settings/agents/{agent_id}/revoke")
async def agents_revoke(agent_id: str) -> RedirectResponse:
    """v0.10.0+ revoke the agent; v0.10.4+ also kicks the active
    WebSocket session so the operator sees the effect immediately
    (pre-v0.10.4 the existing socket kept delivering snapshots until
    the agent naturally disconnected)."""
    state = get_state()
    if state.settings.fleet.role != "operator":
        raise HTTPException(status_code=400, detail="fleet role is not operator")
    from driveforge.core import fleet as fleet_mod
    from driveforge.daemon import fleet_server
    with state.session_factory() as session:
        fleet_mod.revoke_agent(session, agent_id)
    # v0.10.4+ — kick any active session. `kick_agent_session`
    # returns False cleanly if the agent wasn't connected.
    await fleet_server.kick_agent_session(state, agent_id, reason="revoked by operator")
    return RedirectResponse(url="/settings/agents", status_code=303)


@router.post("/settings/agents/{agent_id}/rotate")
async def agents_rotate(agent_id: str) -> RedirectResponse:
    """v0.10.4+ Rotate an agent's credential.

    One click: revoke the existing agent record + mint a fresh
    enrollment token. The operator runs the new `driveforge fleet
    join` command on the agent console, which creates a BRAND NEW
    agent row (new agent_id) — the old row stays in the DB with
    `revoked_at` set so historical drive/run attribution isn't
    broken.

    The operator should use this when:
      - A credential is suspected to be leaked
      - Regular-cadence rotation per security policy
      - An agent was reinstalled + needs a fresh identity

    NOTE: this does NOT preserve the agent_id. The agent's host_id
    history-page filter will split between "old credential runs"
    and "new credential runs." Operators who need continuity should
    rename the new display_name to match the old one at enrollment
    time; the Agents page will surface two entries (one revoked,
    one active) for the same human-facing name.
    """
    from urllib.parse import quote
    state = get_state()
    if state.settings.fleet.role != "operator":
        raise HTTPException(status_code=400, detail="fleet role is not operator")
    from driveforge.core import fleet as fleet_mod
    from driveforge.daemon import fleet_server
    with state.session_factory() as session:
        fleet_mod.revoke_agent(session, agent_id)
        issue = fleet_mod.issue_enrollment_token(
            session,
            ttl_seconds=state.settings.fleet.enrollment_token_ttl_seconds,
        )
    await fleet_server.kick_agent_session(state, agent_id, reason="credential rotated")
    return RedirectResponse(
        url=f"/settings/agents?new_token={quote(issue.raw_token)}&rotated={agent_id}",
        status_code=303,
    )


@router.post("/settings/wizard-replay")
async def replay_wizard(request: Request) -> RedirectResponse:
    state = get_state()
    state.settings.setup_completed = False
    await _save_settings_or_ignore(request)
    return RedirectResponse(url="/setup/1", status_code=303)


@router.post("/settings/clear-legacy-fails")
async def clear_legacy_fails(request: Request) -> Response:
    """v0.5.1 migration endpoint: delete TestRun rows with the legacy
    pre-v0.5.1 `grade="fail"` vocabulary.

    Before v0.5.1, `grade="fail"` conflated two distinct outcomes:
      - real drive-fail (grading rules determined the drive is bad)
      - pipeline-error (daemon broke mid-run, drive's actual state unknown)

    v0.5.1 splits these into `grade="F"` and `grade="error"`
    respectively. Legacy rows can't be retroactively classified, so
    this endpoint purges them — operator retests affected drives to
    get real-vocabulary verdicts.

    Returns JSON `{"deleted": N}`. Logged at WARNING level since it's
    destructive. Not authenticated (homelab assumption, same as every
    other action endpoint).

    The endpoint also deletes cascading SmartSnapshot and
    TelemetrySample rows via the FK relationships — dangling
    telemetry from legacy runs goes away cleanly.
    """
    import json
    state = get_state()
    with state.session_factory() as session:
        legacy_count = (
            session.query(m.TestRun)
            .filter(m.TestRun.grade == "fail")
            .count()
        )
        if legacy_count == 0:
            return Response(
                content=json.dumps({"deleted": 0, "message": "no legacy-fail rows present"}),
                media_type="application/json",
            )
        session.query(m.TestRun).filter(m.TestRun.grade == "fail").delete(
            synchronize_session=False,
        )
        session.commit()
    logger.warning(
        "v0.5.1 migration: deleted %d legacy grade='fail' TestRun row(s) "
        "(and their cascaded telemetry/snapshots) per operator request",
        legacy_count,
    )
    return Response(
        content=json.dumps({
            "deleted": legacy_count,
            "message": (
                f"purged {legacy_count} pre-v0.5.1 fail rows. Affected drives "
                f"will show as 'never tested' until re-run."
            ),
        }),
        media_type="application/json",
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
