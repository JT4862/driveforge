"""v0.10.6 — fleet token ownership + loud error surface.

Discovered during v0.10.5 real-hardware walkthrough: the CLI writes
/etc/driveforge/agent.token as root (via `sudo driveforge fleet join`),
so without an explicit chown the file lands `root:root 0600` and the
`User=driveforge` daemon unit can't read it. The agent-side
fleet_client silently died inside the lifespan task on PermissionError.

This patch:
  - write_agent_token chowns to driveforge:driveforge when run as root
    and the user exists
  - read_agent_token distinguishes "missing" (None) from "unreadable"
    (AgentTokenUnreadable raised)
  - FleetClient.run catches AgentTokenUnreadable and logs loudly

Covers:
  - read_agent_token returns None when file absent
  - read_agent_token returns content when file readable
  - read_agent_token raises AgentTokenUnreadable on PermissionError
  - write_agent_token writes mode 0600 content
  - write_agent_token doesn't crash on non-root
  - write_agent_token skips chown cleanly if 'driveforge' user absent
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from driveforge.core import fleet as fleet_mod


def test_read_agent_token_returns_none_when_missing(tmp_path: Path) -> None:
    assert fleet_mod.read_agent_token(tmp_path / "nope") is None


def test_read_agent_token_returns_content(tmp_path: Path) -> None:
    p = tmp_path / "t"
    fleet_mod.write_agent_token(p, "abc.xyz")
    assert fleet_mod.read_agent_token(p) == "abc.xyz"


def test_read_agent_token_raises_on_permission_error(tmp_path: Path, monkeypatch) -> None:
    """File exists but process can't read it → AgentTokenUnreadable
    with an actionable message. Symptom pre-v0.10.6 was a silent
    lifespan crash."""
    p = tmp_path / "t"
    p.write_text("dummy")

    def _raise_permission(*_a, **_kw):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_text", _raise_permission)
    with pytest.raises(fleet_mod.AgentTokenUnreadable) as exc_info:
        fleet_mod.read_agent_token(p)
    # Error message must tell the operator how to fix it
    assert "chown" in str(exc_info.value).lower()
    assert "driveforge" in str(exc_info.value).lower()


def test_write_agent_token_sets_mode_600(tmp_path: Path) -> None:
    p = tmp_path / "t"
    fleet_mod.write_agent_token(p, "hello")
    assert (p.stat().st_mode & 0o777) == 0o600
    assert p.read_text() == "hello"


def test_write_agent_token_noop_chown_when_not_root(tmp_path: Path, monkeypatch) -> None:
    """Dev machines, CI runners, etc. — whenever not root, chown is
    skipped cleanly. Pre-v0.10.6 this code path didn't exist at all;
    we just want to make sure adding the chown didn't break non-root."""
    p = tmp_path / "t"
    # Simulate non-root even if tests happen to run as root somewhere
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    fleet_mod.write_agent_token(p, "content")
    assert p.read_text() == "content"


def test_write_agent_token_skips_missing_daemon_user(tmp_path: Path, monkeypatch) -> None:
    """If the chown would target a user that doesn't exist (exotic
    install, dev environment), fall through gracefully with the file
    still written. Operator can chown by hand later if they care."""
    import pwd as pwd_mod
    p = tmp_path / "t"
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    def _no_such_user(_name):
        raise KeyError("no such user: driveforge")

    monkeypatch.setattr(pwd_mod, "getpwnam", _no_such_user)
    # Also stub chown so if somehow it gets called, we notice
    chown_called = []
    monkeypatch.setattr(os, "chown", lambda *a: chown_called.append(a))
    fleet_mod.write_agent_token(p, "x")
    assert p.read_text() == "x"
    assert chown_called == []


def test_write_agent_token_chowns_when_root_with_user(tmp_path: Path, monkeypatch) -> None:
    """The intended-production path: root user + `driveforge` user
    exists → chown called with the right uid/gid."""
    import pwd as pwd_mod
    p = tmp_path / "t"
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    fake_pw = MagicMock()
    fake_pw.pw_uid = 999
    fake_pw.pw_gid = 998
    monkeypatch.setattr(pwd_mod, "getpwnam", lambda _n: fake_pw)

    chown_calls: list[tuple] = []
    monkeypatch.setattr(os, "chown", lambda *a: chown_calls.append(a))
    fleet_mod.write_agent_token(p, "content")
    # Called once with the expected uid/gid
    assert len(chown_calls) == 1
    _path_arg, uid, gid = chown_calls[0]
    assert uid == 999
    assert gid == 998


def test_agent_token_unreadable_class_exists() -> None:
    """Regression guard: the new exception class is part of the
    module's public surface."""
    assert hasattr(fleet_mod, "AgentTokenUnreadable")
    assert issubclass(fleet_mod.AgentTokenUnreadable, Exception)


# ---------------------------------------------------- v0.10.6 snapshot bug


def _bootstrap_app(tmp_path, *, role: str = "agent"):
    from driveforge import config as cfg
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


def test_snapshot_does_not_include_db_only_drives(tmp_path: Path, monkeypatch) -> None:
    """v0.10.6 fix: agent's snapshot sends PRESENT drives, not every
    drive ever enrolled. Pre-fix the R720's entire historical drive
    list showed up on the operator's dashboard as 'installed', which
    was wildly misleading.

    Setup: seed DB with 5 historical drives, make discover() return
    an empty list (no drives plugged in), build snapshot — should
    be empty."""
    _bootstrap_app(tmp_path, role="agent")
    from driveforge.core import drive as drive_mod_
    from driveforge.daemon.fleet_client import FleetClient
    from driveforge.daemon.state import get_state
    from driveforge.db import models as m
    state = get_state()
    with state.session_factory() as session:
        for i in range(5):
            session.add(m.Drive(
                serial=f"HISTORICAL-{i}", model="WD Blue",
                capacity_bytes=1_000_000_000_000, transport="sata",
            ))
        session.commit()
    # No drives currently plugged in
    monkeypatch.setattr(drive_mod_, "discover", lambda: [])
    client = FleetClient(state)
    snap = client._build_snapshot()
    # Pre-v0.10.6 this would have been 5 (all the historical rows).
    assert len(snap.drives) == 0


def test_snapshot_includes_discovered_drives(tmp_path: Path, monkeypatch) -> None:
    """Present drives (lsblk says so) appear in the snapshot even
    when the DB hasn't seen them yet (fresh install)."""
    _bootstrap_app(tmp_path, role="agent")
    from driveforge.core import drive as drive_mod_
    from driveforge.core.drive import Drive, Transport
    from driveforge.daemon.fleet_client import FleetClient
    from driveforge.daemon.state import get_state
    state = get_state()

    fresh = Drive(
        serial="FRESH-1",
        model="Seagate ST1000",
        capacity_bytes=1_000_000_000_000,
        device_path="/dev/sdx",
        transport=Transport.SATA,
    )
    monkeypatch.setattr(drive_mod_, "discover", lambda: [fresh])

    client = FleetClient(state)
    snap = client._build_snapshot()
    serials = [d.serial for d in snap.drives]
    assert serials == ["FRESH-1"]
    assert snap.drives[0].model == "Seagate ST1000"


def test_snapshot_includes_active_even_if_lsblk_missed_it(
    tmp_path: Path, monkeypatch,
) -> None:
    """A drive in active_phase but temporarily absent from lsblk
    (kernel hotplug race window) must still be reported upstream
    so the operator sees ongoing pipeline progress."""
    _bootstrap_app(tmp_path, role="agent")
    from driveforge.core import drive as drive_mod_
    from driveforge.daemon.fleet_client import FleetClient
    from driveforge.daemon.state import get_state
    from driveforge.db import models as m
    state = get_state()
    monkeypatch.setattr(drive_mod_, "discover", lambda: [])
    # Seed DB + active_phase for the same serial
    with state.session_factory() as session:
        session.add(m.Drive(
            serial="ACTIVE-RACE", model="WD",
            capacity_bytes=2_000_000_000_000, transport="sata",
        ))
        session.commit()
    state.active_phase["ACTIVE-RACE"] = "badblocks"
    state.active_percent["ACTIVE-RACE"] = 40.0

    client = FleetClient(state)
    snap = client._build_snapshot()
    assert len(snap.drives) == 1
    assert snap.drives[0].serial == "ACTIVE-RACE"
    assert snap.drives[0].phase == "badblocks"
    assert snap.drives[0].percent == 40.0
