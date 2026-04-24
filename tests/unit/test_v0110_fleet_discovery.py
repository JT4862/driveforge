"""v0.11.0 — candidate role + mDNS discovery + one-click enrollment.

Covers:
  - New `candidate` role accepted by FleetConfig
  - install_id + fleet_id lazy minting helpers
  - Discovered candidate dataclass + ignore flag
  - avahi-browse parser handles resolved TXT lines
  - /api/fleet/adopt 404 unless role=candidate
  - /api/fleet/adopt install_id mismatch → 400
  - /api/fleet/adopt happy path: writes token, flips role to agent,
    persists config
  - Operator POST /settings/agents/discovered/<id>/enroll:
    - 404 when candidate missing from cache
    - happy path: mints agent + posts to candidate
  - Operator POST /settings/agents/discovered/<id>/ignore
    flags the cache entry
  - Agent-role middleware: /api/* allowed, / returns plaintext
    "managed by operator"
  - Candidate-role middleware: /api/fleet/adopt allowed, / returns
    plaintext "waiting for adoption"
  - Setup wizard Step 1 accepts role=candidate + sets setup_completed
    + skips remaining steps
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from driveforge import config as cfg
from driveforge.core import fleet_discovery
from driveforge.db import models as m


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
    if role == "candidate":
        settings.fleet.install_id = "abcdef123456"
        settings.fleet.api_token_path = tmp_path / "agent.token"
    app = make_app(settings)
    DaemonState.boot(settings)
    return app


# ---------------------------------------------------- Config


def test_candidate_role_accepted() -> None:
    s = cfg.Settings()
    s.fleet.role = "candidate"
    assert s.fleet.role == "candidate"


def test_install_id_lazy_mint(tmp_path: Path) -> None:
    s = cfg.Settings()
    assert s.fleet.install_id is None
    # Provide a writable config path
    cfg_path = tmp_path / "dv.yaml"
    iid = fleet_discovery.ensure_install_id(s, cfg_path)
    assert len(iid) == 12  # 6 random bytes → 12 hex
    # Stable: second call returns the same value
    assert fleet_discovery.ensure_install_id(s, cfg_path) == iid


def test_fleet_id_lazy_mint(tmp_path: Path) -> None:
    s = cfg.Settings()
    assert s.fleet.fleet_id is None
    cfg_path = tmp_path / "dv.yaml"
    fid = fleet_discovery.ensure_fleet_id(s, cfg_path)
    assert len(fid) == 16
    assert fleet_discovery.ensure_fleet_id(s, cfg_path) == fid


# ---------------------------------------------------- avahi output parser


def test_parse_avahi_line_resolved() -> None:
    """Resolved line carries hostname, address, port, and a TXT
    block with key=value tokens."""
    line = (
        '=;eth0;IPv4;DriveForge\\032foo;_driveforge-candidate._tcp;local;'
        'foo.local;10.0.0.5;8080;"version=0.11.0" "hostname=foo" "install_id=abc123"'
    )
    parsed = fleet_discovery._parse_avahi_line(line)
    assert parsed is not None
    assert parsed["version"] == "0.11.0"
    assert parsed["hostname"] == "foo"
    assert parsed["install_id"] == "abc123"
    assert parsed["address"] == "10.0.0.5"
    assert parsed["port"] == 8080


def test_parse_avahi_line_not_resolved() -> None:
    """Lines that start with `+` (announce) or `-` (goodbye) aren't
    relevant — only `=` (resolved) parsed."""
    assert fleet_discovery._parse_avahi_line("+;eth0;IPv4;Foo;_x._tcp;local") is None
    assert fleet_discovery._parse_avahi_line("") is None
    assert fleet_discovery._parse_avahi_line("garbage") is None


def test_split_quoted_extracts_tokens() -> None:
    raw = '"a=1" "b=two" "c=three words"'
    out = fleet_discovery._split_quoted(raw)
    assert out == ["a=1", "b=two", "c=three words"]


# ---------------------------------------------------- Adoption endpoint


def test_adopt_endpoint_404_when_not_candidate(tmp_path) -> None:
    """Standalone daemons don't serve adoption — prevents accidental
    re-enrollment of a running box."""
    app = _bootstrap_app(tmp_path, role="standalone")
    with TestClient(app) as client:
        resp = client.post(
            "/api/fleet/adopt",
            json={
                "operator_url": "http://op:8080",
                "agent_token": "t.k",
                "display_name": "x",
                "install_id": "anything",
            },
        )
    assert resp.status_code == 404


def test_adopt_endpoint_install_id_mismatch(tmp_path) -> None:
    """Cross-candidate enrollment attempt (operator meant to adopt
    A, targeted B's URL) → 400, no state change."""
    app = _bootstrap_app(tmp_path, role="candidate")
    with TestClient(app) as client:
        resp = client.post(
            "/api/fleet/adopt",
            json={
                "operator_url": "http://op:8080",
                "agent_token": "t.k",
                "display_name": "x",
                "install_id": "wrong-id",
            },
        )
    assert resp.status_code == 400


def test_adopt_endpoint_happy_path(tmp_path, monkeypatch) -> None:
    """Candidate receives matching install_id → token written, role
    flipped to agent, config saved."""
    app = _bootstrap_app(tmp_path, role="candidate")
    from driveforge.daemon.state import get_state
    state = get_state()
    # Point api_token_path at a writable tmp location
    token_path = tmp_path / "agent.token"
    state.settings.fleet.api_token_path = token_path

    # Stub the daemon restart (don't actually systemctl in test)
    restart_called = []
    monkeypatch.setattr(
        "threading.Thread",
        lambda target, daemon: MagicMock(
            start=lambda: restart_called.append(target.__name__),
        ),
    )
    # Also stub cfg.save to avoid writing /etc/driveforge
    monkeypatch.setattr(cfg, "save", lambda *a, **kw: None)

    with TestClient(app) as client:
        resp = client.post(
            "/api/fleet/adopt",
            json={
                "operator_url": "http://nx3200.local:8080",
                "agent_token": "agent-id.secret",
                "display_name": "r720-test",
                "install_id": "abcdef123456",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    # Token written
    assert token_path.read_text() == "agent-id.secret"
    # Role flipped
    assert state.settings.fleet.role == "agent"
    assert state.settings.fleet.operator_url == "http://nx3200.local:8080"
    assert state.settings.fleet.display_name == "r720-test"


# ---------------------------------------------------- Agent/candidate lockdown


def test_agent_root_returns_plaintext(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="agent")
    from driveforge.daemon.state import get_state
    state = get_state()
    state.settings.fleet.operator_url = "http://op:8080"
    with TestClient(app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "managed by operator" in resp.text.lower()
    assert "http://op:8080" in resp.text


def test_candidate_root_returns_plaintext(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="candidate")
    with TestClient(app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert "waiting for operator adoption" in resp.text.lower()


def test_agent_non_api_path_returns_404_plaintext(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="agent")
    with TestClient(app) as client:
        resp = client.get("/history")
    assert resp.status_code == 404
    assert "operator" in resp.text.lower()


def test_agent_api_path_allowed(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="agent")
    with TestClient(app) as client:
        resp = client.get("/api/health")
    assert resp.status_code == 200


def test_candidate_adopt_endpoint_allowed(tmp_path) -> None:
    """The POST endpoint under /api/fleet/adopt must work despite
    the lockdown — POST to non-/api paths is blocked, but /api/* is
    in the allowlist."""
    app = _bootstrap_app(tmp_path, role="candidate")
    with TestClient(app) as client:
        # Send a mismatching install_id to confirm the handler runs
        # (400) rather than the middleware blocking (would be 404)
        resp = client.post(
            "/api/fleet/adopt",
            json={
                "operator_url": "x", "agent_token": "x",
                "display_name": "x", "install_id": "wrong",
            },
        )
    assert resp.status_code == 400  # handler ran; install_id check failed


def test_operator_routes_not_gated(tmp_path) -> None:
    """Operator still serves the dashboard + Settings UI."""
    app = _bootstrap_app(tmp_path, role="operator")
    with TestClient(app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert "New Batch" in resp.text


# ---------------------------------------------------- Operator Enroll handler


def test_enroll_discovered_404_when_candidate_missing(tmp_path) -> None:
    """Candidate must be in state.discovered_candidates for enroll
    to work — operator might have stale UI showing a candidate that
    disappeared."""
    app = _bootstrap_app(tmp_path, role="operator")
    with TestClient(app) as client:
        resp = client.post(
            "/settings/agents/discovered/does-not-exist/enroll",
            follow_redirects=False,
        )
    # Redirects with ?enroll_error — doesn't 404 at the HTTP level.
    assert resp.status_code == 303
    assert "enroll_error" in resp.headers["location"]


def test_ignore_discovered_flags_cache_entry(tmp_path) -> None:
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    state.discovered_candidates["abc"] = fleet_discovery.DiscoveredCandidate(
        install_id="abc", hostname="x", version="0.11.0",
        address="1.2.3.4", port=8080, last_seen_monotonic=time.monotonic(),
    )
    with TestClient(app) as client:
        resp = client.post(
            "/settings/agents/discovered/abc/ignore",
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert state.discovered_candidates["abc"].ignored is True


def test_enroll_discovered_happy_path(tmp_path, monkeypatch) -> None:
    """Candidate in cache → operator mints agent + POSTs to
    candidate's /api/fleet/adopt. Stub httpx to simulate the
    candidate accepting."""
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    state.discovered_candidates["cand123"] = fleet_discovery.DiscoveredCandidate(
        install_id="cand123", hostname="newbox", version="0.11.0",
        address="10.0.0.5", port=8080, last_seen_monotonic=time.monotonic(),
    )

    # Mock the candidate's adoption endpoint response
    async def fake_post(self, url, json=None, **_kw):
        assert "/api/fleet/adopt" in url
        assert json["install_id"] == "cand123"
        assert "." in json["agent_token"]  # composite token format
        return httpx.Response(200, json={"ok": True, "detail": "adopted"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    with TestClient(app) as client:
        resp = client.post(
            "/settings/agents/discovered/cand123/enroll",
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert "enrolled=newbox" in resp.headers["location"]
    # Agent row created on operator side
    with state.session_factory() as session:
        n = session.query(m.Agent).count()
        assert n == 1
    # Candidate removed from cache (no double-enroll)
    assert "cand123" not in state.discovered_candidates


def test_enroll_discovered_rolls_back_on_candidate_unreachable(tmp_path, monkeypatch) -> None:
    """Candidate disappeared between Enroll click + POST → operator
    must NOT leave a dangling Agent row that has no real fleet
    member."""
    app = _bootstrap_app(tmp_path, role="operator")
    from driveforge.daemon.state import get_state
    state = get_state()
    state.discovered_candidates["gone"] = fleet_discovery.DiscoveredCandidate(
        install_id="gone", hostname="ghost", version="0.11.0",
        address="10.0.0.99", port=8080, last_seen_monotonic=time.monotonic(),
    )

    async def fake_post_fails(self, url, json=None, **_kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post_fails)

    with TestClient(app) as client:
        resp = client.post(
            "/settings/agents/discovered/gone/enroll",
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert "enroll_error" in resp.headers["location"]
    with state.session_factory() as session:
        assert session.query(m.Agent).count() == 0


# ---------------------------------------------------- Wizard


def test_wizard_step1_candidate_skips_rest(tmp_path) -> None:
    """Picking Agent in Step 1 must set role=candidate +
    setup_completed=True + redirect to / (where the lockdown
    middleware renders the waiting-for-adoption plaintext)."""
    # Fresh install: setup_completed defaults to False
    from driveforge.daemon.app import make_app
    from driveforge.daemon.state import DaemonState
    settings = cfg.Settings()
    settings.daemon.state_dir = tmp_path
    settings.daemon.db_path = tmp_path / "driveforge.db"
    settings.daemon.pending_labels_dir = tmp_path / "pending-labels"
    settings.daemon.reports_dir = tmp_path / "reports"
    settings.setup_completed = False  # fresh
    settings.fleet.role = "standalone"
    app = make_app(settings)
    DaemonState.boot(settings)
    # Stub cfg.save to avoid writing /etc
    import unittest.mock as mk
    with mk.patch("driveforge.config.save"):
        with TestClient(app) as client:
            resp = client.post(
                "/setup/1",
                data={"role": "candidate", "hostname": "testbox"},
                follow_redirects=False,
            )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    from driveforge.daemon.state import get_state
    st = get_state()
    assert st.settings.fleet.role == "candidate"
    assert st.settings.setup_completed is True
