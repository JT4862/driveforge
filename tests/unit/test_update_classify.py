"""Tests for the v0.5.0 update-state classifier.

`classify_update_state(log_text, service_state)` collapses two
ambiguous signals (systemctl `is-active` output + grep of the log
for explicit markers) into one of four unambiguous states: idle,
running, succeeded, failed. Every branch has a concrete test.

The whole point of this classifier is to catch the failure modes
that `systemctl is-active` alone doesn't distinguish — in particular
the "unit exited 0 but the update script crashed halfway through"
case that happens when the script is SIGKILL'd by OOM or power
glitch before reaching its success trap.
"""

from __future__ import annotations

from driveforge.core import updates as updates_mod
from driveforge.core.updates import UpdateState


# ---------------------------------------------------------------- idle


def test_classify_idle_on_empty_log_and_inactive_unit() -> None:
    """No log content + unit never ran → idle. Dashboard renders
    nothing about the update panel."""
    state, detail = updates_mod.classify_update_state("", "inactive")
    assert state == UpdateState.IDLE
    assert detail is None


def test_classify_idle_on_unparseable_log() -> None:
    """Log has content but no marker lines — treat as idle (some
    other process wrote to the log? Impossible in practice but
    handled gracefully)."""
    state, _ = updates_mod.classify_update_state(
        "random unrelated lines\n1234 5678\n", "inactive",
    )
    assert state == UpdateState.IDLE


# ---------------------------------------------------------------- running


def test_classify_running_on_start_marker_and_active_unit() -> None:
    """START marker present, unit running → running."""
    log = "=== DRIVEFORGE_UPDATE_START ===\n[2026-04-21T00:00:01Z] fetching ...\n"
    state, detail = updates_mod.classify_update_state(log, "active")
    assert state == UpdateState.RUNNING
    assert detail is None


def test_classify_running_on_activating_unit() -> None:
    """`activating` is a transient state systemd shows before `active`
    — still counts as running."""
    log = "=== DRIVEFORGE_UPDATE_START ===\n"
    state, _ = updates_mod.classify_update_state(log, "activating")
    assert state == UpdateState.RUNNING


def test_classify_running_on_deactivating_unit() -> None:
    """`deactivating` = unit is in the middle of stopping. The script
    is still running until the unit fully exits — treat as running
    so the poll stays alive."""
    log = "=== DRIVEFORGE_UPDATE_START ===\n"
    state, _ = updates_mod.classify_update_state(log, "deactivating")
    assert state == UpdateState.RUNNING


# ---------------------------------------------------------------- succeeded


def test_classify_succeeded_on_success_marker() -> None:
    """SUCCESS marker present → succeeded. Unit state doesn't
    matter (could be inactive after oneshot completion, or
    momentarily deactivating)."""
    log = (
        "=== DRIVEFORGE_UPDATE_START ===\n"
        "some log lines here\n"
        "=== DRIVEFORGE_UPDATE_SUCCESS ===\n"
    )
    state, detail = updates_mod.classify_update_state(log, "inactive")
    assert state == UpdateState.SUCCEEDED
    assert detail is None


def test_classify_succeeded_even_if_unit_is_active() -> None:
    """Edge case: the script emitted SUCCESS right before the
    install.sh-triggered daemon restart, and we're polling within
    that window. The marker wins; show success."""
    log = "=== DRIVEFORGE_UPDATE_START ===\n=== DRIVEFORGE_UPDATE_SUCCESS ===\n"
    state, _ = updates_mod.classify_update_state(log, "active")
    assert state == UpdateState.SUCCEEDED


def test_classify_takes_latest_marker_on_retry() -> None:
    """A retry scenario: run 1 failed, run 2 succeeded. The log
    has both markers. The LATEST wins — we report succeeded."""
    log = (
        "=== DRIVEFORGE_UPDATE_START ===\n"
        "=== DRIVEFORGE_UPDATE_FAILED: old failure ===\n"
        "=== DRIVEFORGE_UPDATE_START ===\n"
        "=== DRIVEFORGE_UPDATE_SUCCESS ===\n"
    )
    state, detail = updates_mod.classify_update_state(log, "inactive")
    assert state == UpdateState.SUCCEEDED
    assert detail is None


# ---------------------------------------------------------------- failed


def test_classify_failed_on_explicit_marker_with_reason() -> None:
    """FAILED marker includes a reason string — surface it as `detail`
    so the UI can render 'Update failed: <reason>' directly."""
    log = (
        "=== DRIVEFORGE_UPDATE_START ===\n"
        "=== DRIVEFORGE_UPDATE_FAILED: git fetch failed ===\n"
    )
    state, detail = updates_mod.classify_update_state(log, "failed")
    assert state == UpdateState.FAILED
    assert detail == "git fetch failed"


def test_classify_failed_marker_handles_trailing_equals() -> None:
    """Robustness: the marker uses `=== DRIVEFORGE_UPDATE_FAILED: X ===`
    format with trailing equals. Detail extraction must strip them."""
    log = "=== DRIVEFORGE_UPDATE_FAILED: install.sh failed ==="
    state, detail = updates_mod.classify_update_state(log, "failed")
    assert state == UpdateState.FAILED
    assert detail == "install.sh failed"


def test_classify_failed_on_unit_failed_without_marker() -> None:
    """Unit transitioned to `failed` state but never emitted a
    FAILED marker — the script likely died hard (SIGKILL, OOM)
    before reaching the trap. Surface a clear reason."""
    log = "=== DRIVEFORGE_UPDATE_START ===\n[some partial output]\n"
    state, detail = updates_mod.classify_update_state(log, "failed")
    assert state == UpdateState.FAILED
    assert detail is not None
    assert "before emitting" in detail.lower() or "marker" in detail.lower()


def test_classify_failed_on_inactive_without_success_marker() -> None:
    """Unit exited cleanly (inactive) but never emitted SUCCESS or
    FAILED — implies the script did `exit 0` without reaching the
    end path. Surface as failure so the operator retries."""
    log = "=== DRIVEFORGE_UPDATE_START ===\n[partial output]\n"
    state, detail = updates_mod.classify_update_state(log, "inactive")
    assert state == UpdateState.FAILED
    assert detail is not None


def test_classify_failed_marker_latest_wins_over_older_success() -> None:
    """Hypothetical but possible: run 1 succeeded (marker still in
    log), run 2 failed (marker added later). The LATEST marker is
    FAILED, that's the current state."""
    log = (
        "=== DRIVEFORGE_UPDATE_START ===\n"
        "=== DRIVEFORGE_UPDATE_SUCCESS ===\n"
        "=== DRIVEFORGE_UPDATE_START ===\n"
        "=== DRIVEFORGE_UPDATE_FAILED: new failure ===\n"
    )
    state, detail = updates_mod.classify_update_state(log, "failed")
    assert state == UpdateState.FAILED
    assert detail == "new failure"


# ---------------------------------------------------------------- edge cases


def test_classify_empty_service_state_is_handled() -> None:
    """`update_service_state()` can return 'unknown' if systemctl is
    missing. START marker + unknown service state → assume still
    running (conservative — avoid false 'failed' on broken probes)."""
    log = "=== DRIVEFORGE_UPDATE_START ===\n"
    state, _ = updates_mod.classify_update_state(log, "unknown")
    assert state == UpdateState.RUNNING


def test_classify_no_markers_with_active_unit_is_idle() -> None:
    """Theoretical: unit reports active but log has no markers.
    Probably a fresh unit start that hasn't written anything yet.
    We classify as IDLE until we see START. Dashboard will re-render
    on next poll and catch up."""
    state, _ = updates_mod.classify_update_state("", "active")
    assert state == UpdateState.IDLE
