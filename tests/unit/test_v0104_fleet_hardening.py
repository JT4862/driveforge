"""v0.10.4 — fleet hardening.

Covers:
  - _record_refusal appends to buffer + caps at 32
  - Missing-token path lands in refusals with reason "missing bearer token"
  - Invalid-token + revoked-token paths surface distinct reasons
  - Protocol-skew hello records a refusal
  - agent_id mismatch records a refusal
  - kick_agent_session closes the active WS + clears ws ref
  - kick_agent_session returns False on disconnected agent
  - Revoke handler kicks the live session
  - Rotate handler: revokes old agent + mints enrollment token + kicks
  - Agents page renders live status (connected / online / offline) +
    refusals panel
  - /api/fleet/local-status returns role-specific fields for all
    three roles
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

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
    app = make_app(settings)
    DaemonState.boot(settings)
    return app


def _enroll(state) -> tuple[str, str]:
    with state.session_factory() as session:
        issue = fleet_mod.issue_enrollment_token(session, ttl_seconds=60)
    with state.session_factory() as session:
        result = fleet_mod.consume_enrollment_token(
            session, composite_token=issue.raw_token,
            display_name="r720", hostname="driveforge-r720", version="0.10.4",
        )
    return result.agent_id, result.api_token


# ---------------------------------------------------- Refusals buffer


def test_record_refusal_appends_and_caps(tmp_path) -> None:
    _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.fleet_server import _record_refusal
    from driveforge.daemon.state import get_state
    state = get_state()
    ws = MagicMock()
    ws.client = None
    for i in range(50):
        _record_refusal(state, f"reason-{i}", token_agent_id=None, ws=ws)
    # Cap at 32
    assert len(state.fleet_refusals) == 32
    # Newest survives; oldest drops
    reasons = [r["reason"] for r in state.fleet_refusals]
    assert "reason-49" in reasons
    assert "reason-0" not in reasons


def test_missing_token_lands_in_refusals(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/fleet/ws"):
                pass
    reasons = [r["reason"] for r in state.fleet_refusals]
    assert "missing bearer token" in reasons


def test_invalid_token_distinguished_from_revoked(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    # Unknown token
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/fleet/ws", headers={"Authorization": "Bearer abc.bogus"},
            ):
                pass
    # Enrolled, then revoked
    agent_id, token = _enroll(state)
    with state.session_factory() as session:
        fleet_mod.revoke_agent(session, agent_id)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/fleet/ws", headers={"Authorization": f"Bearer {token}"},
            ):
                pass
    reasons = [r["reason"] for r in state.fleet_refusals]
    assert "invalid token" in reasons
    assert "token revoked" in reasons


def test_protocol_skew_records_refusal(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    agent_id, token = _enroll(state)
    with TestClient(app) as client:
        with client.websocket_connect(
            "/fleet/ws", headers={"Authorization": f"Bearer {token}"},
        ) as ws:
            ws.send_json({
                "msg": "hello",
                "agent_id": agent_id,
                "display_name": "r720",
                "agent_version": "0.10.4",
                "protocol_version": "99.0",  # major skew
            })
            ws.receive_json()  # hello_ack with refused_reason
    reasons = [r["reason"] for r in state.fleet_refusals]
    assert any("incompatible" in r for r in reasons)


def test_agent_id_mismatch_records_refusal(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    _agent_id, token = _enroll(state)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/fleet/ws", headers={"Authorization": f"Bearer {token}"},
            ) as ws:
                ws.send_json({
                    "msg": "hello",
                    "agent_id": "imposter",
                    "display_name": "r720",
                    "agent_version": "0.10.4",
                    "protocol_version": proto.PROTOCOL_VERSION,
                })
                ws.receive_json()
    reasons = [r["reason"] for r in state.fleet_refusals]
    assert "agent_id mismatch" in reasons


# ---------------------------------------------------- kick_agent_session


def test_kick_agent_session_closes_ws(tmp_path) -> None:
    _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.fleet_server import kick_agent_session
    from driveforge.daemon.state import RemoteAgentState, get_state
    import time as _time
    state = get_state()
    fake_ws = MagicMock()
    fake_ws.close = AsyncMock()
    state.remote_agents["abc"] = RemoteAgentState(
        agent_id="abc", display_name="r720", hostname=None,
        agent_version="0.10.4", protocol_version="1.0",
        connected_at=_time.monotonic(), last_message_at=_time.monotonic(),
        drives={}, ws=fake_ws,
    )

    async def run_it():
        kicked = await kick_agent_session(state, "abc", reason="test")
        assert kicked is True
        fake_ws.close.assert_awaited_once()
        assert state.remote_agents["abc"].ws is None

    asyncio.run(run_it())


def test_kick_agent_session_noop_when_not_connected(tmp_path) -> None:
    _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.fleet_server import kick_agent_session
    from driveforge.daemon.state import get_state
    state = get_state()

    async def run_it():
        # Never connected
        assert await kick_agent_session(state, "no-such", reason="x") is False

    asyncio.run(run_it())


# ---------------------------------------------------- Revoke handler


def test_revoke_handler_kicks_active_session(tmp_path) -> None:
    """POST /settings/agents/<id>/revoke kicks the WS handle so the
    agent immediately loses its connection."""
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import RemoteAgentState, get_state
    import time as _time
    state = get_state()
    agent_id, _token = _enroll(state)
    fake_ws = MagicMock()
    fake_ws.close = AsyncMock()
    state.remote_agents[agent_id] = RemoteAgentState(
        agent_id=agent_id, display_name="r720", hostname=None,
        agent_version="0.10.4", protocol_version="1.0",
        connected_at=_time.monotonic(), last_message_at=_time.monotonic(),
        drives={}, ws=fake_ws,
    )
    with TestClient(app) as client:
        resp = client.post(
            f"/settings/agents/{agent_id}/revoke", follow_redirects=False,
        )
    assert resp.status_code == 303
    fake_ws.close.assert_awaited_once()
    # Agent row marked revoked
    with state.session_factory() as session:
        agent = session.get(m.Agent, agent_id)
        assert agent.revoked_at is not None


# ---------------------------------------------------- Rotate handler


def test_rotate_handler_revokes_and_issues_new_token(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import RemoteAgentState, get_state
    import time as _time
    state = get_state()
    agent_id, _token = _enroll(state)
    fake_ws = MagicMock()
    fake_ws.close = AsyncMock()
    state.remote_agents[agent_id] = RemoteAgentState(
        agent_id=agent_id, display_name="r720", hostname=None,
        agent_version="0.10.4", protocol_version="1.0",
        connected_at=_time.monotonic(), last_message_at=_time.monotonic(),
        drives={}, ws=fake_ws,
    )
    with TestClient(app) as client:
        resp = client.post(
            f"/settings/agents/{agent_id}/rotate", follow_redirects=False,
        )
    assert resp.status_code == 303
    location = resp.headers["location"]
    # New enrollment token shown via ?new_token + ?rotated
    assert "new_token=" in location
    assert f"rotated={agent_id}" in location
    # Old agent revoked
    with state.session_factory() as session:
        old = session.get(m.Agent, agent_id)
        assert old.revoked_at is not None
        # Exactly one UNCONSUMED fresh enrollment token in the DB
        # (the original _enroll() helper consumed its own token
        # during setup; rotate issues a new one.)
        unconsumed = (
            session.query(m.EnrollmentToken)
            .filter(m.EnrollmentToken.consumed_at.is_(None))
            .all()
        )
        assert len(unconsumed) == 1
    # Session kicked
    fake_ws.close.assert_awaited_once()


def test_rotate_refuses_on_standalone(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="standalone")
    with TestClient(app) as client:
        resp = client.post(
            "/settings/agents/abc/rotate", follow_redirects=False,
        )
    assert resp.status_code == 400


# ---------------------------------------------------- Agents page render


def test_agents_page_shows_live_connected_badge(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import RemoteAgentState, get_state
    import time as _time
    state = get_state()
    agent_id, _token = _enroll(state)
    state.remote_agents[agent_id] = RemoteAgentState(
        agent_id=agent_id, display_name="r720", hostname=None,
        agent_version="0.10.4", protocol_version="1.0",
        connected_at=_time.monotonic(), last_message_at=_time.monotonic(),
        drives={"X": proto.DriveState(
            serial="X", model="m", capacity_bytes=1, transport="sata",
        )},
        ws=MagicMock(),
    )
    with TestClient(app) as client:
        resp = client.get("/settings/agents")
    body = resp.text
    assert "connected" in body
    assert "1 drives" in body


def test_agents_page_shows_offline_when_no_live_state(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    _enroll(state)
    # No remote_agents entry → offline
    with TestClient(app) as client:
        resp = client.get("/settings/agents")
    assert "offline" in resp.text


def test_agents_page_renders_refusals_panel(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.fleet_server import _record_refusal
    from driveforge.daemon.state import get_state
    state = get_state()
    ws = MagicMock(); ws.client = None
    _record_refusal(state, "token revoked", token_agent_id="abc123", ws=ws)
    with TestClient(app) as client:
        resp = client.get("/settings/agents")
    body = resp.text
    assert "Recent connection refusals" in body
    assert "token revoked" in body
    assert "abc123" in body


# ---------------------------------------------------- local-status API


def test_local_status_operator(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import RemoteAgentState, get_state
    import time as _time
    state = get_state()
    state.remote_agents["a1"] = RemoteAgentState(
        agent_id="a1", display_name="r720", hostname=None,
        agent_version="0.10.4", protocol_version="1.0",
        connected_at=_time.monotonic(), last_message_at=_time.monotonic(),
        drives={}, ws=MagicMock(),
    )
    with TestClient(app) as client:
        resp = client.get("/api/fleet/local-status")
    body = resp.json()
    assert body["role"] == "operator"
    assert body["agents_total"] == 1
    assert body["agents_online"] == 1
    assert body["agents_connected"] == 1


def test_local_status_agent_without_client(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="agent")
    with TestClient(app) as client:
        resp = client.get("/api/fleet/local-status")
    body = resp.json()
    assert body["role"] == "agent"
    # Lifespan didn't install a client (role had no operator_url),
    # so the endpoint surfaces the "not running" branch
    assert body["connected"] is False


def test_local_status_standalone(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="standalone")
    with TestClient(app) as client:
        resp = client.get("/api/fleet/local-status")
    body = resp.json()
    assert body["role"] == "standalone"
    # No role-specific keys
    assert "connected" not in body
    assert "agents_total" not in body
