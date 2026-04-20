"""Secure erase dispatch.

One entry point — `secure_erase(drive)` — picks the right mechanism based on
transport:

- SATA: `hdparm --security-erase`
- SAS:  `sg_format --format`
- NVMe: `nvme format -s 1`

All are destructive. The orchestrator is responsible for confirming intent
before calling this.
"""

from __future__ import annotations

import logging
import re

from driveforge.core.drive import Drive, Transport
from driveforge.core.process import run

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


def _sata_secure_erase(device: str, password: str = "driveforge", *, owner: str | None = None) -> None:
    # Enable security with a throwaway password
    r1 = run(
        ["hdparm", "--user-master", "u", "--security-set-pass", password, device],
        owner=owner,
    )
    if not r1.ok:
        raise EraseError(f"failed to set ATA security pass on {device}: {r1.stderr}")

    # Drive-reported SECURITY ERASE UNIT time × 1.5 headroom, floored at 6 h
    # and capped at 16 h. 4 TB drives report ~450 min (7.5 h); giving them
    # the old flat 6 h timeout killed the erase mid-flight and the drive
    # showed up as a "unexpected command" failure. Large SMR or 8 TB+
    # drives can legitimately need 10 h+.
    est = _sata_estimated_seconds(device)
    timeout_s = max(6 * 3600, int((est or 0) * 1.5))
    timeout_s = min(timeout_s, 16 * 3600)

    r2 = run(
        ["hdparm", "--user-master", "u", "--security-erase", password, device],
        timeout=timeout_s,
        owner=owner,
    )
    if not r2.ok:
        raise EraseError(f"security-erase failed on {device}: {r2.stderr}")


def _sas_secure_erase(device: str, *, owner: str | None = None) -> None:
    r = run(["sg_format", "--format", device], timeout=12 * 60 * 60, owner=owner)
    if not r.ok:
        raise EraseError(f"sg_format failed on {device}: {r.stderr}")


def _nvme_format(device: str, *, owner: str | None = None) -> None:
    # -s 1 = user-data erase; -f = force, suppress prompts
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
        _sata_secure_erase(drive.device_path, owner=drive.serial)
    elif effective == Transport.SAS:
        _sas_secure_erase(drive.device_path, owner=drive.serial)
    elif effective == Transport.NVME:
        _nvme_format(drive.device_path, owner=drive.serial)
    else:
        raise EraseError(f"no erase path for transport={effective}")
