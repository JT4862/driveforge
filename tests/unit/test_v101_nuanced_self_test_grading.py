"""v1.0.1 — nuanced SMART self-test grading.

JT's first 15-drive 6TB enterprise pull batch through the v1.0
fleet capped 12/14 at C from the single pre-v1.0.1 ceiling rule
`no_past_self_test_failure`. That rule fires on ANY past failure
in the drive's SMART self-test log, no matter how ancient or
which test type — over-firing on enterprise drives that ran
weekly long-tests for years and accumulated normal-noise entries.

v1.0.1 splits the single rule into four nuanced cases that
distinguish:
  - test type (short electronics-only vs long full-LBA-scan)
  - recency (failure in last 30% of POH vs ancient with clean
    tests since)
  - clustering (one-off vs multiple recent failures = active
    deterioration)

Tests cover:
  - SMART parser extracts per-entry list with all v1.0.1 fields
  - Each of the 4 grading rule branches fires correctly
  - Backwards compat: pre-v1.0.1 SmartSnapshot rows fall through
    to the v1.0 single rule
  - Toggle: nuanced_self_test_grading=False also falls through
    to v1.0 behavior
  - JT's actual scenario: ancient enterprise-drive self-test
    failure now grades B not C
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from driveforge import config as cfg
from driveforge.core import grading, smart


# ============================================================ Parser


def test_parse_self_test_log_extracts_per_entry_list() -> None:
    """The parser populates `self_test_entries` with all v1.0.1
    fields (test_type, passed, lifetime_hours, lba_first_error,
    remaining_percent) — not just the v1.0 summary."""
    payload = json.dumps({
        "device": {"name": "/dev/sda"},
        "ata_smart_self_test_log": {
            "standard": {
                "table": [
                    {
                        "type": {"string": "Extended offline"},
                        "status": {"passed": False, "remaining_percent": 60},
                        "lifetime_hours": 45120,
                        "lba": 8123456,
                    },
                    {
                        "type": {"string": "Short offline"},
                        "status": {"passed": True},
                        "lifetime_hours": 43000,
                    },
                ],
            },
        },
    })
    snap = smart.parse(payload)
    assert snap.self_test_entries is not None
    assert len(snap.self_test_entries) == 2
    failed, clean = snap.self_test_entries
    assert failed.test_type == "Extended offline"
    assert failed.passed is False
    assert failed.lifetime_hours == 45120
    assert failed.lba_first_error == 8123456
    assert failed.remaining_percent == 60
    assert clean.passed is True
    assert clean.test_type == "Short offline"


def test_parse_self_test_log_entries_none_when_absent() -> None:
    """Drive with no self-test log → `self_test_entries` is None,
    not an empty list. Distinguishes "log section missing" from
    "log section present but no entries"."""
    payload = json.dumps({"device": {"name": "/dev/sda"}})
    snap = smart.parse(payload)
    assert snap.self_test_entries is None


# ============================================================ Helpers for grading tests


def _snap(
    *,
    poh: int = 50_000,
    entries: list[smart.SelfTestEntry] | None = None,
) -> smart.SmartSnapshot:
    """Build a minimal SmartSnapshot with the fields grading actually
    reads. Defaults to a "healthy enterprise drive" baseline so
    each test only has to override what it cares about."""
    has_failure = entries is not None and any(not e.passed for e in entries)
    last_failed = None
    if has_failure:
        for e in entries or []:
            if not e.passed and e.lifetime_hours is not None:
                last_failed = e.lifetime_hours
                break
    return smart.SmartSnapshot(
        device="/dev/sda",
        captured_at=datetime.now(UTC),
        attributes=[],
        raw={},
        power_on_hours=poh,
        smart_status_passed=True,
        reallocated_sectors=0,
        current_pending_sector=0,
        offline_uncorrectable=0,
        self_test_total_count=(len(entries) if entries is not None else None),
        self_test_has_past_failure=(has_failure if entries is not None else None),
        self_test_last_failed_at_hour=last_failed,
        self_test_entries=entries,
    )


def _grade(snap: smart.SmartSnapshot, *, config=None):
    cfg_ = config or cfg.GradingConfig()
    # Disable other ceilings so we're only measuring the self-test
    # rules under test (otherwise age/workload ceilings on a
    # 50,000 POH drive will mask the self-test rule outcome).
    cfg_.age_ceiling_enabled = False
    cfg_.workload_ceiling_enabled = False
    return grading.grade_drive(
        pre=snap, post=snap, config=cfg_,
        short_test_passed=True, long_test_passed=True,
    )


def _rule_names(result) -> list[str]:
    return [r.name for r in result.rules]


# ============================================================ Rule 1: recent long


def test_recent_long_test_failure_caps_at_c() -> None:
    """Long test failed 5,000 POH ago on a 50,000 POH drive
    (recency = 10% — within default 30% window) → C cap."""
    snap = _snap(
        poh=50_000,
        entries=[
            smart.SelfTestEntry(
                test_type="Extended offline", passed=False,
                lifetime_hours=45_000, lba_first_error=8_000_000,
            ),
        ],
    )
    result = _grade(snap)
    assert "no_recent_long_test_failure" in _rule_names(result)
    assert result.grade == "C"


# ============================================================ Rule 2: ancient long + recovered


def test_ancient_long_test_failure_with_clean_tests_caps_at_b() -> None:
    """Long test failed at POH=10,000 on a 50,000 POH drive (recency
    = 80% — well past 30% window) AND 3+ subsequent clean long
    tests → B cap (one tier softer than C)."""
    snap = _snap(
        poh=50_000,
        entries=[
            smart.SelfTestEntry(
                test_type="Extended offline", passed=True, lifetime_hours=49_000,
            ),
            smart.SelfTestEntry(
                test_type="Extended offline", passed=True, lifetime_hours=40_000,
            ),
            smart.SelfTestEntry(
                test_type="Extended offline", passed=True, lifetime_hours=20_000,
            ),
            smart.SelfTestEntry(
                test_type="Extended offline", passed=False,
                lifetime_hours=10_000, lba_first_error=12_345,
            ),
        ],
    )
    result = _grade(snap)
    assert "no_ancient_long_test_failure_or_recovered" in _rule_names(result)
    assert result.grade == "B"


def test_ancient_long_test_failure_without_recovery_still_caps_at_c() -> None:
    """Same ancient failure as above but NO clean tests since →
    still C (no evidence of recovery)."""
    snap = _snap(
        poh=50_000,
        entries=[
            smart.SelfTestEntry(
                test_type="Extended offline", passed=False,
                lifetime_hours=10_000,
            ),
        ],
    )
    result = _grade(snap)
    assert "no_ancient_long_test_failure_without_recovery" in _rule_names(result)
    assert result.grade == "C"


# ============================================================ Rule 3: short-only


def test_short_test_only_failure_caps_at_b() -> None:
    """Short test failed but no long tests failed → B cap.
    Electronics-class signal (heads, read path) but NOT confirmed
    media damage — softer demotion than long-test failure."""
    snap = _snap(
        poh=50_000,
        entries=[
            smart.SelfTestEntry(
                test_type="Short offline", passed=False, lifetime_hours=49_000,
            ),
            smart.SelfTestEntry(
                test_type="Extended offline", passed=True, lifetime_hours=48_000,
            ),
        ],
    )
    result = _grade(snap)
    assert "no_short_test_only_failure" in _rule_names(result)
    assert result.grade == "B"


# ============================================================ Rule 4: cluster


def test_cluster_of_recent_failures_forces_f() -> None:
    """≥2 failures within last 1000 POH → sticky F (active
    deterioration pattern). Strongest signal in the family."""
    snap = _snap(
        poh=50_000,
        entries=[
            smart.SelfTestEntry(
                test_type="Extended offline", passed=False, lifetime_hours=49_800,
            ),
            smart.SelfTestEntry(
                test_type="Extended offline", passed=False, lifetime_hours=49_200,
            ),
        ],
    )
    result = _grade(snap)
    assert "no_recent_cluster_failures" in _rule_names(result)
    assert result.grade == "F"


# ============================================================ Backwards compat


def test_no_failures_no_rules_fire() -> None:
    """Clean drive with all-passing entries → no past-self-test
    rules in the result."""
    snap = _snap(
        poh=50_000,
        entries=[
            smart.SelfTestEntry(
                test_type="Extended offline", passed=True, lifetime_hours=49_000,
            ),
        ],
    )
    result = _grade(snap)
    rule_names = _rule_names(result)
    assert "no_recent_long_test_failure" not in rule_names
    assert "no_ancient_long_test_failure_or_recovered" not in rule_names
    assert "no_ancient_long_test_failure_without_recovery" not in rule_names
    assert "no_short_test_only_failure" not in rule_names
    assert "no_recent_cluster_failures" not in rule_names


def test_pre_v101_snapshot_falls_back_to_v10_rule() -> None:
    """Old SmartSnapshot rows have self_test_entries=None but
    self_test_has_past_failure=True. Grading falls back to the
    v1.0 single ceiling rule so historical data renders sanely."""
    snap = _snap(poh=50_000)
    snap = snap.model_copy(update={
        "self_test_entries": None,
        "self_test_has_past_failure": True,
        "self_test_last_failed_at_hour": 12_345,
    })
    result = _grade(snap)
    assert "no_past_self_test_failure" in _rule_names(result)
    assert result.grade == "C"


def test_nuanced_grading_disabled_falls_back_to_v10_rule() -> None:
    """Operator who explicitly disables the nuanced grading toggle
    in Settings gets the v1.0 single-rule behavior even on
    v1.0.1+ snapshots."""
    snap = _snap(
        poh=50_000,
        entries=[
            smart.SelfTestEntry(
                test_type="Extended offline", passed=False,
                lifetime_hours=10_000,
            ),
        ],
    )
    custom_cfg = cfg.GradingConfig()
    custom_cfg.nuanced_self_test_grading = False
    custom_cfg.cap_c_on_past_self_test_failure = True
    result = _grade(snap, config=custom_cfg)
    assert "no_past_self_test_failure" in _rule_names(result)
    # And the nuanced rules don't fire.
    assert "no_recent_long_test_failure" not in _rule_names(result)
    assert "no_ancient_long_test_failure_or_recovered" not in _rule_names(result)


# ============================================================ JT's xVault scenario


def test_primary_ceiling_reason_returns_strictest() -> None:
    """v1.0.1+ helper picks the strictest ceiling (C beats B) for the
    cert-label headline."""
    from driveforge.core.printer import primary_ceiling_reason
    rules = [
        {"name": "ceiling_a", "passed": True, "forces_grade": "B",
         "detail": "old workload — capped at B"},
        {"name": "ceiling_b", "passed": True, "forces_grade": "C",
         "detail": "drive had recent long-test failure — capped at C"},
    ]
    out = primary_ceiling_reason(rules)
    assert out is not None
    # C-cap wins over B-cap (strictest binds the actual grade).
    assert "long-test" in out
    # Trailing "— capped at X" is stripped (the glyph already shows
    # the tier on the label).
    assert "capped at" not in out


def test_primary_ceiling_reason_returns_none_when_no_ceilings() -> None:
    """Clean A drive with no ceiling rules → None (sticker prints
    no extra reason line)."""
    from driveforge.core.printer import primary_ceiling_reason
    rules = [
        {"name": "smart_short_test_passed", "passed": True,
         "forces_grade": None, "detail": "ok"},
    ]
    assert primary_ceiling_reason(rules) is None


def test_cert_label_renders_ceiling_reason_line() -> None:
    """Pass-tier label with ceiling_reason populated renders a
    'Capped at X: <reason>' line in the body."""
    from datetime import date
    from driveforge.core.printer import CertLabelData, render_label
    data = CertLabelData(
        model="ST6000NM0034", serial="S0M063D3", capacity_tb=6.0,
        grade="C", tested_date=date(2026, 5, 2),
        power_on_hours=49_500,
        report_url="http://op.local:8080/reports/S0M063D3",
        reallocated_sectors=0, current_pending_sector=0,
        badblocks_errors=(0, 0, 0),
        ceiling_reason="drive had a long-test failure within the last 30% of POH",
    )
    img = render_label(data)
    # Smoke test — render returned a non-empty image at expected size.
    assert img.size[0] > 100 and img.size[1] > 100


def test_jt_enterprise_drive_with_ancient_failure_grades_b_not_c() -> None:
    """The actual UX win: an enterprise drive (60,000 POH) that had
    one long-test failure 5 years ago and has run weekly long
    tests cleanly since now grades B, not C. Pre-v1.0.1 this same
    drive would have been C purely from `no_past_self_test_failure`
    firing on the ancient entry."""
    # 60k POH drive (~7 years 24/7), failed once at 10k POH
    # (early life), 12 clean long tests since then (more than the
    # default `ancient_min_clean_since=3` threshold).
    entries = []
    # 12 clean long tests at quarterly cadence after the failure
    for clean_poh in range(58_000, 10_000, -4_000):
        entries.append(smart.SelfTestEntry(
            test_type="Extended offline", passed=True,
            lifetime_hours=clean_poh,
        ))
    # The historical failure
    entries.append(smart.SelfTestEntry(
        test_type="Extended offline", passed=False, lifetime_hours=10_000,
    ))
    snap = _snap(poh=60_000, entries=entries)
    result = _grade(snap)
    assert result.grade == "B", (
        f"expected B (ancient failure + recovery), got {result.grade}"
    )
    assert "no_ancient_long_test_failure_or_recovered" in _rule_names(result)
    # And the recent-failure / cluster rules don't fire.
    assert "no_recent_long_test_failure" not in _rule_names(result)
    assert "no_recent_cluster_failures" not in _rule_names(result)
