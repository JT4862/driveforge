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


def _sata_secure_erase(device: str, password: str = "driveforge", *, owner: str | None = None) -> None:
    # Enable security with a throwaway password
    r1 = run(
        ["hdparm", "--user-master", "u", "--security-set-pass", password, device],
        owner=owner,
    )
    if not r1.ok:
        raise EraseError(f"failed to set ATA security pass on {device}: {r1.stderr}")
    # Issue the secure erase
    r2 = run(
        ["hdparm", "--user-master", "u", "--security-erase", password, device],
        timeout=6 * 60 * 60,  # generous — can run for hours
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
