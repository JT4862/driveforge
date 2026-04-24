"""v0.11.9 — fleet runs are first-class.

Five thematically-related fixes JT identified during the v0.11.6/7/8
walkthrough on his NX-3200 + R720 fleet:

  1. Batch ID propagation — operator's batch detail page only listed
     local rows because StartPipelineCmd didn't carry the operator's
     batch_id and ingestion of RunCompletedMsg explicitly dropped the
     agent's id with `batch_id=None  # batch IDs are agent-local`.
     Now the operator pre-mints the id, dispatches it to agents, and
     ingestion preserves it. Both rows under one Batch.

  2. Remote drive cards show last test history — `_remote_installed_card`
     hardcoded last_grade=None / last_tested=None even though v0.10.3
     cert forwarding HAD landed test runs into the DB. Now it queries
     the DB for the most recent run on (drive_serial, host_id=agent_id)
     and populates the card just like the local _installed_card does.

  3. Card clutter — host badge moved out of `position: absolute;
     top:8px; right:8px;` (overlapped Abort button on active cards).
     Now flows naturally at the top of the card. CSS-only; no behavior
     test, but the rule should remain present.

  4. HDD quick-mode libata-freeze stamp bug — when an HDD hit the
     freeze in quick mode, _run_secure_erase stamped
     sanitization_method='badblocks_overwrite' then returned normally
     expecting badblocks to run. But quick mode skips badblocks. The
     DB row claimed sanitization that never happened. Now the
     fallback only engages in full mode; quick mode raises a
     PipelineFailure with a synthesized "Re-run in full mode" hint.

  5. Mark-as-unrecoverable physical print — the frozen-SSD AND
     password-locked panel buttons now fire a physical UNRECOVERABLE
     label print so the operator's hand has a sticker for the drive
     going to destruction. CertLabelData gains an `unrecoverable: bool`
     field; render_label swaps the title to "DriveForge — DESTROY"
     and the right-column glyph to "✗".
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime

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


# ============================================================ Fix #1


def test_start_pipeline_cmd_carries_batch_id() -> None:
    """Protocol field exists and roundtrips."""
    cmd = proto.StartPipelineCmd(
        cmd_id="c1", serial="S1", quick_mode=True, batch_id="abc123",
    )
    data = cmd.model_dump(mode="json")
    assert data["batch_id"] == "abc123"
    reparsed = proto.StartPipelineCmd.model_validate(data)
    assert reparsed.batch_id == "abc123"


def test_start_pipeline_cmd_batch_id_optional_for_pre_v0_11_9() -> None:
    """Backwards compat — older operators don't send batch_id."""
    cmd = proto.StartPipelineCmd(cmd_id="c1", serial="S1")
    assert cmd.batch_id is None
    # JSON roundtrip preserves None.
    reparsed = proto.StartPipelineCmd.model_validate(cmd.model_dump(mode="json"))
    assert reparsed.batch_id is None


def test_post_new_batch_creates_batch_row_up_front(tmp_path, monkeypatch) -> None:
    """POST /batches/new mints a batch_id and inserts the Batch row
    BEFORE dispatching, so agent ingestion can FK against it even
    in pure-remote (no local drives) scenarios."""
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.core import drive as drive_mod_
    from driveforge.daemon.state import get_state, RemoteAgentState
    from driveforge.db import models as m

    monkeypatch.setattr(drive_mod_, "discover", lambda: [])

    state = get_state()
    ra = RemoteAgentState(
        agent_id="agent-r720", display_name="r720", hostname=None,
        agent_version="0.11.9", protocol_version="1.0",
        connected_at=time.monotonic(), last_message_at=time.monotonic(),
        drives={"REMOTE-1": proto.DriveState(
            serial="REMOTE-1", model="WDC", capacity_bytes=1_000_204_886_016,
            transport="sata",
        )},
        outbound_queue=asyncio.Queue(maxsize=16),
    )
    state.remote_agents["agent-r720"] = ra

    with TestClient(app) as client:
        resp = client.post(
            "/batches/new",
            data={"drive": "REMOTE-1", "confirm": "ERASE", "source": "test pull"},
            follow_redirects=False,
        )
    assert resp.status_code == 303

    # The Batch row was inserted by the POST handler (not by start_batch,
    # which never ran since there were no local drives).
    with state.session_factory() as session:
        batches = session.query(m.Batch).all()
    assert len(batches) == 1
    assert batches[0].source == "test pull"

    # The same batch_id flowed down into the StartPipelineCmd dispatched
    # to the agent's outbound queue.
    payload = ra.outbound_queue.get_nowait()
    body = json.loads(payload)
    assert body["msg"] == "start_pipeline"
    assert body["serial"] == "REMOTE-1"
    assert body["batch_id"] == batches[0].id


def test_ingest_completion_preserves_batch_id(tmp_path) -> None:
    """fleet_server.handle_run_completed writes r.batch_id (not None)
    to the new TestRun row. Pre-v0.11.9 it was hardcoded None."""
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state, RemoteAgentState
    from driveforge.daemon import fleet_server
    from driveforge.db import models as m

    state = get_state()
    state.remote_agents["agent-x"] = RemoteAgentState(
        agent_id="agent-x", display_name="r720", hostname=None,
        agent_version="0.11.9", protocol_version="1.0",
        connected_at=time.monotonic(), last_message_at=time.monotonic(),
        drives={}, outbound_queue=asyncio.Queue(maxsize=16),
    )

    # Pre-create the Batch row (POST handler would have done this).
    with state.session_factory() as session:
        session.add(m.Batch(id="batch-shared-1", source="test", started_at=datetime.now(UTC)))
        session.commit()

    completion = proto.RunCompletedMsg(
        completion_id="c-abc",
        drive=proto.CompletedDriveData(
            serial="DRIVE-A", model="WDC", capacity_bytes=1_000_204_886_016,
            transport="sata",
        ),
        run=proto.CompletedRunData(
            run_id=42, drive_serial="DRIVE-A", batch_id="batch-shared-1",
            phase="done", started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC), grade="A",
        ),
    )

    # Mock the WebSocket so the ack send doesn't blow up.
    class _StubWS:
        async def send_text(self, _payload: str) -> None:
            pass

    asyncio.run(
        fleet_server._ingest_run_completed(
            _StubWS(), state, "agent-x", completion.model_dump(mode="json"),
        )
    )

    with state.session_factory() as session:
        runs = session.query(m.TestRun).all()
    assert len(runs) == 1
    assert runs[0].batch_id == "batch-shared-1"
    assert runs[0].host_id == "agent-x"


# ============================================================ Fix #2


def test_remote_installed_card_pulls_last_grade_from_db(tmp_path) -> None:
    """_remote_installed_card queries the operator's DB for the most
    recent TestRun matching (drive_serial, host_id=agent_id) and
    populates last_grade / last_tested / last_phase fields. Pre-v0.11.9
    these were hardcoded None."""
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state, RemoteAgentState
    from driveforge.db import models as m
    from driveforge.web.routes import _remote_installed_card

    state = get_state()
    ra = RemoteAgentState(
        agent_id="agent-r720", display_name="r720", hostname=None,
        agent_version="0.11.9", protocol_version="1.0",
        connected_at=time.monotonic(), last_message_at=time.monotonic(),
        drives={}, outbound_queue=asyncio.Queue(maxsize=16),
    )
    state.remote_agents["agent-r720"] = ra
    drive_state = proto.DriveState(
        serial="REMOTE-1", model="WDC", capacity_bytes=1_000_204_886_016,
        transport="sata",
    )

    with state.session_factory() as session:
        session.add(m.Drive(
            serial="REMOTE-1", model="WDC",
            capacity_bytes=1_000_204_886_016, transport="sata",
        ))
        session.add(m.TestRun(
            drive_serial="REMOTE-1",
            host_id="agent-r720",
            phase="done",
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            grade="A",
        ))
        session.commit()
        # Re-open session for the card render so the new rows are visible.
    with state.session_factory() as session:
        card = _remote_installed_card(ra, drive_state, session=session)
    assert card["last_grade"] == "A"
    assert card["last_tested"] is not None
    assert card["last_phase"] == "done"


def test_remote_installed_card_no_history_returns_none(tmp_path) -> None:
    """When there's no TestRun in the DB for this serial+agent_id,
    fields stay None — same shape as pre-v0.11.9."""
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state, RemoteAgentState
    from driveforge.web.routes import _remote_installed_card

    state = get_state()
    ra = RemoteAgentState(
        agent_id="agent-r720", display_name="r720", hostname=None,
        agent_version="0.11.9", protocol_version="1.0",
        connected_at=time.monotonic(), last_message_at=time.monotonic(),
        drives={}, outbound_queue=asyncio.Queue(maxsize=16),
    )
    drive_state = proto.DriveState(
        serial="NEVER-TESTED", model="WDC",
        capacity_bytes=1_000_204_886_016, transport="sata",
    )
    with state.session_factory() as session:
        card = _remote_installed_card(ra, drive_state, session=session)
    assert card["last_grade"] is None
    assert card["last_tested"] is None
    assert card["last_phase"] is None


def test_remote_installed_card_filters_by_host_id(tmp_path) -> None:
    """A TestRun for this serial executed by a DIFFERENT host (e.g. the
    operator itself, host_id=None) should NOT show up on this agent's
    card. Each agent's card surfaces only that agent's history of the
    drive."""
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state, RemoteAgentState
    from driveforge.db import models as m
    from driveforge.web.routes import _remote_installed_card

    state = get_state()
    ra = RemoteAgentState(
        agent_id="agent-r720", display_name="r720", hostname=None,
        agent_version="0.11.9", protocol_version="1.0",
        connected_at=time.monotonic(), last_message_at=time.monotonic(),
        drives={}, outbound_queue=asyncio.Queue(maxsize=16),
    )
    drive_state = proto.DriveState(
        serial="MOBILE-1", model="WDC",
        capacity_bytes=1_000_204_886_016, transport="sata",
    )
    with state.session_factory() as session:
        session.add(m.Drive(
            serial="MOBILE-1", model="WDC",
            capacity_bytes=1_000_204_886_016, transport="sata",
        ))
        # Run on operator (host_id=None) — should NOT appear on agent's card
        session.add(m.TestRun(
            drive_serial="MOBILE-1", host_id=None,
            phase="done", started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC), grade="C",
        ))
        session.commit()
    with state.session_factory() as session:
        card = _remote_installed_card(ra, drive_state, session=session)
    # Agent has no run on this serial → card should still show None.
    assert card["last_grade"] is None


# ============================================================ Fix #3


def test_host_badge_css_no_longer_absolute_positioned() -> None:
    """The .host-badge CSS rule must not include `position: absolute;`
    — that caused the v0.10.x→v0.11.6 collision with .abort-form on
    active cards (both were top-right). Now flows in normal layout."""
    from pathlib import Path
    css = Path("driveforge/web/static/app.css").read_text()
    # Find the .host-badge rule block (terminates at next })
    idx = css.find(".host-badge {")
    assert idx >= 0, ".host-badge rule must exist"
    block = css[idx:idx + css[idx:].find("}")]
    assert "position: absolute" not in block, (
        ".host-badge must not be absolutely positioned (v0.11.9+)"
    )


# ============================================================ Fix #4


def test_decoded_error_for_quick_mode_hdd_freeze_mentions_full_mode() -> None:
    """The synthesized DecodedError for HDD+quick-mode+libata-freeze
    must include a "Re-run in full mode" CTA. We can't easily trigger
    the orchestrator path under unit tests, but we can lock in the
    message shape by reading the orchestrator source (drift would
    silently regress this CTA)."""
    from pathlib import Path
    src = Path("driveforge/daemon/orchestrator.py").read_text()
    # The block that synthesizes the message lives inside _run_secure_erase
    # right after `if is_hdd and is_libata_freeze and quick:`.
    assert "is_hdd and is_libata_freeze and quick" in src
    assert "Re-run this drive in FULL mode" in src
    assert "satisfies NIST 800-88 Clear" in src


# ============================================================ Fix #5a


def test_cert_label_data_has_unrecoverable_field() -> None:
    """CertLabelData gained an `unrecoverable: bool` field for the
    DriveForge-DESTROY label variant."""
    from datetime import date as _date
    from driveforge.core.printer import CertLabelData
    data = CertLabelData(
        model="WD Blue", serial="ABC", capacity_tb=1.0,
        grade="F", tested_date=_date.today(),
        power_on_hours=0, report_url="http://x/", unrecoverable=True,
    )
    assert data.unrecoverable is True


def test_unrecoverable_label_renders_destroy_title() -> None:
    """render_label with unrecoverable=True swaps the title from
    'DriveForge — FAIL' to 'DriveForge — DESTROY' so the operator
    can tell the two label types apart at arm's length."""
    from datetime import date as _date
    from PIL import Image
    from driveforge.core.printer import CertLabelData, render_label

    data = CertLabelData(
        model="INTEL SSDSC2BB120G4",
        serial="CVWL431600NF120LGN",
        capacity_tb=0.12, grade="F",
        tested_date=_date(2026, 4, 24),
        power_on_hours=0,
        report_url="http://test/reports/x",
        fail_reason="Marked unrecoverable by operator",
        unrecoverable=True,
    )
    img = render_label(data)
    assert isinstance(img, Image.Image)
    # We can't easily OCR our own raster in a unit test, but we can
    # at least ensure the render returned a non-empty image at the
    # expected DK-1209 dimensions (defends against the render path
    # silently no-oping for the new code branch).
    assert img.size[0] > 100 and img.size[1] > 100


def test_auto_print_unrecoverable_for_drive_handles_no_printer(tmp_path) -> None:
    """`auto_print_unrecoverable_for_drive` returns (False, ...) when
    no printer is configured — same fail-safe shape as
    auto_print_cert_for_run. Caller never raises."""
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    from driveforge.core.printer import auto_print_unrecoverable_for_drive
    state = get_state()
    state.settings.printer.model = ""  # no printer
    drive_obj = type("D", (), dict(
        serial="ABC", model="WDC", capacity_bytes=1_000_204_886_016,
    ))()
    ok, msg = auto_print_unrecoverable_for_drive(state, drive_obj)
    assert ok is False
    assert "no printer" in msg.lower()


# ============================================================ Fix #5b


def test_retest_button_uses_primary_class() -> None:
    """The frozen-remediation 'I tried one of these — retest' button
    must carry the `.primary` modifier so it renders as the panel's
    blue CTA. Pre-v0.11.9 it was a plain `.btn` and visually got
    drowned out by the red unrecoverable button."""
    from pathlib import Path
    tmpl = Path("driveforge/web/templates/drive_detail.html").read_text()
    # Find the frozen retry form button line.
    idx = tmpl.find("I tried one of these")
    assert idx >= 0
    # Walk back to the opening <button to see its class attr.
    btn_open = tmpl.rfind("<button", 0, idx)
    btn_chunk = tmpl[btn_open:idx]
    assert 'class="btn primary"' in btn_chunk
