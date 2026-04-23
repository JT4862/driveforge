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
import functools
import logging
import subprocess
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path

from driveforge.core import badblocks, blinker, erase, grading, process, smart, telemetry, timing, webhook
from driveforge.core import drive as drive_mod
from driveforge.core import throughput as throughput_mod
from driveforge.core import triage as triage_mod
from driveforge.core.drive import Drive, Transport
from driveforge.daemon.state import DaemonState
from driveforge.db import models as m

logger = logging.getLogger(__name__)


def _classify_failure_grade(phase: str, detail: str) -> str | None:
    """v0.6.5+ classification helper. Return the right grade for a
    pipeline failure — used by `_record_failure` to decide between
    "error" (transient/pipeline, retry-on-reinsert) and "F"
    (drive-verdict fail, sticky + sticker).

    Returns `None` when phase indicates an abort (never tested;
    grade stays NULL). Otherwise "F" for drive-verdict failures,
    "error" for everything else.

    Rules — drive-verdict signals (→ "F"):
      - Phase is short_test or long_test: SMART self-test is the
        drive reporting on its own health. Failing this IS the
        drive's verdict.
      - Detail mentions "device fault" or "drive hardware is
        likely failing": DF bit set, internal failure.
      - Detail mentions "uncorrectable" or "media error(s)" or
        "physically failing": UNC/BBK bits set, drive can't read
        its own sectors.

    Everything else (including both-paths-abort on secure_erase —
    likely a libata-timing freeze, may be transient) → "error".

    Module-level (not a method) so test code can import + call it
    directly without needing an Orchestrator instance.
    """
    if phase == "aborted":
        return None
    if phase in ("short_test", "long_test"):
        # SMART self-test reported failure — drive's own verdict.
        return "F"
    lower = (detail or "").lower()
    f_signals = (
        "device fault",
        "drive hardware is likely failing",
        "uncorrectable",
        "media error",
        "physically failing",
    )
    if any(sig in lower for sig in f_signals):
        return "F"
    return "error"

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

        Only task entries whose futures haven't completed are counted. A
        done task in `_tasks` isn't actually busy — and prior to v0.2.7 a
        missing pop in `_run_drive`'s finally meant every previously-run
        serial showed up as "busy" here, which in turn caused
        `start_batch` to reject auto-enroll of an aborted → re-inserted
        drive via `BatchRejected`. The finally now pops, but this filter
        is defence-in-depth against the same class of bug re-emerging.
        """
        running = {s for s, t in self._tasks.items() if not t.done()}
        return running | self.state.active_serials()

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

    def _cancel_identify(self, serial: str) -> None:
        """Stop an in-flight identify blinker for this drive, if any."""
        task = self.state.identify_blinkers.pop(serial, None)
        if task is not None and not task.done():
            task.cancel()

    def is_identifying(self, serial: str) -> bool:
        """True if an identify blinker is currently running for this serial.
        Used by the dashboard to toggle the button label between 'Ident'
        and 'Stop'."""
        task = self.state.identify_blinkers.get(serial)
        return task is not None and not task.done()

    async def identify_drive(self, drive: Drive) -> tuple[bool, str]:
        """Start the LED strobe so the operator can physically locate this
        drive in the rack. Safe to call on already-identifying drives —
        the previous identify is cancelled first.

        Refuses if the drive is currently running a pipeline: identify I/O
        would fight with real test I/O for the same block device, and the
        active pipeline is already blinking the drive anyway.

        Returns (ok, message). The Settings/dashboard handler surfaces
        the message to the user on refusal.

        When identify exits (deadline, drive pull, or explicit Stop click),
        restore the previous pass/fail LED pattern via
        `restore_blinker_for_drive` so the bay's "safe to pull" heartbeat
        isn't lost just because someone clicked Ident and walked away.
        """
        serial = drive.serial
        if serial in self.state.active_phase:
            return (
                False,
                "Drive is currently under test; its activity LED is already lit by the pipeline.",
            )
        # Cancel any existing identify + cancel the pre-existing done-blinker
        # (we'll restore it when identify exits).
        self._cancel_identify(serial)
        self._cancel_blinker(serial)

        drive_for_restore = drive  # captured for the finally path

        async def _wrapped() -> None:
            try:
                await blinker.blink_identify(drive.device_path)
            finally:
                # Self-cleanup so a naturally-exiting identify (deadline hit,
                # drive pulled, user clicked Stop) doesn't leak its entry.
                self.state.identify_blinkers.pop(serial, None)
                # Restore whatever done-blinker pattern this drive would
                # naturally have (pass/fail from its last completed run,
                # or nothing for never-tested drives). Best-effort — a
                # transient failure must not propagate out of the finally.
                try:
                    self.restore_blinker_for_drive(drive_for_restore)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "identify: failed to restore done-blinker for %s", serial,
                    )

        task = asyncio.create_task(_wrapped())
        self.state.identify_blinkers[serial] = task
        logger.info(
            "identify blinker started for %s (%s)", serial, drive.device_path,
        )
        return (True, "Identifying drive — LED will blink rapidly for up to 5 minutes.")

    def stop_identify(self, serial: str) -> bool:
        """Cancel a running identify blinker. Returns True if one was
        actually running. The task's finally block restores the prior
        pass/fail LED pattern automatically."""
        task = self.state.identify_blinkers.get(serial)
        if task is None or task.done():
            return False
        self._cancel_identify(serial)
        logger.info("identify blinker stopped for %s", serial)
        return True

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
            # Any pass-tier grade → heartbeat. Any fail-like outcome
            # (real drive-fail "F" OR pipeline-error "error") →
            # lighthouse. Legacy "fail" rows (from pre-v0.5.1 code)
            # also go to lighthouse. The operator distinguishes
            # drive-fail from pipeline-error via sticker presence at
            # the rack, not via LED pattern.
            if last_run.grade in ("A", "B", "C"):
                outcome = "pass"
            elif last_run.grade in ("F", "error", "fail"):
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

    async def abort_drive(self, serial: str) -> dict[str, object]:
        """Cancel one drive's pipeline + kill its subprocesses.

        v0.7.0+: returns a structured outcome dict instead of a bare
        bool so the HTTP route can surface a specific flash banner
        (previously the route redirected to `/` with no feedback on
        either success or "not in _tasks", making the button feel dead
        — the actual bug class that motivated this rework).

        Outcome keys:
          status:   "aborted" | "not_active" | "already_done"
          killed:   int — number of subprocesses that got SIGTERM/KILL
          phase:    str | None — the phase the drive was in at abort
          note:     str — short human-readable one-liner for the banner

        All branches now LOG explicitly. Pre-v0.7.0 the "not_active"
        branch returned False + logged nothing, so diagnosing why
        abort appeared to do nothing required narrowing from journal
        absence — which is exactly what happened on NX-3200 during the
        v0.7.0 kickoff session.
        """
        task = self._tasks.get(serial)
        phase = self.state.active_phase.get(serial)

        if task is None:
            logger.info(
                "abort_drive: %s is not in _tasks (no active pipeline); nothing to abort",
                serial,
            )
            return {
                "status": "not_active",
                "killed": 0,
                "phase": phase,
                "note": f"{serial} isn't currently running a pipeline.",
            }

        if task.done():
            logger.info(
                "abort_drive: %s's task already completed — clearing stale entry",
                serial,
            )
            self._tasks.pop(serial, None)
            return {
                "status": "already_done",
                "killed": 0,
                "phase": phase,
                "note": f"{serial}'s pipeline had already finished.",
            }

        # Live task — proceed with abort.
        killed = process.kill_owner(serial)
        if killed:
            logger.warning(
                "abort_drive: SIGTERM → SIGKILL %d subprocess(es) for %s (phase=%s)",
                killed, serial, phase,
            )
        else:
            # No registered PIDs means the task is between subprocess
            # invocations (e.g. in a post_smart DB write). Cancel will
            # still tear it down cleanly.
            logger.warning(
                "abort_drive: %s has no registered subprocesses in phase=%s "
                "(cancelling task directly)",
                serial, phase,
            )

        # v0.7.0+: surface "aborting" on the dashboard so operators see
        # the UI acknowledge the click. Cleared when the task's finally
        # block runs + the drive leaves active_phase. If the teardown
        # hangs (D-state subprocess), the sublabel persists until pull
        # or kernel unstick — which is itself useful signal.
        self.state.active_sublabel[serial] = "aborting — waiting for pipeline to tear down"

        task.cancel()
        await asyncio.sleep(0)
        logger.warning("aborted drive %s (phase=%s, killed=%d)", serial, phase, killed)
        return {
            "status": "aborted",
            "killed": killed,
            "phase": phase,
            "note": (
                f"Abort signalled for {serial}"
                + (f" in {phase}" if phase else "")
                + (f"; SIGTERM/SIGKILL to {killed} subprocess(es)." if killed else ".")
            ),
        }

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
                self.state.drive_command_executor,
                erase._sas_secure_erase,
                drive.device_path,
            )
            self._log(serial, "recovery: sg_format completed; media is valid again")
            return

        if effective == Transport.SATA:
            # v0.5.0+: delegate to the shared `ensure_clean_security_state`
            # primitive. Same probe + unlock + disable flow that the
            # secure_erase pre-flight uses on every run; keeping the
            # logic in one place means future edits don't drift between
            # recovery and pre-flight. The preflight primitive raises
            # EraseError on unrecoverable states (frozen, unknown
            # password) with user-facing messages.
            #
            # v0.6.3+ Case B handling: if preflight raises because the
            # drive refuses unlock, the drive might be autonomously
            # erasing (firmware resumed the ERASE UNIT after power-on
            # and will not respond to security commands until the
            # internal erase finishes — hours for HDDs). We detect
            # that pattern and wait for the drive to return to CLEAN
            # state rather than failing the recovery outright.
            self._log(
                serial,
                "recovery: running shared preflight to clean security state "
                "(unlock + disable if needed)",
            )
            # v0.6.7+: show the drive as active during recovery preflight
            # so operators see progress on the dashboard during the
            # potentially-long SAT unlock + disable-password sequence.
            self.state.active_phase[serial] = "recovery_preflight"
            self.state.active_sublabel[serial] = (
                "recovery: probing security state + unlocking if needed"
            )
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(
                    self.state.drive_command_executor,
                    erase.ensure_clean_security_state,
                    drive,
                )
            except erase.EraseError as exc:
                # v0.6.3: Case B — drive might be mid-erase. Try the
                # wait pattern before giving up. The drive's own
                # estimate informs how long we're willing to wait.
                self._log(
                    serial,
                    f"recovery: preflight couldn't unlock ({exc}) — "
                    f"drive may be autonomously completing a prior "
                    f"secure_erase. Entering wait pattern."
                )
                self.state.active_sublabel[serial] = (
                    "Waiting for prior secure-erase to complete (drive locked, "
                    "self-erasing)"
                )

                def _wait_status(elapsed_s: float, state) -> None:
                    # Runs in the executor thread. Dict assignment is
                    # thread-safe in CPython; good enough for a sublabel.
                    mins = elapsed_s / 60
                    self.state.active_sublabel[serial] = (
                        f"Waiting for prior secure-erase to complete · "
                        f"{mins:.0f} min elapsed · state={state.value}"
                    )

                completed = await loop.run_in_executor(
                    self.state.drive_command_executor,
                    functools.partial(
                        erase.wait_for_prior_erase_completion,
                        drive,
                        poll_interval_s=60,
                        max_wait_s=12 * 3600,
                        progress_callback=_wait_status,
                    ),
                )
                if not completed:
                    raise RuntimeError(
                        f"Drive did not return to a usable state within 12 h. "
                        f"Either the prior erase is taking longer than expected "
                        f"or the drive is genuinely unresponsive. Underlying "
                        f"preflight error: {exc}"
                    ) from exc
                self._log(
                    serial,
                    "recovery: prior secure_erase completed; drive is CLEAN; "
                    "fresh pipeline will proceed",
                )
                return
            self._log(serial, "recovery: drive is in CLEAN security state; fresh pipeline will re-erase")
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
                # Normal completion — a grading fail-tier verdict is not
                # an exception, so refine outcome from the DB grade.
                # v0.5.1+ vocabulary: grade "F" is the real-drive-fail
                # verdict; "error" is the pipeline-error (not reached
                # here since pipeline errors raise). Legacy "fail" rows
                # from pre-v0.5.1 still honored.
                with self.state.session_factory() as session:
                    latest = (
                        session.query(m.TestRun)
                        .filter_by(drive_serial=drive.serial)
                        .order_by(m.TestRun.started_at.desc())
                        .first()
                    )
                    if latest and latest.grade in ("F", "error", "fail"):
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
                # Drop this drive's (now-finished) task handle so the
                # orchestrator doesn't treat the serial as "still busy"
                # forever. Without this pop, `active_serials()` keeps
                # returning every serial ever run, which causes
                # `start_batch` to raise `BatchRejected` on auto-enroll
                # of a re-inserted drive (common path: abort → pull →
                # reinsert with auto-enroll on). Bug fixed in v0.2.7.
                self._tasks.pop(drive.serial, None)
                # Keep the last log in memory briefly so a refresh after a
                # batch completes still shows the final lines. Let the next
                # run clear it.
                if outcome is not None:
                    self._spawn_done_blinker(drive, outcome)

    def _looks_like_pull(self, drive: Drive) -> bool:
        """Did this drive leave the bus while the pipeline was running?

        Ordered signals (first-match wins):
          1. `state.interrupted_serials` was set by the hotplug remove
             handler (normal case: udev fired the event + carried a serial).
          2. Rediscovery finds the drive's SERIAL under any current
             device path. Modern Debian kernels will re-enumerate a
             drive (new /dev/sdX letter) after certain errors — the
             NX-3200's LSI SAS2308 does this on `CONFIG_IDE_TASK_IOCTL`
             hdparm failures in particular. Serial-based rediscovery
             correctly classifies that as "drive still present, bus
             glitched" rather than a pull. If the serial is still
             discoverable, return False authoritatively. Added v0.2.9;
             before that, a bus glitch would leave a TestRun stuck in
             `interrupted_at_phase="secure_erase"` forever because no
             hotplug ADD event ever fires without a real pull.
          3. Fallback: the exact device_path we were using is gone on
             disk AND rediscovery didn't confirm the serial either way
             (discovery errored, returned empty, etc.). Safer to assume
             gone than to close a stale run as a permanent Fail.

        Returning True means "don't close the run as failed; mark for
        recovery on re-insert." Returning False means "legitimate
        hardware / pipeline failure; close the run as Fail."
        """
        import os
        if drive.serial in self.state.interrupted_serials:
            return True
        # Serial-based rediscovery is authoritative when it succeeds.
        try:
            present = drive_mod.discover()
        except Exception:  # noqa: BLE001
            present = None
        if present is not None:
            if any(d.serial == drive.serial for d in present):
                # Drive is still present (possibly under a different
                # device letter after kernel re-enumeration). This is
                # NOT a pull — let the caller close the run as Fail.
                return False
            # Discovery succeeded AND the serial wasn't in the results.
            # That's a stronger "gone" signal than just device_path
            # missing, because it means lsblk has fully settled and
            # the drive really isn't there.
            return True
        # Discovery errored. Fall back to the device-path check.
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

    def _stamp_sanitization_method(self, run_id: int, method: str) -> None:
        """v0.6.7+ — mark the sanitization method on a live run.

        Called from `_run_secure_erase` after either path completes:
          - "secure_erase" on successful SAT/hdparm SECURITY ERASE UNIT
          - "badblocks_overwrite" when the HDD libata-freeze fallback
            engaged and the pipeline is deferring sanitization to
            badblocks

        The cert label's "Sanitized via ..." line reads from this
        column. Fail-safe: any DB error is logged but never raises —
        the sanitization happened regardless of whether we stamped it.
        """
        try:
            with self.state.session_factory() as session:
                run = session.get(m.TestRun, run_id)
                if run is None:
                    return
                run.sanitization_method = method
                session.commit()
        except Exception:  # noqa: BLE001
            logger.exception(
                "failed to stamp sanitization_method=%s on run_id=%s",
                method, run_id,
            )

    def _record_failure(self, drive: Drive, *, phase: str, detail: str) -> None:
        """Close an open TestRun with a non-success verdict.

        Four distinct outcomes encoded via `grade`:

          - aborted (user cancelled mid-pipeline):
              phase="aborted", grade=NULL
              Re-insert triggers auto-enroll. Drive was never graded.

          - pipeline error (code/infra broke, transient drive
            refusal that might clear, SAT+hdparm ABRT on what
            looks like a libata-timing freeze, subprocess crash,
            etc.):
              phase="failed", grade="error"
              Re-insert triggers auto-retest — the error may have been
              transient. Drive's health is unknown; no cert, no label.

          - drive-verdict fail from grading rules:
              phase="failed", grade="F"
              Written by `_finalize_run` when grading rules return
              Grade.FAIL. Sticky — auto-enroll skips.

          - drive-verdict fail from early phase (v0.6.5+): the DRIVE
            itself reported failure (SMART short/long self-test
            failed, device-fault bit set during erase, UNC media
            during erase). Written from THIS function when the
            classifier picks "F" based on phase + decoded error
            nature. Sticky — auto-enroll skips. Auto-prints a fail
            sticker (same as _finalize_run path) so the operator has
            physical evidence of the F verdict.

        Vocabulary added in v0.5.1; early-phase F classification
        added in v0.6.5. Pre-v0.6.5 graded ALL non-abort failures
        as "error", which meant a drive that failed its own SMART
        self-test would auto-re-trigger on every reinsert instead
        of staying sticky F.
        """
        self._log(drive.serial, f"✗ {phase}: {detail}")
        grade = _classify_failure_grade(phase, detail)
        with self.state.session_factory() as session:
            run = (
                session.query(m.TestRun)
                .filter_by(drive_serial=drive.serial, completed_at=None)
                .order_by(m.TestRun.started_at.desc())
                .first()
            )
            if run is None:
                return
            run.phase = "aborted" if grade is None else "failed"
            run.completed_at = datetime.now(UTC)
            run.grade = grade
            run.error_message = f"[{phase}] {detail}"[:4000]
            run.log_tail = "\n".join(self.state.active_log.get(drive.serial, []))
            session.commit()

            # v0.6.5+: auto-print on early-phase F. v0.6.4 only auto-printed
            # from _finalize_run, so drives that failed early (short test,
            # secure_erase with drive-verdict failure) got no physical
            # sticker even when they were definitively F. Print now so the
            # operator has a sticker on the physical drive reflecting the
            # F verdict.
            #
            # Failures graded "error" don't print — they might be transient
            # and re-trigger on reinsert; a sticker saying "FAIL" on a
            # drive that might actually be fine is worse than no sticker.
            if grade == "F":
                pc = self.state.settings.printer
                if pc.model and getattr(pc, "auto_print", True):
                    from driveforge.core import printer as printer_mod
                    self._log(drive.serial, "auto-print: early-phase F → scheduling fail sticker print")
                    # v0.6.6+: fire-and-forget via the drive-command
                    # executor. Pre-v0.6.6 this ran synchronously from
                    # _record_failure, which is called from the async
                    # _run_drive except block — a sync call in that
                    # context blocks the event loop for the full USB
                    # print dispatch (20-30s per label, longer if USB
                    # queue is backed up). With multiple F drives in
                    # a batch, the event loop stalled, dashboard went
                    # unresponsive, pipeline progress stopped. Moving
                    # the print to the drive executor plus a done-
                    # callback to log the result is the correct
                    # pattern.
                    try:
                        loop = asyncio.get_event_loop()
                    except RuntimeError:
                        # No running loop — shouldn't happen from
                        # _run_drive's async except block, but
                        # defensive anyway. Fall back to skip-print;
                        # operator can reprint manually.
                        logger.warning(
                            "auto-print: no running event loop in _record_failure "
                            "for %s; skipping auto-print", drive.serial,
                        )
                        return
                    serial = drive.serial  # capture for callback closure
                    future = loop.run_in_executor(
                        self.state.drive_command_executor,
                        functools.partial(
                            printer_mod.auto_print_cert_for_run,
                            self.state, drive, run,
                        ),
                    )

                    def _on_print_done(fut, _serial=serial):
                        try:
                            ok, msg = fut.result()
                            self._log(_serial, f"auto-print: {msg}")
                        except Exception as exc:  # noqa: BLE001
                            logger.exception(
                                "auto-print on early-phase F raised for %s",
                                _serial,
                            )
                            self._log(_serial, f"auto-print: crashed ({exc})")

                    future.add_done_callback(_on_print_done)


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

        # v0.5.5+ \u2014 start the periodic telemetry sampler for this run.
        # Runs until the pipeline finishes (or crashes), at which point
        # the finally block below cancels it. Pre-v0.5.5 telemetry was
        # only captured at the two SMART-snapshot phase boundaries,
        # producing sparse 2-sample charts on multi-hour runs.
        sampler_task = asyncio.create_task(
            self._telemetry_sampler_loop(run_id, drive),
            name=f"telemetry-sampler:{drive.serial}",
        )

        try:
            await self._execute_pipeline_inner(
                batch_id=batch_id,
                drive=drive,
                quick=quick,
                run_id=run_id,
            )
        finally:
            sampler_task.cancel()
            try:
                await sampler_task
            except (asyncio.CancelledError, Exception):
                # Expected on cancel. A real sampler error should have
                # been logged inside the loop; don't let cleanup raise.
                pass

    async def _execute_pipeline_inner(
        self,
        *,
        batch_id: str,
        drive: Drive,
        quick: bool,
        run_id: int,
    ) -> None:
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
        await self._run_secure_erase(drive, dev_mode=dev_mode, run_id=run_id)

        bb_errors = (0, 0, 0)
        bb_throughput = throughput_mod.ThroughputCollector().finalize()
        long_ok: bool | None = True
        if quick:
            logger.info("drive %s quick-mode: skipping badblocks + long test", drive.serial)
        else:
            # Phase 5: badblocks
            await self._advance(run_id, "badblocks", drive)
            bb_errors, bb_throughput = await self._run_badblocks(
                drive, dev_mode=dev_mode, run_id=run_id
            )

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
        # v0.8.0+: classify drive for workload-ceiling rule. Uses model
        # + transport + rotation_rate + optional operator overrides at
        # /etc/driveforge/drive_class_overrides.yaml. Falls through to
        # consumer-tier defaults on unknown models (tighter workload
        # ceiling by default; safer for refurb grading).
        from driveforge.core import drive_class as drive_class_mod
        dclass = drive_class_mod.classify(
            model=drive.model,
            transport=(
                drive.transport.value
                if hasattr(drive.transport, "value")
                else str(drive.transport)
            ),
            rotation_rate=drive.rotation_rate,
            # /etc/driveforge/drive_class_overrides.yaml — optional;
            # classifier gracefully falls through when the file is
            # absent or unparseable. Config-dir sibling of grading.yaml.
            overrides_path=Path("/etc/driveforge/drive_class_overrides.yaml"),
        )
        result = grading.grade_drive(
            pre=pre_snap,
            post=post_snap,
            config=self.state.settings.grading,
            short_test_passed=short_ok,
            long_test_passed=long_ok,
            badblocks_errors=bb_errors,
            max_temperature_c=max_temp,
            throughput=bb_throughput,
            drive_class=dclass,
        )
        await self._finalize_run(run_id, drive, post_snap, result, bb_throughput)
        # v0.5.5+ quick-pass fail action: prompt the operator or auto-promote
        # to a full pipeline run. Evaluated after finalize so the triage
        # verdict is committed to the DB first.
        if quick:
            self._maybe_handle_quick_pass_fail(run_id, drive)
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
        # v0.6.9+: native async subprocess via smart.snapshot_async
        # (which uses asyncio.create_subprocess_exec under the hood).
        # Replaces the v0.6.6 `run_in_executor(drive_command_executor,
        # smart.snapshot, ...)` dance — same D-state isolation (a
        # hung smartctl no longer blocks the event loop) without
        # burning a thread per call. Thread-pool path remains as a
        # safety net elsewhere; this one of the hottest sites (two
        # calls per pipeline run) and the first to migrate.
        try:
            snap = await smart.snapshot_async(drive.device_path)
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
            # v0.5.5 — denormalize the sector counters onto the run row so
            # the dashboard + API can report the healing delta without
            # having to join and parse the SmartSnapshot JSON payload.
            # Post-SMART values land in the legacy reallocated_sectors /
            # current_pending_sector columns via _finalize_run; here we
            # only populate the pre-SMART side.
            if kind == "pre":
                run = session.get(m.TestRun, run_id)
                if run is not None:
                    run.pre_reallocated_sectors = snap.reallocated_sectors
                    run.pre_current_pending_sector = snap.current_pending_sector
            session.commit()
        # v0.6.9+: _record_telemetry is async now (ipmitool call goes
        # through read_chassis_power_async). Pre-v0.6.9 this was a
        # direct sync call from async context — the last stragger
        # sync-in-async site the v0.6.6 audit missed.
        await self._record_telemetry(
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
                status = await loop.run_in_executor(self.state.drive_command_executor, smart.self_test_status, drive.device_path)
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

    async def _run_secure_erase(self, drive: Drive, *, dev_mode: bool, run_id: int | None = None) -> None:
        if dev_mode:
            await asyncio.sleep(1.0)
            return
        if drive.transport == Transport.USB:
            # Safety: never erase a USB-attached drive. Likely an external
            # boot drive or adapter, not a test target.
            raise PipelineFailure("secure_erase", "refusing to erase USB-transport drive")
        serial = drive.serial

        # v0.6.7+: pre-active state visibility. Mark the drive as active in
        # "preflight" phase BEFORE calling ensure_clean_security_state, so
        # operators see the drive on the dashboard during the
        # preflight/recovery window instead of a silent 1-2 minute gap
        # while nothing renders. The preflight call can take 60+ seconds
        # on drives stuck in LOCKED state (SAT unlock + disable-password).
        self.state.active_phase[serial] = "preflight"
        self.state.active_sublabel[serial] = "preflight: probing security state"

        # v0.6.3+: advisory-print for known-flaky drive families. ST3000DM001
        # et al — surface the heads-up in the drive log before any erase
        # machinery touches the drive, so operators understand why the
        # pipeline might hit the hdparm fallback path or take longer than
        # expected. Informational; doesn't change any behavior.
        from driveforge.core import drive_advisory
        advisory = drive_advisory.advisory_for(drive.model)
        if advisory:
            self._log(serial, f"advisory: {advisory}")

        # v0.5.0 pre-flight: ensure the drive is in a clean security state
        # before we start. Self-heals drives still enabled/locked from a
        # previous interrupted run; raises a clear user-facing EraseError
        # on genuinely unrecoverable states (BIOS-frozen, unknown user-set
        # password). No-op on SAS/NVMe.
        self.state.active_sublabel[serial] = "preflight: checking security state"
        self._log(serial, "secure_erase preflight: probing drive security state")
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                self.state.drive_command_executor,
                erase.ensure_clean_security_state,
                drive,
            )
        except erase.EraseError as exc:
            # Surface the preflight error as a pipeline failure with a
            # distinctive phase label so the failed-card shows what
            # actually blocked the erase vs. an erase that got partway in.
            raise PipelineFailure("secure_erase", f"preflight: {exc}") from exc
        self._log(serial, "secure_erase preflight: drive in CLEAN state; proceeding")

        # Surface the specific erase mechanism on the dashboard card so the
        # operator can see at a glance why some drives' activity LEDs blink
        # (SAS `sg_format` issues per-sector SCSI commands → HBA sees traffic
        # → backplane LED activity) while others stay solid (SATA drives
        # take a single ATA command and let the drive firmware handle the
        # overwrite internally → no link-level traffic → LED idle).
        mechanism = {
            Transport.SATA: "SAT passthrough SECURITY ERASE UNIT",
            Transport.SAS: "sg_format --format",
            Transport.NVME: "nvme format -s 1",
        }.get(drive.transport, "secure erase")
        self.state.active_sublabel[serial] = mechanism
        # hdparm / sg_format / nvme format emit no progress — we synthesize a
        # time-based bar from the drive's own estimate (hdparm -I) or a
        # capacity heuristic. Caps at 99% so the bar never claims done until
        # the blocking call actually returns.
        # v0.6.6+: estimate_erase_seconds runs `hdparm -I` which is a
        # subprocess call. Usually fast (<1s) but can block if the
        # drive or HBA is misbehaving — offload to the drive executor
        # so a slow drive can't block the event loop here.
        estimate = await loop.run_in_executor(
            self.state.drive_command_executor,
            erase.estimate_erase_seconds,
            drive,
        )
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

        # v0.6.3+: callback the erase layer uses to report progress
        # (SAT starting, SAT aborted → hdparm fallback, hdparm erasing,
        # etc.). Runs on the executor thread; dict-assign to
        # `active_sublabel` is thread-safe enough in CPython for UI
        # sublabels. Also feeds the per-drive rolling log tail so the
        # drive detail page shows the fallback narrative.
        def _erase_status(msg: str) -> None:
            self.state.active_sublabel[serial] = msg
            # _log is not strictly thread-safe (it may be mutating a
            # list while the main thread reads it), but list.append is
            # atomic enough under GIL for this use case. Worst case:
            # the dashboard misses one log line on a race, which is fine.
            self._log(serial, f"secure_erase: {msg}")

        # v0.6.3+: asyncio-side wall-clock outer timeout. Belt-and-
        # suspenders over sg_raw's own `-t` timeout and subprocess.run's
        # timeout. The cascade-hang we saw on the R720 (2026-04-21) had
        # the inner timeouts nominally correct, but D-state sg_raw
        # processes couldn't be killed by SIGKILL — which meant
        # subprocess.run itself hung trying to wait() on the dead child.
        # This outer timeout bypasses that: asyncio.wait_for cancels the
        # awaiting coroutine and we proceed, even if the executor thread
        # stays stuck behind. One leaked thread is far better than a
        # wedged orchestrator.
        inner_estimate = estimate or 7200
        outer_timeout_s = int(inner_estimate * 2) + 600
        try:
            await asyncio.wait_for(
                loop.run_in_executor(
                    self.state.drive_command_executor,
                    functools.partial(erase.secure_erase, drive, on_status=_erase_status),
                ),
                timeout=outer_timeout_s,
            )
            # v0.6.7+: stamp the method that actually sanitized the drive.
            # Pass-through success path = "secure_erase" (either SAT
            # directly or v0.6.3 hdparm fallback — both are real
            # ATA SECURITY ERASE UNIT, just different transports).
            if run_id is not None:
                self._stamp_sanitization_method(run_id, "secure_erase")
        except asyncio.TimeoutError as exc:
            # Outer deadline tripped. Inner timeouts should have fired
            # before this — if we're here, either sg_raw/hdparm is
            # D-state-stuck or the estimate was dramatically wrong.
            # Either way, don't keep waiting; raise and let the
            # failure path handle it.
            raise PipelineFailure(
                "secure_erase",
                f"wall-clock outer timeout ({outer_timeout_s // 60} min) — "
                f"drive may be D-state-stuck on secure_erase. Pull and "
                f"reseat the drive to release the HBA.",
            ) from exc
        except erase.EraseError as exc:
            # v0.6.3+: decode the raw SAT/hdparm error into operator-
            # facing cause + next-step text. The decoder falls through
            # to a generic wrapper for patterns it doesn't recognize,
            # so we always get a useful message (just less specific
            # for novel patterns).
            from driveforge.core import ata_errors
            decoded = ata_errors.decode_secure_erase_error(str(exc))

            # v0.6.7+: HDD badblocks-only sanitization fallback. When the
            # error is the libata-freeze signature (both SAT and hdparm
            # paths refused with ABRT — which we've observed is 100% of
            # ST3000DM001 inserts on libata-using hosts), AND the drive
            # is a rotating HDD (rotation_rate > 0), the pipeline can
            # safely continue to badblocks. A 4-pattern destructive
            # overwrite IS NIST 800-88 Clear for magnetic media — a
            # legitimate sanitization method. For SSDs this fallback is
            # unsafe (wear leveling may leave stale NAND pages) so we
            # keep the original failure for them.
            is_hdd = drive.rotation_rate and drive.rotation_rate > 0
            is_libata_freeze = erase.is_libata_freeze_pattern(str(exc))
            if is_hdd and is_libata_freeze:
                self._log(
                    serial,
                    "secure_erase unavailable (libata freeze detected) — HDD "
                    "proceeding via badblocks 4-pattern overwrite (NIST 800-88 "
                    "Clear for magnetic media)",
                )
                self.state.active_sublabel[serial] = (
                    "secure_erase unavailable → proceeding via badblocks overwrite"
                )
                if run_id is not None:
                    self._stamp_sanitization_method(run_id, "badblocks_overwrite")
                # Return normally — the pipeline continues to the
                # badblocks phase which will do the actual sanitization.
                return

            # v0.6.9+: SSDs hitting the same libata-freeze pattern CAN'T
            # safely fall back to badblocks (wear leveling + NIST 800-88
            # flash exclusion). Register the drive for operator-facing
            # remediation: dashboard renders a structured checklist of
            # bypass paths (USB-SATA enclosure, Dell Lifecycle, vendor
            # tools, PSID reset, or destroy). Pipeline still fails with
            # the decoded message — nothing auto-runs — but the operator
            # gets an actionable panel instead of raw prose.
            if not is_hdd and is_libata_freeze:
                from driveforge.core import frozen_remediation
                rem_state = frozen_remediation.register_freeze(
                    self.state.frozen_remediation,
                    serial=serial,
                    drive_model=drive.model or "unknown",
                )
                if rem_state.retry_count == 0:
                    self._log(
                        serial,
                        "SSD frozen by libata — structured remediation panel "
                        "available on drive detail page (USB enclosure, "
                        "Lifecycle Controller, vendor ISO, PSID reset, destroy)",
                    )
                else:
                    self._log(
                        serial,
                        f"SSD frozen by libata AGAIN after retry #{rem_state.retry_count} "
                        f"— remediation panel escalated to destruction-recommended tone",
                    )

            # v0.9.0+: drives locked with an unknown password (some prior
            # tool / host set a SECURITY password that DriveForge's default
            # + the vendor-factory-master auto-recovery path both couldn't
            # clear). Sibling path to the frozen-SSD remediation above.
            # Panel offers PSID-revert guidance, manual-password unlock,
            # and mark-as-unrecoverable.
            if erase.is_security_locked_pattern(str(exc)):
                from driveforge.core import password_locked_remediation as pwd_lock
                lock_state = pwd_lock.register_locked(
                    self.state.password_locked,
                    serial=serial,
                    drive_model=drive.model or "unknown",
                )
                if lock_state.retry_count == 0:
                    self._log(
                        serial,
                        "drive is security-locked by unknown password + factory-"
                        "master auto-recovery failed — remediation panel available "
                        "on drive detail page (PSID revert, manual password, "
                        "mark unrecoverable)",
                    )
                else:
                    self._log(
                        serial,
                        f"drive STILL security-locked after operator retry #"
                        f"{lock_state.retry_count} — panel escalated. "
                        f"{lock_state.attempts_remaining_estimate} manual attempts "
                        f"estimated remaining before drive locks out permanently.",
                    )

            failure_msg = f"{decoded.cause} — {decoded.next_step}"
            self._log(serial, f"secure_erase failed: {decoded.cause}")
            self._log(serial, f"  → {decoded.next_step}")
            raise PipelineFailure("secure_erase", failure_msg) from exc
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

    async def _run_badblocks(
        self, drive: Drive, *, dev_mode: bool, run_id: int
    ) -> tuple[tuple[int, int, int], throughput_mod.ThroughputStats]:
        """Run the 8-pass destructive badblocks sweep.

        Returns `(errors, throughput_stats)` where `errors` is the
        legacy (read, write, compare) tuple and `throughput_stats` is
        the v0.5.6+ per-pass throughput summary. stats.mean_mbps is
        None when diskstats was unavailable for this device (e.g.
        some USB-SATA bridges); callers should treat it as "no
        throughput data" rather than as zero.
        """
        if dev_mode:
            for pct in range(0, 101, 20):
                self.state.active_percent[drive.serial] = float(pct)
                await asyncio.sleep(0.1)
            return ((0, 0, 0), throughput_mod.ThroughputCollector().finalize())
        serial = drive.serial
        self._log(
            serial,
            f"badblocks -wsv -b {badblocks.BLOCK_SIZE} -c {badblocks.BLOCK_COUNT} "
            f"{drive.device_path}",
        )
        # Throttle log updates to every 10% so we don't fill the buffer
        last_logged_pct = [0.0]
        last_pass_label = [""]

        # v0.5.6+ throughput collector. The on_progress callback writes
        # the current pass label here; a background sampler task reads
        # state.active_io_rate every few seconds and writes samples
        # tagged with that label. At end of badblocks the collector is
        # finalized into per-pass means + overall percentiles, which
        # feed the new "throughput consistency" grading rules.
        collector = throughput_mod.ThroughputCollector()

        def on_progress(
            pct: float,
            errs: tuple[int, int, int],
            pass_label: str | None,
        ) -> None:
            self.state.active_percent[serial] = float(pct)
            if pass_label is not None:
                self.state.active_sublabel[serial] = pass_label
                collector.note_pass(pass_label)
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

        sampler_task = asyncio.create_task(
            self._throughput_sampler_loop(collector, serial),
            name=f"throughput-sampler:{serial}",
        )

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
        finally:
            sampler_task.cancel()
            try:
                await sampler_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        stats = collector.finalize()
        log_line = f"badblocks complete: errors={errs[0]}/{errs[1]}/{errs[2]}"
        if stats.mean_mbps is not None:
            log_line += (
                f"; throughput mean={stats.mean_mbps:.0f} MB/s "
                f"(p5={stats.p5_mbps:.0f}, p95={stats.p95_mbps:.0f}, "
                f"{stats.sample_count} samples across {len(stats.per_pass_means)} pass(es))"
            )
        self._log(serial, log_line)
        return errs, stats

    async def _throughput_sampler_loop(
        self,
        collector: throughput_mod.ThroughputCollector,
        serial: str,
        *,
        interval_s: float = 3.0,
    ) -> None:
        """Periodic sampler that correlates diskstats output (written
        to state.active_io_rate by _poll_io_rates) with the current
        badblocks pass label (written to the collector by the
        on_progress callback).

        Runs for the lifetime of one badblocks phase; cancelled by
        _run_badblocks' finally block. Uses max(read, write) to get
        the active I/O direction regardless of whether badblocks is
        in a write-pattern pass or a verify-read pass.
        """
        while True:
            try:
                rate = self.state.active_io_rate.get(serial, {})
                # badblocks alternates write and verify-read passes;
                # the max captures whichever direction is active.
                w = float(rate.get("write_mbps", 0.0) or 0.0)
                r = float(rate.get("read_mbps", 0.0) or 0.0)
                sample = max(w, r)
                collector.note_sample(sample)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                # A transient dict-read error must not kill the sampler;
                # the stats are advisory, not load-bearing.
                logger.exception(
                    "throughput sampler tick failed for %s; continuing", serial
                )
            try:
                await asyncio.sleep(interval_s)
            except asyncio.CancelledError:
                return

    def _maybe_handle_quick_pass_fail(self, run_id: int, drive: Drive) -> None:
        """v0.5.5+ \u2014 act on a quick-pass triage=fail verdict per the
        operator's `settings.daemon.quick_pass_fail_action` preference.

        Three modes:
          badge_only  \u2014 no-op here; the dashboard already surfaces the
                        triage-fail badge. Default.
          prompt      \u2014 add serial to state.promote_prompts so the
                        dashboard card renders a "Run full pipeline?"
                        banner with Yes / Dismiss actions.
          auto_promote \u2014 schedule a full-pipeline start_batch on this
                         same drive after a short delay (to let the
                         current run's cleanup finish).

        Called from _execute_pipeline immediately after _finalize_run,
        only when quick=True. Reads the persisted triage_result so the
        verdict is authoritative (not a function argument that could
        drift out of sync).
        """
        action = (self.state.settings.daemon.quick_pass_fail_action or "badge_only").lower()
        if action not in ("prompt", "auto_promote"):
            return
        with self.state.session_factory() as session:
            run = session.get(m.TestRun, run_id)
            if run is None or not run.quick_mode or run.triage_result != "fail":
                return

        if action == "prompt":
            self.state.promote_prompts.add(drive.serial)
            logger.info(
                "quick-pass triage=fail for %s \u2014 prompt mode; awaiting operator decision",
                drive.serial,
            )
            return

        # auto_promote
        async def _promote():
            # Brief delay so the current run's finally block pops this
            # drive from active_phase / _tasks before start_batch tries
            # to register it.
            await asyncio.sleep(1.0)
            try:
                await self.start_batch(
                    [drive],
                    source="auto-promote after quick-pass triage=fail",
                    quick=False,
                )
                logger.info(
                    "quick-pass triage=fail for %s \u2014 auto-promoted to full pipeline",
                    drive.serial,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "auto-promote to full pipeline failed for %s after quick-pass triage=fail",
                    drive.serial,
                )
        asyncio.create_task(_promote())

    async def _telemetry_sampler_loop(self, run_id: int, drive: Drive) -> None:
        """Periodic telemetry sampler for one active pipeline run.

        Emits a TelemetrySample row every
        `settings.daemon.telemetry_sample_interval_s` seconds until
        cancelled by `_execute_pipeline`'s finally block. Captures
        chassis power (cheap — one ipmitool call) and drive
        temperature (via a lightweight smartctl read).

        Prior to v0.5.5 telemetry was only written at SMART-snapshot
        phase boundaries (pre and post), producing 2-sample charts on
        multi-hour runs. This loop fills in the gaps so the telemetry
        charts actually tell a story \u2014 warm-up curve during erase,
        thermal plateau during badblocks, spikes during self-tests.
        """
        interval = self.state.settings.daemon.telemetry_sample_interval_s
        # Defensive bound so a misconfigured 0 or negative value doesn't
        # turn this into a tight busy-loop.
        interval = max(5, int(interval or 30))

        # A brief pre-sleep so we don't race the pre-SMART phase's own
        # telemetry write \u2014 no need to sample at t=0 and t=0.01.
        try:
            await asyncio.sleep(min(interval, 10))
        except asyncio.CancelledError:
            return

        # v0.6.9+: native-async subprocess path. Calls smart.snapshot_async
        # + telemetry.read_chassis_power_async directly instead of
        # offloading their sync twins to the drive-command executor.
        # Same D-state isolation (asyncio.wait_for with timeout cancels
        # the child process cleanly even when it's Ds waiting on SG
        # I/O — the kernel task can't be SIGKILL'd but the asyncio
        # child handle can be released) without burning a thread per
        # tick. Hottest site in the codebase: one tick per 30 s per
        # active pipeline = dozens of subprocess spawns per minute
        # under realistic batch load.
        while True:
            try:
                phase = self.state.active_phase.get(drive.serial, "unknown")
                temp = await self._sample_drive_temp_quietly_async(drive)
                await self._record_telemetry(
                    run_id,
                    drive.serial,
                    phase=phase,
                    drive_temp_c=temp,
                )
            except asyncio.CancelledError:
                # Propagate \u2014 the finally block in _execute_pipeline
                # is waiting for us to exit.
                raise
            except Exception:  # noqa: BLE001
                # A transient smartctl / ipmitool hiccup must NOT kill
                # the sampler. Log once per tick and keep going.
                logger.exception(
                    "telemetry sampler tick failed for %s (run_id=%s); continuing",
                    drive.serial,
                    run_id,
                )

            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return

    def _sample_drive_temp_quietly(self, drive: Drive) -> int | None:
        """Fetch current drive temperature for a telemetry tick (sync).

        Falls back to None on smartctl errors / timeouts \u2014 telemetry is
        best-effort, a temperature gap is a smaller sin than crashing
        the sampler. Heavy smartctl reads are acceptable at 30 s
        cadence; badblocks' own I/O cost dwarfs a periodic SMART query.

        Kept for sync callers (CLI, tests). Async code uses
        `_sample_drive_temp_quietly_async` (v0.6.9+).
        """
        try:
            snap = smart.snapshot(drive.device_path, timeout=10.0)
        except Exception:  # noqa: BLE001
            return None
        return snap.temperature_c

    async def _sample_drive_temp_quietly_async(self, drive: Drive) -> int | None:
        """v0.6.9+: async twin of `_sample_drive_temp_quietly`. Preferred
        from inside the event loop. Uses smart.snapshot_async, so
        smartctl's subprocess is owned by asyncio rather than a
        run_in_executor thread."""
        try:
            snap = await smart.snapshot_async(drive.device_path, timeout=10.0)
        except Exception:  # noqa: BLE001
            return None
        return snap.temperature_c

    async def _record_telemetry(
        self,
        run_id: int,
        drive_serial: str,
        *,
        phase: str,
        drive_temp_c: int | None,
    ) -> None:
        """v0.6.9+: now async. Previous sync body blocked the event
        loop on `telemetry.read_chassis_power` (ipmitool subprocess)
        every time `_capture_smart` called it directly — one of the
        sync-in-async sites v0.6.6 partially fixed (the sampler
        path) but left broken in the _capture_smart direct-call
        path. Converting the whole method to async closes the leak
        + lets the sampler drop its run_in_executor wrap.

        Surface the live temp on the dashboard, then fetch chassis
        power via the async ipmitool twin, then persist. Best-effort
        throughout: any exception inside this path must propagate so
        the caller's except handler logs it — but nothing in here
        should raise under normal operation."""
        if drive_temp_c is not None:
            self.state.active_drive_temp[drive_serial] = drive_temp_c
        chassis_w = await telemetry.read_chassis_power_async()
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
        throughput_stats: throughput_mod.ThroughputStats | None = None,
    ) -> None:
        with self.state.session_factory() as session:
            run = session.get(m.TestRun, run_id)
            if run is None:
                return
            run.completed_at = datetime.now(UTC)
            run.power_on_hours_at_test = post_snap.power_on_hours
            run.reallocated_sectors = post_snap.reallocated_sectors
            run.current_pending_sector = post_snap.current_pending_sector
            run.offline_uncorrectable = post_snap.offline_uncorrectable
            run.smart_status_passed = post_snap.smart_status_passed
            run.rules = [rule.model_dump() for rule in result.rules]
            run.report_url = f"/reports/{drive.serial}"

            # v0.8.0+ buyer-transparency fields: lifetime I/O, wear,
            # error-class counters, self-test-log summary. All sourced
            # from the post-SMART snapshot the `grade_drive` call above
            # just scored. NULL on drives that don't report the signal,
            # same as the snapshot itself.
            run.lifetime_host_reads_bytes = post_snap.lifetime_host_reads_bytes
            run.lifetime_host_writes_bytes = post_snap.lifetime_host_writes_bytes
            run.wear_pct_used = post_snap.wear_pct_used
            run.available_spare_pct = post_snap.available_spare_pct
            run.end_to_end_error_count = post_snap.end_to_end_error_count
            run.command_timeout_count = post_snap.command_timeout_count
            run.reallocation_event_count = post_snap.reallocation_event_count
            run.nvme_critical_warning = post_snap.nvme_critical_warning
            run.nvme_media_errors = post_snap.nvme_media_errors
            run.self_test_has_past_failure = post_snap.self_test_has_past_failure
            # Drive class (classifier output fed to grade_drive earlier
            # in _execute_pipeline). Re-classify here so _finalize_run
            # works standalone (e.g. called from a regrade path that
            # skipped the earlier phases).
            try:
                from driveforge.core import drive_class as drive_class_mod
                run.drive_class = drive_class_mod.classify(
                    model=drive.model,
                    transport=(
                        drive.transport.value
                        if hasattr(drive.transport, "value")
                        else str(drive.transport)
                    ),
                    rotation_rate=drive.rotation_rate,
                    overrides_path=Path("/etc/driveforge/drive_class_overrides.yaml"),
                )
            except Exception:  # noqa: BLE001
                # Classifier must never block a finalize.
                logger.exception("drive_class classify failed for %s", drive.serial)
            # v0.5.6+ throughput stats. NULL when the run didn't go
            # through badblocks (quick-pass), or diskstats wasn't
            # available for this device.
            if throughput_stats is not None and throughput_stats.mean_mbps is not None:
                run.throughput_mean_mbps = throughput_stats.mean_mbps
                run.throughput_p5_mbps = throughput_stats.p5_mbps
                run.throughput_p95_mbps = throughput_stats.p95_mbps
                run.throughput_pass_means = list(throughput_stats.per_pass_means)
            # v0.5.5 — verdict depends on pipeline mode:
            #   quick_mode=True  -> triage verdict (Clean/Watch/Fail); grade stays NULL
            #   quick_mode=False -> A/B/C/F grade as before; triage_result stays NULL
            # Rationale: quick pass skips badblocks + long self-test, so it
            # can't honestly award a certification grade. Triage is the
            # honest verdict for a fast check.
            if run.quick_mode:
                triage = triage_mod.triage_quick_pass(
                    pre_pending=run.pre_current_pending_sector,
                    post_pending=post_snap.current_pending_sector,
                    pre_reallocated=run.pre_reallocated_sectors,
                    post_reallocated=post_snap.reallocated_sectors,
                )
                run.triage_result = triage.verdict.value
                run.grade = None
                self._log(
                    drive.serial,
                    f"quick-pass triage: {triage.verdict.value} \u2014 {triage.summary}",
                )
            else:
                run.grade = result.grade.value
                run.triage_result = None
            session.commit()

            # v0.6.9+: a successful pipeline completion clears any
            # frozen-SSD remediation entry for this serial. If the
            # operator's remediation (USB enclosure round-trip, etc.)
            # cleared the freeze and the retry pipeline graded the
            # drive, we don't want the remediation panel to linger.
            # No-op if the serial was never registered.
            try:
                from driveforge.core import frozen_remediation
                frozen_remediation.clear(self.state.frozen_remediation, drive.serial)
            except Exception:  # noqa: BLE001
                # Defensive: never fail a finalize on a housekeeping
                # miss. The next orchestrator cycle will retry.
                logger.exception(
                    "frozen_remediation.clear failed for %s (non-fatal)", drive.serial
                )

            # v0.9.0+: same housekeeping for password-locked remediation
            # state. A successful pipeline completion means the drive is
            # now accessible (operator unlocked it manually, or the
            # factory-master auto-recovery DID work and this is the
            # post-recovery pipeline run), so the remediation panel
            # should no longer render.
            try:
                from driveforge.core import password_locked_remediation as pwd_lock
                pwd_lock.clear(self.state.password_locked, drive.serial)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "password_locked clear failed for %s (non-fatal)", drive.serial
                )

            # v0.6.4+: auto-print the cert label now that the run is
            # finalized + graded. Only fires on full pipelines (not
            # quick-pass triage — triage verdicts don't produce a
            # cert) and only when a printer is configured with
            # auto_print enabled (Settings → Printer toggle).
            #
            # Print failures do NOT fail the run: the drive's grade
            # stands, only the sticker didn't come out. Operator can
            # click Print Label manually once they fix the printer
            # issue. Print status goes to the drive's log so the
            # dashboard shows why the sticker didn't print.
            if run.grade and not run.quick_mode:
                pc = self.state.settings.printer
                if pc.model and getattr(pc, "auto_print", True):
                    from driveforge.core import printer as printer_mod
                    self.state.active_sublabel[drive.serial] = "printing cert label..."
                    self._log(drive.serial, "auto-print: printing cert label")
                    loop = asyncio.get_event_loop()
                    try:
                        ok, msg = await loop.run_in_executor(
                            self.state.drive_command_executor,
                            functools.partial(
                                printer_mod.auto_print_cert_for_run,
                                self.state, drive, run,
                            ),
                        )
                        self._log(drive.serial, f"auto-print: {msg}")
                        if not ok:
                            logger.warning(
                                "auto-print failed for %s: %s",
                                drive.serial, msg,
                            )
                    except Exception:  # noqa: BLE001
                        # Defensive: never let a print bug fail the
                        # finalize path. Log + continue.
                        logger.exception(
                            "auto-print raised unexpectedly for %s — "
                            "drive's grade stands; operator can reprint manually",
                            drive.serial,
                        )
                        self._log(
                            drive.serial,
                            "auto-print: crashed unexpectedly (see daemon log); "
                            "grade unchanged, click Print Label to retry",
                        )

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
            # v0.5.1+ totals vocabulary: A/B/C pass tiers, F for
            # real drive-fail, error for pipeline-error. Legacy "fail"
            # bucket kept so pre-v0.5.1 rows still appear in totals
            # (rather than silently dropping from the count).
            totals = {"A": 0, "B": 0, "C": 0, "F": 0, "error": 0, "fail": 0}
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
