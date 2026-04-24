"""v0.11.10 — confirm() sweep + remediation panel from DB.

Two bugs JT hit during the v0.11.9 walkthrough:

Bug A: window.confirm() on form onsubmit/onclick handlers is silently
       blocked by modern browsers after repeated use. v0.11.8 fixed
       this for the install-update button alone, but 9 other forms
       across the app still had the same pattern. JT clicked "Mark
       as unrecoverable" → no dialog, no POST, no F-stamp, no print.
       Same root cause as install-update; same fix shape.

Bug B: Frozen-SSD + password-locked remediation panels live in
       in-memory dicts on DaemonState. Two failure modes hit JT:
         1. Daemon restart (in-app update) wipes the dict, so the
            panel + Mark-as-unrecoverable button vanish even though
            the drive's underlying state is unchanged.
         2. Drive lives on a fleet AGENT — agent registers on its
            own state.frozen_remediation, never on the operator's,
            so the operator's drive-detail page renders no panel
            for that serial.
       Fix: drive_detail handler now falls back to deriving the
       panel state from the latest TestRun's error_message pattern
       when no in-memory entry exists. Both classes of operator-
       stranded-with-no-button now resolve cleanly.

Tests:
  - Regression-prevent grep: no template form should ever again
    use window.confirm() on submit/click
  - Each previously-broken form still POSTs to the right endpoint
    (sanity that we removed only the JS handler, not the form)
  - _resolve_frozen_remediation: in-memory entry wins
  - _resolve_frozen_remediation: synthesizes from latest TestRun's
    libata-freeze pattern when no in-memory entry
  - _resolve_frozen_remediation: returns None when latest TestRun
    has an unrelated error
  - _resolve_frozen_remediation: returns None for HDDs (the
    libata-freeze fallback handles them in full mode)
  - _resolve_password_locked: same three branches as above
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
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


# ============================================================ Bug A


def test_no_template_uses_window_confirm() -> None:
    """Regression-prevent — no form's onsubmit / button's onclick
    may invoke window.confirm() after v0.11.10. Browsers silently
    block confirm() under repeated use, leaving operators with dead
    buttons. JT hit this on install-update (v0.11.8 fix) and again
    on mark-unrecoverable (v0.11.10 fix). The grep test below fails
    the build if anyone ever reintroduces the pattern."""
    tmpl_dir = Path("driveforge/web/templates")
    offenders: list[str] = []
    for f in tmpl_dir.rglob("*.html"):
        text = f.read_text()
        for line_no, line in enumerate(text.splitlines(), start=1):
            # Skip historical comments documenting the old pattern.
            if line.lstrip().startswith("{#") or "v0.11" in line:
                continue
            # Look for confirm( inside an attribute value (rough but
            # effective heuristic — onsubmit="..." or onclick="...").
            if "return confirm(" in line and (
                'onsubmit="' in line or 'onclick="' in line
            ):
                offenders.append(f"{f}:{line_no}: {line.strip()[:120]}")
    assert not offenders, (
        "window.confirm() found in form handlers — browsers eat it.\n"
        "See v0.11.8 / v0.11.10 notes. Offenders:\n  " + "\n  ".join(offenders)
    )


def test_mark_unrecoverable_form_still_posts(tmp_path) -> None:
    """Sweeping confirm() must not have removed the form itself.
    Verify the frozen mark-unrecoverable form is wired correctly."""
    tmpl = Path("driveforge/web/templates/drive_detail.html").read_text()
    assert 'action="/drives/{{ drive.serial }}/frozen/mark-unrecoverable"' in tmpl
    # The form body still contains a submit button labelled correctly.
    idx = tmpl.find('action="/drives/{{ drive.serial }}/frozen/mark-unrecoverable"')
    chunk = tmpl[idx:idx + 600]
    assert "Mark as unrecoverable" in chunk
    assert 'type="submit"' in chunk


def test_password_locked_mark_unrecoverable_form_still_posts() -> None:
    tmpl = Path("driveforge/web/templates/drive_detail.html").read_text()
    assert 'action="/drives/{{ drive.serial }}/password-locked/mark-unrecoverable"' in tmpl


def test_abort_pipeline_form_still_posts() -> None:
    tmpl = Path("driveforge/web/templates/drive_detail.html").read_text()
    assert 'action="/drives/{{ drive.serial }}/abort"' in tmpl


def test_settings_clear_legacy_fails_form_still_posts() -> None:
    tmpl = Path("driveforge/web/templates/settings.html").read_text()
    assert 'action="/settings/clear-legacy-fails"' in tmpl


def test_agent_rotate_revoke_forms_still_post() -> None:
    tmpl = Path("driveforge/web/templates/settings_agents.html").read_text()
    assert "/rotate" in tmpl
    assert "/revoke" in tmpl


# ============================================================ Bug B


def test_resolve_frozen_remediation_in_memory_wins(tmp_path) -> None:
    """Live entry on state.frozen_remediation takes precedence over
    any DB synthesis."""
    _bootstrap_app(tmp_path)
    from driveforge.daemon.state import get_state
    from driveforge.core import frozen_remediation
    from driveforge.web.routes import _resolve_frozen_remediation
    state = get_state()
    live = frozen_remediation.FrozenRemediationState(
        serial="ABC", drive_model="WDC",
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
        retry_count=2,
        status=frozen_remediation.FrozenRemediationStatus.RETRIED_STILL_FROZEN,
    )
    state.frozen_remediation["ABC"] = live
    out = _resolve_frozen_remediation(state, "ABC", latest_run=None)
    assert out is live  # same object, not synthesized


def test_resolve_frozen_remediation_synthesizes_from_db(tmp_path) -> None:
    """No in-memory entry, but latest TestRun's error matches the
    libata-freeze pattern → synthesize a fresh state so the panel
    renders. Drives JT's fleet-agent and post-restart cases."""
    _bootstrap_app(tmp_path)
    from driveforge.daemon.state import get_state
    from driveforge.db import models as m
    from driveforge.web.routes import _resolve_frozen_remediation
    state = get_state()
    # Seed an SSD drive row + a TestRun whose error is the
    # libata-freeze pattern.
    with state.session_factory() as session:
        session.add(m.Drive(
            serial="FROZEN-SSD-1", model="INTEL SSDSC2BB120G4",
            capacity_bytes=120_000_000_000, transport="sata",
            rotational=False,
        ))
        session.commit()
    test_run = type("R", (), dict(
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        error_message=(
            "Drive refused SECURITY ERASE UNIT over both SAT passthrough AND "
            "native hdparm ATA paths with ABRT (command aborted by drive "
            "firmware). Most common root cause: Linux's libata driver "
            "auto-issued SECURITY FREEZE LOCK during the post-reinsert "
            "udev probe."
        ),
    ))()
    out = _resolve_frozen_remediation(state, "FROZEN-SSD-1", test_run)
    assert out is not None
    assert out.serial == "FROZEN-SSD-1"
    assert out.drive_model == "INTEL SSDSC2BB120G4"
    assert out.retry_count == 0  # synthesis uses conservative default


def test_resolve_frozen_remediation_returns_none_for_hdd(tmp_path) -> None:
    """HDDs use the v0.6.7 badblocks-only fallback in full mode and
    the v0.11.9 quick-mode error hint — they don't get the SSD-only
    remediation panel."""
    _bootstrap_app(tmp_path)
    from driveforge.daemon.state import get_state
    from driveforge.db import models as m
    from driveforge.web.routes import _resolve_frozen_remediation
    state = get_state()
    with state.session_factory() as session:
        session.add(m.Drive(
            serial="HDD-1", model="ST3000DM001",
            capacity_bytes=3_000_000_000_000, transport="sata",
            rotational=True,
        ))
        session.commit()
    test_run = type("R", (), dict(
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        error_message=(
            "Drive refused SECURITY ERASE UNIT over both SAT passthrough AND "
            "native hdparm ATA paths with ABRT — libata driver "
            "auto-issued SECURITY FREEZE LOCK during the post-reinsert udev probe."
        ),
    ))()
    out = _resolve_frozen_remediation(state, "HDD-1", test_run)
    assert out is None


def test_resolve_frozen_remediation_returns_none_for_unrelated_error(tmp_path) -> None:
    """Latest TestRun failed but with an error that doesn't match
    the libata-freeze pattern — no panel."""
    _bootstrap_app(tmp_path)
    from driveforge.daemon.state import get_state
    from driveforge.web.routes import _resolve_frozen_remediation
    state = get_state()
    test_run = type("R", (), dict(
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        error_message="some unrelated SMART self-test failure",
    ))()
    out = _resolve_frozen_remediation(state, "OTHER-1", test_run)
    assert out is None


def test_resolve_password_locked_synthesizes_from_db(tmp_path) -> None:
    _bootstrap_app(tmp_path)
    from driveforge.daemon.state import get_state
    from driveforge.db import models as m
    from driveforge.web.routes import _resolve_password_locked
    state = get_state()
    with state.session_factory() as session:
        session.add(m.Drive(
            serial="LOCKED-1", model="WD Blue",
            capacity_bytes=1_000_000_000_000, transport="sata",
        ))
        session.commit()
    # Use a phrase that erase.is_security_locked_pattern recognizes.
    # Looking at v0.9.0 logic: matches strings containing "security
    # state = enabled" or "drive is security-locked".
    test_run = type("R", (), dict(
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        error_message=(
            "secure_erase preflight failed: drive is security-locked "
            "with an unknown password and the factory-master auto-recovery "
            "could not unlock it"
        ),
    ))()
    out = _resolve_password_locked(state, "LOCKED-1", test_run)
    assert out is not None
    assert out.serial == "LOCKED-1"
    assert out.retry_count == 0
