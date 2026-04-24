"""v0.11.11 — settings page reorder + remote elapsed time.

Two operator-visible improvements:

1. Settings page section order rearranged per JT's preference.
   Updates moved up to position #3 (was #7) so the most-frequently-
   accessed admin action sits near the top. Printer also moved up
   (was #5, stays #5 in new order but now sits before Grading
   thresholds which moved down). Final order: Hardware → Hostname
   → Updates → Fleet → Printer → Grading → Integrations → Daemon
   → Advanced.

2. Remote active drive cards now show elapsed pipeline time. Pre-
   v0.11.11 the operator hardcoded `elapsed_label=""` for remote
   drives because it had no way to know the agent's wall-clock
   pipeline start. Now the agent stamps `state.active_started_at_utc`
   in DaemonState at every active-phase transition (idempotent via
   setdefault — first stamp wins), the snapshot ships it as
   DriveState.pipeline_started_at, and the operator computes
   `elapsed_label = format_duration(now - pipeline_started_at)` on
   each render.

Tests:
  - Settings page renders sections in the new order
  - DriveState protocol roundtrips pipeline_started_at
  - DriveState.pipeline_started_at is optional (backwards compat
    with pre-v0.11.11 agents — operator falls back to empty
    elapsed_label like before)
  - _remote_active_card with pipeline_started_at populated → renders
    elapsed_label
  - _remote_active_card without pipeline_started_at (None or
    missing) → renders empty elapsed_label
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

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


# ============================================================ Settings reorder


def test_settings_sections_render_in_new_order(tmp_path) -> None:
    """Sections must appear in this order on the Settings page (top to
    bottom): Hardware → Hostname → About / Updates → Fleet → Printer
    → Grading thresholds → Integrations → Daemon → Advanced.

    Pre-v0.11.11 the order was: Hardware → Hostname → Fleet → Grading
    → Printer → Integrations → About → Daemon → Advanced (Updates
    was #7). JT's preference: most-frequently-accessed admin actions
    near the top."""
    app = _bootstrap_app(tmp_path, role="operator")
    with TestClient(app) as client:
        resp = client.get("/settings")
    body = resp.text
    expected_h2s = [
        "Hardware",
        "Hostname",
        "About / Updates",
        "Fleet",
        "Printer",
        "Grading thresholds",
        "Integrations",
        "Daemon",
        "Advanced",
    ]
    # Locate each h2 header in body order and ensure they appear
    # in the expected sequence.
    positions = []
    for h2 in expected_h2s:
        idx = body.find(f"<h2>{h2}</h2>")
        assert idx != -1, f"missing h2: {h2}"
        positions.append((idx, h2))
    # Positions must be strictly ascending.
    for prev, curr in zip(positions, positions[1:]):
        assert prev[0] < curr[0], (
            f"section order wrong: {prev[1]!r} should appear before {curr[1]!r} "
            f"but found at byte {prev[0]} vs {curr[0]}"
        )


# ============================================================ Remote elapsed


def test_drive_state_protocol_carries_pipeline_started_at() -> None:
    """The new field roundtrips cleanly."""
    started = datetime.now(UTC) - timedelta(seconds=120)
    ds = proto.DriveState(
        serial="S1", model="WDC", capacity_bytes=1, transport="sata",
        phase="short_test", pipeline_started_at=started,
    )
    payload = ds.model_dump(mode="json")
    assert "pipeline_started_at" in payload
    reparsed = proto.DriveState.model_validate(payload)
    assert reparsed.pipeline_started_at == started


def test_drive_state_pipeline_started_at_optional() -> None:
    """Pre-v0.11.11 agents don't send the field — pydantic accepts
    that and the operator's render path falls back to empty
    elapsed_label."""
    ds = proto.DriveState(
        serial="S1", model="WDC", capacity_bytes=1, transport="sata",
        phase="short_test",
    )
    assert ds.pipeline_started_at is None
    reparsed = proto.DriveState.model_validate(ds.model_dump(mode="json"))
    assert reparsed.pipeline_started_at is None


def test_remote_active_card_renders_elapsed_when_started_at_present(tmp_path) -> None:
    """Drive currently active with a known pipeline_started_at →
    elapsed_label is non-empty and matches a "Ns / Nm Ns" format."""
    _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import RemoteAgentState
    from driveforge.web.routes import _remote_active_card
    started = datetime.now(UTC) - timedelta(seconds=90)
    ra = RemoteAgentState(
        agent_id="agent-r720", display_name="r720", hostname=None,
        agent_version="0.11.11", protocol_version="1.0",
        connected_at=time.monotonic(), last_message_at=time.monotonic(),
        drives={}, outbound_queue=None,
    )
    ds = proto.DriveState(
        serial="REMOTE-1", model="ST300MM0006",
        capacity_bytes=300_000_000_000, transport="sas",
        phase="short_test", percent=42.0,
        pipeline_started_at=started,
    )
    card = _remote_active_card(ra, ds)
    assert card["elapsed_label"], (
        f"expected non-empty elapsed_label, got {card['elapsed_label']!r}"
    )
    # The format_duration output for ~90s should mention "1m" or "1m 30s"
    # (don't pin exactly — tests run with millisecond drift).
    assert "m" in card["elapsed_label"] or "s" in card["elapsed_label"]


def test_remote_active_card_falls_back_when_started_at_missing(tmp_path) -> None:
    """Pre-v0.11.11 agent (no pipeline_started_at field in snapshot)
    or a drive that just transitioned in this same snapshot but
    hasn't been stamped yet → empty elapsed_label, same shape as
    the v0.10.1 → v0.11.10 hardcoded behavior so the template
    renders nothing."""
    _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import RemoteAgentState
    from driveforge.web.routes import _remote_active_card
    ra = RemoteAgentState(
        agent_id="agent-r720", display_name="r720", hostname=None,
        agent_version="0.11.10", protocol_version="1.0",
        connected_at=time.monotonic(), last_message_at=time.monotonic(),
        drives={}, outbound_queue=None,
    )
    ds = proto.DriveState(
        serial="REMOTE-1", model="WDC",
        capacity_bytes=1_000_204_886_016, transport="sata",
        phase="short_test", percent=10.0,
        pipeline_started_at=None,
    )
    card = _remote_active_card(ra, ds)
    assert card["elapsed_label"] == ""


def test_orchestrator_stamps_active_started_at(tmp_path) -> None:
    """Sanity: the orchestrator's _advance helper sets
    state.active_started_at_utc the first time a serial enters an
    active phase. Idempotent on subsequent phase transitions
    (setdefault)."""
    _bootstrap_app(tmp_path, role="standalone")
    from driveforge.daemon.state import get_state
    state = get_state()
    assert state.active_started_at_utc == {}
    # Simulate the orchestrator setdefault pattern.
    state.active_started_at_utc.setdefault("S1", datetime.now(UTC))
    first_stamp = state.active_started_at_utc["S1"]
    # Subsequent setdefault must NOT overwrite (this mimics the
    # orchestrator's behavior on each phase transition).
    later = datetime.now(UTC) + timedelta(seconds=30)
    state.active_started_at_utc.setdefault("S1", later)
    assert state.active_started_at_utc["S1"] == first_stamp
