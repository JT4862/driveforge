"""Test pipeline orchestrator.

Drives the per-drive state machine: pre-SMART → short test → firmware check
→ secure erase → badblocks → long test → post-SMART → grade → print →
webhook. In dev mode with fixtures, each phase completes fast (seconds)
so the full pipeline can be exercised locally without real hardware.

Real hardware runs take days. The orchestrator runs each drive in its own
asyncio task; up to `MAX_PARALLEL` drives are active at once.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from driveforge.core import badblocks, enclosures, grading, smart, telemetry, webhook
from driveforge.core.drive import Drive, Transport
from driveforge.daemon.state import DaemonState
from driveforge.db import models as m

logger = logging.getLogger(__name__)

MAX_PARALLEL = 8

PHASES = [
    "queued",
    "pre_smart",
    "short_test",
    "firmware_check",
    "secure_erase",
    "badblocks",
    "long_test",
    "post_smart",
    "grading",
    "done",
]


class Orchestrator:
    def __init__(self, state: DaemonState) -> None:
        self.state = state
        self._tasks: dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(MAX_PARALLEL)

    async def start_batch(self, drives: list[Drive], source: str | None = None) -> str:
        """Create a batch and kick off testing for each drive."""
        batch_id = uuid.uuid4().hex[:12]
        # Refresh the bay plan so new enclosures / drives show up
        plan = self.state.refresh_bay_plan()
        with self.state.session_factory() as session:
            batch = m.Batch(id=batch_id, source=source, started_at=datetime.now(UTC))
            session.add(batch)
            # Upsert drives
            for d in drives:
                existing = session.get(m.Drive, d.serial)
                if existing is None:
                    session.add(
                        m.Drive(
                            serial=d.serial,
                            model=d.model,
                            capacity_bytes=d.capacity_bytes,
                            transport=d.transport.value,
                            firmware_version=d.firmware_version,
                        )
                    )
            session.commit()
        used_keys = set(self.state.bay_assignments.keys())
        for drive in drives:
            bay_key = enclosures.bay_key_for_device(plan, drive.device_path)
            if bay_key is None:
                if plan.virtual_bay_count > 0:
                    bay_key = enclosures.assign_virtual_bay(plan, used_keys)
                if bay_key is None:
                    bay_key = enclosures.unbayed_key(drive.serial)
            used_keys.add(bay_key)
            self.state.bay_assignments[bay_key] = drive.serial
            task = asyncio.create_task(self._run_drive(batch_id, bay_key, drive))
            self._tasks[drive.serial] = task
        # Fire webhook when all drives in this batch complete
        asyncio.create_task(self._on_batch_complete(batch_id, [d.serial for d in drives]))
        return batch_id

    async def _run_drive(self, batch_id: str, bay_key: str, drive: Drive) -> None:
        """Per-drive pipeline."""
        async with self._semaphore:
            try:
                await self._execute_pipeline(batch_id, bay_key, drive)
            except Exception:
                logger.exception("drive pipeline failed for %s", drive.serial)
            finally:
                self.state.bay_assignments.pop(bay_key, None)
                self.state.active_phase.pop(drive.serial, None)
                self.state.active_percent.pop(drive.serial, None)

    async def _execute_pipeline(self, batch_id: str, bay_key: str, drive: Drive) -> None:
        with self.state.session_factory() as session:
            test_run = m.TestRun(
                drive_serial=drive.serial,
                batch_id=batch_id,
                bay=None,  # numeric bay deprecated — bay_key tracked in-memory instead
                phase="queued",
            )
            session.add(test_run)
            session.commit()
            session.refresh(test_run)
            run_id = test_run.id

        dev_mode = self.state.settings.dev_mode

        # Phase 1: pre-SMART
        await self._advance(run_id, "pre_smart", drive)
        pre_snap = await self._capture_smart(run_id, drive, kind="pre")
        if dev_mode:
            await asyncio.sleep(0.5)

        # Phase 2: short self-test
        await self._advance(run_id, "short_test", drive)
        short_ok = await self._run_short_test(drive, dev_mode=dev_mode)

        # Phase 3: firmware check (no apply in MVP)
        await self._advance(run_id, "firmware_check", drive)
        if dev_mode:
            await asyncio.sleep(0.2)

        # Phase 4: secure erase
        await self._advance(run_id, "secure_erase", drive)
        if dev_mode:
            await asyncio.sleep(1.0)
        # (real erase call goes here — skipped in dev)

        # Phase 5: badblocks
        await self._advance(run_id, "badblocks", drive)
        bb_errors = await self._run_badblocks(drive, dev_mode=dev_mode, run_id=run_id)

        # Phase 6: long self-test
        await self._advance(run_id, "long_test", drive)
        long_ok = await self._run_long_test(drive, dev_mode=dev_mode)

        # Phase 7: post-SMART
        await self._advance(run_id, "post_smart", drive)
        post_snap = await self._capture_smart(run_id, drive, kind="post")

        # Phase 8: grading
        await self._advance(run_id, "grading", drive)
        max_temp = self._max_temp_for_run(run_id)
        result = grading.grade_drive(
            pre=pre_snap,
            post=post_snap,
            config=self.state.settings.grading,
            short_test_passed=short_ok,
            long_test_passed=long_ok,
            badblocks_errors=bb_errors,
            max_temperature_c=max_temp,
        )
        await self._finalize_run(run_id, drive, post_snap, result)
        await self._advance(run_id, "done", drive)
        self.state.active_percent[drive.serial] = 100.0

    async def _advance(self, run_id: int, phase: str, drive: Drive) -> None:
        self.state.active_phase[drive.serial] = phase
        # Reset percent for each phase
        self.state.active_percent[drive.serial] = 0.0
        with self.state.session_factory() as session:
            run = session.get(m.TestRun, run_id)
            if run is None:
                return
            run.phase = phase
            session.commit()

    async def _capture_smart(self, run_id: int, drive: Drive, *, kind: str) -> smart.SmartSnapshot:
        # In dev/fixture mode this reads canned JSON
        snap = smart.snapshot(drive.device_path)
        with self.state.session_factory() as session:
            session.add(
                m.SmartSnapshot(
                    test_run_id=run_id,
                    kind=kind,
                    captured_at=snap.captured_at,
                    payload=snap.model_dump(mode="json"),
                )
            )
            session.commit()
        # Also record a telemetry sample for this moment
        self._record_telemetry(
            run_id,
            drive.serial,
            phase=f"smart_{kind}",
            drive_temp_c=snap.temperature_c,
        )
        return snap

    async def _run_short_test(self, drive: Drive, *, dev_mode: bool) -> bool:
        if dev_mode:
            await asyncio.sleep(0.3)
            return True
        smart.start_self_test(drive.device_path, kind="short")
        return True  # real polling loop lands with integration tests

    async def _run_long_test(self, drive: Drive, *, dev_mode: bool) -> bool:
        if dev_mode:
            await asyncio.sleep(0.3)
            return True
        smart.start_self_test(drive.device_path, kind="long")
        return True

    async def _run_badblocks(self, drive: Drive, *, dev_mode: bool, run_id: int) -> tuple[int, int, int]:
        if dev_mode:
            # Simulate a few seconds of progress
            for pct in range(0, 101, 20):
                self.state.active_percent[drive.serial] = float(pct)
                await asyncio.sleep(0.1)
            return (0, 0, 0)
        result = await badblocks.run_destructive(drive.device_path)
        # Very coarse parse: count errors from the last progress line
        errs = (0, 0, 0)
        for line in (result.stderr + result.stdout).splitlines():
            parsed = badblocks.parse_progress(line)
            if parsed is not None:
                errs = parsed[1]
        return errs

    def _record_telemetry(
        self,
        run_id: int,
        drive_serial: str,
        *,
        phase: str,
        drive_temp_c: int | None,
    ) -> None:
        chassis_w = telemetry.read_chassis_power()
        with self.state.session_factory() as session:
            session.add(
                m.TelemetrySample(
                    test_run_id=run_id,
                    drive_serial=drive_serial,
                    phase=phase,
                    drive_temp_c=drive_temp_c,
                    chassis_power_w=chassis_w,
                )
            )
            session.commit()

    def _max_temp_for_run(self, run_id: int) -> int | None:
        with self.state.session_factory() as session:
            temps = [
                s.drive_temp_c
                for s in session.query(m.TelemetrySample).filter_by(test_run_id=run_id).all()
                if s.drive_temp_c is not None
            ]
        return max(temps) if temps else None

    async def _finalize_run(
        self,
        run_id: int,
        drive: Drive,
        post_snap: smart.SmartSnapshot,
        result: grading.GradingResult,
    ) -> None:
        with self.state.session_factory() as session:
            run = session.get(m.TestRun, run_id)
            if run is None:
                return
            run.completed_at = datetime.now(UTC)
            run.grade = result.grade.value
            run.power_on_hours_at_test = post_snap.power_on_hours
            run.reallocated_sectors = post_snap.reallocated_sectors
            run.current_pending_sector = post_snap.current_pending_sector
            run.offline_uncorrectable = post_snap.offline_uncorrectable
            run.smart_status_passed = post_snap.smart_status_passed
            run.rules = [rule.model_dump() for rule in result.rules]
            run.report_url = f"/reports/{drive.serial}"
            session.commit()

    async def _on_batch_complete(self, batch_id: str, drive_serials: list[str]) -> None:
        """Wait for every per-drive task, then finalize batch + fire webhook."""
        tasks = [self._tasks[s] for s in drive_serials if s in self._tasks]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        with self.state.session_factory() as session:
            batch = session.get(m.Batch, batch_id)
            if batch is None:
                return
            batch.completed_at = datetime.now(UTC)
            runs = session.query(m.TestRun).filter_by(batch_id=batch_id).all()
            totals = {"A": 0, "B": 0, "C": 0, "fail": 0}
            drives_summary = []
            for run in runs:
                if run.grade and run.grade in totals:
                    totals[run.grade] += 1
                drives_summary.append(
                    {
                        "serial": run.drive_serial,
                        "model": run.drive.model if run.drive else None,
                        "capacity_tb": (run.drive.capacity_bytes / 1_000_000_000_000) if run.drive else None,
                        "grade": run.grade,
                        "tested_at": run.started_at.isoformat() if run.started_at else None,
                        "power_on_hours": run.power_on_hours_at_test,
                        "reallocated_sectors": run.reallocated_sectors,
                        "report_url": run.report_url,
                    }
                )
            session.commit()
        payload = {
            "event": "batch.complete",
            "batch_id": batch_id,
            "source": batch.source,
            "totals": totals,
            "drives": drives_summary,
        }
        delivered = await webhook.dispatch(self.state.settings.integrations.webhook_url, payload)
        with self.state.session_factory() as session:
            session.add(
                m.WebhookDelivery(
                    batch_id=batch_id,
                    url=self.state.settings.integrations.webhook_url or "",
                    succeeded=delivered,
                    payload=payload,
                    completed_at=datetime.now(UTC),
                )
            )
            session.commit()
