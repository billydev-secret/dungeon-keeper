"""Display-name handling on quote cards — stylised letterforms and emoji."""

from __future__ import annotations

import io

import matplotlib
import pytest

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


def _attr_drawn(monkeypatch, **kwargs):
    """Render a card and capture the attribution exactly as drawn.

    Returns ``(x, y, text, font)`` — the text and font matter because a long name
    is shrunk and truncated to fit, so re-measuring at the nominal size would check
    a string the renderer never drew.

    Spying on the draw call reads the real layout's geometry without pixel-peeping
    a blurred, shadowed card. With an avatar present the attribution is the only
    `_draw_text_layers` caller (the body goes through `_render_line_mixed`).
    """
    from bot_modules.services import quote_renderer as qr

    seen: list[tuple] = []
    real = qr._draw_text_layers

    def _spy(bg, draw, layers, text, **kw):
        x, y = layers[-1][0]  # the top (unshadowed) layer's anchor
        seen.append((x, y, text, kw.get("font")))
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


def _attr_xy(monkeypatch, **kwargs) -> tuple[int, int]:
    x, y, _, _ = _attr_drawn(monkeypatch, **kwargs)
    return x, y


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
    """Inside a frame (or against the card edge) the line is pulled up to fit."""
    from bot_modules.services.quote_renderer import attribution_y

    # Unclamped: hangs a gap below the quote.
    assert attribution_y(quote_bot=400, attr_h=30, gap=20) == 420
    # Clamped: the bound is at 430, so it rides up to fit (430 - 30 - 4).
    assert attribution_y(quote_bot=400, attr_h=30, gap=20, limit_bot=430) == 396


# A quote long enough to wrap past the card. Caught by review: anchoring the name
# to the quote (rather than to a fixed y under the avatar) meant a mid-length quote
# pushed it clean off the bottom edge — at ~9 lines the byline was simply absent.
LONG_QUOTE = "the quick brown fox jumps over the lazy dog and keeps on running far away past the hills " * 3


def test_attribution_stays_on_card_for_long_quotes(monkeypatch) -> None:
    """Regression: the byline must never fall off the bottom of the card."""
    from bot_modules.services.quote_renderer import QUOTE_MAX_CHARS, _load_font

    attr_h = sum(_load_font(max(19, 900 // 33)).getmetrics())
    # Sweep the whole legal length range, not just a short quote.
    for n in (30, 60, 100, 130, 150, 180, 221, QUOTE_MAX_CHARS):
        _, ay = _attr_xy(monkeypatch, text=LONG_QUOTE[:n])
        assert ay + attr_h <= 500, f"byline off-card at {n} chars: bottom={ay + attr_h}"


def test_long_quote_body_stays_on_card(monkeypatch) -> None:
    """Regression: the quote is capped to the band instead of overflowing both edges."""
    from bot_modules.services import quote_renderer as qr

    body_ys: list[int] = []
    real = qr._render_line_mixed

    def _spy(line, x, y, **kw):
        body_ys.append(y)
        return real(line, x, y, **kw)

    monkeypatch.setattr(qr, "_render_line_mixed", _spy)
    qr.render_quote_card(
        LONG_QUOTE[:qr.QUOTE_MAX_CHARS],
        author_name=LONG_EMOJI_NAME,
        avatar_bytes=_avatar(),
        theme=next(iter(THEMES.values())),
    )
    assert min(body_ys) >= 0, f"quote clipped off the top: first line at y={min(body_ys)}"


def test_attribution_keeps_clear_of_the_floral_corner(monkeypatch) -> None:
    """The name must not be drawn under the slim border's flower cluster."""
    from bot_modules.services import quote_renderer as qr

    border = qr.BORDERS["golden_poppy"]
    if not border.path.exists():
        pytest.skip("bundled frame not resolvable from this CWD")
    edge = qr.slim_flower_left_edge(border, 900, 500)
    assert edge is not None

    # A mid-length quote pushes the byline down into the flowers' rows.
    for n in (100, 130, 150, 200):
        ax, ay, text, font = _attr_drawn(monkeypatch, text=LONG_QUOTE[:n])
        right = ax + qr._emoji_getsize(text, font=font)[0]
        limit = qr.flower_limit(edge, ay, sum(font.getmetrics()))
        assert right <= limit, (
            f"{n} chars: name drawn to x={right}, past the flower edge at {limit}"
        )


def test_flower_bound_matches_where_the_cluster_is_pasted() -> None:
    """The layout's flower rect must agree with the compositor's paste position."""
    from bot_modules.services.quote_renderer import (
        _SLIM_FLOWER_CROP,
        _SLIM_FLOWER_SCALE,
        slim_flower_bound,
        slim_flower_rect,
    )

    w, h = 900, 500
    fl, ft, fr, fb = _SLIM_FLOWER_CROP
    fw = max(1, int((int(w * fr) - int(w * fl)) * _SLIM_FLOWER_SCALE))
    fh = max(1, int((int(h * fb) - int(h * ft)) * _SLIM_FLOWER_SCALE))
    assert slim_flower_bound(w, h) == slim_flower_rect(w, h, fw, fh)


def test_attribution_height_is_stable_across_names() -> None:
    """Geometry must not jitter with the letters a name happens to contain.

    An ink bbox gives 20px for "Bob" but 30px for a parenthesised name; the font's
    line box is constant, and identical with or without pilmoji installed.
    """
    from bot_modules.services.quote_renderer import _load_font

    font = _load_font(27)
    assert sum(font.getmetrics()) == 34
    # The ink bbox — what this used to use — is the thing that varies.
    inks = {font.getbbox(f"— {n}")[3] - font.getbbox(f"— {n}")[1] for n in ("Bob", "gg", "(X)")}
    assert len(inks) > 1, "expected ink heights to vary, proving why they're unusable"


def test_fit_attribution_handles_degenerate_input() -> None:
    """No crash on an empty size list; no overflow when even a stub can't fit."""
    from bot_modules.services.quote_renderer import fit_attribution_text

    def _measure(t: str, sz: int) -> int:
        return len(t) * sz

    assert fit_attribution_text("abc", 100, _measure, []) == (0, "abc")
    # Column so narrow that even "…" overflows → drop the line, don't draw over.
    sz, txt = fit_attribution_text("abcdefgh", 5, _measure, [20, 16])
    assert txt == ""


def test_flower_edge_is_read_from_alpha_not_the_bounding_box() -> None:
    """The per-row bound must be looser than the cluster's box where it's sparse.

    Using the box for every row cost a wrapped line at every quote length: the
    cluster's upper rows are a few buds, and reserving its full width for them
    narrowed the column for text that had no petals beside it.
    """
    from bot_modules.services.quote_renderer import (
        BORDERS,
        slim_flower_bound,
        slim_flower_left_edge,
    )

    border = BORDERS["golden_poppy"]
    if not border.path.exists():
        pytest.skip("bundled frame not resolvable from this CWD")
    edge = slim_flower_left_edge(border, 900, 500)
    assert edge is not None
    box_x, box_y = slim_flower_bound(900, 500)
    # Somewhere in the cluster's upper half, the true edge is right of the box edge.
    upper = [edge[y] for y in range(box_y, min(box_y + 60, 500))]
    assert max(upper) > box_x, "expected sparse upper rows to allow more text"
    # And rows above the cluster are unbounded.
    assert edge[box_y - 20] == 900
    # Cached: a second call returns the identical object.
    assert slim_flower_left_edge(border, 900, 500) is edge


def test_flower_limit_bounds_a_line_over_its_whole_height() -> None:
    """A line is a band — petals dipping into its lower rows must bound it."""
    from bot_modules.services.quote_renderer import flower_limit

    edge = [900] * 100
    edge[50] = 700  # a petal intrudes on one row only
    # A line covering that row is bounded by it, even though its top row is clear.
    assert flower_limit(edge, 45, 10) == 700
    # A line finishing above it is not.
    assert flower_limit(edge, 30, 10) == 900
    # Out-of-range rows clamp rather than raise.
    assert flower_limit(edge, 98, 40) == 900
    assert flower_limit([], 0, 10) > 0


def test_body_text_stays_clear_of_the_flowers(monkeypatch) -> None:
    """No body line may reach into the floral corner on the slim border."""
    from bot_modules.services import quote_renderer as qr

    border = qr.BORDERS["golden_poppy"]
    if not border.path.exists():
        pytest.skip("bundled frame not resolvable from this CWD")
    edge = qr.slim_flower_left_edge(border, 900, 500)
    assert edge is not None

    drawn: list[tuple[str, int, int]] = []
    real = qr._render_line_mixed

    def _spy(line, x, y, **kw):
        drawn.append((line, x, y))
        return real(line, x, y, **kw)

    monkeypatch.setattr(qr, "_render_line_mixed", _spy)
    body_font = qr._load_font(max(32, 900 // 19))
    line_h = body_font.getbbox("Ag")[3] - body_font.getbbox("Ag")[1]

    for n in (60, 130, 200, 280):
        drawn.clear()
        qr.render_quote_card(
            LONG_QUOTE[:n], author_name=LONG_EMOJI_NAME, avatar_bytes=_avatar(),
            theme=next(iter(THEMES.values())), border_style=border,
        )
        for line, x, y in drawn:
            right = x + qr._emoji_getsize(line, font=body_font)[0]
            assert right <= qr.flower_limit(edge, y, line_h), (
                f"{n} chars: line {line!r} reaches x={right} into the flowers"
            )


# --- Banner (no-pfp) layout --------------------------------------------------
# Shared by photo challenge, FFA, economy, chat revive and /quote's banner mode.
# It carved space for the floral corner with a hand-tuned linear ramp that
# over-reserved by up to 233px on a 900x500 card, so each line wrapped shorter
# than the last and short words were orphaned onto their own line.
BANNER_TEXT = "You have access to everything, I gave it to you personally"


def _banner_lines(monkeypatch, text=BANNER_TEXT, **kwargs):
    from bot_modules.services import quote_renderer as qr

    drawn: list[tuple[str, int, int]] = []
    real = qr._render_line_mixed

    def _spy(line, x, y, **kw):
        drawn.append((line, x, y))
        return real(line, x, y, **kw)

    monkeypatch.setattr(qr, "_render_line_mixed", _spy)
    qr.render_quote_card(
        text, author_name=kwargs.pop("author_name", LONG_EMOJI_NAME),
        avatar_bytes=_avatar(), theme=next(iter(THEMES.values())),
        pfp_shape="none", **kwargs,
    )
    return drawn


def test_banner_text_stays_clear_of_the_flowers(monkeypatch) -> None:
    """Centred banner lines must not reach into the poppy cluster."""
    from bot_modules.services import quote_renderer as qr

    border = qr.BORDERS["golden_poppy"]
    if not border.path.exists():
        pytest.skip("bundled frame not resolvable from this CWD")
    edge = qr.slim_flower_left_edge(border, 900, 500)
    assert edge is not None
    font = qr._load_font(max(32, 900 // 19))
    line_h = font.getbbox("Ag")[3] - font.getbbox("Ag")[1]

    for text in (BANNER_TEXT, BANNER_TEXT * 2, "Short."):
        for line, x, y in _banner_lines(monkeypatch, text):
            right = x + qr._emoji_getsize(line, font=font)[0]
            assert right <= qr.flower_limit(edge, y, line_h), (
                f"banner line {line!r} reaches x={right} into the flowers"
            )


def test_banner_wraps_without_orphaning_short_lines(monkeypatch) -> None:
    """The ramp orphaned 'to you' onto its own line; the real edge does not.

    Needs the bundled frame: without it there is no cluster to measure and the
    layout correctly falls back to the ramp, orphan and all.
    """
    from bot_modules.services.quote_renderer import BORDERS

    if not BORDERS["golden_poppy"].path.exists():
        pytest.skip("bundled frame not resolvable from this CWD")
    lines = [line for line, _, _ in _banner_lines(monkeypatch)]
    assert len(lines) <= 3, f"expected a tight wrap, got {len(lines)}: {lines}"
    # No interior line may be a small fraction of the longest — that's the
    # staircase the over-reserving ramp produced.
    longest = max(len(line) for line in lines)
    for line in lines[:-1]:  # the last line is legitimately short
        assert len(line) > longest * 0.5, f"orphaned line {line!r} among {lines}"


def test_banner_keeps_the_ramp_for_frames_without_a_poppy_cluster(monkeypatch) -> None:
    """Only the bundled slim frame has a separable cluster to measure.

    Other frames keep the hand-tuned ramp, which is a crude but safe stand-in for
    border art this can't measure — dropping it could put text over their artwork.
    """
    from bot_modules.services import quote_renderer as qr

    other = qr.BORDERS["midnight_frame"]
    assert not other.slim_frame
    # Renders without error and still reserves the corner (lines stay left of the
    # ramp's floor at the rows it applies to).
    drawn = _banner_lines(monkeypatch, BANNER_TEXT, border_style=other)
    assert drawn


def test_default_border_is_resolved_before_layout(monkeypatch) -> None:
    """A caller passing no border must lay out as if the default were present.

    The default was filled in just before compositing, so a caller that passed none
    laid its text out as if the card were bare and then had the poppy frame drawn
    over it — putting the last lines back under the petals. No caller but /quote
    passes a border, so this was the common path.
    """
    from bot_modules.services import quote_renderer as qr

    border = qr.BORDERS["golden_poppy"]
    if not border.path.exists():
        pytest.skip("bundled frame not resolvable from this CWD")
    edge = qr.slim_flower_left_edge(border, 900, 500)
    assert edge is not None
    font = qr._load_font(max(32, 900 // 19))
    line_h = font.getbbox("Ag")[3] - font.getbbox("Ag")[1]

    def _worst(border_style):
        drawn: list[tuple[str, int, int]] = []
        real = qr._render_line_mixed
        monkeypatch.setattr(
            qr, "_render_line_mixed",
            lambda line, x, y, **kw: (drawn.append((line, x, y)), real(line, x, y, **kw))[1],
        )
        qr.render_quote_card(
            LONG_QUOTE[:200], author_name=LONG_EMOJI_NAME, avatar_bytes=_avatar(),
            theme=next(iter(THEMES.values())), border_style=border_style,
        )
        monkeypatch.setattr(qr, "_render_line_mixed", real)
        return max(
            x + qr._emoji_getsize(line, font=font)[0] - qr.flower_limit(edge, y, line_h)
            for line, x, y in drawn
        )

    # Passing no border must be as clear of the flowers as passing it explicitly.
    assert _worst(None) <= 0
    assert _worst(None) == _worst(border)


def test_banner_body_wrap_does_not_depend_on_the_name(monkeypatch) -> None:
    """The quote must wrap the same regardless of the glyphs in the author's name.

    The header's height sets where the body starts, and the body's usable width
    narrows toward the floral corner — so measuring the header by its ink bbox made
    a name with parentheses or descenders push the body down and re-wrap the quote.
    """
    # Holds with or without the bundled frame: the header height feeding layout is
    # a font metric either way, so no skip is needed here.
    wraps = {
        name: tuple(line for line, _, _ in _banner_lines(monkeypatch, author_name=name))
        for name in ("Chi-Gal", LONG_EMOJI_NAME, "gggg", "AAAA")
    }
    assert len(set(wraps.values())) == 1, f"wrap varies with the name: {wraps}"


# --- Ellipsize / vertical bounds (round 2 of review) -------------------------
# The cap appends "…”" AFTER wrapping, on the block's bottom line — exactly the row
# where the floral corner leaves least room. Nothing re-checked that row, so the
# closing text landed under the petals (worst case 84px past the bound).
# A capped quote whose last line lands wide. Capping shifts the block down, so the
# final row is narrower than the one the line was wrapped against — the ellipsis is
# then appended on top of that. Found by fuzzing; pinned here because the failure
# needs a last line that is both truncated and wide (short-word text hides it).
WIDE_WORDS = (
    "fantastic marvellous fantastic zz wonderful fantastic tu marvellous "
    "marvellous zz tu fantastic marvellous vwxyz zz marvellous wonderful tu tu zz "
    "brilliant vwxyz fantastic marvellous fantastic fantastic tu tu brilliant tu "
    "vwxyz zz fantastic extraor"
)


def test_ellipsized_last_line_respects_the_flower_bound(monkeypatch) -> None:
    """The `…”` appended after wrapping must be re-fitted to its own row."""
    from bot_modules.services import quote_renderer as qr

    border = qr.BORDERS["golden_poppy"]
    if not border.path.exists():
        pytest.skip("bundled frame not resolvable from this CWD")
    edge = qr.slim_flower_left_edge(border, 900, 500)
    assert edge is not None
    font = qr._load_font(max(32, 900 // 19))
    line_h = font.getbbox("Ag")[3] - font.getbbox("Ag")[1]

    drawn: list[tuple[str, int, int]] = []
    real = qr._render_line_mixed
    monkeypatch.setattr(
        qr, "_render_line_mixed",
        lambda line, x, y, **kw: (drawn.append((line, x, y)), real(line, x, y, **kw))[1],
    )
    # Long words make the truncated line wide — short-word text hides this.
    for n in (len(WIDE_WORDS), 200, qr.QUOTE_MAX_CHARS):
        drawn.clear()
        qr.render_quote_card(
            WIDE_WORDS[:n], author_name="Chi-Gal", avatar_bytes=_avatar(),
            theme=next(iter(THEMES.values())),
        )
        assert any(line.endswith("…”") for line in (d[0] for d in drawn)), (
            f"expected an ellipsized line at {n} chars"
        )
        for line, x, y in drawn:
            right = x + qr._emoji_getsize(line, font=font)[0]
            assert right <= qr.flower_limit(edge, y, line_h), (
                f"{n} chars: {line!r} drawn to x={right}, past the flowers"
            )


def test_ellipsize_line_fits_the_width_it_is_given() -> None:
    """Unit: the closing glyphs are part of what must fit, not an afterthought."""
    from bot_modules.services.quote_renderer import ellipsize_line

    def measure(t: str) -> int:
        return len(t) * 10

    # Trailing quote/space are stripped before the ellipsis is added.
    assert ellipsize_line('some text” ', 200, measure) == "some text…”"
    # Too wide → characters come off until it fits, ellipsis included.
    out = ellipsize_line("abcdefghijklmnop", 80, measure)
    assert out.endswith("…”")
    assert measure(out) <= 80
    # Degenerate width still terminates rather than looping.
    assert ellipsize_line("abc", 1, measure) == "…”"


def test_banner_text_stays_on_the_card(monkeypatch) -> None:
    """Regression: the banner had no vertical bound and grew off the bottom."""
    from bot_modules.services import quote_renderer as qr

    font = qr._load_font(max(32, 900 // 19))
    line_h = font.getbbox("Ag")[3] - font.getbbox("Ag")[1]
    for n in (100, 130, 200, qr.QUOTE_MAX_CHARS):
        drawn = _banner_lines(monkeypatch, LONG_QUOTE[:n])
        bottom = max(y for _, _, y in drawn) + line_h
        assert bottom <= 500, f"{n} chars: banner text reaches y={bottom} on a 500px card"


def test_banner_header_is_fitted_to_the_card(monkeypatch) -> None:
    """A long display name must not run off the edges of the banner header.

    For the four callers that render in banner mode this header is the only place
    the name appears, so an overflowing header loses the attribution entirely.
    """
    from bot_modules.services import quote_renderer as qr

    real = qr._draw_text_layers  # capture once: re-reading it inside the loop would
    seen: list[tuple] = []       # wrap the already-patched function and recurse

    def _spy(bg, dr, ly, t, **kw):
        seen.append((ly[-1][0], t, kw.get("font"), kw.get("stroke_width", 0)))
        return real(bg, dr, ly, t, **kw)

    monkeypatch.setattr(qr, "_draw_text_layers", _spy)
    for name in (
        "Bartholomew Q. Fortescue-Wellington III",
        LONG_EMOJI_NAME,
        "W" * 60,
        "Chi-Gal",
    ):
        seen.clear()
        qr.render_quote_card(
            "Short.", author_name=name, avatar_bytes=_avatar(),
            theme=next(iter(THEMES.values())), pfp_shape="none",
        )
        (hx, _), text, font, stroke = seen[0]
        right = hx + qr._emoji_getsize(text, font=font)[0] + 2 * stroke
        assert hx >= 0, f"{name[:20]!r}: header starts off-card at x={hx}"
        assert right <= 900, f"{name[:20]!r}: header ends off-card at x={right}"
