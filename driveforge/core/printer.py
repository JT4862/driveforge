"""Brother QL cert label rendering + dispatch.

Label rendering uses Pillow to compose a fixed-size PNG from the cert
fields, then hands it to `brother_ql` for rasterization. In dev, the
`file` backend writes the raster output to a PNG on disk so we can see
what the printed label would look like without a physical printer.

See BUILD.md → Certification Labels for the full label design.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# Label size presets. Width x height in pixels at 300dpi.
LABEL_SIZES = {
    "DK-1209": (696, 271),  # 29 x 62mm die-cut, 300dpi
    "DK-1208": (991, 464),  # 38 x 90mm die-cut
    "DK-1201": (991, 306),  # 29 x 90mm die-cut
    "DK-1221": (230, 230),  # 23 x 23mm square
    "DK-22210": (696, 300),  # continuous 29mm — length is picked at render time
}


@dataclass(frozen=True, slots=True)
class CertLabelData:
    """Payload for cert-label rendering. Fields beyond the original six
    (v0.5.2+) give the label space to show *why* a drive got the
    verdict it did — reallocated sector count, pending sectors, and
    for F drives, the primary rule that caused the failure.

    The QR code still links to the full report for drives where the
    sticker can only show the headline; the sticker's job is to answer
    "at arm's length, what's this drive worth?" — the report answers
    "give me every detail."

    Field conventions:
      - `reallocated_sectors` / `current_pending_sector` — None means
        "not captured by this run" (legacy rows, aborted runs, etc.).
        Rendered as "—" on the label.
      - `badblocks_errors` — tuple (read, write, compare) counts.
        None means badblocks didn't run (quick mode) or the column
        doesn't exist. Total rendered as one number on the sticker.
      - `fail_reason` — short human-readable reason for F drives.
        Populated by `primary_fail_reason(rules)` in the caller; None
        for A/B/C. Rendered on the label body for F drives; ignored
        for pass tiers.
    """

    model: str
    serial: str
    capacity_tb: float
    grade: str
    tested_date: date
    power_on_hours: int
    report_url: str
    quick_mode: bool = False
    # v0.5.2+ enriched-label fields.
    reallocated_sectors: int | None = None
    current_pending_sector: int | None = None
    badblocks_errors: tuple[int, int, int] | None = None
    fail_reason: str | None = None
    # v0.5.5+ healing-delta field. post_reallocated - pre_reallocated,
    # computed by the caller from the TestRun snapshots. The render
    # path prints a "Remapped N during burn-in" line on pass-tier
    # labels when this value is > 0 and falsy otherwise, so old rows
    # (None) and drives that didn't heal anything (0) both stay quiet.
    remapped_during_run: int | None = None
    # v0.5.6+ sustained throughput mean (MB/s) across the 8-pass
    # badblocks sweep. NULL on quick-pass / legacy / diskstats-failed
    # runs. Pass-tier labels print "Sustained N MB/s over 8-pass
    # burn-in" when present. Keeps the label honest — we're not
    # claiming a synthetic benchmark number, just surfacing what the
    # drive actually did during the burn-in we ran on it.
    throughput_mean_mbps: float | None = None
    # v0.6.7+ sanitization method actually used for this run. Affects
    # the Wipe: line on pass-tier labels:
    #   "secure_erase" (default)  → "Wipe: NIST 800-88 + 4-pass"
    #   "badblocks_overwrite"     → "Wipe: NIST 800-88 Clear (4-pass)"
    #   None                      → fall through to quick_mode /
    #                                secure_erase default
    # The badblocks_overwrite case is when the HDD libata-freeze
    # fallback engaged and we sanitized via 4-pattern destructive
    # write only (no ATA SECURITY ERASE UNIT). Legitimate NIST 800-88
    # Clear for magnetic media, but the label should state the method
    # honestly so a downstream auditor can trace the verdict.
    sanitization_method: str | None = None


def primary_fail_reason(rules: list[dict]) -> str | None:
    """Given a TestRun's `rules` JSON (a list of dict rule results from
    grading.py), return a short human-readable string describing the
    primary reason the drive was graded F. Returns None if no
    fail-causing rule fired — caller should treat None as "no reason
    to render" (caller probably shouldn't be calling this for an A/B/C
    drive in the first place).

    Picks the FIRST rule where `passed=False` and `forces_grade="F"`
    (or the legacy "fail" value for pre-v0.5.1 rows). Maps each
    canonical rule name to a compact label-friendly phrasing. Other
    fail-causing rules are accessible via the QR → full report; we
    only show the headline on the 29mm sticker.

    Format targets: ≤ ~40 characters so it fits one label line at the
    body font size; if it must wrap, it wraps gracefully via standard
    text-draw wrapping in `render_label`.
    """
    for rule in rules or []:
        # Some legacy rows carry Rule objects serialized as pydantic
        # model_dump() output; others come in as raw dicts. Handle both.
        passed = rule.get("passed", True)
        forces = rule.get("forces_grade")
        if passed or forces not in ("F", "fail"):
            continue
        name = rule.get("name", "")
        detail = rule.get("detail", "")
        # Canonical rule-name → label-copy map. If a new rule gets
        # added to grading.py without a mapping here, fall back to the
        # rule's own detail string (still useful, just less polished).
        if name == "smart_short_test_passed":
            return "SMART short self-test failed"
        if name == "smart_long_test_passed":
            return "SMART long self-test failed"
        if name == "badblocks_clean":
            # detail looks like "badblocks found errors: read=3 write=0 compare=0"
            import re as _re
            m = _re.search(r"read=(\d+)\s+write=(\d+)\s+compare=(\d+)", detail)
            if m:
                r, w, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
                total = r + w + c
                kind = "read" if r else ("write" if w else "compare")
                return f"{total} badblocks {kind} error{'s' if total != 1 else ''}"
            return "badblocks found errors"
        if name == "no_pending_sectors":
            # detail: "current_pending_sector=5"
            import re as _re
            m = _re.search(r"=(\d+)", detail)
            n = int(m.group(1)) if m else 0
            return f"{n} pending sector{'s' if n != 1 else ''}"
        if name == "no_offline_uncorrectable":
            import re as _re
            m = _re.search(r"=(\d+)", detail)
            n = int(m.group(1)) if m else 0
            return f"{n} offline uncorrectable"
        if name.startswith("no_degradation_"):
            # Any counter that grew during the test — label just says
            # what kind. The full delta is in the report.
            attr = name.removeprefix("no_degradation_").replace("_", " ")
            # Trim "_sector" suffix for brevity on the sticker.
            attr = attr.replace("sectors", "sect").replace("sector", "sect")
            return f"{attr} grew during test"
        if name.startswith("grade_") and "_reallocated" in name:
            # detail: "reallocated_sectors=47 > 40 (fail)"
            import re as _re
            m = _re.search(r"=(\d+)\s*>\s*(\d+)", detail)
            if m:
                return f"{m.group(1)} reallocated (> {m.group(2)})"
            return "reallocated sectors over threshold"
        # Unknown rule name — fall back to the detail as-is, truncated.
        return (detail or "failed grading rule")[:40]
    return None


def _load_font(size: int) -> ImageFont.ImageFont:
    # Try common DejaVu path; fall back to default bitmap font so rendering
    # never fails. On the R720 (Debian), DejaVu ships by default.
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
    ):
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _format_poh(hours: int) -> str:
    """Format power-on hours as "45,123 POH (5.2 y)" for the sticker.

    Pre-v0.5.2 the label said "Hours on: 12,432 h" — just the raw
    number. The years-equivalent was on the drive detail page but not
    on the sticker, forcing operators to mentally divide by 8760 at
    the rack. Now both are rendered directly.
    """
    if hours <= 0:
        return "POH: —"
    years = hours / 8760.0
    return f"POH: {hours:,} ({years:.1f} y)"


def _format_health_line(
    reallocated: int | None,
    pending: int | None,
    badblocks_errors: tuple[int, int, int] | None,
    *,
    available_chars: int = 32,
) -> str | None:
    """Compose the "Realloc · Pending · BB" line shown on pass labels
    (v0.5.2+). If all three values are unknown (legacy row or an
    aborted run), return None so the caller can skip this line
    entirely — an empty "Realloc: — · ..." row is just noise.

    If the full three-field version overflows the available character
    budget, we drop badblocks first (implicit via the grade — if it
    had errors the drive would be F, not A/B/C) and then pending.
    """
    parts: list[str] = []
    if reallocated is not None:
        parts.append(f"Realloc: {reallocated}")
    if pending is not None:
        parts.append(f"Pending: {pending}")
    if badblocks_errors is not None:
        total = sum(badblocks_errors)
        parts.append(f"BB: {total}")
    if not parts:
        return None
    full = " · ".join(parts)
    if len(full) <= available_chars:
        return full
    # Drop badblocks first
    if len(parts) == 3:
        full_trim = " · ".join(parts[:2])
        if len(full_trim) <= available_chars:
            return full_trim
    # Still too long — just the reallocated count, most important
    return parts[0] if parts else None


def render_label(data: CertLabelData, *, roll: str = "DK-1209") -> Image.Image:
    """Compose a cert label as a Pillow image.

    Two layouts, picked by `data.grade`:

    Pass tier (A/B/C) — emphasis: "this drive is certified, here's why":
      +-----------------------------------------------+
      | DriveForge Certified                          |
      |----------------------------------   [A*]      |
      | Model: Seagate ST3000DM001                    |
      | Serial: Z1F248SL · 3.0 TB       [QR code]     |
      | Tested: 2026-04-21                            |
      | POH: 45,123 (5.2 y)                           |
      | Realloc: 0 · Pending: 0 · BB: 0               |
      | Wipe: NIST 800-88 + 4-pass                    |
      | Scan QR to verify.                            |
      +-----------------------------------------------+

    Fail tier (F) — emphasis: "this drive is bad, here's the reason":
      +-----------------------------------------------+
      | DriveForge — FAIL                             |
      |----------------------------------    [F]      |
      | Model: Seagate ST3000DM001                    |
      | Serial: Z1F248SL · 3.0 TB       [QR code]     |
      | Failed: 2026-04-21                            |
      | POH: 45,123 (5.2 y)                           |
      | Reason: 47 reallocated sectors                |
      |         exceeded threshold of 40              |
      | See QR for full report.                       |
      +-----------------------------------------------+

    v0.6.9+: grade glyph + QR are stacked in a single right-column
    block so body text gets the full label width minus ~130px for
    the block. Pre-v0.6.9 had the QR mid-label which forced text
    wrapping to 24 chars.

    Pipeline-error drives (grade="error") do NOT print labels — the
    web layer refuses the print before this function is called. If
    somehow the render path IS reached for an error grade, we render
    it as an F-style label with the error message as the reason, so
    no operator ever gets a half-valid-looking sticker.
    """
    size = LABEL_SIZES.get(roll, LABEL_SIZES["DK-1209"])
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)

    is_compact = size[0] < 280  # DK-1221 square-label path
    is_fail = data.grade.upper() in ("F", "FAIL", "ERROR")

    if is_compact:
        # Compact layout (DK-1221 square): QR + grade + last-6 of serial.
        # No room for extra fields; the QR is the only link to detail.
        font_body = _load_font(18)
        font_grade = _load_font(42)
        qr = qrcode.QRCode(box_size=3, border=1)
        qr.add_data(data.report_url)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        qr_size = min(size[1] - 50, size[0] - 80)
        qr_img = qr_img.resize((qr_size, qr_size))
        img.paste(qr_img, (6, (size[1] - qr_size) // 2))
        grade_text = "F" if is_fail else data.grade.upper()
        draw.text((qr_size + 14, 10), grade_text, font=font_grade, fill="black")
        draw.text((qr_size + 14, 60), data.serial[-6:], font=font_body, fill="black")
        return img

    font_title = _load_font(34)
    font_body = _load_font(20)
    # v0.6.9+: grade glyph shrunk from 72pt → 56pt so it can stack
    # above the QR in a single ~130px right-column block (see the
    # QR-layout rework below). 56pt is still the biggest glyph on
    # the label — the visual anchor function is preserved.
    font_grade = _load_font(56)
    font_footer = _load_font(14)

    padding = 14
    title = "DriveForge — FAIL" if is_fail else "DriveForge Certified"
    draw.text((padding, padding), title, font=font_title, fill="black")
    draw.line(
        [(padding, padding + 42), (size[0] - padding, padding + 42)],
        fill="black",
        width=2,
    )

    # Right-column geometry (v0.6.9+): grade+QR form a single stacked
    # block along the right edge. Computed UP FRONT so the body-text
    # column below can use `right_col_left` as its right-edge budget
    # for wrap / truncation decisions. Pre-v0.6.9 had the QR mid-label
    # and the text column was pinched to ~24 chars to avoid overlap;
    # the stacked block frees the text column to ~36 chars.
    right_col_w = 130
    right_col_x = size[0] - padding - right_col_w
    right_col_left = right_col_x - 12  # 12px gap between text and right col

    # Body lines
    body_y = padding + 52
    tested_label = "Failed" if is_fail else "Tested"
    lines: list[str] = [
        # v0.6.9+: model truncation bumped 28 → 36 chars (extra room
        # from the QR-layout rework).
        f"Model: {data.model[:36]}",
        f"Serial: {data.serial} · {data.capacity_tb:.1f} TB",
        f"{tested_label}: {data.tested_date.isoformat()}",
        _format_poh(data.power_on_hours),
    ]

    if is_fail:
        # Reason line — the whole point of the F label variant.
        # v0.6.9+: wrap at 32 chars (up from v0.6.7's 24-char limit).
        # The QR no longer blocks the body column mid-label; the only
        # rightward constraint now is the right-column block at
        # x=right_col_left. 32 chars at 20pt fits comfortably.
        reason = data.fail_reason or "failed grading"
        if len(reason) <= 32:
            lines.append(f"Reason: {reason}")
        else:
            # Split at the first space past the midpoint so wrap
            # breaks look natural.
            mid = len(reason) // 2
            split_at = reason.find(" ", mid)
            if split_at == -1 or split_at > 32:
                split_at = 32
            lines.append(f"Reason: {reason[:split_at]}")
            lines.append(f"        {reason[split_at:].strip()}")
    else:
        # Pass tier — health counts line. Skipped if all three values
        # are unknown (keeps the label clean on legacy / aborted runs
        # that somehow got here).
        health_line = _format_health_line(
            data.reallocated_sectors,
            data.current_pending_sector,
            data.badblocks_errors,
        )
        if health_line:
            lines.append(health_line)

        # v0.5.5+ healing line \u2014 only printed when the drive actually
        # remapped sectors during our pipeline. Silent on zero-delta and
        # missing-pre-snapshot runs; those labels already have enough
        # information without a "0 healed" pseudo-line.
        if data.remapped_during_run and data.remapped_during_run > 0:
            suffix = "during quick pass" if data.quick_mode else "during burn-in"
            lines.append(f"Remapped {data.remapped_during_run} {suffix}")

        # v0.5.6+ sustained-throughput line \u2014 only printed on full
        # pipeline runs where diskstats was available. Silent on
        # quick-pass (no badblocks) and legacy rows (None).
        if data.throughput_mean_mbps is not None and not data.quick_mode:
            lines.append(f"Sustained {data.throughput_mean_mbps:.0f} MB/s over 8-pass burn-in")

        # v0.6.7+: wipe line reflects the actual sanitization method
        # used. For HDDs where the libata-freeze fallback engaged, the
        # honest label says "Clear (4-pass)" not the default
        # "NIST 800-88 + 4-pass" (which implies secure_erase too).
        if data.quick_mode:
            wipe_line = "Wipe: NIST 800-88 Purge*"
        elif data.sanitization_method == "badblocks_overwrite":
            wipe_line = "Wipe: NIST 800-88 Clear (4-pass)"
        else:
            # Default / None / "secure_erase" — ATA SECURITY ERASE UNIT
            # + 4-pass badblocks overwrite was run.
            wipe_line = "Wipe: NIST 800-88 + 4-pass"
        lines.append(wipe_line)

    for line in lines:
        draw.text((padding, body_y), line, font=font_body, fill="black")
        body_y += 24

    # Right-column block: grade glyph stacked above QR, both
    # centered within the ~130px right column reserved above.
    #
    # v0.6.9 layout rework. Previously the grade lived in the
    # top-right corner and the QR sat mid-label at padding+420,
    # which forced body text into a cramped left column (~24-char
    # wrap). Now grade + QR form a single vertical block along
    # the right edge; body text gets the full left column.
    grade_text = "F" if is_fail else data.grade.upper()
    grade_suffix = "*" if (data.quick_mode and not is_fail) else ""
    grade_display = grade_text + grade_suffix
    grade_bbox = draw.textbbox((0, 0), grade_display, font=font_grade)
    grade_w = grade_bbox[2] - grade_bbox[0]
    grade_h = grade_bbox[3] - grade_bbox[1]
    grade_x = right_col_x + (right_col_w - grade_w) // 2
    grade_y = padding + 48  # 6px below title separator at padding+42
    draw.text((grade_x, grade_y), grade_display, font=font_grade, fill="black")

    # QR directly below the grade, centered within the right column.
    # Size picks up whatever vertical space is left between the grade
    # glyph and the footer, capped at right_col_w to avoid spilling
    # sideways, floored at 96px so phone-camera scanning stays
    # reliable. DK-1209 (271 px tall) typically lands ~120-130px;
    # larger rolls (DK-1208) get proportionally bigger.
    qr = qrcode.QRCode(box_size=3, border=1)
    qr.add_data(data.report_url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qr_top = grade_y + grade_h + 8
    qr_bottom_max = size[1] - padding - 24  # leave room for footer text
    qr_size = min(right_col_w, qr_bottom_max - qr_top)
    qr_size = max(qr_size, 96)
    qr_img = qr_img.resize((qr_size, qr_size))
    qr_x = right_col_x + (right_col_w - qr_size) // 2
    img.paste(qr_img, (qr_x, qr_top))

    # Footer — slightly different wording for F labels to reinforce
    # "this is not a certification."
    footer = (
        "See QR for full fail report."
        if is_fail
        else "Scan QR to verify — printed text alone is not authoritative."
    )
    draw.text((padding, size[1] - 22), footer, font=font_footer, fill="black")

    return img


def save_label_png(img: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG")


# Brother's USB vendor ID — every QL-series printer identifies as this.
# Used by discover_usb_printer() to auto-detect the attached printer so
# operators don't need to know the USB PID to fill in
# `backend_identifier` manually. v0.6.1+.
_BROTHER_USB_VID = 0x04F9

# DriveForge-roll-name → brother_ql-label-identifier. brother_ql uses a
# "{width}" ID for continuous rolls and "{width}x{length}" for die-cut
# rolls; the printer validates the raster's label ID against the
# physical label loaded and refuses the job if they disagree. v0.6.0
# hardcoded "62" (continuous 62mm) regardless of the saved roll, so
# any die-cut label attempt hit "wrong roll type" at the printer. v0.6.1
# adds the explicit mapping so the raster matches whatever the
# operator picked in Settings.
_BROTHER_QL_LABEL_IDS: dict[str, str] = {
    "DK-1201": "29x90",      # 29mm x 90mm die-cut
    "DK-1208": "39x90",      # 38mm x 90mm die-cut (brother_ql calls it 39mm — backing included)
    "DK-1209": "62x29",      # 62mm x 29mm die-cut — the default DriveForge cert label
    "DK-1221": "23x23",      # 23mm x 23mm die-cut square
    "DK-22205": "62",        # 62mm continuous
    "DK-22210": "29",        # 29mm continuous
    "DK-22223": "50",        # 50mm continuous
}
# Default when the operator picks a roll we don't have mapped, or leaves
# the roll field blank. Continuous 62mm works on most Brother QL units
# even without a physical roll match — the printer treats it as a
# generic paper warning rather than a hard refusal for many firmwares.
_DEFAULT_BROTHER_QL_LABEL_ID = "62"


def _brother_ql_label_id(roll: str | None) -> str:
    """Return the brother_ql label identifier matching a DriveForge roll
    name. Falls back to continuous 62mm for unknown / unmapped roll
    values so a new DK-*** SKU added to the Settings dropdown without
    a corresponding entry here degrades to "try 62mm" rather than
    crashing the print flow."""
    if not roll:
        return _DEFAULT_BROTHER_QL_LABEL_ID
    return _BROTHER_QL_LABEL_IDS.get(roll, _DEFAULT_BROTHER_QL_LABEL_ID)


def discover_usb_printer() -> str | None:
    """Scan connected USB devices for a Brother QL printer and return
    a brother_ql-format identifier string like ``usb://0x04f9:0x209d``.

    Returns None if:
      - pyusb isn't installed (dev laptop without the optional dep)
      - No Brother USB device is present (printer unplugged)
      - The scan fails for any reason (permissions, etc.)

    Pre-v0.6.1 the operator had to paste this identifier into the
    ``backend_identifier`` field manually, but the Settings UI didn't
    expose that field at all — so the saved config sat at
    ``backend_identifier: null`` and brother_ql's pyusb backend blew
    up trying to parse the empty string as hex. This helper removes
    that configuration dance entirely: `usb` connection with no saved
    identifier means "find me the first Brother printer on the bus."

    If multiple Brother printers are connected, the first one found
    wins. In practice that's a non-issue (nobody has two QL printers
    on one DriveForge host). A future release could expose a picker
    if it matters.
    """
    try:
        import usb.core  # type: ignore[import-not-found]
    except ImportError:
        logger.debug("pyusb not available; USB printer discovery disabled")
        return None
    try:
        dev = usb.core.find(idVendor=_BROTHER_USB_VID)
    except Exception:  # noqa: BLE001
        logger.exception("USB discovery failed")
        return None
    if dev is None:
        return None
    # brother_ql expects both VID and PID in lowercase hex with the
    # `0x` prefix, colon-separated, prefixed with `usb://`.
    return f"usb://0x{_BROTHER_USB_VID:04x}:0x{dev.idProduct:04x}"


def print_label(
    img: Image.Image,
    *,
    model: str,
    backend: str = "file",
    identifier: str | None = None,
    roll: str | None = None,
) -> tuple[bool, str]:
    """Print a label. `backend=file` dumps raster output to disk (dev mode).

    Returns ``(ok, message)``. On success, ``message`` is a short
    human-readable status; on failure, it's the reason suitable for
    surfacing to the operator in a Settings banner. Pre-v0.6.1 this
    returned a bare bool — the string form carries the specific
    failure reason (unknown printer model, pyusb discovery failed,
    wrong-roll rejection from the printer, etc.) through to the UI
    without spilling a raw 500 traceback.

    ``roll`` (v0.6.1+) is the DriveForge roll name (e.g. ``DK-1209``)
    and gets translated to brother_ql's label identifier via
    ``_brother_ql_label_id``. Pre-v0.6.1 the brother_ql label was
    hardcoded to ``"62"`` (continuous 62mm) so every die-cut print
    attempt hit "wrong roll type" at the physical printer.

    In production with a real printer: ``backend='pyusb'`` + a
    discovered USB identifier (see ``discover_usb_printer``). If
    ``identifier`` is falsy and ``backend='pyusb'``, auto-discover
    rather than passing an empty string to brother_ql (which would
    hit ``invalid literal for int() with base 16: ''`` deep in pyusb).
    """
    try:
        from brother_ql.backends.helpers import send  # type: ignore[import-untyped]
        from brother_ql.conversion import convert  # type: ignore[import-untyped]
        from brother_ql.exceptions import BrotherQLUnknownModel  # type: ignore[import-untyped]
        from brother_ql.raster import BrotherQLRaster  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("brother_ql not installed; label printing disabled")
        return (False, "brother_ql not installed")

    try:
        qlr = BrotherQLRaster(model)
    except BrotherQLUnknownModel:
        logger.error("unknown Brother QL model %r in printer config", model)
        return (
            False,
            f"unknown printer model {model!r} — pick a valid model in Settings → Printer",
        )
    qlr.exception_on_warning = True
    label_id = _brother_ql_label_id(roll)
    instructions = convert(
        qlr=qlr,
        images=[img],
        label=label_id,
        rotate="auto",
        threshold=70.0,
        dither=False,
        compress=False,
        red=False,
        dpi_600=False,
        hq=True,
        cut=True,
    )
    if backend == "file":
        # Dev-mode preview: save the rendered label as a viewable PNG next to
        # the raw Brother QL raster bytes. The PNG is what the operator
        # actually wants to see; the .bin is for protocol-level debugging.
        target_bin = Path(identifier or "/tmp/driveforge-label.bin")
        target_bin.parent.mkdir(parents=True, exist_ok=True)
        target_bin.write_bytes(bytes(instructions))
        target_png = target_bin.with_suffix(".png")
        img.save(target_png, "PNG")
        logger.info(
            "label dev-preview written: %s (%d bytes raster) + %s (PNG)",
            target_bin, len(instructions), target_png,
        )
        return (True, f"dev-preview written to {target_png}")
    # v0.6.1+: auto-discover the USB identifier when the operator picked
    # `usb` in Settings but hasn't (can't — no UI field) filled in a
    # backend_identifier. Without this, pyusb's backend hits
    # `invalid literal for int() with base 16: ''` on the empty string.
    effective_identifier = identifier
    if backend == "pyusb" and not effective_identifier:
        effective_identifier = discover_usb_printer()
        if not effective_identifier:
            return (
                False,
                "no Brother USB printer detected — check the cable and `lsusb`",
            )
        logger.info("auto-discovered USB printer at %s", effective_identifier)
    try:
        send(
            instructions=instructions,
            printer_identifier=effective_identifier or "",
            backend_identifier=backend,
            blocking=True,
        )
        return (True, "label dispatched to printer")
    except Exception as exc:  # noqa: BLE001
        logger.error("label print failed: %s", exc)
        return (False, f"printer dispatch failed: {exc}")


def build_cert_label_data_from_run(drive, run, *, report_url: str) -> CertLabelData:
    """Build CertLabelData for a completed pipeline run.

    Shared by the Settings/drive-detail print path (which gets its
    report_url from the incoming HTTP request) and the v0.6.4+
    auto-print path (which synthesizes the URL from daemon settings
    because no request object exists at pipeline finalization).

    `run.rules` is the pydantic-serialized grading rules list; it's
    parsed here for badblocks error counts and the primary fail
    reason (for F-tier drives). Caller provides `report_url` so the
    QR code on the sticker resolves to the full report page.
    """
    # badblocks errors live in the grading rule output, not directly on
    # TestRun. Parse them out when the `badblocks_clean` rule is present.
    badblocks: tuple[int, int, int] | None = None
    for rule in (run.rules or []):
        if rule.get("name") == "badblocks_clean":
            import re as _re
            m = _re.search(
                r"read=(\d+)\s+write=(\d+)\s+compare=(\d+)",
                rule.get("detail", ""),
            )
            if m:
                badblocks = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            elif rule.get("passed"):
                badblocks = (0, 0, 0)
            break

    fail_reason = primary_fail_reason(run.rules or [])

    # v0.5.5+ healing delta. Only meaningful when both pre and post
    # snapshots are present (pre is NULL on legacy pre-v0.5.5 rows).
    remapped = None
    if (
        run.reallocated_sectors is not None
        and run.pre_reallocated_sectors is not None
    ):
        remapped = run.reallocated_sectors - run.pre_reallocated_sectors

    from datetime import UTC, datetime
    return CertLabelData(
        model=drive.model,
        serial=drive.serial,
        capacity_tb=round(drive.capacity_bytes / 1_000_000_000_000, 2),
        grade=run.grade or "—",
        tested_date=(run.completed_at or run.started_at or datetime.now(UTC)).date(),
        power_on_hours=run.power_on_hours_at_test or 0,
        report_url=report_url,
        quick_mode=bool(run.quick_mode),
        reallocated_sectors=run.reallocated_sectors,
        current_pending_sector=run.current_pending_sector,
        badblocks_errors=badblocks,
        fail_reason=fail_reason,
        remapped_during_run=remapped,
        throughput_mean_mbps=run.throughput_mean_mbps,
        # v0.6.7+: pulled onto the label so the Wipe: line reflects
        # the honest method (secure_erase vs 4-pass overwrite only).
        # None on pre-v0.6.7 rows → label falls back to the default
        # NIST 800-88 + 4-pass wording (backward compatible).
        sanitization_method=getattr(run, "sanitization_method", None),
    )


# DriveForge-connection → brother_ql backend-id. Duplicated here rather
# than reaching into web/routes.py so core/ stays free of web deps.
_BROTHER_QL_BACKENDS = {
    "usb": "pyusb",
    "network": "network",
    "bluetooth": "bluetooth",
    "file": "file",
}


def auto_print_cert_for_run(state, drive, run) -> tuple[bool, str]:
    """Render + print a completed run's cert label without an HTTP
    request. v0.6.4+, called from `orchestrator._finalize_run` when
    the drive has a grade AND a printer is configured AND the
    operator has enabled `printer.auto_print`.

    Returns `(ok, message)`. On success the message is a short status
    ("printed cert label for <serial>"). On failure the message
    carries the specific reason (no printer configured, render failed,
    dispatch failed) for surfacing in the drive card log — but the
    orchestrator does NOT fail the run over a print error: the drive's
    grade stands, only the sticker didn't print, and operator can
    click Print Label manually once the printer issue is fixed.

    Synthesizes the QR-code report URL from the daemon's configured
    tunnel hostname (preferred, resolves externally) or from the
    daemon's bind host+port (internal only — operators' phones on
    the same LAN can still scan it).
    """
    pc = state.settings.printer
    if not pc.model:
        return (False, "auto-print skipped: no printer configured")
    if not getattr(pc, "auto_print", True):
        # auto_print toggle is off — not a failure, just not an attempt.
        return (False, "auto-print disabled in Settings")

    # Build the QR URL. Prefer the tunnel hostname so the sticker's QR
    # resolves from anywhere; fall back to LAN-only URL using the
    # daemon's bind config. The bind host might be 0.0.0.0 (any), in
    # which case we use the box's hostname (via avahi .local suffix)
    # since operators probably reach the dashboard that way too.
    tun = state.settings.integrations.cloudflare_tunnel_hostname
    if tun:
        if not tun.startswith(("http://", "https://")):
            tun = f"https://{tun}"
        report_url = f"{tun.rstrip('/')}/reports/{drive.serial}"
    else:
        host = state.settings.daemon.host
        port = state.settings.daemon.port
        if host in ("0.0.0.0", "::", "*", ""):
            # Guess a reachable name. `hostname -s` + .local is the
            # mDNS path every DriveForge install advertises on.
            import socket
            host = f"{socket.gethostname()}.local"
        report_url = f"http://{host}:{port}/reports/{drive.serial}"

    try:
        data = build_cert_label_data_from_run(drive, run, report_url=report_url)
        img = render_label(data, roll=pc.label_roll or "DK-1209")
    except Exception as exc:  # noqa: BLE001
        logger.exception("auto-print: label render failed for %s", drive.serial)
        return (False, f"auto-print render failed: {exc}")

    backend = _BROTHER_QL_BACKENDS.get(pc.connection, "file")
    ok, print_msg = print_label(
        img,
        model=pc.model,
        backend=backend,
        identifier=pc.backend_identifier,
        roll=pc.label_roll,
    )
    if not ok:
        return (False, f"auto-print dispatch failed: {print_msg}")
    return (True, f"auto-printed cert label for {drive.serial}")


def render_test_label(*, roll: str = "DK-1209") -> Image.Image:
    """Render a sentinel label for the Settings → Printer Test Print
    button. Uses the same ``render_label`` pipeline as real cert labels
    so layout regressions are caught before they hit a drive run —
    but with a dummy CertLabelData payload (sentinel serial, grade A,
    stand-in POH, QR pointing at the DriveForge repo). v0.6.1+.

    Purpose: lets the operator verify the printer is configured and
    the USB/network/Bluetooth backend is working without having to
    wait for a completed drive run. Also serves as the mechanism for
    v1.0's "Brother QL hardware test" validation gate.
    """
    data = CertLabelData(
        model="TEST DRIVE",
        serial="TEST-PRINT",
        capacity_tb=1.0,
        grade="A",
        tested_date=date.today(),
        power_on_hours=12345,
        report_url="https://github.com/JT4862/driveforge",
        quick_mode=False,
        reallocated_sectors=0,
        current_pending_sector=0,
        badblocks_errors=(0, 0, 0),
        fail_reason=None,
        remapped_during_run=0,
        throughput_mean_mbps=180.0,
    )
    return render_label(data, roll=roll)


def render_to_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
