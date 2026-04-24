"""v0.11.7 — batch creation form sees fleet drives.

Pre-v0.11.7, GET /batches/new only enumerated `drive_mod.discover()`,
so when the operator opened "Start a new batch" the agent's drives
were invisible — no checkboxes, no way to dispatch. The POST handler
HAD already been fleet-aware since v0.10.2 (per-serial routing via
`fleet_server.find_agent_for_serial`), but with the form blind to
remote drives, that path was unreachable from the UI.

JT caught this on the NX-3200 operator: the form showed only the
operator's local Intel SSD; the R720 agent's drive was missing.

v0.11.7:
  - Operator-role daemons merge `state.remote_agents[*].drives` into
    the form's drive list.
  - Each row carries `host_display` (and `host_offline`) for a Host
    column.
  - Remote drives mid-pipeline render disabled (mirrors local-busy).
  - Remote drives whose agent is offline render disabled with an
    "offline" status pill.
  - Standalone-role form keeps the prior shape (no Host column).

Tests:
  - Standalone: only local rows, no Host column header
  - Operator + no agents: local rows + "this operator" host badge
  - Operator + online agent: local + remote rows, both pickable
  - Operator + agent w/ active drive: remote row disabled with
    "testing" pill, still listed
  - Operator + offline agent: remote row disabled with "offline" pill
  - Operator + multiple agents: all drives listed with correct
    host badges
"""

from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

from driveforge import config as cfg
from driveforge.core import fleet_protocol as proto


# ---------------------------------------------------- bootstrap


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


def _seed_local_drive(monkeypatch, serial="LOCAL-1", model="INTEL SSDSC2BB120G4"):
    from driveforge.core import drive as drive_mod_
    from driveforge.core.drive import Drive, Transport
    d = Drive(
        serial=serial,
        model=model,
        capacity_bytes=120_034_123_776,
        device_path="/dev/sda",
        transport=Transport.SATA,
    )
    monkeypatch.setattr(drive_mod_, "discover", lambda: [d])
    return d


def _seed_no_local(monkeypatch):
    from driveforge.core import drive as drive_mod_
    monkeypatch.setattr(drive_mod_, "discover", lambda: [])


def _seed_agent(
    state,
    agent_id: str,
    display_name: str,
    *drives: proto.DriveState,
    last_seen_offset: float = 0.0,
):
    """Add an agent to the operator's fleet view. last_seen_offset
    in seconds — pass a large positive value (e.g. 200) to make the
    agent appear offline (RemoteAgentState.is_online uses 120s)."""
    from driveforge.daemon.state import RemoteAgentState
    now = time.monotonic()
    ra = RemoteAgentState(
        agent_id=agent_id,
        display_name=display_name,
        hostname=None,
        agent_version="0.11.7",
        protocol_version="1.0",
        connected_at=now - 60,
        last_message_at=now - last_seen_offset,
        drives={d.serial: d for d in drives},
        outbound_queue=asyncio.Queue(maxsize=16),
    )
    state.remote_agents[agent_id] = ra
    return ra


# ---------------------------------------------------- standalone


def test_standalone_form_shows_only_local_no_host_column(tmp_path, monkeypatch) -> None:
    app = _bootstrap_app(tmp_path, role="standalone")
    _seed_local_drive(monkeypatch, serial="LOCAL-1")
    with TestClient(app) as client:
        resp = client.get("/batches/new")
    assert resp.status_code == 200
    body = resp.text
    assert "LOCAL-1" in body
    # No Host column header in standalone mode — keeps the form
    # uncluttered for single-box installs.
    assert "<th>Host</th>" not in body
    assert "this operator" not in body


# ---------------------------------------------------- operator + no agents


def test_operator_no_agents_shows_host_column_and_self_label(tmp_path, monkeypatch) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    _seed_local_drive(monkeypatch, serial="LOCAL-1")
    with TestClient(app) as client:
        resp = client.get("/batches/new")
    assert resp.status_code == 200
    body = resp.text
    assert "LOCAL-1" in body
    assert "<th>Host</th>" in body
    # Operator's own drives are tagged "this operator" so the
    # operator can tell at a glance which row is local vs. agent.
    assert "this operator" in body


# ---------------------------------------------------- operator + online agent


def test_operator_with_online_agent_lists_remote_drives(tmp_path, monkeypatch) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    _seed_local_drive(monkeypatch, serial="LOCAL-1")
    from driveforge.daemon.state import get_state
    _seed_agent(
        get_state(), "agent-r720", "r720",
        proto.DriveState(
            serial="REMOTE-1", model="WDC WD1000CHTZ",
            capacity_bytes=1_000_204_886_016, transport="sata",
        ),
    )
    with TestClient(app) as client:
        resp = client.get("/batches/new")
    body = resp.text
    assert "LOCAL-1" in body
    assert "REMOTE-1" in body
    assert "WDC WD1000CHTZ" in body
    # Remote row carries the agent's display name in a host badge.
    assert "r720" in body
    # Remote drive is idle → checkbox is checked, not disabled.
    # Easiest assertion: a checkbox with value="REMOTE-1" exists and
    # is NOT marked disabled.
    assert 'value="REMOTE-1" checked' in body


def test_operator_remote_drive_in_pipeline_renders_disabled(tmp_path, monkeypatch) -> None:
    """A remote drive whose snapshot reports `phase` is mid-pipeline
    on the agent — the operator can't dispatch a second batch on it.
    Mirrors how local-busy drives are rendered."""
    app = _bootstrap_app(tmp_path, role="operator")
    _seed_no_local(monkeypatch)
    from driveforge.daemon.state import get_state
    _seed_agent(
        get_state(), "agent-r720", "r720",
        proto.DriveState(
            serial="REMOTE-BUSY", model="ST3000DM001",
            capacity_bytes=3_000_592_982_016, transport="sata",
            phase="badblocks", percent=42.0,
        ),
    )
    with TestClient(app) as client:
        resp = client.get("/batches/new")
    body = resp.text
    assert "REMOTE-BUSY" in body
    # Disabled checkbox — not selectable.
    assert 'value="REMOTE-BUSY" checked' not in body
    assert "disabled" in body
    # "testing" pill on the status column.
    assert "testing" in body


def test_operator_offline_agent_drive_marked_offline(tmp_path, monkeypatch) -> None:
    """Agent's last_message_at is older than the 120s online window.
    Drive still appears (operator can see it exists) but checkbox
    is disabled and status pill says "offline" so the operator knows
    why they can't dispatch."""
    app = _bootstrap_app(tmp_path, role="operator")
    _seed_no_local(monkeypatch)
    from driveforge.daemon.state import get_state
    _seed_agent(
        get_state(), "agent-r720", "r720",
        proto.DriveState(
            serial="REMOTE-DARK", model="WD Blue",
            capacity_bytes=1_000_204_886_016, transport="sata",
        ),
        last_seen_offset=300.0,  # 5 min ago → offline (>120s)
    )
    with TestClient(app) as client:
        resp = client.get("/batches/new")
    body = resp.text
    assert "REMOTE-DARK" in body
    assert 'value="REMOTE-DARK" checked' not in body
    # Offline pill rendered with our v0.11.7 .pill.warn class.
    assert "offline" in body
    # Host dot has the offline modifier so the badge renders muted.
    assert "host-dot--offline" in body


def test_operator_multiple_agents_all_drives_listed(tmp_path, monkeypatch) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    _seed_local_drive(monkeypatch, serial="LOCAL-1")
    from driveforge.daemon.state import get_state
    state = get_state()
    _seed_agent(
        state, "agent-a", "r720",
        proto.DriveState(
            serial="AGENT-A-1", model="WDC WD1000",
            capacity_bytes=1_000_204_886_016, transport="sata",
        ),
    )
    _seed_agent(
        state, "agent-b", "r430",
        proto.DriveState(
            serial="AGENT-B-1", model="ST3000",
            capacity_bytes=3_000_592_982_016, transport="sas",
        ),
        proto.DriveState(
            serial="AGENT-B-2", model="ST3000",
            capacity_bytes=3_000_592_982_016, transport="sas",
        ),
    )
    with TestClient(app) as client:
        resp = client.get("/batches/new")
    body = resp.text
    for serial in ("LOCAL-1", "AGENT-A-1", "AGENT-B-1", "AGENT-B-2"):
        assert serial in body, f"missing {serial}"
    assert "r720" in body
    assert "r430" in body
    # Discovered count in the page subtitle: 1 local + 3 remote = 4.
    assert "Discovered 4 drives" in body


def test_operator_form_remote_drive_serial_routes_via_post(tmp_path, monkeypatch) -> None:
    """End-to-end: GET form lists remote drive, POST with that serial
    forwards to the agent (StartPipelineCmd lands in the agent's
    outbound queue)."""
    import json
    app = _bootstrap_app(tmp_path, role="operator")
    _seed_no_local(monkeypatch)
    from driveforge.daemon.state import get_state
    state = get_state()
    _seed_agent(
        state, "agent-r720", "r720",
        proto.DriveState(
            serial="REMOTE-1", model="WDC WD1000",
            capacity_bytes=1_000_204_886_016, transport="sata",
        ),
    )
    with TestClient(app) as client:
        # Sanity: form lists it.
        get_resp = client.get("/batches/new")
        assert "REMOTE-1" in get_resp.text
        # Submit batch with that serial.
        post_resp = client.post(
            "/batches/new",
            data={"drive": "REMOTE-1", "confirm": "ERASE"},
            follow_redirects=False,
        )
    assert post_resp.status_code == 303
    # Agent's outbound queue should have one StartPipelineCmd.
    ra = state.remote_agents["agent-r720"]
    payload = ra.outbound_queue.get_nowait()
    body = json.loads(payload)
    assert body["msg"] == "start_pipeline"
    assert body["serial"] == "REMOTE-1"
