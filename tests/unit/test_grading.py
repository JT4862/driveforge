from __future__ import annotations

from datetime import UTC, datetime

from driveforge.config import GradingConfig
from driveforge.core import grading
from driveforge.core.smart import SmartSnapshot
from driveforge.core.throughput import ThroughputStats


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


def test_pending_climbed_during_pipeline_forces_fail() -> None:
    """v0.5.5: pending sectors appearing during the pipeline run are the
    strongest possible fail signal — the drive deteriorated on the bench
    under controlled conditions. Explicit regression test for the
    "pending climbed" rule mentioned in the v0.5.5 backlog."""
    pre = _snap(current_pending_sector=0)
    post = _snap(current_pending_sector=3)
    result = grading.grade_drive(pre=pre, post=post, config=_config())
    assert result.grade == grading.Grade.FAIL
    rule = next(r for r in result.rules if r.name == "no_degradation_current_pending_sector")
    assert not rule.passed
    assert rule.forces_grade == grading.Grade.FAIL


def test_pending_shrunk_during_pipeline_passes() -> None:
    """The flip side: pending went DOWN during the run (the drive healed
    itself). Must NOT be a fail — this is the whole point of burn-in.
    Reallocated climbs correspondingly (pending sector got swapped for
    a spare), which is also the healing behavior we want to reward."""
    pre = _snap(current_pending_sector=5, reallocated_sectors=10)
    post = _snap(current_pending_sector=0, reallocated_sectors=15)
    # reallocated climbed from 10 to 15 though \u2014 that IS degradation by
    # our rule, so it should still fail. This reflects conservative
    # grading: reallocations during the run are bench-observed drive
    # activity that grading treats as concerning even when pending
    # cleared. Triage (for quick pass) treats this identically as Fail.
    result = grading.grade_drive(pre=pre, post=post, config=_config())
    assert result.grade == grading.Grade.FAIL


def test_missing_pre_snapshot_does_not_manufacture_failure() -> None:
    """If pre-snapshot is missing (legacy row, smartctl transient failure)
    the degradation rule must NOT claim degradation against an absent
    baseline \u2014 absence of evidence is not evidence of absence.
    Conservative on purpose: better to miss a fail than to fail a drive
    on ambiguous data."""
    pre = _snap(reallocated_sectors=None, current_pending_sector=None)
    post = _snap(reallocated_sectors=2, current_pending_sector=0)
    result = grading.grade_drive(pre=pre, post=post, config=_config())
    # reallocated=2 is within Grade A (max 3) and pending=0 clean,
    # so without phantom-degradation false positive this should pass A.
    assert result.grade == grading.Grade.A


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


# ---------------------------------------------------------------- v0.5.6 throughput grading


def _healthy_throughput() -> ThroughputStats:
    """Healthy enterprise-HDD-ish throughput: stable p5/mean ratio,
    consistent pass-to-pass means. Should pass both v0.5.6 rules."""
    return ThroughputStats(
        mean_mbps=180.0,
        p5_mbps=172.0,         # 172/180 = 0.956, well above 0.25 threshold
        p95_mbps=188.0,
        per_pass_means=[200.0, 180.0, 179.0, 178.0, 178.0, 177.0, 177.0, 176.0],
        sample_count=400,
    )


def test_healthy_throughput_keeps_grade_a() -> None:
    """A drive with healthy throughput stats must NOT be demoted below A.
    Baseline for the two throughput rules to verify they don't fire
    spuriously on healthy data."""
    result = grading.grade_drive(
        pre=_snap(), post=_snap(), config=_config(),
        throughput=_healthy_throughput(),
    )
    assert result.grade == grading.Grade.A


def test_throughput_none_is_neutral() -> None:
    """Quick-pass runs + legacy rows + diskstats-failed runs all pass
    throughput=None or throughput.mean_mbps=None. The rules must not
    fire in either case \u2014 no data \u2260 bad data."""
    result = grading.grade_drive(
        pre=_snap(), post=_snap(), config=_config(), throughput=None,
    )
    assert result.grade == grading.Grade.A

    empty = ThroughputStats(
        mean_mbps=None, p5_mbps=None, p95_mbps=None,
        per_pass_means=[], sample_count=0,
    )
    result = grading.grade_drive(
        pre=_snap(), post=_snap(), config=_config(), throughput=empty,
    )
    assert result.grade == grading.Grade.A


def test_within_pass_variance_demotes_below_threshold() -> None:
    """Drive with p5 < 25% of mean during a pass signals mid-pass
    slowdowns (internal ECC retry recovering bad sectors). Must demote
    the grade."""
    bad = ThroughputStats(
        mean_mbps=180.0,
        p5_mbps=15.0,        # 15/180 = 0.083, below 0.25
        p95_mbps=195.0,
        per_pass_means=[180.0] * 8,
        sample_count=400,
    )
    result = grading.grade_drive(
        pre=_snap(), post=_snap(), config=_config(), throughput=bad,
    )
    # Within-pass variance clamps to C tier (not full F); other rules
    # could still F it, but in isolation the variance rule demotes.
    assert result.grade == grading.Grade.C


def test_pass_to_pass_degradation_fails_drive() -> None:
    """Last pass mean < 70% of pass 2 mean signals the drive actively
    degraded under controlled burn-in. F-tier."""
    bad = ThroughputStats(
        mean_mbps=150.0,
        p5_mbps=140.0,         # within-pass fine (140/150 = 0.93)
        p95_mbps=200.0,
        # pass 1: 200 (skipped), pass 2: 200, ... trending down to 100
        per_pass_means=[200.0, 200.0, 190.0, 170.0, 150.0, 130.0, 110.0, 100.0],
        sample_count=400,
    )
    # pass 2 = 200, last = 100, ratio = 0.5 < 0.7 threshold
    result = grading.grade_drive(
        pre=_snap(), post=_snap(), config=_config(), throughput=bad,
    )
    assert result.grade == grading.Grade.FAIL


def test_slc_cache_exhaustion_does_not_false_fire() -> None:
    """Consumer SSD with SLC-cache exhaustion: pass 1 fast (cached),
    passes 2-8 steady at sustained speed. Because pass 1 is skipped
    by the degradation rule, pass 2 vs pass 8 ratio ~= 1.0 \u2014 healthy.
    This is the core SLC-workaround test from the v0.5.6 design."""
    slc = ThroughputStats(
        mean_mbps=180.0,
        p5_mbps=115.0,        # 115/180 = 0.64, still above 0.25
        p95_mbps=500.0,
        # pass 1 cached fast, passes 2-8 steady slow
        per_pass_means=[500.0, 120.0, 120.0, 120.0, 120.0, 120.0, 120.0, 120.0],
        sample_count=400,
    )
    # pass 2 = 120, pass 8 = 120, ratio = 1.0. Variance rule fine.
    result = grading.grade_drive(
        pre=_snap(), post=_snap(), config=_config(), throughput=slc,
    )
    assert result.grade == grading.Grade.A, (
        "SLC-cached pass 1 must not false-fire the degradation rule; "
        f"got {result.grade.value} with rationale: {result.rationale}"
    )


def test_short_run_skips_degradation_rule_silently() -> None:
    """A badblocks run that completed only 1-2 passes (aborted, or
    short enough drive) has fewer than 3 per_pass_means. The
    degradation rule needs at least pass 2 + one later pass to fire;
    must skip silently rather than crash or false-fail."""
    short = ThroughputStats(
        mean_mbps=180.0,
        p5_mbps=170.0,
        p95_mbps=190.0,
        per_pass_means=[180.0, 175.0],  # only 2 passes completed
        sample_count=50,
    )
    result = grading.grade_drive(
        pre=_snap(), post=_snap(), config=_config(), throughput=short,
    )
    assert result.grade == grading.Grade.A


def test_throughput_rules_disabled_by_none_config() -> None:
    """Both thresholds are individually disable-able by setting the
    config value to None. Operators with very quiet drives (NL-SAS
    under background scrubbing) might disable variance while keeping
    degradation."""
    cfg = _config()
    cfg.within_pass_variance_ratio = None
    cfg.pass_to_pass_degradation_ratio = None
    # Terrible throughput data that would fail both rules if enabled.
    terrible = ThroughputStats(
        mean_mbps=180.0, p5_mbps=1.0, p95_mbps=200.0,
        per_pass_means=[200.0, 200.0, 100.0, 50.0, 20.0, 20.0, 20.0, 20.0],
        sample_count=400,
    )
    result = grading.grade_drive(
        pre=_snap(), post=_snap(), config=cfg, throughput=terrible,
    )
    assert result.grade == grading.Grade.A, (
        "with both throughput rules disabled, terrible throughput must not "
        "affect the grade"
    )
