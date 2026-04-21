"""Tests for v0.6.0's release-notes markdown → HTML render helper.

`render_release_notes_html()` feeds the Settings-page "What's changing"
preview that appears BEFORE the Install button. It's the proof point
the operator uses to decide whether to commit to an update, so the
rendering needs to handle our typical release-note shape (headings +
bullet lists + inline code + occasional fenced code blocks) correctly
and degrade gracefully when the body is missing.
"""

from __future__ import annotations

from driveforge.core.updates import UpdateInfo, render_release_notes_html


def _info(body: str | None, status: str = "available") -> UpdateInfo:
    return UpdateInfo(
        status=status,
        current_version="0.5.5",
        latest_version="0.6.0",
        release_notes=body,
    )


def test_none_info_returns_none() -> None:
    """No cached UpdateInfo at all — template should hide the preview
    card entirely, so we signal "nothing to show" with None rather
    than an empty string that the template might still try to render."""
    assert render_release_notes_html(None) is None


def test_empty_body_returns_none() -> None:
    """Cached info exists but GitHub returned no body for the release
    (rare but possible — some release-note drafts ship with only a
    tag). Still a no-op: template shouldn't render an empty preview."""
    assert render_release_notes_html(_info("")) is None
    assert render_release_notes_html(_info(None)) is None


def test_basic_markdown_renders_to_html() -> None:
    """The most common release-note shape: an H1 title + bullet list.
    This is the representative smoke test for the whole helper."""
    body = "# v0.6.0\n\n- polkit refactor\n- navbar Update Available button"
    html = render_release_notes_html(_info(body))
    assert html is not None
    assert "<h1>v0.6.0</h1>" in html
    assert "<li>polkit refactor</li>" in html
    assert "<li>navbar Update Available button</li>" in html


def test_inline_code_and_bold_preserved() -> None:
    """Release notes often reference unit names, function names, and
    file paths inline. The `code` and `strong` tags must land so the
    CSS styling kicks in rather than the operator seeing raw backticks."""
    body = "**Important:** the `driveforge-update.service` unit now runs under polkit."
    html = render_release_notes_html(_info(body))
    assert html is not None
    assert "<strong>Important:</strong>" in html
    assert "<code>driveforge-update.service</code>" in html


def test_fenced_code_block_renders() -> None:
    """v0.6.0 release notes include a fenced code block for the polkit
    rule example. The `fenced_code` extension is explicitly enabled
    in render_release_notes_html so ```-blocks render as <pre><code>
    rather than being mangled into a <p>."""
    body = (
        "Polkit rule shape:\n"
        "\n"
        "```\n"
        "polkit.addRule(function(action, subject) { ... });\n"
        "```\n"
    )
    html = render_release_notes_html(_info(body))
    assert html is not None
    assert "<pre>" in html
    assert "polkit.addRule" in html


def test_link_preserved_as_anchor() -> None:
    """Release notes often link to docs or the GitHub commit range.
    The `[text](url)` markdown must become a clickable `<a>` tag so
    the operator can follow it from inside the preview, not bail out
    to GitHub's rendered view."""
    body = "See the [v0.5.x → v0.6.0 diff](https://example.com/diff) for details."
    html = render_release_notes_html(_info(body))
    assert html is not None
    assert '<a href="https://example.com/diff">v0.5.x → v0.6.0 diff</a>' in html


def test_table_renders() -> None:
    """The `tables` extension is enabled so pipe-tables (common in our
    release notes for compare-before-after kinds of summaries) render
    as <table> rather than being left as raw pipe characters."""
    body = (
        "| field | before | after |\n"
        "| --- | --- | --- |\n"
        "| sudo | yes | **no** |\n"
    )
    html = render_release_notes_html(_info(body))
    assert html is not None
    assert "<table>" in html
    assert "<th>before</th>" in html
    assert "<td><strong>no</strong></td>" in html


def test_status_field_does_not_gate_rendering() -> None:
    """render_release_notes_html only inspects `release_notes`; it
    doesn't require `status == "available"`. Callers use the UpdateInfo
    gating separately (the navbar + banner). Keeping this pure lets
    the same helper render a preview for a forced manual re-check
    even when the daemon itself is already up to date."""
    body = "# v0.5.5\n\n- shipped earlier"
    # current tier, not "available"
    html = render_release_notes_html(_info(body, status="current"))
    assert html is not None
    assert "<h1>v0.5.5</h1>" in html
