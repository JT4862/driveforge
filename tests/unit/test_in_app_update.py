"""Tests for the v0.3.1 one-click in-app update flow (v0.6.0 polkit-era).

The update *itself* (systemctl start driveforge-update.service →
git pull → install.sh → daemon restart) is fundamentally an integration
test that needs a real Debian host with systemd + polkit. These unit
tests cover what's testable without that:

  - The refusal preconditions in `POST /settings/install-update` —
    no in-flight pipeline, no recovery in progress.
  - `updates.update_log_tail()` returning "" gracefully when the log
    file doesn't exist (first-ever run on a host).
  - `updates.update_log_tail()` reading a tail bounded by `max_lines`
    so a 100k-line log doesn't blow up the dashboard request.
  - `updates.update_service_state()` returning "unknown" cleanly when
    systemctl is missing (dev macs).
  - `trigger_in_app_update()` surfacing systemctl's stderr verbatim
    on failure so the operator sees the real reason (typically a
    missing/broken polkit rule on upgraded hosts — see
    test_update_trigger_polkit.py for the v0.6.0 argv/shape tests).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from driveforge.core import updates as updates_mod


# ---------------------------------------------------------------- update_log_tail


def test_update_log_tail_missing_file_returns_empty(tmp_path, monkeypatch) -> None:
    """No log file = first-ever run on this host. Must return "" without
    raising — UI renders "no update log yet" panel cleanly."""
    monkeypatch.setattr(
        updates_mod, "UPDATE_LOG_PATH", str(tmp_path / "does-not-exist.log")
    )
    assert updates_mod.update_log_tail() == ""


def test_update_log_tail_returns_last_n_lines(tmp_path, monkeypatch) -> None:
    """A 1000-line log must be truncated to the last `max_lines` so the
    dashboard request stays bounded."""
    log = tmp_path / "update.log"
    log.write_text("\n".join(f"line {i}" for i in range(1000)) + "\n")
    monkeypatch.setattr(updates_mod, "UPDATE_LOG_PATH", str(log))
    tail = updates_mod.update_log_tail(max_lines=10)
    lines = tail.strip().split("\n")
    assert len(lines) == 10
    assert lines[0] == "line 990"
    assert lines[-1] == "line 999"


def test_update_log_tail_handles_unreadable(tmp_path, monkeypatch) -> None:
    """If the daemon can't read the log file (permission denied), the
    UI should render the "no log" state rather than crash."""
    log = tmp_path / "update.log"
    log.write_text("some content\n")
    monkeypatch.setattr(updates_mod, "UPDATE_LOG_PATH", str(log))
    # Patch open() inside the module to raise OSError
    real_open = Path.open
    def boom(self, *args, **kwargs):
        if str(self) == str(log):
            raise PermissionError("simulated EACCES")
        return real_open(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", boom)
    assert updates_mod.update_log_tail() == ""


# ---------------------------------------------------------------- service_state


def test_update_service_state_returns_state_string(monkeypatch) -> None:
    """systemctl is-active prints state to stdout regardless of exit
    code (active=0, others=non-zero). We must read stdout, not the
    return code."""
    fake_proc = MagicMock(stdout="inactive\n", returncode=3)
    def fake_run(argv, **kwargs):
        return fake_proc
    monkeypatch.setattr("subprocess.run", fake_run)
    assert updates_mod.update_service_state() == "inactive"


def test_update_service_state_returns_active(monkeypatch) -> None:
    fake_proc = MagicMock(stdout="active\n", returncode=0)
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: fake_proc)
    assert updates_mod.update_service_state() == "active"


def test_update_service_state_returns_unknown_on_subprocess_failure(monkeypatch) -> None:
    """systemctl missing or hung → return 'unknown', never raise."""
    import subprocess
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="systemctl", timeout=5)
    monkeypatch.setattr("subprocess.run", boom)
    assert updates_mod.update_service_state() == "unknown"


def test_update_service_state_returns_unknown_for_garbage_output(monkeypatch) -> None:
    """If is-active prints something we don't recognize (different
    systemd version, error garbage), return 'unknown' rather than
    surfacing the unknown string up to the template."""
    fake_proc = MagicMock(stdout="something-weird\n", returncode=1)
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: fake_proc)
    assert updates_mod.update_service_state() == "unknown"


# ---------------------------------------------------------------- trigger


def test_trigger_in_app_update_returns_failure_on_non_zero(monkeypatch) -> None:
    """When sudo systemctl returns non-zero, the operator sees the
    actual stderr — most commonly the sudoers rule isn't installed."""
    fake_proc = MagicMock(
        returncode=1,
        stdout="",
        stderr="sudo: a password is required\n",
    )
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: fake_proc)
    ok, message = updates_mod.trigger_in_app_update()
    assert ok is False
    assert "password is required" in message
    assert "rc=1" in message


def test_trigger_in_app_update_returns_success_on_zero(monkeypatch) -> None:
    """systemctl exit 0 → unit started, return success message."""
    fake_proc = MagicMock(returncode=0, stdout="", stderr="")
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: fake_proc)
    ok, message = updates_mod.trigger_in_app_update()
    assert ok is True
    assert "live log" in message.lower() or "started" in message.lower()


def test_trigger_in_app_update_handles_missing_systemctl(monkeypatch) -> None:
    """OSError from subprocess.run (systemctl binary missing) must
    surface as a refusal, not a crash."""
    def boom(*a, **kw):
        raise FileNotFoundError("systemctl: no such file or directory")
    monkeypatch.setattr("subprocess.run", boom)
    ok, message = updates_mod.trigger_in_app_update()
    assert ok is False
    assert "systemctl" in message.lower()


# ---------------------------------------------------------------- HTTP refusal


def test_install_update_refuses_when_drives_active(tmp_path, monkeypatch) -> None:
    """The HTTP route must check state.active_phase BEFORE invoking
    sudo. A running pipeline means the daemon restart at the end of
    install.sh would orphan the test run."""
    from fastapi.testclient import TestClient

    from driveforge import config as cfg
    from driveforge.daemon.app import make_app
    from driveforge.daemon.state import DaemonState

    settings = cfg.Settings()
    settings.daemon.state_dir = tmp_path
    settings.daemon.db_path = tmp_path / "driveforge.db"
    settings.daemon.pending_labels_dir = tmp_path / "pending-labels"
    settings.daemon.reports_dir = tmp_path / "reports"
    settings.setup_completed = True  # skip the wizard middleware redirect

    app = make_app(settings)
    state = DaemonState.boot(settings)
    # Spy on the trigger primitive — it MUST NOT be called when refused.
    trigger_called = []
    monkeypatch.setattr(
        "driveforge.core.updates.trigger_in_app_update",
        lambda: (trigger_called.append(1), (True, "ok"))[1],
    )
    # Inject an active drive and verify the route refuses.
    from driveforge.daemon.state import get_state
    get_state().active_phase["FAKE-SERIAL"] = "secure_erase"
    try:
        with TestClient(app, follow_redirects=False) as client:
            resp = client.post("/settings/install-update")
        assert resp.status_code == 303
        # Refusal carries an install_error query param explaining why.
        assert "install_error=" in resp.headers["location"]
        assert "drive" in resp.headers["location"].lower()
        assert trigger_called == [], "trigger must NOT be called when refused"
    finally:
        get_state().active_phase.pop("FAKE-SERIAL", None)


def test_install_update_refuses_when_recovery_active(tmp_path, monkeypatch) -> None:
    """Recovery dispatches a fresh pipeline — same daemon-restart
    concern as active drives."""
    from fastapi.testclient import TestClient

    from driveforge import config as cfg
    from driveforge.daemon.app import make_app
    from driveforge.daemon.state import DaemonState, get_state

    settings = cfg.Settings()
    settings.daemon.state_dir = tmp_path
    settings.daemon.db_path = tmp_path / "driveforge.db"
    settings.daemon.pending_labels_dir = tmp_path / "pending-labels"
    settings.daemon.reports_dir = tmp_path / "reports"
    settings.setup_completed = True

    app = make_app(settings)
    DaemonState.boot(settings)
    trigger_called = []
    monkeypatch.setattr(
        "driveforge.core.updates.trigger_in_app_update",
        lambda: (trigger_called.append(1), (True, "ok"))[1],
    )
    get_state().recovery_serials.add("FAKE-RECOVERY")
    try:
        with TestClient(app, follow_redirects=False) as client:
            resp = client.post("/settings/install-update")
        assert resp.status_code == 303
        assert "install_error=" in resp.headers["location"]
        assert "recovery" in resp.headers["location"].lower()
        assert trigger_called == []
    finally:
        get_state().recovery_serials.discard("FAKE-RECOVERY")


def test_install_update_fires_when_idle(tmp_path, monkeypatch) -> None:
    """Idle daemon (no active drives, no recovery) → trigger called,
    redirect to /settings?install_started=1 so the live-log panel
    appears."""
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
    trigger_called = []
    def fake_trigger():
        trigger_called.append(1)
        return (True, "started")
    monkeypatch.setattr("driveforge.core.updates.trigger_in_app_update", fake_trigger)

    with TestClient(app, follow_redirects=False) as client:
        resp = client.post("/settings/install-update")
    assert resp.status_code == 303
    assert "install_started=1" in resp.headers["location"]
    assert trigger_called == [1], "trigger must be called when idle"
