"""Counts to PNG.

``wordcloud`` pulls in matplotlib for its colormaps, and the unit runs with
``ProtectHome=read-only``, so matplotlib cannot write its default config dir
and rebuilds its font cache on every boot. Point it at the repo-local cache
*before* the import, exactly as ``services/activity_graphs`` does — matplotlib
resolves the path at import time, so a later assignment is too late.

We never touch ``pyplot``: ``WordCloud.to_image()`` hands back a PIL image
directly, so this needs none of the serialisation ``pyplot_lock`` exists for.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

os.environ.setdefault(
    "MPLCONFIGDIR",
    str(Path(__file__).resolve().parents[3] / ".cache" / "matplotlib"),
)

from wordcloud import WordCloud  # noqa: E402

from .logic import WordStat  # noqa: E402
from .presets import Preset, rank_color, sentiment_color  # noqa: E402

WIDTH = 1600
HEIGHT = 900


def build_color_map(
    stats: list[WordStat], preset: Preset, *, by_sentiment: bool
) -> dict[str, str]:
    """Decide every word's colour up front.

    Doing it here rather than inside the render callback keeps the choice a
    pure function of the stats and the preset — assertable in a test without
    starting a render.
    """
    if by_sentiment:
        return {s.word: sentiment_color(preset, s.sentiment) for s in stats}
    return {s.word: rank_color(preset, i) for i, s in enumerate(stats)}


def render_png(
    stats: list[WordStat],
    preset: Preset,
    *,
    by_sentiment: bool = False,
    width: int = WIDTH,
    height: int = HEIGHT,
) -> bytes:
    """Render ``stats`` to PNG bytes. Blocking — call via ``asyncio.to_thread``.

    A missing font raises rather than silently degrading, matching
    ``quote_renderer``: a card in the wrong typeface is a bug someone should
    see, not something to paper over.
    """
    if not stats:
        raise ValueError("nothing to render")

    font_path = preset.font_path
    if not font_path.is_file():
        raise FileNotFoundError(f"word cloud font missing: {font_path}")

    colors = build_color_map(stats, preset, by_sentiment=by_sentiment)

    def color_func(word, **_kwargs):  # noqa: ANN001, ANN003
        return colors.get(word, preset.palette[0])

    cloud = WordCloud(
        width=width,
        height=height,
        background_color=preset.background,
        font_path=str(font_path),
        max_words=len(stats),
        prefer_horizontal=0.9,
        # Without this the largest word swallows the canvas whenever one term
        # dominates, which is the normal shape of chat traffic.
        relative_scaling=0.5,
        min_font_size=10,
        color_func=color_func,
    ).generate_from_frequencies({s.word: float(s.count) for s in stats})

    buf = io.BytesIO()
    cloud.to_image().save(buf, format="PNG", optimize=True)
    return buf.getvalue()
