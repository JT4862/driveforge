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
from driveforge.core.throughput import ThroughputStats


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


def _ceiling(name: str, detail: str, tier: Grade) -> Rule:
    """v0.8.0+ ceiling rule — the rule PASSES (there's nothing wrong
    with the drive) but CAPS the grade at `tier`. Distinct from `_rule`
    which only applies a `forces_grade` when the rule fails. Ceilings
    are the "you still have all your counters clean, but your POH /
    workload / wear disqualifies you from Grade A" pattern.
    """
    return Rule(name=name, passed=True, detail=detail, forces_grade=tier)


def grade_drive(
    pre: SmartSnapshot,
    post: SmartSnapshot,
    *,
    config: GradingConfig,
    short_test_passed: bool | None = True,
    long_test_passed: bool | None = True,
    badblocks_errors: tuple[int, int, int] = (0, 0, 0),
    max_temperature_c: int | None = None,
    throughput: ThroughputStats | None = None,
    drive_class: str | None = None,
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
    # A missing pre-snapshot (legacy row, smartctl transient failure) is
    # treated as "unknown, can't prove degradation" — the rule passes neutrally
    # rather than manufacturing a False positive against an absent baseline.
    for attr in ("reallocated_sectors", "current_pending_sector", "offline_uncorrectable"):
        pre_v = getattr(pre, attr)
        post_v = getattr(post, attr)
        if pre_v is None or post_v is None:
            rules.append(
                Rule(
                    name=f"no_degradation_{attr}",
                    passed=True,
                    detail=f"{attr}: pre/post comparison skipped (pre={pre_v}, post={post_v})",
                )
            )
            continue
        degraded = post_v > pre_v
        rules.append(
            _rule(
                f"no_degradation_{attr}",
                not degraded,
                f"{attr}: pre={pre_v} \u2192 post={post_v}",
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

    # v0.5.6+ throughput-consistency rules. Both are self-referential
    # (drive vs itself) to avoid the "maintain a per-SKU benchmark
    # table" trap. See driveforge/core/throughput.py for design rationale.
    #
    # Rule applies only when throughput stats are present AND non-None
    # (full pipeline runs where diskstats was available). Quick-pass
    # runs, legacy rows, and runs where diskstats failed all skip
    # these rules rather than firing false positives on missing data.
    if throughput is not None and throughput.mean_mbps is not None:
        # Rule 1 — within-pass variance. If p5 dropped below
        # within_pass_variance_ratio × mean, the drive had significant
        # mid-pass slowdowns (signal of sector recovery via internal
        # ECC retry). Demotes one tier; does not F outright.
        if config.within_pass_variance_ratio is not None and throughput.mean_mbps > 0:
            ratio = (throughput.p5_mbps or 0) / throughput.mean_mbps
            threshold = config.within_pass_variance_ratio
            consistent = ratio >= threshold
            rules.append(
                _rule(
                    "throughput_within_pass_consistent",
                    consistent,
                    f"p5/mean={ratio:.2f} (threshold {threshold:.2f}); "
                    f"p5={throughput.p5_mbps:.0f} mean={throughput.mean_mbps:.0f} MB/s",
                    fail_tier=Grade.C if not consistent else None,
                )
            )

        # Rule 2 — pass-to-pass degradation. Compare the LAST pass
        # mean to pass 2's mean (pass 1 skipped to avoid false-firing
        # on SLC-cache exhaustion in consumer SSDs, where pass 1 is
        # cached-fast and pass 2+ are sustained-slow). Fires F if
        # the drive visibly degraded during burn-in.
        #
        # Needs at least 3 passes completed (pass 1 skipped, need
        # pass 2 and a later pass to compare). Short/aborted runs
        # skip silently rather than firing on partial data.
        if (
            config.pass_to_pass_degradation_ratio is not None
            and len(throughput.per_pass_means) >= 3
        ):
            baseline = throughput.per_pass_means[1]  # pass 2 (index 1)
            final = throughput.per_pass_means[-1]
            threshold = config.pass_to_pass_degradation_ratio
            ratio = final / baseline if baseline > 0 else 1.0
            stable = ratio >= threshold
            rules.append(
                _rule(
                    "throughput_pass_to_pass_stable",
                    stable,
                    f"last-pass/pass-2 ratio={ratio:.2f} (threshold {threshold:.2f}); "
                    f"pass2={baseline:.0f} last={final:.0f} MB/s "
                    f"across {len(throughput.per_pass_means)} passes",
                    fail_tier=Grade.FAIL if not stable else None,
                )
            )

    # =========================================================
    # v0.8.0+ buyer-transparency grading rules. All new rules use
    # `forces_grade=...` as a CEILING (the existing grade-resolution
    # logic below takes max(forces_grade_across_all_rules), which
    # means setting forces_grade=Grade.B reads as "can't be better
    # than B"). Ceilings are honest about what they are: a drive
    # with pristine error counters can still be capped at B or C
    # by age / workload / wear — these reflect real-world reliability
    # that SMART counters alone miss.
    #
    # Every new rule is individually disableable via GradingConfig
    # toggles so operators can experiment or soften specific signals
    # without turning off the whole category.

    # --- Age-based ceilings (POH) ---
    if config.age_ceiling_enabled and post.power_on_hours is not None:
        poh = post.power_on_hours
        years = poh / 8760.0
        if config.poh_fail_hours is not None and poh > config.poh_fail_hours:
            rules.append(_rule(
                "age_ceiling_fail",
                False,
                f"{poh:,} POH ({years:.1f} yrs 24/7) exceeds fail threshold {config.poh_fail_hours:,}",
                fail_tier=Grade.FAIL,
            ))
        elif poh > config.poh_b_ceiling_hours:
            rules.append(_ceiling(
                "age_ceiling_c",
                f"{poh:,} POH ({years:.1f} yrs) exceeds B ceiling {config.poh_b_ceiling_hours:,} — capped at C",
                Grade.C,
            ))
        elif poh > config.poh_a_ceiling_hours:
            rules.append(_ceiling(
                "age_ceiling_b",
                f"{poh:,} POH ({years:.1f} yrs) exceeds A ceiling {config.poh_a_ceiling_hours:,} — capped at B",
                Grade.B,
            ))
        else:
            rules.append(Rule(
                name="age_ceiling_a_ok",
                passed=True,
                detail=f"{poh:,} POH ({years:.1f} yrs) within A ceiling {config.poh_a_ceiling_hours:,}",
            ))

    # --- Workload ceilings (lifetime writes vs rated TBW) ---
    if (
        config.workload_ceiling_enabled
        and post.lifetime_host_writes_bytes is not None
        and drive_class is not None
    ):
        rated_tbw_map = {
            "enterprise_hdd": config.rated_tbw_enterprise_hdd,
            "enterprise_ssd": config.rated_tbw_enterprise_ssd,
            "consumer_hdd": config.rated_tbw_consumer_hdd,
            "consumer_ssd": config.rated_tbw_consumer_ssd,
        }
        rated_tb = rated_tbw_map.get(drive_class)
        if rated_tb is not None and rated_tb > 0:
            written_tb = post.lifetime_host_writes_bytes / 1_000_000_000_000
            pct = (written_tb / rated_tb) * 100
            if pct > config.workload_fail_pct:
                rules.append(_rule(
                    "workload_ceiling_fail",
                    False,
                    f"{written_tb:.1f} TB written = {pct:.0f}% of rated {rated_tb} TB "
                    f"({drive_class}) — exceeds fail threshold {config.workload_fail_pct}%",
                    fail_tier=Grade.FAIL,
                ))
            elif pct > config.workload_b_ceiling_pct:
                rules.append(_ceiling(
                    "workload_ceiling_c",
                    f"{written_tb:.1f} TB written = {pct:.0f}% of rated {rated_tb} TB "
                    f"({drive_class}) — exceeds B ceiling, capped at C",
                    Grade.C,
                ))
            elif pct > config.workload_a_ceiling_pct:
                rules.append(_ceiling(
                    "workload_ceiling_b",
                    f"{written_tb:.1f} TB written = {pct:.0f}% of rated {rated_tb} TB "
                    f"({drive_class}) — exceeds A ceiling, capped at B",
                    Grade.B,
                ))
            else:
                rules.append(Rule(
                    name="workload_ceiling_a_ok",
                    passed=True,
                    detail=f"{written_tb:.1f} TB written = {pct:.0f}% of rated {rated_tb} TB "
                           f"({drive_class}) — within A ceiling",
                ))

    # --- SSD wear ceilings ---
    if config.ssd_wear_ceiling_enabled and post.wear_pct_used is not None:
        wear = post.wear_pct_used
        if wear > config.ssd_wear_fail_pct:
            rules.append(_rule(
                "ssd_wear_fail",
                False,
                f"SSD wear {wear}% exceeds fail threshold {config.ssd_wear_fail_pct}%",
                fail_tier=Grade.FAIL,
            ))
        elif wear > config.ssd_wear_b_ceiling_pct:
            rules.append(_ceiling(
                "ssd_wear_ceiling_c",
                f"SSD wear {wear}% exceeds B ceiling {config.ssd_wear_b_ceiling_pct}% — capped at C",
                Grade.C,
            ))
        elif wear > config.ssd_wear_a_ceiling_pct:
            rules.append(_ceiling(
                "ssd_wear_ceiling_b",
                f"SSD wear {wear}% exceeds A ceiling {config.ssd_wear_a_ceiling_pct}% — capped at B",
                Grade.B,
            ))
        else:
            rules.append(Rule(
                name="ssd_wear_a_ok",
                passed=True,
                detail=f"SSD wear {wear}% within A ceiling {config.ssd_wear_a_ceiling_pct}%",
            ))

    # --- NVMe low-spare auto-fail ---
    # Firmware is telling us the drive is running out of error-recovery
    # headroom. Any such drive must F regardless of other signals.
    if (
        config.fail_on_low_nvme_spare
        and post.available_spare_pct is not None
        and post.available_spare_threshold_pct is not None
        and post.available_spare_pct < post.available_spare_threshold_pct
    ):
        rules.append(_rule(
            "nvme_spare_above_threshold",
            False,
            f"NVMe available_spare={post.available_spare_pct}% below "
            f"drive-reported threshold={post.available_spare_threshold_pct}%",
            fail_tier=Grade.FAIL,
        ))

    # --- Error-class auto-fail / ceiling rules ---
    if config.error_rules_enabled:
        # SATA end-to-end error (attr 184) — silent corruption detected.
        if config.fail_on_end_to_end_error:
            e2e = post.end_to_end_error_count
            if e2e is not None and e2e > 0:
                rules.append(_rule(
                    "no_end_to_end_errors",
                    False,
                    f"end_to_end_error_count={e2e} — drive detected silent data corruption",
                    fail_tier=Grade.FAIL,
                ))

        # NVMe critical_warning — any bit = firmware alert
        if config.fail_on_nvme_critical_warning:
            cw = post.nvme_critical_warning
            if cw is not None and cw != 0:
                rules.append(_rule(
                    "nvme_no_critical_warning",
                    False,
                    f"nvme critical_warning bitfield=0x{cw:02x} (drive firmware alert)",
                    fail_tier=Grade.FAIL,
                ))

        # NVMe media_errors — any uncorrected = cap at C
        if config.cap_c_on_nvme_media_errors:
            me = post.nvme_media_errors
            if me is not None and me > 0:
                rules.append(_ceiling(
                    "nvme_no_media_errors",
                    f"nvme media_errors={me} — capped at C",
                    Grade.C,
                ))

        # SATA command timeout count — > threshold = cap at B
        if config.command_timeout_b_ceiling is not None:
            ct = post.command_timeout_count
            if ct is not None and ct > config.command_timeout_b_ceiling:
                rules.append(_ceiling(
                    "command_timeout_ok",
                    f"command_timeout_count={ct} exceeds ceiling "
                    f"{config.command_timeout_b_ceiling} — capped at B",
                    Grade.B,
                ))

        # Self-test log history — past long-test failure caps at C
        # even if the short test we just ran passed.
        if config.cap_c_on_past_self_test_failure and post.self_test_has_past_failure:
            failed_at = post.self_test_last_failed_at_hour
            rules.append(_ceiling(
                "no_past_self_test_failure",
                f"drive's own self-test log shows a past failure"
                + (f" at POH={failed_at:,}" if failed_at else "")
                + " — capped at C",
                Grade.C,
            ))

    # --- UDMA CRC — explicitly NOT counted against the drive ---
    # High CRC count means the CABLE / backplane is bad, not the
    # drive. Surface as an advisory rule (always passes) so the
    # buyer-facing report can show "check cabling" without penalizing
    # a drive that's otherwise fine.
    if post.udma_crc_error_count is not None and post.udma_crc_error_count > 0:
        rules.append(Rule(
            name="udma_crc_cabling_advisory",
            passed=True,
            detail=f"udma_crc_error_count={post.udma_crc_error_count} — points at cabling/connector, "
                   f"NOT drive fault. Does not affect grade.",
        ))

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
