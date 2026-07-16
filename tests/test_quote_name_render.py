"""Display-name handling on quote cards — stylised letterforms and emoji."""

from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")

import pilmoji.source as _pilmoji_source
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


def test_render_survives_emoji_source_failure(monkeypatch) -> None:
    """A Twemoji fetch failure degrades to tofu — it must not kill the card."""

    def _boom(self, url):  # noqa: ANN001, ANN202
        raise OSError("simulated network failure")

    monkeypatch.setattr(_pilmoji_source.HTTPBasedSource, "request", _boom)

    png = render_quote_card(
        "Network is down but the card still renders.",
        author_name=FANCY,
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
