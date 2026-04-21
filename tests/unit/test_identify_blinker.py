"""Tests for the v0.2.9 identify LED strobe (`blinker.blink_identify`)
and the orchestrator's `identify_drive` / `stop_identify` wiring.

The blinker primitives do real block-device reads, so the pure-Python
tests focus on the parts that don't need a device: the top-level
`blink_identify` respects cancellation, honors the max-duration
deadline, and best-effort-toggles `ledctl locate`.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from driveforge.core import blinker


# ---------------------------------------------------------------- blinker


async def test_blink_identify_exits_when_cancelled(monkeypatch) -> None:
    """Top-level blink_identify must exit promptly when its task is
    cancelled (operator clicked Stop, new batch kicked off, etc.)."""
    call_count = {"n": 0}

    async def fake_cycle(device_path, idx_ref):
        call_count["n"] += 1
        # Make each "cycle" a cancellable sleep so we can interrupt cleanly.
        await asyncio.sleep(0.05)
        return True

    monkeypatch.setattr(blinker, "_identify_cycle", fake_cycle)
    # Suppress ledctl path in the test environment.
    monkeypatch.setattr(blinker, "_try_ledctl", lambda action, dev: False)

    task = asyncio.create_task(blinker.blink_identify("/dev/fake", max_duration_sec=30))
    await asyncio.sleep(0.08)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert call_count["n"] >= 1, "cycle must have run at least once before cancel"


async def test_blink_identify_honors_max_duration(monkeypatch) -> None:
    """The safety deadline stops the strobe even if the task is never
    cancelled — prevents a forgotten ident from churning I/O forever."""
    async def fake_cycle(device_path, idx_ref):
        await asyncio.sleep(0.02)
        return True

    monkeypatch.setattr(blinker, "_identify_cycle", fake_cycle)
    monkeypatch.setattr(blinker, "_try_ledctl", lambda action, dev: False)

    # 0.1-second deadline — blink should exit cleanly within a short margin.
    start = asyncio.get_event_loop().time()
    await blinker.blink_identify("/dev/fake", max_duration_sec=0.1)
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed < 0.5, f"blink_identify overran its deadline: {elapsed:.2f}s"


async def test_blink_identify_exits_on_drive_pull(monkeypatch) -> None:
    """`_identify_cycle` returning False signals the drive was pulled
    (OSError from the read). blink_identify must stop the outer loop
    immediately rather than churning on a missing device."""
    calls = {"n": 0}

    async def fake_cycle(device_path, idx_ref):
        calls["n"] += 1
        return False  # signal pull on first iteration

    monkeypatch.setattr(blinker, "_identify_cycle", fake_cycle)
    monkeypatch.setattr(blinker, "_try_ledctl", lambda action, dev: False)

    await blinker.blink_identify("/dev/fake", max_duration_sec=30)
    assert calls["n"] == 1, "must stop after the first cycle returns False"


async def test_blink_identify_clears_ledctl_on_exit(monkeypatch) -> None:
    """On chassis where ledctl is available, blink_identify must call
    `locate=` on start AND `locate_off=` on exit so the blue LED
    doesn't stay lit after the strobe stops."""
    actions: list[str] = []

    def fake_ledctl(action, device_path):
        actions.append(action)
        return True  # pretend the chassis supports it

    async def fake_cycle(device_path, idx_ref):
        return False  # exit quickly

    monkeypatch.setattr(blinker, "_try_ledctl", fake_ledctl)
    monkeypatch.setattr(blinker, "_identify_cycle", fake_cycle)

    await blinker.blink_identify("/dev/fake", max_duration_sec=30)
    assert "locate" in actions, "must light the blue locate LED at start"
    assert "locate_off" in actions, "must clear the blue locate LED at exit"


async def test_blink_identify_ledctl_off_survives_cancel(monkeypatch) -> None:
    """When the task is cancelled, the finally block must still clear
    the SES locate LED. Otherwise the blue light stays lit until the
    next reboot."""
    actions: list[str] = []

    def fake_ledctl(action, device_path):
        actions.append(action)
        return True

    async def fake_cycle(device_path, idx_ref):
        await asyncio.sleep(0.5)  # long enough to cancel mid-cycle
        return True

    monkeypatch.setattr(blinker, "_try_ledctl", fake_ledctl)
    monkeypatch.setattr(blinker, "_identify_cycle", fake_cycle)

    task = asyncio.create_task(blinker.blink_identify("/dev/fake", max_duration_sec=30))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert "locate" in actions
    assert "locate_off" in actions, "cancel must still trigger ledctl cleanup"
