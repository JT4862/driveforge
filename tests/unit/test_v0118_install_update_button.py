"""v0.11.8 — Install Update button no longer relies on window.confirm().

JT hit a dead button on the operator after the v0.11.6 → v0.11.7
upgrade flow: clicking "Install update now" produced no popup, no
network request, nothing in the daemon journal. Root cause: modern
browsers (Safari at least, observed in JT's session) silently block
window.confirm() after repeated use as anti-spam, and the form's
`onclick="return confirm(...)"` returned undefined → form submission
cancelled. Operator was stuck with no obvious recourse.

v0.11.8 drops the JS confirm. Confirmation is already redundant on
the page (green "Update available" panel, "Restarts the daemon"
subtitle, server-side gating against active drives). Single click
fires the POST.

Tests guard the regression — the rendered settings page must NOT
contain `onclick="return confirm` on the install-update form, and
the form must still wire to /settings/install-update.
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
    settings.fleet.role = "operator"
    app = make_app(settings)
    DaemonState.boot(settings)
    return app


def _seed_update_available():
    """Populate the updates module's in-process cache so the
    Settings template renders the install-update form (gated behind
    `{% if update_info and update_info.update_available %}`)."""
    from datetime import datetime, timezone
    from driveforge.core import updates as updates_mod
    info = updates_mod.UpdateInfo(
        status="available",
        current_version="0.11.7",
        latest_version="0.11.8",
        release_url="https://example.test/release",
        release_notes="test release notes",
        checked_at=datetime.now(timezone.utc),
    )
    updates_mod._cached = info
    import time as _time
    updates_mod._cached_at = _time.monotonic()


def test_install_update_button_has_no_confirm_dialog(tmp_path) -> None:
    """The Install Update button must not gate submission on
    window.confirm() — browsers silently block it under repeated
    use, leaving the operator with a dead button."""
    app = _bootstrap_app(tmp_path)
    _seed_update_available()
    with TestClient(app) as client:
        resp = client.get("/settings")
    body = resp.text
    # Locate the install-update form so we're asserting against the
    # right block (settings.html has multiple forms).
    assert 'action="/settings/install-update"' in body
    # Find the chunk of HTML around our form and assert no
    # confirm() onclick lives inside it.
    idx = body.find('action="/settings/install-update"')
    form_chunk = body[idx:idx + 1500]
    assert "onclick=\"return confirm" not in form_chunk, (
        "Install Update button must NOT use window.confirm() — "
        "browsers eat it silently. See v0.11.8 notes."
    )
    # Sanity: the actual submit button is still present.
    assert "Install update now" in form_chunk
    assert 'type="submit"' in form_chunk


def test_install_update_form_still_posts_to_correct_endpoint(tmp_path) -> None:
    """Drop the confirm() but keep the form wired to the right
    endpoint with method=post. POST handler hasn't changed since
    v0.11.6 verified-delivery."""
    app = _bootstrap_app(tmp_path)
    _seed_update_available()
    with TestClient(app) as client:
        resp = client.get("/settings")
    body = resp.text
    # Form attribute order isn't guaranteed but both attrs must
    # appear in the same form tag.
    idx = body.find('action="/settings/install-update"')
    assert idx != -1
    # Walk back to the opening <form so we can inspect its attrs.
    form_open = body.rfind("<form", 0, idx)
    form_close = body.find(">", idx)
    form_tag = body[form_open:form_close + 1]
    assert 'method="post"' in form_tag
