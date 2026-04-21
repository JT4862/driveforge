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
      +--------------------------------------------+
      | DriveForge Certified                 [A*]  |
      |---------------------------------------     |
      | Model: Seagate ST3000DM001                 |
      | Serial: Z1F248SL · 3.0 TB       [QR code]  |
      | Tested: 2026-04-21                         |
      | POH: 45,123 (5.2 y)                        |
      | Realloc: 0 · Pending: 0 · BB: 0            |
      | Wipe: NIST 800-88 + 4-pass                 |
      | Scan QR to verify.                         |
      +--------------------------------------------+

    Fail tier (F) — emphasis: "this drive is bad, here's the reason":
      +--------------------------------------------+
      | DriveForge — FAIL                    [F]   |
      |---------------------------------------     |
      | Model: Seagate ST3000DM001                 |
      | Serial: Z1F248SL · 3.0 TB       [QR code]  |
      | Failed: 2026-04-21                         |
      | POH: 45,123 (5.2 y)                        |
      | Reason: 47 reallocated                     |
      |         (> 40 threshold)                   |
      | See QR for full report.                    |
      +--------------------------------------------+

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
    font_grade = _load_font(72)
    font_footer = _load_font(14)

    padding = 14
    title = "DriveForge — FAIL" if is_fail else "DriveForge Certified"
    draw.text((padding, padding), title, font=font_title, fill="black")
    draw.line(
        [(padding, padding + 42), (size[0] - padding, padding + 42)],
        fill="black",
        width=2,
    )

    # Body lines
    body_y = padding + 52
    tested_label = "Failed" if is_fail else "Tested"
    lines: list[str] = [
        f"Model: {data.model[:28]}",
        f"Serial: {data.serial} · {data.capacity_tb:.1f} TB",
        f"{tested_label}: {data.tested_date.isoformat()}",
        _format_poh(data.power_on_hours),
    ]

    if is_fail:
        # Reason line — the whole point of the F label variant. Wrap
        # at ~26 chars so long reasons still fit the left column
        # without overlapping the QR.
        reason = data.fail_reason or "failed grading (see report)"
        if len(reason) <= 28:
            lines.append(f"Reason: {reason}")
        else:
            # Split roughly at the first space past the midpoint so
            # wrap breaks look natural.
            mid = len(reason) // 2
            split_at = reason.find(" ", mid)
            if split_at == -1:
                split_at = 28
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

        wipe_line = (
            "Wipe: NIST 800-88 Purge*"
            if data.quick_mode
            else "Wipe: NIST 800-88 + 4-pass"
        )
        lines.append(wipe_line)

    for line in lines:
        draw.text((padding, body_y), line, font=font_body, fill="black")
        body_y += 24

    # Right column: QR and the big grade letter.
    grade_text = "F" if is_fail else data.grade.upper()
    grade_suffix = "*" if (data.quick_mode and not is_fail) else ""
    grade_display = grade_text + grade_suffix
    grade_bbox = draw.textbbox((0, 0), grade_display, font=font_grade)
    grade_w = grade_bbox[2] - grade_bbox[0]
    grade_x = size[0] - grade_w - padding
    grade_y = padding + 70  # below title separator, vertically centered-ish

    # QR — same geometry as before, just now the body might extend
    # another line or two. The wide left column (380 px) prevents
    # collisions up to 28-char model strings.
    qr = qrcode.QRCode(box_size=3, border=1)
    qr.add_data(data.report_url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qr_top = padding + 52
    qr_x = padding + 380
    qr_size_cap = size[1] - qr_top - 32
    qr_size = min(qr_size_cap, grade_x - qr_x - 12, 150)
    qr_size = max(qr_size, 80)
    qr_img = qr_img.resize((qr_size, qr_size))
    img.paste(qr_img, (qr_x, qr_top))

    draw.text((grade_x, grade_y), grade_display, font=font_grade, fill="black")

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


def print_label(img: Image.Image, *, model: str, backend: str = "file", identifier: str | None = None) -> bool:
    """Print a label. `backend=file` dumps raster output to disk (dev mode).

    In production with a real printer: backend='pyusb' + identifier like
    'usb://0x04f9:0x209c'. See brother_ql docs.
    """
    try:
        from brother_ql.backends.helpers import send  # type: ignore[import-untyped]
        from brother_ql.conversion import convert  # type: ignore[import-untyped]
        from brother_ql.raster import BrotherQLRaster  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("brother_ql not installed; label printing disabled")
        return False

    qlr = BrotherQLRaster(model)
    qlr.exception_on_warning = True
    instructions = convert(
        qlr=qlr,
        images=[img],
        label="62",
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
        return True
    try:
        send(
            instructions=instructions,
            printer_identifier=identifier or "",
            backend_identifier=backend,
            blocking=True,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("label print failed: %s", exc)
        return False


def render_to_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
