"""Tests for v0.7.0's update-safety-gate UX hardening.

The server-side refusal in `/settings/install-update` (active_phase
non-empty → redirect with install_error) already existed since v0.6.x.
v0.7.0 adds client-side UX:

  1. `update_gate()` Jinja global returns {blocked, active_count,
     recovery_count} so base.html's navbar pill can render a muted
     variant.
  2. `settings_page` passes `active_phase_count` + `recovery_count`
     to the Settings template so the Install button renders with
     the `disabled` attribute when > 0.

These tests cover the data plumbing, not the CSS — the muted pill
rendering is a visual check, not unit-testable.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from driveforge import config as cfg


def _bootstrap_app(tmp_path):
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


def test_update_gate_blocked_false_when_idle(tmp_path) -> None:
    """No active drives, no recovery → gate is open."""
    from driveforge.daemon.state import get_state

    app = _bootstrap_app(tmp_path)
    state = get_state()
    state.active_phase.clear()
    state.recovery_serials.clear()

    # Pull the gate function via the app's templates env.
    gate_fn = app.state.templates.env.globals["update_gate"]  # type: ignore[attr-defined]
    g = gate_fn()
    assert g["blocked"] is False
    assert g["active_count"] == 0
    assert g["recovery_count"] == 0


def test_update_gate_blocked_true_with_active_drive(tmp_path) -> None:
    from driveforge.daemon.state import get_state

    app = _bootstrap_app(tmp_path)
    state = get_state()
    state.active_phase["FAKE-SERIAL"] = "badblocks"

    gate_fn = app.state.templates.env.globals["update_gate"]  # type: ignore[attr-defined]
    g = gate_fn()
    assert g["blocked"] is True
    assert g["active_count"] == 1
    assert g["recovery_count"] == 0


def test_update_gate_blocked_true_with_recovery(tmp_path) -> None:
    from driveforge.daemon.state import get_state

    app = _bootstrap_app(tmp_path)
    state = get_state()
    state.recovery_serials.add("RECOV-1")

    gate_fn = app.state.templates.env.globals["update_gate"]  # type: ignore[attr-defined]
    g = gate_fn()
    assert g["blocked"] is True
    assert g["active_count"] == 0
    assert g["recovery_count"] == 1


def test_settings_page_renders_disabled_button_when_pipelines_active(tmp_path) -> None:
    """The Install Update button must carry the `disabled` attribute
    when `active_phase_count > 0`. Renders a message explaining why."""
    from driveforge.core import updates as updates_mod
    from driveforge.daemon.state import get_state

    app = _bootstrap_app(tmp_path)
    state = get_state()
    state.active_phase["FAKE"] = "badblocks"

    # Prime the cached update result so the panel renders at all
    # (the update-available section is hidden when no update exists).
    updates_mod._cached = updates_mod.UpdateInfo(  # type: ignore[attr-defined]
        status="available",
        current_version="0.7.0",
        latest_version="9.9.9",
        release_url="https://example.com",
        release_notes="fake",
    )
    updates_mod._cached_at = 999999999.0  # force cache hit  # type: ignore[attr-defined]

    try:
        with TestClient(app) as client:
            resp = client.get("/settings")
        assert resp.status_code == 200
        body = resp.text
        # The Install button's form block includes both a `disabled`
        # attribute AND an explanatory message about active drives.
        assert "Install update now" in body
        assert "disabled" in body
        assert "Update blocked" in body or "update blocked" in body.lower()
        assert "under test" in body.lower()
    finally:
        updates_mod._cached = None  # type: ignore[attr-defined]
        updates_mod._cached_at = 0.0  # type: ignore[attr-defined]


def test_settings_page_renders_normal_button_when_idle(tmp_path) -> None:
    """No active drives → Install button renders without the
    `disabled` attribute; confirm() JS is bound."""
    from driveforge.core import updates as updates_mod
    from driveforge.daemon.state import get_state

    app = _bootstrap_app(tmp_path)
    state = get_state()
    state.active_phase.clear()
    state.recovery_serials.clear()

    updates_mod._cached = updates_mod.UpdateInfo(  # type: ignore[attr-defined]
        status="available",
        current_version="0.7.0",
        latest_version="9.9.9",
        release_url="https://example.com",
        release_notes="fake",
    )
    updates_mod._cached_at = 999999999.0  # type: ignore[attr-defined]

    try:
        with TestClient(app) as client:
            resp = client.get("/settings")
        assert resp.status_code == 200
        body = resp.text
        assert "Install update now" in body
        # Confirm() onclick handler fires instead of disabled attribute.
        assert "confirm(" in body
    finally:
        updates_mod._cached = None  # type: ignore[attr-defined]
        updates_mod._cached_at = 0.0  # type: ignore[attr-defined]
