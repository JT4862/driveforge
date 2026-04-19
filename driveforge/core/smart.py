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


class SelfTestStatus(BaseModel):
    in_progress: bool
    percent_complete: int | None = None  # 0-100, None if not in progress
    last_result_passed: bool | None = None  # None if no test has completed
    status_string: str = ""


def self_test_status(device: str) -> SelfTestStatus:
    """Query SMART self-test progress. Works for ATA; parses NVMe status separately."""
    result = run(["smartctl", "--json", "-c", "-l", "selftest", device])
    if not result.stdout:
        return SelfTestStatus(in_progress=False)
    import json as _json

    try:
        data = _json.loads(result.stdout)
    except _json.JSONDecodeError:
        return SelfTestStatus(in_progress=False)
    # ATA path: ata_smart_data.self_test.status
    ata = (data.get("ata_smart_data") or {}).get("self_test") or {}
    status = ata.get("status") or {}
    remaining = status.get("remaining_percent")
    value = status.get("value")
    if remaining is not None:
        return SelfTestStatus(
            in_progress=True,
            percent_complete=100 - int(remaining),
            status_string=status.get("string", ""),
        )
    # ATA status values >= 0x80 mean in-progress; < 0x80 means completed
    if isinstance(value, int) and value >= 0x80:
        # Low nibble × 10 = percent remaining
        pct_remaining = (value & 0x0F) * 10
        return SelfTestStatus(
            in_progress=True,
            percent_complete=100 - pct_remaining,
            status_string=status.get("string", ""),
        )
    # Not in progress; was the last completed test a pass?
    last_passed: bool | None = None
    # ATA self-test log
    last_log = (data.get("ata_smart_self_test_log") or {}).get("standard") or {}
    table = last_log.get("table") or []
    if table:
        top = table[0]
        st = (top.get("status") or {}).get("string", "").lower()
        if "without error" in st or "completed without" in st:
            last_passed = True
        elif any(word in st for word in ("fail", "error", "aborted")):
            last_passed = False
    # SCSI / SAS self-test log — scsi_self_test_0 is most recent. In-progress
    # reported via status code 15 per SPC-4.
    for i in range(20):
        entry = data.get(f"scsi_self_test_{i}")
        if not entry:
            continue
        sts = (entry.get("status") or {}).get("value")
        if sts == 15:
            # In progress — percent_remaining is often available separately
            pct_remaining = data.get("scsi_percentage_of_test_remaining")
            return SelfTestStatus(
                in_progress=True,
                percent_complete=(100 - int(pct_remaining)) if isinstance(pct_remaining, int) else None,
                status_string=(entry.get("status") or {}).get("string", ""),
            )
        if isinstance(sts, int) and last_passed is None:
            last_passed = sts == 0  # 0 = "Completed without error"
            break
    # NVMe path: self_test_log.current_self_test_operation
    nvme = (data.get("nvme_self_test_log") or {}).get("current_self_test_operation") or {}
    nvme_op_value = nvme.get("value")
    if isinstance(nvme_op_value, int) and nvme_op_value != 0:
        nvme_completion = data.get("nvme_self_test_log", {}).get("current_self_test_completion") or {}
        nvme_pct = nvme_completion.get("percent_remaining")
        return SelfTestStatus(
            in_progress=True,
            percent_complete=(100 - int(nvme_pct)) if nvme_pct is not None else None,
            status_string=nvme.get("string", ""),
        )
    return SelfTestStatus(
        in_progress=False,
        last_result_passed=last_passed,
        status_string=status.get("string", ""),
    )
