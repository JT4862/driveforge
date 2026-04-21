"""Drive grading — A / B / C / Fail.

Input: a pre-test SMART snapshot and a post-test snapshot + test outcomes.
Output: a `GradingResult` with grade, rationale, and which rules fired.

Grading is deliberately transparent: every grade includes a list of the
specific rule evaluations that led to it. The UI surfaces this so users
understand the verdict instead of trusting a black-box tier.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from driveforge.config import GradingConfig
from driveforge.core.smart import SmartSnapshot


class Grade(str, Enum):
    """Grading verdicts. A/B/C/F are VERDICTS ABOUT THE DRIVE — they
    come from the grading rules applied to SMART data, badblocks
    output, and self-test results. Distinct from pipeline errors
    (represented separately as `grade="error"` at the DB layer),
    which are verdicts about the software, not about the drive.

    Naming change from v0.5.1: FAIL.value was "fail" (the same string
    `_record_failure` wrote for pipeline errors — the two were
    indistinguishable at the DB layer). Now FAIL.value is "F" and
    pipeline errors use "error". See docs/reference/grading.md for
    the full vocabulary.
    """

    A = "A"
    B = "B"
    C = "C"
    FAIL = "F"


class Rule(BaseModel):
    """One evaluation step in the grading pipeline."""

    name: str
    passed: bool
    detail: str
    forces_grade: Grade | None = None  # if set, clamps the grade at this tier


class GradingResult(BaseModel):
    grade: Grade
    rules: list[Rule]
    rationale: str  # human-readable summary

    @property
    def passed(self) -> bool:
        return self.grade != Grade.FAIL


def _rule(name: str, passed: bool, detail: str, *, fail_tier: Grade | None = None) -> Rule:
    return Rule(name=name, passed=passed, detail=detail, forces_grade=fail_tier if not passed else None)


def grade_drive(
    pre: SmartSnapshot,
    post: SmartSnapshot,
    *,
    config: GradingConfig,
    short_test_passed: bool | None = True,
    long_test_passed: bool | None = True,
    badblocks_errors: tuple[int, int, int] = (0, 0, 0),
    max_temperature_c: int | None = None,
) -> GradingResult:
    """Apply grading rules against pre/post snapshots + test outcomes.

    `short_test_passed` / `long_test_passed` accept True / False / None:
      True  = test ran and passed
      False = test ran and failed (grades drive as Fail)
      None  = test not supported on this drive (neutral; rationale notes it)
    """
    rules: list[Rule] = []

    # --- Self-test rules (None = neutral, False = fail) ---
    if short_test_passed is None:
        rules.append(Rule(
            name="smart_short_test_passed",
            passed=True,
            detail="SMART short self-test not supported — skipped",
        ))
    else:
        rules.append(
            _rule(
                "smart_short_test_passed",
                short_test_passed,
                "SMART short self-test passed" if short_test_passed else "SMART short self-test FAILED",
                fail_tier=Grade.FAIL,
            )
        )
    if long_test_passed is None:
        rules.append(Rule(
            name="smart_long_test_passed",
            passed=True,
            detail="SMART long self-test not supported — skipped",
        ))
    else:
        rules.append(
            _rule(
                "smart_long_test_passed",
                long_test_passed,
                "SMART long self-test passed" if long_test_passed else "SMART long self-test FAILED",
                fail_tier=Grade.FAIL,
            )
        )
    bb_total = sum(badblocks_errors)
    rules.append(
        _rule(
            "badblocks_clean",
            bb_total == 0,
            "badblocks reported no errors"
            if bb_total == 0
            else f"badblocks found errors: read={badblocks_errors[0]} write={badblocks_errors[1]} compare={badblocks_errors[2]}",
            fail_tier=Grade.FAIL,
        )
    )

    if config.fail_on_pending_sectors:
        pending = post.current_pending_sector or 0
        rules.append(
            _rule(
                "no_pending_sectors",
                pending == 0,
                f"current_pending_sector={pending}",
                fail_tier=Grade.FAIL,
            )
        )
    if config.fail_on_offline_uncorrectable:
        offu = post.offline_uncorrectable or 0
        rules.append(
            _rule(
                "no_offline_uncorrectable",
                offu == 0,
                f"offline_uncorrectable={offu}",
                fail_tier=Grade.FAIL,
            )
        )

    # Degradation: if any of the key counters got worse between pre and post,
    # that's an automatic fail (the drive actively deteriorated on the bench).
    for attr in ("reallocated_sectors", "current_pending_sector", "offline_uncorrectable"):
        pre_v = getattr(pre, attr) or 0
        post_v = getattr(post, attr) or 0
        degraded = post_v > pre_v
        rules.append(
            _rule(
                f"no_degradation_{attr}",
                not degraded,
                f"{attr}: pre={pre_v} → post={post_v}",
                fail_tier=Grade.FAIL if degraded else None,
            )
        )

    # --- Tier rules (determine A vs B vs C) ---
    realloc = post.reallocated_sectors or 0
    if realloc <= config.grade_a_reallocated_max:
        tier_rule = _rule("grade_a_reallocated", True, f"reallocated_sectors={realloc} ≤ {config.grade_a_reallocated_max} (A)")
        tier_cap = Grade.A
    elif realloc <= config.grade_b_reallocated_max:
        tier_rule = _rule(
            "grade_b_reallocated",
            True,
            f"reallocated_sectors={realloc} ≤ {config.grade_b_reallocated_max} (B)",
        )
        tier_cap = Grade.B
    elif realloc <= config.grade_c_reallocated_max:
        tier_rule = _rule(
            "grade_c_reallocated",
            True,
            f"reallocated_sectors={realloc} ≤ {config.grade_c_reallocated_max} (C)",
        )
        tier_cap = Grade.C
    else:
        tier_rule = _rule(
            "grade_c_reallocated",
            False,
            f"reallocated_sectors={realloc} > {config.grade_c_reallocated_max} (fail)",
            fail_tier=Grade.FAIL,
        )
        tier_cap = Grade.FAIL
    rules.append(tier_rule)

    # Thermal excursion — optional; only demotes, doesn't fail outright.
    if config.thermal_excursion_c is not None and max_temperature_c is not None:
        overheated = max_temperature_c > config.thermal_excursion_c
        rules.append(
            _rule(
                "no_thermal_excursion",
                not overheated,
                f"max_temp={max_temperature_c}°C (threshold {config.thermal_excursion_c}°C)",
                fail_tier=Grade.C if overheated else None,  # demote to C, not fail
            )
        )

    # --- Resolve final grade ---
    worst: Grade = Grade.A
    order = {Grade.A: 0, Grade.B: 1, Grade.C: 2, Grade.FAIL: 3}
    for r in rules:
        if r.forces_grade and order[r.forces_grade] > order[worst]:
            worst = r.forces_grade
    # Cap at the tier the reallocated-sector count allows
    if order[tier_cap] > order[worst]:
        worst = tier_cap

    failures = [r.detail for r in rules if not r.passed]
    if worst == Grade.FAIL:
        rationale = "Failed: " + "; ".join(failures) if failures else "Failed"
    else:
        rationale = f"Grade {worst.value}: {tier_rule.detail}"
    return GradingResult(grade=worst, rules=rules, rationale=rationale)
