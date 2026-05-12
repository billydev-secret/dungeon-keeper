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

def _wrap_text(text: str, font, max_width: int, draw) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if int(bbox[2] - bbox[0]) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


# ── Pfp-background card ───────────────────────────────────────────────────────

def _build_background(
    avatar_bytes: bytes,
    width: int,
    height: int,
    theme: QuoteTheme,
):
    from PIL import Image, ImageEnhance, ImageFilter  # noqa: PLC0415

    # Load and fit-cover the avatar
    avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGB")
    aw, ah = avatar.size
    scale = max(width / aw, height / ah)
    new_w, new_h = int(aw * scale), int(ah * scale)
    avatar = avatar.resize((new_w, new_h), Image.Resampling.LANCZOS)  # type: ignore[attr-defined]
    left = (new_w - width) // 2
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

    Args:
        text: Quote body. Truncated to QUOTE_MAX_CHARS with ellipsis if needed.
        author_name: Attribution line (display name). Empty = no attribution.
        avatar_bytes: Raw bytes of the author's avatar image.
        theme: A QuoteTheme from THEMES.
        font_style: Key into FONT_STYLES — "inter" or "lora".
        width/height: Output dimensions.
        jpeg_quality: JPEG encoding quality.
    """
    from PIL import ImageDraw  # noqa: PLC0415

    if len(text) > QUOTE_MAX_CHARS:
        text = text[:QUOTE_MAX_CHARS - 1] + "…"

    bg = _build_background(avatar_bytes, width, height, theme)
    draw = ImageDraw.Draw(bg)

    padding = int(width * 0.10)
    inner_w = width - 2 * padding

    # Font sizes scale with width
    body_size = max(28, width // 22)
    attr_size = max(18, width // 38)

    body_font = _load_font(body_size, font_style)
    attr_font = _load_font(attr_size, font_style)

    # Measure line height
    probe = draw.textbbox((0, 0), "Ag", font=body_font)
    line_h = int(probe[3] - probe[1])
    line_gap = max(6, line_h // 5)

    lines = _wrap_text(f"“{text}”", body_font, inner_w, draw)
    text_block_h = len(lines) * line_h + max(0, len(lines) - 1) * line_gap

    attr_h = 0
    if author_name:
        ap = draw.textbbox((0, 0), f"— {author_name}", font=attr_font)
        attr_h = int(ap[3] - ap[1])

    total_h = text_block_h + (int(height * 0.06) + attr_h if author_name else 0)
    text_y = (height - total_h) // 2

    # Draw shadow then main text
    for line in lines:
        lb = draw.textbbox((0, 0), line, font=body_font)
        lw = int(lb[2] - lb[0])
        x = (width - lw) // 2
        draw.text((x + 2, text_y + 2), line, font=body_font, fill=(0, 0, 0, 120))
        draw.text((x, text_y), line, font=body_font, fill=theme.text_color)
        text_y += line_h + line_gap

    if author_name:
        attr_text = f"— {author_name}"
        ap = draw.textbbox((0, 0), attr_text, font=attr_font)
        aw = int(ap[2] - ap[0])
        ax = (width - aw) // 2
        ay = text_y + int(height * 0.04)
        draw.text((ax + 1, ay + 1), attr_text, font=attr_font, fill=(0, 0, 0, 100))
        draw.text((ax, ay), attr_text, font=attr_font, fill=theme.attribution_color)

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
