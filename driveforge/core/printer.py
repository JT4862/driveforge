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
    model: str
    serial: str
    capacity_tb: float
    grade: str
    tested_date: date
    power_on_hours: int
    report_url: str
    quick_mode: bool = False


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


def render_label(data: CertLabelData, *, roll: str = "DK-1209") -> Image.Image:
    """Compose a cert label as a Pillow image.

    Layout (landscape DK-1209):
      +--------------------------------------------+
      | DriveForge Certified                  [A]  |
      |---------------------------------------     |
      | Model:    ...                              |
      | Capacity: 6.0 TB                           |
      | Serial:   V8G6X4RL                         |
      | Tested:   2026-04-19          [QR code]    |
      | Hours on: 12,432 h                         |
      | Wipe:     NIST 800-88 Purge + 4-pass       |
      | Scan QR to verify — printed text           |
      | alone is not authoritative.                |
      +--------------------------------------------+
    """
    size = LABEL_SIZES.get(roll, LABEL_SIZES["DK-1209"])
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)

    is_compact = size[0] < 280  # DK-1221 square-label path

    if is_compact:
        # Compact layout: QR + grade + last-4 of serial only
        font_body = _load_font(18)
        font_grade = _load_font(42)
        qr = qrcode.QRCode(box_size=3, border=1)
        qr.add_data(data.report_url)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        qr_size = min(size[1] - 50, size[0] - 80)
        qr_img = qr_img.resize((qr_size, qr_size))
        img.paste(qr_img, (6, (size[1] - qr_size) // 2))
        draw.text((qr_size + 14, 10), data.grade.upper(), font=font_grade, fill="black")
        draw.text((qr_size + 14, 60), data.serial[-6:], font=font_body, fill="black")
        return img

    font_title = _load_font(34)
    font_body = _load_font(20)
    font_grade = _load_font(72)
    font_footer = _load_font(14)

    padding = 14
    draw.text((padding, padding), "DriveForge Certified", font=font_title, fill="black")
    draw.line(
        [(padding, padding + 42), (size[0] - padding, padding + 42)],
        fill="black",
        width=2,
    )

    # Body column on the left
    body_y = padding + 52
    # Tight single-space alignment so long model strings and the Wipe line
    # don't run into the QR column. The prior 4-space padding burned ~45 px
    # of horizontal room.
    wipe_line = (
        "Wipe: NIST 800-88 Purge*"
        if data.quick_mode
        else "Wipe: NIST 800-88 + 4-pass"
    )
    lines = [
        f"Model: {data.model[:24]}",
        f"Capacity: {data.capacity_tb:.1f} TB",
        f"Serial: {data.serial}",
        f"Tested: {data.tested_date.isoformat()}",
        f"Hours on: {data.power_on_hours:,} h",
        wipe_line,
    ]
    for line in lines:
        draw.text((padding, body_y), line, font=font_body, fill="black")
        body_y += 24

    # Right column: QR in the middle, big grade letter at far right
    grade_text = data.grade.upper()
    grade_bbox = draw.textbbox((0, 0), grade_text, font=font_grade)
    grade_w = grade_bbox[2] - grade_bbox[0]
    grade_x = size[0] - grade_w - padding
    grade_y = padding + 70  # below title separator, vertically centered-ish

    # QR sits below the title, in the column between body text and grade.
    # Wide left column (380 px) so 26-char rows like
    # "Model: INTEL SSDSC2BB120G4" don't collide with the QR on the top rows
    # where they vertically overlap.
    qr = qrcode.QRCode(box_size=3, border=1)
    qr.add_data(data.report_url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qr_top = padding + 52  # just below the title separator line
    qr_x = padding + 380
    qr_size_cap = size[1] - qr_top - 32  # leave room for footer
    qr_size = min(qr_size_cap, grade_x - qr_x - 12, 150)
    qr_size = max(qr_size, 80)
    qr_img = qr_img.resize((qr_size, qr_size))
    img.paste(qr_img, (qr_x, qr_top))

    draw.text((grade_x, grade_y), grade_text, font=font_grade, fill="black")

    # Footer — the "verify via QR" guardrail
    footer = "Scan QR to verify — printed text alone is not authoritative."
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
