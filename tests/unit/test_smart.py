from __future__ import annotations

import json

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


def _payload_with_attr194_raw(raw: int, top_level_current: int | None = None) -> str:
    """Build a minimal smartctl --json payload with a chosen raw attr 194.

    Seagate/WD pack current/min/max into a single 48-bit raw int; we saw
    77_309_411_358 in the field, which decodes to current=30 °C. smartctl
    itself publishes the decoded value as `temperature.current` — the parser
    must prefer that top-level field and not echo the packed raw.
    """
    payload: dict = {
        "device": {"name": "/dev/sdx"},
        "model_name": "TEST_MODEL",
        "serial_number": "TEST_SERIAL",
        "ata_smart_attributes": {
            "table": [
                {"id": 194, "name": "Temperature_Celsius",
                 "value": 25, "worst": 25, "thresh": 0,
                 "raw": {"value": raw}},
            ]
        },
    }
    if top_level_current is not None:
        payload["temperature"] = {"current": top_level_current}
    return json.dumps(payload)


def test_temperature_prefers_top_level_over_packed_raw() -> None:
    """Real-field regression: Seagate ST3000DM001 reports raw=77_309_411_358
    for attribute 194; smartctl decodes it to temperature.current=30. We
    must take the decoded value, not the raw (which tripped thermal-
    excursion demotion on healthy drives)."""
    snap = smart.parse(_payload_with_attr194_raw(77_309_411_358, top_level_current=30))
    assert snap.temperature_c == 30


def test_temperature_falls_back_to_low16_when_top_level_missing() -> None:
    """Older smartctl or unusual drives may not publish temperature.current.
    Raw packed value >= 150 is clearly not a °C reading — pull the low 16
    bits as the current-temp lane (true for every Seagate/HGST/WD we've
    handled)."""
    # 77_309_411_358 & 0xFFFF == 30
    snap = smart.parse(_payload_with_attr194_raw(77_309_411_358))
    assert snap.temperature_c == 30


def test_temperature_accepts_plain_raw_in_sane_range() -> None:
    """When a drive actually stores just °C in raw_value (NVMe-ish), use it."""
    snap = smart.parse(_payload_with_attr194_raw(42))
    assert snap.temperature_c == 42


def test_temperature_none_when_nothing_reasonable() -> None:
    """Raw is zero and there's no top-level field — return None rather than
    silently reporting 0 °C and making dashboards look fine on a broken drive."""
    snap = smart.parse(_payload_with_attr194_raw(0))
    assert snap.temperature_c is None
