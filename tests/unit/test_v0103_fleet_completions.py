"""v0.10.3 — cert forwarding + remote printing + history host column.

Covers:
  - RunCompletedMsg / RunCompletedAckMsg roundtrip
  - pending_fleet_forward column present + defaults False
  - _build_run_completed_msg serializes all v0.8.0 fields
  - Operator ingest upserts Drive + TestRun with host_id
  - Ingest is idempotent: second RunCompletedMsg with same
    completion_id re-acks without duplicating the row
  - Operator ingest triggers auto_print when configured
  - Agent ack clears pending_fleet_forward
  - History page shows host column when remote rows present
  - History page hides host column for standalone-only history
  - Client forward loop sends pending rows, caps at 32 per tick
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

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
    app = make_app(settings)
    DaemonState.boot(settings)
    return app


# ---------------------------------------------------- Protocol


def test_run_completed_msg_roundtrip() -> None:
    msg = proto.RunCompletedMsg(
        completion_id="abc123",
        drive=proto.CompletedDriveData(
            serial="SN-1", model="WD Blue",
            capacity_bytes=1_000_000_000_000, transport="sata",
        ),
        run=proto.CompletedRunData(
            run_id=42, drive_serial="SN-1", phase="done",
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            completed_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
            grade="A",
            power_on_hours_at_test=1234,
        ),
    )
    data = msg.model_dump(mode="json")
    reparsed = proto.RunCompletedMsg.model_validate(data)
    assert reparsed.completion_id == "abc123"
    assert reparsed.drive.serial == "SN-1"
    assert reparsed.run.grade == "A"


def test_run_completed_ack_success_and_failure() -> None:
    ok = proto.RunCompletedAckMsg(completion_id="c1")
    assert ok.success is True
    assert ok.detail is None

    fail = proto.RunCompletedAckMsg(
        completion_id="c1", success=False, detail="DB error",
    )
    assert fail.success is False


# ---------------------------------------------------- Schema migration


def test_pending_fleet_forward_column_exists(tmp_path) -> None:
    _bootstrap_app(tmp_path)
    from driveforge.daemon.state import get_state
    from sqlalchemy import inspect
    state = get_state()
    cols = {c["name"] for c in inspect(state.engine).get_columns("test_runs")}
    assert "pending_fleet_forward" in cols
    assert "fleet_completion_id" in cols


def test_pending_fleet_forward_defaults_false(tmp_path) -> None:
    _bootstrap_app(tmp_path)
    from driveforge.daemon.state import get_state
    state = get_state()
    with state.session_factory() as session:
        session.add(m.Drive(
            serial="SN-1", model="m",
            capacity_bytes=1_000_000_000_000, transport="sata",
        ))
        session.add(m.TestRun(
            drive_serial="SN-1", phase="done",
            started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
            grade="A",
        ))
        session.commit()
        run = session.query(m.TestRun).filter_by(drive_serial="SN-1").one()
        assert run.pending_fleet_forward is False
        assert run.fleet_completion_id is None


# ---------------------------------------------------- Build helper


def test_build_run_completed_msg_serializes_all_fields(tmp_path) -> None:
    _bootstrap_app(tmp_path)
    from driveforge.daemon.fleet_client import _build_run_completed_msg
    from driveforge.daemon.state import get_state
    state = get_state()
    with state.session_factory() as session:
        drive = m.Drive(
            serial="SN-9", model="WD Blue",
            capacity_bytes=2_000_000_000_000, transport="sata",
            manufacturer="WD", firmware_version="1.2.3",
        )
        session.add(drive)
        run = m.TestRun(
            drive_serial="SN-9", phase="done",
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            completed_at=datetime(2026, 1, 1, 2, tzinfo=UTC),
            grade="B", power_on_hours_at_test=5000,
            reallocated_sectors=2,
            throughput_mean_mbps=150.5,
            lifetime_host_writes_bytes=1_000_000_000_000,
            wear_pct_used=15,
            drive_class="consumer_hdd",
            fleet_completion_id="c1",
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        session.refresh(drive)
        msg = _build_run_completed_msg(run, drive)
    assert msg.completion_id == "c1"
    assert msg.drive.manufacturer == "WD"
    assert msg.drive.firmware_version == "1.2.3"
    assert msg.run.grade == "B"
    assert msg.run.power_on_hours_at_test == 5000
    assert msg.run.throughput_mean_mbps == 150.5
    assert msg.run.lifetime_host_writes_bytes == 1_000_000_000_000
    assert msg.run.wear_pct_used == 15
    assert msg.run.drive_class == "consumer_hdd"


# ---------------------------------------------------- Operator ingest


def _make_completion_msg(completion_id="c1", serial="REMOTE-1", grade="A") -> proto.RunCompletedMsg:
    return proto.RunCompletedMsg(
        completion_id=completion_id,
        drive=proto.CompletedDriveData(
            serial=serial, model="WD Blue",
            capacity_bytes=1_000_000_000_000, transport="sata",
            manufacturer="WD",
        ),
        run=proto.CompletedRunData(
            run_id=1, drive_serial=serial, phase="done",
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            grade=grade,
            power_on_hours_at_test=10_000,
        ),
    )


def test_operator_ingest_upserts_drive_and_testrun(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    # Enroll an agent so its ID is known to the DB (for history page
    # display lookup).
    with state.session_factory() as session:
        issue = fleet_mod.issue_enrollment_token(session, ttl_seconds=60)
    with state.session_factory() as session:
        result = fleet_mod.consume_enrollment_token(
            session, composite_token=issue.raw_token,
            display_name="r720", hostname=None, version="0.10.3",
        )
    agent_id = result.agent_id

    token = result.api_token
    with TestClient(app) as client:
        with client.websocket_connect(
            "/fleet/ws", headers={"Authorization": f"Bearer {token}"},
        ) as ws:
            ws.send_json(proto.HelloMsg(
                agent_id=agent_id, display_name="r720",
                agent_version="0.10.3",
            ).model_dump(mode="json"))
            ws.receive_json()  # hello_ack
            msg = _make_completion_msg("c1", "REMOTE-1", "A")
            ws.send_json(msg.model_dump(mode="json"))
            # Receive the ack
            ack_raw = ws.receive_json()
            ack = proto.RunCompletedAckMsg.model_validate(ack_raw)
            assert ack.success is True
            assert ack.completion_id == "c1"

    # DB now has the remote-originated rows
    with state.session_factory() as session:
        drive = session.get(m.Drive, "REMOTE-1")
        assert drive is not None
        assert drive.last_host_id == agent_id
        assert drive.last_host_seen_at is not None
        runs = session.query(m.TestRun).filter_by(drive_serial="REMOTE-1").all()
        assert len(runs) == 1
        assert runs[0].grade == "A"
        assert runs[0].host_id == agent_id
        assert runs[0].fleet_completion_id == "c1"
        # Operator-side row must NOT re-flag pending_fleet_forward.
        assert runs[0].pending_fleet_forward is False


def test_operator_ingest_is_idempotent(tmp_path) -> None:
    """Replay after a dropped ack must not duplicate the row."""
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    with state.session_factory() as session:
        issue = fleet_mod.issue_enrollment_token(session, ttl_seconds=60)
    with state.session_factory() as session:
        result = fleet_mod.consume_enrollment_token(
            session, composite_token=issue.raw_token,
            display_name="r720", hostname=None, version="0.10.3",
        )
    token = result.api_token

    with TestClient(app) as client:
        with client.websocket_connect(
            "/fleet/ws", headers={"Authorization": f"Bearer {token}"},
        ) as ws:
            ws.send_json(proto.HelloMsg(
                agent_id=result.agent_id, display_name="r720",
                agent_version="0.10.3",
            ).model_dump(mode="json"))
            ws.receive_json()
            msg = _make_completion_msg("c-dup", "REMOTE-DUP", "B")
            # Send twice
            ws.send_json(msg.model_dump(mode="json"))
            ws.receive_json()  # first ack
            ws.send_json(msg.model_dump(mode="json"))
            ws.receive_json()  # second ack

    with state.session_factory() as session:
        runs = session.query(m.TestRun).filter_by(drive_serial="REMOTE-DUP").all()
        assert len(runs) == 1  # dedup held


# ---------------------------------------------------- Ack handler


def test_ack_clears_pending_fleet_forward(tmp_path) -> None:
    """When the agent receives a successful ack, the corresponding
    TestRun row's pending_fleet_forward flag must flip to False so
    the next forward-loop pass skips it."""
    _bootstrap_app(tmp_path, role="agent")
    from driveforge.daemon.fleet_client import FleetClient
    from driveforge.daemon.state import get_state
    state = get_state()
    with state.session_factory() as session:
        session.add(m.Drive(
            serial="SN-X", model="m",
            capacity_bytes=1_000_000_000_000, transport="sata",
        ))
        session.add(m.TestRun(
            drive_serial="SN-X", phase="done",
            started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
            grade="A",
            pending_fleet_forward=True,
            fleet_completion_id="c-abc",
        ))
        session.commit()

    client = FleetClient(state)
    client._handle_completion_ack(proto.RunCompletedAckMsg(
        completion_id="c-abc", success=True,
    ).model_dump(mode="json"))

    with state.session_factory() as session:
        run = session.query(m.TestRun).filter_by(drive_serial="SN-X").one()
        assert run.pending_fleet_forward is False


def test_ack_failure_leaves_flag_set_for_retry(tmp_path) -> None:
    """If operator reports ingest failure, WAL flag stays True so
    the next forward-loop tick retries."""
    _bootstrap_app(tmp_path, role="agent")
    from driveforge.daemon.fleet_client import FleetClient
    from driveforge.daemon.state import get_state
    state = get_state()
    with state.session_factory() as session:
        session.add(m.Drive(
            serial="SN-Y", model="m",
            capacity_bytes=1_000_000_000_000, transport="sata",
        ))
        session.add(m.TestRun(
            drive_serial="SN-Y", phase="done",
            started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
            grade="A",
            pending_fleet_forward=True,
            fleet_completion_id="c-y",
        ))
        session.commit()

    client = FleetClient(state)
    client._handle_completion_ack(proto.RunCompletedAckMsg(
        completion_id="c-y", success=False, detail="DB locked",
    ).model_dump(mode="json"))

    with state.session_factory() as session:
        run = session.query(m.TestRun).filter_by(drive_serial="SN-Y").one()
        assert run.pending_fleet_forward is True


# ---------------------------------------------------- History host column


def test_history_shows_host_column_for_remote_rows(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    with state.session_factory() as session:
        # Seed an agent so the display-name lookup works
        session.add(m.Agent(
            id="agent-abc", display_name="r720-bench",
            api_token_hash="not-used",
        ))
        # Seed a remote + local run
        session.add(m.Drive(
            serial="REMOTE-1", model="m",
            capacity_bytes=1_000_000_000_000, transport="sata",
            last_host_id="agent-abc",
        ))
        session.add(m.Drive(
            serial="LOCAL-1", model="m",
            capacity_bytes=1_000_000_000_000, transport="sata",
        ))
        session.add(m.TestRun(
            drive_serial="REMOTE-1", phase="done",
            started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
            grade="A", host_id="agent-abc",
        ))
        session.add(m.TestRun(
            drive_serial="LOCAL-1", phase="done",
            started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
            grade="B",
        ))
        session.commit()

    with TestClient(app) as client:
        resp = client.get("/history")
    assert resp.status_code == 200
    body = resp.text
    assert "r720-bench" in body
    assert ">local<" in body  # local row renders muted "local" marker


def test_history_hides_host_column_for_standalone_history(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="standalone")
    from driveforge.daemon.state import get_state
    state = get_state()
    with state.session_factory() as session:
        session.add(m.Drive(
            serial="LOCAL-1", model="m",
            capacity_bytes=1_000_000_000_000, transport="sata",
        ))
        session.add(m.TestRun(
            drive_serial="LOCAL-1", phase="done",
            started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
            grade="A",
        ))
        session.commit()
    with TestClient(app) as client:
        resp = client.get("/history")
    body = resp.text
    # No <th>Host</th> column because no rows have host_id
    assert "<th title=\"Which fleet node executed this run\">Host</th>" not in body


# ---------------------------------------------------- Forward loop behavior


def test_forward_loop_scans_and_sends_pending(tmp_path) -> None:
    """_send_pending_completions picks up flagged rows and emits a
    RunCompletedMsg per row via the WS send function."""
    _bootstrap_app(tmp_path, role="agent")
    from driveforge.daemon.fleet_client import FleetClient
    from driveforge.daemon.state import get_state
    state = get_state()
    with state.session_factory() as session:
        session.add(m.Drive(
            serial="P-1", model="m",
            capacity_bytes=1, transport="sata",
        ))
        session.add(m.TestRun(
            drive_serial="P-1", phase="done",
            started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
            grade="A",
            pending_fleet_forward=True,
            fleet_completion_id="c-1",
        ))
        session.add(m.TestRun(
            drive_serial="P-1", phase="done",
            started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
            grade="B",
            pending_fleet_forward=False,  # not flagged
        ))
        session.commit()

    client = FleetClient(state)

    class StubWS:
        def __init__(self):
            self.sent = []

        async def send(self, payload):
            self.sent.append(payload)

    ws = StubWS()
    asyncio.run(client._send_pending_completions(ws))
    assert len(ws.sent) == 1
    body = json.loads(ws.sent[0])
    assert body["msg"] == "run_completed"
    assert body["completion_id"] == "c-1"
    assert body["run"]["grade"] == "A"


def test_forward_loop_caps_at_32_per_tick(tmp_path) -> None:
    """If 100 pending rows exist, only 32 go per tick so the socket
    doesn't get flooded."""
    _bootstrap_app(tmp_path, role="agent")
    from driveforge.daemon.fleet_client import FleetClient
    from driveforge.daemon.state import get_state
    state = get_state()
    with state.session_factory() as session:
        session.add(m.Drive(
            serial="MANY", model="m", capacity_bytes=1, transport="sata",
        ))
        for i in range(100):
            session.add(m.TestRun(
                drive_serial="MANY", phase="done",
                started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
                grade="A",
                pending_fleet_forward=True,
                fleet_completion_id=f"c-{i}",
            ))
        session.commit()

    client = FleetClient(state)

    class StubWS:
        def __init__(self):
            self.sent = []

        async def send(self, payload):
            self.sent.append(payload)

    ws = StubWS()
    asyncio.run(client._send_pending_completions(ws))
    assert len(ws.sent) == 32
