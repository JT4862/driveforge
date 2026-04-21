"""smartctl wrapper + SMART JSON parser.

Uses `smartctl --json --all /dev/sdX` (smartmontools 7.0+) so we don't
parse English text. Returns a structured `SmartSnapshot`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from driveforge.core.process import run, run_async


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

    # Temperature: prefer smartctl's pre-decoded `temperature.current` (it
    # knows the per-vendor raw packing — Seagate, for instance, crams
    # current/min/max into a 48-bit int, so reading `raw_value` directly
    # yielded values like 77_309_411_358 for a drive actually running at
    # 30 °C, which then tripped the thermal-excursion grade-C demotion on
    # healthy drives). Only fall back to attribute raw values when the
    # top-level field is absent AND the raw passes a 0-150 °C sanity check;
    # if the raw looks packed (>= 150), pick its low-16-bit lane, which is
    # where every Seagate/HGST/WD drive we've seen puts the current temp.
    temp: int | None = None
    top_level = (data.get("temperature") or {}).get("current")
    if isinstance(top_level, int) and 0 < top_level < 150:
        temp = top_level
    else:
        for attr_id in (ATTR_TEMP_DRIVE, ATTR_TEMP_AIRFLOW):
            raw = _raw_of(attrs, attr_id)
            if raw is None:
                continue
            if 0 < raw < 150:
                temp = raw
                break
            low16 = raw & 0xFFFF
            if 0 < low16 < 150:
                temp = low16
                break

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


# Per-call timeouts stop a hung drive from pinning the daemon's worker thread
# in D-state. The old Seagate ST300MM0006 on this R720 demonstrated the
# failure mode: firmware stopped responding, every smartctl piled up for the
# 180s kernel SCSI timeout, dashboard rendering hung. Keep these short enough
# that a failing drive gets flagged as dead rather than hanging the UI.
SMARTCTL_INFO_TIMEOUT = 30.0
SMARTCTL_TEST_START_TIMEOUT = 30.0
SMARTCTL_TEST_STATUS_TIMEOUT = 15.0


def snapshot(device: str, *, timeout: float = SMARTCTL_INFO_TIMEOUT) -> SmartSnapshot:
    """Take a SMART snapshot of a device.

    Raises `subprocess.TimeoutExpired` if smartctl hangs past `timeout` — the
    caller should treat that as drive-dead rather than waiting forever.

    Sync variant — used by CLI entrypoints, tests, and non-async callers.
    From an async context, prefer `snapshot_async` (v0.6.9+) to avoid
    burning a drive-command-executor thread per call.
    """
    result = run(["smartctl", "--json", "--all", device], timeout=timeout)
    # smartctl returns non-zero for minor warnings we still want to parse
    if not result.stdout:
        raise RuntimeError(f"smartctl returned no output for {device}: {result.stderr}")
    return parse(result.stdout, device=device)


async def snapshot_async(
    device: str,
    *,
    timeout: float = SMARTCTL_INFO_TIMEOUT,
) -> SmartSnapshot:
    """Async variant of `snapshot` (v0.6.9+).

    Spawns smartctl via `asyncio.create_subprocess_exec` instead of
    burning a thread in the drive-command executor. Preferred from
    async code paths (orchestrator, telemetry sampler, auto-print).

    Semantics match `snapshot`:
      - Raises `asyncio.TimeoutError` on hang past `timeout`. Callers
        that previously caught `subprocess.TimeoutExpired` need to
        widen the except (both are raised by run_async's timeout
        path — TimeoutError from asyncio.wait_for, TimeoutExpired
        never from this code path).
      - Returns a `SmartSnapshot` on success (even on non-zero rc —
        smartctl reports warnings that way).
      - Raises `RuntimeError` on empty stdout.

    Parse is pure-Python and fast, so it runs inline on the event
    loop thread; no need to offload.
    """
    result = await run_async(
        ["smartctl", "--json", "--all", device],
        timeout=timeout,
    )
    if not result.stdout:
        raise RuntimeError(f"smartctl returned no output for {device}: {result.stderr}")
    return parse(result.stdout, device=device)


def start_self_test(
    device: str,
    *,
    kind: str = "short",
    timeout: float = SMARTCTL_TEST_START_TIMEOUT,
) -> None:
    """Start a SMART self-test. `kind` ∈ {'short', 'long'}."""
    if kind not in {"short", "long"}:
        raise ValueError(f"unsupported self-test kind: {kind}")
    run(["smartctl", "--test", kind, device], check=True, timeout=timeout)


class SelfTestStatus(BaseModel):
    in_progress: bool
    percent_complete: int | None = None  # 0-100, None if not in progress
    last_result_passed: bool | None = None  # None if no test has completed
    status_string: str = ""


def self_test_status(
    device: str,
    *,
    timeout: float = SMARTCTL_TEST_STATUS_TIMEOUT,
) -> SelfTestStatus:
    """Query SMART self-test progress via smartctl (sync wrapper)."""
    result = run(["smartctl", "--json", "-c", "-l", "selftest", device], timeout=timeout)
    if not result.stdout:
        return SelfTestStatus(in_progress=False)
    return parse_self_test_status(result.stdout)


def parse_self_test_status(payload: str) -> SelfTestStatus:
    """Pure-function parser for `smartctl --json -c -l selftest` output.

    Handles ATA (`ata_smart_data.self_test`), NVMe
    (`nvme_self_test_log.current_self_test_operation`), and SAS
    (`scsi_self_test_N.result`) log shapes. Returns in-progress + pass/fail.
    """
    import json as _json

    try:
        data = _json.loads(payload)
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
    # SCSI / SAS self-test log. smartctl --json emits scsi_self_test_0
    # through _19 with this shape:
    #   {"code": {...}, "result": {"value": N, "string": "..."}, ...}
    # result.value semantics per SPC-4 / smartmontools:
    #   0 = Completed without error
    #   1 = Aborted by user (SEND DIAGNOSTIC)
    #   2 = Aborted by reset or power cycle
    #   3 = Unknown error
    #   4-8 = Various failure segments
    #   15 = Self-test in progress
    # (The first entry with result.value=15 indicates a running test.)
    if last_passed is None:
        for i in range(20):
            entry = data.get(f"scsi_self_test_{i}")
            if not entry:
                continue
            result = entry.get("result") or {}
            result_val = result.get("value")
            if result_val == 15:
                return SelfTestStatus(
                    in_progress=True,
                    percent_complete=None,  # SAS log doesn't expose progress %
                    status_string=result.get("string", ""),
                )
            if isinstance(result_val, int):
                last_passed = result_val == 0
                break  # Most recent completed entry is authoritative
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
        status_string=status.get("string", "") if isinstance(status, dict) else "",
    )
