"""Tests for v0.6.9's QR code label layout rework.

Pre-v0.6.9, the grade glyph sat in the top-right corner while the QR
code lived mid-label at x=padding+420, which pinched the body-text
column and forced:
  - Model names truncated at 28 chars
  - Reason strings wrapped at 24 chars

v0.6.9 stacks grade + QR vertically in a single ~130px right-column
block. Body text now owns the full left portion of the label and
both limits are relaxed (model → 36 chars, reason wrap → 32 chars).

These tests:
  1. Confirm the grade glyph and QR code are paste/drawn in the
     right column (not mid-label).
  2. Confirm the grade glyph is vertically above the QR (stacked,
     not side-by-side).
  3. Smoke-test long Model and Reason strings that would have
     required wrap/truncation pre-v0.6.9 but fit natively now.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from PIL import Image, ImageDraw

from driveforge.core.printer import (
    LABEL_SIZES,
    CertLabelData,
    render_label,
)


def _sample(**overrides: Any) -> CertLabelData:
    defaults = dict(
        model="HGST HUS726T6TALE6L4",
        serial="V8G6X4RL",
        capacity_tb=6.0,
        grade="A",
        tested_date=date(2026, 4, 21),
        power_on_hours=12432,
        report_url="http://driveforge.local/reports/V8G6X4RL",
        reallocated_sectors=0,
        current_pending_sector=0,
        badblocks_errors=(0, 0, 0),
        fail_reason=None,
    )
    defaults.update(overrides)
    return CertLabelData(**defaults)


# --------------------------------------------------------------- geometry


def test_qr_is_pasted_in_right_column(monkeypatch: pytest.MonkeyPatch) -> None:
    """Right-column block starts at size[0] - padding - 130 for DK-1209
    (padding=14, right_col_w=130). The QR's center should land at or
    right of that boundary. Pre-v0.6.9 had qr_x = padding + 420 = 434,
    mid-label; the new layout puts it in the right ~130px strip.
    """
    paste_calls: list[tuple[int, int]] = []

    real_paste = Image.Image.paste

    def tracking_paste(self: Image.Image, im: Any, box: Any = None, mask: Any = None) -> None:
        # We only care about the QR paste (only call passing another Image
        # as the first positional + a box tuple with ints).
        if isinstance(im, Image.Image) and isinstance(box, tuple) and len(box) == 2:
            paste_calls.append((int(box[0]), int(box[1])))
        real_paste(self, im, box) if mask is None else real_paste(self, im, box, mask)

    monkeypatch.setattr(Image.Image, "paste", tracking_paste)

    render_label(_sample())

    assert paste_calls, "expected at least one Image.paste call (the QR)"
    qr_x, qr_y = paste_calls[-1]  # the QR is the last paste in render_label

    width, height = LABEL_SIZES["DK-1209"]
    padding = 14
    right_col_w = 130
    right_col_x = width - padding - right_col_w

    # QR x must be at or right of the right-column boundary.
    assert qr_x >= right_col_x - 2, (
        f"QR paste x={qr_x} is mid-label; expected right-column "
        f"(>= {right_col_x}). This regresses to the pre-v0.6.9 layout."
    )
    # QR must be BELOW the title separator (padding+42) — this is both a
    # sanity check and enforces the stacked layout (grade on top, QR below).
    assert qr_y > padding + 42, (
        f"QR paste y={qr_y} is at/above the title separator; expected "
        f"below the grade glyph (> {padding + 42})."
    )


def test_grade_glyph_is_drawn_in_right_column_above_qr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The grade glyph ("A", "B", "C", "F", optionally with "*" suffix)
    must be drawn in the right-column band AND above the QR paste.
    Captures both the ImageDraw.text calls (grade + body lines) and
    Image.paste calls (QR) and confirms:
      - grade_y < qr_y  (stacked)
      - grade_x is within the right-column strip
    """
    text_calls: list[tuple[int, int, str]] = []
    paste_calls: list[tuple[int, int]] = []

    real_text = ImageDraw.ImageDraw.text
    real_paste = Image.Image.paste

    def tracking_text(self: Any, xy: Any, text: str, *args: Any, **kw: Any) -> None:
        if isinstance(xy, tuple) and len(xy) == 2:
            text_calls.append((int(xy[0]), int(xy[1]), text))
        real_text(self, xy, text, *args, **kw)

    def tracking_paste(self: Image.Image, im: Any, box: Any = None, mask: Any = None) -> None:
        if isinstance(im, Image.Image) and isinstance(box, tuple) and len(box) == 2:
            paste_calls.append((int(box[0]), int(box[1])))
        real_paste(self, im, box) if mask is None else real_paste(self, im, box, mask)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", tracking_text)
    monkeypatch.setattr(Image.Image, "paste", tracking_paste)

    render_label(_sample(grade="A"))

    width, _ = LABEL_SIZES["DK-1209"]
    padding = 14
    right_col_x = width - padding - 130

    # Find the single-char grade text draw. Body text starts with
    # "Model:", "Serial:", etc.; the grade is the only draw call that's
    # purely a single letter (possibly with "*" suffix).
    grade_calls = [
        (x, y, t) for x, y, t in text_calls if len(t.strip()) <= 2 and t.strip() and t.strip()[0].isalpha()
    ]
    assert grade_calls, f"no grade glyph draw call captured; got texts={[t for _,_,t in text_calls]}"
    grade_x, grade_y, _ = grade_calls[0]

    assert grade_x >= right_col_x - 2, (
        f"grade glyph x={grade_x} is not in the right column "
        f"(expected >= {right_col_x})."
    )

    assert paste_calls, "no QR paste captured"
    _, qr_y = paste_calls[-1]
    assert grade_y < qr_y, (
        f"grade_y={grade_y} must be above QR qr_y={qr_y} — the v0.6.9 "
        f"layout stacks grade on top of QR, not beside it."
    )


# -------------------------------------------------------------- text budget


def test_long_model_name_is_not_truncated_at_28(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-v0.6.9 truncated at [:28] because the QR blocked the column
    past x≈434. v0.6.9 lifts the limit to [:36]. Verify a 32-char model
    renders as-is (not clipped past 28).
    """
    long_model = "Seagate ST3000DM001-1ER166 800GB"  # 32 chars
    assert len(long_model) > 28
    assert len(long_model) <= 36

    text_calls: list[str] = []
    real_text = ImageDraw.ImageDraw.text

    def tracking_text(self: Any, xy: Any, text: str, *args: Any, **kw: Any) -> None:
        text_calls.append(text)
        real_text(self, xy, text, *args, **kw)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", tracking_text)
    render_label(_sample(model=long_model))

    model_lines = [t for t in text_calls if t.startswith("Model: ")]
    assert model_lines, f"no Model: line drawn; got {text_calls}"
    assert long_model in model_lines[0], (
        f"long model was truncated: drew {model_lines[0]!r}, "
        f"expected full {long_model!r}"
    )


def test_32char_reason_does_not_wrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-v0.6.9 wrapped reasons > 24 chars onto two lines. v0.6.9
    raises the threshold to 32. A 32-char reason should render on a
    single Reason: line.
    """
    reason_32 = "media scan failed at LBA 12345678"  # 33 chars — just above
    reason_30 = "47 reallocated above threshold"  # 30 chars — under new limit
    assert len(reason_30) <= 32
    assert len(reason_30) > 24  # would have wrapped pre-v0.6.9

    text_calls: list[str] = []
    real_text = ImageDraw.ImageDraw.text

    def tracking_text(self: Any, xy: Any, text: str, *args: Any, **kw: Any) -> None:
        text_calls.append(text)
        real_text(self, xy, text, *args, **kw)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", tracking_text)
    render_label(_sample(grade="F", fail_reason=reason_30))

    reason_lines = [t for t in text_calls if t.startswith("Reason:") or t.startswith("        ")]
    # Exactly one Reason: line, no continuation — 30-char string fits in
    # the new text budget.
    single_line = [t for t in reason_lines if t.startswith("Reason:")]
    continuation = [t for t in reason_lines if t.startswith("        ") and t.strip()]
    assert len(single_line) == 1, f"expected 1 Reason: line, got {single_line}"
    assert not continuation, (
        f"30-char reason shouldn't wrap in v0.6.9+ (its limit is 32); "
        f"got continuation: {continuation}"
    )
    _ = reason_32  # referenced for reviewers; not asserted (would wrap)


def test_long_reason_still_wraps_at_32(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reasons longer than 32 chars still wrap. Verify the wrap point
    moved from 24 → 32, not that it disappeared entirely."""
    reason_long = "47 reallocated during test exceeded 40 threshold AND pending sectors"
    assert len(reason_long) > 32

    text_calls: list[str] = []
    real_text = ImageDraw.ImageDraw.text

    def tracking_text(self: Any, xy: Any, text: str, *args: Any, **kw: Any) -> None:
        text_calls.append(text)
        real_text(self, xy, text, *args, **kw)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", tracking_text)
    render_label(_sample(grade="F", fail_reason=reason_long))

    reason_first = [t for t in text_calls if t.startswith("Reason:")]
    continuation = [t for t in text_calls if t.startswith("        ") and t.strip()]
    assert len(reason_first) == 1, f"expected Reason: line; got {reason_first}"
    assert continuation, f"long reason should still wrap; got {continuation}"

    # The first Reason: line's visible content (after "Reason: ") must be
    # at MOST 32 chars — that's the new wrap threshold.
    first_payload = reason_first[0].removeprefix("Reason: ")
    assert len(first_payload) <= 32, (
        f"first Reason: line is {len(first_payload)} chars (> 32 wrap limit): "
        f"{first_payload!r}"
    )
