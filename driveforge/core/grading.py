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


def _apply_nuanced_self_test_rules(rules: list, post, config) -> None:
    """v1.0.1+ — split the v1.0 single past-self-test ceiling into a
    family of nuanced rules. Each rule fires only on its specific
    pattern; the strictest forces_grade wins the final ceiling.

    The four cases:

      1. RECENT long-test failure (within `recent_failure_window_pct`
         of current POH) → cap at C. Same severity as v1.0's blanket
         rule but only fires when the failure is genuinely recent.
      2. ANCIENT long-test failure (>1 - recent_window_pct ago) AND
         enough subsequent clean tests → cap at B. One tier softer,
         on the principle that an ancient failure with proven
         recovery is meaningfully less concerning than a recent one.
      3. SHORT-test-only failures (no failed long tests in the log,
         but at least one failed short test) → cap at B. Short tests
         exercise electronics + heads + read path; failures there
         imply electronics issues but NOT confirmed media damage.
      4. CLUSTER pattern: ≥N failures within the last
         `cluster_failures_window_hours` of POH → sticky F. Multiple
         failures clustering recently is an active-deterioration
         signal — drive is dying.

    Rules are added in order of increasing severity, so the order
    of forces_grade is C → B → C → F. The grade-rollup logic in the
    caller picks the tightest cap.
    """
    # We've been called inside `if any(not e.passed for e in entries)`,
    # so we know there's at least one failure. Sort entries chronological
    # (smartctl returns reverse-chrono — newest first; we want
    # iteration-by-recency below).
    entries = list(post.self_test_entries or [])
    poh_now = post.power_on_hours
    failed = [e for e in entries if not e.passed]
    if not failed:
        return  # defensive — caller should have filtered

    # --- Test type breakdown ---
    failed_long = [
        e for e in failed
        if e.test_type and "extended" in e.test_type.lower()
    ]
    failed_short = [
        e for e in failed
        if e.test_type and "short" in e.test_type.lower()
    ]

    # --- Recency: only meaningful if we have a current POH AND at
    # least one failure entry has a lifetime_hours value to compare.
    # Drives that don't expose lifetime_hours per-entry (rare, mostly
    # very old smartctl output) fall back to "treat as recent" —
    # safer to demote than to silently soften.
    most_recent_long_failure_hour = None
    if failed_long:
        with_hours = [e for e in failed_long if e.lifetime_hours is not None]
        if with_hours:
            # Reverse-chrono ordering preserved: first = most recent.
            most_recent_long_failure_hour = with_hours[0].lifetime_hours

    def _is_recent(failed_at_hour: int | None) -> bool:
        """True when the failure is in the last `recent_window_pct` of
        the drive's lifetime. Returns True when we can't compute
        recency (defensive — over-demote rather than under-demote)."""
        if poh_now is None or poh_now == 0:
            return True
        if failed_at_hour is None:
            return True
        recency = (poh_now - failed_at_hour) / poh_now
        return recency < config.self_test_recent_failure_window_pct

    # --- Rule 1: RECENT long-test failure → C cap ---
    if failed_long and _is_recent(most_recent_long_failure_hour):
        detail = "drive had a long-test failure within the last "
        detail += f"{int(config.self_test_recent_failure_window_pct * 100)}% of POH"
        if most_recent_long_failure_hour is not None:
            detail += f" (failed at POH={most_recent_long_failure_hour:,}"
            if poh_now is not None:
                detail += f", now at {poh_now:,}"
            detail += ")"
        detail += " — capped at C"
        rules.append(_ceiling(
            "no_recent_long_test_failure", detail, Grade.C,
        ))

    # --- Rule 2: ANCIENT long-test failure + clean tests since → B cap ---
    if failed_long and not _is_recent(most_recent_long_failure_hour):
        # Count clean tests AFTER the most-recent failure. Entries are
        # reverse-chrono; "after" the failure means "earlier in the
        # entries list" (closer to index 0).
        clean_since = 0
        for e in entries:
            if e.lifetime_hours is None:
                continue
            if (
                most_recent_long_failure_hour is not None
                and e.lifetime_hours > most_recent_long_failure_hour
                and e.passed
            ):
                clean_since += 1
        if clean_since >= config.self_test_ancient_min_clean_since:
            detail = (
                f"drive had an ancient long-test failure (POH="
                f"{most_recent_long_failure_hour:,}) but has {clean_since} "
                f"clean tests since — capped at B"
            )
            rules.append(_ceiling(
                "no_ancient_long_test_failure_or_recovered", detail, Grade.B,
            ))
        else:
            # Ancient failure but not enough clean tests since to
            # demonstrate recovery — still demote to C, just with the
            # honest "ancient but no recovery evidence" wording.
            detail = (
                f"drive had an ancient long-test failure (POH="
                f"{most_recent_long_failure_hour:,}) and only {clean_since} "
                f"clean tests since (need {config.self_test_ancient_min_clean_since}) "
                f"— capped at C"
            )
            rules.append(_ceiling(
                "no_ancient_long_test_failure_without_recovery",
                detail, Grade.C,
            ))

    # --- Rule 3: SHORT-test-only failures → B cap ---
    if failed_short and not failed_long:
        rules.append(_ceiling(
            "no_short_test_only_failure",
            f"drive had {len(failed_short)} short-test failure(s) but no "
            f"long-test failures (electronics signal, not media) "
            f"— capped at B",
            Grade.B,
        ))

    # --- Rule 4: CLUSTER pattern → sticky F ---
    if poh_now is not None:
        cluster_window_lower = poh_now - config.self_test_cluster_failures_window_hours
        cluster_failures = [
            e for e in failed
            if e.lifetime_hours is not None
            and e.lifetime_hours >= cluster_window_lower
        ]
        if len(cluster_failures) >= config.self_test_cluster_failures_threshold:
            # This is a HARD fail — passed=False so the grade rollup
            # treats it as a forces_grade=F ceiling. The cluster
            # pattern is the strongest signal in the family: it
            # means the drive is actively deteriorating, not just
            # carrying historical noise.
            rules.append(Rule(
                name="no_recent_cluster_failures",
                passed=False,
                detail=(
                    f"{len(cluster_failures)} self-test failures within the "
                    f"last {config.self_test_cluster_failures_window_hours} "
                    f"POH — active-deterioration pattern; failing the drive"
                ),
                forces_grade=Grade.FAIL,
            ))


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

        # Self-test log history — pre-v1.0.1 was a single blunt
        # ceiling-C rule that fired on ANY past failure, no matter
        # how ancient or which test type. JT's first 15-drive 6TB
        # enterprise pull batch capped 12/14 at C from this single
        # rule — exactly the over-firing v1.0.1's nuanced version
        # fixes.
        if (
            config.nuanced_self_test_grading
            and post.self_test_entries is not None
            and any(not e.passed for e in post.self_test_entries)
        ):
            # v1.0.1+ nuanced fork: split into (up to) 4 rules based on
            # test type, recency, and clustering. Multiple rules can
            # fire concurrently (e.g. ancient long-test failure + recent
            # short-test failure → both apply, the strictest wins for
            # the actual grade ceiling).
            _apply_nuanced_self_test_rules(rules, post, config)
        elif (
            config.cap_c_on_past_self_test_failure
            and post.self_test_has_past_failure
        ):
            # v1.0 fallback path — fires when the nuanced grading is
            # disabled OR when per-entry data is missing (pre-v1.0.1
            # SmartSnapshot rows that haven't been re-captured).
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
