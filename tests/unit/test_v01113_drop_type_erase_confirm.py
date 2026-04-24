"""v0.11.13 — remove Type ERASE to confirm from New Batch form.

JT 2026-04-24: "People that are going to use DriveForge know that
they are literally formatting hard drives, so there's no need to
type a confirmation in here. It's just annoying."

Same philosophy as v0.11.8 (dropped window.confirm() from Install
Update button — browsers silently blocked it) and v0.11.10 (swept
the pattern from all 9 other forms). The New Batch form's
typed-confirmation input was the last major friction-gate in the
app. Now:

  - The HTML form no longer renders the "Type ERASE to confirm"
    input / label
  - The server-side guard at routes.py no longer checks `confirm
    == "ERASE"`. Older clients that still send the field submit
    it harmlessly.
  - The `err == "confirm"` template branch is unreachable now
    (the server never emits ?err=confirm anymore)

Tests guard the regression:
  - Form HTML does NOT contain a confirm text input
  - POST without any confirm field succeeds (no 303-to-err)
  - POST with an old client's `confirm=ERASE` also succeeds
    (backwards compatible — older form submissions still work)
"""

from __future__ import annotations

from pathlib import Path

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


def test_form_html_no_longer_renders_confirm_input(tmp_path) -> None:
    """GET /batches/new must not contain a text input named
    "confirm" (the old typed-ERASE gate). The destructive-warning
    box itself stays — it's informational content."""
    app = _bootstrap_app(tmp_path)
    with TestClient(app) as client:
        resp = client.get("/batches/new")
    assert resp.status_code == 200
    body = resp.text
    # Destructive warning still present — this is context, not friction.
    assert "Destructive operation" in body
    assert "secure-erase" in body
    # But no typed-confirmation input field.
    assert 'name="confirm"' not in body
    assert "Type <code>ERASE</code>" not in body
    assert "Type `ERASE`" not in body


def test_post_without_confirm_field_succeeds(tmp_path, monkeypatch) -> None:
    """An operator clicking Start Batch with no confirm field in the
    form POST (the v0.11.13+ happy path) gets a 303 redirect to /,
    not a 303 to /batches/new?err=confirm."""
    app = _bootstrap_app(tmp_path)
    # Seed at least one discoverable drive so start_batch has
    # something to dispatch without falling through to
    # `drive_mod.discover()` which returns real system drives.
    from driveforge.core import drive as drive_mod_
    from driveforge.core.drive import Drive, Transport
    d = Drive(
        serial="TEST-1", model="WDC WD1000",
        capacity_bytes=1_000_000_000_000,
        device_path="/dev/sdX", transport=Transport.SATA,
    )
    monkeypatch.setattr(drive_mod_, "discover", lambda: [d])

    with TestClient(app) as client:
        resp = client.post(
            "/batches/new",
            data={"drive": "TEST-1"},  # no confirm field at all
            follow_redirects=False,
        )
    assert resp.status_code == 303
    # Must NOT redirect to the err=confirm page (that URL is dead).
    assert "err=confirm" not in resp.headers.get("location", "")


def test_post_with_legacy_confirm_field_also_succeeds(tmp_path, monkeypatch) -> None:
    """An older client that still POSTs `confirm=ERASE` submits
    harmlessly — the field is ignored by the handler."""
    app = _bootstrap_app(tmp_path)
    from driveforge.core import drive as drive_mod_
    from driveforge.core.drive import Drive, Transport
    d = Drive(
        serial="TEST-2", model="WDC WD1000",
        capacity_bytes=1_000_000_000_000,
        device_path="/dev/sdX", transport=Transport.SATA,
    )
    monkeypatch.setattr(drive_mod_, "discover", lambda: [d])

    with TestClient(app) as client:
        resp = client.post(
            "/batches/new",
            data={"drive": "TEST-2", "confirm": "ERASE"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert "err=confirm" not in resp.headers.get("location", "")


def test_post_with_wrong_confirm_value_still_succeeds(tmp_path, monkeypatch) -> None:
    """Even if a user (or old test) POSTs `confirm=whatever`, the
    handler no longer rejects it — the gate has been removed
    entirely. Pre-v0.11.13 this would 303-to-err=confirm."""
    app = _bootstrap_app(tmp_path)
    from driveforge.core import drive as drive_mod_
    from driveforge.core.drive import Drive, Transport
    d = Drive(
        serial="TEST-3", model="WDC WD1000",
        capacity_bytes=1_000_000_000_000,
        device_path="/dev/sdX", transport=Transport.SATA,
    )
    monkeypatch.setattr(drive_mod_, "discover", lambda: [d])

    with TestClient(app) as client:
        resp = client.post(
            "/batches/new",
            data={"drive": "TEST-3", "confirm": "nope"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert "err=confirm" not in resp.headers.get("location", "")
