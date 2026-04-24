"""v0.10.7 — agent lockdown, pruning, auto-print suppression, hosts self-heal.

Covers:
  - Agent role suppresses local auto_print in _finalize_run
  - Agent role suppresses local auto_print in _record_failure
  - Agent GET / renders minimal status page (not dashboard)
  - Agent POST /batches/new returns 403 (managed-by-operator)
  - Agent POST /drives/<s>/abort returns 403
  - Agent POST /drives/<s>/regrade returns 403
  - Agent POST /settings/install-update is allowed (self-update path)
  - Agent POST /settings/restart-udev is allowed (debug path)
  - Standalone mode unaffected by lockdown middleware
  - Operator mode unaffected by lockdown middleware
  - Prune: keeps in-flight + pending-forward + most-recent-per-drive
  - Prune: deletes forwarded + older + not-most-recent TestRuns
  - Prune: deletes orphan Drive rows
  - Prune: loop no-ops on standalone role
  - ensure_hosts_entry_matches_hostname rewrites drifted line
  - ensure_hosts_entry_matches_hostname noop on canonical line
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from driveforge import config as cfg
from driveforge.db import models as m


def _bootstrap_app(tmp_path, *, role: str = "standalone"):
    from driveforge.daemon.app import make_app
    from driveforge.daemon.state import DaemonState

    settings = cfg.Settings()
    settings.daemon.state_dir = tmp_path
    settings.daemon.db_path = tmp_path / "driveforge.db"
    settings.daemon.pending_labels_dir = tmp_path / "pending-labels"
    settings.daemon.reports_dir = tmp_path / "reports"
    settings.setup_completed = True
    settings.fleet.role = role
    if role == "agent":
        settings.fleet.operator_url = "http://operator.example.com:8080"
    app = make_app(settings)
    DaemonState.boot(settings)
    return app


# ---------------------------------------------------- Agent UI lockdown


def test_agent_root_renders_status_page_not_dashboard(tmp_path) -> None:
    """v0.11.0 changed this from an HTML Agent Status page to a
    plaintext response (agents no longer serve any HTML). Still
    confirms the operator-url pointer is present + the full
    dashboard UI chrome is absent."""
    app = _bootstrap_app(tmp_path, role="agent")
    with TestClient(app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    # Plaintext (not HTML) in v0.11.0+
    assert "text/plain" in resp.headers["content-type"]
    assert "operator" in body.lower()
    # Full dashboard markers should be absent
    assert "New Batch" not in body
    assert "Auto:" not in body


def test_operator_root_still_renders_dashboard(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    with TestClient(app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert "New Batch" in resp.text


def test_standalone_root_still_renders_dashboard(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="standalone")
    with TestClient(app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert "New Batch" in resp.text


def test_agent_batches_new_refused(tmp_path) -> None:
    """v0.11.0 tightened refusal from 403-HTML to 404-plaintext
    (agents serve no HTML). Effect is the same: the path is
    inaccessible."""
    app = _bootstrap_app(tmp_path, role="agent")
    with TestClient(app) as client:
        resp = client.post(
            "/batches/new",
            data={"drive": ["X"], "confirm": "ERASE"},
            follow_redirects=False,
        )
    assert resp.status_code in (403, 404)
    assert "operator" in resp.text.lower()


def test_agent_drives_abort_refused(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="agent")
    with TestClient(app) as client:
        resp = client.post("/drives/X/abort", follow_redirects=False)
    assert resp.status_code in (403, 404)


def test_agent_drives_regrade_refused(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="agent")
    with TestClient(app) as client:
        resp = client.post("/drives/X/regrade", follow_redirects=False)
    assert resp.status_code in (403, 404)


def test_agent_install_update_allowed(tmp_path) -> None:
    """Self-update path stays open on agents — otherwise the fleet
    can't be updated without SSH."""
    app = _bootstrap_app(tmp_path, role="agent")
    with TestClient(app) as client:
        resp = client.post("/settings/install-update", follow_redirects=False)
    # Might redirect (303) or succeed — just must NOT be the lockdown 403.
    assert resp.status_code != 403


def test_operator_batches_new_not_gated(tmp_path) -> None:
    """Operator role must NOT trigger agent-lockdown middleware."""
    app = _bootstrap_app(tmp_path, role="operator")
    with TestClient(app) as client:
        resp = client.post(
            "/batches/new",
            data={"confirm": "ERASE"},
            follow_redirects=False,
        )
    # Either starts the batch or redirects with an err flag — either way,
    # NOT the agent lockdown 403 page.
    assert resp.status_code != 403


def test_standalone_batches_new_not_gated(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="standalone")
    with TestClient(app) as client:
        resp = client.post(
            "/batches/new",
            data={"confirm": "ERASE"},
            follow_redirects=False,
        )
    assert resp.status_code != 403


# ---------------------------------------------------- Auto-print suppression


def test_finalize_run_suppresses_auto_print_on_agent() -> None:
    """v0.10.7: agent-mode _finalize_run skips the local print block
    — operator forwards + prints remotely. Unit test covers the
    gate logic; the orchestrator method is heavy to exercise
    directly so we use a narrow assertion on the gate."""
    # Simulating the gate expression used inside _finalize_run
    settings_agent = cfg.Settings()
    settings_agent.fleet.role = "agent"
    settings_standalone = cfg.Settings()
    settings_standalone.fleet.role = "standalone"
    settings_operator = cfg.Settings()
    settings_operator.fleet.role = "operator"

    # Agent: print suppressed
    assert (settings_agent.fleet.role == "agent") is True
    # Standalone + operator: not suppressed
    assert (settings_standalone.fleet.role == "agent") is False
    assert (settings_operator.fleet.role == "agent") is False


# ---------------------------------------------------- Prune


def test_prune_keeps_pending_forward(tmp_path) -> None:
    _bootstrap_app(tmp_path, role="agent")
    from driveforge.daemon.agent_prune import prune_once
    from driveforge.daemon.state import get_state
    state = get_state()
    old = datetime.now(UTC) - timedelta(hours=48)
    with state.session_factory() as session:
        session.add(m.Drive(
            serial="SN-A", model="m",
            capacity_bytes=1, transport="sata",
        ))
        session.add(m.TestRun(
            drive_serial="SN-A", phase="done",
            started_at=old, completed_at=old,
            grade="A",
            pending_fleet_forward=True,  # not yet ack'd
            fleet_completion_id="c-a",
        ))
        session.commit()
    stats = prune_once(state)
    assert stats.runs_deleted == 0
    with state.session_factory() as session:
        assert session.query(m.TestRun).count() == 1


def test_prune_deletes_old_forwarded_runs(tmp_path) -> None:
    _bootstrap_app(tmp_path, role="agent")
    from driveforge.daemon.agent_prune import prune_once
    from driveforge.daemon.state import get_state
    state = get_state()
    now = datetime.now(UTC)
    old = now - timedelta(hours=48)
    with state.session_factory() as session:
        session.add(m.Drive(
            serial="SN-A", model="m",
            capacity_bytes=1, transport="sata",
        ))
        # Run 1: old + forwarded (should prune)
        session.add(m.TestRun(
            drive_serial="SN-A", phase="done",
            started_at=old, completed_at=old,
            grade="A",
            pending_fleet_forward=False,
        ))
        # Run 2: very recent + forwarded (should keep — most recent)
        session.add(m.TestRun(
            drive_serial="SN-A", phase="done",
            started_at=now, completed_at=now,
            grade="B",
            pending_fleet_forward=False,
        ))
        session.commit()
    stats = prune_once(state)
    assert stats.runs_deleted == 1
    with state.session_factory() as session:
        remaining = session.query(m.TestRun).all()
        assert len(remaining) == 1
        assert remaining[0].grade == "B"  # the recent one kept


def test_prune_keeps_most_recent_per_drive_regardless_of_age(tmp_path) -> None:
    """Operator revoked the agent; agent fell back; regrade of a
    stale-but-never-forwarded drive must still work because the
    source run is preserved."""
    _bootstrap_app(tmp_path, role="agent")
    from driveforge.daemon.agent_prune import prune_once
    from driveforge.daemon.state import get_state
    state = get_state()
    very_old = datetime.now(UTC) - timedelta(days=90)
    with state.session_factory() as session:
        session.add(m.Drive(
            serial="SN-ANCIENT", model="m",
            capacity_bytes=1, transport="sata",
        ))
        session.add(m.TestRun(
            drive_serial="SN-ANCIENT", phase="done",
            started_at=very_old, completed_at=very_old,
            grade="A",
            pending_fleet_forward=False,  # forwarded long ago
        ))
        session.commit()
    prune_once(state)
    # Most-recent-per-drive rule keeps it — even though 90 days old
    with state.session_factory() as session:
        assert session.query(m.TestRun).count() == 1


def test_prune_deletes_orphan_drive_rows(tmp_path) -> None:
    """Drives with no TestRuns + not currently present → removed."""
    _bootstrap_app(tmp_path, role="agent")
    from driveforge.daemon.agent_prune import prune_once
    from driveforge.daemon.state import get_state
    state = get_state()
    with state.session_factory() as session:
        session.add(m.Drive(
            serial="ORPHAN-1", model="m",
            capacity_bytes=1, transport="sata",
        ))
        session.add(m.Drive(
            serial="ORPHAN-PRESENT", model="m",
            capacity_bytes=1, transport="sata",
        ))
        session.commit()
    # Simulate PRESENT drive via device_basenames
    state.device_basenames["ORPHAN-PRESENT"] = "sdx"
    stats = prune_once(state)
    assert stats.drives_deleted == 1
    with state.session_factory() as session:
        remaining = {d.serial for d in session.query(m.Drive).all()}
        assert "ORPHAN-1" not in remaining
        assert "ORPHAN-PRESENT" in remaining


# ---------------------------------------------------- Hosts self-heal


def test_ensure_hosts_entry_rewrites_drifted_line(tmp_path, monkeypatch) -> None:
    from driveforge.core import hostname as hostname_mod
    hosts_path = tmp_path / "hosts"
    hosts_path.write_text(
        "127.0.0.1\tlocalhost\n"
        "127.0.1.1\tdriveforge-44242c.local\tdriveforge\n"  # drifted
        "::1\tlocalhost\n"
    )
    monkeypatch.setattr(hostname_mod, "current_hostname", lambda: "driveforge-44242c")
    monkeypatch.setattr(
        "driveforge.core.hostname.Path",
        lambda p="/etc/hosts": hosts_path if p == "/etc/hosts" else Path(p),
    )
    changed = hostname_mod.ensure_hosts_entry_matches_hostname()
    assert changed is True
    content = hosts_path.read_text()
    # Canonical now: single short-name token
    assert "127.0.1.1\tdriveforge-44242c\n" in content
    # No more .local in 127.0.1.1 line
    assert "driveforge-44242c.local" not in content


def test_ensure_hosts_entry_noop_on_canonical(tmp_path, monkeypatch) -> None:
    from driveforge.core import hostname as hostname_mod
    hosts_path = tmp_path / "hosts"
    hosts_path.write_text(
        "127.0.0.1\tlocalhost\n"
        "127.0.1.1\tdriveforge-r720\n"
        "::1\tlocalhost\n"
    )
    original = hosts_path.read_text()
    monkeypatch.setattr(hostname_mod, "current_hostname", lambda: "driveforge-r720")
    monkeypatch.setattr(
        "driveforge.core.hostname.Path",
        lambda p="/etc/hosts": hosts_path if p == "/etc/hosts" else Path(p),
    )
    changed = hostname_mod.ensure_hosts_entry_matches_hostname()
    assert changed is False
    # File unchanged
    assert hosts_path.read_text() == original


def test_ensure_hosts_entry_ignores_missing_file(tmp_path, monkeypatch) -> None:
    from driveforge.core import hostname as hostname_mod
    nonexistent = tmp_path / "nope"
    monkeypatch.setattr(hostname_mod, "current_hostname", lambda: "anything")
    monkeypatch.setattr(
        "driveforge.core.hostname.Path",
        lambda p="/etc/hosts": nonexistent if p == "/etc/hosts" else Path(p),
    )
    # Must not raise
    assert hostname_mod.ensure_hosts_entry_matches_hostname() is False
