"""smartctl wrapper + SMART JSON parser.

Uses `smartctl --json --all /dev/sdX` (smartmontools 7.0+) so we don't
parse English text. Returns a structured `SmartSnapshot`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from driveforge.core.process import run


class SmartAttribute(BaseModel):
    id: int
    name: str
    value: int | None = None
    worst: int | None = None
    threshold: int | None = None
    raw_value: int | None = None


class SmartSnapshot(BaseModel):
    """Point-in-time SMART snapshot for a drive.

    Stored pre- and post-test; diffed in Phase 8 to grade degradation.
    """

    device: str
    captured_at: datetime
    model: str | None = None
    serial: str | None = None
    firmware: str | None = None
    power_on_hours: int | None = None
    temperature_c: int | None = None
    reallocated_sectors: int | None = None
    current_pending_sector: int | None = None
    offline_uncorrectable: int | None = None
    udma_crc_error_count: int | None = None
    smart_status_passed: bool | None = None
    attributes: list[SmartAttribute] = []
    raw: dict[str, Any] = {}


ATTR_REALLOCATED = 5
ATTR_POWER_ON_HOURS = 9
ATTR_TEMP_AIRFLOW = 190
ATTR_TEMP_DRIVE = 194
ATTR_CURRENT_PENDING = 197
ATTR_OFFLINE_UNCORRECTABLE = 198
ATTR_UDMA_CRC_ERROR = 199


def _raw_of(attrs: list[SmartAttribute], attr_id: int) -> int | None:
    for a in attrs:
        if a.id == attr_id:
            return a.raw_value
    return None


def parse(payload: str, *, device: str = "") -> SmartSnapshot:
    """Parse `smartctl --json --all` output."""
    data = json.loads(payload)
    attrs: list[SmartAttribute] = []
    for raw in data.get("ata_smart_attributes", {}).get("table", []) or []:
        attrs.append(
            SmartAttribute(
                id=raw.get("id", 0),
                name=raw.get("name", ""),
                value=raw.get("value"),
                worst=raw.get("worst"),
                threshold=raw.get("thresh"),
                raw_value=(raw.get("raw") or {}).get("value"),
            )
        )

    # Temperature: prefer the drive-temp attribute, fall back to airflow, fall
    # back to the `temperature` top-level block (NVMe path).
    temp = (
        _raw_of(attrs, ATTR_TEMP_DRIVE)
        or _raw_of(attrs, ATTR_TEMP_AIRFLOW)
        or (data.get("temperature") or {}).get("current")
    )

    # NVMe health log sits at `nvme_smart_health_information_log`
    nvme_log = data.get("nvme_smart_health_information_log") or {}
    power_on_hours = (
        _raw_of(attrs, ATTR_POWER_ON_HOURS)
        or nvme_log.get("power_on_hours")
        or (data.get("power_on_time") or {}).get("hours")
    )

    status = data.get("smart_status") or {}

    return SmartSnapshot(
        device=device or (data.get("device") or {}).get("name", ""),
        captured_at=datetime.now(UTC),
        model=data.get("model_name"),
        serial=data.get("serial_number"),
        firmware=data.get("firmware_version"),
        power_on_hours=power_on_hours,
        temperature_c=temp,
        reallocated_sectors=_raw_of(attrs, ATTR_REALLOCATED),
        current_pending_sector=_raw_of(attrs, ATTR_CURRENT_PENDING),
        offline_uncorrectable=_raw_of(attrs, ATTR_OFFLINE_UNCORRECTABLE),
        udma_crc_error_count=_raw_of(attrs, ATTR_UDMA_CRC_ERROR),
        smart_status_passed=status.get("passed"),
        attributes=attrs,
        raw=data,
    )


def snapshot(device: str) -> SmartSnapshot:
    """Take a SMART snapshot of a device."""
    result = run(["smartctl", "--json", "--all", device])
    # smartctl returns non-zero for minor warnings we still want to parse
    if not result.stdout:
        raise RuntimeError(f"smartctl returned no output for {device}: {result.stderr}")
    return parse(result.stdout, device=device)


def start_self_test(device: str, *, kind: str = "short") -> None:
    """Start a SMART self-test. `kind` ∈ {'short', 'long'}."""
    if kind not in {"short", "long"}:
        raise ValueError(f"unsupported self-test kind: {kind}")
    run(["smartctl", "--test", kind, device], check=True)
