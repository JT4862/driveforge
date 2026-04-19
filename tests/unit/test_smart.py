from __future__ import annotations

from driveforge.core import smart


def test_snapshot_parses_core_attributes() -> None:
    snap = smart.snapshot("/dev/sda")
    assert snap.model == "HGST HUS726T6TALE6L4"
    assert snap.serial == "V8G6X4RL"
    assert snap.firmware == "K8GNW7LH"
    assert snap.power_on_hours == 12432
    assert snap.temperature_c == 34
    assert snap.reallocated_sectors == 0
    assert snap.current_pending_sector == 0
    assert snap.offline_uncorrectable == 0
    assert snap.smart_status_passed is True


def test_snapshot_attribute_table_populated() -> None:
    snap = smart.snapshot("/dev/sda")
    # Temperature attribute should be present; we don't care about the full
    # list length — just sanity that parsing populated something useful.
    names = {a.name for a in snap.attributes}
    assert "Reallocated_Sector_Ct" in names
    assert "Power_On_Hours" in names
