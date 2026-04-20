"""Test pipeline orchestrator.

Drives the per-drive state machine: pre-SMART → short test → firmware check
→ secure erase → badblocks → long test → post-SMART → grade → print →
webhook. In dev mode with fixtures, each phase completes fast (seconds)
so the full pipeline can be exercised locally without real hardware.

Real hardware runs take days. Each drive runs as its own asyncio task; up
to `MAX_PARALLEL` drives are active at once. Blocking subprocess calls
(secure_erase) run via `run_in_executor` so they don't stall the daemon's
event loop for the other drives or the HTTP API.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import traceback
import uuid
from datetime import UTC, datetime

from driveforge.core import badblocks, blinker, erase, grading, process, smart, telemetry, timing, webhook
from driveforge.core import drive as drive_mod
from driveforge.core.drive import Drive, Transport
from driveforge.daemon.state import DaemonState
from driveforge.db import models as m

logger = logging.getLogger(__name__)

# Upper bound on drives running concurrent pipelines. Drives past this
# limit still get asyncio tasks spawned, but they block on _semaphore
# until a slot frees up — and while blocked they're NOT in state.active_phase,
# so the dashboard renders them under "Installed" rather than "Active",
# which looks like "nothing happened to those drives." Keep this >= the
# largest chassis we expect to see. 32 covers the NX-3200 (14 bays) and a
# 24-bay JBOD expander in a single rig with room to spare. The historical
# 8 was an R720-era accident.
MAX_PARALLEL = 32

# Poll intervals (real-hardware)
SMART_SHORT_POLL_SEC = 15.0
SMART_LONG_POLL_SEC = 60.0
SMART_SHORT_TIMEOUT_SEC = 20 * 60  # 20 min — short test usually ~2 min
# Long self-test scales with capacity — the drive's firmware paces the test
# itself (smartctl reports the expected polling time), so our timeout is
# just the outer "if the drive never signals done, give up" bound. Computed
# per-drive in _run_self_test so 8+ TB doesn't false-fail at a flat 24 h.

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


class PipelineFailure(Exception):
    """Raised inside the pipeline to short-circuit to the failed state."""

    def __init__(self, phase: str, detail: str) -> None:
        super().__init__(f"{phase}: {detail}")
        self.phase = phase
        self.detail = detail


class BatchRejected(Exception):
    """Raised by start_batch when every requested drive is already under test
    (or the caller passed an empty selection). The web handler converts this
    into a user-facing flash so the operator knows nothing was started."""

    def __init__(self, detail: str, *, conflicts: list[str] | None = None) -> None:
        super().__init__(detail)
        self.conflicts = conflicts or []


LOG_TAIL_MAX_LINES = 40


class Orchestrator:
    def __init__(self, state: DaemonState) -> None:
        self.state = state
        self._tasks: dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(MAX_PARALLEL)

    def _log(self, drive_serial: str, line: str) -> None:
        """Append a line to the drive's in-memory log tail (capped)."""
        ts = datetime.now(UTC).strftime("%H:%M:%S")
        buf = self.state.active_log.setdefault(drive_serial, [])
        buf.append(f"[{ts}] {line}")
        if len(buf) > LOG_TAIL_MAX_LINES:
            del buf[: len(buf) - LOG_TAIL_MAX_LINES]

    def _persist_log(self, drive_serial: str, run_id: int) -> None:
        """Flush the in-memory log tail into test_run.log_tail."""
        buf = self.state.active_log.get(drive_serial)
        if not buf:
            return
        with self.state.session_factory() as session:
            run = session.get(m.TestRun, run_id)
            if run is None:
                return
            run.log_tail = "\n".join(buf)
            session.commit()

    def active_serials(self) -> set[str]:
        """Serials currently running in any in-flight batch. Guards against
        double-booking a drive into two batches — the second start would
        overwrite the task handle, orphan the first pipeline, and race the
        same device with parallel smartctl/hdparm/badblocks calls.
        """
        return set(self._tasks) | self.state.active_serials()

    def _spawn_done_blinker(self, drive: Drive, outcome: str) -> None:
        """Start the post-pipeline activity-LED blinker for this drive.

        Cancels any previous blinker for the same serial first. The task is
        stored in state.done_blinkers so start_batch / abort_all can clear
        it when the drive is re-enrolled or globally stopped.
        """
        old = self.state.done_blinkers.pop(drive.serial, None)
        if old is not None and not old.done():
            old.cancel()

        async def _wrapped() -> None:
            try:
                await blinker.blink_done(drive.device_path, pattern=outcome)
            finally:
                # Self-cleanup so a naturally-exiting blinker (drive pulled)
                # doesn't leak its entry in state.
                self.state.done_blinkers.pop(drive.serial, None)

        task = asyncio.create_task(_wrapped())
        self.state.done_blinkers[drive.serial] = task
        logger.info(
            "drive %s blinker started (outcome=%s, device=%s)",
            drive.serial, outcome, drive.device_path,
        )

    def _cancel_blinker(self, serial: str) -> None:
        task = self.state.done_blinkers.pop(serial, None)
        if task is not None and not task.done():
            task.cancel()

    def restore_blinker_for_drive(self, drive: Drive) -> None:
        """If this freshly-discovered drive has a completed run with a
        clear verdict, spawn the matching post-run blinker.

        Called on daemon boot (for drives present at startup) and on every
        hotplug DRIVE_ADDED event so a previously-tested drive that gets
        re-inserted resumes its pass/fail LED pattern without requiring
        the operator to start a new batch.

        Takes a Pydantic `core.drive.Drive` (not the SQLAlchemy model) —
        the DB schema doesn't store `device_path` since kernel letters
        drift across reboots. The caller is expected to have just obtained
        `drive` from `drive_mod.discover()` so its device_path is current.

        No-op in three cases:
          - Drive is currently under test (serial is in state.active_phase).
          - Drive has no completed test runs in the DB.
          - The latest run was user-aborted (phase=="aborted"). Aborts are
            operator-initiated; re-showing a fail LED on re-insert would
            misrepresent what happened.
        """
        if drive.serial in self.state.active_phase:
            return
        if drive.serial in self.state.done_blinkers:
            # Already blinking — no need to restart.
            return
        with self.state.session_factory() as session:
            # Just a sanity check that the drive is enrolled; we don't read
            # any paths off the DB row. Enrollment happens when the drive is
            # first included in a batch, so a never-tested drive returns None.
            if session.get(m.Drive, drive.serial) is None:
                return
            last_run = (
                session.query(m.TestRun)
                .filter_by(drive_serial=drive.serial)
                .filter(m.TestRun.completed_at.isnot(None))
                .order_by(m.TestRun.completed_at.desc())
                .first()
            )
            if last_run is None:
                return
            if last_run.phase == "aborted":
                return
            # Any pass-tier grade → heartbeat. Any fail (grade-fail OR
            # pipeline-error where grade wasn't assigned) → lighthouse.
            if last_run.grade in ("A", "B", "C"):
                outcome = "pass"
            elif last_run.grade == "fail":
                outcome = "fail"
            else:
                return
        self._spawn_done_blinker(drive, outcome)

    async def start_batch(
        self,
        drives: list[Drive],
        source: str | None = None,
        *,
        quick: bool = False,
    ) -> str:
        """Create a batch and kick off testing for each drive.

        Refuses to include any drive that's already under test in another
        in-flight batch (raises `BatchRejected` if every requested drive is
        already active).
        """
        busy = self.active_serials()
        conflicts = [d.serial for d in drives if d.serial in busy]
        if conflicts:
            logger.warning(
                "skipping %d already-active drive(s) from new batch: %s",
                len(conflicts), ", ".join(conflicts),
            )
        drives = [d for d in drives if d.serial not in busy]
        if not drives:
            raise BatchRejected(
                "all selected drives are already under test in another batch",
                conflicts=conflicts,
            )
        batch_id = uuid.uuid4().hex[:12]
        plan = self.state.refresh_bay_plan()
        with self.state.session_factory() as session:
            batch = m.Batch(id=batch_id, source=source, started_at=datetime.now(UTC))
            session.add(batch)
            for d in drives:
                # Refine transport for drives lsblk reports as SAS: the
                # `tran=sas` field just says "attached to a SAS HBA" — actual
                # SATA drives connected through the same HBA carry that tag
                # too. smartctl sees the wire protocol and returns the real
                # one. Without this refinement the DB and dashboard mislabel
                # SATA-on-SAS drives as SAS (erase.py re-probes internally
                # so correctness is fine, only the display was wrong).
                # Only probe on SAS to avoid unnecessary smartctl calls.
                if d.transport == Transport.SAS:
                    refined = drive_mod.detect_true_transport(d.device_path)
                    if refined is not None and refined != d.transport:
                        d = d.model_copy(update={"transport": refined})
                # rotational=True for spinning HDDs, False for SSDs/NVMe. Used
                # by the dashboard's ETA computation.
                rota = None if d.rotation_rate is None else d.rotation_rate > 0
                # Refine manufacturer via smartctl INQUIRY on SAS drives, with
                # OEM firmware-pattern override (Dell LS0x, HP HPGx, NetApp NAxx)
                # taking precedence over the underlying manufacturer's INQUIRY
                # vendor. One smartctl call per enrolled drive, not per
                # dashboard refresh — safe from the D-state pile-up pattern.
                mfr = (
                    drive_mod.probe_manufacturer(d.device_path, d.model, firmware=d.firmware_version)
                    or d.manufacturer
                )
                existing = session.get(m.Drive, d.serial)
                if existing is None:
                    session.add(
                        m.Drive(
                            serial=d.serial,
                            model=d.model,
                            manufacturer=mfr,
                            capacity_bytes=d.capacity_bytes,
                            transport=d.transport.value,
                            firmware_version=d.firmware_version,
                            rotational=rota,
                        )
                    )
                else:
                    # Backfill legacy rows + always refresh manufacturer to
                    # pick up improvements in the OEM-detection heuristic
                    # (e.g. drives previously logged as "Seagate" that we now
                    # recognize as Dell-OEM via the LS0x firmware pattern).
                    if existing.rotational is None and rota is not None:
                        existing.rotational = rota
                    if mfr and existing.manufacturer != mfr:
                        existing.manufacturer = mfr
                    # Correct legacy rows with a stale lsblk-level transport
                    # (e.g. "sas" for an Intel SATA SSD on a SAS HBA).
                    if existing.transport != d.transport.value:
                        existing.transport = d.transport.value
            session.commit()
        # Stop any "safe to pull" blinkers for drives we're re-enrolling so
        # they don't race real pipeline I/O.
        for drive in drives:
            self._cancel_blinker(drive.serial)
        for drive in drives:
            # Cache the basename so the diskstats poller can map back from
            # its per-device rows to the active serial without hitting the DB
            # (which doesn't store device_path).
            self.state.device_basenames[drive.serial] = drive.device_path.rsplit("/", 1)[-1]
            task = asyncio.create_task(self._run_drive(batch_id, drive, quick=quick))
            self._tasks[drive.serial] = task
        asyncio.create_task(self._on_batch_complete(batch_id, [d.serial for d in drives]))
        return batch_id

    async def abort_all(self) -> int:
        """Cancel every in-flight drive task + kill spawned subprocesses.

        Returns how many drives were aborted.
        """
        cancelled = 0
        for serial, task in list(self._tasks.items()):
            if not task.done():
                # Kill any subprocess still holding the drive BEFORE cancelling
                # the asyncio task — otherwise orphan processes keep running
                # in thread pool executors with no way for asyncio to reach them.
                killed = process.kill_owner(serial)
                if killed:
                    logger.warning("abort_all: killed %d subprocess(es) for %s", killed, serial)
                task.cancel()
                cancelled += 1
        await asyncio.sleep(0)
        self.state.active_phase.clear()
        self.state.active_percent.clear()
        self.state.active_sublabel.clear()
        self.state.active_drive_temp.clear()
        self.state.phase_change_ts.clear()
        self.state.recovery_serials.clear()
        # Stop all post-pipeline blinkers too — abort implies "don't touch
        # anything on these devices anymore."
        for serial in list(self.state.done_blinkers):
            self._cancel_blinker(serial)
        self._tasks.clear()
        logger.warning("abort_all cancelled %d drive task(s)", cancelled)
        return cancelled

    async def abort_drive(self, serial: str) -> bool:
        """Cancel one drive's pipeline + kill its subprocesses."""
        task = self._tasks.get(serial)
        if task is None or task.done():
            return False
        killed = process.kill_owner(serial)
        if killed:
            logger.warning("abort_drive: killed %d subprocess(es) for %s", killed, serial)
        task.cancel()
        await asyncio.sleep(0)
        logger.warning("aborted drive %s", serial)
        return True

    # ------------------------------------------------------------------ recovery

    async def recover_drive(self, drive: Drive) -> bool:
        """Look up an open `interrupted_at_phase` TestRun for this drive's
        serial and dispatch a recovery flow. Returns True if a recovery
        was started, False if there was nothing to recover.

        Called from the hotplug add handler BEFORE the blinker-restore /
        auto-enroll logic so that pull-interrupted drives get fixed up
        before anything else touches them.
        """
        serial = drive.serial
        with self.state.session_factory() as session:
            run = (
                session.query(m.TestRun)
                .filter_by(drive_serial=serial, completed_at=None)
                .filter(m.TestRun.interrupted_at_phase.isnot(None))
                .order_by(m.TestRun.started_at.desc())
                .first()
            )
            if run is None:
                return False
            interrupted_phase = run.interrupted_at_phase
            quick = bool(run.quick_mode)
            # Close the interrupted run: mark it done-as-superseded-by-recovery.
            # The new recovery pipeline gets its own TestRun row via start_batch.
            run.completed_at = datetime.now(UTC)
            run.phase = "interrupted"
            run.grade = None
            run.error_message = (
                f"Drive pulled during {interrupted_phase}; recovery initiated on re-insert."
            )
            session.commit()

        self.state.interrupted_serials.discard(serial)
        logger.warning(
            "drive %s recovery dispatched (was interrupted during %s, quick=%s)",
            serial,
            interrupted_phase,
            quick,
        )
        asyncio.create_task(self._run_recovery(drive, interrupted_phase, quick))
        return True

    async def _run_recovery(self, drive: Drive, interrupted_phase: str, quick: bool) -> None:
        """Restore drive state after a pull, then re-enroll in a fresh pipeline."""
        serial = drive.serial
        # Mark this drive as "in recovery" for the ENTIRE duration —
        # drive-state repair + the fresh pipeline that follows. Cleared
        # in _run_drive's finally when that pipeline exits (pass/fail).
        # The dashboard reads this to draw a persistent amber glow.
        self.state.recovery_serials.add(serial)
        self.state.active_log[serial] = []
        self._log(serial, f"recovery: drive was pulled during {interrupted_phase}")
        # Show a "recovering" phase on the dashboard while we repair state
        self.state.active_phase[serial] = "recovering"
        self.state.active_percent[serial] = 0.0
        self.state.active_sublabel[serial] = f"restoring after pull during {interrupted_phase}"
        self.state.device_basenames[serial] = drive.device_path.rsplit("/", 1)[-1]
        import time as _time
        self.state.phase_change_ts[serial] = _time.monotonic()

        try:
            if interrupted_phase == "secure_erase":
                await self._recover_secure_erase(drive)
            else:
                # Non-erase phases need no drive-side recovery — half-written
                # badblocks data will be overwritten by the fresh pipeline's
                # erase step; SMART self-test state clears on re-issue.
                self._log(
                    serial,
                    f"recovery: no drive-state repair needed for {interrupted_phase}; "
                    "restarting pipeline",
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("recovery failed for drive %s", serial)
            self._log(serial, f"recovery failed: {exc}. Drive left idle; retry manually.")
            # Leave drive idle — user can click New Batch to retry.
            self.state.active_phase.pop(serial, None)
            self.state.active_percent.pop(serial, None)
            self.state.active_sublabel.pop(serial, None)
            self.state.device_basenames.pop(serial, None)
            self.state.recovery_serials.discard(serial)
            return

        # Clear the recovering-phase state so start_batch can initialize fresh.
        self.state.active_phase.pop(serial, None)
        self.state.active_percent.pop(serial, None)
        self.state.active_sublabel.pop(serial, None)
        # Fresh pipeline run. Same quick flag as the interrupted run.
        await self.start_batch(
            [drive],
            source=f"auto-recovery after pull during {interrupted_phase}",
            quick=quick,
        )

    async def _recover_secure_erase(self, drive: Drive) -> None:
        """Per-transport drive-state repair after a mid-erase pull.

        SAS: Complete the sg_format that was interrupted (drive is in
             "Medium format corrupted" state; only sg_format-to-completion
             recovers it).
        SATA: The drive fell out of its ATA security session. Hdparm
              leaves it `security-locked`. Try unlock with the known
              password; on failure, try security-disable. Frozen state
              (BIOS re-froze at boot) can't be fixed in software.
        NVMe: Crypto-erase is atomic at the drive; power-cycle leaves
              the drive in a clean state. No action needed.
        """
        serial = drive.serial
        # Same transport-refinement pattern as erase.secure_erase() so
        # SATA-on-SAS drives take the SATA recovery path.
        effective = drive.transport
        if effective == Transport.SAS:
            refined = drive_mod.detect_true_transport(drive.device_path)
            if refined in (Transport.SATA, Transport.SAS, Transport.NVME):
                effective = refined

        if effective == Transport.NVME:
            self._log(serial, "recovery: NVMe crypto-erase is atomic — nothing to recover")
            return

        if effective == Transport.SAS:
            self._log(
                serial,
                "recovery: running sg_format --format to completion "
                "(drive was in 'Medium format corrupted' state; expect 15-60+ min)",
            )
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                erase._sas_secure_erase,
                drive.device_path,
            )
            self._log(serial, "recovery: sg_format completed; media is valid again")
            return

        if effective == Transport.SATA:
            # Check the security block of hdparm -I
            result = process.run(["hdparm", "-I", drive.device_path], timeout=10)
            out = (result.stdout or "").lower() if result.ok else ""
            is_frozen = "\tfrozen" in out and "not\tfrozen" not in out
            is_locked = "\tlocked" in out and "not\tlocked" not in out

            if is_frozen:
                self._log(
                    serial,
                    "recovery: drive security is FROZEN (BIOS/OS froze it at boot) — "
                    "cannot unlock via software. Power-cycle required with a BIOS "
                    "that doesn't issue SECURITY FREEZE LOCK.",
                )
                raise RuntimeError("SATA security is frozen; cannot recover via software")

            if is_locked:
                self._log(serial, "recovery: drive security-locked; attempting unlock")
                unlock = process.run(
                    [
                        "hdparm",
                        "--user-master", "u",
                        "--security-unlock", "driveforge",
                        drive.device_path,
                    ],
                    owner=serial,
                )
                if not unlock.ok:
                    raise RuntimeError(
                        f"hdparm --security-unlock failed: {unlock.stderr.strip() or 'unknown error'}"
                    )
                self._log(serial, "recovery: security-unlock succeeded")
                # Clear the ATA security password so the next erase starts fresh.
                disable = process.run(
                    [
                        "hdparm",
                        "--user-master", "u",
                        "--security-disable", "driveforge",
                        drive.device_path,
                    ],
                    owner=serial,
                )
                if disable.ok:
                    self._log(serial, "recovery: security-disable succeeded; drive is fully released")
                else:
                    # Non-fatal — pipeline will set its own password at next erase.
                    self._log(
                        serial,
                        f"recovery: security-disable failed (non-fatal): {disable.stderr.strip()}",
                    )
                return

            # Not locked, not frozen — drive's security state was never entered
            # (pull happened before security-set-pass succeeded) or the drive
            # self-cleared on power-cycle. Either way, nothing to do.
            self._log(serial, "recovery: drive not locked; fresh pipeline will re-erase")
            return

        self._log(serial, f"recovery: unknown transport {effective}; skipping drive-state repair")

    # ------------------------------------------------------------------ pipeline

    async def _run_drive(self, batch_id: str, drive: Drive, *, quick: bool) -> None:
        """Per-drive pipeline."""
        async with self._semaphore:
            # Fresh log buffer for this run
            self.state.active_log[drive.serial] = []
            self._log(
                drive.serial,
                f"start {drive.device_path} {drive.model} ({drive.transport.value}){' [quick]' if quick else ''}",
            )
            # Outcome drives the post-pipeline activity-LED blinker:
            #   "pass" → 3-pulse heartbeat, "fail" → slow single pulse,
            #   None → no blink (user aborted, or drive already removed).
            outcome: str | None = "pass"
            try:
                await self._execute_pipeline(batch_id, drive, quick=quick)
            except asyncio.CancelledError:
                logger.warning("drive %s cancelled mid-pipeline", drive.serial)
                self._record_failure(drive, phase="aborted", detail="aborted by user")
                outcome = None
            except PipelineFailure as exc:
                # Two distinct ways a pipeline fails:
                #   (a) the drive was pulled mid-phase (either flagged
                #       in-memory by the hotplug remove handler, or
                #       detected here by checking whether /dev/sdX is
                #       still present — udev's remove event sometimes
                #       arrives too late or without a serial, so the
                #       device-existence check is the authoritative
                #       signal). Leave the TestRun open + flag for
                #       recovery, no blinker.
                #   (b) legitimate hardware/pipeline failure. Close the
                #       run as fail + spawn the fail blinker.
                if self._looks_like_pull(drive):
                    self._flag_interrupted(drive, phase=exc.phase)
                    logger.warning(
                        "drive %s pipeline aborted by pull during phase=%s "
                        "(leaving TestRun open for recovery)",
                        drive.serial, exc.phase,
                    )
                    outcome = None
                else:
                    logger.error("drive %s failed in %s: %s", drive.serial, exc.phase, exc.detail)
                    self._record_failure(drive, phase=exc.phase, detail=exc.detail)
                    outcome = "fail"
            except Exception:
                tb = traceback.format_exc()
                # Same distinction for unexpected crashes.
                if self._looks_like_pull(drive):
                    self._flag_interrupted(drive, phase="error")
                    logger.warning(
                        "drive %s pipeline crashed after apparent pull "
                        "(leaving TestRun open for recovery)",
                        drive.serial,
                    )
                    outcome = None
                else:
                    logger.exception("drive %s pipeline crashed", drive.serial)
                    self._record_failure(drive, phase="error", detail=tb)
                    outcome = "fail"
            else:
                # Normal completion — a grading "fail" verdict is not an
                # exception, so refine outcome from the DB grade.
                with self.state.session_factory() as session:
                    latest = (
                        session.query(m.TestRun)
                        .filter_by(drive_serial=drive.serial)
                        .order_by(m.TestRun.started_at.desc())
                        .first()
                    )
                    if latest and latest.grade == "fail":
                        outcome = "fail"
            finally:
                import time as _time
                # Flag "just completed" so the dashboard can flash the
                # Installed card for a few seconds. Only stamp when the
                # drive actually finished (pass/fail) — user aborts don't
                # warrant the celebratory flash.
                if outcome in ("pass", "fail"):
                    self.state.just_completed_ts[drive.serial] = _time.monotonic()
                self.state.device_basenames.pop(drive.serial, None)
                self.state.active_phase.pop(drive.serial, None)
                self.state.active_percent.pop(drive.serial, None)
                self.state.active_sublabel.pop(drive.serial, None)
                self.state.active_drive_temp.pop(drive.serial, None)
                self.state.phase_change_ts.pop(drive.serial, None)
                # End of recovery-triggered pipeline — clear the amber
                # glow flag. No-op for normal (non-recovery) pipelines.
                self.state.recovery_serials.discard(drive.serial)
                # Keep the last log in memory briefly so a refresh after a
                # batch completes still shows the final lines. Let the next
                # run clear it.
                if outcome is not None:
                    self._spawn_done_blinker(drive, outcome)

    def _looks_like_pull(self, drive: Drive) -> bool:
        """Did this drive leave the bus while the pipeline was running?

        Two signals, either one authoritative:
          - `state.interrupted_serials` was set by the hotplug remove
            handler (normal case: udev fired the event + carried a serial).
          - The device file is gone on disk — covers the cases where the
            udev remove event hasn't arrived yet or carried no serial.
            This runs on the failure path after the subprocess already
            returned, so the kernel has had time to remove /dev/sdX.

        Returning True means "don't close the run as failed; mark for
        recovery on re-insert." Returning False means "legitimate
        hardware / pipeline failure; close the run as Fail."
        """
        import os
        if drive.serial in self.state.interrupted_serials:
            return True
        try:
            return not os.path.exists(drive.device_path)
        except OSError:
            return True  # can't stat → safer to assume gone

    def _flag_interrupted(self, drive: Drive, *, phase: str) -> None:
        """Mark this drive's open TestRun as pull-interrupted and stash the
        serial in state.interrupted_serials so the re-insert handler can
        find and recover it. Idempotent — safe to call repeatedly."""
        self.state.interrupted_serials.add(drive.serial)
        with self.state.session_factory() as session:
            run = (
                session.query(m.TestRun)
                .filter_by(drive_serial=drive.serial, completed_at=None)
                .order_by(m.TestRun.started_at.desc())
                .first()
            )
            if run is not None and run.interrupted_at_phase is None:
                run.interrupted_at_phase = phase
                session.commit()

    def _record_failure(self, drive: Drive, *, phase: str, detail: str) -> None:
        self._log(drive.serial, f"✗ {phase}: {detail}")
        with self.state.session_factory() as session:
            run = (
                session.query(m.TestRun)
                .filter_by(drive_serial=drive.serial, completed_at=None)
                .order_by(m.TestRun.started_at.desc())
                .first()
            )
            if run is None:
                return
            run.phase = "failed" if phase != "aborted" else "aborted"
            run.completed_at = datetime.now(UTC)
            run.grade = "fail"
            run.error_message = f"[{phase}] {detail}"[:4000]
            run.log_tail = "\n".join(self.state.active_log.get(drive.serial, []))
            session.commit()

    async def _execute_pipeline(
        self, batch_id: str, drive: Drive, *, quick: bool
    ) -> None:
        with self.state.session_factory() as session:
            test_run = m.TestRun(
                drive_serial=drive.serial,
                batch_id=batch_id,
                phase="queued",
                quick_mode=quick,
            )
            session.add(test_run)
            session.commit()
            session.refresh(test_run)
            run_id = test_run.id

        dev_mode = self.state.settings.dev_mode

        # Phase 1: pre-SMART
        await self._advance(run_id, "pre_smart", drive)
        pre_snap = await self._capture_smart(run_id, drive, kind="pre")

        # Phase 2: short self-test (always runs, even in quick mode).
        # `None` return = drive doesn't support it; treat as neutral, not a
        # failure. Only an explicit False (test completed + reported failure)
        # aborts the pipeline.
        await self._advance(run_id, "short_test", drive)
        short_ok = await self._run_self_test(drive, kind="short", dev_mode=dev_mode)
        if short_ok is False:
            raise PipelineFailure("short_test", "SMART short self-test reported failure")

        # Phase 3: firmware — just log the current version. Drive firmware
        # updates are a manual operation; we don't download or apply anything
        # automatically. Users with a firmware blob can flash it via the
        # vendor's tool and re-run the batch.
        await self._advance(run_id, "firmware_check", drive)
        if drive.firmware_version:
            self._log(drive.serial, f"firmware: {drive.firmware_version} (manual updates only)")
        if dev_mode:
            await asyncio.sleep(0.2)

        # Phase 4: secure erase (ALWAYS runs — this is the destructive step)
        await self._advance(run_id, "secure_erase", drive)
        await self._run_secure_erase(drive, dev_mode=dev_mode)

        bb_errors = (0, 0, 0)
        long_ok: bool | None = True
        if quick:
            logger.info("drive %s quick-mode: skipping badblocks + long test", drive.serial)
        else:
            # Phase 5: badblocks
            await self._advance(run_id, "badblocks", drive)
            bb_errors = await self._run_badblocks(drive, dev_mode=dev_mode, run_id=run_id)

            # Phase 6: long self-test — same neutral-on-unsupported semantics
            await self._advance(run_id, "long_test", drive)
            long_ok = await self._run_self_test(drive, kind="long", dev_mode=dev_mode)
            if long_ok is False:
                raise PipelineFailure("long_test", "SMART long self-test reported failure")

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

    # ------------------------------------------------------------------ phases

    async def _advance(self, run_id: int, phase: str, drive: Drive) -> None:
        # Record the transition so the dashboard can pulse the card briefly.
        # Only stamp if the phase actually changed — re-entering the same
        # phase (shouldn't happen, but defensive) doesn't need a pulse.
        if self.state.active_phase.get(drive.serial) != phase:
            import time as _time
            self.state.phase_change_ts[drive.serial] = _time.monotonic()
        self.state.active_phase[drive.serial] = phase
        self.state.active_percent[drive.serial] = 0.0
        self.state.active_sublabel.pop(drive.serial, None)
        self._log(drive.serial, f"→ phase: {phase}")
        with self.state.session_factory() as session:
            run = session.get(m.TestRun, run_id)
            if run is None:
                return
            run.phase = phase
            run.log_tail = "\n".join(self.state.active_log.get(drive.serial, []))
            session.commit()

    async def _capture_smart(self, run_id: int, drive: Drive, *, kind: str) -> smart.SmartSnapshot:
        try:
            snap = smart.snapshot(drive.device_path)
        except Exception as exc:  # noqa: BLE001
            raise PipelineFailure(f"smart_{kind}", f"smartctl failed: {exc}") from exc
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
        self._record_telemetry(
            run_id,
            drive.serial,
            phase=f"smart_{kind}",
            drive_temp_c=snap.temperature_c,
        )
        return snap

    async def _run_self_test(self, drive: Drive, *, kind: str, dev_mode: bool) -> bool | None:
        """Run a SMART self-test.

        Returns True on pass, False on fail, None if the drive doesn't support
        it. SAS drives in particular often skip self-test support entirely;
        that's not a pipeline failure — the destructive badblocks pass
        provides the real validation.
        """
        if dev_mode:
            await asyncio.sleep(0.3)
            return True
        try:
            smart.start_self_test(drive.device_path, kind=kind)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "drive %s doesn't support %s self-test: %s — skipping",
                drive.serial,
                kind,
                exc,
            )
            return None
        poll_sec = SMART_SHORT_POLL_SEC if kind == "short" else SMART_LONG_POLL_SEC
        if kind == "short":
            timeout = SMART_SHORT_TIMEOUT_SEC
        else:
            # 1 full-disk read, 2× headroom. 4 TB → ~22 h, 8 TB → ~44 h,
            # 16 TB → ~89 h. Drive firmware actually paces the test; we only
            # care that our polling loop doesn't give up before it finishes.
            timeout = timing.capacity_timeout(drive.capacity_bytes, passes=1)
        deadline = asyncio.get_event_loop().time() + timeout
        loop = asyncio.get_event_loop()
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(poll_sec)
            try:
                status = await loop.run_in_executor(None, smart.self_test_status, drive.device_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("self-test status poll failed for %s: %s", drive.serial, exc)
                continue
            if status.percent_complete is not None:
                self.state.active_percent[drive.serial] = float(status.percent_complete)
            if not status.in_progress:
                # None = couldn't determine (unsupported log format, SAS,
                # first-ever test, etc.) → neutral, not a failure
                return status.last_result_passed
        logger.warning("drive %s %s self-test timed out after %ds", drive.serial, kind, timeout)
        return None

    async def _run_secure_erase(self, drive: Drive, *, dev_mode: bool) -> None:
        if dev_mode:
            await asyncio.sleep(1.0)
            return
        if drive.transport == Transport.USB:
            # Safety: never erase a USB-attached drive. Likely an external
            # boot drive or adapter, not a test target.
            raise PipelineFailure("secure_erase", "refusing to erase USB-transport drive")
        serial = drive.serial
        # Surface the specific erase mechanism on the dashboard card so the
        # operator can see at a glance why some drives' activity LEDs blink
        # (SAS `sg_format` issues per-sector SCSI commands → HBA sees traffic
        # → backplane LED activity) while others stay solid (SATA drives
        # take a single ATA command and let the drive firmware handle the
        # overwrite internally → no link-level traffic → LED idle).
        mechanism = {
            Transport.SATA: "hdparm --security-erase",
            Transport.SAS: "sg_format --format",
            Transport.NVME: "nvme format -s 1",
        }.get(drive.transport, "secure erase")
        self.state.active_sublabel[serial] = mechanism
        # hdparm / sg_format / nvme format emit no progress — we synthesize a
        # time-based bar from the drive's own estimate (hdparm -I) or a
        # capacity heuristic. Caps at 99% so the bar never claims done until
        # the blocking call actually returns.
        estimate = erase.estimate_erase_seconds(drive)
        if estimate:
            mins = estimate / 60
            self._log(
                serial,
                f"secure_erase estimated ~{mins:.0f} min (ticker based on drive estimate)",
            )
        else:
            self._log(serial, "secure_erase: no duration estimate — showing indeterminate progress")

        async def tick() -> None:
            start = asyncio.get_event_loop().time()
            while True:
                elapsed = asyncio.get_event_loop().time() - start
                if estimate:
                    pct = min(99.0, (elapsed / estimate) * 100.0)
                else:
                    # Indeterminate: saw-tooth 0 → 95 → 0 every 30s so the
                    # user sees the UI is alive without us pretending to know
                    # the actual progress.
                    pct = (elapsed % 30.0) / 30.0 * 95.0
                self.state.active_percent[serial] = pct
                await asyncio.sleep(1.0)

        ticker = asyncio.create_task(tick())
        loop = asyncio.get_event_loop()
        # Capture actual wall-clock duration. Useful when the drive completes
        # dramatically faster than the pre-flight estimate — e.g. Intel /
        # Samsung enterprise SSDs do ATA SECURITY ERASE UNIT via firmware-
        # level crypto-erase (rotate the media key → all ciphertext is
        # unrecoverable) and return in seconds, while the hdparm -I estimate
        # still quotes the legacy full-overwrite duration. Without an
        # explicit completion log it looks like the phase was skipped.
        erase_start = loop.time()
        try:
            await loop.run_in_executor(None, erase.secure_erase, drive)
        except erase.EraseError as exc:
            raise PipelineFailure("secure_erase", str(exc)) from exc
        except subprocess.TimeoutExpired as exc:
            # Surface the timeout plainly instead of the old wrapped
            # "unexpected: Command '[...]' timed out after N seconds" string,
            # which read like a command-dispatch bug rather than what it
            # actually is: the drive's erase didn't finish inside the
            # dynamic timeout. 4 TB+ drives hit this most often — if it
            # repeats, either raise the cap in erase.py or move the drive
            # to full mode (badblocks is interruptible).
            hours = exc.timeout / 3600 if exc.timeout else 0
            raise PipelineFailure(
                "secure_erase",
                f"erase timed out after {hours:.1f}h — drive may need a larger timeout or a different erase path",
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise PipelineFailure("secure_erase", f"unexpected: {exc}") from exc
        finally:
            ticker.cancel()
            try:
                await ticker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # Log wall-clock duration so operators can tell at a glance whether
        # the drive did a real full-media overwrite or a near-instant
        # crypto-erase. Both are valid secure erases; the visible difference
        # otherwise reads as "did the phase actually run?".
        erase_elapsed = loop.time() - erase_start
        if erase_elapsed < 30:
            self._log(
                serial,
                f"secure_erase completed in {erase_elapsed:.1f}s "
                f"(likely vendor crypto-erase — data is still unrecoverable)",
            )
        elif erase_elapsed < 120:
            self._log(serial, f"secure_erase completed in {erase_elapsed:.0f}s")
        else:
            self._log(serial, f"secure_erase completed in {erase_elapsed / 60:.1f} min")
        self.state.active_percent[serial] = 100.0

    async def _run_badblocks(self, drive: Drive, *, dev_mode: bool, run_id: int) -> tuple[int, int, int]:
        if dev_mode:
            for pct in range(0, 101, 20):
                self.state.active_percent[drive.serial] = float(pct)
                await asyncio.sleep(0.1)
            return (0, 0, 0)
        serial = drive.serial
        self._log(
            serial,
            f"badblocks -wsv -b {badblocks.BLOCK_SIZE} -c {badblocks.BLOCK_COUNT} "
            f"{drive.device_path}",
        )
        # Throttle log updates to every 10% so we don't fill the buffer
        last_logged_pct = [0.0]
        last_pass_label = [""]

        def on_progress(
            pct: float,
            errs: tuple[int, int, int],
            pass_label: str | None,
        ) -> None:
            self.state.active_percent[serial] = float(pct)
            if pass_label is not None:
                self.state.active_sublabel[serial] = pass_label
                # Log once per pass transition so the phase log shows the
                # sweep boundaries without drowning in per-% spam.
                if pass_label != last_pass_label[0]:
                    self._log(serial, f"badblocks: starting {pass_label}")
                    last_pass_label[0] = pass_label
                    self._persist_log(serial, run_id)
            if pct - last_logged_pct[0] >= 10.0:
                self._log(serial, f"badblocks: {pct:.1f}% errors={errs[0]}/{errs[1]}/{errs[2]}")
                last_logged_pct[0] = pct
                self._persist_log(serial, run_id)

        # 8 passes × capacity at pessimistic 100 MB/s with 2× headroom.
        # Old flat 72 h default choked anything over ~4 TB; 8 TB legit needs
        # ~15 days at full 8-pass burn-in, and that's fine — the operator
        # can abort if they change their mind.
        bb_timeout = timing.capacity_timeout(
            drive.capacity_bytes, passes=badblocks.TOTAL_PASSES
        )
        try:
            errs = await badblocks.run_destructive_streaming(
                drive.device_path,
                on_progress=on_progress,
                owner=serial,
                timeout=bb_timeout,
            )
        except badblocks.BadblocksError as exc:
            raise PipelineFailure("badblocks", str(exc)) from exc
        except asyncio.TimeoutError:
            hours = bb_timeout / 3600
            raise PipelineFailure(
                "badblocks",
                f"badblocks exceeded capacity-based timeout of {hours:.1f}h",
            ) from None
        self._log(serial, f"badblocks complete: errors={errs[0]}/{errs[1]}/{errs[2]}")
        return errs

    def _record_telemetry(
        self,
        run_id: int,
        drive_serial: str,
        *,
        phase: str,
        drive_temp_c: int | None,
    ) -> None:
        # Surface the live temp on the dashboard in addition to persisting it.
        # Cleared when the drive leaves active_phase (in _run_drive's finally).
        if drive_temp_c is not None:
            self.state.active_drive_temp[drive_serial] = drive_temp_c
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
