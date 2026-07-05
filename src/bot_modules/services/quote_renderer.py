"""Generic text-to-image quote card renderer.

Supports two render paths:
- render_quote()       — dark, solid-bg card (used by legacy callers)
- render_quote_card()  — pfp-as-background with color grading (used by QuoteCog)

Fonts are loaded from assets/fonts/; missing files raise FileNotFoundError loudly
so the problem is immediately visible rather than silently degrading.
"""
from __future__ import annotations

import io
import logging
import re as _re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_ASSETS = Path("assets") / "fonts"
_INTER = _ASSETS / "Inter-Regular.ttf"
_LORA = _ASSETS / "Lora-Regular.ttf"
_PLAYFAIR = _ASSETS / "PlayfairDisplay-Regular.ttf"
_OSWALD = _ASSETS / "Oswald-Regular.ttf"
_CAVEAT = _ASSETS / "Caveat-Regular.ttf"
_BEBAS = _ASSETS / "BebasNeue-Regular.ttf"
# Arimo is the OFL, metric-compatible stand-in for Helvetica/Arial — Helvetica
# itself is proprietary and can't be bundled. Exposed to users as "Helvetica".
_HELVETICA = _ASSETS / "Arimo-Regular.ttf"

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

# Matches Discord custom emoji tokens: <:name:id> and <a:name:id>
_DISCORD_EMOJI_RE = _re.compile(r'<a?:[^:]+:(\d+)>')


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
    "playfair": _PLAYFAIR,
    "oswald": _OSWALD,
    "caveat": _CAVEAT,
    "bebas": _BEBAS,
    "helvetica": _HELVETICA,
}


# ── Border definition ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BorderStyle:
    name: str
    path: Path
    # Flip horizontally so a bottom-left floral corner lands bottom-right (away
    # from the left-side pfp). Only needed for sources drawn in the left corner.
    flip: bool
    # Luminance-key transparency: source has an opaque (black) background that
    # must be keyed out. False when the PNG already carries a real alpha channel.
    luma_key: bool


BORDERS: dict[str, BorderStyle] = {
    "golden_poppy": BorderStyle(
        name="Golden Poppy",
        path=Path("assets") / "border.png",
        flip=True,
        luma_key=True,
    ),
    "midnight_frame": BorderStyle(
        name="Midnight Frame",
        path=Path("assets") / "midnightbordertransparent.png",
        flip=False,
        luma_key=False,
    ),
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


def _make_emoji_measure(base_fn, emoji_size: int):
    """Wrap a text-measure function to account for Discord custom emoji token widths."""
    def _measure(s: str) -> int:
        total = 0
        pos = 0
        for m in _DISCORD_EMOJI_RE.finditer(s):
            seg = s[pos:m.start()]
            if seg:
                total += base_fn(seg)
            total += emoji_size
            pos = m.end()
        tail = s[pos:]
        if tail:
            total += base_fn(tail)
        return total
    return _measure


def _render_line_mixed(
    line: str,
    x: int,
    y: int,
    *,
    font,
    color: tuple[int, int, int],
    emoji_size: int,
    custom_emojis: "dict[str, bytes] | None",
    bg,
    draw,
    pilmoji=None,
) -> None:
    """Render a text line, compositing Discord custom emoji images at token positions."""
    from PIL import Image as _I  # noqa: PLC0415

    cx = x
    pos = 0
    for m in _DISCORD_EMOJI_RE.finditer(line):
        seg = line[pos:m.start()]
        if seg:
            if pilmoji is not None:
                pilmoji.text((cx, y), seg, fill=color, font=font)
                seg_w = _emoji_getsize(seg, font=font)[0]  # type: ignore[misc]
            else:
                draw.text((cx, y), seg, fill=color, font=font)
                bbox = draw.textbbox((cx, y), seg, font=font)
                seg_w = int(bbox[2] - bbox[0])
            cx += seg_w

        eid = m.group(1)
        if custom_emojis and eid in custom_emojis:
            try:
                ei = _I.open(io.BytesIO(custom_emojis[eid]))
                if getattr(ei, "n_frames", 1) > 1:
                    ei.seek(0)
                ei = ei.convert("RGBA").resize(
                    (emoji_size, emoji_size), _I.Resampling.LANCZOS  # type: ignore[attr-defined]
                )
                bg.paste(ei, (cx, y), mask=ei.split()[3])
            except Exception:
                log.exception("quote_renderer: emoji paste")
        cx += emoji_size
        pos = m.end()

    tail = line[pos:]
    if tail:
        if pilmoji is not None:
            pilmoji.text((cx, y), tail, fill=color, font=font)
        else:
            draw.text((cx, y), tail, fill=color, font=font)


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
    border_style: "BorderStyle | None" = None,
    width: int = 900,
    height: int = 500,
    custom_emojis: "dict[str, bytes] | None" = None,
    pfp_shape: str = "circle",
) -> bytes:
    """Render a quote card with the avatar as a blurred, color-graded background.

    Layout: pfp on LEFT, text on RIGHT. Returns PNG bytes with transparent corners.

    ``pfp_shape`` controls the foreground avatar: ``"circle"`` (default — circular
    crop with a double ring), ``"square"`` (rounded-square that shows the whole
    avatar without clipping its corners), or ``"none"`` (no avatar box at all —
    the prompt is centred across the card and ``author_name`` becomes a centred
    header above it).
    """
    from PIL import Image, ImageDraw, ImageFilter  # noqa: PLC0415

    if len(text) > QUOTE_MAX_CHARS:
        text = text[:QUOTE_MAX_CHARS - 1] + "…"

    # Blurred background — when there's a left-side pfp, push the face left so it
    # doesn't sit under the text column; with no pfp keep the image centred.
    _no_pfp = pfp_shape == "none"
    bg = _build_background(
        avatar_bytes, width, height, theme,
        offset_x=0 if _no_pfp else int(width * 0.20),
    )

    # Outer card shape — full canvas with rounded corners matching the border frame.
    card_mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(card_mask).rounded_rectangle(
        (0, 0, width - 1, height - 1),
        radius=max(20, int(min(width, height) * 0.10)),
        fill=255,
    )

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
    pfp_cx = int(width * 0.18)
    pfp_cy = height // 2
    pfp_d = pfp_r * 2
    px, py = pfp_cx - pfp_r, pfp_cy - pfp_r

    # Text column sits between the left-side pfp (outer ring ≈ 0.28w) and the
    # right frame / floral corner. Halve the slack on both sides for more room:
    # left edge moved toward the avatar, right edge toward the flowers, while
    # staying clear of the gold frame (inner edge ≈ 0.93w) and the upper petals.
    text_pad_l = int(width * 0.34)
    text_col_w = int(width * 0.48)

    body_size = max(26, width // 24)
    attr_size = max(16, width // 40)
    body_font = _load_font(body_size, font_style)
    attr_font = _load_font(attr_size, font_style)

    draw = ImageDraw.Draw(bg)
    probe = draw.textbbox((0, 0), "Ag", font=body_font)
    line_h = int(probe[3] - probe[1])
    line_gap = max(6, line_h // 5)

    if _HAS_PILMOJI:
        def _base_m(t: str) -> int:
            return _emoji_getsize(t, font=body_font)[0]  # type: ignore[misc]
    else:
        def _base_m(t: str) -> int:  # type: ignore[misc]
            return int(draw.textbbox((0, 0), t, font=body_font)[2] - draw.textbbox((0, 0), t, font=body_font)[0])
    _quoted_text = f"“{text}”"
    _full_measure = _make_emoji_measure(_base_m, line_h)

    # No-pfp mode turns the label into a centred header above the prompt. Give it
    # a dedicated font that's larger than the body and faux-bolded with a stroke
    # (there's no bold TTF in assets/) so it reads clearly as a title.
    _header_text = author_name if (_no_pfp and author_name) else ""
    header_size = max(body_size + 10, int(body_size * 1.6))
    header_font = _load_font(header_size, font_style)
    _header_stroke = max(2, header_size // 16)
    _header_h = _header_gap = 0
    if _header_text:
        _hb = draw.textbbox((0, 0), _header_text, font=header_font, stroke_width=_header_stroke)
        _header_h = int(_hb[3] - _hb[1])
        _header_gap = max(14, line_h)
    _header_block = (_header_h + _header_gap) if _header_text else 0

    left_margin = int(width * 0.06)

    if _no_pfp:
        # Left-justified body: keep ~one character of buffer off the left frame.
        left_margin += max(1, _full_measure("n"))
        # The brand's flowers fill the bottom-right corner. Carve a matching
        # exclusion so the usable right edge drops toward the bottom; each line is
        # centred within the remaining [left_margin, right_limit] band, so the
        # prompt reads centred yet flows around the floral corner.
        _ex_apex_y = height * 0.24          # above this the full width is free
        _ex_reach_y = height * 0.62         # at/below this the carve is maxed out
        _ex_left_top = width * 0.95         # flowers' left edge above the corner
        _ex_left_min = width * 0.58         # flowers' left edge level with them
        _gap3 = 3 * max(1, _full_measure("nnn") // 3)  # ~3 characters of breathing room

        def _flower_left(y: float) -> float:
            if y <= _ex_apex_y:
                return _ex_left_top
            frac = min(1.0, (y - _ex_apex_y) / max(1.0, _ex_reach_y - _ex_apex_y))
            return _ex_left_top - frac * (_ex_left_top - _ex_left_min)

        def _avail_w(y: float) -> int:
            return max(int(width * 0.28), int(_flower_left(y) - _gap3 - left_margin))

        def _flow(text_start_y: int) -> list[str]:
            out: list[str] = []
            for para in _quoted_text.splitlines():
                words = para.split()
                if not words:
                    out.append("")
                    continue
                cur = ""
                for w in words:
                    y = text_start_y + len(out) * (line_h + line_gap)
                    cand = f"{cur} {w}".strip()
                    if _full_measure(cand) <= _avail_w(y) or not cur:
                        cur = cand
                    else:
                        out.append(cur)
                        cur = w
                if cur:
                    out.append(cur)
            return out or [""]

        def _layout(lines_: list[str]) -> tuple[int, int]:
            blk = len(lines_) * line_h + max(0, len(lines_) - 1) * line_gap
            if _header_block:
                top = int(height * 0.15)  # pin the header near the top of the card
            else:
                top = int((height - blk) * 0.40)  # no header: bias the prompt up
            return top + _header_block, top

        # One re-flow: lay out at a nominal top, re-centre, then flow at the final
        # start (usable width depends on absolute y).
        lines = _flow(int(height * 0.26))
        text_y_start, _content_top = _layout(lines)
        lines = _flow(text_y_start)
        text_y_start, _content_top = _layout(lines)

        def _line_x(s: str, y: int) -> int:
            # Left-justified: every line starts at the left margin. Wrapping via
            # _avail_w(y) already keeps lines clear of the floral corner.
            return left_margin
    else:
        _measure = _make_emoji_measure(_base_m, line_h) if _DISCORD_EMOJI_RE.search(_quoted_text) else (_base_m if _HAS_PILMOJI else None)
        lines = _wrap_text(_quoted_text, body_font, text_col_w, draw, measure=_measure)
        _content_top = (height - (len(lines) * line_h + max(0, len(lines) - 1) * line_gap)) // 2
        text_y_start = _content_top

        def _line_x(s: str, y: int) -> int:
            return text_pad_l

    # Soft gaussian text shadow
    _shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    _sdraw = ImageDraw.Draw(_shadow)
    _sy = text_y_start
    for line in lines:
        _sdraw.text((_line_x(line, _sy) + 4, _sy + 4), _DISCORD_EMOJI_RE.sub('', line), font=body_font, fill=(0, 0, 0, 170))
        _sy += line_h + line_gap
    _shadow = _shadow.filter(ImageFilter.GaussianBlur(radius=5))
    _bg_rgba = bg.convert("RGBA")
    _bg_rgba.alpha_composite(_shadow)
    bg = _bg_rgba.convert("RGB")
    draw = ImageDraw.Draw(bg)

    # Draw text — pilmoji handles Unicode emoji; _render_line_mixed composites Discord custom emojis
    text_y = text_y_start
    if _HAS_PILMOJI:
        with _Pilmoji(bg, source=_EmojiSource) as _pm:  # type: ignore[misc]
            for line in lines:
                _render_line_mixed(
                    line, _line_x(line, text_y), text_y,
                    font=body_font, color=theme.text_color,
                    emoji_size=line_h, custom_emojis=custom_emojis,
                    bg=bg, draw=draw, pilmoji=_pm,
                )
                text_y += line_h + line_gap
    else:
        for line in lines:
            _render_line_mixed(
                line, text_pad_l, text_y,
                font=body_font, color=theme.text_color,
                emoji_size=line_h, custom_emojis=custom_emojis,
                bg=bg, draw=draw,
            )
            text_y += line_h + line_gap
    draw = ImageDraw.Draw(bg)

    if _no_pfp:
        # No avatar box — draw the label as a centred header above the prompt.
        if _header_text:
            _hb2 = draw.textbbox((0, 0), _header_text, font=header_font, stroke_width=_header_stroke)
            _hx = (width - int(_hb2[2] - _hb2[0])) // 2
            draw.text(
                (_hx + 2, _content_top + 2), _header_text, font=header_font,
                fill=(0, 0, 0), stroke_width=_header_stroke, stroke_fill=(0, 0, 0),
            )
            draw.text(
                (_hx, _content_top), _header_text, font=header_font,
                fill=theme.attribution_color, stroke_width=_header_stroke,
                stroke_fill=theme.attribution_color,
            )
    else:
        _square = pfp_shape == "square"
        _sq_r = max(6, int(pfp_d * 0.10))

        # Pfp drop shadow
        _pfp_sh = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        _soff = pfp_r // 5
        _sh_draw = ImageDraw.Draw(_pfp_sh)
        _sh_box = (px + _soff - 6, py + _soff - 6, px + pfp_d + _soff + 6, py + pfp_d + _soff + 6)
        if _square:
            _sh_draw.rounded_rectangle(_sh_box, radius=_sq_r + 6, fill=(0, 0, 0, 150))
        else:
            _sh_draw.ellipse(_sh_box, fill=(0, 0, 0, 150))
        _pfp_sh = _pfp_sh.filter(ImageFilter.GaussianBlur(radius=pfp_r // 3))
        _bg_rgba = bg.convert("RGBA")
        _bg_rgba.alpha_composite(_pfp_sh)
        bg = _bg_rgba.convert("RGB")
        draw = ImageDraw.Draw(bg)

        # Pfp — unblurred avatar, circle-cropped or rounded-square per pfp_shape
        avatar_img = Image.open(io.BytesIO(avatar_bytes)).convert("RGB")
        avatar_img = avatar_img.resize((pfp_d, pfp_d), Image.Resampling.LANCZOS)  # type: ignore[attr-defined]
        pfp_mask = Image.new("L", (pfp_d, pfp_d), 0)
        if _square:
            ImageDraw.Draw(pfp_mask).rounded_rectangle((0, 0, pfp_d - 1, pfp_d - 1), radius=_sq_r, fill=255)
        else:
            ImageDraw.Draw(pfp_mask).ellipse((0, 0, pfp_d - 1, pfp_d - 1), fill=255)
        bg.paste(avatar_img, (px, py), mask=pfp_mask)
        draw = ImageDraw.Draw(bg)

        # Double frame: outer cream + inner gold, matching the pfp shape
        _rg, _rt = 4, 3
        _outer = (px - _rg - _rt, py - _rg - _rt, px + pfp_d + _rg + _rt - 1, py + pfp_d + _rg + _rt - 1)
        _inner = (px - 3, py - 3, px + pfp_d + 2, py + pfp_d + 2)
        if _square:
            draw.rounded_rectangle(_outer, radius=_sq_r + _rg + _rt, outline=(255, 248, 220), width=_rt)
            draw.rounded_rectangle(_inner, radius=_sq_r + 3, outline=theme.attribution_color, width=3)
        else:
            draw.ellipse(_outer, outline=(255, 248, 220), width=_rt)
            draw.ellipse(_inner, outline=theme.attribution_color, width=3)

        # Author name centred below pfp
        if author_name:
            attr_text = f"— {author_name}"
            attr_bbox = draw.textbbox((0, 0), attr_text, font=attr_font)
            attr_w = attr_bbox[2] - attr_bbox[0]
            # Centre under the (left-shifted) pfp, but never let a long name slide
            # behind the left gold frame.
            ax = max(left_margin, pfp_cx - attr_w // 2)
            ay = pfp_cy + pfp_r + int(height * 0.04)
            draw.text((ax + 1, ay + 1), attr_text, font=attr_font, fill=(0, 0, 0))
            draw.text((ax, ay), attr_text, font=attr_font, fill=theme.attribution_color)

    # Apply rounded-rect transparency — pixels outside the card shape go fully transparent
    out = bg.convert("RGBA")
    out.putalpha(card_mask)

    # Border overlay — composited after transparency so it shows over the full card area
    if border_style is None:
        border_style = BORDERS["golden_poppy"]
    if border_style.path.exists():
        border = Image.open(border_style.path).convert("RGBA")
        if border_style.flip:
            border = border.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        border = border.resize((width, height), Image.Resampling.LANCZOS)  # type: ignore[attr-defined]
        if border_style.luma_key:
            lum = border.convert("RGB").convert("L")
            border.putalpha(lum.point([0 if i <= 20 else 255 for i in range(256)]))  # type: ignore[arg-type]
        out.alpha_composite(border)

    buf = io.BytesIO()
    out.save(buf, format="PNG")
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
