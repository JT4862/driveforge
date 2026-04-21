"""Regression tests for the v0.2.9 `Orchestrator._looks_like_pull`
tightening.

Scenario matrix:

  | interrupted_serials | rediscover finds serial | /dev/X exists | expected |
  |---------------------|-------------------------|---------------|----------|
  | yes                 | (irrelevant)            | (irrelevant)  | True     |
  | no                  | yes                     | (irrelevant)  | False    |
  | no                  | no (discovery ok)       | (irrelevant)  | True     |
  | no                  | discovery errored       | no            | True     |
  | no                  | discovery errored       | yes           | False    |

The interesting row is (no, yes, irrelevant) → False. That's the fix:
before v0.2.9 this case would return True if /dev/X was briefly missing
due to kernel re-enumeration after a CONFIG_IDE_TASK_IOCTL error, which
stuck the TestRun in interrupted-state forever.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from driveforge.core.drive import Drive, Transport
from driveforge.daemon.orchestrator import Orchestrator


def _drive(serial: str = "SN-PULL-1", device_path: str = "/dev/sdz") -> Drive:
    return Drive(
        serial=serial,
        model="PULLDRIVE-1",
        capacity_bytes=1_000_000_000,
        transport=Transport.SATA,
        device_path=device_path,
        rotation_rate=0,
    )


def _orch(interrupted: set[str] | None = None) -> Orchestrator:
    """Minimal Orchestrator with a state stub that has the fields
    `_looks_like_pull` consults."""
    state = SimpleNamespace(
        interrupted_serials=interrupted or set(),
        active_serials=lambda: set(),
    )
    return Orchestrator(state)  # type: ignore[arg-type]


def test_interrupted_serials_flag_wins(monkeypatch) -> None:
    """The hotplug-remove handler setting state.interrupted_serials is
    authoritative — no need to even touch discovery."""
    drive = _drive()
    orch = _orch(interrupted={drive.serial})
    # Discovery should NOT be called in this path — if it is, fail loudly.
    def _boom():
        raise AssertionError("discover() must not be called when interrupted_serials matches")
    monkeypatch.setattr("driveforge.daemon.orchestrator.drive_mod.discover", _boom)
    assert orch._looks_like_pull(drive) is True


def test_rediscovery_finds_serial_under_different_device_path(monkeypatch) -> None:
    """The KEY fix. Kernel re-enumeration moves the drive from /dev/sdk
    to /dev/sdl on CONFIG_IDE_TASK_IOCTL; discovery still finds the
    serial. Must return False (NOT a pull) so the run closes as Fail
    cleanly instead of sticking in interrupted state."""
    drive = _drive(device_path="/dev/sdk")  # original path
    orch = _orch()
    # After re-enumeration, discovery finds the serial under /dev/sdl.
    reenumerated = Drive(
        serial=drive.serial,
        model=drive.model,
        capacity_bytes=drive.capacity_bytes,
        transport=drive.transport,
        device_path="/dev/sdl",  # NEW path
    )
    monkeypatch.setattr(
        "driveforge.daemon.orchestrator.drive_mod.discover",
        lambda: [reenumerated],
    )
    assert orch._looks_like_pull(drive) is False


def test_rediscovery_succeeds_but_serial_missing_means_pulled(monkeypatch) -> None:
    """Discovery worked AND the drive's serial isn't in the result.
    That's a confident "drive is gone" — return True."""
    drive = _drive()
    orch = _orch()
    # Discovery returns other drives but not this one.
    other = Drive(
        serial="SOME-OTHER",
        model="OTHER",
        capacity_bytes=1_000_000_000,
        transport=Transport.SAS,
        device_path="/dev/sda",
    )
    monkeypatch.setattr(
        "driveforge.daemon.orchestrator.drive_mod.discover",
        lambda: [other],
    )
    assert orch._looks_like_pull(drive) is True


def test_discovery_error_falls_back_to_device_path_gone(monkeypatch, tmp_path) -> None:
    """If discovery raises (lsblk unavailable, etc.) AND the device
    path doesn't exist, fall back to the old behavior: assume pulled."""
    drive = _drive(device_path=str(tmp_path / "definitely-not-there"))
    orch = _orch()

    def _raises():
        raise RuntimeError("lsblk is broken")

    monkeypatch.setattr("driveforge.daemon.orchestrator.drive_mod.discover", _raises)
    assert orch._looks_like_pull(drive) is True


def test_discovery_error_but_device_path_exists(monkeypatch, tmp_path) -> None:
    """Discovery failed but the device file is still there — treat
    as NOT pulled. This is a SAFER-THAN-PREVIOUSLY outcome: before
    v0.2.9 a discovery failure would fall straight to the path check
    (same as now), but we now have a clearer decision tree."""
    fake_dev = tmp_path / "sdz"
    fake_dev.write_text("")  # make it exist
    drive = _drive(device_path=str(fake_dev))
    orch = _orch()

    def _raises():
        raise RuntimeError("lsblk is broken")

    monkeypatch.setattr("driveforge.daemon.orchestrator.drive_mod.discover", _raises)
    assert orch._looks_like_pull(drive) is False
