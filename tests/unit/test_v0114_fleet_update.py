"""v0.11.4 — coupled fleet-wide update.

Click Install Update on the operator → operator first broadcasts
UpdateCmd to every connected agent (in parallel), THEN triggers
its own update. Agents update independently via their existing
polkit-authorized driveforge-update.service unit. Net effect:
single-button fleet-wide upgrade with no operator/agent version
skew.

Tests:
  - UpdateCmd protocol roundtrip
  - Agent dispatch fires updates.trigger_in_app_update locally
  - Agent reports success/failure via CommandResultMsg
  - Operator's POST /settings/install-update broadcasts UpdateCmd
    to all online agents before triggering its own update
  - Offline agents are skipped without error
  - fleet_pushed query param surfaces count
  - Standalone install-update doesn't try to broadcast
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

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


# ---------------------------------------------------- Protocol


def test_update_cmd_roundtrip() -> None:
    cmd = proto.UpdateCmd(cmd_id="c1")
    data = cmd.model_dump(mode="json")
    assert data["msg"] == "update"
    reparsed = proto.UpdateCmd.model_validate(data)
    assert reparsed.cmd_id == "c1"


# ---------------------------------------------------- Agent dispatch


def test_agent_dispatch_update_fires_local_trigger(tmp_path, monkeypatch) -> None:
    """Agent receives UpdateCmd → calls updates.trigger_in_app_update.
    Returns success+message in CommandResultMsg."""
    _bootstrap_app(tmp_path, role="agent")
    from driveforge.daemon.fleet_client import FleetClient
    from driveforge.daemon.state import get_state
    from driveforge.core import updates as updates_mod
    state = get_state()

    calls = []

    def fake_trigger():
        calls.append("triggered")
        return (True, "driveforge-update.service started; live log streaming below.")

    monkeypatch.setattr(updates_mod, "trigger_in_app_update", fake_trigger)

    client = FleetClient(state)

    async def run_it():
        cmd_id, success, detail = await client._apply_command(
            "update", proto.UpdateCmd(cmd_id="up1").model_dump(mode="json"),
        )
        assert cmd_id == "up1"
        assert success is True
        assert "started" in detail.lower()
        assert calls == ["triggered"]

    asyncio.run(run_it())


def test_agent_dispatch_update_reports_failure(tmp_path, monkeypatch) -> None:
    """If the local update trigger returns failure (polkit refused,
    etc.), the agent reports success=False with the reason."""
    _bootstrap_app(tmp_path, role="agent")
    from driveforge.daemon.fleet_client import FleetClient
    from driveforge.daemon.state import get_state
    from driveforge.core import updates as updates_mod
    state = get_state()

    monkeypatch.setattr(
        updates_mod, "trigger_in_app_update",
        lambda: (False, "Interactive authentication required"),
    )

    client = FleetClient(state)

    async def run_it():
        cmd_id, success, detail = await client._apply_command(
            "update", proto.UpdateCmd(cmd_id="up2").model_dump(mode="json"),
        )
        assert success is False
        assert "authentication" in detail.lower()

    asyncio.run(run_it())


# ---------------------------------------------------- Operator broadcast


def test_install_update_broadcasts_to_agents_then_self(tmp_path, monkeypatch) -> None:
    """Operator's Install Update click pushes UpdateCmd to every
    connected agent BEFORE firing its own update."""
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import RemoteAgentState, get_state
    from driveforge.core import updates as updates_mod
    state = get_state()

    # Two enrolled, online agents
    for aid in ("agent-a", "agent-b"):
        state.remote_agents[aid] = RemoteAgentState(
            agent_id=aid, display_name=aid, hostname=None,
            agent_version="0.11.3", protocol_version="1.0",
            connected_at=time.monotonic(), last_message_at=time.monotonic(),
            drives={}, outbound_queue=asyncio.Queue(maxsize=16),
        )

    # Stub the operator's own update trigger
    monkeypatch.setattr(
        updates_mod, "trigger_in_app_update",
        lambda: (True, "started"),
    )

    with TestClient(app) as client:
        resp = client.post("/settings/install-update", follow_redirects=False)
    assert resp.status_code == 303
    # URL carries the install_started + fleet_pushed annotations
    location = resp.headers["location"]
    assert "install_started=1" in location
    assert "fleet_pushed=2" in location

    # Each agent's outbound queue should have an UpdateCmd
    for aid in ("agent-a", "agent-b"):
        q = state.remote_agents[aid].outbound_queue
        assert q.qsize() == 1
        body = json.loads(q.get_nowait())
        assert body["msg"] == "update"


def test_install_update_skips_offline_agents(tmp_path, monkeypatch) -> None:
    """Agents with outbound_queue=None (no active session) get
    skipped cleanly. fleet_pushed reflects the actual successful
    push count, not the total enrolled count."""
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import RemoteAgentState, get_state
    from driveforge.core import updates as updates_mod
    state = get_state()
    state.remote_agents["online"] = RemoteAgentState(
        agent_id="online", display_name="online", hostname=None,
        agent_version="0.11.3", protocol_version="1.0",
        connected_at=time.monotonic(), last_message_at=time.monotonic(),
        drives={}, outbound_queue=asyncio.Queue(maxsize=16),
    )
    state.remote_agents["offline"] = RemoteAgentState(
        agent_id="offline", display_name="offline", hostname=None,
        agent_version="0.11.3", protocol_version="1.0",
        connected_at=time.monotonic(), last_message_at=time.monotonic(),
        drives={}, outbound_queue=None,  # offline
    )
    monkeypatch.setattr(
        updates_mod, "trigger_in_app_update", lambda: (True, "ok"),
    )
    with TestClient(app) as client:
        resp = client.post("/settings/install-update", follow_redirects=False)
    assert resp.status_code == 303
    assert "fleet_pushed=1" in resp.headers["location"]


def test_install_update_standalone_no_broadcast(tmp_path, monkeypatch) -> None:
    """Standalone daemons skip the broadcast block — there's no
    fleet to push to."""
    app = _bootstrap_app(tmp_path, role="standalone")
    from driveforge.core import updates as updates_mod
    monkeypatch.setattr(
        updates_mod, "trigger_in_app_update", lambda: (True, "ok"),
    )
    with TestClient(app) as client:
        resp = client.post("/settings/install-update", follow_redirects=False)
    assert resp.status_code == 303
    # No fleet_pushed annotation when nothing was pushed
    assert "fleet_pushed=" not in resp.headers["location"]


def test_install_update_blocks_when_local_drives_active(tmp_path, monkeypatch) -> None:
    """Existing safety gate still applies: refuse to update while
    operator's own drives are mid-pipeline. Agents would still get
    the push if we passed the gate, but we don't broadcast on
    refusal — both sides stay on the old version."""
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import RemoteAgentState, get_state
    state = get_state()
    state.active_phase["LOCAL-1"] = "badblocks"
    state.remote_agents["agentA"] = RemoteAgentState(
        agent_id="agentA", display_name="x", hostname=None,
        agent_version="0.11.3", protocol_version="1.0",
        connected_at=time.monotonic(), last_message_at=time.monotonic(),
        drives={}, outbound_queue=asyncio.Queue(maxsize=16),
    )
    with TestClient(app) as client:
        resp = client.post("/settings/install-update", follow_redirects=False)
    assert resp.status_code == 303
    assert "install_error" in resp.headers["location"]
    # Agent's queue was NOT touched
    assert state.remote_agents["agentA"].outbound_queue.qsize() == 0


def test_settings_page_shows_fleet_pushed_banner(tmp_path) -> None:
    """When ?install_started=1&fleet_pushed=N is on the URL, the
    Settings page renders a confirmation noting the count of
    agents that received the push."""
    app = _bootstrap_app(tmp_path, role="operator")
    with TestClient(app) as client:
        resp = client.get("/settings?install_started=1&fleet_pushed=2")
    body = resp.text
    assert "Pushed update to" in body
    assert "<strong>2</strong>" in body


def test_settings_page_no_fleet_banner_for_operator_only_update(tmp_path) -> None:
    """When fleet_pushed is absent or 0, no fleet-banner appears
    (operator-only update — usually because no agents enrolled or
    none currently online)."""
    app = _bootstrap_app(tmp_path, role="operator")
    with TestClient(app) as client:
        resp = client.get("/settings?install_started=1")
    assert "Pushed update to" not in resp.text
