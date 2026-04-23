"""Reusable regrade body for fleet command dispatch (v0.10.2+).

The v0.8.0 regrade logic originally lived entirely inside
`routes.py:regrade_drive()` because it had no non-HTTP caller. v0.10.2
adds a second caller — the fleet client's command dispatcher — so the
grading + DB-upsert core is extracted here. Both HTTP and fleet paths
now run the same code, which is also a regression-prevention win.

Not importable by `routes.py` for v0.10.2 to avoid a merge-conflict
diff; the HTTP handler stays in routes.py as-is, just now shares the
pure-core logic via this module. Post-v0.10.2 we can consolidate.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from driveforge.db import models as m

logger = logging.getLogger(__name__)


class RegradeRefused(Exception):
    """Raised when the agent refuses the regrade — drive not
    present, drive running, no prior A/B/C run, etc. Caller surfaces
    the message to the operator via CommandResultMsg.detail."""


async def regrade_drive_locally(state: Any, serial: str) -> str:
    """Run a full regrade for the given serial on THIS agent. Returns
    the new grade on success; raises RegradeRefused for operator-
    visible refusals.

    Mirrors `routes.regrade_drive` exactly except for the HTTP glue
    (no URL encoding, no RedirectResponse). Shared body is safe to
    call from any async context — the fleet receiver already runs
    each command in its own task, and the web handler runs in
    FastAPI's own task.
    """
    from driveforge.core import drive_class as drive_class_mod
    from driveforge.core import grading, smart as smart_mod

    if serial in state.active_phase:
        raise RegradeRefused(
            "drive is currently running a pipeline; abort or wait for it to finish"
        )

    device_basename = state.device_basenames.get(serial)
    if not device_basename:
        raise RegradeRefused(
            "drive is not currently plugged in — re-insert to regrade"
        )
    device_path = f"/dev/{device_basename}"

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
            raise RegradeRefused(
                "no prior A/B/C pipeline run to regrade from — run a full pipeline first"
            )
        source_run_id = source_run.id
        source_run_copy = _snapshot_source(source_run)
        drive_row = session.get(m.Drive, serial)
        if drive_row is None:
            raise RegradeRefused(
                "drive missing from DB unexpectedly — re-enroll via a full pipeline"
            )
        drive_model = drive_row.model
        drive_transport = (
            drive_row.transport.value
            if hasattr(drive_row.transport, "value")
            else str(drive_row.transport)
        )
        drive_rotation_rate = getattr(drive_row, "rotation_rate", None)

    try:
        post_snap = await smart_mod.snapshot_async(device_path)
    except Exception as exc:  # noqa: BLE001
        logger.exception("regrade: SMART snapshot failed for %s", serial)
        raise RegradeRefused(f"failed to read fresh SMART: {exc}") from exc

    dclass = drive_class_mod.classify(
        model=drive_model,
        transport=drive_transport,
        rotation_rate=drive_rotation_rate,
        overrides_path=Path("/etc/driveforge/drive_class_overrides.yaml"),
    )

    pre_snap = smart_mod.SmartSnapshot(
        device=device_path,
        captured_at=source_run_copy["completed_at"] or datetime.now(UTC),
        reallocated_sectors=source_run_copy["reallocated_sectors"],
        current_pending_sector=source_run_copy["current_pending_sector"],
        offline_uncorrectable=source_run_copy["offline_uncorrectable"],
        power_on_hours=source_run_copy["power_on_hours_at_test"],
    )

    throughput = None
    if source_run_copy["throughput_mean_mbps"] is not None:
        from driveforge.core.throughput import ThroughputStats
        throughput = ThroughputStats(
            mean_mbps=source_run_copy["throughput_mean_mbps"],
            p5_mbps=source_run_copy["throughput_p5_mbps"] or 0,
            p95_mbps=source_run_copy["throughput_p95_mbps"] or 0,
            per_pass_means=list(source_run_copy["throughput_pass_means"] or []),
        )

    result = grading.grade_drive(
        pre=pre_snap,
        post=post_snap,
        config=state.settings.grading,
        short_test_passed=True,
        long_test_passed=True,
        badblocks_errors=(0, 0, 0),
        max_temperature_c=None,
        throughput=throughput,
        drive_class=dclass,
    )

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
            throughput_mean_mbps=source_run_copy["throughput_mean_mbps"],
            throughput_p5_mbps=source_run_copy["throughput_p5_mbps"],
            throughput_p95_mbps=source_run_copy["throughput_p95_mbps"],
            throughput_pass_means=source_run_copy["throughput_pass_means"],
            sanitization_method=source_run_copy["sanitization_method"],
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

    return result.grade.value


def _snapshot_source(row: m.TestRun) -> dict:
    """Copy the columns we read AFTER the session closes — SQLAlchemy
    will detach the row otherwise and attribute access raises."""
    return {
        "completed_at": row.completed_at,
        "reallocated_sectors": row.reallocated_sectors,
        "current_pending_sector": row.current_pending_sector,
        "offline_uncorrectable": row.offline_uncorrectable,
        "power_on_hours_at_test": row.power_on_hours_at_test,
        "throughput_mean_mbps": row.throughput_mean_mbps,
        "throughput_p5_mbps": row.throughput_p5_mbps,
        "throughput_p95_mbps": row.throughput_p95_mbps,
        "throughput_pass_means": row.throughput_pass_means,
        "sanitization_method": row.sanitization_method,
    }
