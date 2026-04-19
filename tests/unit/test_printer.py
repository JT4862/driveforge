from __future__ import annotations

from datetime import date

from driveforge.core.printer import CertLabelData, LABEL_SIZES, render_label


def _sample() -> CertLabelData:
    return CertLabelData(
        model="HGST HUS726T6TALE6L4",
        serial="V8G6X4RL",
        capacity_tb=6.0,
        grade="A",
        tested_date=date(2026, 4, 19),
        power_on_hours=12432,
        report_url="http://driveforge.local/reports/V8G6X4RL",
    )


def test_render_default_roll_matches_expected_size() -> None:
    img = render_label(_sample())
    assert img.size == LABEL_SIZES["DK-1209"]
    assert img.size[0] > img.size[1]  # landscape


def test_render_compact_roll_uses_compact_layout() -> None:
    img = render_label(_sample(), roll="DK-1221")
    assert img.size == LABEL_SIZES["DK-1221"]
    # Compact layout still returns a valid image; no exception
    assert img.mode == "RGB"


def test_render_unknown_roll_falls_back_to_default() -> None:
    img = render_label(_sample(), roll="DK-BOGUS")
    assert img.size == LABEL_SIZES["DK-1209"]
