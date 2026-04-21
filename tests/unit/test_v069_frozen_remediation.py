"""Tests for v0.6.9's frozen-SSD remediation panel.

The orchestrator registers SSDs that hit the libata-freeze signature
via `frozen_remediation.register_freeze(state.frozen_remediation, ...)`.
The dashboard's drive-detail page renders a structured checklist from
the resulting FrozenRemediationState. Operator actions (retry /
mark-unrecoverable) clear or bump the entry via dedicated HTTP routes.

These tests cover:
  1. register_freeze state machine — first call, retry bump, status
     promotion.
  2. clear() idempotence.
  3. The HTTP retry route starts a fresh pipeline on a present drive
     and preserves the entry (orchestrator bumps retry_count on the
     next failed attempt).
  4. The HTTP mark-unrecoverable route stamps F on the latest TestRun
     + clears remediation state.
"""

from __future__ import annotations

from datetime import UTC, datetime

from driveforge.core.frozen_remediation import (
    REMEDIATION_STEPS,
    FrozenRemediationState,
    FrozenRemediationStatus,
    clear,
    register_freeze,
)

# ------------------------------------------------------------- state machine


def test_register_freeze_first_call_creates_needs_action() -> None:
    """First time a drive hits the freeze pattern: status should be
    NEEDS_ACTION, retry_count 0, timestamps populated."""
    frozen: dict[str, FrozenRemediationState] = {}
    state = register_freeze(
        frozen,
        serial="SSD-ABC123",
        drive_model="Samsung 860 EVO 500GB",
    )

    assert state.serial == "SSD-ABC123"
    assert state.drive_model == "Samsung 860 EVO 500GB"
    assert state.status is FrozenRemediationStatus.NEEDS_ACTION
    assert state.retry_count == 0
    assert state.first_seen_at.tzinfo is UTC
    assert state.last_seen_at == state.first_seen_at
    # Entry is stored in the passed dict
    assert frozen["SSD-ABC123"] is state


def test_register_freeze_second_call_bumps_retry_and_promotes_status() -> None:
    """Second call on the same serial: retry_count increments,
    status promotes to RETRIED_STILL_FROZEN, last_seen_at advances
    but first_seen_at is preserved."""
    frozen: dict[str, FrozenRemediationState] = {}
    first = register_freeze(
        frozen,
        serial="SSD-X",
        drive_model="Intel 320 Series",
    )
    first_seen = first.first_seen_at

    # Simulate a later freeze-signature hit on the same drive.
    second = register_freeze(
        frozen,
        serial="SSD-X",
        drive_model="Intel 320 Series",
    )

    assert second is first, "same dict entry, not a new object"
    assert second.retry_count == 1
    assert second.status is FrozenRemediationStatus.RETRIED_STILL_FROZEN
    assert second.first_seen_at == first_seen
    assert second.last_seen_at >= first_seen


def test_register_freeze_third_call_keeps_status_escalated_and_bumps_count() -> None:
    """Once escalated, status stays RETRIED_STILL_FROZEN; retry_count
    keeps climbing so the UI / log can show cumulative attempts."""
    frozen: dict[str, FrozenRemediationState] = {}
    register_freeze(frozen, serial="SSD-Y", drive_model="model")
    register_freeze(frozen, serial="SSD-Y", drive_model="model")
    third = register_freeze(frozen, serial="SSD-Y", drive_model="model")

    assert third.retry_count == 2
    assert third.status is FrozenRemediationStatus.RETRIED_STILL_FROZEN


def test_register_freeze_updates_drive_model_on_replacement() -> None:
    """Defensive: if the operator swapped the drive in the same slot
    between the first and second freeze sightings, keep the newer
    model string. Unlikely in practice but zero cost to get right."""
    frozen: dict[str, FrozenRemediationState] = {}
    register_freeze(frozen, serial="SN-7", drive_model="Drive A")
    updated = register_freeze(frozen, serial="SN-7", drive_model="Drive B")

    assert updated.drive_model == "Drive B"


def test_clear_removes_entry_and_is_idempotent() -> None:
    frozen: dict[str, FrozenRemediationState] = {}
    register_freeze(frozen, serial="SSD-Z", drive_model="m")
    assert "SSD-Z" in frozen

    clear(frozen, "SSD-Z")
    assert "SSD-Z" not in frozen

    # Clearing a serial that isn't in the map is a no-op, not an error.
    clear(frozen, "SSD-Z")
    clear(frozen, "NEVER-REGISTERED")


# -------------------------------------------------------- checklist exposure


def test_remediation_steps_ordered_least_to_most_invasive() -> None:
    """Sanity check the baked-in order: USB enclosure first (cheapest),
    destruction last (most invasive). Order matters for operator UX —
    the panel is decision-tree shaped."""
    kinds = [step.kind for step in REMEDIATION_STEPS]
    assert kinds[0] == "usb_enclosure"
    assert kinds[-1] == "destroy"
    # Each step must have non-empty title + detail so the template
    # doesn't render blank list items.
    for step in REMEDIATION_STEPS:
        assert step.title
        assert step.detail
        assert step.kind


def test_state_exposes_steps_via_property() -> None:
    """Template hits `state.steps`, not `REMEDIATION_STEPS`. Verify the
    property forwards cleanly."""
    frozen: dict[str, FrozenRemediationState] = {}
    state = register_freeze(frozen, serial="X", drive_model="m")
    assert state.steps is REMEDIATION_STEPS


def test_is_retry_property_tracks_retry_count() -> None:
    frozen: dict[str, FrozenRemediationState] = {}
    state = register_freeze(frozen, serial="X", drive_model="m")
    assert state.is_retry is False

    register_freeze(frozen, serial="X", drive_model="m")
    assert state.is_retry is True


# ------------------------------------------------------------- HTTP routes


def _bootstrap_app(tmp_path):
    """Shared TestClient fixture helper."""
    from driveforge import config as cfg
    from driveforge.daemon.app import make_app
    from driveforge.daemon.state import DaemonState

    settings = cfg.Settings()
    settings.daemon.state_dir = tmp_path
    settings.daemon.db_path = tmp_path / "driveforge.db"
    settings.daemon.pending_labels_dir = tmp_path / "pending-labels"
    settings.daemon.reports_dir = tmp_path / "reports"
    settings.setup_completed = True

    app = make_app(settings)
    DaemonState.boot(settings)
    return app


def test_retry_route_kicks_pipeline_and_preserves_entry(tmp_path, monkeypatch) -> None:
    """Clicking "I tried something, retest" should call start_batch
    on the current Drive object + keep the frozen_remediation entry
    alive. We don't clear on retry — the orchestrator is the one that
    decides to clear (on success) or bump (on next failure)."""
    from fastapi.testclient import TestClient

    from driveforge.daemon.state import get_state

    app = _bootstrap_app(tmp_path)
    state = get_state()

    # Pre-seed a frozen entry.
    register_freeze(
        state.frozen_remediation,
        serial="SSD-RETRY-ME",
        drive_model="Samsung 860 EVO",
    )

    # Stub drive discovery so the route finds a matching Drive.
    from driveforge.core import drive as drive_mod

    class _FakeDrive:
        serial = "SSD-RETRY-ME"
        device_path = "/dev/sdz"
        transport = "sata"
        model = "Samsung 860 EVO"
        capacity_bytes = 500_000_000_000

    def fake_discover():
        return [_FakeDrive()]

    monkeypatch.setattr(drive_mod, "discover", fake_discover)

    # Stub orchestrator.start_batch so we don't actually launch pipelines.
    called_with: list[object] = []

    async def fake_start_batch(drives, *, source, quick):
        called_with.append((drives, source, quick))

    app.state.orchestrator.start_batch = fake_start_batch  # type: ignore[attr-defined]

    with TestClient(app, follow_redirects=False) as client:
        resp = client.post("/drives/SSD-RETRY-ME/frozen/retry")

    assert resp.status_code == 303
    assert "frozen=retry-started" in resp.headers["location"]
    assert called_with, "start_batch was not invoked"
    # Entry still present — orchestrator will bump count on next failure,
    # or clear on success.
    assert "SSD-RETRY-ME" in state.frozen_remediation


def test_mark_unrecoverable_route_stamps_F_and_clears_entry(tmp_path, monkeypatch) -> None:
    """Clicking "Mark as unrecoverable" must:
    (a) set grade=F on the latest TestRun (so auto-enroll skips),
    (b) clear the frozen_remediation entry (panel disappears),
    (c) redirect with a confirmation flash."""
    from fastapi.testclient import TestClient

    from driveforge.daemon.state import get_state
    from driveforge.db import models as m

    app = _bootstrap_app(tmp_path)
    state = get_state()

    # Seed a Drive row + a latest TestRun so the route has something
    # to update. Otherwise it falls through to the "create new" branch
    # which we cover separately below.
    with state.session_factory() as session:
        session.add(
            m.Drive(
                serial="SSD-DESTROY",
                model="Intel 320",
                manufacturer="Intel",
                transport="sata",
                capacity_bytes=120_000_000_000,
            )
        )
        session.add(
            m.TestRun(
                drive_serial="SSD-DESTROY",
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                phase="secure_erase",
                grade="error",
            )
        )
        session.commit()

    register_freeze(
        state.frozen_remediation,
        serial="SSD-DESTROY",
        drive_model="Intel 320",
    )

    with TestClient(app, follow_redirects=False) as client:
        resp = client.post("/drives/SSD-DESTROY/frozen/mark-unrecoverable")

    assert resp.status_code == 303
    assert "marked-unrecoverable" in resp.headers["location"]
    # Remediation entry cleared.
    assert "SSD-DESTROY" not in state.frozen_remediation

    with state.session_factory() as session:
        run = (
            session.query(m.TestRun)
            .filter_by(drive_serial="SSD-DESTROY")
            .order_by(m.TestRun.completed_at.desc())
            .first()
        )
        assert run is not None
        assert run.grade == "F"
        assert run.phase == "frozen_unrecoverable"
        assert run.error_message and "unrecoverable" in run.error_message.lower()


def test_mark_unrecoverable_creates_run_when_none_exists(tmp_path) -> None:
    """Edge: operator marks a drive unrecoverable before it ever got
    a TestRun (rare, but possible if the pipeline errored before
    inserting its row). Route creates a synthetic TestRun carrying the
    sticky F so auto-enroll skip semantics still apply."""
    from fastapi.testclient import TestClient

    from driveforge.daemon.state import get_state
    from driveforge.db import models as m

    app = _bootstrap_app(tmp_path)
    state = get_state()

    # Seed a Drive row but NO TestRun.
    with state.session_factory() as session:
        session.add(
            m.Drive(
                serial="SSD-NO-RUN",
                model="Crucial MX500",
                manufacturer="Crucial",
                transport="sata",
                capacity_bytes=250_000_000_000,
            )
        )
        session.commit()

    register_freeze(
        state.frozen_remediation,
        serial="SSD-NO-RUN",
        drive_model="Crucial MX500",
    )

    with TestClient(app, follow_redirects=False) as client:
        resp = client.post("/drives/SSD-NO-RUN/frozen/mark-unrecoverable")

    assert resp.status_code == 303

    with state.session_factory() as session:
        runs = (
            session.query(m.TestRun)
            .filter_by(drive_serial="SSD-NO-RUN")
            .all()
        )
        assert len(runs) == 1
        assert runs[0].grade == "F"
        assert runs[0].phase == "frozen_unrecoverable"
