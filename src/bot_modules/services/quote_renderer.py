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
import unicodedata
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_ASSETS = Path("assets") / "fonts"
_INTER = _ASSETS / "Inter-Regular.ttf"
_PLAYFAIR = _ASSETS / "PlayfairDisplay-Regular.ttf"
_OSWALD = _ASSETS / "Oswald-Regular.ttf"
_CAVEAT = _ASSETS / "Caveat-Regular.ttf"
_BEBAS = _ASSETS / "BebasNeue-Regular.ttf"
# Arimo and Liberation Serif are the OFL, metric-compatible stand-ins for
# Helvetica/Arial and Times New Roman — the originals are proprietary and can't
# be bundled. Exposed to users as "Helvetica" and "Times".
_HELVETICA = _ASSETS / "Arimo-Regular.ttf"
_TIMES = _ASSETS / "LiberationSerif-Regular.ttf"

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


def _draw_text_layers(
    bg, draw, layers, text: str, *, font, stroke_width: int = 0
) -> None:
    """Draw ``text`` once per ``(xy, fill, stroke_fill)`` layer, emoji in colour.

    Callers pass a shadow layer then a foreground layer. pilmoji fetches emoji
    over HTTP, so a network blip would otherwise take out the whole card: on any
    failure this degrades to PIL's own text, which draws emoji as tofu but still
    renders the name. Re-drawing a layer pilmoji already got to is harmless —
    same string, same coordinates, same fill.
    """
    if _HAS_PILMOJI:
        try:
            with _Pilmoji(bg, source=_EmojiSource) as pm:  # type: ignore[misc]
                for xy, fill, stroke_fill in layers:
                    pm.text(
                        xy, text, font=font, fill=fill,
                        stroke_width=stroke_width, stroke_fill=stroke_fill,
                    )
            return
        except Exception:
            log.exception("quote_renderer: emoji text fell back to plain PIL")
    for xy, fill, stroke_fill in layers:
        draw.text(
            xy, text, font=font, fill=fill,
            stroke_width=stroke_width, stroke_fill=stroke_fill,
        )


def normalize_display_name(name: str) -> str:
    """Fold stylised Unicode letterforms in a display name back to plain letters.

    Discord names lean on Mathematical Alphanumeric Symbols and fullwidth forms
    (𝓟𝓻𝓲𝓷𝓬𝓮𝓼𝓼 𝓡𝓪𝓬𝓱𝓮𝓵 → Princess Rachel). None of the bundled TTFs carry those
    codepoints, so without this the whole name draws as tofu boxes. NFKC maps
    them to their compatibility equivalents; ordinary names are unchanged, and
    emoji have no decomposition so they survive for pilmoji to draw.
    """
    return unicodedata.normalize("NFKC", name)


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
    "times": _TIMES,
    "helvetica": _HELVETICA,
    "inter": _INTER,
    "playfair": _PLAYFAIR,
    "oswald": _OSWALD,
    "caveat": _CAVEAT,
    "bebas": _BEBAS,
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
    # Derive the writable area from this frame's own transparency and fit the
    # avatar + quote text inside it (see ``analyze_opening``). Only set for
    # uploaded per-guild frames; bundled borders keep their hand-tuned layout.
    mask_fit: bool = False
    # Render a slim drawn frame instead of compositing the (thick, baked-in)
    # frame, and shrink the decorative flower cluster into the corner. Keeps the
    # frame full-bleed so the tuned avatar/text layout is unaffected. Only the
    # bundled floral border opts in — see ``_composite_slim_border``.
    slim_frame: bool = False


BORDERS: dict[str, BorderStyle] = {
    "golden_poppy": BorderStyle(
        name="Golden Poppy",
        path=Path("assets") / "border.png",
        flip=True,
        luma_key=True,
        slim_frame=True,
    ),
    "midnight_frame": BorderStyle(
        name="Midnight Frame",
        path=Path("assets") / "midnightbordertransparent.png",
        flip=False,
        luma_key=False,
    ),
}

# Border key used for a guild's own uploaded frame. Not a member of BORDERS (that
# dict is global/bundled); the cog resolves it per-guild via ``custom_border_style``.
CUSTOM_BORDER_KEY = "custom"
CUSTOM_BORDER_NAME = "Custom (uploaded)"


def guild_border_dir(db_path: Path | str, guild_id: int) -> Path:
    """Per-guild folder holding an uploaded quote border, beside the DB.

    Mirrors the booster-swatch convention (``db_path.parent/<kind>/<guild_id>``)
    so the web dashboard writes exactly where the bot renderer reads.
    """
    return Path(db_path).parent / "quote_borders" / str(guild_id)


def guild_border_path(db_path: Path | str, guild_id: int) -> Path:
    """Canonical path of a guild's uploaded border (always a normalized PNG)."""
    return guild_border_dir(db_path, guild_id) / "border.png"


def custom_border_style(db_path: Path | str, guild_id: int) -> BorderStyle | None:
    """Return a ``BorderStyle`` for the guild's uploaded border, or None.

    The upload path re-encodes to a real-alpha RGBA PNG, so ``flip``/``luma_key``
    are both False — the frame is composited using its own transparency.
    """
    path = guild_border_path(db_path, guild_id)
    if path.is_file():
        return BorderStyle(
            name=CUSTOM_BORDER_NAME, path=path, flip=False, luma_key=False,
            mask_fit=True,
        )
    return None


# ── Slim frame + shrunk-flower composite ──────────────────────────────────────
#
# The bundled floral PNG bakes a thick gold frame and a large flower cluster into
# one image. To keep the frame full-bleed (so the tuned avatar/text layout stays
# put) while making the decoration less obtrusive, we draw a thin gold frame
# ourselves and composite only the flower cluster, shrunk into the corner.

# Sampled from the baked frame's gold; used for the drawn slim frame.
_SLIM_FRAME_GOLD = (232, 168, 30)
# Fraction of the flower cluster's baked size to render it at.
_SLIM_FLOWER_SCALE = 0.72
# Interior box holding the flower cluster, clear of the baked frame lines
# (fractions of width/height). Cropped, shrunk, then tucked into the corner.
_SLIM_FLOWER_CROP = (0.494, 0.30, 0.947, 0.93)


def _composite_slim_border(out, border_style: BorderStyle, width: int, height: int) -> None:
    """Draw a thin gold frame and tuck a shrunk flower cluster into the corner.

    Mutates ``out`` (an RGBA card with rounded-corner transparency already
    applied). Only used for borders with ``slim_frame`` set.
    """
    from PIL import Image, ImageDraw  # noqa: PLC0415

    # Key the frame+flowers exactly as the full composite would, then lift just
    # the interior flower cluster (the frame lines stay behind, redrawn below).
    border = Image.open(border_style.path).convert("RGBA")
    if border_style.flip:
        border = border.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    border = border.resize((width, height), Image.Resampling.LANCZOS)
    if border_style.luma_key:
        lum = border.convert("RGB").convert("L")
        border.putalpha(lum.point([0 if i <= 20 else 255 for i in range(256)]))

    inset = max(8, int(min(width, height) * 0.03))
    fl, ft, fr, fb = _SLIM_FLOWER_CROP
    flowers = border.crop((int(width * fl), int(height * ft),
                           int(width * fr), int(height * fb)))
    flowers = flowers.resize(
        (max(1, int(flowers.width * _SLIM_FLOWER_SCALE)),
         max(1, int(flowers.height * _SLIM_FLOWER_SCALE))),
        Image.Resampling.LANCZOS,
    )
    ax = width - inset - 6 - flowers.width
    ay = height - inset - 6 - flowers.height
    out.alpha_composite(flowers, (ax, ay))

    rad = max(20, int(min(width, height) * 0.10))
    ImageDraw.Draw(out).rounded_rectangle(
        (inset, inset, width - 1 - inset, height - 1 - inset),
        radius=rad, outline=_SLIM_FRAME_GOLD, width=max(2, int(min(width, height) * 0.006)),
    )


# ── Dominant border color ─────────────────────────────────────────────────────

_DOMINANT_CACHE: "dict[tuple, tuple[int, int, int]]" = {}


def dominant_border_color(border_style: BorderStyle) -> tuple[int, int, int]:
    """The border's dominant *vivid* color — used to tint the header text.

    Counts the frame's opaque pixels weighted by saturation×value, so the pick
    lands on the border's signature accent (Golden Poppy's gold) instead of the
    dark leaves or a keyed-out black background that a plain most-common count
    would surface. Cached by (path, mtime). Falls back to a warm gold if the
    frame can't be read or has no vivid pixels.
    """
    import numpy as np  # noqa: PLC0415

    fallback = (232, 168, 30)
    try:
        st = border_style.path.stat()
    except OSError:
        return fallback
    key = (str(border_style.path), st.st_mtime_ns)
    if key in _DOMINANT_CACHE:
        return _DOMINANT_CACHE[key]

    from PIL import Image  # noqa: PLC0415

    img = Image.open(border_style.path).convert("RGBA").resize((120, 120))
    if border_style.luma_key:
        lum = img.convert("RGB").convert("L")
        img.putalpha(lum.point([0 if i <= 20 else 255 for i in range(256)]))

    arr = np.asarray(img, dtype=np.float64).reshape(-1, 4)
    rgb = arr[arr[:, 3] >= 128][:, :3]
    if rgb.shape[0] == 0:
        _DOMINANT_CACHE[key] = fallback
        return fallback

    # Vividness weight per pixel = saturation × value (HSV), so dark leaves and a
    # keyed-out background carry ~0 weight and the signature accent wins.
    mx = rgb.max(axis=1)
    mn = rgb.min(axis=1)
    value = mx / 255.0
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-9), 0.0)
    weight = sat * value

    # Accumulate weight into 24-wide colour buckets and take the heaviest.
    quant = (rgb // 24).astype(np.int64) * 24
    codes = quant[:, 0] * 65536 + quant[:, 1] * 256 + quant[:, 2]
    uniq, inverse = np.unique(codes, return_inverse=True)
    totals = np.zeros(uniq.shape[0])
    np.add.at(totals, inverse, weight)
    best = int(uniq[int(totals.argmax())])

    # Unpack the winning code and snap to the centre of its 24-wide cell.
    color = (
        min(255, (best // 65536) + 12),
        min(255, (best // 256) % 256 + 12),
        min(255, best % 256 + 12),
    )
    _DOMINANT_CACHE[key] = color
    return color


# ── Border-shape masking ──────────────────────────────────────────────────────
#
# For an uploaded frame we don't assume a fixed text column — we read the frame's
# own transparency and fit the avatar + quote inside the hole it leaves. The
# geometry here is pure (numpy over the alpha channel); the actual text flow that
# consumes it lives inside render_quote_card so it can reuse the emoji-aware
# measurer. Results are cached by (path, mtime, size) since a frame is analysed
# once and rendered many times.


@dataclass
class BorderOpening:
    """The transparent hole in a frame, as per-row [left, right] spans.

    ``left``/``right`` are x-edges valid for rows in ``[top, bot]`` (the vertical
    band where the card centre column is see-through). ``pfp`` is a fitted
    ``(cx, cy, r)`` avatar disc on the left, or None when no disc fits with room
    left for text (the card then falls back to centred, avatar-as-background).
    """
    left: "list[int]"
    right: "list[int]"
    top: int
    bot: int
    pfp: "tuple[int, int, int] | None"


_MASK_CACHE: "dict[tuple, BorderOpening | None]" = {}


def _border_alpha(border_style: BorderStyle, width: int, height: int):
    """Alpha channel (H×W uint8) of the frame exactly as it will be composited."""
    import numpy as np  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    img = Image.open(border_style.path).convert("RGBA")
    if border_style.flip:
        img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    img = img.resize((width, height), Image.Resampling.LANCZOS)
    return np.array(img.getchannel("A"))


def _erode(mask, k: int):
    """Separable binary erosion by a (2k+1) square — insets the passable area."""
    if k <= 0:
        return mask
    h = mask.copy()
    for d in range(1, k + 1):
        h[:, d:] &= mask[:, :-d]
        h[:, :-d] &= mask[:, d:]
    v = h.copy()
    for d in range(1, k + 1):
        v[d:, :] &= h[:-d, :]
        v[:-d, :] &= h[d:, :]
    return v


def _fit_pfp(passable, top: int, bot: int, left: "list[int]", right: "list[int]",
             width: int, height: int):
    """Largest left-hugging avatar disc that fits the opening, or None.

    Fits against an inflated radius so the double ring and drop shadow (drawn
    ~1.15×r plus a down-right offset) stay inside the frame, not just the disc.
    """
    import math  # noqa: PLC0415

    cyp = (top + bot) // 2
    r0 = int(min(width, height) * 0.16)
    r_min = int(min(width, height) * 0.11)

    def r_eff(r: int) -> int:
        # Drawn footprint: double ring (~r+7) plus the down-right drop shadow.
        return int(r * 1.15) + r // 5 + 6

    def fits(cxp: int, r: int) -> bool:
        re = r_eff(r)
        if cxp - re < 0 or cxp + re >= width or cyp - re < 0 or cyp + re >= height:
            return False
        for y in range(cyp - re, cyp + re + 1):
            dx = int(math.sqrt(max(0, re * re - (y - cyp) ** 2)))
            if not passable[y, cxp - dx:cxp + dx + 1].all():
                return False
        return True

    lo, hi, best = r_min, r0, None
    while lo <= hi:
        r = (lo + hi) // 2
        cxp = left[cyp] + r_eff(r)  # push right until the whole footprint clears
        if fits(cxp, r):
            best = (cxp, cyp, r)
            lo = r + 1
        else:
            hi = r - 1
    if best is None:
        return None
    cxp, cyp, r = best
    # Only worth an avatar if meaningful text still fits to its right.
    if right[cyp] - (cxp + r_eff(r)) < int(width * 0.22):
        return None
    return best


def analyze_border_opening(
    border_style: BorderStyle, width: int, height: int
) -> BorderOpening | None:
    """Detect a frame's usable opening + a fitted avatar disc, or None.

    None means there's no see-through region around the card centre big enough to
    hold a quote — the upload path rejects such frames so rendering always has a
    valid opening to fit into.
    """
    try:
        st = border_style.path.stat()
    except OSError:
        return None
    key = (str(border_style.path), st.st_mtime_ns, width, height)
    if key in _MASK_CACHE:
        return _MASK_CACHE[key]

    result = _compute_border_opening(border_style, width, height)
    _MASK_CACHE[key] = result
    return result


def _compute_border_opening(
    border_style: BorderStyle, width: int, height: int
) -> BorderOpening | None:
    alpha = _border_alpha(border_style, width, height)
    margin = max(8, int(min(width, height) * 0.025))
    passable = _erode(alpha < 128, margin)

    cx, cyc = width // 2, height // 2
    col = passable[:, cx]
    if not col[cyc]:
        return None  # centre covered — no usable opening

    top = cyc
    while top - 1 >= 0 and col[top - 1]:
        top -= 1
    bot = cyc
    while bot + 1 < height and col[bot + 1]:
        bot += 1

    # Require a band that can hold at least ~2 lines and a readable width.
    if (bot - top) < int(height * 0.20):
        return None

    left = [cx] * height
    right = [cx] * height
    for y in range(top, bot + 1):
        row = passable[y]
        lx = cx
        while lx - 1 >= 0 and row[lx - 1]:
            lx -= 1
        rx = cx
        while rx + 1 < width and row[rx + 1]:
            rx += 1
        left[y], right[y] = lx, rx

    if (right[cyc] - left[cyc]) < int(width * 0.30):
        return None

    pfp = _fit_pfp(passable, top, bot, left, right, width, height)
    return BorderOpening(left=left, right=right, top=top, bot=bot, pfp=pfp)


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
    header_font_style: str = "helvetica",
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

    ``font_style`` sets the quote-body typeface; ``header_font_style`` sets the
    no-pfp header's, defaulting to Helvetica so the editorial pairing (sans header
    over serif body) holds regardless of the body font the caller picks.
    """
    from PIL import Image, ImageDraw, ImageFilter  # noqa: PLC0415

    if len(text) > QUOTE_MAX_CHARS:
        text = text[:QUOTE_MAX_CHARS - 1] + "…"

    # Fold stylised letterforms once, up front: author_name feeds both the
    # attribution line and the no-pfp header.
    author_name = normalize_display_name(author_name)

    # Blurred background — when there's a left-side pfp, push the face left so it
    # doesn't sit under the text column; with no pfp keep the image centred.
    _no_pfp = pfp_shape == "none"

    # Uploaded frames drive their own layout: read the transparent opening and fit
    # the avatar + text inside it. A frame with no usable opening (rejected at
    # upload) falls back to the standard layout; one with no room for a disc
    # renders centred (avatar as background, author as a header).
    _mask = border_style is not None and border_style.mask_fit
    _mask_opening = (
        analyze_border_opening(border_style, width, height)
        if _mask and border_style is not None
        else None
    )
    if _mask and _mask_opening is None:
        _mask = False
    if _mask and _mask_opening is not None and _mask_opening.pfp is None:
        _no_pfp = True

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
    if _mask and _mask_opening is not None and _mask_opening.pfp is not None:
        pfp_cx, pfp_cy, pfp_r = _mask_opening.pfp
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
    # a dedicated font that's larger than the body; a light stroke (there's no bold
    # TTF in assets/) plus the drop shadow keeps it legible without reading as a
    # heavy, cartoonish title — the size alone carries the "header" role.
    _header_text = author_name if (_no_pfp and author_name) else ""
    header_size = max(body_size + 10, int(body_size * 1.6))
    header_font = _load_font(header_size, header_font_style)
    _header_stroke = max(1, header_size // 40)
    _header_h = _header_gap = 0
    if _header_text:
        _hb = draw.textbbox((0, 0), _header_text, font=header_font, stroke_width=_header_stroke)
        _header_h = int(_hb[3] - _hb[1])
        _header_gap = max(14, line_h)
    _header_block = (_header_h + _header_gap) if _header_text else 0

    left_margin = int(width * 0.06)

    if _mask and _mask_opening is not None:
        # Fit the quote into the frame's own opening: per-row left/right bounds
        # from the transparency, flowing around the fitted avatar disc, with the
        # body font auto-shrunk until the block fits the opening's vertical band.
        op = _mask_opening
        # A disc only affects layout when one is actually drawn — banner mode
        # (pfp_shape="none") fits text into the full opening with no avatar.
        _has_disc = op.pfp is not None and not _no_pfp
        _mgap = max(10, int(width * 0.02))
        # Breathing room between text and the frame: ~one character horizontally,
        # a little top/bottom so lines don't kiss the opening edge.
        _linset = max(6, _full_measure("n"))
        _vpad = max(6, int(height * 0.02))
        _attr_reserve = int(attr_size * 1.7) if (_has_disc and author_name) else 0

        def _m_left(y: int) -> int:
            y = min(max(int(y), op.top), op.bot)
            lb = op.left[y] + _linset
            if _has_disc:
                _cxp, _cyp, _rr = op.pfp  # type: ignore[misc]
                # Keep the quote as a clean rectangular column to the RIGHT of the
                # avatar — every line starts at the disc's right edge, so the top
                # and bottom lines don't jut left over/under it.
                lb = max(lb, _cxp + int(_rr * 1.15) + _mgap)
            return lb

        def _m_right(y: int) -> int:
            y = min(max(int(y), op.top), op.bot)
            return op.right[y] - _linset

        _band_top = op.top + _vpad + (_header_block if not _has_disc else 0)
        _band_bot = op.bot - _vpad - _attr_reserve
        _band_h = max(1, _band_bot - _band_top)

        def _flow_mask(start_y: int, lh: int, lg: int, measure) -> list[str]:
            out: list[str] = []
            for para in _quoted_text.splitlines():
                words = para.split()
                if not words:
                    out.append("")
                    continue
                cur = ""
                for w in words:
                    y = start_y + len(out) * (lh + lg)
                    cand = f"{cur} {w}".strip()
                    if measure(cand) <= (_m_right(y) - _m_left(y)) or not cur:
                        cur = cand
                    else:
                        out.append(cur)
                        cur = w
                if cur:
                    out.append(cur)
            return out or [""]

        # Auto-fit: largest size whose (twice-reflowed) block fits the band.
        _chosen = None
        for _sz in range(body_size, 15, -2):
            _f = _load_font(_sz, font_style)
            _pb = draw.textbbox((0, 0), "Ag", font=_f)
            _lh = int(_pb[3] - _pb[1])
            _lg = max(6, _lh // 5)
            if _HAS_PILMOJI:
                def _bm(t: str, _ff=_f) -> int:
                    return _emoji_getsize(t, font=_ff)[0]  # type: ignore[misc]
            else:
                def _bm(t: str, _ff=_f) -> int:
                    return int(draw.textbbox((0, 0), t, font=_ff)[2] - draw.textbbox((0, 0), t, font=_ff)[0])
            _meas = _make_emoji_measure(_bm, _lh)
            _ls = _flow_mask(_band_top, _lh, _lg, _meas)
            _y0 = _band_top + max(0, (_band_h - len(_ls) * (_lh + _lg)) // 2)
            _ls = _flow_mask(_y0, _lh, _lg, _meas)
            if len(_ls) * (_lh + _lg) <= _band_h or _sz <= 17:
                _chosen = (_f, _lh, _lg, _meas, _ls)
                break
        assert _chosen is not None
        body_font, line_h, line_gap, _full_measure, lines = _chosen

        # Ellipsize if even the smallest size overflows the opening.
        _max_lines = max(1, _band_h // (line_h + line_gap))
        if len(lines) > _max_lines:
            lines = lines[:_max_lines]
            lines[-1] = lines[-1].rstrip("” ").rstrip() + "…”"

        _blk = len(lines) * (line_h + line_gap)
        text_y_start = _band_top + max(0, (_band_h - _blk) // 2)
        _content_top = op.top + max(6, int(height * 0.03))

        def _line_x(s: str, y: int) -> int:
            lo = _m_left(y)
            if not _no_pfp:
                return lo  # quote-with-avatar: keep the left-aligned column
            hi = _m_right(y)  # banner over a custom frame: centre in the opening
            return lo + max(0, (hi - lo - _full_measure(s)) // 2)
    elif _no_pfp:
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
            # Centred: announcement banners read centred. Start from the true card
            # centre, then shove left only if the line would otherwise reach into
            # the floral corner (wrapping via _avail_w already bounds the width).
            lw = _full_measure(s)
            x = (width - lw) // 2
            right_limit = int(_flower_left(y) - _gap3)
            if x + lw > right_limit:
                x = right_limit - lw
            return max(left_margin, x)
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
                line, _line_x(line, text_y), text_y,
                font=body_font, color=theme.text_color,
                emoji_size=line_h, custom_emojis=custom_emojis,
                bg=bg, draw=draw,
            )
            text_y += line_h + line_gap
    draw = ImageDraw.Draw(bg)

    if _no_pfp:
        # No avatar box — draw the label as a centred header above the prompt,
        # tinted with the border's dominant colour so the title echoes the frame.
        if _header_text:
            _hdr_color = dominant_border_color(border_style or BORDERS["golden_poppy"])
            if _HAS_PILMOJI:
                # getsize takes no stroke_width; add it back so an emoji-bearing
                # header centres on the same width pilmoji actually draws.
                _hw = _emoji_getsize(_header_text, font=header_font)[0]  # type: ignore[misc]
                _hw += _header_stroke * 2
            else:
                _hb2 = draw.textbbox(
                    (0, 0), _header_text, font=header_font, stroke_width=_header_stroke
                )
                _hw = int(_hb2[2] - _hb2[0])
            _hx = (width - _hw) // 2
            _draw_text_layers(
                bg, draw,
                [
                    ((_hx + 2, _content_top + 2), (0, 0, 0), (0, 0, 0)),
                    ((_hx, _content_top), _hdr_color, _hdr_color),
                ],
                _header_text, font=header_font, stroke_width=_header_stroke,
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
            if _HAS_PILMOJI:
                # Measure through pilmoji so an emoji in the name contributes its
                # drawn width — textbbox would only count the tofu box it replaces.
                attr_w, attr_h = _emoji_getsize(attr_text, font=attr_font)  # type: ignore[misc]
            else:
                attr_bbox = draw.textbbox((0, 0), attr_text, font=attr_font)
                attr_w = attr_bbox[2] - attr_bbox[0]
                attr_h = attr_bbox[3] - attr_bbox[1]
            # Centre under the (left-shifted) pfp, but never let a long name slide
            # behind the left gold frame.
            ax = max(left_margin, pfp_cx - attr_w // 2)
            ay = pfp_cy + pfp_r + int(height * 0.04)
            if _mask and _mask_opening is not None:
                # Keep the attribution inside the frame's opening.
                ay = min(ay, _mask_opening.bot - attr_h - 4)
                _ry = min(max(int(ay), _mask_opening.top), _mask_opening.bot)
                ax = max(_mask_opening.left[_ry] + 4, pfp_cx - attr_w // 2)
            _draw_text_layers(
                bg, draw,
                [
                    ((ax + 1, ay + 1), (0, 0, 0), None),
                    ((ax, ay), theme.attribution_color, None),
                ],
                attr_text, font=attr_font,
            )

    # Apply rounded-rect transparency — pixels outside the card shape go fully transparent
    out = bg.convert("RGBA")
    out.putalpha(card_mask)

    # Border overlay — composited after transparency so it shows over the full card area
    if border_style is None:
        border_style = BORDERS["golden_poppy"]
    if border_style.slim_frame and border_style.path.exists():
        _composite_slim_border(out, border_style, width, height)
    elif border_style.path.exists():
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
