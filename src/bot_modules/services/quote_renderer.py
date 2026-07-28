"""Generic text-to-image quote card renderer.

Supports two render paths:
- render_quote()       — dark, solid-bg card (used by legacy callers)
- render_quote_card()  — pfp-as-background with color grading (used by QuoteCog)

Fonts are loaded from assets/fonts/; missing files raise FileNotFoundError loudly
so the problem is immediately visible rather than silently degrading.
"""
from __future__ import annotations

import colorsys
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

# Each Twemoji glyph is fetched over HTTP the first time it's drawn. Bound that
# fetch: pilmoji sets no timeout, so a slow or stalled CDN would block the render
# worker thread indefinitely — and since display-name emoji put this on the hot
# path for most cards, one stalled request could tie up the whole pool.
_EMOJI_FETCH_TIMEOUT = 5.0

try:
    from pilmoji import Pilmoji as _Pilmoji
    from pilmoji.helpers import getsize as _emoji_getsize
    from pilmoji.source import TwemojiEmojiSource as _BaseEmojiSource

    class _EmojiSource(_BaseEmojiSource):  # type: ignore[misc,valid-type]
        """Twemoji source whose HTTP fetch can't hang.

        pilmoji's ``request`` passes ``REQUEST_KWARGS`` to both the requests and
        urllib backends but never a timeout. ``timeout`` can't live in
        ``REQUEST_KWARGS`` because ``urllib.request.Request`` rejects it (only
        ``urlopen`` takes it), so override ``request`` to thread it into whichever
        backend pilmoji picked. A timeout raises, which the render's callers catch
        and degrade to tofu — far better than a hung thread.
        """

        def request(self, url: str) -> bytes:
            from pilmoji import source as _src

            if getattr(_src, "_has_requests", False):
                with self._requests_session.get(
                    url, timeout=_EMOJI_FETCH_TIMEOUT, **self.REQUEST_KWARGS
                ) as response:
                    if response.ok:
                        return response.content
                    response.raise_for_status()
                    return b""
            from urllib.request import Request, urlopen

            req = Request(url, **self.REQUEST_KWARGS)
            with urlopen(req, timeout=_EMOJI_FETCH_TIMEOUT) as response:
                return response.read()

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
    """Draw ``text`` once per ``(xy, fill, stroke_fill)`` layer, emoji in color.

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
    desaturate: float                    # 0.0=gray, 1.0=full color; applied before overlay
    # Text colors
    text_color: tuple[int, int, int]
    attribution_color: tuple[int, int, int]
    # Vignette darkness (0.0 = none, 1.0 = black edges)
    vignette_strength: float


THEMES: dict[str, QuoteTheme] = {
    # Key is historical (stored on every rendered card); the label is the
    # colour, not a server name.
    "golden_meadow": QuoteTheme(
        name="Golden",
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

def theme_from_accent(
    accent_rgb: tuple[int, int, int], *, name: str = "Server Color"
) -> QuoteTheme:
    """Build a QuoteTheme whose color grading follows a guild's brand accent.

    ``accent_rgb`` is the guild accent (from ``resolve_accent_color``). The
    overlay takes the accent directly; body text and the attribution line are
    derived as a near-white and a lighter saturated tint of the *same hue*, so a
    pink, teal, or red brand color each yields a coherent, readable card — the
    same cream-body / brighter-name split the hand-tuned ``golden_meadow`` theme
    uses. The non-color knobs (overlay alpha, desaturation, vignette) mirror
    ``golden_meadow`` so accent-derived cards sit alongside it consistently.
    """
    r, g, b = (max(0, min(255, int(c))) for c in accent_rgb)
    h, s, _v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    # Near-white body text carrying just a hint of the brand hue.
    tr, tg, tb = colorsys.hsv_to_rgb(h, min(s, 0.10), 1.0)
    # Lighter, more saturated attribution line, echoing golden_meadow's gold name.
    ar, ag, ab = colorsys.hsv_to_rgb(h, min(max(s, 0.35), 0.55), 1.0)
    return QuoteTheme(
        name=name,
        overlay_color=(r, g, b),
        overlay_alpha=0.38,
        desaturate=0.55,
        text_color=(round(tr * 255), round(tg * 255), round(tb * 255)),
        attribution_color=(round(ar * 255), round(ag * 255), round(ab * 255)),
        vignette_strength=0.72,
    )


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


def slim_flower_rect(width: int, height: int, flower_w: int, flower_h: int) -> "tuple[int, int]":
    """Top-left corner the slim border's flower cluster is pasted at.

    Shared with ``_composite_slim_border`` so the layout's idea of where the
    flowers sit can't drift from where they're actually drawn. Text that must stay
    clear of the corner bounds itself against this.
    """
    inset = max(8, int(min(width, height) * 0.03))
    return width - inset - 6 - flower_w, height - inset - 6 - flower_h


_FLOWER_EDGE_CACHE: "dict[tuple, list[int]]" = {}


def slim_flower_left_edge(
    border_style: BorderStyle, width: int, height: int
) -> "list[int] | None":
    """Per-row leftmost *opaque* pixel of the slim border's flower cluster.

    ``[y] -> x``, or ``width`` for rows the cluster doesn't reach. ``None`` if the
    frame can't be read (callers then fall back to the cluster's bounding box).

    A bounding box is far too blunt here: the cluster's upper rows are a few sparse
    buds, so reserving its full width for them costs real typography — every quote
    length gained a wrapped line, and short quotes wrap narrower than they need to.
    Reading the actual alpha lets text run right up to the petals and no further.
    Cached per (frame, mtime, size) like the other frame analyses, since this loads
    and resizes the PNG.
    """
    try:
        st = border_style.path.stat()
    except OSError:
        return None
    key = (str(border_style.path), st.st_mtime_ns, width, height)
    if key in _FLOWER_EDGE_CACHE:
        return _FLOWER_EDGE_CACHE[key]

    import numpy as np  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    border = Image.open(border_style.path).convert("RGBA")
    if border_style.flip:
        border = border.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    border = border.resize((width, height), Image.Resampling.LANCZOS)
    if border_style.luma_key:
        lum = border.convert("RGB").convert("L")
        border.putalpha(lum.point([0 if i <= 20 else 255 for i in range(256)]))

    fl, ft, fr, fb = _SLIM_FLOWER_CROP
    flowers = border.crop((int(width * fl), int(height * ft),
                           int(width * fr), int(height * fb)))
    flowers = flowers.resize(
        (max(1, int(flowers.width * _SLIM_FLOWER_SCALE)),
         max(1, int(flowers.height * _SLIM_FLOWER_SCALE))),
        Image.Resampling.LANCZOS,
    )
    ox, oy = slim_flower_rect(width, height, flowers.width, flowers.height)

    # Treat only solidly-drawn pixels as blocking; the cluster's edges feather out
    # over a few near-transparent pixels that text can safely sit against.
    solid = np.array(flowers.getchannel("A")) > 96
    edge = [width] * height
    for row in range(solid.shape[0]):
        cols = np.nonzero(solid[row])[0]
        if cols.size:
            y = oy + row
            if 0 <= y < height:
                edge[y] = ox + int(cols[0])
    _FLOWER_EDGE_CACHE[key] = edge
    return edge


def flower_limit(edge: "list[int]", y: int, h: int) -> int:
    """Rightmost x a line of height ``h`` starting at ``y`` can reach.

    The tightest bound over the rows the line actually covers — a line is a band,
    not a single row, so petals dipping into its lower rows must bound it too.
    """
    if not edge:
        return 1 << 30
    lo = max(0, min(y, len(edge) - 1))
    hi = max(0, min(y + max(1, h) - 1, len(edge) - 1))
    return min(edge[lo:hi + 1], default=1 << 30)


def slim_flower_bound(width: int, height: int) -> "tuple[int, int]":
    """``(left_x, top_y)`` of the slim border's flower cluster, from constants alone.

    Derived without opening the PNG so layout code can call it cheaply. The cluster
    has its own transparency, so its sparse upper rows are treated as occupied —
    conservative in the safe direction (text keeps clear rather than risking
    petals).
    """
    fl, ft, fr, fb = _SLIM_FLOWER_CROP
    crop_w = int(width * fr) - int(width * fl)
    crop_h = int(height * fb) - int(height * ft)
    fw = max(1, int(crop_w * _SLIM_FLOWER_SCALE))
    fh = max(1, int(crop_h * _SLIM_FLOWER_SCALE))
    return slim_flower_rect(width, height, fw, fh)


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
    out.alpha_composite(flowers, slim_flower_rect(width, height, flowers.width, flowers.height))

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

    # Accumulate weight into 24-wide color buckets and take the heaviest.
    quant = (rgb // 24).astype(np.int64) * 24
    codes = quant[:, 0] * 65536 + quant[:, 1] * 256 + quant[:, 2]
    uniq, inverse = np.unique(codes, return_inverse=True)
    totals = np.zeros(uniq.shape[0])
    np.add.at(totals, inverse, weight)
    best = int(uniq[int(totals.argmax())])

    # Unpack the winning code and snap to the center of its 24-wide cell.
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
# measurer. Results are cached by (path, mtime, size) since a frame is analyzed
# once and rendered many times.


@dataclass
class BorderOpening:
    """The transparent hole in a frame, as per-row [left, right] spans.

    ``left``/``right`` are x-edges valid for rows in ``[top, bot]`` (the vertical
    band where the card center column is see-through). ``pfp`` is a fitted
    ``(cx, cy, r)`` avatar disc on the left, or None when no disc fits with room
    left for text (the card then falls back to centered, avatar-as-background).
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

    None means there's no see-through region around the card center big enough to
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
        return None  # center covered — no usable opening

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


def ellipsize_line(line: str, avail_w: int, measure) -> str:
    """Close a truncated quote with `…”`, shrunk until it fits ``avail_w``.

    Appending the ellipsis *after* wrapping adds two glyphs the wrapper never
    accounted for, and the capped line is always the block's bottom line — exactly
    the row where the floral corner (or a narrowing frame opening) leaves least
    room. Without re-fitting here, the closing text is drawn over the artwork the
    surrounding layout exists to avoid.

    ``measure(text) -> width``.
    """
    base = line.rstrip("” ").rstrip()
    while base and measure(f"{base}…”") > avail_w:
        base = base[:-1].rstrip()
    return f"{base}…”"


def attribution_y(
    *, quote_bot: int, attr_h: int, gap: int, limit_bot: "int | None" = None
) -> int:
    """Top edge of the attribution line, as a pure function of the layout.

    The line hangs a ``gap`` below the quote block and ``limit_bot`` (the card's or
    a frame opening's bottom edge) clamps it up so it stays on the card. Resolved
    before x, because the column's horizontal bounds are read at whatever row the
    line ends up on.

    Its x comes from the **quote column**, not from centring under the avatar.
    Centring on the disc (``pfp_cx - attr_w // 2``) drove the anchor negative for
    any name wider than the disc, collapsing it onto the left-margin floor and
    straight through the avatar's lower-left arc. The column already begins clear
    of the avatar's drawn footprint, so no name length can reach back into it —
    which is also why clamping upward here is safe.
    """
    ay = quote_bot + gap
    if limit_bot is not None:
        ay = min(ay, limit_bot - attr_h - 4)
    return ay


def attribution_block_h(attr_h: int, gap: int) -> int:
    """Vertical space the attribution claims, for centring quote+name as one group."""
    return attr_h + gap if attr_h else 0


def fit_attribution_text(
    text: str, avail_w: int, measure, sizes: "list[int]"
) -> "tuple[int, str]":
    """Largest size in ``sizes`` whose measured width fits ``avail_w``.

    Anchoring the attribution to the quote column means it can no longer borrow the
    card's left margin for extra room, so a very long name has to be made to fit
    rather than run off the column's right edge. Shrink first — a slightly smaller
    byline still reads — and only truncate at the floor size, where nothing else
    would keep the name on the card.

    ``measure(text, size) -> width`` so the caller owns font loading (and whether
    emoji widths come from pilmoji or a bare textbbox).
    """
    if not sizes:
        return 0, text
    for sz in sizes:
        if measure(text, sz) <= avail_w:
            return sz, text
    sz = sizes[-1]
    t = text
    while t and measure(f"{t}…", sz) > avail_w:
        t = t[:-1]
    # Truncating to nothing still leaves the ellipsis wider than the space, which
    # means the column is too narrow for any name. Drop the line rather than draw
    # over whatever is beside it.
    return sz, f"{t.rstrip()}…" if t else ""


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
    the prompt is centered across the card and ``author_name`` becomes a centered
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
    # doesn't sit under the text column; with no pfp keep the image centered.
    _no_pfp = pfp_shape == "none"

    # Uploaded frames drive their own layout: read the transparent opening and fit
    # the avatar + text inside it. A frame with no usable opening (rejected at
    # upload) falls back to the standard layout; one with no room for a disc
    # renders centered (avatar as background, author as a header).
    # Resolve the default frame BEFORE laying anything out. It used to be filled in
    # just before compositing, which meant a caller passing no border laid its text
    # out as if the card were bare and then had the poppy frame drawn over it —
    # putting the last lines back under the petals the layout is meant to avoid.
    if border_style is None:
        border_style = BORDERS["golden_poppy"]

    _mask = border_style.mask_fit
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

    body_size = max(32, width // 19)
    attr_size = max(19, width // 33)
    body_font = _load_font(body_size, font_style)
    attr_font = _load_font(attr_size, font_style)

    draw = ImageDraw.Draw(bg)
    probe = draw.textbbox((0, 0), "Ag", font=body_font)
    line_h = int(probe[3] - probe[1])
    line_gap = max(6, line_h // 5)

    # Measure the attribution up front: the quote block is centred as a group with
    # the name below it, so the layout has to know the name's height before it can
    # pick the text's top. Drawn later (see attribution_pos) from these same
    # numbers, so measurement and placement can't drift apart.
    attr_text = f"— {author_name}" if author_name else ""
    attr_w = attr_h = 0
    if attr_text and not _no_pfp:
        # Height from the font's own line box (ascent + descent), NOT from the
        # string's ink bbox: an ink bbox varies with which letters the name happens
        # to contain ("Bob" 20px vs "gg" 21px vs a parenthesised name 30px), which
        # would make the reserve and the centring jitter per user. The line box is
        # constant for a given size, and identical whether or not pilmoji is
        # installed, so the geometry doesn't fork on an optional dependency.
        attr_h = sum(attr_font.getmetrics())
        if _HAS_PILMOJI:
            # Width still comes through pilmoji so an emoji contributes its drawn
            # width — textbbox would only count the tofu box it replaces.
            attr_w = _emoji_getsize(attr_text, font=attr_font)[0]  # type: ignore[misc]
        else:
            _ab = draw.textbbox((0, 0), attr_text, font=attr_font)
            attr_w = int(_ab[2] - _ab[0])
    _attr_gap = max(12, int(attr_size * 0.85))
    _attr_block = attribution_block_h(attr_h, _attr_gap)

    if _HAS_PILMOJI:
        def _base_m(t: str) -> int:
            return _emoji_getsize(t, font=body_font)[0]  # type: ignore[misc]
    else:
        def _base_m(t: str) -> int:  # type: ignore[misc]
            return int(draw.textbbox((0, 0), t, font=body_font)[2] - draw.textbbox((0, 0), t, font=body_font)[0])
    _quoted_text = f"“{text}”"
    _full_measure = _make_emoji_measure(_base_m, line_h)

    # No-pfp mode turns the label into a centered header above the prompt. Give it
    # a dedicated font that's larger than the body; a light stroke (there's no bold
    # TTF in assets/) plus the drop shadow keeps it legible without reading as a
    # heavy, cartoonish title — the size alone carries the "header" role.
    _header_text = author_name if (_no_pfp and author_name) else ""
    header_size = max(body_size + 10, int(body_size * 1.6))
    header_font = _load_font(header_size, header_font_style)
    _header_stroke = max(1, header_size // 40)
    if _header_text:
        # Fit the header to the card the same way the attribution is fitted to its
        # column. It was drawn from a bare centre offset with no bound, so a long
        # display name ran clean off both edges (a 39-char name measured 1350px on a
        # 900px card, drawn from x=-225). For four of the callers this header is the
        # *only* place the name appears, so losing its ends loses the attribution.
        def _hdr_measure(t: str, sz: int) -> int:
            _hf = _load_font(sz, header_font_style)
            _w = (
                _emoji_getsize(t, font=_hf)[0]  # type: ignore[misc]
                if _HAS_PILMOJI
                else int(draw.textbbox((0, 0), t, font=_hf)[2]
                         - draw.textbbox((0, 0), t, font=_hf)[0])
            )
            return _w + 2 * max(1, sz // 40)  # stroke widens the drawn glyphs

        _hdr_avail = width - 2 * int(width * 0.06)
        if _hdr_measure(_header_text, header_size) > _hdr_avail:
            header_size, _header_text = fit_attribution_text(
                _header_text, _hdr_avail, _hdr_measure,
                list(range(header_size, max(18, body_size // 2) - 1, -1)),
            )
            header_font = _load_font(header_size, header_font_style)
            _header_stroke = max(1, header_size // 40)
    _header_h = _header_gap = 0
    if _header_text:
        # Line box (ascent + descent), not the string's ink bbox. The header's
        # height sets where the body starts, and the body's usable width narrows
        # toward the floral corner — so an ink bbox made the *quote* re-wrap based
        # on which glyphs the author's name happened to contain (a name with
        # parentheses pushed the body down a line). Same reason the attribution
        # measures its line box; see attribution height above.
        _header_h = sum(header_font.getmetrics()) + 2 * _header_stroke
        _header_gap = max(14, line_h)
    _header_block = (_header_h + _header_gap) if _header_text else 0

    left_margin = int(width * 0.06)

    # Where the attribution aligns, per layout branch: the quote column's left edge
    # at a given row, plus any frame bottom that clamps it. Defaults cover the
    # no-pfp branch, which draws a centered header instead of an attribution.
    def _col_left(_y: int) -> int:
        return text_pad_l

    def _col_right(_y: int) -> int:
        return text_pad_l + text_col_w

    _attr_left, _attr_right = _col_left, _col_right
    _attr_limit_bot: "int | None" = None

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
        # Reserve the attribution's real line box + gap rather than a 1.7×font-size
        # estimate. The two are close at the default size (49 vs 45 px), so this is
        # a consistency fix, not a big one: the reserve, the group centring and the
        # draw now all derive from the same numbers instead of an independent guess
        # that would drift if the gap or font ever changed.
        _attr_reserve = _attr_block if (_has_disc and author_name) else 0

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
        _mcapped = len(lines) > _max_lines
        if _mcapped:
            lines = lines[:_max_lines]

        _blk = len(lines) * (line_h + line_gap)
        text_y_start = _band_top + max(0, (_band_h - _blk) // 2)
        if _mcapped:
            # Fit the closing `…”` to the last line's own row — in an opening that
            # narrows toward the bottom, that row is the tightest of the block.
            _mlast_y = text_y_start + (len(lines) - 1) * (line_h + line_gap)
            lines[-1] = ellipsize_line(
                lines[-1], _m_right(_mlast_y) - _m_left(_mlast_y), _full_measure
            )
        _content_top = op.top + max(6, int(height * 0.03))

        def _line_x(s: str, y: int) -> int:
            lo = _m_left(y)
            if not _no_pfp:
                return lo  # quote-with-avatar: keep the left-aligned column
            hi = _m_right(y)  # banner over a custom frame: center in the opening
            return lo + max(0, (hi - lo - _full_measure(s)) // 2)

        _attr_left = _m_left          # attribution shares the quote column's bounds
        _attr_right = _m_right
        _attr_limit_bot = op.bot
    elif _no_pfp:
        # Left-justified body: keep ~one character of buffer off the left frame.
        left_margin += max(1, _full_measure("n"))
        # The brand's flowers fill the bottom-right corner. Carve a matching
        # exclusion so the usable right edge drops toward the bottom; each line is
        # centered within the remaining [left_margin, right_limit] band, so the
        # prompt reads centered yet flows around the floral corner.
        _ex_apex_y = height * 0.24          # above this the full width is free
        _ex_reach_y = height * 0.62         # at/below this the carve is maxed out
        _ex_left_top = width * 0.95         # flowers' left edge above the corner
        _ex_left_min = width * 0.58         # flowers' left edge level with them
        _gap3 = 3 * max(1, _full_measure("nnn") // 3)  # ~3 characters of breathing room

        # Prefer the cluster's real per-row silhouette over the straight-line ramp
        # below. The ramp was tuned by eye and reserves far more than the artwork
        # occupies — 233px too much at the worst row on a 900×500 card — which
        # wrapped each line shorter than the last and orphaned short words onto
        # their own line. Only the bundled poppy frame has a separable cluster;
        # every other frame keeps the ramp, which for them is a crude but safe
        # stand-in for border art this can't measure.
        _b_edge = (
            slim_flower_left_edge(border_style, width, height)
            if border_style.slim_frame and border_style.path.exists()
            else None
        )

        def _flower_left(y: float) -> float:
            if _b_edge is not None:
                return float(flower_limit(_b_edge, int(y), line_h))
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

        # One re-flow: lay out at a nominal top, re-center, then flow at the final
        # start (usable width depends on absolute y).
        lines = _flow(int(height * 0.26))
        text_y_start, _content_top = _layout(lines)
        lines = _flow(text_y_start)
        text_y_start, _content_top = _layout(lines)

        # Bound the banner the same way the avatar path is bounded. Without this it
        # grew off the bottom of the card: ~130 chars overflowed a 900x500 card and
        # QUOTE_MAX_CHARS put the tail at y=725 on a 500px canvas, invisible. This
        # is the layout every non-quote caller uses, so it was the common case.
        _bvpad = max(6, int(height * 0.03))
        _bmax_lines = max(
            1, (height - _bvpad - text_y_start + line_gap) // (line_h + line_gap)
        )
        if len(lines) > _bmax_lines:
            lines = lines[:_bmax_lines]
            text_y_start, _content_top = _layout(lines)
            _blast_y = text_y_start + (len(lines) - 1) * (line_h + line_gap)
            lines[-1] = ellipsize_line(lines[-1], _avail_w(_blast_y), _full_measure)

        def _line_x(s: str, y: int) -> int:
            # Centered: announcement banners read centered. Start from the true card
            # center, then shove left only if the line would otherwise reach into
            # the floral corner (wrapping via _avail_w already bounds the width).
            lw = _full_measure(s)
            x = (width - lw) // 2
            right_limit = int(_flower_left(y) - _gap3)
            if x + lw > right_limit:
                x = right_limit - lw
            return max(left_margin, x)
    else:
        # The text column's nominal right edge (738 at 900w) runs straight into the
        # slim border's flower cluster (from x=586, y=253), so any line low enough
        # and long enough was drawn under the petals. Bound the right edge per row —
        # only for the rows the flowers actually occupy, so short cards are
        # unaffected — and wrap the body against that.
        _fl_x, _fl_y = slim_flower_bound(width, height)
        _slim = border_style is not None and border_style.slim_frame
        _fpad = max(6, int(width * 0.01))
        _fl_edge = (
            slim_flower_left_edge(border_style, width, height)
            if _slim and border_style is not None and border_style.path.exists()
            else None
        )

        def _col_right_slim(y: int, h: int = 0) -> int:
            hi = text_pad_l + text_col_w
            if not _slim:
                return hi
            if _fl_edge is not None:
                return min(hi, flower_limit(_fl_edge, y, h) - _fpad)
            # Frame unreadable: fall back to the cluster's bounding box.
            if y + h > _fl_y:
                hi = min(hi, _fl_x - _fpad)
            return hi

        def _avail_p(y: int) -> int:
            # Never collapse the column to nothing, however far into the corner a
            # row reaches; a very narrow line is better than an unwrappable one.
            return max(int(width * 0.20), _col_right_slim(y, line_h) - text_pad_l)

        def _flow_p(start_y: int) -> list[str]:
            out: list[str] = []
            for para in _quoted_text.splitlines():
                words = para.split()
                if not words:
                    out.append("")
                    continue
                cur = ""
                for w in words:
                    y = start_y + len(out) * (line_h + line_gap)
                    cand = f"{cur} {w}".strip()
                    if _full_measure(cand) <= _avail_p(y) or not cur:
                        cur = cand
                    else:
                        out.append(cur)
                        cur = w
                if cur:
                    out.append(cur)
            return out or [""]

        # Bound the block to a real vertical band, the way the frame-fit path does.
        # Without this the quote ran off both card edges once it passed ~9 lines
        # (~150 chars, well inside QUOTE_MAX_CHARS) and the attribution — which now
        # follows the quote instead of sitting at a fixed y — went off the bottom
        # with it. Cap the line count to what fits, then ellipsize.
        _pvpad = max(6, int(height * 0.03))
        _pband_top = _pvpad
        _pband_bot = height - _pvpad - _attr_block
        _pband_h = max(1, _pband_bot - _pband_top)
        _pmax_lines = max(1, (_pband_h + line_gap) // (line_h + line_gap))

        # Two passes: usable width depends on absolute y, which depends on how many
        # lines there are. Flow at a nominal top, re-centre, then flow for real.
        lines = _flow_p(_pband_top)
        _blk_h = len(lines) * line_h + max(0, len(lines) - 1) * line_gap
        lines = _flow_p(_pband_top + max(0, (_pband_h - _blk_h) // 2))
        _capped = len(lines) > _pmax_lines
        if _capped:
            lines = lines[:_pmax_lines]
        # Centre the quote AND its attribution as one group within that band.
        # Centring the quote alone (what this did before) pushed the pair
        # top-heavy: the name hung below the already-centred block, leaving a dead
        # band along the bottom.
        _blk_h = len(lines) * line_h + max(0, len(lines) - 1) * line_gap
        if _capped:
            # Ellipsize only now that the block's final position is known: the
            # closing `…”` has to be fitted against the *last line's own row*, which
            # is the narrowest one under the floral corner.
            _last_y = (
                _pband_top + max(0, (_pband_h - _blk_h) // 2)
                + (len(lines) - 1) * (line_h + line_gap)
            )
            lines[-1] = ellipsize_line(lines[-1], _avail_p(_last_y), _full_measure)
        _content_top = _pband_top + max(0, (_pband_h - _blk_h) // 2)
        text_y_start = _content_top

        def _line_x(s: str, y: int) -> int:
            return text_pad_l

        def _attr_right_p(y: int) -> int:
            return _col_right_slim(y, attr_h)

        _attr_right = _attr_right_p
        _attr_limit_bot = height - _pvpad

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
    def _draw_body(pilmoji) -> None:
        text_y = text_y_start
        for line in lines:
            _render_line_mixed(
                line, _line_x(line, text_y), text_y,
                font=body_font, color=theme.text_color,
                emoji_size=line_h, custom_emojis=custom_emojis,
                bg=bg, draw=draw, pilmoji=pilmoji,
            )
            text_y += line_h + line_gap

    _body_drawn = False
    if _HAS_PILMOJI:
        # pilmoji fetches emoji over HTTP mid-render; a stalled CDN raises here.
        # Snapshot first so a partial draw can be rolled back cleanly, then
        # re-render Unicode emoji as tofu — a degraded card beats a failed one.
        _pre_body = bg.copy()
        try:
            with _Pilmoji(bg, source=_EmojiSource) as _pm:  # type: ignore[misc]
                _draw_body(_pm)
            _body_drawn = True
        except Exception:
            log.exception("quote_renderer: body emoji render fell back to plain text")
            bg.paste(_pre_body)
            draw = ImageDraw.Draw(bg)
    if not _body_drawn:
        _draw_body(None)
    draw = ImageDraw.Draw(bg)

    if _no_pfp:
        # No avatar box — draw the label as a centered header above the prompt,
        # tinted with the border's dominant color so the title echoes the frame.
        if _header_text:
            _hdr_color = dominant_border_color(border_style or BORDERS["golden_poppy"])
            if _HAS_PILMOJI:
                # getsize takes no stroke_width; add it back so an emoji-bearing
                # header centers on the same width pilmoji actually draws.
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

        # Author name below the quote, left-aligned to the quote column (not
        # centered under the pfp — see attribution_pos for why).
        if author_name:
            _quote_bot = text_y_start + len(lines) * line_h + max(0, len(lines) - 1) * line_gap
            # Resolve y first, then read the column bounds at the row the line
            # actually lands on. Reading them at the pre-clamp row would place the
            # line against the wrong bound whenever limit_bot moved it, and in a
            # frame whose opening narrows toward the bottom that means drawing over
            # the frame itself.
            ay = attribution_y(
                quote_bot=_quote_bot, attr_h=attr_h,
                gap=_attr_gap, limit_bot=_attr_limit_bot,
            )
            ax = _attr_left(ay)
            # Shrink (then truncate) a name too wide for the column. Only ever
            # reduces height, so the space reserved from the full-size measurement
            # above stays sufficient.
            def _attr_measure(t: str, sz: int) -> int:
                _f = _load_font(sz, font_style)
                if _HAS_PILMOJI:
                    return _emoji_getsize(t, font=_f)[0]  # type: ignore[misc]
                _bb = draw.textbbox((0, 0), t, font=_f)
                return int(_bb[2] - _bb[0])

            _avail = max(60, _attr_right(ay) - ax)
            if attr_w > _avail:
                _sz, attr_text = fit_attribution_text(
                    attr_text, _avail, _attr_measure,
                    list(range(attr_size, max(14, attr_size // 2) - 1, -1)),
                )
                attr_font = _load_font(_sz, font_style)
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

    # Border overlay — composited after transparency so it shows over the full card
    # area. Defaulted at the top of the function, so layout already knows the frame.
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
