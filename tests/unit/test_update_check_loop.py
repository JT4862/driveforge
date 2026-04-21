"""Tests for v0.6.0's background GitHub-Releases poll loop.

The loop runs for the daemon's entire lifetime. Each iteration calls
`updates.check_for_updates(force=True)` which updates the module-level
cache; the navbar pill renderer (via the `cached_update` Jinja global)
reads that cache on every template render. So the loop is the
mechanism that keeps the navbar live without the operator having to
click "Check for updates" manually.

Critical invariants tested here:
  - The loop actually calls `check_for_updates(force=True)` per tick.
  - Exceptions from `check_for_updates` must NOT crash the loop —
    a network outage must not silently disable update checks until
    the next daemon restart.
  - The initial delay runs before the first check fires.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from driveforge.daemon import app as app_mod


pytestmark = pytest.mark.asyncio


class _FakeState:
    """The loop only touches the state object through tasks it spawns
    on it (none right now) — so a bare sentinel is enough. Kept as a
    class (not None) so a future expansion that does touch state
    doesn't silently no-op on None."""


async def test_loop_calls_check_for_updates_each_tick() -> None:
    """Each loop iteration after the initial delay must call
    `check_for_updates(force=True)`. Without this, the cache never
    refreshes and the navbar pill gets stuck on whatever the first
    successful check reported."""
    calls: list[bool] = []

    def fake_check(force: bool = False) -> None:
        calls.append(force)

    _real_sleep = asyncio.sleep

    async def fast_sleep(duration: float) -> None:
        # Don't actually wait — the test drives progress itself via
        # await _real_sleep(0) ticks. Using the saved reference avoids
        # recursing into our own patch.
        await _real_sleep(0)

    with patch("driveforge.daemon.app.updates_mod.check_for_updates", side_effect=fake_check), \
         patch("driveforge.daemon.app.asyncio.sleep", side_effect=fast_sleep):
        task = asyncio.create_task(app_mod._update_check_loop(_FakeState()))
        # Let the initial delay + several ticks land.
        for _ in range(20):
            await _real_sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # At least one check must have happened — more importantly every
    # call must have been force=True so the loop always refreshes
    # rather than returning a stale cache.
    assert len(calls) >= 1
    assert all(c is True for c in calls), "every tick must pass force=True"


async def test_loop_survives_exceptions_from_check_for_updates() -> None:
    """A dead GitHub connection, a 403 rate-limit, a malformed JSON
    response — none of these should terminate the loop. The daemon
    lives for weeks between restarts; a single bad iteration must
    not disable update checks for that entire window."""
    call_count = {"n": 0}

    def flaky_check(force: bool = False) -> None:
        call_count["n"] += 1
        # First two calls explode with different error types; third+
        # succeeds silently. The loop must still be running to observe
        # the third success.
        if call_count["n"] == 1:
            raise OSError("connection refused")
        if call_count["n"] == 2:
            raise RuntimeError("malformed release payload")
        # Third+ succeeds.

    _real_sleep = asyncio.sleep

    async def fast_sleep(duration: float) -> None:
        await _real_sleep(0)

    with patch("driveforge.daemon.app.updates_mod.check_for_updates", side_effect=flaky_check), \
         patch("driveforge.daemon.app.asyncio.sleep", side_effect=fast_sleep):
        task = asyncio.create_task(app_mod._update_check_loop(_FakeState()))
        for _ in range(30):
            await _real_sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # The loop must have made it past both failures to at least a third
    # call — proving exceptions are swallowed per-iteration, not
    # bubbling up out of the while-True.
    assert call_count["n"] >= 3


async def test_loop_cancels_cleanly() -> None:
    """The loop is cancelled in the daemon lifespan's shutdown path.
    It must respond to CancelledError without swallowing it — otherwise
    daemon shutdown hangs waiting for the task to exit."""
    _real_sleep = asyncio.sleep

    async def slow_sleep(duration: float) -> None:
        # A realistic sleep that actually yields. Cancellation has to
        # work through this too.
        await _real_sleep(0.01)

    with patch("driveforge.daemon.app.updates_mod.check_for_updates"), \
         patch("driveforge.daemon.app.asyncio.sleep", side_effect=slow_sleep):
        task = asyncio.create_task(app_mod._update_check_loop(_FakeState()))
        await _real_sleep(0.05)
        task.cancel()
        # Must complete within a reasonable window — if this hangs,
        # cancellation is broken (e.g. the loop catches CancelledError
        # somewhere it shouldn't). wait_for re-raises the cancelled
        # task's CancelledError; swallowing it here proves the cancel
        # landed AND the task actually stopped.
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except asyncio.CancelledError:
            pass


async def test_initial_delay_runs_before_first_check() -> None:
    """First check fires AFTER `UPDATE_CHECK_INITIAL_DELAY_SEC`, not
    at daemon boot. This is deliberate — piling the outbound HTTP
    onto the same 1-2 s window as startup hotplug scans + DB
    migrations is noisy, and nothing correctness-critical depends
    on an immediate first check. Test: observe the first asyncio.sleep
    call is the initial-delay constant, not the main interval."""
    sleep_durations: list[float] = []
    _real_sleep = asyncio.sleep

    async def record_sleep(duration: float) -> None:
        sleep_durations.append(duration)
        await _real_sleep(0)

    with patch("driveforge.daemon.app.updates_mod.check_for_updates"), \
         patch("driveforge.daemon.app.asyncio.sleep", side_effect=record_sleep):
        task = asyncio.create_task(app_mod._update_check_loop(_FakeState()))
        for _ in range(10):
            await _real_sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert len(sleep_durations) >= 2
    # First sleep is the initial delay; subsequent sleeps are the
    # main polling interval. Protects the "don't flood at startup"
    # behavior against a refactor that accidentally inlines the delay
    # with the main interval.
    assert sleep_durations[0] == app_mod.UPDATE_CHECK_INITIAL_DELAY_SEC
    assert sleep_durations[1] == app_mod.UPDATE_CHECK_INTERVAL_SEC
