"""REST API routes. Mounted under /api by app.py."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from driveforge.core import drive as drive_mod
from driveforge.core import firmware
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
    started_at: str | None
    completed_at: str | None
    power_on_hours: int | None
    reallocated_sectors: int | None
    report_url: str | None


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
        "bay_assignments": state.bay_assignments,
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
            totals = {"A": 0, "B": 0, "C": 0, "fail": 0}
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
    batch_id = await orch.start_batch(drives, source=body.source)
    return BatchOut(
        id=batch_id,
        source=body.source,
        started_at=None,
        completed_at=None,
        totals={"A": 0, "B": 0, "C": 0, "fail": 0},
    )


class FirmwareApproveIn(BaseModel):
    model: str
    transport: str
    version: str
    blob_sha256: str
    signature_verified: bool = False
    notes: str | None = None


@router.get("/firmware/approvals")
def list_firmware_approvals() -> list[dict[str, Any]]:
    state = get_state()
    with state.session_factory() as session:
        rows = session.execute(select(m.FirmwareApproval)).scalars().all()
        return [
            {
                "id": r.id,
                "model": r.model,
                "transport": r.transport,
                "version": r.version,
                "blob_sha256": r.blob_sha256,
                "signature_verified": r.signature_verified,
                "approved_at": r.approved_at.isoformat() if r.approved_at else None,
                "notes": r.notes,
            }
            for r in rows
        ]


@router.post("/firmware/approvals")
def approve_firmware(body: FirmwareApproveIn) -> dict[str, Any]:
    state = get_state()
    with state.session_factory() as session:
        row = m.FirmwareApproval(
            model=body.model,
            transport=body.transport,
            version=body.version,
            blob_sha256=body.blob_sha256,
            signature_verified=body.signature_verified,
            notes=body.notes,
        )
        session.add(row)
        session.commit()
        return {"id": row.id, "status": "approved"}


@router.delete("/firmware/approvals/{approval_id}")
def unapprove_firmware(approval_id: int) -> dict[str, str]:
    state = get_state()
    with state.session_factory() as session:
        row = session.get(m.FirmwareApproval, approval_id)
        if row is None:
            raise HTTPException(status_code=404, detail="approval not found")
        session.delete(row)
        session.commit()
        return {"status": "removed"}


@router.get("/firmware/check/{serial}")
def firmware_check(serial: str) -> dict[str, Any]:
    state = get_state()
    with state.session_factory() as session:
        d = session.get(m.Drive, serial)
        if d is None:
            raise HTTPException(status_code=404, detail="drive not found")
        drive_obj = drive_mod.Drive(
            serial=d.serial,
            model=d.model,
            capacity_bytes=d.capacity_bytes,
            transport=drive_mod.Transport(d.transport),
            device_path=f"/dev/{d.serial}",  # placeholder — real path only known at test time
            firmware_version=d.firmware_version,
        )
    from pathlib import Path

    db_path = Path(__file__).parent.parent / "data" / "firmware_db.yaml"
    check = firmware.check_firmware(drive_obj, db_path=db_path)
    return check.model_dump(mode="json")


def _test_run_to_out(r: m.TestRun) -> TestRunOut:
    return TestRunOut(
        id=r.id,
        drive_serial=r.drive_serial,
        batch_id=r.batch_id,
        bay=r.bay,
        phase=r.phase,
        grade=r.grade,
        started_at=r.started_at.isoformat() if r.started_at else None,
        completed_at=r.completed_at.isoformat() if r.completed_at else None,
        power_on_hours=r.power_on_hours_at_test,
        reallocated_sectors=r.reallocated_sectors,
        report_url=r.report_url,
    )
