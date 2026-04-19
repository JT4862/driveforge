from __future__ import annotations

from driveforge.core import telemetry


def test_chassis_power_parses_ipmitool_output() -> None:
    watts = telemetry.read_chassis_power()
    assert watts == 342.0
