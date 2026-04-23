"""REST API routes. Mounted under /api by app.py."""

from __future__ import annotations

from typing import Any

from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from driveforge.core import drive as drive_mod
from driveforge.daemon.state import get_state
from driveforge.db import models as m

router = APIRouter(prefix="/api", tags=["api"])


class DriveOut(BaseModel):
    serial: str
    model: str
    capacity_bytes: int
    capacity_tb: float
    transport: str
    firmware_version: str | None
    first_seen_at: str | None


class TestRunOut(BaseModel):
    id: int
    drive_serial: str
    batch_id: str | None
    bay: int | None
    phase: str
    grade: str | None
    # v0.5.5: triage_result is the verdict for quick-pass runs
    # ("clean" | "watch" | "fail"); grade stays NULL for quick-pass.
    # For full-pipeline runs the grade field carries the verdict and
    # triage_result stays NULL.
    triage_result: str | None = None
    started_at: str | None
    completed_at: str | None
    power_on_hours: int | None
    reallocated_sectors: int | None
    current_pending_sector: int | None = None
    # Start-of-test snapshots (v0.5.5+). NULL on runs that predate the
    # denormalization. `remapped_during_run` is the convenience delta
    # post_reallocated - pre_reallocated, shown to consumers who want
    # the "healing" story without re-computing it client-side.
    pre_reallocated_sectors: int | None = None
    pre_current_pending_sector: int | None = None
    remapped_during_run: int | None = None
    # Throughput stats (v0.5.6+). NULL for quick-pass, legacy, and
    # diskstats-failed runs. per_pass_means is ordered by pass index
    # (length = number of passes completed; typically 8 for a clean run).
    throughput_mean_mbps: float | None = None
    throughput_p5_mbps: float | None = None
    throughput_p95_mbps: float | None = None
    throughput_pass_means: list[float] | None = None
    report_url: str | None
    quick_mode: bool = False
    error_message: str | None = None


class BatchOut(BaseModel):
    id: str
    source: str | None
    started_at: str | None
    completed_at: str | None
    totals: dict[str, int]


@router.get("/health")
def health() -> dict[str, Any]:
    state = get_state()
    return {
        "status": "ok",
        "dev_mode": state.settings.dev_mode,
        "active_serials": sorted(state.active_phase.keys()),
    }


@router.get("/drives", response_model=list[DriveOut])
def list_drives() -> list[DriveOut]:
    state = get_state()
    with state.session_factory() as session:
        rows = session.query(m.Drive).all()
        return [
            DriveOut(
                serial=d.serial,
                model=d.model,
                capacity_bytes=d.capacity_bytes,
                capacity_tb=round(d.capacity_bytes / 1_000_000_000_000, 2),
                transport=d.transport,
                firmware_version=d.firmware_version,
                first_seen_at=d.first_seen_at.isoformat() if d.first_seen_at else None,
            )
            for d in rows
        ]


@router.get("/drives/discover", response_model=list[DriveOut])
def discover_drives() -> list[DriveOut]:
    """Scan the host right now (uses lsblk fixture in dev)."""
    drives = drive_mod.discover()
    return [
        DriveOut(
            serial=d.serial,
            model=d.model,
            capacity_bytes=d.capacity_bytes,
            capacity_tb=d.capacity_tb,
            transport=d.transport.value,
            firmware_version=d.firmware_version,
            first_seen_at=None,
        )
        for d in drives
    ]


@router.get("/drives/{serial}", response_model=DriveOut)
def get_drive(serial: str) -> DriveOut:
    state = get_state()
    with state.session_factory() as session:
        d = session.get(m.Drive, serial)
        if d is None:
            raise HTTPException(status_code=404, detail="drive not found")
        return DriveOut(
            serial=d.serial,
            model=d.model,
            capacity_bytes=d.capacity_bytes,
            capacity_tb=round(d.capacity_bytes / 1_000_000_000_000, 2),
            transport=d.transport,
            firmware_version=d.firmware_version,
            first_seen_at=d.first_seen_at.isoformat() if d.first_seen_at else None,
        )


@router.get("/drives/{serial}/telemetry")
def drive_telemetry(serial: str, test_run_id: int | None = None, bucket_min: int = 5) -> dict[str, Any]:
    """Return telemetry points for charting.

    If samples span > 1 hour, aggregates into `bucket_min`-minute buckets
    (mean per bucket) to keep payloads small. Otherwise returns raw samples.
    """
    state = get_state()
    with state.session_factory() as session:
        q = session.query(m.TelemetrySample).filter_by(drive_serial=serial)
        if test_run_id is not None:
            q = q.filter_by(test_run_id=test_run_id)
        else:
            latest = (
                session.query(m.TestRun)
                .filter_by(drive_serial=serial)
                .order_by(m.TestRun.started_at.desc())
                .first()
            )
            if latest is None:
                return {"points": [], "bucket_min": 0}
            q = q.filter_by(test_run_id=latest.id)
        samples = q.order_by(m.TelemetrySample.ts.asc()).all()
    if not samples:
        return {"points": [], "bucket_min": 0}
    first = samples[0].ts
    last = samples[-1].ts
    span = (last - first) if last and first else timedelta(0)
    raw = [
        {
            "ts": s.ts.isoformat() if s.ts else None,
            "phase": s.phase,
            "temp": s.drive_temp_c,
            "watts": s.chassis_power_w,
        }
        for s in samples
    ]
    if span.total_seconds() <= 3600:
        return {"points": raw, "bucket_min": 0}
    bucket = timedelta(minutes=bucket_min)
    buckets: dict[datetime, list[dict]] = {}
    for p in raw:
        if p["ts"] is None:
            continue
        t = datetime.fromisoformat(p["ts"])
        key = t - timedelta(
            minutes=t.minute % bucket_min,
            seconds=t.second,
            microseconds=t.microsecond,
        )
        buckets.setdefault(key, []).append(p)
    agg = []
    for key in sorted(buckets):
        group = buckets[key]
        temps = [g["temp"] for g in group if g["temp"] is not None]
        watts = [g["watts"] for g in group if g["watts"] is not None]
        agg.append(
            {
                "ts": key.isoformat(),
                "phase": group[-1]["phase"],
                "temp": sum(temps) / len(temps) if temps else None,
                "watts": sum(watts) / len(watts) if watts else None,
            }
        )
    return {"points": agg, "bucket_min": bucket_min}


@router.get("/drives/{serial}/test_runs", response_model=list[TestRunOut])
def drive_test_runs(serial: str) -> list[TestRunOut]:
    state = get_state()
    with state.session_factory() as session:
        runs = session.query(m.TestRun).filter_by(drive_serial=serial).order_by(m.TestRun.started_at.desc()).all()
        return [_test_run_to_out(r) for r in runs]


@router.get("/batches", response_model=list[BatchOut])
def list_batches() -> list[BatchOut]:
    state = get_state()
    with state.session_factory() as session:
        batches = session.query(m.Batch).order_by(m.Batch.started_at.desc()).all()
        out: list[BatchOut] = []
        for b in batches:
            totals = {"A": 0, "B": 0, "C": 0, "F": 0, "error": 0, "fail": 0}
            for run in b.test_runs:
                if run.grade in totals:
                    totals[run.grade] += 1
            out.append(
                BatchOut(
                    id=b.id,
                    source=b.source,
                    started_at=b.started_at.isoformat() if b.started_at else None,
                    completed_at=b.completed_at.isoformat() if b.completed_at else None,
                    totals=totals,
                )
            )
        return out


@router.get("/batches/{batch_id}/test_runs", response_model=list[TestRunOut])
def batch_test_runs(batch_id: str) -> list[TestRunOut]:
    state = get_state()
    with state.session_factory() as session:
        runs = session.query(m.TestRun).filter_by(batch_id=batch_id).all()
        return [_test_run_to_out(r) for r in runs]


@router.get("/test_runs/{run_id}", response_model=TestRunOut)
def get_test_run(run_id: int) -> TestRunOut:
    state = get_state()
    with state.session_factory() as session:
        r = session.get(m.TestRun, run_id)
        if r is None:
            raise HTTPException(status_code=404, detail="test run not found")
        return _test_run_to_out(r)


class StartBatchIn(BaseModel):
    source: str | None = None
    drive_serials: list[str] | None = None  # None = start with all discovered
    quick: bool = False


@router.post("/batches", response_model=BatchOut)
async def start_batch(body: StartBatchIn, request: Request) -> BatchOut:
    """Start a new batch. With no drive_serials, uses all discovered drives."""
    orch = request.app.state.orchestrator
    drives = drive_mod.discover()
    if body.drive_serials:
        wanted = set(body.drive_serials)
        drives = [d for d in drives if d.serial in wanted]
    if not drives:
        raise HTTPException(status_code=400, detail="no drives to start")
    batch_id = await orch.start_batch(drives, source=body.source, quick=body.quick)
    return BatchOut(
        id=batch_id,
        source=body.source,
        started_at=None,
        completed_at=None,
        totals={"A": 0, "B": 0, "C": 0, "fail": 0},
    )


@router.post("/abort-all")
async def abort_all(request: Request) -> dict[str, Any]:
    orch = request.app.state.orchestrator
    cancelled = await orch.abort_all()
    return {"cancelled": cancelled}


@router.post("/drives/{serial}/abort")
async def abort_drive(serial: str, request: Request) -> dict[str, Any]:
    orch = request.app.state.orchestrator
    # v0.7.0+ abort_drive now returns a structured outcome dict.
    # Surface it raw to API consumers so they can distinguish
    # "not active" from a real abort; preserve the historical 404 on
    # "not active" for clients that pre-dated the structured return.
    outcome = await orch.abort_drive(serial)
    if outcome["status"] == "not_active":
        raise HTTPException(status_code=404, detail="drive not in-flight")
    return {"aborted": serial, "outcome": outcome}


def _test_run_to_out(r: m.TestRun) -> TestRunOut:
    # Compute the healing delta for quick consumption by dashboard / label.
    # Only meaningful when both pre and post snapshots are present; NULL
    # otherwise so clients can tell "no data" from "zero healing."
    remapped = None
    if r.reallocated_sectors is not None and r.pre_reallocated_sectors is not None:
        remapped = r.reallocated_sectors - r.pre_reallocated_sectors
    return TestRunOut(
        id=r.id,
        drive_serial=r.drive_serial,
        batch_id=r.batch_id,
        bay=r.bay,
        phase=r.phase,
        grade=r.grade,
        triage_result=r.triage_result,
        started_at=r.started_at.isoformat() if r.started_at else None,
        completed_at=r.completed_at.isoformat() if r.completed_at else None,
        power_on_hours=r.power_on_hours_at_test,
        reallocated_sectors=r.reallocated_sectors,
        current_pending_sector=r.current_pending_sector,
        pre_reallocated_sectors=r.pre_reallocated_sectors,
        pre_current_pending_sector=r.pre_current_pending_sector,
        remapped_during_run=remapped,
        throughput_mean_mbps=r.throughput_mean_mbps,
        throughput_p5_mbps=r.throughput_p5_mbps,
        throughput_p95_mbps=r.throughput_p95_mbps,
        throughput_pass_means=list(r.throughput_pass_means) if r.throughput_pass_means else None,
        report_url=r.report_url,
        quick_mode=bool(r.quick_mode),
        error_message=r.error_message,
    )


# ---------------------------------------------------------------- fleet
#
# v0.10.0 operator-side enrollment endpoint. The agent-side CLI
# (`driveforge fleet join`) posts here during bootstrap. No auth
# beyond the enrollment token itself — the token IS the auth. Only
# serves when `settings.fleet.role == "operator"`; standalone +
# agent daemons return 404.


class FleetEnrollRequest(BaseModel):
    token: str
    display_name: str
    hostname: str | None = None
    version: str | None = None


class FleetEnrollResponse(BaseModel):
    agent_id: str
    api_token: str
    operator_version: str


@router.get("/fleet/local-status")
def fleet_local_status() -> dict[str, Any]:
    """v0.10.4+ — live fleet status for the local daemon. Called by
    `driveforge fleet status` CLI. Role-aware response:

    - agent → fields from `state.fleet_client.status` (connected,
      last_error, counters)
    - operator → aggregate (agents_total, online, connected, refusals)
    - standalone → just {"role": "standalone"}

    No auth — localhost-only by convention (CLI binds to 127.0.0.1
    default). If this ends up being reachable from the LAN, all it
    reveals is fleet-level counters, no tokens or drive identity.
    """
    state = get_state()
    fcfg = state.settings.fleet
    out: dict[str, Any] = {"role": fcfg.role}
    if fcfg.role == "agent":
        client = getattr(state, "fleet_client", None)
        if client is None:
            out.update({"connected": False, "last_error": "client not running"})
            return out
        s = client.status
        out.update({
            "connected": s.connected,
            "last_error": s.last_error,
            "snapshots_sent": s.snapshots_sent,
            "heartbeats_sent": s.heartbeats_sent,
            "completions_sent": getattr(s, "completions_sent", 0),
            "reconnect_attempts": s.reconnect_attempts,
        })
    elif fcfg.role == "operator":
        import time as _time
        now = _time.monotonic()
        agents_total = len(state.remote_agents)
        online = 0
        connected = 0
        for ra in state.remote_agents.values():
            if ra.is_online(now):
                online += 1
            if ra.ws is not None:
                connected += 1
        out.update({
            "agents_total": agents_total,
            "agents_online": online,
            "agents_connected": connected,
            "recent_refusals": len(state.fleet_refusals),
        })
    return out


@router.post("/fleet/enroll", response_model=FleetEnrollResponse)
def fleet_enroll(req: FleetEnrollRequest) -> FleetEnrollResponse:
    """Consume a one-shot enrollment token, mint a long-lived agent
    token, create the Agent row. Called by the agent during
    `driveforge fleet join`.

    Returns 400 on any token-validation failure (unknown / expired /
    consumed / malformed) with a generic-ish message — the agent-side
    CLI surfaces the HTTP body to the operator who's running the
    enrollment, but we don't want to leak whether a specific token
    existed vs was just expired.
    """
    state = get_state()
    if state.settings.fleet.role != "operator":
        # Standalone + agent roles don't expose the enrollment path.
        raise HTTPException(status_code=404, detail="fleet enrollment not enabled")

    from driveforge.core import fleet as fleet_mod
    from driveforge.version import __version__ as DRIVEFORGE_VERSION

    with state.session_factory() as session:
        try:
            result = fleet_mod.consume_enrollment_token(
                session,
                composite_token=req.token,
                display_name=req.display_name.strip() or "unnamed-agent",
                hostname=req.hostname,
                version=req.version,
            )
        except fleet_mod.EnrollmentError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return FleetEnrollResponse(
        agent_id=result.agent_id,
        api_token=result.api_token,
        operator_version=DRIVEFORGE_VERSION,
    )
