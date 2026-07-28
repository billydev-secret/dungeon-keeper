"""Display-name handling on quote cards — stylised letterforms and emoji."""

from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")

import requests
from PIL import Image, ImageDraw

from bot_modules.services.quote_renderer import (
    THEMES,
    normalize_display_name,
    render_quote_card,
)

# The name that prompted this: Mathematical Bold Script capitals plus a kiss
# mark. None of the bundled TTFs carry U+1D4xx, so unnormalised it draws as a
# row of tofu boxes.
FANCY = "\U0001d4df\U0001d4fb\U0001d4f2\U0001d4f7\U0001d4ec\U0001d4ee\U0001d4fc\U0001d4fc"
FANCY += " \U0001d4e1\U0001d4ea\U0001d4ec\U0001d4f1\U0001d4ee\U0001d4f5 \U0001f48b"


def _avatar() -> bytes:
    av = Image.new("RGB", (256, 256), (80, 50, 130))
    ImageDraw.Draw(av).ellipse([40, 40, 216, 216], fill=(230, 170, 70))
    buf = io.BytesIO()
    av.save(buf, "PNG")
    return buf.getvalue()


def test_normalize_folds_math_script_to_ascii() -> None:
    assert normalize_display_name(FANCY) == "Princess Rachel \U0001f48b"


def test_normalize_preserves_emoji() -> None:
    # Emoji have no NFKC decomposition, so they survive for pilmoji to draw.
    assert "\U0001f48b" in normalize_display_name(FANCY)


def test_normalize_leaves_plain_names_untouched() -> None:
    for name in ("BoringName", "rachel_132", "Ben", "a b c", ""):
        assert normalize_display_name(name) == name


def test_normalized_name_is_covered_by_the_bundled_font() -> None:
    """The whole point: every letter must exist in the face we draw with."""
    from fontTools.ttLib import TTFont

    from bot_modules.services.quote_renderer import _INTER

    font = TTFont(str(_INTER), fontNumber=0, lazy=True)
    covered: set[int] = set()
    for table in font["cmap"].tables:
        covered |= set(table.cmap.keys())

    letters = [c for c in normalize_display_name(FANCY) if c.isalpha()]
    assert letters, "expected letters to survive normalisation"
    assert all(ord(c) in covered for c in letters)
    # And confirm the unnormalised form genuinely would not have rendered.
    assert not any(ord(c) in covered for c in FANCY if c.isalpha())


def _fail_emoji_fetch(monkeypatch) -> None:
    """Make every Twemoji fetch fail.

    Patches ``requests.Session.get`` — the call the renderer's custom source
    actually makes. (Patching ``pilmoji``'s base ``HTTPBasedSource.request``
    would be a no-op, since the custom source overrides ``request`` to add the
    timeout and never calls the base.)
    """

    def _boom(self, url, **kwargs):  # noqa: ANN001, ANN202
        raise OSError("simulated network failure")

    monkeypatch.setattr(requests.Session, "get", _boom)


def test_render_survives_emoji_source_failure(monkeypatch) -> None:
    """A fetch failure in the *attribution* (fancy name + emoji) degrades to tofu."""
    _fail_emoji_fetch(monkeypatch)

    png = render_quote_card(
        "Network is down but the card still renders.",  # no body emoji
        author_name=FANCY,  # the 💋 here is the only thing needing a fetch
        avatar_bytes=_avatar(),
        theme=next(iter(THEMES.values())),
    )
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_accepts_fancy_name_in_both_layouts() -> None:
    theme = next(iter(THEMES.values()))
    for shape in ("circle", "none"):
        png = render_quote_card(
            "Layout check.",
            author_name=FANCY,
            avatar_bytes=_avatar(),
            theme=theme,
            pfp_shape=shape,
        )
        assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_emoji_fetch_passes_a_timeout(monkeypatch) -> None:
    """pilmoji sets no timeout, so a stalled CDN would hang the render thread.

    The custom source must thread ``_EMOJI_FETCH_TIMEOUT`` into the HTTP call.
    """
    import requests

    from bot_modules.services.quote_renderer import _EMOJI_FETCH_TIMEOUT

    seen: dict[str, object] = {}

    def _spy_get(self, url, **kwargs):  # noqa: ANN001, ANN202
        seen["timeout"] = kwargs.get("timeout")
        raise OSError("simulated stall")  # then behave like an outage

    monkeypatch.setattr(requests.Session, "get", _spy_get)

    # A Unicode emoji in the body forces a Twemoji fetch.
    png = render_quote_card(
        "Body emoji \U0001f48b forces a fetch.",
        author_name="Plain",
        avatar_bytes=_avatar(),
        theme=next(iter(THEMES.values())),
    )
    assert seen["timeout"] == _EMOJI_FETCH_TIMEOUT
    # And the outage degraded rather than crashed.
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_body_emoji_failure_degrades_without_crashing(monkeypatch) -> None:
    """A fetch failure during the *body* render falls back to plain text.

    The body path is separate from the attribution path; it must not raise.
    """

    _fail_emoji_fetch(monkeypatch)

    png = render_quote_card(
        "A body with an emoji \U0001f48b during an outage.",
        author_name="Plain",  # plain name → failure can only come from the body
        avatar_bytes=_avatar(),
        theme=next(iter(THEMES.values())),
    )
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


# --- Attribution placement ---------------------------------------------------
# The bug: a long, emoji-bearing name ("Chi-Gal 🩵 (#FUCK ICE)") was centred on
# the avatar via `pfp_cx - attr_w // 2`. Any name wider than the disc drove that
# negative, so it collapsed onto the left-margin floor and struck the avatar's
# lower-left arc. The name now anchors to the quote column instead.
LONG_EMOJI_NAME = "Chi-Gal \U0001fa75 (#FUCK ICE)"
QUOTE = "You have access to everything, I gave it to you personally"


def _attr_xy(monkeypatch, **kwargs) -> tuple[int, int]:
    """Render a card and capture where the attribution was actually drawn.

    Spying on the draw call reads the real layout's geometry without pixel-peeping
    a blurred, shadowed card. With an avatar present the attribution is the only
    `_draw_text_layers` caller (the body goes through `_render_line_mixed`).
    """
    from bot_modules.services import quote_renderer as qr

    seen: list[tuple[int, int]] = []
    real = qr._draw_text_layers

    def _spy(bg, draw, layers, text, **kw):
        seen.append(layers[-1][0])  # the top (unshadowed) layer's anchor
        return real(bg, draw, layers, text, **kw)

    monkeypatch.setattr(qr, "_draw_text_layers", _spy)
    qr.render_quote_card(
        kwargs.pop("text", QUOTE),
        author_name=kwargs.pop("author_name", LONG_EMOJI_NAME),
        avatar_bytes=_avatar(),
        theme=next(iter(THEMES.values())),
        **kwargs,
    )
    assert len(seen) == 1, f"expected one attribution draw, got {len(seen)}"
    return seen[0]


def test_attribution_aligns_to_the_quote_column(monkeypatch) -> None:
    """It starts at the text column, not pinned to the card's left margin."""
    ax, _ = _attr_xy(monkeypatch)
    # text_pad_l for the default 900-wide card. Pre-fix this was left_margin (54).
    assert ax == int(900 * 0.34)


def test_attribution_clears_the_avatar_footprint(monkeypatch) -> None:
    """The exact defect: the name must not cross the avatar's drawn footprint.

    Checked against the *drawn* footprint (double ring + drop shadow), not the bare
    radius — the old 4%-of-height gap cleared `pfp_r` but not the ring and shadow.
    """
    ax, ay = _attr_xy(monkeypatch)
    pfp_r = int(min(900, 500) * 0.16)
    pfp_cx, pfp_cy = int(900 * 0.18), 500 // 2
    ring_r = int(pfp_r * 1.15) + pfp_r // 5 + 6  # matches _fit_pfp's r_eff
    # Clear horizontally (the column starts right of the disc) or vertically.
    assert ax > pfp_cx + ring_r or ay > pfp_cy + ring_r


def test_attribution_and_quote_are_centred_as_a_group(monkeypatch) -> None:
    """No dead band at the bottom: quote+name centre as one block.

    Measures the real margin above the first quote line against the margin below
    the attribution. Pre-fix the quote centred *alone* and the name hung off the
    avatar, so the two margins disagreed; now they match.
    """
    from bot_modules.services import quote_renderer as qr

    body_ys: list[int] = []
    attr_ys: list[int] = []
    real_body, real_attr = qr._render_line_mixed, qr._draw_text_layers

    def _spy_body(line, x, y, **kw):
        body_ys.append(y)
        return real_body(line, x, y, **kw)

    def _spy_attr(bg, draw, layers, text, **kw):
        attr_ys.append(layers[-1][0][1])
        return real_attr(bg, draw, layers, text, **kw)

    monkeypatch.setattr(qr, "_render_line_mixed", _spy_body)
    monkeypatch.setattr(qr, "_draw_text_layers", _spy_attr)
    qr.render_quote_card(
        QUOTE,
        author_name=LONG_EMOJI_NAME,
        avatar_bytes=_avatar(),
        theme=next(iter(THEMES.values())),
    )

    attr_h = qr._load_font(max(19, 900 // 33)).getbbox("Ag")[3]
    above = min(body_ys)
    below = 500 - (max(attr_ys) + attr_h)
    assert abs(above - below) <= 10, f"block not centred: {above}px above, {below}px below"


def test_long_name_is_fitted_into_the_column_not_clipped() -> None:
    """A name too wide to fit shrinks, then truncates — it never runs off-column."""
    from bot_modules.services.quote_renderer import fit_attribution_text

    def _measure(t: str, sz: int) -> int:
        return len(t) * sz  # 1 unit per char per size, easy to reason about

    # Fits at full size → untouched.
    assert fit_attribution_text("abc", 100, _measure, [20, 18, 16]) == (20, "abc")
    # Only fits smaller → shrunk to the largest size that fits, text intact.
    assert fit_attribution_text("abcde", 90, _measure, [20, 18, 16]) == (18, "abcde")
    # Cannot fit even at the floor → truncated with an ellipsis, at the floor size.
    sz, txt = fit_attribution_text("a" * 40, 96, _measure, [20, 18, 16])
    assert sz == 16
    assert txt.endswith("…")
    assert _measure(txt, sz) <= 96


def test_attribution_reserve_uses_measured_height() -> None:
    """The frame-fit path reserves real height, not a 1.7x font-size guess."""
    from bot_modules.services.quote_renderer import attribution_block_h

    assert attribution_block_h(40, 12) == 52
    assert attribution_block_h(0, 12) == 0  # no name → no reserve


def test_attribution_clamp_respects_the_frame_opening() -> None:
    """Inside a frame the line is pulled up to stay in the opening."""
    from bot_modules.services.quote_renderer import attribution_pos

    # Unclamped: hangs a gap below the quote.
    assert attribution_pos(col_left=300, quote_bot=400, attr_h=30, gap=20) == (300, 420)
    # Clamped: opening ends at 430, so it rides up to fit (430 - 30 - 4).
    assert attribution_pos(
        col_left=300, quote_bot=400, attr_h=30, gap=20, limit_bot=430
    ) == (300, 396)
