from __future__ import annotations

from datetime import UTC, datetime

from driveforge.config import GradingConfig
from driveforge.core import grading
from driveforge.core.smart import SmartSnapshot


def _snap(**overrides) -> SmartSnapshot:
    defaults = dict(
        device="/dev/sda",
        captured_at=datetime.now(UTC),
        reallocated_sectors=0,
        current_pending_sector=0,
        offline_uncorrectable=0,
        smart_status_passed=True,
    )
    defaults.update(overrides)
    return SmartSnapshot(**defaults)


def _config() -> GradingConfig:
    return GradingConfig()


def test_pristine_drive_gets_grade_a() -> None:
    pre = _snap()
    post = _snap()
    result = grading.grade_drive(pre=pre, post=post, config=_config())
    assert result.grade == grading.Grade.A
    assert result.passed


def test_small_reallocated_count_is_grade_b() -> None:
    pre = _snap(reallocated_sectors=5)
    post = _snap(reallocated_sectors=5)
    result = grading.grade_drive(pre=pre, post=post, config=_config())
    assert result.grade == grading.Grade.B


def test_larger_reallocated_count_is_grade_c() -> None:
    pre = _snap(reallocated_sectors=20)
    post = _snap(reallocated_sectors=20)
    result = grading.grade_drive(pre=pre, post=post, config=_config())
    assert result.grade == grading.Grade.C


def test_pending_sector_forces_fail() -> None:
    pre = _snap()
    post = _snap(current_pending_sector=1)
    result = grading.grade_drive(pre=pre, post=post, config=_config())
    assert result.grade == grading.Grade.FAIL


def test_degradation_between_pre_and_post_forces_fail() -> None:
    pre = _snap(reallocated_sectors=3)
    post = _snap(reallocated_sectors=5)  # got worse
    result = grading.grade_drive(pre=pre, post=post, config=_config())
    assert result.grade == grading.Grade.FAIL


def test_bad_short_test_fails() -> None:
    result = grading.grade_drive(
        pre=_snap(), post=_snap(), config=_config(), short_test_passed=False
    )
    assert result.grade == grading.Grade.FAIL


def test_thermal_excursion_demotes_to_c() -> None:
    result = grading.grade_drive(
        pre=_snap(), post=_snap(), config=_config(), max_temperature_c=80
    )
    assert result.grade == grading.Grade.C


def test_rationale_is_populated() -> None:
    pre = _snap()
    post = _snap()
    result = grading.grade_drive(pre=pre, post=post, config=_config())
    assert "Grade A" in result.rationale
    assert len(result.rules) > 0
