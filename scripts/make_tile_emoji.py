"""Generate the 37 Meadow Mahjong tile emoji (spec §7.1) — reproducibly.

128×128 PNGs, ivory tile face, suit-colored glyphs (Dots blue, Bams green,
Craks red), designed to stay readable at Discord's 22px inline size: one
dominant glyph per tile, no fine detail. Output goes to ``assets/tile_emoji/``
as ``mm_<code>.png`` (the codes are ``Tile.code`` plus ``back``), which is
also the name each emoji is registered under (scripts/register_tile_emoji.py).

Run from the repo root:

    python scripts/make_tile_emoji.py [--out assets/tile_emoji]

The set is data, not art history — rerun to reskin. Committed PNGs are the
artifacts of record; the script needs a local bold-capable font (Noto Sans VF
or DejaVu) and says so plainly if none is found.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bot_modules.games.mahjong.tiles import Tile  # noqa: E402

SIZE = 128
RADIUS = 22

IVORY = (248, 244, 232, 255)
IVORY_EDGE = (201, 191, 168, 255)
SHADOW = (222, 214, 194, 255)
BLUE = (29, 95, 184, 255)      # Dots
GREEN = (30, 138, 76, 255)     # Bams
RED = (194, 55, 46, 255)       # Craks
SLATE = (58, 67, 86, 255)      # Winds
PURPLE = (123, 63, 191, 255)   # Joker
PINK = (214, 83, 138, 255)     # Flower petals
GOLD = (218, 165, 32, 255)     # Flower center (house goldenrod)
BACK_GREEN = (30, 77, 59, 255)
BACK_LINE = (52, 110, 86, 255)

SUIT_COLOR = {"d": BLUE, "b": GREEN, "c": RED}
SUIT_LETTER = {"d": "D", "b": "B", "c": "C"}
WIND_LETTER = {"wn": "N", "we": "E", "ww": "W", "ws": "S"}

_FONT_CANDIDATES = [
    "/usr/share/fonts/google-noto-vf/NotoSans[wght].ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def load_font(px: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            font = ImageFont.truetype(path, px)
            try:  # variable font: ask for bold
                font.set_variation_by_axes([700])
            except OSError:
                pass  # static bold file — already bold
            return font
    raise SystemExit(
        "No usable bold font found — install Noto Sans VF or DejaVu Sans Bold, "
        f"or add its path to _FONT_CANDIDATES ({_FONT_CANDIDATES})"
    )


def tile_face(back: bool = False) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if back:
        d.rounded_rectangle((4, 4, SIZE - 4, SIZE - 4), RADIUS, fill=BACK_GREEN,
                            outline=(20, 51, 39, 255), width=4)
    else:
        # a slightly darker south edge reads as tile thickness at any size
        d.rounded_rectangle((4, 10, SIZE - 4, SIZE - 4), RADIUS, fill=SHADOW)
        d.rounded_rectangle((4, 4, SIZE - 4, SIZE - 10), RADIUS, fill=IVORY,
                            outline=IVORY_EDGE, width=3)
    return img, d


def center_text(d: ImageDraw.ImageDraw, xy: tuple[float, float], text: str,
                px: int, fill) -> None:
    d.text(xy, text, font=load_font(px), fill=fill, anchor="mm")


def draw_suited(tile: Tile) -> Image.Image:
    img, d = tile_face()
    color = SUIT_COLOR[tile.suit or "d"]
    center_text(d, (SIZE / 2, 52), str(tile.rank), 78, color)
    letter = SUIT_LETTER[tile.suit or "d"]
    # suit pip row: letter plus a tiny shape so suits differ by more than hue
    center_text(d, (SIZE / 2 + 14, 98), letter, 34, color)
    cx = SIZE / 2 - 18
    if tile.suit == "d":       # dot: ring
        d.ellipse((cx - 10, 88, cx + 10, 108), outline=color, width=5)
    elif tile.suit == "b":     # bam: stick
        d.rounded_rectangle((cx - 5, 86, cx + 5, 110), 4, fill=color)
        d.line((cx - 5, 98, cx + 5, 98), fill=IVORY, width=3)
    else:                      # crak: three strokes
        for i, y in enumerate((88, 97, 106)):
            d.line((cx - 11, y, cx + 11, y), fill=color, width=5)
    return img


def draw_wind(tile: Tile) -> Image.Image:
    img, d = tile_face()
    center_text(d, (SIZE / 2, 62), WIND_LETTER[tile.code], 88, SLATE)
    return img


def draw_dragon(tile: Tile) -> Image.Image:
    img, d = tile_face()
    if tile is Tile.SOAP:  # the classic open frame — doubles as the zero
        d.rounded_rectangle((34, 30, SIZE - 34, SIZE - 34), 10,
                            outline=BLUE, width=9)
    else:
        color = RED if tile is Tile.RED else GREEN
        center_text(d, (SIZE / 2, 62), "R" if tile is Tile.RED else "G", 88, color)
    return img


def draw_flower(_: Tile) -> Image.Image:
    img, d = tile_face()
    cx, cy, r = SIZE / 2, 60, 21
    import math
    for k in range(5):
        a = math.tau * k / 5 - math.pi / 2
        px, py = cx + 26 * math.cos(a), cy + 26 * math.sin(a)
        d.ellipse((px - r, py - r, px + r, py + r), fill=PINK)
    d.ellipse((cx - 13, cy - 13, cx + 13, cy + 13), fill=GOLD)
    return img


def draw_joker(_: Tile) -> Image.Image:
    img, d = tile_face()
    center_text(d, (SIZE / 2, 58), "J", 84, PURPLE)
    # a small star so a joker never reads as "J-something" suited
    import math
    cx, cy, r1, r2 = 92.0, 96.0, 14.0, 6.0
    pts = []
    for k in range(10):
        r = r1 if k % 2 == 0 else r2
        a = math.tau * k / 10 - math.pi / 2
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    d.polygon(pts, fill=GOLD)
    return img


def draw_back() -> Image.Image:
    img, _ = tile_face(back=True)
    lattice = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lattice)
    for off in range(-SIZE, SIZE * 2, 22):  # diagonal lattice
        ld.line((off, 4, off + SIZE, SIZE - 4), fill=BACK_LINE, width=3)
        ld.line((off + SIZE, 4, off, SIZE - 4), fill=BACK_LINE, width=3)
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (8, 8, SIZE - 8, SIZE - 8), RADIUS - 4, fill=255
    )
    img.paste(lattice, (0, 0), Image.composite(lattice.getchannel("A"),
                                               Image.new("L", (SIZE, SIZE), 0), mask))
    return img


def render(tile: Tile) -> Image.Image:
    if tile.is_suited:
        return draw_suited(tile)
    if tile.is_wind:
        return draw_wind(tile)
    if tile.is_dragon:
        return draw_dragon(tile)
    if tile.is_flower:
        return draw_flower(tile)
    return draw_joker(tile)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(PROJECT_ROOT / "assets" / "tile_emoji"))
    ap.add_argument("--montage", help="also write an all-tiles contact sheet here")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    images: dict[str, Image.Image] = {t.code: render(t) for t in Tile}
    images["back"] = draw_back()
    for code, img in images.items():
        img.save(out / f"mm_{code}.png")
    print(f"wrote {len(images)} tiles to {out}")

    if args.montage:
        cols = 10
        rows = (len(images) + cols - 1) // cols
        pad = 8
        cell = SIZE // 2  # montage at half size ≈ closer to chat scale
        sheet = Image.new("RGBA", (cols * (cell + pad) + pad, rows * (cell + pad) + pad),
                          (54, 57, 63, 255))  # Discord dark background
        for i, (code, img) in enumerate(images.items()):
            x = pad + (i % cols) * (cell + pad)
            y = pad + (i // cols) * (cell + pad)
            sheet.paste(img.resize((cell, cell)), (x, y), img.resize((cell, cell)))
        sheet.save(args.montage)
        print(f"montage: {args.montage}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
