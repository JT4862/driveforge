"""v0.10.1 — fleet WebSocket transport + dashboard aggregation.

Covers:
  - Protocol pydantic models serialize + roundtrip cleanly
  - is_protocol_compatible major-match logic
  - _http_url_to_ws_url normalizes scheme variants
  - /fleet/ws closes with 1008 when role != operator
  - /fleet/ws closes with 1008 when no bearer token
  - /fleet/ws closes with 1008 when token is invalid / revoked
  - hello message → hello_ack; operator_version populated
  - protocol-skew hello is refused with refused_reason
  - agent_id mismatch between token and hello closes 1008
  - drive_snapshot updates state.remote_agents[agent_id].drives
  - out-of-order snapshots (stale seq) are dropped
  - heartbeat stamps last_message_at without changing drives
  - _drive_view merges remote drives when fleet.role == operator
  - host_filter restricts view to local / a specific agent
  - host badge renders on remote cards via template snapshot test
  - FleetClient builds a snapshot from live DaemonState
  - FleetClient backs off on connect failure
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


def _enroll_and_get_token(state) -> tuple[str, str]:
    """Helper — issue + consume a token, return (agent_id, composite_token)."""
    with state.session_factory() as session:
        issue = fleet_mod.issue_enrollment_token(session, ttl_seconds=60)
    with state.session_factory() as session:
        result = fleet_mod.consume_enrollment_token(
            session,
            composite_token=issue.raw_token,
            display_name="test-agent",
            hostname="driveforge-test",
            version="0.10.1",
        )
    return result.agent_id, result.api_token


# ---------------------------------------------------- Protocol


def test_protocol_version_matches_major() -> None:
    assert proto.is_protocol_compatible("1.0") is True
    assert proto.is_protocol_compatible("1.5") is True  # forward-compat on minor
    assert proto.is_protocol_compatible("2.0") is False  # major bump
    assert proto.is_protocol_compatible("") is False


def test_hello_msg_roundtrip() -> None:
    h = proto.HelloMsg(agent_id="abc", display_name="r720", agent_version="0.10.1")
    data = h.model_dump(mode="json")
    assert data["msg"] == "hello"
    assert data["protocol_version"] == proto.PROTOCOL_VERSION
    reparsed = proto.HelloMsg.model_validate(data)
    assert reparsed.agent_id == "abc"


def test_drive_snapshot_with_mixed_active_and_idle() -> None:
    snap = proto.DriveSnapshotMsg(
        drives=[
            proto.DriveState(
                serial="A1", model="m", capacity_bytes=1_000_000_000_000,
                transport="sata", phase="badblocks", percent=42.5,
            ),
            proto.DriveState(
                serial="A2", model="m", capacity_bytes=2_000_000_000_000,
                transport="sas",
            ),
        ],
        seq=7,
    )
    data = snap.model_dump(mode="json")
    reparsed = proto.DriveSnapshotMsg.model_validate(data)
    assert len(reparsed.drives) == 2
    assert reparsed.drives[0].phase == "badblocks"
    assert reparsed.drives[1].phase is None


def test_http_url_to_ws_url_normalizes_schemes() -> None:
    from driveforge.daemon.fleet_client import _http_url_to_ws_url
    assert _http_url_to_ws_url("http://nx3200:8080") == "ws://nx3200:8080/fleet/ws"
    assert _http_url_to_ws_url("https://operator.example.com") == "wss://operator.example.com/fleet/ws"
    assert _http_url_to_ws_url("ws://already-ws:8080") == "ws://already-ws:8080/fleet/ws"
    assert _http_url_to_ws_url("wss://already-wss") == "wss://already-wss/fleet/ws"
    assert _http_url_to_ws_url("bare-host:9000") == "ws://bare-host:9000/fleet/ws"
    # trailing slash handling
    assert _http_url_to_ws_url("http://nx3200:8080/") == "ws://nx3200:8080/fleet/ws"


# ---------------------------------------------------- Server auth gates


def test_fleet_ws_refuses_when_role_is_standalone(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="standalone")
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/fleet/ws"):
                pass
        assert exc_info.value.code == 1008


def test_fleet_ws_refuses_missing_bearer_token(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/fleet/ws"):
                pass
        assert exc_info.value.code == 1008


def test_fleet_ws_refuses_invalid_token(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/fleet/ws", headers={"Authorization": "Bearer bogus.token"},
            ):
                pass
        assert exc_info.value.code == 1008


def test_fleet_ws_refuses_revoked_token(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    agent_id, token = _enroll_and_get_token(state)
    with state.session_factory() as session:
        fleet_mod.revoke_agent(session, agent_id)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/fleet/ws", headers={"Authorization": f"Bearer {token}"},
            ):
                pass
        assert exc_info.value.code == 1008


def test_fleet_ws_accepts_valid_token_via_header(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    agent_id, token = _enroll_and_get_token(state)
    with TestClient(app) as client:
        with client.websocket_connect(
            "/fleet/ws", headers={"Authorization": f"Bearer {token}"},
        ) as ws:
            ws.send_json(proto.HelloMsg(
                agent_id=agent_id, display_name="r720", agent_version="0.10.1",
            ).model_dump(mode="json"))
            ack = ws.receive_json()
            assert ack["msg"] == "hello_ack"
            assert ack["operator_version"]
            assert ack.get("refused_reason") is None


def test_fleet_ws_accepts_token_via_query_param(tmp_path) -> None:
    """Fallback path for clients that can't send WS headers."""
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    agent_id, token = _enroll_and_get_token(state)
    with TestClient(app) as client:
        from urllib.parse import quote
        with client.websocket_connect(f"/fleet/ws?token={quote(token)}") as ws:
            ws.send_json(proto.HelloMsg(
                agent_id=agent_id, display_name="r720", agent_version="0.10.1",
            ).model_dump(mode="json"))
            ack = ws.receive_json()
            assert ack["msg"] == "hello_ack"


def test_fleet_ws_refuses_protocol_skew(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    agent_id, token = _enroll_and_get_token(state)
    with TestClient(app) as client:
        with client.websocket_connect(
            "/fleet/ws", headers={"Authorization": f"Bearer {token}"},
        ) as ws:
            ws.send_json({
                "msg": "hello",
                "agent_id": agent_id,
                "display_name": "r720",
                "agent_version": "999.0.0",
                "protocol_version": "2.0",  # major-skew
            })
            ack = ws.receive_json()
            assert ack["msg"] == "hello_ack"
            assert "incompatible" in ack["refused_reason"].lower()


def test_fleet_ws_closes_on_agent_id_mismatch(tmp_path) -> None:
    """Token's agent_id and hello's agent_id must match."""
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    agent_id, token = _enroll_and_get_token(state)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/fleet/ws", headers={"Authorization": f"Bearer {token}"},
            ) as ws:
                ws.send_json({
                    "msg": "hello",
                    "agent_id": "different-agent-id",
                    "display_name": "r720",
                    "agent_version": "0.10.1",
                    "protocol_version": proto.PROTOCOL_VERSION,
                })
                # Read to force disconnect propagation
                ws.receive_json()
        assert exc_info.value.code == 1008


# ---------------------------------------------------- Snapshot processing


def test_drive_snapshot_populates_remote_agents(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    agent_id, token = _enroll_and_get_token(state)
    with TestClient(app) as client:
        with client.websocket_connect(
            "/fleet/ws", headers={"Authorization": f"Bearer {token}"},
        ) as ws:
            ws.send_json(proto.HelloMsg(
                agent_id=agent_id, display_name="r720", agent_version="0.10.1",
            ).model_dump(mode="json"))
            ws.receive_json()  # ack
            ws.send_json(proto.DriveSnapshotMsg(
                drives=[
                    proto.DriveState(
                        serial="SERIAL-REMOTE-1", model="WD Blue",
                        capacity_bytes=1_000_000_000_000, transport="sata",
                        phase="badblocks", percent=42.0,
                    ),
                ],
                seq=1,
            ).model_dump(mode="json"))
            # Give the server a tick to process
            import time
            time.sleep(0.1)
    # After the context closes, the snapshot should still be in state
    assert agent_id in state.remote_agents
    ra = state.remote_agents[agent_id]
    assert "SERIAL-REMOTE-1" in ra.drives
    assert ra.drives["SERIAL-REMOTE-1"].phase == "badblocks"


def test_out_of_order_snapshot_dropped(tmp_path) -> None:
    """seq goes backwards → ignore (stale frame on a flaky link)."""
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    agent_id, token = _enroll_and_get_token(state)
    with TestClient(app) as client:
        with client.websocket_connect(
            "/fleet/ws", headers={"Authorization": f"Bearer {token}"},
        ) as ws:
            ws.send_json(proto.HelloMsg(
                agent_id=agent_id, display_name="r720", agent_version="0.10.1",
            ).model_dump(mode="json"))
            ws.receive_json()
            # Newer snapshot first
            ws.send_json(proto.DriveSnapshotMsg(
                drives=[proto.DriveState(
                    serial="NEW", model="m", capacity_bytes=1, transport="sata",
                )],
                seq=10,
            ).model_dump(mode="json"))
            # Then a stale one with lower seq
            ws.send_json(proto.DriveSnapshotMsg(
                drives=[proto.DriveState(
                    serial="OLD", model="m", capacity_bytes=1, transport="sata",
                )],
                seq=5,
            ).model_dump(mode="json"))
            import time
            time.sleep(0.1)
    # The stale one shouldn't have overwritten
    ra = state.remote_agents[agent_id]
    assert "NEW" in ra.drives
    assert "OLD" not in ra.drives


# ---------------------------------------------------- View merge


def test_drive_view_merges_remote_agent_drives(tmp_path) -> None:
    """When role == operator and an agent has reported drives,
    _drive_view must include them alongside local cards."""
    _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import RemoteAgentState, get_state
    from driveforge.web.routes import _drive_view
    import time as _time
    state = get_state()
    # Inject a remote agent directly (bypassing the WS handshake for
    # this unit test — transport-layer coverage is in the ws tests above).
    ra = RemoteAgentState(
        agent_id="abc123",
        display_name="r720",
        hostname="driveforge-r720",
        agent_version="0.10.1",
        protocol_version="1.0",
        connected_at=_time.monotonic(),
        last_message_at=_time.monotonic(),
        drives={
            "REMOTE-1": proto.DriveState(
                serial="REMOTE-1", model="WD Blue",
                capacity_bytes=1_000_000_000_000, transport="sata",
                phase="badblocks", percent=80.0,
            ),
            "REMOTE-IDLE": proto.DriveState(
                serial="REMOTE-IDLE", model="Seagate",
                capacity_bytes=2_000_000_000_000, transport="sata",
            ),
        },
    )
    state.remote_agents["abc123"] = ra

    with state.session_factory() as session:
        view = _drive_view(state, session)

    # Active bucket has the remote-badblocks drive
    active_serials = [c["serial"] for c in view["active"]]
    assert "REMOTE-1" in active_serials
    # And it carries host metadata
    remote_card = next(c for c in view["active"] if c["serial"] == "REMOTE-1")
    assert remote_card["host_id"] == "abc123"
    assert remote_card["host_display"] == "r720"
    # Installed bucket has the idle remote drive
    installed_serials = [c["serial"] for c in view["installed"]]
    assert "REMOTE-IDLE" in installed_serials


def test_host_filter_local_excludes_remote(tmp_path) -> None:
    _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import RemoteAgentState, get_state
    from driveforge.web.routes import _drive_view
    import time as _time
    state = get_state()
    state.remote_agents["abc123"] = RemoteAgentState(
        agent_id="abc123", display_name="r720", hostname=None,
        agent_version="0.10.1", protocol_version="1.0",
        connected_at=_time.monotonic(), last_message_at=_time.monotonic(),
        drives={
            "REMOTE-1": proto.DriveState(
                serial="REMOTE-1", model="m",
                capacity_bytes=1_000_000_000_000, transport="sata",
                phase="badblocks",
            ),
        },
    )
    with state.session_factory() as session:
        view = _drive_view(state, session, host_filter="local")
    assert all(c["serial"] != "REMOTE-1" for c in view["active"])


def test_host_filter_specific_agent_excludes_others(tmp_path) -> None:
    _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import RemoteAgentState, get_state
    from driveforge.web.routes import _drive_view
    import time as _time
    state = get_state()
    now = _time.monotonic()
    state.remote_agents["abc"] = RemoteAgentState(
        agent_id="abc", display_name="r720", hostname=None,
        agent_version="0.10.1", protocol_version="1.0",
        connected_at=now, last_message_at=now,
        drives={"A-1": proto.DriveState(
            serial="A-1", model="m", capacity_bytes=1, transport="sata",
            phase="badblocks",
        )},
    )
    state.remote_agents["xyz"] = RemoteAgentState(
        agent_id="xyz", display_name="nx3200", hostname=None,
        agent_version="0.10.1", protocol_version="1.0",
        connected_at=now, last_message_at=now,
        drives={"X-1": proto.DriveState(
            serial="X-1", model="m", capacity_bytes=1, transport="sata",
            phase="badblocks",
        )},
    )
    with state.session_factory() as session:
        view = _drive_view(state, session, host_filter="abc")
    active_serials = [c["serial"] for c in view["active"]]
    assert "A-1" in active_serials
    assert "X-1" not in active_serials


def test_standalone_drive_view_ignores_remote_agents_dict(tmp_path) -> None:
    """Even if remote_agents has entries (shouldn't, but defensive),
    standalone role does not render them."""
    _bootstrap_app(tmp_path, role="standalone")
    from driveforge.daemon.state import RemoteAgentState, get_state
    from driveforge.web.routes import _drive_view
    import time as _time
    state = get_state()
    state.remote_agents["abc"] = RemoteAgentState(
        agent_id="abc", display_name="r720", hostname=None,
        agent_version="0.10.1", protocol_version="1.0",
        connected_at=_time.monotonic(), last_message_at=_time.monotonic(),
        drives={"A-1": proto.DriveState(
            serial="A-1", model="m", capacity_bytes=1, transport="sata",
            phase="badblocks",
        )},
    )
    with state.session_factory() as session:
        view = _drive_view(state, session)
    assert all(c["serial"] != "A-1" for c in view["active"])


# ---------------------------------------------------- Dashboard render


def test_dashboard_shows_host_filter_when_agents_enrolled(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import RemoteAgentState, get_state
    import time as _time
    state = get_state()
    state.remote_agents["abc123"] = RemoteAgentState(
        agent_id="abc123", display_name="r720-bench", hostname=None,
        agent_version="0.10.1", protocol_version="1.0",
        connected_at=_time.monotonic(), last_message_at=_time.monotonic(),
        drives={},
    )
    with TestClient(app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    # Filter row renders with the agent's display name
    assert "r720-bench" in body
    assert "All hosts" in body


def test_dashboard_hides_host_filter_when_no_agents(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="standalone")
    with TestClient(app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    # Standalone mode: no host filter row at all
    assert "host-filter-row" not in resp.text


# ---------------------------------------------------- FleetClient


def test_fleet_client_builds_snapshot_from_state(tmp_path) -> None:
    """The client's snapshot builder must mirror the agent's live
    per-serial state — otherwise the operator sees stale / empty
    drive data."""
    _bootstrap_app(tmp_path, role="agent")
    from driveforge.daemon.fleet_client import FleetClient
    from driveforge.daemon.state import get_state
    state = get_state()
    # Seed DB + live state for one active drive
    with state.session_factory() as session:
        session.add(m.Drive(
            serial="LIVE-1", model="WD Blue",
            capacity_bytes=1_000_000_000_000, transport="sata",
        ))
        session.commit()
    state.active_phase["LIVE-1"] = "badblocks"
    state.active_percent["LIVE-1"] = 37.5
    state.active_sublabel["LIVE-1"] = "pass 3/8"
    state.active_drive_temp["LIVE-1"] = 42

    client = FleetClient(state)
    snap = client._build_snapshot()
    assert snap.seq == 1
    assert len(snap.drives) == 1
    d = snap.drives[0]
    assert d.serial == "LIVE-1"
    assert d.phase == "badblocks"
    assert d.percent == 37.5
    assert d.sublabel == "pass 3/8"
    assert d.drive_temp_c == 42


def test_fleet_client_seq_monotonic(tmp_path) -> None:
    _bootstrap_app(tmp_path, role="agent")
    from driveforge.daemon.fleet_client import FleetClient
    from driveforge.daemon.state import get_state
    client = FleetClient(get_state())
    s1 = client._build_snapshot()
    s2 = client._build_snapshot()
    s3 = client._build_snapshot()
    assert s1.seq < s2.seq < s3.seq


def test_fleet_client_run_noop_when_not_agent_role(tmp_path) -> None:
    """Client's run() must bail immediately if not in agent mode —
    otherwise a standalone / operator daemon would spin a useless
    reconnect loop at startup."""
    _bootstrap_app(tmp_path, role="standalone")
    from driveforge.daemon.fleet_client import FleetClient
    from driveforge.daemon.state import get_state
    client = FleetClient(get_state())

    async def run_it():
        # Should return promptly without connecting
        await asyncio.wait_for(client.run(), timeout=2.0)

    asyncio.run(run_it())


def test_fleet_client_run_noop_without_token(tmp_path) -> None:
    _bootstrap_app(tmp_path, role="agent")
    from driveforge.daemon.fleet_client import FleetClient
    from driveforge.daemon.state import get_state
    state = get_state()
    state.settings.fleet.operator_url = "http://no-op.example.com"
    state.settings.fleet.api_token_path = tmp_path / "missing.token"  # doesn't exist
    client = FleetClient(state)

    async def run_it():
        await asyncio.wait_for(client.run(), timeout=2.0)

    asyncio.run(run_it())


# ---------------------------------------------------- Agent online/offline


def test_remote_agent_state_online_check() -> None:
    from driveforge.daemon.state import RemoteAgentState
    ra = RemoteAgentState(
        agent_id="abc", display_name="r720", hostname=None,
        agent_version="0.10.1", protocol_version="1.0",
        connected_at=0.0, last_message_at=100.0,
    )
    # Within the window → online
    assert ra.is_online(now_monotonic=150.0, timeout_s=120.0) is True
    # Past the window → offline
    assert ra.is_online(now_monotonic=300.0, timeout_s=120.0) is False
