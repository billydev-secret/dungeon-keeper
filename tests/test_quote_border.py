"""Unit tests for the per-guild quote-border resolver + shape masking."""

from __future__ import annotations

import io
from pathlib import Path

import matplotlib
import pytest

matplotlib.use("Agg")

from PIL import Image, ImageDraw

from bot_modules.services.quote_renderer import (
    BORDERS,
    CUSTOM_BORDER_NAME,
    THEMES,
    BorderStyle,
    _MASK_CACHE,
    analyze_border_opening,
    card_size_for_border,
    square_crop_box,
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


# ── Frame aspect ratio (the card canvas follows the frame) ────────────
#
# Every frame used to be force-resized to the card canvas, so a frame whose
# native ratio wasn't the canvas's 900x500 (1.80:1) rendered stretched — the
# bundled Midnight Frame is 1536x1024 (1.50:1), a 20% horizontal stretch, and
# both live per-guild uploads are 3:2 too. The canvas now takes its height from
# the frame's own ratio so the art is neither distorted nor cropped.


def _frame_of_size(tmp_path, name: str, w: int, h: int) -> BorderStyle:
    """A hollow rectangular frame of a given pixel size (so, a given ratio)."""
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle(
        [0, 0, w - 1, h - 1], radius=int(min(w, h) * 0.08),
        outline=(40, 120, 200, 255), width=max(8, int(min(w, h) * 0.05)),
    )
    return _save_border(tmp_path, name, im)


@pytest.mark.parametrize(
    ("frame_w", "frame_h", "expected"),
    [
        pytest.param(1536, 1024, (900, 600), id="3:2-midnight-and-uploads"),
        pytest.param(1000, 1000, (900, 900), id="square"),
        pytest.param(1672, 941, (900, 507), id="golden-poppy-native"),
        pytest.param(2400, 1000, (900, 375), id="ultra-wide"),
    ],
)
def test_card_canvas_takes_the_frames_aspect_ratio(tmp_path, frame_w, frame_h, expected):
    style = _frame_of_size(tmp_path, f"f{frame_w}x{frame_h}", frame_w, frame_h)
    png = render_quote_card(
        "A quote whose frame must not be stretched to fit the card.",
        author_name="Ada", avatar_bytes=_avatar(),
        theme=THEMES["golden_meadow"], font_style="inter", border_style=style,
    )
    out = Image.open(io.BytesIO(png))
    assert out.size == expected
    # The rendered ratio matches the source frame's to within a pixel of rounding.
    assert out.width / out.height == pytest.approx(frame_w / frame_h, abs=0.005)


def test_card_size_for_border_keeps_requested_size_when_frame_unreadable(tmp_path):
    """A missing frame can't dictate a ratio — fall back to the requested canvas."""
    missing = BorderStyle(
        name="gone", path=tmp_path / "nope.png", flip=False, luma_key=False
    )
    assert card_size_for_border(W, H, missing) == (W, H)


def test_card_size_for_border_is_the_size_render_uses(tmp_path):
    """The helper the upload validator probes with agrees with the renderer."""
    style = _frame_of_size(tmp_path, "agree", 1536, 1024)
    size = card_size_for_border(W, H, style)
    png = render_quote_card(
        "Probe and render must agree on the canvas.",
        author_name="Ada", avatar_bytes=_avatar(),
        theme=THEMES["golden_meadow"], font_style="inter", border_style=style,
    )
    assert Image.open(io.BytesIO(png)).size == size


def test_bundled_midnight_frame_renders_at_its_own_ratio():
    style = BORDERS["midnight_frame"]
    if not style.path.exists():
        pytest.skip("bundled frame not resolvable from this CWD")
    with Image.open(style.path) as src:
        ratio = src.width / src.height
    png = render_quote_card(
        "The bundled Midnight Frame must not be stretched.",
        author_name="Ada", avatar_bytes=_avatar(),
        theme=THEMES["midnight"], font_style="inter", border_style=style,
    )
    out = Image.open(io.BytesIO(png))
    assert out.width / out.height == pytest.approx(ratio, abs=0.005)


def test_bundled_poppy_frame_renders_at_its_own_ratio():
    style = BORDERS["golden_poppy"]
    if not style.path.exists():
        pytest.skip("bundled frame not resolvable from this CWD")
    with Image.open(style.path) as src:
        ratio = src.width / src.height
    png = render_quote_card(
        "The bundled Golden Poppy frame must not be stretched.",
        author_name="Ada", avatar_bytes=_avatar(),
        theme=THEMES["golden_meadow"], font_style="inter", border_style=style,
    )
    out = Image.open(io.BytesIO(png))
    assert out.width / out.height == pytest.approx(ratio, abs=0.005)


# ── Midnight Frame fits its own opening ───────────────────────────────
#
# Its art is left-heavy: laid out with the poppy-tuned constants the avatar disc
# (pinned at 0.18w) sat buried under the frame's flowers and the attribution ran
# under them. Like an uploaded frame, it now fits the transparency it actually
# leaves — and that opening turns out to have no room for a disc at all, so the
# card degrades to the banner layout rather than drawing over the artwork.


def test_midnight_frame_is_mask_fit():
    assert BORDERS["midnight_frame"].mask_fit is True


def test_midnight_frame_declines_the_avatar_disc():
    """No disc fits beside this frame's flowers, so the layout must not force one."""
    style = BORDERS["midnight_frame"]
    if not style.path.exists():
        pytest.skip("bundled frame not resolvable from this CWD")
    w, h = card_size_for_border(W, H, style)
    op = analyze_border_opening(style, w, h)
    assert op is not None  # there is a usable opening…
    assert op.pfp is None  # …but not one an avatar fits in


def test_midnight_frame_keeps_content_inside_its_opening():
    """The quote is laid out against the frame's real opening, not the 0.18w anchor."""
    style = BORDERS["midnight_frame"]
    if not style.path.exists():
        pytest.skip("bundled frame not resolvable from this CWD")
    w, h = card_size_for_border(W, H, style)
    op = analyze_border_opening(style, w, h)
    assert op is not None
    # The flowers push the opening's left edge well right of where the poppy-tuned
    # layout pinned the avatar (0.18w) and the text column (0.34w) — which is
    # exactly how both ended up drawn under the artwork.
    assert min(op.left[op.top:op.bot + 1]) > int(w * 0.18)

    png = render_quote_card(
        "A Midnight quote must sit inside the frame, clear of the flowers.",
        author_name="Ada", avatar_bytes=_avatar(),
        theme=THEMES["midnight"], font_style="inter", border_style=style,
    )
    out = Image.open(io.BytesIO(png))
    assert out.size == (w, h)


# ── Foreground avatar keeps its own ratio ─────────────────────────────


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        pytest.param((512, 512), (0, 0, 512, 512), id="square-is-identity"),
        pytest.param((400, 200), (100, 0, 300, 200), id="wide"),
        pytest.param((200, 400), (0, 100, 200, 300), id="tall"),
        pytest.param((101, 100), (0, 0, 100, 100), id="odd-by-one"),
    ],
)
def test_square_crop_box_centers_without_squashing(size, expected):
    assert square_crop_box(*size) == expected


def test_wide_avatar_is_cropped_not_squashed(tmp_path):
    """A 2:1 source must not stretch into the disc — the drawn circle stays round."""
    src = Image.new("RGB", (400, 200), (20, 20, 20))
    # A circle inscribed in the center square: squashing 2:1 would flatten it into
    # an ellipse, cropping keeps it circular. Saturated blue survives untouched in
    # the foreground disc, while the background copy of the same avatar is
    # desaturated and gold-blended into a gray the threshold below rejects.
    ImageDraw.Draw(src).ellipse([100, 0, 299, 199], fill=(60, 60, 240))
    buf = io.BytesIO()
    src.save(buf, "PNG")

    # A gold frame, so the blue threshold below picks up only the avatar.
    frame = Image.new("RGBA", (900, 500), (0, 0, 0, 0))
    ImageDraw.Draw(frame).rounded_rectangle(
        [0, 0, 899, 499], radius=40, outline=(200, 150, 40, 255), width=26
    )
    style = _save_border(tmp_path, "wide-av", frame)
    png = render_quote_card(
        "A wide avatar must not be squashed into the disc.",
        author_name="Ada", avatar_bytes=buf.getvalue(),
        theme=THEMES["golden_meadow"], font_style="inter", border_style=style,
    )
    out = Image.open(io.BytesIO(png)).convert("RGB")

    # Bounding box of the drawn disc, wherever the frame's opening placed it.
    import numpy as np

    arr = np.asarray(out, dtype=np.int16)
    disc = (arr[:, :, 2] > 180) & (arr[:, :, 0] < 120)
    ys, xs = np.nonzero(disc)
    assert ys.size > 0, "the avatar didn't render"
    box_w = int(xs.max() - xs.min()) + 1
    box_h = int(ys.max() - ys.min()) + 1
    # Round, not oval: squashing a 2:1 source would leave the box half as tall as
    # it is wide. A few px of tolerance covers the ring's antialiasing.
    assert abs(box_w - box_h) <= 4, f"avatar is {box_w}×{box_h}, not round"
