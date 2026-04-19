"""Drive model + block-device discovery.

`discover()` enumerates attached block devices (excluding the OS disk) and
returns a list of `Drive` records. Used on daemon start and on udev
`add`/`remove` events.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

from driveforge.core.process import run


class Transport(str, Enum):
    SATA = "sata"
    SAS = "sas"
    NVME = "nvme"
    USB = "usb"
    UNKNOWN = "unknown"


class Drive(BaseModel):
    """A physical storage device attached to the rig.

    Identifiers use the drive's serial as the primary key. Device paths
    (`/dev/sda`, `/dev/nvme0n1`) are runtime-only — they can shift across
    reboots while serials are stable.
    """

    serial: str
    model: str
    capacity_bytes: int = Field(ge=0)
    transport: Transport = Transport.UNKNOWN
    device_path: str
    rotation_rate: int | None = None  # 0 for SSD
    firmware_version: str | None = None
    bay: int | None = None  # assigned by the orchestrator

    @property
    def capacity_tb(self) -> float:
        return round(self.capacity_bytes / 1_000_000_000_000, 2)

    @property
    def is_ssd(self) -> bool:
        return self.rotation_rate == 0 or self.transport == Transport.NVME


def _parse_lsblk_json(payload: str) -> list[dict]:
    """Return the flat list of disk entries from lsblk --json output."""
    data = json.loads(payload)
    devices = data.get("blockdevices", [])
    return [d for d in devices if d.get("type") == "disk"]


def _transport_of(entry: dict) -> Transport:
    tran = (entry.get("tran") or "").lower()
    if tran == "sata":
        return Transport.SATA
    if tran == "sas":
        return Transport.SAS
    if tran == "nvme":
        return Transport.NVME
    if tran == "usb":
        return Transport.USB
    # lsblk sometimes leaves tran empty on SAS drives behind HBAs
    name = entry.get("name", "")
    if name.startswith("nvme"):
        return Transport.NVME
    return Transport.UNKNOWN


def _root_device_name() -> str | None:
    """Return the name (e.g. 'sda') of the device holding the root filesystem.

    Discovery excludes it so we never secure-erase the OS disk.
    """
    result = run(["findmnt", "-no", "SOURCE", "/"])
    if not result.ok:
        return None
    src = result.stdout.strip()
    # /dev/sda2 → sda; /dev/nvme0n1p2 → nvme0n1; /dev/mapper/... → bail out
    if src.startswith("/dev/mapper/"):
        return None
    name = Path(src).name
    if name.startswith("nvme"):
        # strip trailing partition (e.g. p2)
        return name.split("p")[0] if "p" in name else name
    return name.rstrip("0123456789")


def discover(include_root: bool = False) -> list[Drive]:
    """Discover all attached disks, excluding the root device by default."""
    result = run(
        [
            "lsblk",
            "--json",
            "--bytes",
            "--output",
            "NAME,MODEL,SERIAL,SIZE,TRAN,ROTA,TYPE,REV",
        ]
    )
    if not result.ok:
        return []
    entries = _parse_lsblk_json(result.stdout)
    root_name = None if include_root else _root_device_name()
    drives: list[Drive] = []
    for entry in entries:
        name = entry.get("name", "")
        if root_name and name == root_name:
            continue
        serial = entry.get("serial")
        if not serial:
            # Drives without a serial are usually virtual / unusable for cert
            continue
        rota = entry.get("rota")
        if isinstance(rota, str):
            rota_int: int | None = int(rota)
        elif isinstance(rota, bool):
            rota_int = int(rota)
        elif isinstance(rota, int):
            rota_int = rota
        else:
            rota_int = None
        drives.append(
            Drive(
                serial=serial,
                model=(entry.get("model") or "Unknown").strip(),
                capacity_bytes=int(entry.get("size") or 0),
                transport=_transport_of(entry),
                device_path=f"/dev/{name}",
                rotation_rate=(0 if rota_int == 0 else (7200 if rota_int == 1 else None)),
                firmware_version=entry.get("rev"),
            )
        )
    return drives
