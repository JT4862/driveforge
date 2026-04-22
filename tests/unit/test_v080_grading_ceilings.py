"""v0.8.0 — new ceiling + auto-fail grading rules.

Covers:
  - Age ceiling (POH → B/C/F caps)
  - Workload ceiling (lifetime writes vs rated TBW)
  - SSD wear ceiling (percentage_used)
  - NVMe low-spare auto-fail
  - End-to-end error auto-fail
  - NVMe critical_warning auto-fail
  - NVMe media_errors ceiling-C
  - Command timeout ceiling-B
  - Past self-test failure ceiling-C
  - UDMA CRC is explicitly NOT a grading factor (advisory only)
  - Ceilings only demote — they can't rescue a drive that already
    graded worse on another rule.
"""

from __future__ import annotations

from datetime import UTC, datetime

from driveforge.config import GradingConfig
from driveforge.core.grading import Grade, grade_drive
from driveforge.core.smart import SmartSnapshot


def _snap(**kw) -> SmartSnapshot:
    """Build a SmartSnapshot with clean defaults so tests can override
    just the fields they care about. Defaults are 'pristine drive'."""
    defaults = dict(
        device="/dev/sda",
        captured_at=datetime.now(UTC),
        power_on_hours=1000,
        reallocated_sectors=0,
        current_pending_sector=0,
        offline_uncorrectable=0,
        smart_status_passed=True,
    )
    defaults.update(kw)
    return SmartSnapshot(**defaults)


def _pristine_pre():
    return _snap(power_on_hours=500)


# ----------------------------------------------------- age ceiling


def test_poh_within_a_ceiling_stays_at_a() -> None:
    """Drive under the A threshold should grade A (all other rules
    pristine)."""
    cfg = GradingConfig()
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(power_on_hours=10_000),  # well under 35k A ceiling
        config=cfg,
    )
    assert result.grade == Grade.A


def test_poh_above_a_ceiling_caps_at_b() -> None:
    """POH just past the A ceiling but under B ceiling → grade B."""
    cfg = GradingConfig()
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(power_on_hours=40_000),  # > 35040 A ceiling
        config=cfg,
    )
    assert result.grade == Grade.B
    assert any(r.name == "age_ceiling_b" for r in result.rules)


def test_poh_above_b_ceiling_caps_at_c() -> None:
    """70k POH (the JT-Dell-300 case) → capped at C."""
    cfg = GradingConfig()
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(power_on_hours=71_000),
        config=cfg,
    )
    assert result.grade == Grade.C
    assert any(r.name == "age_ceiling_c" for r in result.rules)


def test_poh_fail_ceiling_forces_f_when_configured() -> None:
    """When poh_fail_hours is set, a drive past that threshold auto-F's."""
    cfg = GradingConfig(poh_fail_hours=100_000)
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(power_on_hours=100_500),
        config=cfg,
    )
    assert result.grade == Grade.FAIL


def test_age_ceiling_disabled_skips_rule() -> None:
    """age_ceiling_enabled=False → no ceiling fires even on old drives."""
    cfg = GradingConfig(age_ceiling_enabled=False)
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(power_on_hours=80_000),
        config=cfg,
    )
    assert result.grade == Grade.A


# ----------------------------------------------------- workload ceiling


def test_workload_below_a_ceiling_stays_a() -> None:
    """Drive at 20% of rated TBW → stays A."""
    cfg = GradingConfig()
    # 20% of 2750 TB = 550 TB written
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(lifetime_host_writes_bytes=550 * 1_000_000_000_000),
        config=cfg,
        drive_class="enterprise_hdd",
    )
    assert result.grade == Grade.A


def test_workload_above_a_ceiling_caps_at_b() -> None:
    """75% of rated TBW → above 60% A ceiling → capped at B."""
    cfg = GradingConfig()
    # 75% of 2750 TB enterprise HDD = 2062.5 TB
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(lifetime_host_writes_bytes=int(2062 * 1_000_000_000_000)),
        config=cfg,
        drive_class="enterprise_hdd",
    )
    assert result.grade == Grade.B


def test_workload_above_b_ceiling_caps_at_c() -> None:
    """120% of rated TBW → above 100% B ceiling → capped at C."""
    cfg = GradingConfig()
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(lifetime_host_writes_bytes=int(3300 * 1_000_000_000_000)),
        config=cfg,
        drive_class="enterprise_hdd",
    )
    assert result.grade == Grade.C


def test_workload_above_fail_threshold_forces_f() -> None:
    """160% of rated TBW → past fail threshold (150%) → F."""
    cfg = GradingConfig()
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(lifetime_host_writes_bytes=int(4400 * 1_000_000_000_000)),
        config=cfg,
        drive_class="enterprise_hdd",
    )
    assert result.grade == Grade.FAIL


def test_workload_consumer_tbw_is_much_tighter() -> None:
    """Consumer HDD's 275 TB rating means even modest writes push the
    ceilings. 200 TB written on a consumer HDD is 72% → B cap (not A)."""
    cfg = GradingConfig()
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(lifetime_host_writes_bytes=200 * 1_000_000_000_000),
        config=cfg,
        drive_class="consumer_hdd",
    )
    assert result.grade == Grade.B


def test_workload_ceiling_skipped_without_drive_class() -> None:
    """No drive_class passed → workload rule can't compute a ratio →
    rule silently skips rather than dividing by an arbitrary default."""
    cfg = GradingConfig()
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(lifetime_host_writes_bytes=10_000 * 1_000_000_000_000),  # huge
        config=cfg,
        drive_class=None,
    )
    assert result.grade == Grade.A  # rule didn't fire


# ----------------------------------------------------- SSD wear ceiling


def test_ssd_wear_below_a_ceiling_stays_a() -> None:
    cfg = GradingConfig()
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(wear_pct_used=10),
        config=cfg,
    )
    assert result.grade == Grade.A


def test_ssd_wear_above_a_ceiling_caps_at_b() -> None:
    cfg = GradingConfig()
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(wear_pct_used=25),  # > 20% A ceiling
        config=cfg,
    )
    assert result.grade == Grade.B


def test_ssd_wear_above_b_ceiling_caps_at_c() -> None:
    cfg = GradingConfig()
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(wear_pct_used=60),
        config=cfg,
    )
    assert result.grade == Grade.C


def test_ssd_wear_fail_threshold_forces_f() -> None:
    cfg = GradingConfig()
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(wear_pct_used=95),
        config=cfg,
    )
    assert result.grade == Grade.FAIL


def test_hdd_with_wear_none_skips_rule_cleanly() -> None:
    """HDDs don't report wear_pct_used. Rule should not fire."""
    cfg = GradingConfig()
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(wear_pct_used=None),
        config=cfg,
    )
    assert result.grade == Grade.A


# ---------------------------------------------- NVMe low-spare fail


def test_nvme_low_spare_below_threshold_fails() -> None:
    """Available spare below the drive's own threshold → F. The drive
    firmware is telling us it's near media exhaustion."""
    cfg = GradingConfig()
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(available_spare_pct=5, available_spare_threshold_pct=10),
        config=cfg,
    )
    assert result.grade == Grade.FAIL


def test_nvme_spare_at_threshold_does_not_fail() -> None:
    """Exactly at threshold → strict < comparison, doesn't trip."""
    cfg = GradingConfig()
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(available_spare_pct=10, available_spare_threshold_pct=10),
        config=cfg,
    )
    assert result.grade == Grade.A


# ---------------------------------------------- error-class rules


def test_end_to_end_error_forces_f() -> None:
    """SATA attr 184 > 0 = silent corruption detected. Hard fail."""
    cfg = GradingConfig()
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(end_to_end_error_count=1),
        config=cfg,
    )
    assert result.grade == Grade.FAIL


def test_nvme_critical_warning_forces_f() -> None:
    """Any critical_warning bit set → F."""
    cfg = GradingConfig()
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(nvme_critical_warning=0x04),
        config=cfg,
    )
    assert result.grade == Grade.FAIL


def test_nvme_media_errors_caps_c() -> None:
    """media_errors > 0 = uncorrected NAND error → cap at C (not F by
    itself)."""
    cfg = GradingConfig()
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(nvme_media_errors=3),
        config=cfg,
    )
    assert result.grade == Grade.C


def test_command_timeout_caps_b_above_threshold() -> None:
    """Command timeout count > 5 → cap at B."""
    cfg = GradingConfig()
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(command_timeout_count=10),
        config=cfg,
    )
    assert result.grade == Grade.B


def test_past_self_test_failure_caps_c() -> None:
    """Drive's own self-test log recorded a past failure → cap at C
    even though the short test we just ran passed."""
    cfg = GradingConfig()
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(self_test_has_past_failure=True),
        config=cfg,
    )
    assert result.grade == Grade.C


def test_udma_crc_does_not_affect_grade() -> None:
    """High UDMA CRC error count = cabling issue, not drive issue.
    Must remain advisory; MUST NOT demote the grade."""
    cfg = GradingConfig()
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(udma_crc_error_count=500),  # alarming-looking number
        config=cfg,
    )
    assert result.grade == Grade.A  # still A
    # But there IS an advisory rule present so the buyer report can show it.
    assert any(r.name == "udma_crc_cabling_advisory" for r in result.rules)


# --------------------------------------- error rules globally disableable


def test_error_rules_disabled_skips_all() -> None:
    """error_rules_enabled=False prevents the whole category — even
    drives with end_to_end errors + critical warnings grade normally."""
    cfg = GradingConfig(error_rules_enabled=False)
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(
            end_to_end_error_count=5,
            nvme_critical_warning=0x01,
            command_timeout_count=100,
        ),
        config=cfg,
    )
    assert result.grade == Grade.A


# --------------------------------------- ceiling-only semantics


def test_ceilings_never_promote_a_failed_drive() -> None:
    """A drive that fails the base counter rule must stay F even
    though age ceiling says B. Ceilings only demote, never promote."""
    cfg = GradingConfig()
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(
            power_on_hours=40_000,  # would cap at B on its own
            reallocated_sectors=500,  # but reallocations force F
        ),
        config=cfg,
    )
    assert result.grade == Grade.FAIL


def test_worst_ceiling_wins_when_multiple_fire() -> None:
    """Drive that hits multiple ceilings: worst one wins. POH 70k caps
    at C; wear 25% caps at B. Final grade = C (worse of the two)."""
    cfg = GradingConfig()
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(power_on_hours=70_000, wear_pct_used=25),
        config=cfg,
    )
    assert result.grade == Grade.C


# --------------------------------------- rationale content


def test_rule_detail_text_is_operator_friendly() -> None:
    """The 'detail' string for each new rule should be readable by a
    human — it's what the buyer-rationale panel renders."""
    cfg = GradingConfig()
    result = grade_drive(
        pre=_pristine_pre(),
        post=_snap(power_on_hours=70_000),
        config=cfg,
    )
    age_rule = next(r for r in result.rules if r.name == "age_ceiling_c")
    assert "70,000 POH" in age_rule.detail or "70000" in age_rule.detail
    assert "yrs" in age_rule.detail.lower()
    assert "C" in age_rule.detail  # mentions the cap tier
