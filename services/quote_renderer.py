"""Generic text-to-image quote card renderer.

Returns a JPEG image as bytes. No caller-specific logic — all veil
details (round numbers, colours) are passed as arguments.

Drop a TrueType font at ``assets/fonts/quote.ttf`` for best results;
falls back to Pillow's built-in bitmap font automatically.
"""
from __future__ import annotations

import io
from pathlib import Path

_FONT_PATH = Path("assets") / "fonts" / "quote.ttf"


def _load_font(size: int):  # type: ignore[return]
    """Return an ImageFont at the requested size, trying bundled → default."""
    from PIL import ImageFont  # noqa: PLC0415

    if _FONT_PATH.exists():
        try:
            return ImageFont.truetype(str(_FONT_PATH), size)
        except OSError:
            pass
    # Pillow 10+ load_default accepts a size keyword argument.
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _wrap_text(text: str, font, max_width: int, draw) -> list[str]:  # type: ignore[type-arg]
    """Greedily wrap *text* into lines no wider than *max_width* pixels."""
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
    return lines if lines else [""]


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
    """Render *text* as a dark quote-card image and return JPEG bytes.

    Args:
        text: Main quote body. Long text is word-wrapped automatically.
        footer: Small label at the bottom (e.g. "Veil #42"). Omit for none.
        width: Output image width in pixels.
        bg_color: RGB background colour.
        text_color: RGB colour for the main text.
        footer_color: RGB colour for the footer text.
        accent_color: RGB colour for the top/bottom accent bars.
        font_size: Point size for the main text.
        footer_font_size: Point size for the footer text.
        padding: Horizontal and vertical padding in pixels.
        jpeg_quality: JPEG encoding quality (1-95).

    Returns:
        JPEG bytes of the rendered card.
    """
    from PIL import Image, ImageDraw  # noqa: PLC0415

    body_font = _load_font(font_size)
    footer_font = _load_font(footer_font_size) if footer else None

    inner_w = width - 2 * padding

    # Measure a single line's height using a representative string.
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

    # Centre text block vertically in the space above the footer.
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
