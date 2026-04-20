"""Telemetry collection: per-drive temp + chassis power.

Called on a ~30s cadence from the orchestrator during active test runs.
Samples are appended to the `telemetry_sample` table, consumed by the
charts in the web UI and the grading thermal-excursion rule.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from pydantic import BaseModel

from driveforge.core.process import run


class TelemetryPoint(BaseModel):
    drive_serial: str | None
    ts: datetime
    phase: str
    drive_temp_c: int | None = None
    chassis_power_w: float | None = None


_IPMITOOL_POWER_RE = re.compile(r"Instantaneous power reading\s*:\s*(\d+)\s*Watts", re.IGNORECASE)
# Matches an ipmitool sdr line of the form:
#   "Inlet Temp       | 17 degrees C      | ok"
# Capture groups: label, temperature as int, status.
_IPMITOOL_SDR_TEMP_RE = re.compile(
    r"^\s*([^|]+?)\s*\|\s*(\d+)\s*degrees\s*C\s*\|\s*(\S+)",
    re.MULTILINE,
)


def read_chassis_power() -> float | None:
    """Return instantaneous chassis power draw in watts, via local BMC (IPMI DCMI)."""
    result = run(["ipmitool", "dcmi", "power", "reading"])
    if not result.ok:
        return None
    m = _IPMITOOL_POWER_RE.search(result.stdout)
    return float(m.group(1)) if m else None


def read_chassis_temperatures() -> dict[str, int]:
    """Return a dict of {sensor_label: temperature_C} via `ipmitool sdr`.

    Typical labels on a server BMC: "Inlet Temp", "Exhaust Temp", "Temp"
    (CPU package), "DIMM Temp", sometimes per-fan labels that include a
    temperature probe. Empty dict on hosts without IPMI or without a
    readable /dev/ipmi0 (daemon user perms).
    """
    result = run(["ipmitool", "sdr"])
    if not result.ok:
        return {}
    out: dict[str, int] = {}
    for match in _IPMITOOL_SDR_TEMP_RE.finditer(result.stdout):
        label = match.group(1).strip()
        try:
            temp = int(match.group(2))
        except ValueError:
            continue
        # Status column: "ok" is fine; "ns" (not specified / not present)
        # means the sensor is defined but has no current reading — drop it.
        status = match.group(3).strip().lower()
        if status in {"ns", "nr"}:
            continue
        out[label] = temp
    return out


def sample(drive_serial: str, phase: str, *, drive_temp_c: int | None, chassis_power_w: float | None) -> TelemetryPoint:
    return TelemetryPoint(
        drive_serial=drive_serial,
        ts=datetime.now(UTC),
        phase=phase,
        drive_temp_c=drive_temp_c,
        chassis_power_w=chassis_power_w,
    )
