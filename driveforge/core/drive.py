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
    manufacturer: str | None = None
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


# Known drive-manufacturer prefixes. Keys are what shows up verbatim in the
# first token of the model string (INQUIRY vendor on SCSI, or model_name on
# ATA). Values are the normalized display name.
_MFR_PREFIXES = {
    "INTEL": "Intel",
    "SAMSUNG": "Samsung",
    "SEAGATE": "Seagate",
    "WDC": "Western Digital",
    "WD": "Western Digital",
    "HGST": "HGST",
    "TOSHIBA": "Toshiba",
    "KIOXIA": "Kioxia",
    "KINGSTON": "Kingston",
    "CRUCIAL": "Crucial",
    "MICRON": "Micron",
    "SANDISK": "SanDisk",
    "SK": "SK Hynix",  # "SK hynix" → first token is "SK"
    "HYNIX": "SK Hynix",
    "HITACHI": "Hitachi",
    "FUJITSU": "Fujitsu",
    "SOLIDIGM": "Solidigm",
    "IBM": "IBM",
    "DELL": "Dell",
    "HP": "HP",
    "NETAPP": "NetApp",
}


# OEM firmware-revision signatures. Big-iron vendors (Dell, HP, NetApp, IBM)
# buy raw drives from the actual manufacturers (Seagate, WD, HGST, Toshiba)
# and flash a vendor-customized firmware before reselling them in their own
# servers. The drive's INQUIRY VENDOR keeps reporting the real manufacturer
# ("SEAGATE", "WDC"), but the firmware-revision string carries the OEM
# signature. We use that to retag the drive with the OEM brand the operator
# (and the chassis sticker) recognize.
#
# Patterns are matched against the firmware revision string; first match wins
# and overrides any vendor/prefix-based detection.
import re as _re  # noqa: E402

_OEM_FIRMWARE_PATTERNS = [
    # Dell-customized Seagate enterprise SAS drives. Confirmed on
    # ST300MM0006 LS08 + LS0A pulled from a Dell PowerEdge. Vanilla retail
    # Seagate ships these as "0003" or "B005" — the LS prefix is Dell's.
    (_re.compile(r"^LS[0-9A-F]{2}$"), "Dell"),
    # HP/HPE-customized Seagate/HGST drives commonly use HPGx / HPDx.
    (_re.compile(r"^HP[A-Z0-9]{2}$"), "HPE"),
    # NetApp-customized: NA0x.
    (_re.compile(r"^NA[0-9A-F]{2}$"), "NetApp"),
]


def _detect_oem_from_firmware(firmware: str | None) -> str | None:
    if not firmware:
        return None
    fw = firmware.strip().upper()
    for pattern, name in _OEM_FIRMWARE_PATTERNS:
        if pattern.match(fw):
            return name
    return None


def detect_manufacturer(
    model: str,
    vendor_hint: str | None = None,
    firmware: str | None = None,
) -> str | None:
    """Best-effort manufacturer detection.

    Resolution order:
      1. OEM firmware signature (Dell LS0x, HP HPGx, NetApp NAxx) — wins
         even when INQUIRY VENDOR says the underlying manufacturer, because
         the operator and the chassis sticker think of it as the OEM brand.
      2. Explicit `vendor_hint` (e.g. smartctl INQUIRY on SAS drives).
      3. Model-string prefix parse against the known-vendor list.
      4. The "ST<digit>..." Seagate drive-code convention.

    Returns None if nothing matches; the caller should leave the column
    null rather than showing a wrong guess.
    """
    oem = _detect_oem_from_firmware(firmware)
    if oem:
        return oem
    if vendor_hint:
        clean = vendor_hint.strip().upper()
        # smartctl on a SATA-behind-SAT tunnel often reports "ATA" as vendor;
        # that's not a real manufacturer, so ignore it and fall through.
        if clean and clean != "ATA":
            return _MFR_PREFIXES.get(clean, vendor_hint.strip())
    if not model:
        return None
    first_token = model.strip().split(maxsplit=1)[0].upper()
    if first_token in _MFR_PREFIXES:
        return _MFR_PREFIXES[first_token]
    # Seagate model numbers like "ST300MM0006" — "ST" followed by a digit is
    # the Seagate drive-code convention going back 30+ years.
    if len(first_token) >= 3 and first_token.startswith("ST") and first_token[2].isdigit():
        return "Seagate"
    return None


def probe_manufacturer(
    device_path: str,
    model: str,
    firmware: str | None = None,
) -> str | None:
    """Enrollment-time probe: smartctl INQUIRY + OEM firmware override.

    Only called at start_batch time (once per drive), never in the dashboard
    hot path. Timeout-protected so a hung drive can't stall enrollment. The
    `firmware` arg lets the caller pass the lsblk REV value as a fallback
    when smartctl can't be reached.
    """
    import json as _json

    vendor_hint: str | None = None
    fw_from_smartctl: str | None = None
    try:
        result = run(["smartctl", "--json", "-i", device_path], timeout=15.0)
        if result.stdout:
            data = _json.loads(result.stdout)
            vendor_hint = (data.get("vendor") or "").strip() or None
            fw_from_smartctl = (data.get("firmware_version") or "").strip() or None
    except Exception:  # noqa: BLE001
        # smartctl failed / hung / not-JSON → fall back to model parse + the
        # firmware we got from lsblk earlier.
        vendor_hint = None
    return detect_manufacturer(
        model,
        vendor_hint=vendor_hint,
        firmware=fw_from_smartctl or firmware,
    )


def detect_true_transport(device_path: str) -> Transport | None:
    """Probe smartctl for the drive's actual wire protocol.

    lsblk's `tran` field reports the HBA-level transport, which isn't always
    the drive's protocol. A SATA SSD attached to a SAS HBA shows up as
    tran=sas at the block layer but speaks ATA over STP tunneling. This
    matters for erase dispatch — sg_format (SCSI FORMAT UNIT) will fail
    with "Illegal request" on such drives; they want hdparm instead.

    smartctl reports:
      "type": "sat", "protocol": "ATA"   → SATA drive (even via SAS HBA)
      "type": "scsi", "protocol": "SCSI" → true SAS
      "type": "nvme"                      → NVMe
    """
    import json as _json

    # Short timeout: this probe runs in the erase dispatch critical path.
    # A hung drive should fail fast, not wait 3 minutes for SCSI timeouts.
    try:
        result = run(["smartctl", "--json", "-i", device_path], timeout=15.0)
    except Exception:  # subprocess.TimeoutExpired or anything else
        return None
    if not result.stdout:
        return None
    try:
        data = _json.loads(result.stdout)
    except _json.JSONDecodeError:
        return None
    device = data.get("device") or {}
    dtype = (device.get("type") or "").lower()
    protocol = (device.get("protocol") or "").lower()
    if dtype == "nvme" or protocol == "nvme":
        return Transport.NVME
    if dtype.startswith("sat") or protocol == "ata":
        return Transport.SATA
    if dtype == "scsi" or protocol == "scsi":
        return Transport.SAS
    return None


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
        device_path = f"/dev/{name}"
        transport = _transport_of(entry)
        # NOTE: we no longer call detect_true_transport() here. That probe
        # shells out to smartctl and was running on every dashboard refresh
        # (HTMX polls every 3s), piling up concurrent smartctl processes on
        # the same drive and timing out. Instead, the orchestrator re-probes
        # right before dispatching secure_erase — the only place it matters.
        model_str = (entry.get("model") or "Unknown").strip()
        firmware = entry.get("rev")
        drives.append(
            Drive(
                serial=serial,
                model=model_str,
                capacity_bytes=int(entry.get("size") or 0),
                transport=transport,
                device_path=device_path,
                rotation_rate=(0 if rota_int == 0 else (7200 if rota_int == 1 else None)),
                firmware_version=firmware,
                # Fast prefix parse + firmware-pattern OEM override. No smartctl
                # call here — the discovery hot path stays subprocess-free.
                # probe_manufacturer() runs the full smartctl path at enrollment.
                manufacturer=detect_manufacturer(model_str, firmware=firmware),
            )
        )
    return drives
