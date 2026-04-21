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


def test_grade_enum_values_match_v0_5_1_vocabulary() -> None:
    """v0.5.1 renamed Grade.FAIL.value from 'fail' to 'F' to disambiguate
    it from pipeline-error (grade='error' in the DB). If someone reverts
    this to 'fail', the DB-layer distinction between drive-fail and
    pipeline-error collapses and the UI / auto-enroll / sticky-retry
    behavior all break in ways that are hard to notice in isolation.
    Fail loudly if the vocabulary regresses."""
    assert grading.Grade.A.value == "A"
    assert grading.Grade.B.value == "B"
    assert grading.Grade.C.value == "C"
    assert grading.Grade.FAIL.value == "F", (
        "Grade.FAIL.value must be 'F' (v0.5.1+). The literal string 'fail' "
        "is now RESERVED for legacy pre-v0.5.1 DB rows and is no longer "
        "written by the grading layer."
    )


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
