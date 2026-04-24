"""v0.11.2 — daemon self-restart on role change.

Covers the bug JT hit during v0.11.0 walkthrough: fresh ISO install
ran wizard, picked Operator, saved config. Daemon had booted in
standalone lifespan — operator discovery loop never spawned.
avahi-browse saw the candidate on the LAN but the operator's
Discovered panel was empty because no Python code was populating
state.discovered_candidates.

Fix: any code path that changes fleet.role fires
`self_restart.schedule_self_restart()`, which triggers a polkit-
authorized `systemctl restart driveforge-daemon` so the new
role's lifespan tasks spawn.

Tests:
  - schedule_self_restart spawns a daemon thread + calls systemctl
  - setup wizard final step restarts when role changed from boot
  - setup wizard step 1 with candidate restarts
  - setup wizard doesn't restart if role didn't change
  - settings role-toggle restarts on flip
  - /api/fleet/adopt restarts after accepting adoption
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from driveforge import config as cfg


def _bootstrap_app(tmp_path, *, role: str = "standalone", setup_completed: bool = True):
    from driveforge.daemon.app import make_app
    from driveforge.daemon.state import DaemonState
    settings = cfg.Settings()
    settings.daemon.state_dir = tmp_path
    settings.daemon.db_path = tmp_path / "driveforge.db"
    settings.daemon.pending_labels_dir = tmp_path / "pending-labels"
    settings.daemon.reports_dir = tmp_path / "reports"
    settings.setup_completed = setup_completed
    settings.fleet.role = role
    if role == "candidate":
        settings.fleet.install_id = "abcdef123456"
        settings.fleet.api_token_path = tmp_path / "agent.token"
    app = make_app(settings)
    DaemonState.boot(settings)
    return app


# ---------------------------------------------------- helper


def test_schedule_self_restart_fires_systemctl(monkeypatch) -> None:
    """The scheduler spawns a daemon thread that runs
    `systemctl restart driveforge-daemon`. We capture the subprocess
    call + confirm the command shape."""
    from driveforge.core import self_restart

    calls: list[list[str]] = []

    def fake_run(cmd, **_kw):
        calls.append(cmd)

        class _Ret:
            returncode = 0
        return _Ret()

    monkeypatch.setattr("subprocess.run", fake_run)
    # Use a 0-second delay so the test doesn't actually wait
    self_restart.schedule_self_restart(delay_s=0.01, reason="test")
    # Give the daemon thread time to fire
    time.sleep(0.2)
    assert calls, "subprocess.run was never invoked"
    assert calls[0] == ["systemctl", "restart", "driveforge-daemon"]


def test_schedule_self_restart_swallows_subprocess_error(monkeypatch) -> None:
    """Restart failure must not crash the calling request. Logged
    as a warning; caller returns normally."""
    from driveforge.core import self_restart

    def fake_run(*_a, **_kw):
        raise OSError("systemctl not found")

    monkeypatch.setattr("subprocess.run", fake_run)
    self_restart.schedule_self_restart(delay_s=0.01, reason="test")
    time.sleep(0.2)
    # If we got here without an exception propagating out of the
    # scheduler, the thread swallowed it correctly.


# ---------------------------------------------------- Setup wizard


def _patch_restart(monkeypatch):
    """Capture schedule_self_restart calls without actually spawning
    a thread or shelling out."""
    calls: list[dict] = []

    def fake_schedule(delay_s=1.5, reason="role change"):
        calls.append({"delay_s": delay_s, "reason": reason})

    monkeypatch.setattr(
        "driveforge.core.self_restart.schedule_self_restart", fake_schedule,
    )
    return calls


def test_wizard_final_step_restarts_when_role_changed(tmp_path, monkeypatch) -> None:
    """Standalone boot → wizard picks Operator → final step save
    fires self-restart so the operator-discover-loop can spawn in
    the NEW lifespan. Regression guard for the v0.11.0 JT bug."""
    app = _bootstrap_app(tmp_path, role="standalone", setup_completed=False)
    calls = _patch_restart(monkeypatch)
    monkeypatch.setattr(cfg, "save", lambda *a, **kw: None)

    with TestClient(app) as client:
        # Step 1: pick Operator
        client.post(
            "/setup/1", data={"role": "operator", "hostname": "testbox"},
            follow_redirects=False,
        )
        # Walk through steps 2-5 to hit the final save
        for step in (2, 3, 4, 5):
            client.post(
                f"/setup/{step}", data={"confirm": "skip"},
                follow_redirects=False,
            )

    # Restart should have been scheduled exactly once
    assert len(calls) == 1
    assert "operator" in calls[0]["reason"]


def test_wizard_step1_candidate_restarts(tmp_path, monkeypatch) -> None:
    """Picking Agent (headless) in step 1 skips the rest of the
    wizard + restarts the daemon so candidate_publish_loop spawns."""
    app = _bootstrap_app(tmp_path, role="standalone", setup_completed=False)
    calls = _patch_restart(monkeypatch)
    monkeypatch.setattr(cfg, "save", lambda *a, **kw: None)

    with TestClient(app) as client:
        resp = client.post(
            "/setup/1",
            data={"role": "candidate", "hostname": "testbox"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert len(calls) == 1
    assert "candidate" in calls[0]["reason"]


def test_wizard_standalone_no_restart(tmp_path, monkeypatch) -> None:
    """If the user picks Standalone (same as boot role), the wizard
    saves + completes without triggering a restart — nothing lifespan-
    level changed."""
    app = _bootstrap_app(tmp_path, role="standalone", setup_completed=False)
    calls = _patch_restart(monkeypatch)
    monkeypatch.setattr(cfg, "save", lambda *a, **kw: None)

    with TestClient(app) as client:
        client.post(
            "/setup/1", data={"role": "standalone", "hostname": "testbox"},
            follow_redirects=False,
        )
        for step in (2, 3, 4, 5):
            client.post(
                f"/setup/{step}", data={"confirm": "skip"},
                follow_redirects=False,
            )
    assert calls == []


# ---------------------------------------------------- Settings role toggle


def test_settings_role_toggle_fires_restart(tmp_path, monkeypatch) -> None:
    """Clicking Save on Settings → Fleet with a new role must
    auto-restart. Pre-v0.11.2 this returned ?restart=1 in the URL
    as a hint but didn't actually do the restart."""
    app = _bootstrap_app(tmp_path, role="standalone")
    calls = _patch_restart(monkeypatch)
    monkeypatch.setattr(cfg, "save", lambda *a, **kw: None)
    with TestClient(app) as client:
        resp = client.post(
            "/settings/fleet-role",
            data={"role": "operator"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert "restart=1" in resp.headers["location"]
    assert len(calls) == 1
    assert "operator" in calls[0]["reason"]


def test_settings_role_toggle_noop_no_restart(tmp_path, monkeypatch) -> None:
    """Same-role click: no config change → no restart."""
    app = _bootstrap_app(tmp_path, role="standalone")
    calls = _patch_restart(monkeypatch)
    monkeypatch.setattr(cfg, "save", lambda *a, **kw: None)
    with TestClient(app) as client:
        client.post(
            "/settings/fleet-role",
            data={"role": "standalone"},
            follow_redirects=False,
        )
    assert calls == []


# ---------------------------------------------------- Adoption endpoint


def test_adopt_endpoint_fires_restart(tmp_path, monkeypatch) -> None:
    """Candidate accepting adoption must restart so it boots into
    agent mode. Pre-v0.11.2 used a direct `systemctl restart`
    subprocess that would have EACCES'd under the daemon user
    without the polkit rule this release adds."""
    app = _bootstrap_app(tmp_path, role="candidate")
    calls = _patch_restart(monkeypatch)
    monkeypatch.setattr(cfg, "save", lambda *a, **kw: None)

    # Redirect the token write path to a tmp location
    from driveforge.daemon.state import get_state
    state = get_state()
    token_path = tmp_path / "agent.token"
    state.settings.fleet.api_token_path = token_path

    with TestClient(app) as client:
        resp = client.post(
            "/api/fleet/adopt",
            json={
                "operator_url": "http://op:8080",
                "agent_token": "id.raw",
                "display_name": "x",
                "install_id": "abcdef123456",
            },
        )
    assert resp.status_code == 200
    assert len(calls) == 1
    assert "adopt" in calls[0]["reason"].lower()
