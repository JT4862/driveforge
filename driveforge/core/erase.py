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
) -> None:
    """SAT-passthrough secure erase for SATA drives. Replaces the
    pre-v0.3.0 `hdparm --security-erase` call, which fails on modern
    Debian kernels for SATA drives behind a SAS HBA with the
    `CONFIG_IDE_TASK_IOCTL` kernel-configuration error. The SAT path
    uses ATA-PASS-THROUGH(16) (SCSI opcode 0x85) wrapping the same
    ATA SECURITY ERASE UNIT command, submitted via `sg_raw` →
    `SG_IO` ioctl — which the kernel still supports universally.

    The drive-side operation is identical to what hdparm would have
    done: SET PASSWORD → ERASE PREPARE → ERASE UNIT. Only the
    transport changes."""
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
    try:
        sat_passthru.sat_secure_erase(
            device, password=password, timeout_s=timeout_s, owner=owner
        )
    except sat_passthru.SatPassthruError as exc:
        # Re-wrap so the orchestrator's generic EraseError catch still works
        # without needing to know about the SAT layer.
        raise EraseError(str(exc)) from exc


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


def secure_erase(drive: Drive) -> None:
    """Dispatch to the right erase path based on transport.

    For drives classified as SAS by lsblk, re-probe via smartctl first —
    SATA drives attached to SAS HBAs show up as tran=sas in lsblk but
    actually speak ATA. sg_format (SCSI FORMAT UNIT) fails on those with
    "Illegal request"; they want hdparm instead.
    """
    effective = drive.transport
    if effective == Transport.SAS:
        from driveforge.core.drive import detect_true_transport

        refined = detect_true_transport(drive.device_path)
        if refined in (Transport.SATA, Transport.SAS, Transport.NVME):
            effective = refined

    if effective == Transport.SATA:
        _sata_secure_erase(drive.device_path, owner=drive.serial, capacity_bytes=drive.capacity_bytes)
    elif effective == Transport.SAS:
        _sas_secure_erase(drive.device_path, owner=drive.serial, capacity_bytes=drive.capacity_bytes)
    elif effective == Transport.NVME:
        _nvme_format(drive.device_path, owner=drive.serial)
    else:
        raise EraseError(f"no erase path for transport={effective}")
