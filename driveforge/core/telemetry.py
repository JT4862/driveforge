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


def read_chassis_power() -> float | None:
    """Return instantaneous chassis power draw in watts, via iDRAC / IPMI."""
    result = run(["ipmitool", "dcmi", "power", "reading"])
    if not result.ok:
        return None
    m = _IPMITOOL_POWER_RE.search(result.stdout)
    return float(m.group(1)) if m else None


def sample(drive_serial: str, phase: str, *, drive_temp_c: int | None, chassis_power_w: float | None) -> TelemetryPoint:
    return TelemetryPoint(
        drive_serial=drive_serial,
        ts=datetime.now(UTC),
        phase=phase,
        drive_temp_c=drive_temp_c,
        chassis_power_w=chassis_power_w,
    )
