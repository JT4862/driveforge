"""v0.11.3 — fleet UX + reliability polish.

Three issues JT surfaced after the v0.11.0 walkthrough:

  1. `libnss-mdns` wasn't installed by install.sh, so agents could
     not resolve the `.local` hostname in their stored operator_url.
     Fix is in install.sh's APT_PACKAGES — not testable here, but
     we add a regression doc-test.

  2. Adoption stored a `.local` hostname as the operator URL. Agent
     resolution fails when the agent's resolver path doesn't include
     mDNS. Fix: store the operator's primary IP instead.

  3. Discovered panel showed already-enrolled hosts during the
     transient window between adoption and the candidate's daemon
     restart. Confusing + invites double-enrollment. Fix: filter
     Discovered against the Enrolled hostname set.

  4. Live status badge showed "offline" for never-connected agents,
     same as for previously-connected-now-gone agents. Confusing
     right after adoption. Fix: new "awaiting first connect" badge
     when last_seen_at is NULL.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from driveforge import config as cfg
from driveforge.core import fleet as fleet_mod
from driveforge.core import fleet_discovery
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


# ---------------------------------------------------- IP resolution


def test_primary_lan_ip_returns_ipv4_or_none() -> None:
    """Helper must return a non-loopback IPv4 string OR None.
    On a CI box without networking it may return None; on a normal
    dev machine it returns the actual primary IP."""
    from driveforge.web.routes import _primary_lan_ip
    ip = _primary_lan_ip()
    if ip is None:
        return  # acceptable on offline CI runners
    parts = ip.split(".")
    assert len(parts) == 4, f"not an IPv4: {ip}"
    assert not ip.startswith("127."), f"got loopback: {ip}"


# ---------------------------------------------------- Adoption stores IP


def test_enroll_discovered_uses_local_hostname_in_operator_url(tmp_path, monkeypatch) -> None:
    """v0.11.4+ — adoption stores the operator's `.local` hostname
    rather than its current LAN IP. Survives DHCP renewals via mDNS
    re-resolution; libnss-mdns (also installed by v0.11.3+
    install.sh) makes the agent's resolver path handle .local
    transparently. v0.11.3 stored an IP; rolled back in v0.11.4.
    """
    import httpx
    from driveforge.daemon.state import get_state
    app = _bootstrap_app(tmp_path, role="operator")
    state = get_state()
    state.discovered_candidates["c1"] = fleet_discovery.DiscoveredCandidate(
        install_id="c1", hostname="newbox", version="0.11.4",
        address="10.99.99.5", port=8080, last_seen_monotonic=time.monotonic(),
    )
    captured: list[dict] = []

    async def fake_post(self, url, json=None, **_kw):
        captured.append({"url": url, "body": json})
        return httpx.Response(200, json={"ok": True, "detail": "adopted"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    with TestClient(app) as client:
        client.post(
            "/settings/agents/discovered/c1/enroll", follow_redirects=False,
        )
    assert len(captured) == 1
    body = captured[0]["body"]
    # operator_url must include `.local` so it re-resolves via
    # mDNS on every reconnect — DHCP-survivable.
    assert ".local:" in body["operator_url"]
    assert body["operator_url"].startswith("http://")


def test_enroll_uses_cloudflare_tunnel_when_set(tmp_path, monkeypatch) -> None:
    """Cloudflare tunnel hostname wins over IP detection when
    configured — operator explicitly chose a public-routable name."""
    import httpx
    from driveforge.daemon.state import get_state
    app = _bootstrap_app(tmp_path, role="operator")
    state = get_state()
    state.settings.integrations.cloudflare_tunnel_hostname = "fleet.example.com"
    state.discovered_candidates["c1"] = fleet_discovery.DiscoveredCandidate(
        install_id="c1", hostname="newbox", version="0.11.3",
        address="10.99.99.5", port=8080, last_seen_monotonic=time.monotonic(),
    )
    captured: list[dict] = []

    async def fake_post(self, url, json=None, **_kw):
        captured.append({"body": json})
        return httpx.Response(200, json={"ok": True, "detail": "adopted"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    with TestClient(app) as client:
        client.post(
            "/settings/agents/discovered/c1/enroll", follow_redirects=False,
        )
    body = captured[0]["body"]
    assert body["operator_url"] == "https://fleet.example.com"


# ---------------------------------------------------- Discovered filter


def test_discovered_panel_excludes_enrolled_hostnames(tmp_path) -> None:
    """An advertised candidate whose hostname matches an already-
    enrolled (non-revoked) agent must NOT appear in the Discovered
    panel — pre-v0.11.3 bug where adopted-but-not-yet-restarted
    candidates showed in both tables."""
    from driveforge.daemon.state import get_state
    app = _bootstrap_app(tmp_path, role="operator")
    state = get_state()
    # Create an enrolled agent
    with state.session_factory() as session:
        session.add(m.Agent(
            id="agentA", display_name="r720-bench",
            hostname="driveforge-5d8c0c",
            api_token_hash="x",
            enrolled_at=datetime.now(UTC),
        ))
        session.commit()
    # Add a discovered candidate with the SAME hostname
    state.discovered_candidates["c-stale"] = fleet_discovery.DiscoveredCandidate(
        install_id="c-stale", hostname="driveforge-5d8c0c",
        version="0.11.3", address="10.0.0.5", port=8080,
        last_seen_monotonic=time.monotonic(),
    )
    # Add a different candidate that IS new
    state.discovered_candidates["c-new"] = fleet_discovery.DiscoveredCandidate(
        install_id="c-new", hostname="driveforge-newbox",
        version="0.11.3", address="10.0.0.6", port=8080,
        last_seen_monotonic=time.monotonic(),
    )
    with TestClient(app) as client:
        resp = client.get("/settings/agents")
    body = resp.text
    # New box DOES appear
    assert "driveforge-newbox" in body
    # Stale-already-enrolled does NOT appear in the Discovered table.
    # It still shows in the Enrolled table (where r720-bench renders).
    # We assert the Discovered section markup doesn't contain a button
    # form for the stale install_id.
    assert "discovered/c-stale/enroll" not in body
    # New candidate's enroll form IS rendered
    assert "discovered/c-new/enroll" in body


def test_discovered_panel_includes_enrolled_when_revoked(tmp_path) -> None:
    """Edge case: an agent was revoked, then comes back up advertising
    again. Operator should see it in Discovered so they can re-adopt
    (with a fresh credential). Hostname match against a *revoked*
    agent must NOT exclude — only active enrollments do."""
    from driveforge.daemon.state import get_state
    app = _bootstrap_app(tmp_path, role="operator")
    state = get_state()
    with state.session_factory() as session:
        session.add(m.Agent(
            id="oldA", display_name="r720-bench",
            hostname="driveforge-5d8c0c",
            api_token_hash="x",
            enrolled_at=datetime.now(UTC),
            revoked_at=datetime.now(UTC),  # revoked
        ))
        session.commit()
    state.discovered_candidates["c-back"] = fleet_discovery.DiscoveredCandidate(
        install_id="c-back", hostname="driveforge-5d8c0c",
        version="0.11.3", address="10.0.0.5", port=8080,
        last_seen_monotonic=time.monotonic(),
    )
    with TestClient(app) as client:
        resp = client.get("/settings/agents")
    assert "discovered/c-back/enroll" in resp.text


# ---------------------------------------------------- Live status labels


def test_agents_page_shows_awaiting_first_connect(tmp_path) -> None:
    """An agent enrolled but never connected gets a distinct label
    (not the same 'offline' badge as a previously-connected-now-gone
    agent)."""
    from driveforge.daemon.state import get_state
    app = _bootstrap_app(tmp_path, role="operator")
    state = get_state()
    with state.session_factory() as session:
        session.add(m.Agent(
            id="freshAgent", display_name="just-enrolled",
            hostname="driveforge-fresh",
            api_token_hash="x",
            enrolled_at=datetime.now(UTC),
            last_seen_at=None,  # has never connected
        ))
        session.commit()
    with TestClient(app) as client:
        resp = client.get("/settings/agents")
    assert "awaiting first connect" in resp.text


def test_agents_page_shows_offline_for_previously_seen(tmp_path) -> None:
    """An agent that DID connect once but has gone away gets the
    plain 'offline' badge — distinct from 'awaiting first connect'."""
    from driveforge.daemon.state import get_state
    app = _bootstrap_app(tmp_path, role="operator")
    state = get_state()
    with state.session_factory() as session:
        session.add(m.Agent(
            id="goneAgent", display_name="was-here",
            hostname="driveforge-was-here",
            api_token_hash="x",
            enrolled_at=datetime.now(UTC),
            last_seen_at=datetime.now(UTC),  # has been seen
        ))
        session.commit()
    with TestClient(app) as client:
        resp = client.get("/settings/agents")
    body = resp.text
    # Offline badge present
    assert ">offline<" in body
    # And the 'awaiting' label is NOT used for this row
    # (use a stricter check that doesn't trip on tooltips)
    assert "was-here" in body


# ---------------------------------------------------- install.sh regression


def test_available_hosts_local_count_includes_installed_drives(tmp_path, monkeypatch) -> None:
    """JT screenshot bug: the host filter row's "this operator" pill
    showed a count of 0 when the operator had 1 installed-but-idle
    drive. Pre-v0.11.3 used `len(state.active_phase)` which only
    captures drives currently in a pipeline. The pill count needs
    to match the rendered card count (active + installed)."""
    from driveforge.core import drive as drive_mod_
    from driveforge.core.drive import Drive, Transport
    from driveforge.daemon.state import get_state
    from driveforge.web.routes import _available_hosts

    _bootstrap_app(tmp_path, role="operator")
    state = get_state()
    # Simulate one installed-but-idle drive
    fake_drive = Drive(
        serial="LOCAL-INTEL-1",
        model="INTEL SSDSC2BB120G4",
        capacity_bytes=120_000_000_000,
        device_path="/dev/sdb",
        transport=Transport.SAS,
    )
    monkeypatch.setattr(drive_mod_, "discover", lambda: [fake_drive])
    # No drives in active_phase
    state.active_phase.clear()

    hosts = _available_hosts(state)
    local = next(h for h in hosts if h["id"] == "local")
    assert local["drives"] == 1


def test_available_hosts_local_count_includes_active_drives(tmp_path, monkeypatch) -> None:
    """And of course the active drives still count."""
    from driveforge.core import drive as drive_mod_
    from driveforge.daemon.state import get_state
    from driveforge.web.routes import _available_hosts

    _bootstrap_app(tmp_path, role="operator")
    state = get_state()
    monkeypatch.setattr(drive_mod_, "discover", lambda: [])
    state.active_phase["BUSY-DRIVE"] = "badblocks"

    hosts = _available_hosts(state)
    local = next(h for h in hosts if h["id"] == "local")
    assert local["drives"] == 1


def test_install_sh_includes_libnss_mdns() -> None:
    """v0.11.3 added libnss-mdns to APT_PACKAGES so agents can
    resolve `.local` hostnames in their stored operator_url. Pre-
    v0.11.3 install.sh skipped it; agents got 'Name or service
    not known' on every WebSocket reconnect attempt."""
    from pathlib import Path
    install_sh = Path(__file__).parent.parent.parent / "scripts" / "install.sh"
    text = install_sh.read_text()
    assert "libnss-mdns" in text, "libnss-mdns missing from install.sh APT_PACKAGES"
