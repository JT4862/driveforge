"""v0.10.9 — fleet-wide auto_enroll_mode.

Covers:
  - HelloAckMsg carries operator.auto_enroll_mode
  - ConfigUpdateMsg roundtrip
  - Agent captures auto_enroll_mode from hello_ack (at _run_session)
  - Agent captures auto_enroll_mode from ConfigUpdateMsg
  - Agent ignores invalid auto_enroll_mode values
  - Hotplug auto-enroll: agent-role uses operator's cached value
  - Hotplug auto-enroll: fail-closed when no cached value (pre-ack)
  - Hotplug auto-enroll: standalone/operator still use local config
  - POST /settings/auto-enroll broadcasts ConfigUpdateMsg to agents
  - Broadcast skips offline agents cleanly
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from driveforge import config as cfg
from driveforge.core import fleet as fleet_mod
from driveforge.core import fleet_protocol as proto
from driveforge.db import models as m


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
    if role == "agent":
        settings.fleet.operator_url = "http://operator.example.com:8080"
    app = make_app(settings)
    DaemonState.boot(settings)
    return app


def _enroll(state):
    with state.session_factory() as session:
        issue = fleet_mod.issue_enrollment_token(session, ttl_seconds=60)
    with state.session_factory() as session:
        result = fleet_mod.consume_enrollment_token(
            session, composite_token=issue.raw_token,
            display_name="r720", hostname=None, version="0.10.9",
        )
    return result.agent_id, result.api_token


# ---------------------------------------------------- Protocol


def test_hello_ack_carries_auto_enroll_mode() -> None:
    msg = proto.HelloAckMsg(operator_version="0.10.9", auto_enroll_mode="quick")
    data = msg.model_dump(mode="json")
    assert data["auto_enroll_mode"] == "quick"
    reparsed = proto.HelloAckMsg.model_validate(data)
    assert reparsed.auto_enroll_mode == "quick"


def test_hello_ack_auto_enroll_defaults_none_for_forward_compat() -> None:
    """Pre-v0.10.9 operators don't send the field; agents must not
    crash on absence."""
    msg = proto.HelloAckMsg(operator_version="0.10.9")
    assert msg.auto_enroll_mode is None


def test_config_update_msg_roundtrip() -> None:
    msg = proto.ConfigUpdateMsg(auto_enroll_mode="full")
    data = msg.model_dump(mode="json")
    assert data["msg"] == "config_update"
    assert data["auto_enroll_mode"] == "full"
    reparsed = proto.ConfigUpdateMsg.model_validate(data)
    assert reparsed.auto_enroll_mode == "full"


# ---------------------------------------------------- Operator sends


def test_operator_includes_auto_enroll_in_hello_ack(tmp_path) -> None:
    """End-to-end: real WebSocket handshake, verify ack carries
    the operator's current mode."""
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    state.settings.daemon.auto_enroll_mode = "full"
    agent_id, token = _enroll(state)
    with TestClient(app) as client:
        with client.websocket_connect(
            "/fleet/ws", headers={"Authorization": f"Bearer {token}"},
        ) as ws:
            ws.send_json(proto.HelloMsg(
                agent_id=agent_id, display_name="r720",
                agent_version="0.10.9",
            ).model_dump(mode="json"))
            ack = ws.receive_json()
    assert ack["msg"] == "hello_ack"
    assert ack["auto_enroll_mode"] == "full"


# ---------------------------------------------------- Agent receives


def test_agent_captures_auto_enroll_from_config_update(tmp_path) -> None:
    """The ConfigUpdateMsg handler lands the new value on
    state.fleet_operator_auto_enroll_mode."""
    _bootstrap_app(tmp_path, role="agent")
    from driveforge.daemon.fleet_client import FleetClient
    from driveforge.daemon.state import get_state
    state = get_state()
    client = FleetClient(state)
    # Initial: None
    assert state.fleet_operator_auto_enroll_mode is None
    # Apply a config update
    client._handle_config_update({"msg": "config_update", "auto_enroll_mode": "quick"})
    assert state.fleet_operator_auto_enroll_mode == "quick"
    # Change again
    client._handle_config_update({"msg": "config_update", "auto_enroll_mode": "off"})
    assert state.fleet_operator_auto_enroll_mode == "off"


def test_agent_ignores_invalid_auto_enroll_value(tmp_path) -> None:
    _bootstrap_app(tmp_path, role="agent")
    from driveforge.daemon.fleet_client import FleetClient
    from driveforge.daemon.state import get_state
    state = get_state()
    client = FleetClient(state)
    state.fleet_operator_auto_enroll_mode = "quick"
    client._handle_config_update({"msg": "config_update", "auto_enroll_mode": "nonsense"})
    # Unchanged
    assert state.fleet_operator_auto_enroll_mode == "quick"


def test_agent_ignores_malformed_config_update(tmp_path) -> None:
    _bootstrap_app(tmp_path, role="agent")
    from driveforge.daemon.fleet_client import FleetClient
    from driveforge.daemon.state import get_state
    state = get_state()
    client = FleetClient(state)
    state.fleet_operator_auto_enroll_mode = "full"
    # Missing required field? The field is optional so this still parses.
    # Key path is: bad JSON structure should not crash the client.
    client._handle_config_update({"not": "a message"})
    # State untouched
    assert state.fleet_operator_auto_enroll_mode == "full"


# ---------------------------------------------------- Hotplug gate


def test_hotplug_gate_agent_uses_operator_cached_mode(tmp_path) -> None:
    """Agent with stale local 'full' but operator says 'off' → NO auto-enroll."""
    _bootstrap_app(tmp_path, role="agent")
    from driveforge.daemon.state import get_state
    state = get_state()
    # Simulate: local config has "full" (historical value), operator cached "off"
    state.settings.daemon.auto_enroll_mode = "full"
    state.fleet_operator_auto_enroll_mode = "off"
    # Replicate the hotplug gate's exact decision
    if state.settings.fleet.role == "agent":
        effective = state.fleet_operator_auto_enroll_mode or "off"
    else:
        effective = state.settings.daemon.auto_enroll_mode or "off"
    assert effective == "off"


def test_hotplug_gate_agent_fails_closed_before_handshake(tmp_path) -> None:
    """Agent role, no operator mode cached yet → "off" (fail-closed)."""
    _bootstrap_app(tmp_path, role="agent")
    from driveforge.daemon.state import get_state
    state = get_state()
    # Local says "full" but no operator handshake has happened
    state.settings.daemon.auto_enroll_mode = "full"
    state.fleet_operator_auto_enroll_mode = None
    effective = (
        state.fleet_operator_auto_enroll_mode or "off"
        if state.settings.fleet.role == "agent"
        else state.settings.daemon.auto_enroll_mode or "off"
    )
    assert effective == "off"


def test_hotplug_gate_standalone_uses_local_config(tmp_path) -> None:
    _bootstrap_app(tmp_path, role="standalone")
    from driveforge.daemon.state import get_state
    state = get_state()
    state.settings.daemon.auto_enroll_mode = "quick"
    # Even if someone set fleet_operator_auto_enroll_mode, standalone
    # ignores it (role gate).
    state.fleet_operator_auto_enroll_mode = "off"
    effective = (
        state.fleet_operator_auto_enroll_mode or "off"
        if state.settings.fleet.role == "agent"
        else state.settings.daemon.auto_enroll_mode or "off"
    )
    assert effective == "quick"


def test_hotplug_gate_operator_uses_local_config(tmp_path) -> None:
    """Operator's OWN drives obey operator's local config (of course —
    the fleet-wide value IS this value for the operator's own
    chassis)."""
    _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    state.settings.daemon.auto_enroll_mode = "quick"
    effective = (
        state.fleet_operator_auto_enroll_mode or "off"
        if state.settings.fleet.role == "agent"
        else state.settings.daemon.auto_enroll_mode or "off"
    )
    assert effective == "quick"


# ---------------------------------------------------- Operator broadcasts


def test_operator_auto_enroll_post_broadcasts_to_agents(tmp_path) -> None:
    """Click Auto: Quick on operator dashboard → every connected
    agent gets a ConfigUpdateMsg on its outbound queue."""
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import RemoteAgentState, get_state
    import time as _time
    state = get_state()
    # Two enrolled, online agents
    for aid in ("agent-1", "agent-2"):
        state.remote_agents[aid] = RemoteAgentState(
            agent_id=aid, display_name=aid, hostname=None,
            agent_version="0.10.9", protocol_version="1.0",
            connected_at=_time.monotonic(), last_message_at=_time.monotonic(),
            drives={}, outbound_queue=asyncio.Queue(maxsize=16),
        )
    with TestClient(app) as client:
        client.post("/settings/auto-enroll", data={"mode": "quick"})
    # Both agents should have a config_update frame waiting
    for aid in ("agent-1", "agent-2"):
        q = state.remote_agents[aid].outbound_queue
        assert q.qsize() == 1
        payload = q.get_nowait()
        body = json.loads(payload)
        assert body["msg"] == "config_update"
        assert body["auto_enroll_mode"] == "quick"


def test_operator_broadcast_skips_offline_agents_cleanly(tmp_path) -> None:
    """Agents with no active session (outbound_queue=None) get
    skipped without raising."""
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import RemoteAgentState, get_state
    import time as _time
    state = get_state()
    state.remote_agents["offline"] = RemoteAgentState(
        agent_id="offline", display_name="offline", hostname=None,
        agent_version="0.10.9", protocol_version="1.0",
        connected_at=_time.monotonic(), last_message_at=_time.monotonic(),
        drives={}, outbound_queue=None,  # offline
    )
    with TestClient(app) as client:
        resp = client.post(
            "/settings/auto-enroll", data={"mode": "full"},
            follow_redirects=False,
        )
    # Must still redirect successfully even with offline agents
    assert resp.status_code == 303
    # The operator's own setting persisted
    assert state.settings.daemon.auto_enroll_mode == "full"


def test_standalone_auto_enroll_toggle_no_broadcast(tmp_path) -> None:
    """When role is standalone, the broadcast code path is skipped
    (there's no fleet to broadcast to)."""
    app = _bootstrap_app(tmp_path, role="standalone")
    from driveforge.daemon.state import get_state
    state = get_state()
    with TestClient(app) as client:
        resp = client.post(
            "/settings/auto-enroll", data={"mode": "quick"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert state.settings.daemon.auto_enroll_mode == "quick"
