"""v0.10.2 — operator → agent remote commands.

Covers:
  - find_agent_for_serial resolves correctly across multiple agents
  - send_command_to_agent enqueues JSON on the agent's outbound queue
  - send_command_to_agent raises CommandDispatchError when no queue
  - CommandResultMsg roundtrip + recording on RemoteAgentState
  - drain_command_failures pops only failures, leaves successes
  - POST /drives/<serial>/abort forwards to agent when remote
  - POST /drives/<serial>/identify toggles on→off using snapshot
  - POST /drives/<serial>/regrade forwards to agent when remote
  - POST /batches/new splits selection into local + remote dispatch
  - Client-side command dispatcher calls orchestrator correctly
  - Client returns CommandResultMsg success/failure appropriately
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from driveforge import config as cfg
from driveforge.core import fleet as fleet_mod
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


def _seed_agent_with_drive(state, agent_id: str, display_name: str, *drives) -> None:
    """Wire a fake agent into state.remote_agents for view + POST tests."""
    import time as _time
    from driveforge.daemon.state import RemoteAgentState
    ra = RemoteAgentState(
        agent_id=agent_id, display_name=display_name, hostname=None,
        agent_version="0.10.2", protocol_version="1.0",
        connected_at=_time.monotonic(), last_message_at=_time.monotonic(),
        drives={d.serial: d for d in drives},
        outbound_queue=asyncio.Queue(maxsize=256),
    )
    state.remote_agents[agent_id] = ra
    return ra


# ---------------------------------------------------- helpers


def test_find_agent_for_serial_across_multiple_agents(tmp_path) -> None:
    _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    from driveforge.daemon.fleet_server import find_agent_for_serial
    state = get_state()
    _seed_agent_with_drive(
        state, "agent-a", "r720",
        proto.DriveState(serial="S1", model="m", capacity_bytes=1, transport="sata"),
    )
    _seed_agent_with_drive(
        state, "agent-b", "nx3200",
        proto.DriveState(serial="S2", model="m", capacity_bytes=1, transport="sata"),
    )
    assert find_agent_for_serial(state, "S1") == "agent-a"
    assert find_agent_for_serial(state, "S2") == "agent-b"
    assert find_agent_for_serial(state, "S-nowhere") is None


def test_send_command_to_agent_enqueues_json(tmp_path) -> None:
    _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    from driveforge.daemon.fleet_server import send_command_to_agent
    state = get_state()
    _seed_agent_with_drive(state, "abc", "r720")

    async def run_it():
        cmd = proto.AbortCmd(cmd_id="c1", serial="S1")
        await send_command_to_agent(state, "abc", cmd)
        ra = state.remote_agents["abc"]
        payload = ra.outbound_queue.get_nowait()
        body = json.loads(payload)
        assert body["msg"] == "abort"
        assert body["cmd_id"] == "c1"
        assert body["serial"] == "S1"

    asyncio.run(run_it())


def test_send_command_to_unknown_agent_raises(tmp_path) -> None:
    _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    from driveforge.daemon.fleet_server import send_command_to_agent, CommandDispatchError
    state = get_state()

    async def run_it():
        cmd = proto.AbortCmd(cmd_id="c1", serial="S1")
        with pytest.raises(CommandDispatchError):
            await send_command_to_agent(state, "no-such-agent", cmd)

    asyncio.run(run_it())


def test_send_command_when_agent_has_no_queue_raises(tmp_path) -> None:
    """Session mid-handshake edge case — agent in remote_agents but
    outbound_queue not yet set up."""
    _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import RemoteAgentState, get_state
    from driveforge.daemon.fleet_server import send_command_to_agent, CommandDispatchError
    state = get_state()
    import time as _time
    state.remote_agents["bare"] = RemoteAgentState(
        agent_id="bare", display_name="r720", hostname=None,
        agent_version="0.10.2", protocol_version="1.0",
        connected_at=_time.monotonic(), last_message_at=_time.monotonic(),
        drives={}, outbound_queue=None,
    )

    async def run_it():
        cmd = proto.AbortCmd(cmd_id="c1", serial="S1")
        with pytest.raises(CommandDispatchError):
            await send_command_to_agent(state, "bare", cmd)

    asyncio.run(run_it())


# ---------------------------------------------------- Dashboard routing


def test_abort_remote_drive_forwards_to_agent(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    ra = _seed_agent_with_drive(
        state, "abc", "r720",
        proto.DriveState(
            serial="REMOTE-1", model="m", capacity_bytes=1, transport="sata",
            phase="badblocks",
        ),
    )
    with TestClient(app) as client:
        resp = client.post(
            "/drives/REMOTE-1/abort", follow_redirects=False,
        )
    assert resp.status_code == 303
    # Queue should have an AbortCmd
    assert ra.outbound_queue.qsize() == 1
    payload = ra.outbound_queue.get_nowait()
    body = json.loads(payload)
    assert body["msg"] == "abort"
    assert body["serial"] == "REMOTE-1"


def test_identify_remote_drive_forwards_correct_toggle(tmp_path) -> None:
    """When the drive's current snapshot says identifying=False, the
    click should send IdentifyCmd(on=True). When identifying=True, on=False."""
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    # Not currently identifying → click should start it
    ra = _seed_agent_with_drive(
        state, "abc", "r720",
        proto.DriveState(
            serial="REMOTE-1", model="m", capacity_bytes=1, transport="sata",
            identifying=False,
        ),
    )
    with TestClient(app) as client:
        client.post("/drives/REMOTE-1/identify", follow_redirects=False)
    payload = ra.outbound_queue.get_nowait()
    body = json.loads(payload)
    assert body["msg"] == "identify"
    assert body["on"] is True

    # Flip snapshot bit → click should stop it
    ra.drives["REMOTE-1"] = proto.DriveState(
        serial="REMOTE-1", model="m", capacity_bytes=1, transport="sata",
        identifying=True,
    )
    with TestClient(app) as client:
        client.post("/drives/REMOTE-1/identify", follow_redirects=False)
    payload = ra.outbound_queue.get_nowait()
    body = json.loads(payload)
    assert body["on"] is False


def test_regrade_remote_drive_forwards_to_agent(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    ra = _seed_agent_with_drive(
        state, "abc", "r720",
        proto.DriveState(
            serial="REMOTE-1", model="m", capacity_bytes=1, transport="sata",
        ),
    )
    with TestClient(app) as client:
        resp = client.post("/drives/REMOTE-1/regrade", follow_redirects=False)
    assert resp.status_code == 303
    # Redirect URL carries a "forwarded" flash
    assert "regrade_forwarded" in resp.headers["location"]
    payload = ra.outbound_queue.get_nowait()
    body = json.loads(payload)
    assert body["msg"] == "regrade"
    assert body["serial"] == "REMOTE-1"


def test_abort_unknown_serial_local_path_noop(tmp_path) -> None:
    """Non-existent serial on standalone should still hit the local
    abort code without errors (returns 'not_active')."""
    app = _bootstrap_app(tmp_path, role="standalone")
    with TestClient(app) as client:
        resp = client.post("/drives/UNKNOWN/abort", follow_redirects=False)
    assert resp.status_code == 303


# ---------------------------------------------------- command_result flash


def test_dashboard_drains_and_renders_command_failures(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    from driveforge.daemon.fleet_server import _record_command_result
    state = get_state()
    _seed_agent_with_drive(state, "abc", "r720-bench")
    # Record one failure + one success — only failure should flash
    _record_command_result(state, "abc", proto.CommandResultMsg(
        cmd_id="c1", command="abort", success=False,
        detail="drive in secure_erase phase",
    ))
    _record_command_result(state, "abc", proto.CommandResultMsg(
        cmd_id="c2", command="identify", success=True,
    ))
    with TestClient(app) as client:
        resp = client.get("/")
    body = resp.text
    assert "secure_erase phase" in body
    assert "r720-bench" in body
    # Second render: failure drained, shouldn't re-flash
    with TestClient(app) as client:
        resp2 = client.get("/")
    assert "secure_erase phase" not in resp2.text


def test_successful_command_result_does_not_flash(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    from driveforge.daemon.fleet_server import _record_command_result
    state = get_state()
    _seed_agent_with_drive(state, "abc", "r720")
    _record_command_result(state, "abc", proto.CommandResultMsg(
        cmd_id="c1", command="abort", success=True, detail="ok",
    ))
    with TestClient(app) as client:
        resp = client.get("/")
    # Successful detail shouldn't appear as a warn banner
    assert "warn-banner" not in resp.text or "command" not in resp.text.lower() or True
    # Explicit: the specific detail string isn't in the body
    assert "warn-banner" not in resp.text


# ---------------------------------------------------- Agent-side dispatch


def test_fleet_client_dispatch_abort_calls_orchestrator(tmp_path) -> None:
    """The client's _apply_command must invoke orch.abort_drive
    and report the outcome back as a CommandResultMsg."""
    _bootstrap_app(tmp_path, role="agent")
    from driveforge.daemon.fleet_client import FleetClient
    from driveforge.daemon.state import get_state
    state = get_state()
    # Install a fake orchestrator with an abort_drive that returns the
    # v0.7.0-shaped outcome dict.
    fake_orch = MagicMock()
    fake_orch.abort_drive = AsyncMock(return_value={
        "status": "aborted", "killed": 1, "phase": "badblocks", "note": "ok",
    })
    state.orchestrator = fake_orch

    client = FleetClient(state)

    async def run_it():
        cmd_id, success, detail = await client._apply_command(
            "abort",
            proto.AbortCmd(cmd_id="c1", serial="SN-1").model_dump(mode="json"),
        )
        assert cmd_id == "c1"
        assert success is True
        fake_orch.abort_drive.assert_awaited_once_with("SN-1")

    asyncio.run(run_it())


def test_fleet_client_dispatch_abort_reports_failure(tmp_path) -> None:
    _bootstrap_app(tmp_path, role="agent")
    from driveforge.daemon.fleet_client import FleetClient
    from driveforge.daemon.state import get_state
    state = get_state()
    fake_orch = MagicMock()
    fake_orch.abort_drive = AsyncMock(return_value={
        "status": "not_active", "note": "drive not in active_phase",
    })
    state.orchestrator = fake_orch
    client = FleetClient(state)

    async def run_it():
        _, success, detail = await client._apply_command(
            "abort",
            proto.AbortCmd(cmd_id="c1", serial="SN-1").model_dump(mode="json"),
        )
        assert success is False
        assert "not_active" in detail

    asyncio.run(run_it())


def test_fleet_client_dispatch_identify_off_calls_stop(tmp_path) -> None:
    _bootstrap_app(tmp_path, role="agent")
    from driveforge.daemon.fleet_client import FleetClient
    from driveforge.daemon.state import get_state
    state = get_state()
    fake_orch = MagicMock()
    fake_orch.stop_identify = MagicMock(return_value=True)
    state.orchestrator = fake_orch
    client = FleetClient(state)

    async def run_it():
        _, success, detail = await client._apply_command(
            "identify",
            proto.IdentifyCmd(cmd_id="c1", serial="SN", on=False).model_dump(mode="json"),
        )
        assert success is True
        fake_orch.stop_identify.assert_called_once_with("SN")

    asyncio.run(run_it())


def test_fleet_client_dispatch_returns_failure_when_no_orch(tmp_path) -> None:
    _bootstrap_app(tmp_path, role="agent")
    from driveforge.daemon.fleet_client import FleetClient
    from driveforge.daemon.state import get_state
    state = get_state()
    # No orchestrator attached (startup race)
    assert not hasattr(state, "orchestrator") or getattr(state, "orchestrator", None) is None
    client = FleetClient(state)

    async def run_it():
        cmd_id, success, detail = await client._apply_command(
            "abort",
            proto.AbortCmd(cmd_id="c1", serial="SN").model_dump(mode="json"),
        )
        assert cmd_id == "c1"
        assert success is False
        assert "orchestrator" in detail.lower()

    asyncio.run(run_it())


def test_fleet_client_dispatch_unknown_command(tmp_path) -> None:
    _bootstrap_app(tmp_path, role="agent")
    from driveforge.daemon.fleet_client import FleetClient
    from driveforge.daemon.state import get_state
    state = get_state()
    state.orchestrator = MagicMock()
    client = FleetClient(state)

    async def run_it():
        cmd_id, success, detail = await client._apply_command(
            "telepathy",
            {"cmd_id": "c1"},
        )
        assert success is False
        assert "unknown" in detail.lower()

    asyncio.run(run_it())


# ---------------------------------------------------- New batch fan-out


def test_new_batch_splits_selection_into_local_and_remote(tmp_path, monkeypatch) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    from driveforge.core import drive as drive_mod_
    state = get_state()
    # Seed one remote drive on an agent
    ra = _seed_agent_with_drive(
        state, "abc", "r720",
        proto.DriveState(serial="REMOTE-1", model="m", capacity_bytes=1, transport="sata"),
    )
    # Stub discover() to not return REMOTE-1 locally (obvious) but
    # also not return anything so the "empty selected → run all local"
    # branch doesn't overflow
    monkeypatch.setattr(drive_mod_, "discover", lambda: [])
    # Stub orch.start_batch so local side is inert
    fake_orch = MagicMock()
    fake_orch.start_batch = AsyncMock()
    app.state.orchestrator = fake_orch

    with TestClient(app) as client:
        resp = client.post(
            "/batches/new",
            data={"drive": ["REMOTE-1"], "quick": "on", "confirm": "ERASE"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    # The remote command should be enqueued
    assert ra.outbound_queue.qsize() == 1
    payload = ra.outbound_queue.get_nowait()
    body = json.loads(payload)
    assert body["msg"] == "start_pipeline"
    assert body["serial"] == "REMOTE-1"
    assert body["quick_mode"] is True


# ---------------------------------------------------- regrade protocol only


def test_regrade_cmd_model_validation() -> None:
    cmd = proto.RegradeCmd(cmd_id="c1", serial="SN-1")
    data = cmd.model_dump(mode="json")
    reparsed = proto.RegradeCmd.model_validate(data)
    assert reparsed.cmd_id == "c1"
    assert reparsed.serial == "SN-1"


def test_command_result_roundtrip() -> None:
    msg = proto.CommandResultMsg(
        cmd_id="c1", command="abort", success=False, detail="no such drive",
    )
    data = msg.model_dump(mode="json")
    reparsed = proto.CommandResultMsg.model_validate(data)
    assert reparsed.success is False
    assert reparsed.detail == "no such drive"
