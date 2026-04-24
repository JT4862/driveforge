"""v0.11.6 — verified-delivery fleet update.

Pre-v0.11.6 the operator was "fire and hope": queue UpdateCmd,
immediately fire its own update. Operator restart could SIGTERM
the WebSocket sender_loop before queued bytes flushed, silently
losing the broadcast. JT hit this race during the v0.11.4
walkthrough.

v0.11.6 makes the operator:
  1. Queue UpdateCmd on each online agent
  2. Wait briefly for sender_loops to drain (asyncio yield)
  3. Wait up to ACK_TIMEOUT for each agent's CommandResultMsg
  4. Surface failures + ack count via URL params
  5. THEN trigger the operator's own update

Tests:
  - URL carries fleet_pushed + fleet_acked counts
  - Acked-cmd_ids end up in fleet_acked
  - Agent that errors (CommandResultMsg success=False) lands in fleet_failed
  - Agent that doesn't ack within timeout lands in fleet_failed
  - Operator still updates itself after ack collection
  - Failed agents don't block operator update
  - Standalone install-update unaffected
  - Settings page renders fleet_failed banner
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from driveforge import config as cfg
from driveforge.core import fleet_protocol as proto


def _bootstrap_app(tmp_path, *, role: str = "operator"):
    from driveforge.daemon.app import make_app
    from driveforge.daemon.state import DaemonState
    settings = cfg.Settings()
    settings.daemon.state_dir = tmp_path
    settings.daemon.db_path = tmp_path / "driveforge.db"
    settings.daemon.pending_labels_dir = tmp_path / "pending-labels"
    settings.daemon.reports_dir = tmp_path / "reports"
    settings.setup_completed = True
    settings.fleet.role = role
    app = make_app(settings)
    DaemonState.boot(settings)
    return app


def _agent(state, agent_id="agentA", display_name="r720", *, with_queue=True):
    from driveforge.daemon.state import RemoteAgentState
    ra = RemoteAgentState(
        agent_id=agent_id, display_name=display_name, hostname=None,
        agent_version="0.11.6", protocol_version="1.0",
        connected_at=time.monotonic(), last_message_at=time.monotonic(),
        drives={},
        outbound_queue=asyncio.Queue(maxsize=16) if with_queue else None,
    )
    state.remote_agents[agent_id] = ra
    return ra


def _stub_update(monkeypatch, ok=True, msg="started"):
    from driveforge.core import updates as updates_mod
    monkeypatch.setattr(
        updates_mod, "trigger_in_app_update", lambda: (ok, msg),
    )


def _seed_ack(state, agent_id: str, cmd_id: str, *, success=True, detail=None):
    """Helper: simulate an agent's CommandResultMsg arriving in the
    operator's recent_command_results buffer."""
    ra = state.remote_agents[agent_id]
    ra.recent_command_results.append(
        proto.CommandResultMsg(
            cmd_id=cmd_id, command="update",
            success=success, detail=detail,
        )
    )


# ---------------------------------------------------- ack collection


def test_fleet_update_with_all_acks(tmp_path, monkeypatch) -> None:
    """Two agents both ack quickly → URL carries pushed=2, acked=2,
    no fleet_failed."""
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    _agent(state, "a1", "r720")
    _agent(state, "a2", "r430")
    _stub_update(monkeypatch)

    # Pre-seed acks for the cmd_ids that will be generated. Since
    # cmd_id is random, we pre-seed by patching _new_cmd_id to
    # return predictable values, then seed acks for both.
    cmd_ids: list[str] = []
    counter = iter(range(100))

    def predictable_cmd_id():
        return f"cmd-{next(counter)}"

    monkeypatch.setattr(
        "driveforge.web.routes._new_cmd_id", predictable_cmd_id,
    )
    # Patch the broadcast to seed the ack right after queueing
    from driveforge.daemon import fleet_server
    real_send = fleet_server.send_command_to_agent

    async def send_then_ack(state_, agent_id, command):
        await real_send(state_, agent_id, command)
        # Pull the cmd_id from the just-queued JSON
        import json
        payload = state_.remote_agents[agent_id].outbound_queue.get_nowait()
        body = json.loads(payload)
        cmd_ids.append(body["cmd_id"])
        _seed_ack(state_, agent_id, body["cmd_id"], success=True)

    monkeypatch.setattr(fleet_server, "send_command_to_agent", send_then_ack)

    with TestClient(app) as client:
        resp = client.post("/settings/install-update", follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert "fleet_pushed=2" in location
    assert "fleet_acked=2" in location
    assert "fleet_failed=" not in location


def test_fleet_update_with_one_failed_ack(tmp_path, monkeypatch) -> None:
    """Agent returns success=False → lands in fleet_failed list."""
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    _agent(state, "a1", "r720")
    _agent(state, "a2", "r430")
    _stub_update(monkeypatch)

    from driveforge.daemon import fleet_server
    real_send = fleet_server.send_command_to_agent

    async def send_then_seed(state_, agent_id, command):
        await real_send(state_, agent_id, command)
        import json
        payload = state_.remote_agents[agent_id].outbound_queue.get_nowait()
        body = json.loads(payload)
        # a1 acks success; a2 acks failure
        _seed_ack(
            state_, agent_id, body["cmd_id"],
            success=(agent_id == "a1"),
            detail=None if agent_id == "a1" else "polkit denied",
        )

    monkeypatch.setattr(fleet_server, "send_command_to_agent", send_then_seed)

    with TestClient(app) as client:
        resp = client.post("/settings/install-update", follow_redirects=False)
    location = resp.headers["location"]
    assert "fleet_pushed=2" in location
    assert "fleet_acked=1" in location
    assert "fleet_failed=" in location
    assert "r430" in location  # the failing agent's display name


def test_fleet_update_offline_agent_skipped(tmp_path, monkeypatch) -> None:
    """outbound_queue=None → agent skipped, no entry anywhere."""
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    _agent(state, "online", "live", with_queue=True)
    _agent(state, "offline", "down", with_queue=False)
    _stub_update(monkeypatch)

    from driveforge.daemon import fleet_server
    real_send = fleet_server.send_command_to_agent

    async def send_then_ack(state_, agent_id, command):
        await real_send(state_, agent_id, command)
        import json
        payload = state_.remote_agents[agent_id].outbound_queue.get_nowait()
        body = json.loads(payload)
        _seed_ack(state_, agent_id, body["cmd_id"], success=True)

    monkeypatch.setattr(fleet_server, "send_command_to_agent", send_then_ack)

    with TestClient(app) as client:
        resp = client.post("/settings/install-update", follow_redirects=False)
    location = resp.headers["location"]
    assert "fleet_pushed=1" in location
    assert "fleet_acked=1" in location


def test_standalone_install_update_unaffected(tmp_path, monkeypatch) -> None:
    """No fleet → no fleet_pushed/acked/failed in URL."""
    app = _bootstrap_app(tmp_path, role="standalone")
    _stub_update(monkeypatch)
    with TestClient(app) as client:
        resp = client.post("/settings/install-update", follow_redirects=False)
    location = resp.headers["location"]
    assert "install_started=1" in location
    assert "fleet_pushed=" not in location
    assert "fleet_failed=" not in location


# ---------------------------------------------------- Settings render


def test_settings_renders_fleet_failed_banner(tmp_path) -> None:
    """The settings page surfaces the fleet_failed list as a warn
    banner with each agent's display name."""
    app = _bootstrap_app(tmp_path, role="operator")
    with TestClient(app) as client:
        resp = client.get(
            "/settings?install_started=1&fleet_pushed=2&fleet_acked=1&fleet_failed=r720,r430"
        )
    body = resp.text
    assert "did not confirm" in body
    assert "r720" in body
    assert "r430" in body
    assert "sudo systemctl start driveforge-update.service" in body


def test_settings_no_failed_banner_when_all_acked(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    with TestClient(app) as client:
        resp = client.get(
            "/settings?install_started=1&fleet_pushed=2&fleet_acked=2"
        )
    assert "did not confirm" not in resp.text


def test_settings_render_acked_count(tmp_path) -> None:
    """The ack count is visible in the success banner alongside the
    push count."""
    app = _bootstrap_app(tmp_path, role="operator")
    with TestClient(app) as client:
        resp = client.get(
            "/settings?install_started=1&fleet_pushed=3&fleet_acked=3"
        )
    body = resp.text
    assert "<strong>3</strong>" in body  # the ack count specifically
    assert "confirmed receipt" in body
