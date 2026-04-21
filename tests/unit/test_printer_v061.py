"""Tests for v0.6.1's printer improvements.

Three failure modes bit real hardware during v0.6.0 validation when the
first physical Brother QL got connected:

  1. The Settings dropdown offered `QL-820NWBc` / `QL-1110NWBc`, which
     brother_ql's library rejects (`BrotherQLUnknownModel` → raw 500).
  2. `backend_identifier` was always `null` in saved config (no UI field
     to fill it in), and brother_ql's pyusb backend blew up parsing an
     empty identifier string.
  3. `print_label()` returned a bare `bool`, so the UI couldn't surface
     the specific failure reason to the operator.

These tests cover the v0.6.1 fixes for all three, plus the new
test-print rendering helper.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from driveforge.core import printer as printer_mod


# ──────────────────────────── print_label ────────────────────────────

def _dummy_image():
    """A real 1x1 PIL image — brother_ql's `convert()` tries to open
    whatever's passed as an image, so a MagicMock trips its
    NotImplementedError path. A tiny real image costs nothing and lets
    convert() run through to send() for the cases where we're testing
    the send layer."""
    from PIL import Image

    return Image.new("RGB", (696, 271), "white")


def test_unknown_model_returns_clean_error_tuple() -> None:
    """The smoking-gun v0.6.0 regression: `BrotherQLUnknownModel` must
    surface to the caller as a `(False, "...")` tuple with a helpful
    message, not as a raw exception that bubbles up to a 500. Without
    this, picking any unsupported model in the Settings dropdown (or
    a saved-but-invalid value like the pre-v0.6.1 `QL-820NWBc` typo)
    crashes the Print Label flow with no actionable error."""
    ok, msg = printer_mod.print_label(
        _dummy_image(),
        model="DOES-NOT-EXIST",
        backend="file",
        identifier="/tmp/irrelevant.bin",
    )
    assert ok is False
    assert "unknown printer model" in msg.lower()
    assert "DOES-NOT-EXIST" in msg


def test_file_backend_returns_success_tuple(tmp_path) -> None:
    """File backend is the dev preview — no hardware, no USB. Previously
    returned True; now returns (True, message). This is a basic
    positive-path smoke test that the tuple wrapping didn't break
    dev-mode label preview."""
    target = tmp_path / "preview.bin"
    ok, msg = printer_mod.print_label(
        _dummy_image(),
        model="QL-800",
        backend="file",
        identifier=str(target),
    )
    assert ok is True
    assert "preview" in msg.lower() or "dispatched" in msg.lower()
    assert target.exists()


def test_pyusb_backend_auto_discovers_when_identifier_missing() -> None:
    """v0.6.1's central fix: when `connection=usb` and no
    `backend_identifier` is saved (which is the default — the UI
    doesn't even expose that field), the send() call must not receive
    an empty string as `printer_identifier` (that's what hit
    `invalid literal for int() with base 16: ''` on the R720).
    discover_usb_printer() runs instead, and if a Brother device is
    found, its identifier is threaded through."""
    sent_identifier = {}

    def fake_send(*, printer_identifier, instructions, backend_identifier, blocking):
        sent_identifier["value"] = printer_identifier

    with patch("driveforge.core.printer.discover_usb_printer", return_value="usb://0x04f9:0x209d"), \
         patch("brother_ql.backends.helpers.send", side_effect=fake_send):
        ok, _msg = printer_mod.print_label(
            _dummy_image(),
            model="QL-820NWB",
            backend="pyusb",
            identifier=None,  # the common case — no saved identifier
        )
    assert ok is True
    assert sent_identifier["value"] == "usb://0x04f9:0x209d"


def test_pyusb_backend_surfaces_no_printer_detected() -> None:
    """When `discover_usb_printer()` returns None (no Brother device
    on the bus — printer unplugged or wrong USB port), surface a
    clean message. Pre-v0.6.1 this path hit pyusb's "empty hex
    string" error buried inside brother_ql and the operator saw only
    "dispatch failed"."""
    with patch("driveforge.core.printer.discover_usb_printer", return_value=None):
        ok, msg = printer_mod.print_label(
            _dummy_image(),
            model="QL-820NWB",
            backend="pyusb",
            identifier=None,
        )
    assert ok is False
    assert "no Brother USB printer detected" in msg


def test_pyusb_backend_honors_explicit_identifier() -> None:
    """If an identifier is explicitly passed (e.g. a future UI field
    for picking between multiple printers, or an operator who
    edited the YAML by hand), the auto-discover path must NOT
    override it. The explicit value wins."""
    sent_identifier = {}

    def fake_send(*, printer_identifier, instructions, backend_identifier, blocking):
        sent_identifier["value"] = printer_identifier

    # discover_usb_printer would return the bus-found printer; the explicit
    # identifier must be used in preference even when they differ.
    with patch("driveforge.core.printer.discover_usb_printer", return_value="usb://0x04f9:0x2049"), \
         patch("brother_ql.backends.helpers.send", side_effect=fake_send):
        ok, _msg = printer_mod.print_label(
            _dummy_image(),
            model="QL-820NWB",
            backend="pyusb",
            identifier="usb://0x04f9:0x209d",  # explicit — should win
        )
    assert ok is True
    assert sent_identifier["value"] == "usb://0x04f9:0x209d"


def test_send_exception_returns_error_tuple_with_reason() -> None:
    """When send() raises — USB cable yanked mid-print, printer out of
    labels, whatever — the tuple's message carries the exception
    string so the operator sees the actual reason in the Settings
    banner, not a generic "dispatch failed."""
    with patch(
        "brother_ql.backends.helpers.send",
        side_effect=OSError("usb device disappeared"),
    ):
        ok, msg = printer_mod.print_label(
            _dummy_image(),
            model="QL-800",
            backend="pyusb",
            identifier="usb://0x04f9:0x2042",
        )
    assert ok is False
    assert "dispatch failed" in msg
    assert "usb device disappeared" in msg


# ──────────────────────── discover_usb_printer ────────────────────────

def test_discover_returns_formatted_identifier() -> None:
    """Happy path: pyusb finds a Brother device, we return the
    brother_ql-format identifier string. The caller threads this
    through to send() without further parsing."""
    fake_dev = SimpleNamespace(idProduct=0x209D)
    with patch("usb.core.find", return_value=fake_dev):
        ident = printer_mod.discover_usb_printer()
    assert ident == "usb://0x04f9:0x209d"


def test_discover_returns_none_when_no_printer_attached() -> None:
    """No Brother device on the bus → None, not an exception. Callers
    (print_label) check for None and surface a friendly "no printer
    detected" message."""
    with patch("usb.core.find", return_value=None):
        assert printer_mod.discover_usb_printer() is None


def test_discover_survives_usb_core_exception() -> None:
    """Some USB permission failures raise deep from libusb; we must
    not crash the whole print flow just because discovery blew up.
    Return None and let the caller handle it."""
    with patch("usb.core.find", side_effect=OSError("libusb unavailable")):
        assert printer_mod.discover_usb_printer() is None


# ───────────────────────── render_test_label ─────────────────────────

def test_render_test_label_produces_a_pil_image() -> None:
    """The Settings → Test Print button funnels through render_test_label.
    The output must be a PIL Image the printer backend can consume —
    if rendering itself fails (bad roll name, font fallback, etc.) the
    whole test-print flow breaks before it ever reaches the printer."""
    img = printer_mod.render_test_label(roll="DK-1209")
    # PIL Image duck-typing — has `save` and `.size`
    assert hasattr(img, "save")
    assert hasattr(img, "size")
    # DK-1209 is 696x271 per LABEL_SIZES; the Pillow rotation logic in
    # render_label may flip these, so check both orderings.
    assert sorted(img.size) == [271, 696]


def test_render_test_label_accepts_all_known_rolls() -> None:
    """Every roll that appears in the Settings dropdown must render
    cleanly — a rendering bug on a specific DK-*** size would break
    the test-print flow for any operator with that roll loaded."""
    for roll in ("DK-1209", "DK-1208", "DK-1201", "DK-1221", "DK-22210"):
        img = printer_mod.render_test_label(roll=roll)
        assert hasattr(img, "save"), f"render_test_label failed for {roll}"


# ────────────────────── brother_ql label-ID mapping ──────────────────────

@pytest.mark.parametrize(
    "roll,expected_label_id",
    [
        ("DK-1209", "62x29"),   # 62mm x 29mm die-cut — default cert label
        ("DK-1201", "29x90"),   # 29mm x 90mm die-cut
        ("DK-1208", "39x90"),   # 38mm x 90mm die-cut (brother_ql's "39" naming)
        ("DK-1221", "23x23"),   # 23mm x 23mm die-cut square
        ("DK-22210", "29"),     # 29mm continuous
    ],
)
def test_brother_ql_label_id_map(roll: str, expected_label_id: str) -> None:
    """Each DriveForge roll name must map to the brother_ql label
    identifier that matches the physical label. Pre-v0.6.1 this was
    hardcoded to "62" (continuous 62mm) so any die-cut label got
    rejected at the printer with "wrong roll type." Regression
    protection for each mapping."""
    assert printer_mod._brother_ql_label_id(roll) == expected_label_id


def test_brother_ql_label_id_map_default_for_unknown() -> None:
    """An operator who picks a roll we haven't mapped yet (future
    DK-*** SKU, typo, or "") gets the continuous-62 default rather
    than a KeyError. Keeps the print flow graceful under roll-list
    drift."""
    assert printer_mod._brother_ql_label_id(None) == "62"
    assert printer_mod._brother_ql_label_id("") == "62"
    assert printer_mod._brother_ql_label_id("DK-9999-UNKNOWN") == "62"


def test_print_label_threads_correct_label_id_for_die_cut_roll() -> None:
    """End-to-end: when the operator has DK-1209 saved, the brother_ql
    `convert()` call must receive `label="62x29"`, not `label="62"`.
    Without this the physical printer rejects the job. This is the
    direct regression protection for the bug JT hit on the R720
    during v0.6.0 validation."""
    convert_calls = {}

    def fake_convert(*, qlr, images, label, **kwargs):
        convert_calls["label"] = label
        return b"fake-raster-bytes"

    def fake_send(*, printer_identifier, instructions, backend_identifier, blocking):
        pass

    with patch("brother_ql.conversion.convert", side_effect=fake_convert), \
         patch("brother_ql.backends.helpers.send", side_effect=fake_send):
        ok, _msg = printer_mod.print_label(
            _dummy_image(),
            model="QL-820NWB",
            backend="pyusb",
            identifier="usb://0x04f9:0x209d",
            roll="DK-1209",
        )
    assert ok is True
    assert convert_calls["label"] == "62x29", (
        f"DK-1209 must yield brother_ql label '62x29', got {convert_calls['label']!r}"
    )


# ──────────────────────── brother_ql model list ────────────────────────

def test_settings_dropdown_models_all_valid_brother_ql_identifiers() -> None:
    """The v0.6.0 regression root cause: the Settings dropdown offered
    model identifiers that brother_ql's library doesn't know about
    (`QL-820NWBc`, `QL-1110NWBc`), so saving + printing raised
    `BrotherQLUnknownModel` on every attempt.

    Keep the dropdown list in sync with brother_ql's accepted
    identifiers by instantiating BrotherQLRaster for each dropdown
    value. If this test ever fails, it means someone added a model
    to the template that brother_ql doesn't support — don't ship it.
    """
    from brother_ql.raster import BrotherQLRaster
    from brother_ql.exceptions import BrotherQLUnknownModel

    # Kept in sync manually with the <option value="..."> list in
    # driveforge/web/templates/settings.html's Printer panel. Any
    # model added to the template MUST also be added here AND pass
    # this test against the installed brother_ql. v0.6.1 dropped
    # QL-1100 / QL-1110NWB — brother_ql's library doesn't recognize
    # them; adding them back requires either a brother_ql upgrade
    # that adds support or picking a different (supported) model.
    DROPDOWN_MODELS = ["QL-800", "QL-810W", "QL-820NWB"]
    for model in DROPDOWN_MODELS:
        try:
            BrotherQLRaster(model)
        except BrotherQLUnknownModel:
            pytest.fail(
                f"Template offers {model!r} but brother_ql rejects it — "
                f"the dropdown is drifted from brother_ql's model list. "
                f"Either rename in the template or stop offering."
            )
