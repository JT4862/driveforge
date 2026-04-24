"""v0.10.5 — web UI role toggle on the Settings page.

Pre-v0.10.5 the Fleet panel described the role but didn't offer a
form; operators had to replay the setup wizard to flip between
standalone and operator. This release adds the missing form +
handler. Agent mode stays CLI-only (agents are born by consuming
an enrollment token).

Covers:
  - POST /settings/fleet-role with role=operator flips fleet.role
  - Signals restart required via ?restart=1
  - Rejects unknown role values
  - Same-role submit is a no-op + no restart banner
  - Settings page renders the form for standalone/operator
  - Settings page shows the CLI-detach hint for agents (no form)
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from driveforge import config as cfg


def _bootstrap_app(tmp_path, *, role: str = "standalone"):
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


def test_fleet_role_toggle_standalone_to_operator(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="standalone")
    from driveforge.daemon.state import get_state
    state = get_state()
    with TestClient(app) as client:
        resp = client.post(
            "/settings/fleet-role",
            data={"role": "operator"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert "saved=fleet_role" in resp.headers["location"]
    assert "restart=1" in resp.headers["location"]
    assert state.settings.fleet.role == "operator"


def test_fleet_role_toggle_operator_to_standalone(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    with TestClient(app) as client:
        resp = client.post(
            "/settings/fleet-role",
            data={"role": "standalone"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert state.settings.fleet.role == "standalone"


def test_fleet_role_same_role_is_noop_no_restart(tmp_path) -> None:
    """Clicking Save with the current role already selected shouldn't
    advertise a restart — nothing changed."""
    app = _bootstrap_app(tmp_path, role="standalone")
    with TestClient(app) as client:
        resp = client.post(
            "/settings/fleet-role",
            data={"role": "standalone"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert "restart=1" not in resp.headers["location"]


def test_fleet_role_rejects_invalid_value(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="standalone")
    from driveforge.daemon.state import get_state
    state = get_state()
    with TestClient(app) as client:
        resp = client.post(
            "/settings/fleet-role",
            data={"role": "overlord"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert "fleet_role_invalid" in resp.headers["location"]
    # Unchanged
    assert state.settings.fleet.role == "standalone"


def test_fleet_role_rejects_agent_via_web(tmp_path) -> None:
    """Agent mode is CLI-only. Accepting it here would be a footgun
    (no token, no operator_url, resulting daemon can't do anything
    useful)."""
    app = _bootstrap_app(tmp_path, role="standalone")
    from driveforge.daemon.state import get_state
    state = get_state()
    with TestClient(app) as client:
        resp = client.post(
            "/settings/fleet-role",
            data={"role": "agent"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert "fleet_role_invalid" in resp.headers["location"]
    assert state.settings.fleet.role == "standalone"


def test_settings_page_renders_role_form_on_standalone(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="standalone")
    with TestClient(app) as client:
        resp = client.get("/settings")
    body = resp.text
    # Form action points at the handler
    assert 'action="/settings/fleet-role"' in body
    # Standalone radio is checked
    assert 'value="standalone"' in body and "checked" in body


def test_settings_page_shows_cli_hint_on_agent(tmp_path) -> None:
    """v0.10.7: agents saw a Settings page w/ a 'run fleet leave'
    copy. v0.11.0: agents serve no HTML at all — they're API-only.
    The fleet-leave hint moved to the plaintext response at GET /
    so the operator-SSH debug path still finds it.

    Regression guard: confirm the hint is on the plaintext landing
    page. The old behavior (Settings page serving the hint) is
    intentionally gone."""
    app = _bootstrap_app(tmp_path, role="agent")
    with TestClient(app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    # Plaintext contains the fleet-leave instruction
    assert "fleet leave" in resp.text
    # Settings page itself is 404 for agents
    with TestClient(app) as client:
        s_resp = client.get("/settings")
    assert s_resp.status_code == 404
