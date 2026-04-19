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
    """Compose a cert label as a Pillow image."""
    size = LABEL_SIZES.get(roll, LABEL_SIZES["DK-1209"])
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)

    font_title = _load_font(36)
    font_body = _load_font(22)
    font_grade = _load_font(72)

    padding = 14
    draw.text((padding, padding), "DriveForge Certified", font=font_title, fill="black")
    draw.line(
        [(padding, padding + 44), (size[0] - padding, padding + 44)],
        fill="black",
        width=2,
    )

    body_y = padding + 54
    lines = [
        f"Model:    {data.model[:28]}",
        f"Capacity: {data.capacity_tb:.1f} TB",
        f"Serial:   {data.serial}",
        f"Tested:   {data.tested_date.isoformat()}",
        f"POH:      {data.power_on_hours:,} h",
    ]
    for line in lines:
        draw.text((padding, body_y), line, font=font_body, fill="black")
        body_y += 28

    # Big grade block on the right
    draw.text(
        (size[0] - 120, padding + 50),
        data.grade.upper(),
        font=font_grade,
        fill="black",
    )

    # QR code for the report URL, bottom-right corner
    qr = qrcode.QRCode(box_size=3, border=1)
    qr.add_data(data.report_url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qr_size = min(size[1] - body_y - padding, 120)
    if qr_size > 40:
        qr_img = qr_img.resize((qr_size, qr_size))
        img.paste(qr_img, (size[0] - qr_size - padding, size[1] - qr_size - padding))

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
        # Write the raw raster bytes to a .bin for inspection
        target = Path(identifier or "/tmp/driveforge-label.bin")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bytes(instructions))
        logger.info("label raster written to %s (%d bytes)", target, len(instructions))
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
