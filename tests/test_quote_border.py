"""Unit tests for the per-guild quote-border resolver + shape masking."""

from __future__ import annotations

import io
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

from PIL import Image, ImageDraw

from bot_modules.services.quote_renderer import (
    CUSTOM_BORDER_NAME,
    THEMES,
    BorderStyle,
    _MASK_CACHE,
    analyze_border_opening,
    custom_border_style,
    guild_border_dir,
    guild_border_path,
    render_quote_card,
)

W, H = 900, 500


def _save_border(tmp_path, name: str, im: Image.Image) -> BorderStyle:
    p = tmp_path / f"{name}.png"
    im.save(p)
    _MASK_CACHE.clear()
    return BorderStyle(name=name, path=p, flip=False, luma_key=False, mask_fit=True)


def _avatar() -> bytes:
    av = Image.new("RGB", (256, 256), (80, 50, 130))
    ImageDraw.Draw(av).ellipse([40, 40, 216, 216], fill=(230, 170, 70))
    buf = io.BytesIO()
    av.save(buf, "PNG")
    return buf.getvalue()


def test_border_paths_are_guild_scoped_beside_db(tmp_path):
    db = tmp_path / "sub" / "bot.db"
    d = guild_border_dir(db, 42)
    assert d == tmp_path / "sub" / "quote_borders" / "42"
    assert guild_border_path(db, 42) == d / "border.png"


def test_custom_border_style_none_when_absent(tmp_path):
    db = tmp_path / "bot.db"
    assert custom_border_style(db, 7) is None


def test_custom_border_style_present_after_write(tmp_path):
    db = tmp_path / "bot.db"
    path = guild_border_path(db, 7)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n")  # content irrelevant to resolution

    style = custom_border_style(db, 7)
    assert style is not None
    assert style.name == CUSTOM_BORDER_NAME
    assert style.path == path
    # Re-encoded uploads carry real alpha, so no flip / luma-key trickery.
    assert style.flip is False
    assert style.luma_key is False


def test_custom_border_accepts_str_db_path(tmp_path):
    db = str(tmp_path / "bot.db")
    assert guild_border_dir(db, 1) == Path(tmp_path) / "quote_borders" / "1"


# ── analyze_border_opening (shape detection) ──────────────────────────


def test_opening_none_for_opaque_frame(tmp_path):
    im = Image.new("RGBA", (W, H), (10, 20, 30, 255))  # fully opaque, no hole
    style = _save_border(tmp_path, "opaque", im)
    assert analyze_border_opening(style, W, H) is None


def test_opening_none_when_center_covered(tmp_path):
    # Transparent only in the corners; the card center is opaque → no opening.
    im = Image.new("RGBA", (W, H), (10, 20, 30, 255))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 60, 60], fill=(0, 0, 0, 0))
    d.rectangle([W - 60, H - 60, W - 1, H - 1], fill=(0, 0, 0, 0))
    style = _save_border(tmp_path, "corners", im)
    assert analyze_border_opening(style, W, H) is None


def test_opening_detected_for_hollow_frame_with_pfp(tmp_path):
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle(
        [0, 0, W - 1, H - 1], radius=40, outline=(40, 120, 200, 255), width=26
    )
    style = _save_border(tmp_path, "rounded", im)
    op = analyze_border_opening(style, W, H)
    assert op is not None
    assert op.top < H // 2 < op.bot
    assert op.left[H // 2] < W // 2 < op.right[H // 2]
    # A wide rectangular hole leaves room for the avatar disc on the left.
    assert op.pfp is not None
    cx, cy, r = op.pfp
    assert r > 0 and op.left[cy] <= cx <= op.right[cy]


def test_opening_no_pfp_when_left_too_narrow(tmp_path):
    # A tall narrow oval: an opening exists but not enough left room for a disc.
    im = Image.new("RGBA", (W, H), (150, 60, 120, 255))
    ImageDraw.Draw(im).ellipse(
        [W * 0.30, H * 0.06, W * 0.70, H * 0.94], fill=(0, 0, 0, 0)
    )
    style = _save_border(tmp_path, "narrow-oval", im)
    op = analyze_border_opening(style, W, H)
    assert op is not None
    assert op.pfp is None  # degrades to centered layout


def test_opening_cached_by_path_mtime(tmp_path):
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle(
        [0, 0, W - 1, H - 1], radius=40, outline=(40, 120, 200, 255), width=26
    )
    style = _save_border(tmp_path, "cache", im)
    a = analyze_border_opening(style, W, H)
    b = analyze_border_opening(style, W, H)
    assert a is b  # second call served from cache


# ── render_quote_card with a mask-fit border ──────────────────────────


def test_render_mask_border_center_visible_and_deterministic(tmp_path):
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle(
        [0, 0, W - 1, H - 1], radius=40, outline=(40, 120, 200, 255), width=30
    )
    style = _save_border(tmp_path, "frame", im)
    kw = dict(
        author_name="Ada", avatar_bytes=_avatar(),
        theme=THEMES["golden_meadow"], font_style="inter", border_style=style,
    )
    png1 = render_quote_card("A quote that should sit inside the frame.", **kw)
    png2 = render_quote_card("A quote that should sit inside the frame.", **kw)
    assert png1 == png2  # deterministic

    out = Image.open(io.BytesIO(png1)).convert("RGBA")
    assert out.size == (W, H)
    # Center is opaque content, not the blue frame color and not transparent.
    px = out.getpixel((W // 2, H // 2))
    assert px[3] == 255
    assert not (px[0] < 80 and 90 < px[1] < 160 and 170 < px[2] < 230)


def test_render_mask_border_confines_text_to_opening(tmp_path):
    # Opaque frame on the right 200px; a long quote must not paint text there
    # (it should wrap left of the frame). The frame is composited last, so any
    # text in that band would only show if wrapping failed AND the frame were
    # transparent — here we assert the wrap by checking the opening bound.
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, W - 1, H - 1], outline=(200, 150, 40, 255), width=20)
    d.rectangle([W - 200, 0, W - 1, H - 1], fill=(200, 150, 40, 255))
    style = _save_border(tmp_path, "right-heavy", im)
    op = analyze_border_opening(style, W, H)
    assert op is not None
    # The detected right edge stays clear of the 200px opaque band.
    assert max(op.right[op.top:op.bot + 1]) <= W - 200
