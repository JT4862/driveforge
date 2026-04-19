"""Generate DriveForge logo assets.

Produces:
  docs/logo-mark.png     square teal icon on transparent (app favicon, universal)
  docs/logo.png          horizontal mark + wordmark, for DARK backgrounds
                         (teal "Drive" + white "Forge") — primary web GUI logo
  docs/logo-light.png    horizontal mark + wordmark, for LIGHT backgrounds
                         (teal "Drive" + dark "Forge") — for README / docs
  docs/logo-label.png    monochrome black on white, for Brother QL thermal labels

Design: a hexagonal chassis containing a stylized drive platter with a swept-back
actuator arm reaching from the upper-right edge toward the spindle hub — a clean,
recognizable HDD silhouette.
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs"

TEAL = (45, 212, 191, 255)     # #2DD4BF — bright teal, pops on #0e1116
WHITE = (255, 255, 255, 255)
DARK = (17, 24, 33, 255)        # #111821 — near-black for light-bg wordmark
BLACK = (0, 0, 0, 255)
CLEAR = (0, 0, 0, 0)
PAPER = (255, 255, 255, 255)

FONT_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"


def hexagon_points(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
    pts = []
    for i in range(6):
        angle = math.radians(60 * i)
        pts.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    return pts


def draw_mark(size: int, stroke: int, color: tuple[int, int, int, int]) -> Image.Image:
    """Square icon: hex chassis + platter circle + actuator arm + spindle."""
    img = Image.new("RGBA", (size, size), CLEAR)
    d = ImageDraw.Draw(img)

    cx = cy = size / 2
    r = size * 0.44  # hex radius

    # hex chassis outline
    hex_pts = hexagon_points(cx, cy, r)
    d.polygon(hex_pts, outline=color, fill=CLEAR, width=stroke)

    # platter: circle roughly inscribed in hex
    platter_r = r * 0.62
    d.ellipse(
        [cx - platter_r, cy - platter_r, cx + platter_r, cy + platter_r],
        outline=color,
        width=max(2, int(stroke * 0.75)),
    )

    # inner platter ring (subtle — implies tracks)
    inner_r = platter_r * 0.45
    d.ellipse(
        [cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r],
        outline=color,
        width=max(2, int(stroke * 0.45)),
    )

    # spindle: filled dot at center
    spindle_r = platter_r * 0.10
    d.ellipse(
        [cx - spindle_r, cy - spindle_r, cx + spindle_r, cy + spindle_r],
        fill=color,
    )

    # actuator arm: anchored outside platter at the upper-right, swept toward
    # (but not touching) the spindle. straight line with rounded caps + a small
    # rectangular read/write head at the inner end.
    pivot_x = cx + r * 0.78
    pivot_y = cy - r * 0.52
    head_x = cx + platter_r * 0.18
    head_y = cy - platter_r * 0.28
    arm_w = max(3, int(stroke * 0.85))
    d.line([(pivot_x, pivot_y), (head_x, head_y)], fill=color, width=arm_w)
    # pivot cap (circle at the outer end)
    cap_r = arm_w * 1.6
    d.ellipse(
        [pivot_x - cap_r, pivot_y - cap_r, pivot_x + cap_r, pivot_y + cap_r],
        fill=color,
    )
    # read/write head (small filled rotated-square at the inner end)
    head_r = arm_w * 1.1
    d.ellipse(
        [head_x - head_r, head_y - head_r, head_x + head_r, head_y + head_r],
        fill=color,
    )

    return img


def draw_wordmark_split(
    left: str,
    right: str,
    height: int,
    left_color: tuple[int, int, int, int],
    right_color: tuple[int, int, int, int],
) -> Image.Image:
    """Render 'LEFT' + 'RIGHT' with independent colors on a transparent canvas."""
    font_size = int(height * 0.72)
    font = ImageFont.truetype(FONT_BOLD, font_size)
    tracking = int(height * 0.04)
    text_upper = (left + right).upper()
    split_idx = len(left)

    tmp = Image.new("RGBA", (10, 10))
    td = ImageDraw.Draw(tmp)
    widths = [td.textbbox((0, 0), ch, font=font)[2] - td.textbbox((0, 0), ch, font=font)[0]
              for ch in text_upper]
    total_w = sum(widths) + tracking * (len(text_upper) - 1)

    pad = int(height * 0.15)
    img = Image.new("RGBA", (total_w + pad * 2, height), CLEAR)
    d = ImageDraw.Draw(img)
    x = pad
    bbox_all = d.textbbox((0, 0), "D", font=font)
    cap_h = bbox_all[3] - bbox_all[1]
    y = (height - cap_h) / 2 - bbox_all[1]
    for i, (ch, w) in enumerate(zip(text_upper, widths)):
        color = left_color if i < split_idx else right_color
        d.text((x, y), ch, fill=color, font=font)
        x += w + tracking
    return img


def compose_horizontal(mark: Image.Image, word: Image.Image, gap: int) -> Image.Image:
    h = max(mark.height, word.height)
    w = mark.width + gap + word.width
    out = Image.new("RGBA", (w, h), CLEAR)
    out.paste(mark, (0, (h - mark.height) // 2), mark)
    out.paste(word, (mark.width + gap, (h - word.height) // 2), word)
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    # 1) Square teal mark — master icon
    mark = draw_mark(size=1024, stroke=44, color=TEAL)
    mark.save(OUT / "logo-mark.png", "PNG")

    # 2) Horizontal for DARK backgrounds (web GUI)
    wm_h = 260
    mark_h = int(wm_h * 1.15)
    mark_dark = draw_mark(size=mark_h, stroke=int(mark_h / 22), color=TEAL)
    word_dark = draw_wordmark_split("Drive", "Forge", wm_h, TEAL, WHITE)
    horiz_dark = compose_horizontal(mark_dark, word_dark, gap=int(wm_h * 0.25))
    horiz_dark.save(OUT / "logo.png", "PNG")

    # 3) Horizontal for LIGHT backgrounds (README, docs)
    mark_light = draw_mark(size=mark_h, stroke=int(mark_h / 22), color=TEAL)
    word_light = draw_wordmark_split("Drive", "Forge", wm_h, TEAL, DARK)
    horiz_light = compose_horizontal(mark_light, word_light, gap=int(wm_h * 0.25))
    horiz_light.save(OUT / "logo-light.png", "PNG")

    # 4) Thermal-label (monochrome black, printable on Brother QL at 300dpi)
    label_h = 160
    label_w = 680
    label = Image.new("RGBA", (label_w, label_h), PAPER)
    mlh = int(label_h * 0.95)
    m2 = draw_mark(size=mlh, stroke=max(4, mlh // 22), color=BLACK)
    word2 = draw_wordmark_split("Drive", "Forge", int(label_h * 0.48), BLACK, BLACK)
    gap = int(label_h * 0.10)
    total_w = m2.width + gap + word2.width
    ox = (label_w - total_w) // 2
    label.paste(m2, (ox, (label_h - m2.height) // 2), m2)
    label.paste(word2, (ox + m2.width + gap, (label_h - word2.height) // 2), word2)
    label.save(OUT / "logo-label.png", "PNG")

    # 5) Dark-bg preview PNG so we can eyeball it without a browser
    preview_bg = Image.new("RGBA", (horiz_dark.width + 120, horiz_dark.height + 80),
                           (14, 17, 22, 255))  # #0e1116
    preview_bg.paste(horiz_dark, (60, 40), horiz_dark)
    preview_bg.save(OUT / "logo-preview-dark.png", "PNG")

    print("wrote:")
    for p in ("logo-mark.png", "logo.png", "logo-light.png", "logo-label.png",
              "logo-preview-dark.png"):
        fp = OUT / p
        print(f"  {fp.relative_to(ROOT)}  {fp.stat().st_size} bytes")


if __name__ == "__main__":
    main()
