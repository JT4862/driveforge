"""v0.10.0 — fleet foundation.

Covers:
  - FleetConfig round-trips via YAML (defaults + set values)
  - Agent + EnrollmentToken models auto-create via init_db
  - host_id columns present on drives + test_runs + telemetry_samples
  - Enrollment token issuance returns a composite token whose raw
    half hashes to the stored hash
  - consume_enrollment_token happy path creates an Agent + marks the
    enrollment row consumed
  - consume refuses: unknown / already-consumed / expired / hash-mismatch
  - authenticate_agent validates long-lived tokens + rejects revoked
  - /api/fleet/enroll endpoint happy path (operator role)
  - /api/fleet/enroll refuses with 404 when role != "operator"
  - /settings/agents page renders with role-specific banners
  - /settings/agents/new-token redirects with raw token in query string
  - /settings/agents/<id>/revoke stamps revoked_at
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from driveforge import config as cfg
from driveforge.core import fleet as fleet_mod
from driveforge.db import models as m


def _bootstrap_app(tmp_path, *, role: str = "standalone"):
    """Match the v0.9.x test shape, with a role override for fleet tests."""
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


# ---------------------------------------------------- FleetConfig


def test_fleet_config_defaults_to_standalone() -> None:
    """Standalone is the v0.10.0 compatibility default — upgrading
    from v0.9.x never accidentally flips anyone into fleet mode."""
    settings = cfg.Settings()
    assert settings.fleet.role == "standalone"
    assert settings.fleet.operator_url is None
    assert settings.fleet.listen_port == 8443
    assert settings.fleet.enrollment_token_ttl_seconds == 900


def test_fleet_config_roundtrips_through_yaml(tmp_path: Path) -> None:
    """Operator toggles role + saves — the next daemon boot must see
    the change. YAML round-trip is the load-bearing path."""
    settings = cfg.Settings()
    settings.fleet.role = "operator"
    settings.fleet.listen_port = 9443
    settings.fleet.display_name = "nx3200"
    cfg_path = tmp_path / "dv.yaml"
    cfg.save(settings, cfg_path)
    reloaded = cfg.load(cfg_path)
    assert reloaded.fleet.role == "operator"
    assert reloaded.fleet.listen_port == 9443
    assert reloaded.fleet.display_name == "nx3200"


# ---------------------------------------------------- DB schema


def test_agent_and_enrollment_tables_exist(tmp_path) -> None:
    _bootstrap_app(tmp_path)
    from driveforge.daemon.state import get_state
    state = get_state()
    from sqlalchemy import inspect
    insp = inspect(state.engine)
    assert insp.has_table("agents")
    assert insp.has_table("enrollment_tokens")


def test_host_id_columns_present_on_aggregation_tables(tmp_path) -> None:
    """v0.10.0 adds host_id to drives, test_runs, telemetry_samples
    so the operator can aggregate remote agent rows. A missing column
    here would silently break the fleet aggregation path later."""
    _bootstrap_app(tmp_path)
    from driveforge.daemon.state import get_state
    from sqlalchemy import inspect
    state = get_state()
    insp = inspect(state.engine)

    drive_cols = {c["name"] for c in insp.get_columns("drives")}
    assert "last_host_id" in drive_cols
    assert "last_host_seen_at" in drive_cols

    run_cols = {c["name"] for c in insp.get_columns("test_runs")}
    assert "host_id" in run_cols

    telem_cols = {c["name"] for c in insp.get_columns("telemetry_samples")}
    assert "host_id" in telem_cols


# ---------------------------------------------------- Token primitives


def test_issue_enrollment_token_stores_hash_not_plaintext(tmp_path) -> None:
    _bootstrap_app(tmp_path)
    from driveforge.daemon.state import get_state
    state = get_state()
    with state.session_factory() as session:
        issue = fleet_mod.issue_enrollment_token(session, ttl_seconds=60)
        # Raw token is composite: "<id>.<raw>"
        assert "." in issue.raw_token
        token_id, raw = issue.raw_token.split(".", 1)
        assert token_id == issue.token_id
        row = session.get(m.EnrollmentToken, token_id)
        assert row is not None
        # Plaintext not stored
        assert row.token_hash != raw
        assert row.token_hash != issue.raw_token
        # Hash matches
        assert fleet_mod._verify_token(raw, row.token_hash)


def test_consume_enrollment_token_happy_path(tmp_path) -> None:
    _bootstrap_app(tmp_path)
    from driveforge.daemon.state import get_state
    state = get_state()
    with state.session_factory() as session:
        issue = fleet_mod.issue_enrollment_token(session, ttl_seconds=60)
    with state.session_factory() as session:
        result = fleet_mod.consume_enrollment_token(
            session,
            composite_token=issue.raw_token,
            display_name="r720",
            hostname="driveforge-r720",
            version="0.10.0",
        )
        assert "." in result.api_token
        agent_id, raw = result.api_token.split(".", 1)
        assert agent_id == result.agent_id
        agent = session.get(m.Agent, agent_id)
        assert agent is not None
        assert agent.display_name == "r720"
        assert agent.hostname == "driveforge-r720"
        assert agent.version == "0.10.0"
        assert agent.revoked_at is None
        # Enrollment row is now consumed
        tok = session.get(m.EnrollmentToken, issue.token_id)
        assert tok.consumed_at is not None
        assert tok.consumed_by_agent_id == agent_id


def test_consume_rejects_reused_token(tmp_path) -> None:
    _bootstrap_app(tmp_path)
    from driveforge.daemon.state import get_state
    state = get_state()
    with state.session_factory() as session:
        issue = fleet_mod.issue_enrollment_token(session, ttl_seconds=60)
    with state.session_factory() as session:
        fleet_mod.consume_enrollment_token(
            session,
            composite_token=issue.raw_token,
            display_name="first",
            hostname=None,
            version=None,
        )
    # Second consumer on the same token must fail
    with state.session_factory() as session:
        import pytest
        with pytest.raises(fleet_mod.EnrollmentError):
            fleet_mod.consume_enrollment_token(
                session,
                composite_token=issue.raw_token,
                display_name="second",
                hostname=None,
                version=None,
            )


def test_consume_rejects_expired_token(tmp_path) -> None:
    _bootstrap_app(tmp_path)
    from driveforge.daemon.state import get_state
    state = get_state()
    with state.session_factory() as session:
        issue = fleet_mod.issue_enrollment_token(session, ttl_seconds=60)
        # Force-expire
        tok = session.get(m.EnrollmentToken, issue.token_id)
        tok.expires_at = datetime.now(UTC) - timedelta(seconds=5)
        session.commit()
    with state.session_factory() as session:
        import pytest
        with pytest.raises(fleet_mod.EnrollmentError) as exc_info:
            fleet_mod.consume_enrollment_token(
                session,
                composite_token=issue.raw_token,
                display_name="x",
                hostname=None,
                version=None,
            )
        assert "expired" in str(exc_info.value).lower()


def test_consume_rejects_unknown_token(tmp_path) -> None:
    _bootstrap_app(tmp_path)
    from driveforge.daemon.state import get_state
    state = get_state()
    with state.session_factory() as session:
        import pytest
        with pytest.raises(fleet_mod.EnrollmentError):
            fleet_mod.consume_enrollment_token(
                session,
                composite_token="deadbeef.not-a-real-token",
                display_name="x",
                hostname=None,
                version=None,
            )


def test_consume_rejects_hash_mismatch(tmp_path) -> None:
    """Malicious / buggy agent presents the right token_id prefix
    but the wrong raw half. Must be rejected in constant time."""
    _bootstrap_app(tmp_path)
    from driveforge.daemon.state import get_state
    state = get_state()
    with state.session_factory() as session:
        issue = fleet_mod.issue_enrollment_token(session, ttl_seconds=60)
    with state.session_factory() as session:
        import pytest
        with pytest.raises(fleet_mod.EnrollmentError):
            fleet_mod.consume_enrollment_token(
                session,
                composite_token=f"{issue.token_id}.wrong-raw-half",
                display_name="x",
                hostname=None,
                version=None,
            )


def test_authenticate_agent_validates_token_and_revocation(tmp_path) -> None:
    _bootstrap_app(tmp_path)
    from driveforge.daemon.state import get_state
    state = get_state()
    with state.session_factory() as session:
        issue = fleet_mod.issue_enrollment_token(session, ttl_seconds=60)
    with state.session_factory() as session:
        result = fleet_mod.consume_enrollment_token(
            session,
            composite_token=issue.raw_token,
            display_name="r720",
            hostname=None,
            version=None,
        )
    with state.session_factory() as session:
        agent = fleet_mod.authenticate_agent(session, result.api_token)
        assert agent is not None
        assert agent.id == result.agent_id
    # Revoke → authentication fails
    with state.session_factory() as session:
        fleet_mod.revoke_agent(session, result.agent_id)
    with state.session_factory() as session:
        agent = fleet_mod.authenticate_agent(session, result.api_token)
        assert agent is None


def test_authenticate_rejects_malformed_token(tmp_path) -> None:
    _bootstrap_app(tmp_path)
    from driveforge.daemon.state import get_state
    state = get_state()
    with state.session_factory() as session:
        assert fleet_mod.authenticate_agent(session, "no-dot-token") is None
        assert fleet_mod.authenticate_agent(session, "unknown.agent.id") is None


# ---------------------------------------------------- agent-side token I/O


def test_write_and_read_agent_token_roundtrip(tmp_path) -> None:
    path = tmp_path / "agent.token"
    fleet_mod.write_agent_token(path, "abc.xyz")
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600
    assert fleet_mod.read_agent_token(path) == "abc.xyz"


def test_read_agent_token_returns_none_when_missing(tmp_path) -> None:
    assert fleet_mod.read_agent_token(tmp_path / "nope") is None


# ---------------------------------------------------- /api/fleet/enroll


def test_api_enroll_happy_path_on_operator(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    with state.session_factory() as session:
        issue = fleet_mod.issue_enrollment_token(session, ttl_seconds=60)
    with TestClient(app) as client:
        resp = client.post(
            "/api/fleet/enroll",
            json={
                "token": issue.raw_token,
                "display_name": "r720",
                "hostname": "driveforge-r720",
                "version": "0.10.0",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "agent_id" in body
    assert "api_token" in body
    assert body["operator_version"]  # whatever __version__ is
    with state.session_factory() as session:
        agent = session.get(m.Agent, body["agent_id"])
        assert agent is not None
        assert agent.display_name == "r720"


def test_api_enroll_rejects_when_role_is_standalone(tmp_path) -> None:
    """Standalone daemons must not expose the enrollment surface —
    otherwise a random DriveForge on the LAN could be tricked into
    accepting enrollments."""
    app = _bootstrap_app(tmp_path, role="standalone")
    with TestClient(app) as client:
        resp = client.post(
            "/api/fleet/enroll",
            json={
                "token": "some.token",
                "display_name": "r720",
            },
        )
    assert resp.status_code == 404


def test_api_enroll_rejects_when_role_is_agent(tmp_path) -> None:
    """Agents aren't enrollment targets either — only operators are.

    v0.10.0 expected 404 (handler-level role check). v0.10.7 added
    the agent-lockdown middleware which refuses all non-allowlisted
    POSTs with 403 BEFORE the handler runs, so the effective
    refusal code shifted. Both are valid "agents don't serve this"
    responses; accept either so we don't regress on either path."""
    app = _bootstrap_app(tmp_path, role="agent")
    with TestClient(app) as client:
        resp = client.post(
            "/api/fleet/enroll",
            json={
                "token": "some.token",
                "display_name": "r720",
            },
        )
    assert resp.status_code in (403, 404)


def test_api_enroll_rejects_bad_token_with_400(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    with TestClient(app) as client:
        resp = client.post(
            "/api/fleet/enroll",
            json={
                "token": "bogus.token",
                "display_name": "r720",
            },
        )
    assert resp.status_code == 400


# ---------------------------------------------------- /settings/agents


def test_settings_agents_page_on_standalone_shows_flip_prompt(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="standalone")
    with TestClient(app) as client:
        resp = client.get("/settings/agents")
    assert resp.status_code == 200
    body = resp.text
    # Banner tells operator to flip to operator mode
    assert "standalone" in body.lower()
    assert "operator" in body.lower()


def test_settings_agents_new_token_redirects_with_raw_token(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    with TestClient(app) as client:
        resp = client.post("/settings/agents/new-token", follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/settings/agents?new_token=")
    # Token is URL-encoded but the dot separator must survive
    assert "." in location or "%2E" in location


def test_settings_agents_new_token_refuses_on_standalone(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="standalone")
    with TestClient(app) as client:
        resp = client.post("/settings/agents/new-token", follow_redirects=False)
    # Non-operator roles cannot mint tokens
    assert resp.status_code == 400


def test_settings_agents_revoke_stamps_revoked_at(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    # Seed an agent
    with state.session_factory() as session:
        issue = fleet_mod.issue_enrollment_token(session, ttl_seconds=60)
    with state.session_factory() as session:
        result = fleet_mod.consume_enrollment_token(
            session,
            composite_token=issue.raw_token,
            display_name="r720",
            hostname=None,
            version=None,
        )
    with TestClient(app) as client:
        resp = client.post(
            f"/settings/agents/{result.agent_id}/revoke",
            follow_redirects=False,
        )
    assert resp.status_code == 303
    with state.session_factory() as session:
        agent = session.get(m.Agent, result.agent_id)
        assert agent.revoked_at is not None


def test_settings_agents_page_on_operator_lists_agents(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    with state.session_factory() as session:
        issue = fleet_mod.issue_enrollment_token(session, ttl_seconds=60)
    with state.session_factory() as session:
        fleet_mod.consume_enrollment_token(
            session,
            composite_token=issue.raw_token,
            display_name="r720-bench",
            hostname="driveforge-r720",
            version="0.10.0",
        )
    with TestClient(app) as client:
        resp = client.get("/settings/agents")
    assert resp.status_code == 200
    body = resp.text
    assert "r720-bench" in body
    assert "driveforge-r720" in body


# ---------------------------------------------------- list + touch helpers


def test_list_agents_orders_newest_first(tmp_path) -> None:
    _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    with state.session_factory() as session:
        issue1 = fleet_mod.issue_enrollment_token(session, ttl_seconds=60)
    with state.session_factory() as session:
        fleet_mod.consume_enrollment_token(
            session, composite_token=issue1.raw_token,
            display_name="first", hostname=None, version=None,
        )
    with state.session_factory() as session:
        issue2 = fleet_mod.issue_enrollment_token(session, ttl_seconds=60)
    with state.session_factory() as session:
        fleet_mod.consume_enrollment_token(
            session, composite_token=issue2.raw_token,
            display_name="second", hostname=None, version=None,
        )
    with state.session_factory() as session:
        agents = fleet_mod.list_agents(session)
        assert len(agents) == 2
        assert agents[0].display_name == "second"
        assert agents[1].display_name == "first"


def test_touch_agent_last_seen_updates_timestamp(tmp_path) -> None:
    _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    with state.session_factory() as session:
        issue = fleet_mod.issue_enrollment_token(session, ttl_seconds=60)
    with state.session_factory() as session:
        result = fleet_mod.consume_enrollment_token(
            session, composite_token=issue.raw_token,
            display_name="r720", hostname=None, version=None,
        )
        agent_before = session.get(m.Agent, result.agent_id)
        first_seen = agent_before.last_seen_at
    # Small delay, then touch
    import time
    time.sleep(0.01)
    with state.session_factory() as session:
        fleet_mod.touch_agent_last_seen(session, result.agent_id)
    with state.session_factory() as session:
        agent_after = session.get(m.Agent, result.agent_id)
        assert agent_after.last_seen_at >= first_seen


def test_revoke_agent_is_idempotent(tmp_path) -> None:
    _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    with state.session_factory() as session:
        issue = fleet_mod.issue_enrollment_token(session, ttl_seconds=60)
    with state.session_factory() as session:
        result = fleet_mod.consume_enrollment_token(
            session, composite_token=issue.raw_token,
            display_name="r720", hostname=None, version=None,
        )
    with state.session_factory() as session:
        assert fleet_mod.revoke_agent(session, result.agent_id) is True
        # Second call returns False (already revoked) but doesn't raise
        assert fleet_mod.revoke_agent(session, result.agent_id) is False
        # Missing agent returns False
        assert fleet_mod.revoke_agent(session, "deadbeef-missing") is False
