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

from driveforge.core.drive import Drive, Transport
from driveforge.core.process import run


class EraseError(RuntimeError):
    pass


def _sata_secure_erase(device: str, password: str = "driveforge") -> None:
    # Enable security with a throwaway password
    r1 = run(["hdparm", "--user-master", "u", "--security-set-pass", password, device])
    if not r1.ok:
        raise EraseError(f"failed to set ATA security pass on {device}: {r1.stderr}")
    # Issue the secure erase
    r2 = run(
        ["hdparm", "--user-master", "u", "--security-erase", password, device],
        timeout=6 * 60 * 60,  # generous — can run for hours
    )
    if not r2.ok:
        raise EraseError(f"security-erase failed on {device}: {r2.stderr}")


def _sas_secure_erase(device: str) -> None:
    r = run(["sg_format", "--format", device], timeout=12 * 60 * 60)
    if not r.ok:
        raise EraseError(f"sg_format failed on {device}: {r.stderr}")


def _nvme_format(device: str) -> None:
    # -s 1 = user-data erase; -f = force, suppress prompts
    r = run(["nvme", "format", "-s", "1", "-f", device], timeout=60 * 60)
    if not r.ok:
        raise EraseError(f"nvme format failed on {device}: {r.stderr}")


def secure_erase(drive: Drive) -> None:
    """Dispatch to the right erase path based on transport."""
    if drive.transport == Transport.SATA:
        _sata_secure_erase(drive.device_path)
    elif drive.transport == Transport.SAS:
        _sas_secure_erase(drive.device_path)
    elif drive.transport == Transport.NVME:
        _nvme_format(drive.device_path)
    else:
        raise EraseError(f"no erase path for transport={drive.transport}")
