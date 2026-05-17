"""Generic text-to-image quote card renderer.

Supports two render paths:
- render_quote()       — dark, solid-bg card (used by legacy callers)
- render_quote_card()  — pfp-as-background with color grading (used by QuoteCog)

Fonts are loaded from assets/fonts/; missing files raise FileNotFoundError loudly
so the problem is immediately visible rather than silently degrading.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

_ASSETS = Path("assets") / "fonts"
_INTER = _ASSETS / "Inter-Regular.ttf"
_LORA = _ASSETS / "Lora-Regular.ttf"
_BORDER = Path("assets") / "border.png"

try:
    from pilmoji import Pilmoji as _Pilmoji
    from pilmoji.helpers import getsize as _emoji_getsize
    from pilmoji.source import TwemojiEmojiSource as _EmojiSource

    _HAS_PILMOJI = True
except ImportError:
    _Pilmoji = None  # type: ignore[assignment]
    _emoji_getsize = None  # type: ignore[assignment]
    _EmojiSource = None  # type: ignore[assignment]
    _HAS_PILMOJI = False

QUOTE_MAX_CHARS = 280


# ── Theme definition ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class QuoteTheme:
    name: str
    # Color-grading: golden overlay blended over the desaturated pfp
    overlay_color: tuple[int, int, int]  # RGB
    overlay_alpha: float                 # 0.0–1.0 blend strength
    desaturate: float                    # 0.0=grey, 1.0=full color; applied before overlay
    # Text colors
    text_color: tuple[int, int, int]
    attribution_color: tuple[int, int, int]
    # Vignette darkness (0.0 = none, 1.0 = black edges)
    vignette_strength: float


THEMES: dict[str, QuoteTheme] = {
    "golden_meadow": QuoteTheme(
        name="Golden Meadow",
        overlay_color=(212, 160, 40),   # warm amber-gold
        overlay_alpha=0.38,
        desaturate=0.55,
        text_color=(255, 248, 220),     # cream
        attribution_color=(255, 220, 120),
        vignette_strength=0.72,
    ),
    "midnight": QuoteTheme(
        name="Midnight",
        overlay_color=(20, 20, 60),
        overlay_alpha=0.50,
        desaturate=0.35,
        text_color=(230, 230, 255),
        attribution_color=(160, 160, 220),
        vignette_strength=0.80,
    ),
    "rose": QuoteTheme(
        name="Rose",
        overlay_color=(200, 60, 100),
        overlay_alpha=0.38,
        desaturate=0.50,
        text_color=(255, 235, 240),
        attribution_color=(255, 180, 200),
        vignette_strength=0.68,
    ),
}

FONT_STYLES: dict[str, Path] = {
    "inter": _INTER,
    "lora": _LORA,
}


# ── Font loading ──────────────────────────────────────────────────────────────

def _load_font(size: int, style: str = "inter"):
    from PIL import ImageFont  # noqa: PLC0415

    path = FONT_STYLES.get(style, _INTER)
    if not path.exists():
        raise FileNotFoundError(
            f"Quote font not found: {path}. "
            "Place Inter-Regular.ttf and Lora-Regular.ttf in assets/fonts/."
        )
    return ImageFont.truetype(str(path), size)


def _load_font_fallback(size: int):
    """Fallback for render_quote() — tries Inter then Pillow default."""
    from PIL import ImageFont  # noqa: PLC0415

    if _INTER.exists():
        try:
            return ImageFont.truetype(str(_INTER), size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


# ── Text wrapping ─────────────────────────────────────────────────────────────

def _wrap_text(text: str, font, max_width: int, draw, measure=None) -> list[str]:
    result: list[str] = []
    for para in text.splitlines():
        words = para.split()
        if not words:
            result.append("")
            continue
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if measure is not None:
                _w = measure(candidate)
            else:
                bbox = draw.textbbox((0, 0), candidate, font=font)
                _w = int(bbox[2] - bbox[0])
            if _w <= max_width or not current:
                current = candidate
            else:
                result.append(current)
                current = word
        if current:
            result.append(current)
    return result or [""]


# ── Pfp-background card ───────────────────────────────────────────────────────

def _build_background(
    avatar_bytes: bytes,
    width: int,
    height: int,
    theme: QuoteTheme,
    offset_x: int = 0,
):
    from PIL import Image, ImageEnhance, ImageFilter  # noqa: PLC0415

    # Load and fit-cover the avatar
    avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGB")
    aw, ah = avatar.size
    scale = max(width / aw, height / ah)
    new_w, new_h = int(aw * scale), int(ah * scale)
    avatar = avatar.resize((new_w, new_h), Image.Resampling.LANCZOS)  # type: ignore[attr-defined]
    left = max(0, min((new_w - width) // 2 + offset_x, new_w - width))
    top = (new_h - height) // 2
    avatar = avatar.crop((left, top, left + width, top + height))

    # Strong blur
    bg = avatar.filter(ImageFilter.GaussianBlur(radius=18))

    # Desaturate
    bg = ImageEnhance.Color(bg).enhance(theme.desaturate)

    # Golden/theme overlay
    overlay = Image.new("RGB", (width, height), theme.overlay_color)
    bg = Image.blend(bg, overlay, theme.overlay_alpha)

    # Radial vignette
    import math  # noqa: PLC0415
    vignette = Image.new("L", (width, height), 0)
    cx, cy = width / 2, height / 2
    max_r = math.hypot(cx, cy)
    pixels = vignette.load()
    s = theme.vignette_strength
    for y in range(height):
        for x in range(width):
            r = math.hypot(x - cx, y - cy) / max_r
            darkness = int(s * r * r * 255)
            pixels[x, y] = min(255, darkness)  # type: ignore[index]

    dark = Image.new("RGB", (width, height), (0, 0, 0))
    bg.paste(dark, mask=vignette)

    return bg


def render_quote_card(
    text: str,
    *,
    author_name: str = "",
    avatar_bytes: bytes,
    theme: QuoteTheme,
    font_style: str = "inter",
    width: int = 900,
    height: int = 500,
    jpeg_quality: int = 90,
) -> bytes:
    """Render a quote card with the avatar as a blurred, color-graded background.

    Layout: pfp on LEFT, text on RIGHT.
    """
    from PIL import Image, ImageDraw, ImageFilter  # noqa: PLC0415

    if len(text) > QUOTE_MAX_CHARS:
        text = text[:QUOTE_MAX_CHARS - 1] + "…"

    # Blurred background, face offset left 20%
    bg = _build_background(avatar_bytes, width, height, theme, offset_x=int(width * 0.20))

    # Clip to just inside the frame inner edges with a uniform 3px gap.
    # Frame inner edges at 900x500: top/bottom ~28px, left/right ~59px.
    _ix = max(4, int(width * 0.068))   # ~62px at 900
    _iy = max(4, int(height * 0.062))  # ~31px at 500
    rr_mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(rr_mask).rounded_rectangle(
        (_ix, _iy, width - _ix, height - _iy), radius=50, fill=255,
    )
    bg = Image.composite(bg, Image.new("RGB", (width, height), (0, 0, 0)), rr_mask)

    # Gold gradient denser toward bottom-right (flower corner)
    _grad = Image.new("L", (width, height))
    _grad_px = _grad.load()
    assert _grad_px is not None
    for _gy in range(height):
        for _gx in range(width):
            _grad_px[_gx, _gy] = int(((_gx / width) * (_gy / height)) ** 0.5 * 90)
    bg.paste(Image.new("RGB", (width, height), theme.overlay_color), mask=_grad)

    # Layout constants
    pfp_r = int(min(width, height) * 0.16)
    pfp_cx = int(width * 0.24)
    pfp_cy = height // 2
    pfp_d = pfp_r * 2
    px, py = pfp_cx - pfp_r, pfp_cy - pfp_r

    text_pad_l = int(width * 0.40)
    text_col_w = int(width * 0.45)

    body_size = max(26, width // 24)
    attr_size = max(16, width // 40)
    body_font = _load_font(body_size, font_style)
    attr_font = _load_font(attr_size, font_style)

    draw = ImageDraw.Draw(bg)
    probe = draw.textbbox((0, 0), "Ag", font=body_font)
    line_h = int(probe[3] - probe[1])
    line_gap = max(6, line_h // 5)

    _measure = (lambda t: _emoji_getsize(t, font=body_font)[0]) if _HAS_PILMOJI else None
    lines = _wrap_text(f"“{text}”", body_font, text_col_w, draw, measure=_measure)
    text_block_h = len(lines) * line_h + max(0, len(lines) - 1) * line_gap
    text_y_start = (height - text_block_h) // 2

    # Soft gaussian text shadow
    _shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    _sdraw = ImageDraw.Draw(_shadow)
    _sy = text_y_start
    for line in lines:
        _sdraw.text((text_pad_l + 4, _sy + 4), line, font=body_font, fill=(0, 0, 0, 170))
        _sy += line_h + line_gap
    _shadow = _shadow.filter(ImageFilter.GaussianBlur(radius=5))
    _bg_rgba = bg.convert("RGBA")
    _bg_rgba.alpha_composite(_shadow)
    bg = _bg_rgba.convert("RGB")
    draw = ImageDraw.Draw(bg)

    # Draw text — pilmoji renders Unicode + Discord emoji images inline
    text_y = text_y_start
    if _HAS_PILMOJI:
        with _Pilmoji(bg, source=_EmojiSource) as _pm:
            for line in lines:
                _pm.text((text_pad_l, text_y), line, fill=theme.text_color, font=body_font)
                text_y += line_h + line_gap
    else:
        for line in lines:
            draw.text((text_pad_l, text_y), line, font=body_font, fill=theme.text_color)
            text_y += line_h + line_gap
    draw = ImageDraw.Draw(bg)

    # Pfp drop shadow
    _pfp_sh = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    _soff = pfp_r // 5
    ImageDraw.Draw(_pfp_sh).ellipse(
        (px + _soff - 6, py + _soff - 6, px + pfp_d + _soff + 6, py + pfp_d + _soff + 6),
        fill=(0, 0, 0, 150),
    )
    _pfp_sh = _pfp_sh.filter(ImageFilter.GaussianBlur(radius=pfp_r // 3))
    _bg_rgba = bg.convert("RGBA")
    _bg_rgba.alpha_composite(_pfp_sh)
    bg = _bg_rgba.convert("RGB")
    draw = ImageDraw.Draw(bg)

    # Circular pfp — unblurred avatar cropped into a circle
    avatar_img = Image.open(io.BytesIO(avatar_bytes)).convert("RGB")
    avatar_img = avatar_img.resize((pfp_d, pfp_d), Image.Resampling.LANCZOS)  # type: ignore[attr-defined]
    circle_mask = Image.new("L", (pfp_d, pfp_d), 0)
    ImageDraw.Draw(circle_mask).ellipse((0, 0, pfp_d - 1, pfp_d - 1), fill=255)
    bg.paste(avatar_img, (px, py), mask=circle_mask)
    draw = ImageDraw.Draw(bg)

    # Double ring: outer cream + inner gold
    _rg, _rt = 4, 3
    draw.ellipse(
        (px - _rg - _rt, py - _rg - _rt, px + pfp_d + _rg + _rt - 1, py + pfp_d + _rg + _rt - 1),
        outline=(255, 248, 220),
        width=_rt,
    )
    draw.ellipse(
        (px - 3, py - 3, px + pfp_d + 2, py + pfp_d + 2),
        outline=theme.attribution_color,
        width=3,
    )

    # Author name centred below pfp
    if author_name:
        attr_text = f"— {author_name}"
        attr_bbox = draw.textbbox((0, 0), attr_text, font=attr_font)
        attr_w = attr_bbox[2] - attr_bbox[0]
        ax = pfp_cx - attr_w // 2
        ay = pfp_cy + pfp_r + int(height * 0.04)
        draw.text((ax + 1, ay + 1), attr_text, font=attr_font, fill=(0, 0, 0))
        draw.text((ax, ay), attr_text, font=attr_font, fill=theme.attribution_color)

    # Border overlay — flipped so poppies land bottom-right
    if _BORDER.exists():
        border = Image.open(_BORDER).convert("RGBA")
        border = border.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        border = border.resize((width, height), Image.Resampling.LANCZOS)  # type: ignore[attr-defined]
        lum = border.convert("RGB").convert("L")
        border.putalpha(lum.point([0 if i <= 20 else 255 for i in range(256)]))  # type: ignore[arg-type]
        _bg_rgba = bg.convert("RGBA")
        _bg_rgba.alpha_composite(border)
        bg = _bg_rgba.convert("RGB")

    buf = io.BytesIO()
    bg.save(buf, format="JPEG", quality=jpeg_quality)
    return buf.getvalue()


# ── Legacy solid-bg card ──────────────────────────────────────────────────────

def render_quote(
    text: str,
    *,
    footer: str = "",
    width: int = 800,
    bg_color: tuple[int, int, int] = (18, 18, 24),
    text_color: tuple[int, int, int] = (235, 230, 245),
    footer_color: tuple[int, int, int] = (140, 120, 165),
    accent_color: tuple[int, int, int] = (100, 40, 130),
    font_size: int = 38,
    footer_font_size: int = 22,
    padding: int = 60,
    jpeg_quality: int = 90,
) -> bytes:
    """Render text as a dark solid-background quote card. Returns JPEG bytes."""
    from PIL import Image, ImageDraw  # noqa: PLC0415

    body_font = _load_font_fallback(font_size)
    footer_font = _load_font_fallback(footer_font_size) if footer else None

    inner_w = width - 2 * padding

    probe_img = Image.new("RGB", (1, 1))
    draw_tmp = ImageDraw.Draw(probe_img)
    line_bbox = draw_tmp.textbbox((0, 0), "Ag", font=body_font)
    line_h = int(line_bbox[3] - line_bbox[1])
    line_spacing = max(8, line_h // 4)

    lines = _wrap_text(text, body_font, inner_w, draw_tmp)
    text_block_h = len(lines) * line_h + max(0, len(lines) - 1) * line_spacing

    footer_h = 0
    footer_gap = 0
    if footer and footer_font:
        fb = draw_tmp.textbbox((0, 0), footer, font=footer_font)
        footer_h = int(fb[3] - fb[1])
        footer_gap = padding // 2

    accent_bar = 4
    height = int(max(
        200,
        2 * padding + text_block_h + footer_gap + footer_h + 2 * accent_bar,
    ))

    img = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    draw.rectangle([(0, 0), (width, accent_bar)], fill=accent_color)
    draw.rectangle([(0, height - accent_bar), (width, height)], fill=accent_color)

    usable_h = height - 2 * padding - footer_h - footer_gap - 2 * accent_bar
    text_y = accent_bar + padding + max(0, (usable_h - text_block_h) // 2)

    for line in lines:
        lb = draw.textbbox((0, 0), line, font=body_font)
        lw = int(lb[2] - lb[0])
        x = (width - lw) // 2
        draw.text((x, text_y), line, font=body_font, fill=text_color)
        text_y += line_h + line_spacing

    if footer and footer_font:
        fb = draw.textbbox((0, 0), footer, font=footer_font)
        fw = int(fb[2] - fb[0])
        fy = height - accent_bar - padding // 2 - footer_h
        draw.text(((width - fw) // 2, fy), footer, font=footer_font, fill=footer_color)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=jpeg_quality)
    return buf.getvalue()
