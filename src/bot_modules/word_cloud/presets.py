"""Named visual styles for a word cloud, and the pure colour choice.

Colours are picked here rather than handed to a matplotlib colormap so the
choice is a plain function of (word, rank, preset) and can be asserted in a
test. It also means the sentiment palette can be tuned per preset: the same
blue that reads as "cool" on parchment disappears entirely on a near-black
background.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_FONT_DIR = Path("assets") / "fonts"


@dataclass(frozen=True)
class Preset:
    """One visual style.

    ``palette`` colours words by frequency rank when sentiment colouring is
    off. ``sentiment_stops`` is (negative, neutral, positive) and is used when
    it is on.
    """

    key: str
    label: str
    font: str
    background: str
    palette: tuple[str, ...]
    sentiment_stops: tuple[str, str, str]

    @property
    def font_path(self) -> Path:
        return _FONT_DIR / self.font


PRESETS: tuple[Preset, ...] = (
    Preset(
        key="midnight",
        label="Midnight",
        font="Inter-Regular.ttf",
        background="#11131a",
        palette=("#8ab4f8", "#c58af9", "#78d9a0", "#f6c177", "#e78ca3"),
        sentiment_stops=("#7aa2f7", "#9aa5b1", "#f2a65a"),
    ),
    Preset(
        key="parchment",
        label="Parchment",
        font="PlayfairDisplay-Regular.ttf",
        background="#f4ecd8",
        palette=("#7a5c3e", "#a3623a", "#5d6b4a", "#8a6d3b", "#4a4a44"),
        sentiment_stops=("#3f6f9f", "#7a7466", "#b4531f"),
    ),
    Preset(
        key="meadow",
        label="Meadow",
        font="Oswald-Regular.ttf",
        background="#f7fbf4",
        palette=("#2f6f4f", "#4a8f5f", "#7aa05a", "#3e7d7a", "#5f6f3a"),
        sentiment_stops=("#2f6f9f", "#6b7280", "#b06a1f"),
    ),
    Preset(
        key="neon",
        label="Neon",
        font="BebasNeue-Regular.ttf",
        background="#08080c",
        palette=("#00e5ff", "#ff3db8", "#9dff3d", "#ffd23d", "#b06bff"),
        sentiment_stops=("#00b8ff", "#8b8b9a", "#ff7a3d"),
    ),
    Preset(
        key="notebook",
        label="Notebook",
        font="Caveat-Regular.ttf",
        background="#fffdf7",
        palette=("#2f4858", "#33658a", "#86bbd8", "#758e4f", "#f26419"),
        sentiment_stops=("#33658a", "#6b7280", "#d1590f"),
    ),
)

PRESETS_BY_KEY: dict[str, Preset] = {p.key: p for p in PRESETS}

DEFAULT_PRESET = "midnight"


def resolve_preset(key: str | None) -> Preset:
    """Return the named preset, falling back to the default for anything else.

    A stored dial holding a preset key that a later release renamed must not
    break the command — it degrades to the default look instead.
    """
    return PRESETS_BY_KEY.get((key or "").strip().lower()) or PRESETS_BY_KEY[
        DEFAULT_PRESET
    ]


def _lerp_hex(start: str, end: str, t: float) -> str:
    """Blend two ``#rrggbb`` colours; ``t`` is clamped to [0, 1]."""
    t = min(1.0, max(0.0, t))
    s = start.lstrip("#")
    e = end.lstrip("#")
    parts = []
    for i in (0, 2, 4):
        a = int(s[i : i + 2], 16)
        b = int(e[i : i + 2], 16)
        parts.append(round(a + (b - a) * t))
    return "#%02x%02x%02x" % tuple(parts)


def sentiment_color(preset: Preset, sentiment: float | None) -> str:
    """Colour for a sentiment score in [-1, 1]; neutral when unscored.

    Blends through the preset's neutral stop rather than straight from
    negative to positive, so a middling word reads as middling instead of as
    a muddy mix of the two extremes.
    """
    neg, mid, pos = preset.sentiment_stops
    if sentiment is None:
        return mid
    score = min(1.0, max(-1.0, sentiment))
    if score < 0:
        return _lerp_hex(mid, neg, -score)
    return _lerp_hex(mid, pos, score)


def rank_color(preset: Preset, rank: int) -> str:
    """Colour for a word by its frequency rank, cycling the preset palette."""
    return preset.palette[rank % len(preset.palette)]
