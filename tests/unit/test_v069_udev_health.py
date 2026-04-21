"""Tests for v0.6.9's udev-pipeline health detector + restart trigger.

The detector's job is to recognize the failure mode observed during
v0.6.x real-hardware validation: a D-state drive subprocess cascades
upward through the HBA SG queue and wedges the udev worker pool, so
newly-inserted drives get a block device in sysfs but never fire a
pyudev add event. The detector samples three signals (udevadm settle,
/proc for D-state udevd processes, /sys/block for orphan sd* entries
without model/vendor populated) and classifies into
OK / DEGRADED / STALLED.

These tests cover:
  1. All three classification paths with synthetic fs + subprocess.
  2. The trigger path does NOT shell out with sudo (polkit-mediated).
  3. The trigger's targeted unit + verb matches the polkit rule.
  4. Handling for "polkit rule missing" stderr.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from driveforge.core import udev_health

# ----------------------------------------------------------------- helpers


def _make_sysfs_block(
    tmp_path: Path,
    devices: dict[str, tuple[str, str]],
) -> Path:
    """Build a fake /sys/block tree for tests.

    `devices` maps basename ("sda") → (model, vendor). Empty strings
    simulate the orphan case (block device exists but udev never
    populated the device/ attributes).
    """
    block = tmp_path / "sys_block"
    block.mkdir()
    for name, (model, vendor) in devices.items():
        dev = block / name / "device"
        dev.mkdir(parents=True)
        (dev / "model").write_text(model)
        (dev / "vendor").write_text(vendor)
    return block


def _make_proc(
    tmp_path: Path,
    udevd_states: list[str],  # list of state chars, one per systemd-udevd pid
) -> Path:
    """Build a fake /proc that _count_dstate_udevd_processes can scan."""
    proc = tmp_path / "proc"
    proc.mkdir()
    for idx, state in enumerate(udevd_states, start=100):
        pid_dir = proc / str(idx)
        pid_dir.mkdir()
        (pid_dir / "comm").write_text("systemd-udevd\n")
        # /proc/<pid>/stat format: "<pid> (comm) <state> <ppid> ..."
        (pid_dir / "stat").write_text(f"{idx} (systemd-udevd) {state} 1 1\n")
    # Add a non-udevd process so we confirm the filter works.
    other = proc / "200"
    other.mkdir()
    (other / "comm").write_text("dash\n")
    (other / "stat").write_text("200 (dash) S 1 1\n")
    return proc


# --------------------------------------------------------- classification


def test_ok_state_when_all_signals_green(tmp_path: Path) -> None:
    sysfs_block = _make_sysfs_block(
        tmp_path,
        {"sda": ("ST3000DM001", "ATA"), "sdb": ("CT240BX200SSD1", "ATA")},
    )
    proc = _make_proc(tmp_path, udevd_states=["S", "S", "S"])

    with patch.object(udev_health, "_run_udevadm_settle", return_value=(True, 0.02)):
        health = udev_health.check(proc_root=proc, sysfs_block=sysfs_block)

    assert health.state is udev_health.UdevHealthState.OK
    assert health.settle_ok is True
    assert health.udevd_dstate_count == 0
    assert health.orphan_block_devices == []
    assert health.note == ""
    assert not health.needs_operator_action


def test_stalled_when_settle_fails_and_orphan_present(tmp_path: Path) -> None:
    """The combined signal the detector is meant to catch: udev queue
    isn't draining AND a freshly-inserted drive shows up in sysfs
    without populated model/vendor. This is exactly the R720 scenario
    from the 2026-04-21 post-session diagnosis."""
    sysfs_block = _make_sysfs_block(
        tmp_path,
        {
            "sda": ("ST3000DM001", "ATA"),  # healthy, discovered
            "sdc": ("", ""),  # orphan — kernel knows about it, udev doesn't
        },
    )
    proc = _make_proc(tmp_path, udevd_states=["D", "S", "S"])

    with patch.object(udev_health, "_run_udevadm_settle", return_value=(False, 2.01)):
        health = udev_health.check(proc_root=proc, sysfs_block=sysfs_block)

    assert health.state is udev_health.UdevHealthState.STALLED
    assert health.needs_operator_action
    assert "sdc" in health.orphan_block_devices
    assert "sda" not in health.orphan_block_devices
    assert health.udevd_dstate_count == 1
    # note is operator-facing; confirm it's non-empty and mentions the
    # specific sd name so it's actionable.
    assert "sdc" in health.note
    assert "restart" in health.note.lower()


def test_degraded_when_settle_fails_but_no_orphan(tmp_path: Path) -> None:
    """Settle timing out on its own is not alarming — it just means
    the queue is busy. No orphan sd* entries means drives are still
    being discovered successfully. DEGRADED is logged (not surfaced)."""
    sysfs_block = _make_sysfs_block(tmp_path, {"sda": ("model", "vendor")})
    proc = _make_proc(tmp_path, udevd_states=["S"])

    with patch.object(udev_health, "_run_udevadm_settle", return_value=(False, 2.05)):
        health = udev_health.check(proc_root=proc, sysfs_block=sysfs_block)

    assert health.state is udev_health.UdevHealthState.DEGRADED
    assert not health.needs_operator_action
    assert "settle" in health.note.lower()


def test_degraded_when_dstate_workers_but_settle_ok(tmp_path: Path) -> None:
    """Transient D-state workers without settle failure = DEGRADED.
    Informational; could be a normal read that happens to be on the
    wrong side of a slow drive."""
    sysfs_block = _make_sysfs_block(tmp_path, {"sda": ("model", "vendor")})
    proc = _make_proc(tmp_path, udevd_states=["D", "S", "S"])

    with patch.object(udev_health, "_run_udevadm_settle", return_value=(True, 0.01)):
        health = udev_health.check(proc_root=proc, sysfs_block=sysfs_block)

    assert health.state is udev_health.UdevHealthState.DEGRADED
    assert health.udevd_dstate_count == 1


def test_multiple_orphans_render_summary_in_note(tmp_path: Path) -> None:
    sysfs_block = _make_sysfs_block(
        tmp_path,
        {
            "sdb": ("", ""),
            "sdc": ("", ""),
            "sdd": ("", ""),
            "sde": ("", ""),
        },
    )
    proc = _make_proc(tmp_path, udevd_states=["D"])

    with patch.object(udev_health, "_run_udevadm_settle", return_value=(False, 2.01)):
        health = udev_health.check(proc_root=proc, sysfs_block=sysfs_block)

    assert health.state is udev_health.UdevHealthState.STALLED
    assert len(health.orphan_block_devices) == 4
    # Summary line should say "4 drives" and include a preview sample.
    assert "4 drives" in health.note
    # First three are sampled inline; the 4th gets a "+1 more" suffix.
    assert "sdb" in health.note
    assert "+1 more" in health.note


# ---------------------------------------------------------------- trigger


def test_trigger_udev_restart_uses_polkit_not_sudo() -> None:
    """The trigger must NOT shell out with sudo. v0.6.9 uses polkit
    (same pattern as v0.6.0's update trigger). The argv should start
    with systemctl directly — if sudo sneaks back in here, the polkit
    rule won't match (subject.user would be root, not driveforge) and
    the restart would fail on production boxes."""
    captured: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        captured.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    with patch.object(subprocess, "run", side_effect=fake_run):
        ok, detail = udev_health.trigger_udev_restart()

    assert ok is True
    assert captured, "expected systemctl to be invoked"
    argv = captured[0]
    assert argv[0] == "systemctl"
    assert "sudo" not in argv
    assert "start" in argv
    assert "--no-block" in argv, "--no-block is load-bearing (update-trigger lesson)"
    # Exact unit name must match the polkit rule's action.lookup("unit").
    assert argv[-1] == "driveforge-udev-restart.service"


def test_trigger_udev_restart_surfaces_polkit_error() -> None:
    """When the polkit rule is missing/mis-installed systemctl exits
    non-zero with "Interactive authentication required" on stderr.
    The trigger must surface that explicitly so the operator sees a
    remediation hint instead of a generic failure."""

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            argv, 1, "", "Failed to start driveforge-udev-restart.service: "
            "Interactive authentication required.\n"
        )

    with patch.object(subprocess, "run", side_effect=fake_run):
        ok, detail = udev_health.trigger_udev_restart()

    assert ok is False
    assert "polkit" in detail.lower()


def test_trigger_udev_restart_handles_systemctl_missing() -> None:
    """On a dev box without systemctl on PATH (Mac/minimal container),
    return a clear (ok=False, detail) instead of crashing."""

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        raise FileNotFoundError("systemctl")

    with patch.object(subprocess, "run", side_effect=fake_run):
        ok, detail = udev_health.trigger_udev_restart()

    assert ok is False
    assert "systemctl" in detail.lower()


def test_settle_handles_missing_udevadm() -> None:
    """On non-Debian dev hosts, udevadm may not exist. _run_udevadm_settle
    should return (True, 0.0) rather than raising — the check should
    gracefully degrade to "no signal, assume OK" instead of flapping
    STALLED on Mac development setups."""

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        raise FileNotFoundError("udevadm")

    with patch.object(subprocess, "run", side_effect=fake_run):
        ok, elapsed = udev_health._run_udevadm_settle()

    assert ok is True
    assert elapsed == 0.0


# ------------------------------------------------------------ polkit rule


POLKIT_RULE = (
    Path(__file__).resolve().parents[2]
    / "config"
    / "driveforge-udev-restart.polkit-rules"
)


def test_restart_udev_route_fires_trigger_and_redirects_ok(tmp_path, monkeypatch) -> None:
    """POST /settings/restart-udev must call trigger_udev_restart(),
    and on success redirect the caller back to the Referer with a
    `udev_restart=ok` query param so the banner can show a confirmation.
    This is distinct from /settings/install-update — no refusal
    preconditions for active drives, because restarting udev is safe
    while pipelines are in flight."""
    from fastapi.testclient import TestClient

    from driveforge import config as cfg
    from driveforge.daemon.app import make_app

    settings = cfg.Settings()
    settings.daemon.state_dir = tmp_path
    settings.daemon.db_path = tmp_path / "driveforge.db"
    settings.daemon.pending_labels_dir = tmp_path / "pending-labels"
    settings.daemon.reports_dir = tmp_path / "reports"
    settings.setup_completed = True

    app = make_app(settings)

    trigger_called: list[int] = []

    def fake_trigger() -> tuple[bool, str]:
        trigger_called.append(1)
        return (True, "udev restart requested")

    monkeypatch.setattr(
        "driveforge.core.udev_health.trigger_udev_restart",
        fake_trigger,
    )

    with TestClient(app, follow_redirects=False) as client:
        resp = client.post(
            "/settings/restart-udev",
            headers={"Referer": "/"},
        )

    assert resp.status_code == 303
    assert trigger_called == [1], "trigger must be called on POST"
    assert "udev_restart=ok" in resp.headers["location"]


def test_restart_udev_route_surfaces_trigger_error(tmp_path, monkeypatch) -> None:
    """On trigger failure (e.g. polkit rule missing) the route redirects
    back with a `udev_restart_error=<message>` query param so the
    banner can render the failure reason."""
    from fastapi.testclient import TestClient

    from driveforge import config as cfg
    from driveforge.daemon.app import make_app

    settings = cfg.Settings()
    settings.daemon.state_dir = tmp_path
    settings.daemon.db_path = tmp_path / "driveforge.db"
    settings.daemon.pending_labels_dir = tmp_path / "pending-labels"
    settings.daemon.reports_dir = tmp_path / "reports"
    settings.setup_completed = True

    app = make_app(settings)

    monkeypatch.setattr(
        "driveforge.core.udev_health.trigger_udev_restart",
        lambda: (False, "polkit rule missing"),
    )

    with TestClient(app, follow_redirects=False) as client:
        resp = client.post(
            "/settings/restart-udev",
            headers={"Referer": "/settings"},
        )

    assert resp.status_code == 303
    assert "udev_restart_error=" in resp.headers["location"]
    assert "polkit" in resp.headers["location"].lower()


def test_polkit_rule_targets_exact_unit() -> None:
    """Defense-in-depth: the polkit rule's `unit` check must match
    the unit name the trigger actually starts. If these drift apart
    (rule says foo.service, trigger fires bar.service) the in-app
    restart silently fails in production."""
    assert POLKIT_RULE.is_file(), f"polkit rule missing at {POLKIT_RULE}"
    rule_text = POLKIT_RULE.read_text()
    assert '"driveforge-udev-restart.service"' in rule_text
    assert '"start"' in rule_text
    assert 'subject.user === "driveforge"' in rule_text
    # Belt-and-suspenders: action.id must be the systemd manage-units
    # one; `manage-unit-files` is a DIFFERENT action that wouldn't
    # cover StartUnit.
    assert '"org.freedesktop.systemd1.manage-units"' in rule_text
