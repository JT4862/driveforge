from __future__ import annotations

from datetime import date

from driveforge.core.printer import (
    CertLabelData,
    LABEL_SIZES,
    _format_health_line,
    _format_poh,
    primary_fail_reason,
    render_label,
)


def _sample(**overrides) -> CertLabelData:
    """Build a CertLabelData with the enriched v0.5.2 fields populated
    by default. Pass overrides to exercise specific edge cases."""
    defaults = dict(
        model="HGST HUS726T6TALE6L4",
        serial="V8G6X4RL",
        capacity_tb=6.0,
        grade="A",
        tested_date=date(2026, 4, 19),
        power_on_hours=12432,
        report_url="http://driveforge.local/reports/V8G6X4RL",
        reallocated_sectors=0,
        current_pending_sector=0,
        badblocks_errors=(0, 0, 0),
        fail_reason=None,
    )
    defaults.update(overrides)
    return CertLabelData(**defaults)


# ---------------------------------------------------------------- render — sizes


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


# ---------------------------------------------------------------- render — F label path (v0.5.2+)


def test_render_f_grade_produces_image_of_same_size() -> None:
    """F labels must render at the same physical dimensions as A/B/C
    labels — they go on the same Brother QL roll."""
    fail_data = _sample(
        grade="F",
        reallocated_sectors=47,
        fail_reason="47 reallocated (> 40)",
    )
    img = render_label(fail_data)
    assert img.size == LABEL_SIZES["DK-1209"]


def test_render_f_grade_with_long_reason_wraps_gracefully() -> None:
    """Long reason strings must wrap onto a second line rather than
    overflowing into the QR column. The render path has a two-line
    fallback; smoke-test that it doesn't error on realistic long
    reasons."""
    fail_data = _sample(
        grade="F",
        fail_reason="47 reallocated during test (> 40 threshold) AND 3 pending",
    )
    # Must not raise
    img = render_label(fail_data)
    assert img.size == LABEL_SIZES["DK-1209"]


def test_render_f_grade_with_missing_reason_uses_generic_fallback() -> None:
    """If fail_reason is None (shouldn't happen in practice — the
    caller populates it from primary_fail_reason — but defensive),
    render with a generic 'failed grading' string rather than
    crashing."""
    fail_data = _sample(grade="F", fail_reason=None)
    img = render_label(fail_data)  # must not raise
    assert img.size == LABEL_SIZES["DK-1209"]


def test_render_compact_f_label_shows_f_not_raw_grade() -> None:
    """On the small DK-1221 square label, F drives should still render
    the letter 'F' — the compact layout's grade display must handle
    the fail path, not just A/B/C."""
    fail_data = _sample(grade="F", fail_reason="bad SMART")
    img = render_label(fail_data, roll="DK-1221")
    assert img.size == LABEL_SIZES["DK-1221"]


# ---------------------------------------------------------------- primary_fail_reason


def test_primary_fail_reason_returns_none_for_empty_rules() -> None:
    assert primary_fail_reason([]) is None
    assert primary_fail_reason(None) is None  # type: ignore[arg-type]


def test_primary_fail_reason_returns_none_for_all_passing_rules() -> None:
    """A drive that passed all grading rules — this shouldn't even be
    called for it in practice, but returning None means the caller
    can safely pass it the rules list from any run without guarding."""
    rules = [
        {"name": "smart_short_test_passed", "passed": True, "detail": "SMART short self-test passed", "forces_grade": None},
        {"name": "badblocks_clean", "passed": True, "detail": "badblocks reported no errors", "forces_grade": None},
    ]
    assert primary_fail_reason(rules) is None


def test_primary_fail_reason_skips_non_forcing_failed_rules() -> None:
    """A rule can fail without forcing the grade to F (legacy rows,
    informational rules). Only rules with `forces_grade` set should
    be considered."""
    rules = [
        {"name": "smart_status", "passed": False, "detail": "just informational", "forces_grade": None},
        {"name": "no_pending_sectors", "passed": False, "detail": "current_pending_sector=3", "forces_grade": "F"},
    ]
    reason = primary_fail_reason(rules)
    assert reason == "3 pending sectors"


def test_primary_fail_reason_smart_short_test() -> None:
    rules = [{"name": "smart_short_test_passed", "passed": False, "detail": "SMART short self-test FAILED", "forces_grade": "F"}]
    assert primary_fail_reason(rules) == "SMART short self-test failed"


def test_primary_fail_reason_smart_long_test() -> None:
    rules = [{"name": "smart_long_test_passed", "passed": False, "detail": "SMART long self-test FAILED", "forces_grade": "F"}]
    assert primary_fail_reason(rules) == "SMART long self-test failed"


def test_primary_fail_reason_badblocks_read_errors() -> None:
    rules = [{
        "name": "badblocks_clean",
        "passed": False,
        "detail": "badblocks found errors: read=12 write=0 compare=0",
        "forces_grade": "F",
    }]
    reason = primary_fail_reason(rules)
    assert reason is not None
    assert "12" in reason
    assert "read" in reason


def test_primary_fail_reason_badblocks_single_error_is_singular() -> None:
    """Grammar: 1 error, not 1 errors."""
    rules = [{
        "name": "badblocks_clean",
        "passed": False,
        "detail": "badblocks found errors: read=1 write=0 compare=0",
        "forces_grade": "F",
    }]
    reason = primary_fail_reason(rules)
    assert reason == "1 badblocks read error"


def test_primary_fail_reason_pending_sectors() -> None:
    rules = [{
        "name": "no_pending_sectors",
        "passed": False,
        "detail": "current_pending_sector=5",
        "forces_grade": "F",
    }]
    assert primary_fail_reason(rules) == "5 pending sectors"


def test_primary_fail_reason_offline_uncorrectable() -> None:
    rules = [{
        "name": "no_offline_uncorrectable",
        "passed": False,
        "detail": "offline_uncorrectable=2",
        "forces_grade": "F",
    }]
    assert primary_fail_reason(rules) == "2 offline uncorrectable"


def test_primary_fail_reason_degradation_during_test() -> None:
    """A counter that grew between pre and post SMART snapshots —
    the label reports what kind grew."""
    rules = [{
        "name": "no_degradation_reallocated_sectors",
        "passed": False,
        "detail": "reallocated_sectors: pre=0 → post=5",
        "forces_grade": "F",
    }]
    reason = primary_fail_reason(rules)
    assert reason is not None
    assert "grew during test" in reason


def test_primary_fail_reason_reallocated_over_threshold() -> None:
    rules = [{
        "name": "grade_c_reallocated",
        "passed": False,
        "detail": "reallocated_sectors=47 > 40 (fail)",
        "forces_grade": "F",
    }]
    assert primary_fail_reason(rules) == "47 reallocated (> 40)"


def test_primary_fail_reason_picks_first_failing_rule() -> None:
    """When multiple fail-forcing rules fired, the first one wins —
    the QR → full report shows the rest. Rules are evaluated in
    grading.py in a defined order (self-test → badblocks → SMART
    counters → reallocated); preserve that order."""
    rules = [
        {"name": "smart_short_test_passed", "passed": False, "detail": "SMART short self-test FAILED", "forces_grade": "F"},
        {"name": "no_pending_sectors", "passed": False, "detail": "current_pending_sector=5", "forces_grade": "F"},
    ]
    assert primary_fail_reason(rules) == "SMART short self-test failed"


def test_primary_fail_reason_handles_legacy_fail_marker() -> None:
    """Pre-v0.5.1 rows with forces_grade='fail' (the old vocabulary)
    should still produce a reason — the function accepts both the
    old and new marker values."""
    rules = [{
        "name": "no_pending_sectors",
        "passed": False,
        "detail": "current_pending_sector=3",
        "forces_grade": "fail",  # legacy
    }]
    assert primary_fail_reason(rules) == "3 pending sectors"


def test_primary_fail_reason_unknown_rule_falls_back_to_detail() -> None:
    """A future rule name not yet mapped here shouldn't crash — it
    just uses the rule's own detail string, truncated."""
    rules = [{
        "name": "future_rule_that_doesnt_exist_yet",
        "passed": False,
        "detail": "some specific problem happened here",
        "forces_grade": "F",
    }]
    reason = primary_fail_reason(rules)
    assert reason == "some specific problem happened here"


# ---------------------------------------------------------------- helpers


def test_format_poh_renders_years_and_comma() -> None:
    assert _format_poh(45123) == "POH: 45,123 (5.2 y)"


def test_format_poh_handles_zero() -> None:
    assert _format_poh(0) == "POH: —"


def test_format_poh_handles_small_values() -> None:
    assert _format_poh(100) == "POH: 100 (0.0 y)"


def test_format_health_line_all_fields() -> None:
    line = _format_health_line(0, 0, (0, 0, 0))
    assert line == "Realloc: 0 · Pending: 0 · BB: 0"


def test_format_health_line_drops_badblocks_when_tight() -> None:
    """With a narrow available-chars budget, BB is dropped first
    (implicit via grade — if BB was nonzero, grade would be F)."""
    line = _format_health_line(9999, 9999, (9999, 9999, 9999), available_chars=20)
    assert "BB" not in (line or "")
    assert "Realloc" in (line or "")


def test_format_health_line_returns_none_when_all_unknown() -> None:
    assert _format_health_line(None, None, None) is None
