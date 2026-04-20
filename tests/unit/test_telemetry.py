from __future__ import annotations

from driveforge.core import telemetry


def test_chassis_power_parses_ipmitool_output() -> None:
    watts = telemetry.read_chassis_power()
    assert watts == 342.0


def test_chassis_temperatures_parses_ipmitool_sdr_output() -> None:
    temps = telemetry.read_chassis_temperatures()
    # Fixture has Inlet (17), Exhaust (22), and two "Temp" lines (CPU package
    # and second CPU). The parser returns a dict keyed on the label, so the
    # two "Temp" entries collapse to one (last wins). That's fine for this
    # use case — we surface inlet + exhaust primarily.
    assert temps["Inlet Temp"] == 17
    assert temps["Exhaust Temp"] == 22
    # Status "ns" (not specified) rows are excluded
    assert "SEL" not in temps
    assert "ROMB Battery" not in temps
    # Non-temp rows are never matched
    assert "Fan1" not in temps
