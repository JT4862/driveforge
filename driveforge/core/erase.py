"""Secure erase dispatch.

One entry point — `secure_erase(drive)` — picks the right mechanism based on
transport:

- SATA: SAT passthrough — ATA SECURITY ERASE UNIT wrapped in
        ATA-PASS-THROUGH(16) SCSI CDB, issued via `sg_raw`. Replaces
        `hdparm --security-erase` as of v0.3.0; the legacy hdparm
        path uses `HDIO_DRIVE_TASKFILE`, which modern Debian kernels
        no longer provide on SAS-attached drives (CONFIG_IDE_TASK_IOCTL
        error). See `driveforge.core.sat_passthru` for the SAT details.
- SAS:  `sg_format --format`
- NVMe: `nvme format -s 1`

All are destructive. The orchestrator is responsible for confirming intent
before calling this.
"""

from __future__ import annotations

import logging
import re
from enum import Enum

from driveforge.core import sat_passthru
from driveforge.core.drive import Drive, Transport
from driveforge.core.process import run
from driveforge.core.timing import capacity_timeout

logger = logging.getLogger(__name__)


class EraseError(RuntimeError):
    pass


# Parses `hdparm -I` security stanza. Examples:
#   "2min for SECURITY ERASE UNIT. 2min for ENHANCED SECURITY ERASE UNIT."
#   "128min for SECURITY ERASE UNIT. 90min for ENHANCED SECURITY ERASE UNIT."
# Some drives report nothing, some report hours — bail out and fall back to
# a capacity heuristic if this doesn't match.
_SATA_SE_TIME_RE = re.compile(
    r"(\d+)\s*min\s+for\s+SECURITY\s+ERASE\s+UNIT", re.IGNORECASE
)


def _sata_estimated_seconds(device: str) -> int | None:
    try:
        r = run(["hdparm", "-I", device], timeout=10)
    except Exception:  # noqa: BLE001
        return None
    if not r.ok:
        return None
    m = _SATA_SE_TIME_RE.search(r.stdout)
    if not m:
        return None
    return max(60, int(m.group(1)) * 60)


def estimate_erase_seconds(drive: Drive) -> int | None:
    """Return a best-guess wall-clock for the secure-erase phase.

    Used by the orchestrator to drive a time-based progress bar for a phase
    that emits no native progress. Returns None when we genuinely can't
    estimate — caller should show an indeterminate/busy state instead of a
    misleading 0%.
    """
    if drive.transport == Transport.SATA:
        # Ask the drive how long SECURITY ERASE UNIT is expected to take.
        est = _sata_estimated_seconds(drive.device_path)
        if est is not None:
            return est
        # Fall through to capacity heuristic below.
    if drive.transport == Transport.NVME:
        # NVMe format is typically a few seconds even on multi-TB drives
        # (crypto-erase), but give ourselves headroom.
        return 60
    # SAS sg_format and the SATA fallback: estimate from capacity. These are
    # intentionally pessimistic so the bar under-reports rather than lying
    # past 100%. Real sg_format on a 300 GB SAS runs 15-60 min.
    if drive.capacity_bytes:
        gb = drive.capacity_bytes / 1_000_000_000
        # 20 min per 100 GB, clamped to [5 min, 6 h]
        seconds = int((gb / 100) * 20 * 60)
        return max(5 * 60, min(seconds, 6 * 3600))
    return None


class SataSecurityState(str, Enum):
    """Possible SATA security states visible via `hdparm -I`."""

    CLEAN = "clean"       # security not enabled — fresh drive, ready to erase
    ENABLED = "enabled"   # password set but drive is not locked — can DISABLE with right password
    LOCKED = "locked"     # password set AND drive locked (post-power-cycle state)
    FROZEN = "frozen"     # BIOS issued SECURITY FREEZE LOCK; cannot unlock via software
    UNKNOWN = "unknown"   # hdparm -I failed or output was unparseable


def _parse_sata_security_state(hdparm_i_output: str) -> SataSecurityState:
    """Extract the drive's security state from `hdparm -I` stdout.

    hdparm prints a `Security:` stanza with lines like:
        	not	enabled
        	not	locked
        	not	frozen
    (tab-separated). Parsing via substring match is brittle because
    'not enabled' and 'enabled' both contain the word 'enabled'; we use
    the tab prefix as a cheap but reliable anchor.
    """
    out = hdparm_i_output.lower()
    is_frozen = "\tfrozen" in out and "not\tfrozen" not in out
    is_locked = "\tlocked" in out and "not\tlocked" not in out
    is_enabled = "\tenabled" in out and "not\tenabled" not in out

    # Order matters: frozen beats locked beats enabled. A frozen drive
    # will typically also report "enabled" in hdparm's output; what
    # matters operationally is that we can't do anything security-side
    # until the BIOS-induced freeze is cleared.
    if is_frozen:
        return SataSecurityState.FROZEN
    if is_locked:
        return SataSecurityState.LOCKED
    if is_enabled:
        return SataSecurityState.ENABLED
    return SataSecurityState.CLEAN


def _probe_sata_security_state(device: str) -> SataSecurityState:
    """Run `hdparm -I <device>` and return the parsed security state.

    hdparm -I is read-only — it uses HDIO_GET_IDENTITY which still
    works on modern kernels (it's the HDIO_DRIVE_TASKFILE ioctl for
    destructive commands that was removed). So we keep using hdparm
    for this probe even though the actual erase went through SAT
    passthrough in v0.3.0+.
    """
    result = run(["hdparm", "-I", device], timeout=10)
    if not result.ok:
        logger.warning(
            "hdparm -I failed on %s (rc=%d): %s",
            device, result.returncode, (result.stderr or "").strip(),
        )
        return SataSecurityState.UNKNOWN
    return _parse_sata_security_state(result.stdout or "")


def ensure_clean_security_state(drive: Drive) -> None:
    """Self-healing pre-flight for the SATA secure_erase phase (v0.5.0+).

    Before kicking off SET PASSWORD → PREPARE → ERASE UNIT, verify the
    drive is in a sane security state. Auto-resolve what we can
    (drives still enabled/locked from a previous interrupted run —
    disable the password, start clean), refuse with a clear user-
    facing explanation on what we can't (frozen BIOS, unknown
    password set by another tool). Net effect: no code path left
    where the operator has to SSH in and hand-run hdparm.

    No-op for SAS and NVMe — they don't have ATA security state.
    SAS drives behind a SAS HBA sometimes report as "sas" via lsblk
    but speak ATA; we refine via smartctl first so we run the right
    pre-flight.

    Raises EraseError on any genuinely-unrecoverable state. The error
    message is written for the operator, not the developer — it
    includes what state the drive is in, why we can't handle it
    automatically, and what they should do next.
    """
    effective = drive.transport
    if effective == Transport.SAS:
        from driveforge.core.drive import detect_true_transport
        refined = detect_true_transport(drive.device_path)
        if refined in (Transport.SATA, Transport.SAS, Transport.NVME):
            effective = refined

    if effective != Transport.SATA:
        # SAS and NVMe have no ATA security state — no pre-flight needed.
        return

    state = _probe_sata_security_state(drive.device_path)
    logger.info(
        "secure_erase preflight: %s (%s) reports security state = %s",
        drive.device_path, drive.serial, state.value,
    )

    if state == SataSecurityState.CLEAN:
        return  # Nothing to do.

    if state == SataSecurityState.UNKNOWN:
        raise EraseError(
            f"preflight: could not read security state from {drive.device_path} "
            f"(hdparm -I failed). Drive may be unresponsive, cabling may be "
            f"loose, or the HBA may have lost the drive. Check "
            f"`dmesg | tail` and `lsblk` for the drive's current state."
        )

    if state == SataSecurityState.FROZEN:
        raise EraseError(
            f"preflight: drive {drive.device_path} is in FROZEN security state. "
            f"The system BIOS issued SECURITY FREEZE LOCK during POST, which "
            f"cannot be undone via software. Options: (1) reboot into a BIOS "
            f"that has 'security freeze lock' disabled, (2) on some chassis, "
            f"hot-remove + re-insert the drive (power cycles just the drive, "
            f"clearing the freeze), (3) replace the drive."
        )

    # state is LOCKED or ENABLED. Both mean "our previous run (or someone
    # else) set a security password." Try to clear it with our known
    # throwaway password; surface a clear error if that fails (drive has
    # a user-set password we don't know).

    if state == SataSecurityState.LOCKED:
        logger.info(
            "preflight: attempting SAT unlock on %s with default password",
            drive.device_path,
        )
        try:
            sat_passthru.security_unlock(
                drive.device_path, owner=drive.serial,
            )
        except sat_passthru.SatPassthruError as exc:
            raise EraseError(
                f"preflight: drive {drive.device_path} is security-locked with "
                f"an unknown password. DriveForge's default password "
                f"('{sat_passthru.DEFAULT_PASSWORD}') did not unlock it, which "
                f"means another tool set this password — DriveForge cannot "
                f"erase drives with passwords it doesn't know. Options: "
                f"(1) boot the drive on a system that knows the password and "
                f"issue SECURITY DISABLE there, (2) if the drive supports "
                f"TCG Opal/SED, issue a PSID-based factory reset with the "
                f"label PSID (not yet supported in DriveForge), (3) replace "
                f"the drive. Underlying error: {exc}"
            ) from exc
        # Unlock succeeded → drive is now enabled-but-not-locked. Fall through.
        logger.info("preflight: SAT unlock succeeded on %s", drive.device_path)

    # state is ENABLED (either originally, or after unlock).
    logger.info(
        "preflight: attempting SAT disable-password on %s with default password",
        drive.device_path,
    )
    try:
        sat_passthru.security_disable_password(
            drive.device_path, owner=drive.serial,
        )
    except sat_passthru.SatPassthruError as exc:
        raise EraseError(
            f"preflight: drive {drive.device_path} has security enabled but "
            f"DriveForge could not disable it with its default password "
            f"('{sat_passthru.DEFAULT_PASSWORD}'). This typically means the "
            f"password was set by another tool. See the locked-state recovery "
            f"options in the docs (/hardware/known-issues). Underlying "
            f"error: {exc}"
        ) from exc

    logger.info(
        "preflight: %s is now in CLEAN security state, ready for fresh erase",
        drive.device_path,
    )


def _sata_secure_erase(
    device: str,
    password: str = sat_passthru.DEFAULT_PASSWORD,
    *,
    owner: str | None = None,
    capacity_bytes: int | None = None,
    on_status=None,
) -> None:
    """SAT-passthrough secure erase for SATA drives with hdparm fallback.

    Primary path: SAT passthrough via `sg_raw` + ATA-PASS-THROUGH(16)
    (v0.3.0+). Works on modern Debian kernels where the old hdparm
    `HDIO_DRIVE_TASKFILE` ioctl is gone.

    Fallback path (v0.6.3+): if the SAT `SECURITY ERASE UNIT`
    command aborts (`SatPassthruError` containing "SECURITY ERASE
    UNIT" + "Aborted"), retry via `hdparm --user-master u
    --security-erase`. We discovered on a ST4000NM0033 on JT's R720
    (2026-04-21) that some drives' SAT translation layer refuses our
    CDB for ERASE UNIT specifically while happily accepting the
    identical ATA command issued directly via hdparm. Rather than
    grade the drive F on a transport-layer refusal, v0.6.3 retries
    via the native-ATA hdparm path. Same password, same drive-side
    semantics, different kernel path.
    """
    # Timeout preference: (1) drive's own hdparm-announced estimate × 1.5 if
    # present — vendor firmware knows the drive better than our blanket
    # capacity model — (2) capacity-based fallback otherwise. hdparm -I
    # uses HDIO_GET_IDENTITY which still works on modern kernels (it's
    # only the legacy IDE *task* ioctl that was removed); we keep using
    # it for the time estimate even though the actual erase has moved to
    # SAT passthrough. No arbitrary upper cap — if an 8 TB drive needs
    # 40 h, give it 40 h. The operator can abort from the dashboard if
    # something's genuinely hung.
    est = _sata_estimated_seconds(device)
    if est is not None:
        timeout_s = max(3600, int(est * 1.5))
    else:
        timeout_s = capacity_timeout(capacity_bytes, passes=1)
    def _notify(msg: str) -> None:
        """Best-effort status callback. Runs in the executor thread on
        the orchestrator's behalf; never raised back to the erase
        logic even if the callback bugs out."""
        if on_status is None:
            return
        try:
            on_status(msg)
        except Exception:  # noqa: BLE001
            logger.exception("on_status callback raised (ignored)")

    try:
        _notify("SAT passthrough secure erase starting")
        sat_passthru.sat_secure_erase(
            device, password=password, timeout_s=timeout_s, owner=owner
        )
        _notify("SAT passthrough secure erase completed")
        return
    except sat_passthru.SatPassthruError as exc:
        if _is_sat_erase_unit_abort(str(exc)):
            logger.warning(
                "secure_erase: SAT ERASE UNIT aborted on %s — falling back to "
                "hdparm native-ATA path (v0.6.3+). Underlying: %s",
                device, exc,
            )
            _notify(
                "SAT ERASE UNIT aborted — falling back to hdparm native-ATA"
            )
            try:
                _sata_secure_erase_hdparm(
                    device,
                    password=password,
                    timeout_s=timeout_s,
                    owner=owner,
                    on_status=_notify,
                )
                logger.info(
                    "secure_erase: hdparm fallback succeeded on %s "
                    "(SAT path refused; hdparm accepted)",
                    device,
                )
                _notify("hdparm fallback secure erase completed")
                return
            except EraseError as hdparm_exc:
                # Both paths refused — this is a legitimate drive-refusal.
                # Surface both errors so the decoder has full context.
                raise EraseError(
                    f"Both SAT and hdparm secure-erase refused. "
                    f"SAT: {exc}. hdparm: {hdparm_exc}"
                ) from hdparm_exc
        # Non-ABRT failure from SAT — re-wrap as EraseError without
        # fallback (hdparm wouldn't help with transport-level failures
        # like "sg_raw returned non-zero before reaching the drive").
        raise EraseError(str(exc)) from exc


def _is_sat_erase_unit_abort(err_text: str) -> bool:
    """True iff the SAT passthrough error looks like an ERASE UNIT
    ABRT — the specific case v0.6.3 hdparm-fallback handles.

    We match on substrings rather than parsing the full error
    because sg_raw's error format varies slightly across versions
    and we'd rather err on the side of triggering the fallback
    (hdparm failing again is cheap) than missing it (operator sees
    F grade they shouldn't).
    """
    t = (err_text or "").lower()
    if "security erase unit" not in t:
        return False
    return (
        "aborted command" in t
        or "aborted" in t
        or "error=0x4" in t
        or "check condition" in t
    )


def _sata_secure_erase_hdparm(
    device: str,
    *,
    password: str,
    timeout_s: int,
    owner: str | None,
    on_status=None,
) -> None:
    """Native-ATA secure-erase via hdparm. The v0.6.3 fallback path for
    drives that refuse SAT passthrough ERASE UNIT.

    Two-command sequence (same drive-side flow as SAT, different
    kernel path to get there):
      1. `hdparm --user-master u --security-set-pass PW DEVICE`
      2. `hdparm --user-master u --security-erase PW DEVICE`

    Blocks until the erase completes (hdparm's own wait) or the
    timeout fires. `hdparm --security-erase` returns only after the
    drive reports the erase finished; for HDDs that can be many hours.
    Same `owner` mechanism as the SAT path so the kill-on-abort
    machinery works consistently.
    """
    def _notify(msg: str) -> None:
        if on_status is None:
            return
        try:
            on_status(msg)
        except Exception:  # noqa: BLE001
            logger.exception("on_status callback raised (ignored)")

    # Step 1: set password. If the drive's already in enabled state
    # (because we're retrying after SAT SET PASSWORD partially worked),
    # hdparm returns an error we can ignore — the important thing is
    # that the password is set and known to us.
    _notify("hdparm: setting security password")
    set_argv = [
        "hdparm", "--user-master", "u",
        "--security-set-pass", password, device,
    ]
    r = run(set_argv, timeout=60, owner=owner)
    if not r.ok and "already" not in (r.stderr or "").lower():
        # Genuine failure (not just "password already set"). Log but
        # proceed — if the password is wrong, the erase step will
        # fail with a distinctive error that propagates up.
        logger.warning(
            "hdparm --security-set-pass failed on %s (rc=%d): %s",
            device, r.returncode, (r.stderr or "").strip(),
        )

    # Step 2: the actual erase. hdparm handles SECURITY ERASE PREPARE
    # + SECURITY ERASE UNIT internally in the right order; we don't
    # need to split them. Blocks until the drive reports done.
    _notify("hdparm: issuing SECURITY ERASE (blocking until drive reports done)")
    erase_argv = [
        "hdparm", "--user-master", "u",
        "--security-erase", password, device,
    ]
    r = run(erase_argv, timeout=timeout_s + 60, owner=owner)
    if not r.ok:
        raise EraseError(
            f"hdparm --security-erase failed on {device} "
            f"(rc={r.returncode}): {(r.stderr or r.stdout or '').strip() or 'non-zero exit'}"
        )


def wait_for_prior_erase_completion(
    drive: Drive,
    *,
    poll_interval_s: int = 60,
    max_wait_s: int = 12 * 3600,
    progress_callback=None,
) -> bool:
    """Wait for a drive's in-progress secure-erase to finish (v0.6.3+).

    Use case — Case B on re-insert: if a drive was pulled mid-erase
    and its firmware resumed the erase on power-up, the drive is
    locked + unresponsive to unlock commands until the internal erase
    finishes. This function polls `hdparm -I` every `poll_interval_s`
    seconds until the drive returns to CLEAN (erase completed, password
    auto-cleared), or `max_wait_s` elapses.

    `progress_callback(elapsed_s, state)` is called on every poll so
    the dashboard can update the drive card sub-label with elapsed
    time + current state. Pass None to skip progress reporting
    (e.g. tests).

    Returns True if the drive became CLEAN within the deadline,
    False if the deadline hit first. Caller decides next step —
    typically "proceed with pipeline" on True, "grade F with
    'erase-never-completed' reason" on False.

    Note: we intentionally do NOT try to accelerate the erase or
    interrupt it. Once a drive starts SECURITY ERASE UNIT, it
    completes on its own schedule. The polling cadence is just to
    detect when it's safe to proceed — there's no mechanism that
    makes it finish faster.
    """
    import time

    start = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        if elapsed > max_wait_s:
            logger.warning(
                "wait_for_prior_erase_completion: %s (%s) did not complete "
                "within %d s deadline; giving up",
                drive.device_path, drive.serial, max_wait_s,
            )
            return False
        state = _probe_sata_security_state(drive.device_path)
        if progress_callback is not None:
            try:
                progress_callback(elapsed, state)
            except Exception:  # noqa: BLE001
                # Never let a callback bug crash the wait loop — we need
                # to keep polling regardless of whether the UI update lands.
                logger.exception("progress_callback raised during wait loop")
        if state == SataSecurityState.CLEAN:
            logger.info(
                "wait_for_prior_erase_completion: %s (%s) returned to CLEAN "
                "after %.0f min — erase completed",
                drive.device_path, drive.serial, elapsed / 60,
            )
            return True
        if state == SataSecurityState.UNKNOWN:
            # hdparm -I failed — drive may have been pulled again, or the
            # bus is having problems. Keep polling; if it stays UNKNOWN
            # we'll eventually hit the max_wait timeout.
            logger.debug(
                "wait_for_prior_erase_completion: %s state=UNKNOWN at %.0f s, "
                "continuing poll",
                drive.device_path, elapsed,
            )
        time.sleep(poll_interval_s)


def _sas_secure_erase(device: str, *, owner: str | None = None, capacity_bytes: int | None = None) -> None:
    # sg_format FORMAT UNIT is one full-disk overwrite in firmware — scales
    # linearly with capacity just like SATA SE. The old flat 12 h cap was
    # fine for 300 GB-1 TB drives but would silently kill 4 TB+ sg_format
    # jobs mid-flight, and mid-flight sg_format abort corrupts the drive
    # (requires manual recovery). Use the same capacity model as SATA.
    timeout_s = capacity_timeout(capacity_bytes, passes=1)
    r = run(["sg_format", "--format", device], timeout=timeout_s, owner=owner)
    if not r.ok:
        raise EraseError(f"sg_format failed on {device}: {r.stderr}")


def _nvme_format(device: str, *, owner: str | None = None) -> None:
    # -s 1 = user-data erase; -f = force, suppress prompts. NVMe format is
    # a crypto-erase — completes in seconds to minutes even on multi-TB
    # drives, so the flat 1 h cap is fine for any size we'd plausibly see.
    r = run(
        ["nvme", "format", "-s", "1", "-f", device],
        timeout=60 * 60,
        owner=owner,
    )
    if not r.ok:
        raise EraseError(f"nvme format failed on {device}: {r.stderr}")


def secure_erase(drive: Drive, *, on_status=None) -> None:
    """Dispatch to the right erase path based on transport.

    For drives classified as SAS by lsblk, re-probe via smartctl first —
    SATA drives attached to SAS HBAs show up as tran=sas in lsblk but
    actually speak ATA. sg_format (SCSI FORMAT UNIT) fails on those with
    "Illegal request"; they want hdparm instead.

    `on_status` (v0.6.3+) is an optional callable that receives
    human-readable progress messages during the erase — "SAT passthrough
    starting", "SAT aborted, falling back to hdparm", etc. The
    orchestrator passes a callback that updates the drive card's
    sublabel so the operator sees which path is running live. Only the
    SATA path currently emits status (SAS sg_format and NVMe format are
    black-box subprocess calls with no intermediate progress signal).
    """
    effective = drive.transport
    if effective == Transport.SAS:
        from driveforge.core.drive import detect_true_transport

        refined = detect_true_transport(drive.device_path)
        if refined in (Transport.SATA, Transport.SAS, Transport.NVME):
            effective = refined

    if effective == Transport.SATA:
        _sata_secure_erase(
            drive.device_path,
            owner=drive.serial,
            capacity_bytes=drive.capacity_bytes,
            on_status=on_status,
        )
    elif effective == Transport.SAS:
        _sas_secure_erase(drive.device_path, owner=drive.serial, capacity_bytes=drive.capacity_bytes)
    elif effective == Transport.NVME:
        _nvme_format(drive.device_path, owner=drive.serial)
    else:
        raise EraseError(f"no erase path for transport={effective}")
