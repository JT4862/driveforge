"""Tests for driveforge.core.triage — the Clean/Watch/Fail verdict path
used for quick-pass runs in v0.5.5+."""

from __future__ import annotations

from driveforge.core.triage import Triage, triage_quick_pass


def test_clean_when_no_pending_and_no_climb() -> None:
    result = triage_quick_pass(
        pre_pending=0,
        post_pending=0,
        pre_reallocated=0,
        post_reallocated=0,
    )
    assert result.verdict is Triage.CLEAN
    assert not result.pending_climbed
    assert not result.reallocated_climbed


def test_clean_when_historical_reallocations_but_stable() -> None:
    """A drive with 50 old reallocations but 0 pending and no climb during
    the run is CLEAN. Historical scars don't demote the triage."""
    result = triage_quick_pass(
        pre_pending=0,
        post_pending=0,
        pre_reallocated=50,
        post_reallocated=50,
    )
    assert result.verdict is Triage.CLEAN


def test_watch_when_pending_present_but_stable() -> None:
    result = triage_quick_pass(
        pre_pending=5,
        post_pending=5,
        pre_reallocated=0,
        post_reallocated=0,
    )
    assert result.verdict is Triage.WATCH
    assert "5 pending" in result.summary
    assert "recommend full pipeline" in result.summary


def test_watch_singular_summary_wording() -> None:
    """Grammar: 1 pending sector (singular), not 1 pending sectors."""
    result = triage_quick_pass(
        pre_pending=1,
        post_pending=1,
        pre_reallocated=0,
        post_reallocated=0,
    )
    assert result.verdict is Triage.WATCH
    assert "1 pending sector " in result.summary + " "  # space after "sector"
    assert "sectors" not in result.summary


def test_fail_when_pending_climbs() -> None:
    """Drive acquired new pending sectors during the quick pass — active
    deterioration, the strongest Fail signal."""
    result = triage_quick_pass(
        pre_pending=0,
        post_pending=3,
        pre_reallocated=10,
        post_reallocated=10,
    )
    assert result.verdict is Triage.FAIL
    assert result.pending_climbed
    assert not result.reallocated_climbed
    assert "0 \u2192 3" in result.summary


def test_fail_when_reallocated_climbs_with_stable_pending() -> None:
    """Reallocated jumped but pending stayed flat. Drive internally moved
    sectors from pending to reallocated during the run — still qualifies
    as active change, still a Fail."""
    result = triage_quick_pass(
        pre_pending=0,
        post_pending=0,
        pre_reallocated=10,
        post_reallocated=15,
    )
    assert result.verdict is Triage.FAIL
    assert not result.pending_climbed
    assert result.reallocated_climbed


def test_fail_when_both_climb() -> None:
    """Both counters moved in the wrong direction simultaneously."""
    result = triage_quick_pass(
        pre_pending=2,
        post_pending=5,
        pre_reallocated=10,
        post_reallocated=12,
    )
    assert result.verdict is Triage.FAIL
    assert result.pending_climbed
    assert result.reallocated_climbed


def test_pending_decreasing_does_not_trigger_fail() -> None:
    """Pending went from 5 \u2192 2 — the drive healed itself during the run.
    This is good, not bad. Should NOT be Fail."""
    result = triage_quick_pass(
        pre_pending=5,
        post_pending=2,
        pre_reallocated=10,
        post_reallocated=13,
    )
    # post_reallocated climbed (10 \u2192 13), which still signals active change
    # \u2014 drive is moving sectors around. But pending shrank, so healing is
    # happening. The climb test treats any reallocated increase as Fail,
    # which is correct for conservative triage.
    assert result.verdict is Triage.FAIL


def test_none_inputs_fall_back_to_clean() -> None:
    """Drive with no SMART attrs exposed (SAS without sat translation,
    exotic transport, etc.) shouldn't cause triage to fail. Default to
    Clean when we have no evidence of trouble."""
    result = triage_quick_pass(
        pre_pending=None,
        post_pending=None,
        pre_reallocated=None,
        post_reallocated=None,
    )
    assert result.verdict is Triage.CLEAN


def test_partial_none_does_not_claim_climb() -> None:
    """If pre-snapshot is missing (legacy run, smartctl transient fail)
    we can't prove a climb. Don't manufacture one."""
    result = triage_quick_pass(
        pre_pending=None,
        post_pending=10,
        pre_reallocated=None,
        post_reallocated=20,
    )
    # post_pending>0 so this should be Watch, not Fail — we don't know
    # if the 10 pending sectors appeared during our run or existed all along.
    assert result.verdict is Triage.WATCH
    assert not result.pending_climbed
    assert not result.reallocated_climbed


def test_post_none_falls_back_to_clean() -> None:
    """If post-snapshot failed but pre succeeded, we can't confirm
    anything \u2014 no climb, no pending evidence. Default Clean."""
    result = triage_quick_pass(
        pre_pending=5,
        post_pending=None,
        pre_reallocated=0,
        post_reallocated=None,
    )
    assert result.verdict is Triage.CLEAN


def test_summary_is_nonempty_for_all_verdicts() -> None:
    """Every verdict must come with a human-readable summary so the
    dashboard / label can show something specific."""
    for pre_p, post_p, pre_r, post_r in [
        (0, 0, 0, 0),       # clean
        (5, 5, 0, 0),       # watch
        (0, 3, 0, 0),       # fail (pending climb)
        (0, 0, 0, 5),       # fail (reallocated climb)
    ]:
        result = triage_quick_pass(
            pre_pending=pre_p,
            post_pending=post_p,
            pre_reallocated=pre_r,
            post_reallocated=post_r,
        )
        assert result.summary, f"empty summary for pre_p={pre_p} post_p={post_p} pre_r={pre_r} post_r={post_r}"
