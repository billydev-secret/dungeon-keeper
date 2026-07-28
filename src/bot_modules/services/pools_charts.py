"""Chart renderers for Pools (plan doc Stage 2, "Charts").

Two images. The market chart is the implied-probability path across the
betting window — the Polymarket/Kalshi idiom, and simply the pool share
plotted over time. The instrument chart is daily candlesticks of total
Petals in circulation, with the day's net change and volume beneath.

The candles are honest OHLC, not decoration: the cumulative ledger *is*
circulation, so a candle body is exactly the day's net change — the
quantity being bet on. A bettor is watching whether today's candle closes
above the line.

Deliberately absent: RSI, MACD, Bollinger bands, regression trend lines.
On a series this short, with a structural break in living memory, they
imply a signal that is not in the data. The candles, target line, rolling
median, band and volume give the same trading-desk look without claiming
anything false. Revisit once there are a few months of history — that
objection expires on its own.

Conventions inherited from activity_graphs.py, and load-bearing rather
than stylistic: MPLCONFIGDIR is redirected before matplotlib is imported
(the unit runs ProtectHome=read-only, so the default config dir is
unwritable and matplotlib would rebuild its font cache on every call), the
Agg backend, the Discord dark palette, and returning raw ``bytes`` for the
caller to wrap in a ``discord.File``.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

os.environ.setdefault(
    "MPLCONFIGDIR",
    str(Path(__file__).resolve().parents[3] / ".cache" / "matplotlib"),
)

import matplotlib  # noqa: E402

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from bot_modules.services import pools_logic  # noqa: E402
from bot_modules.services.pools_logic import DayMetric  # noqa: E402

# Discord dark theme palette, matching activity_graphs.py.
_BG = "#2f3136"
_TEXT = "#dcddde"
_GRID = "#40444b"
_MUTED = "#949ba4"
_BAND = "#5865f2"
# Semantic only — green/red here mean a day closed up or down, nothing else.
_UP = "#3ba55c"
_DOWN = "#ed4245"

_DEFAULT_ACCENT = "#e6a52c"


def accent_hex(color: object | None) -> str:
    """A discord.Color (or None) as the ``#rrggbb`` the renderers take.

    Lives here so the fallback sits with the palette that defines it, and
    callers stop reaching for a private name to spell the default.
    """
    value = getattr(color, "value", None)
    return _DEFAULT_ACCENT if value is None else f"#{int(value):06x}"

INSTRUMENT_FILENAME = "pools_instrument.png"
MARKET_FILENAME = "pools_market.png"


def _kfmt() -> FuncFormatter:
    return FuncFormatter(
        lambda v, _: f"{v / 1000:.0f}k" if abs(v) >= 1000 else f"{v:.0f}"
    )


def _style(ax, *, bottom: bool = False) -> None:
    ax.set_facecolor(_BG)
    ax.grid(True, color=_GRID, lw=0.6, alpha=0.55, zorder=0)
    ax.set_axisbelow(True)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(colors=_MUTED, labelsize=8, length=0)
    if not bottom:
        ax.set_xticklabels([])


def _save(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=_BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def render_instrument_chart(
    days: list[DayMetric],
    line: float | None,
    *,
    accent: str = _DEFAULT_ACCENT,
    currency: str = "Petals",
) -> bytes:
    """Candles of circulation, net change with its band, and volume.

    ``line`` draws two markers when present: the net-change threshold on the
    middle panel, and the circulation level today's close must beat on the
    candles. None (during the 7-day warmup) simply omits them.
    """
    if not days:
        raise ValueError("render_instrument_chart needs at least one day")

    fig, (a1, a2, a3) = plt.subplots(
        3, 1, figsize=(10, 6.4), sharex=True,
        gridspec_kw={"height_ratios": [3, 1.35, 0.9], "hspace": 0.08},
    )
    fig.patch.set_facecolor(_BG)
    x = range(len(days))
    bodies = [d.body for d in days]
    med, sig = pools_logic.median_band(bodies)
    # A flat day would otherwise draw a zero-height rectangle and vanish.
    min_body = max(1, _span(days) // 200)

    for i, d in enumerate(days):
        colour = _UP if d.up else _DOWN
        a1.vlines(i, d.low, d.high, color=colour, lw=1.4, zorder=3)
        a1.add_patch(Rectangle(
            (i - 0.28, min(d.open, d.close)), 0.56,
            max(abs(d.close - d.open), min_body), color=colour, zorder=4,
        ))

    top = max(d.high for d in days)
    if line is not None:
        target = days[-1].open + line
        a1.axhline(target, color=accent, ls="--", lw=1.4, zorder=5)
        a1.annotate(
            f"TARGET {target:,.0f}", (0.2, target), color=accent,
            fontsize=8.5, fontweight="bold", va="bottom",
        )
        top = max(top, target)
    floor = min(d.low for d in days)
    pad = max(1, (top - floor) // 12)
    a1.set_ylim(floor - pad, top + pad * 2)
    _style(a1)
    a1.yaxis.set_major_formatter(_kfmt())
    a1.set_ylabel(f"{currency} in circulation", color=_MUTED, fontsize=9)
    a1.set_title(
        "POOLS  ·  each body is one day's net change",
        color=_TEXT, fontsize=12, fontweight="bold", loc="left", pad=12,
    )

    # The overlay is drawn only where it is defined. During the warmup week
    # there is no median and no line — the same window the spec refuses to
    # open a round on — so an average drawn there would imply a signal that
    # does not exist yet.
    overlay = [i for i, m in enumerate(med) if m is not None]
    if overlay:
        a2.fill_between(
            overlay,
            [med[i] - sig[i] for i in overlay],  # type: ignore[operator]
            [med[i] + sig[i] for i in overlay],  # type: ignore[operator]
            color=_BAND, alpha=0.20, zorder=1, label="±1σ",
        )
        a2.plot(
            overlay, [med[i] for i in overlay], color=accent, lw=1.7,
            zorder=3, label="7d median",
        )
    a2.bar(
        x, bodies, width=0.56, zorder=2,
        color=[_UP if b >= 0 else _DOWN for b in bodies],
    )
    if line is not None:
        a2.axhline(line, color=accent, ls="--", lw=1.3, zorder=4)
        a2.annotate(
            f"LINE {pools_logic.format_line(line)}", (0.2, line), color=accent,
            fontsize=8.5, fontweight="bold", va="bottom",
        )
    a2.axhline(0, color=_MUTED, lw=0.8, zorder=2)
    _style(a2)
    a2.yaxis.set_major_formatter(_kfmt())
    a2.set_ylabel("Net change", color=_MUTED, fontsize=9)
    if overlay:
        legend = a2.legend(loc="upper right", fontsize=7.5, frameon=False, ncol=2)
        for text in legend.get_texts():
            text.set_color(_MUTED)

    a3.bar(x, [d.volume for d in days], color=_MUTED, alpha=0.55, width=0.56)
    _style(a3, bottom=True)
    a3.yaxis.set_major_formatter(_kfmt())
    a3.set_ylabel("Volume", color=_MUTED, fontsize=9)
    step = 1 if len(days) <= 8 else 2
    ticks = list(x)[::step]
    a3.set_xticks(ticks)
    a3.set_xticklabels([days[i].day[5:] for i in ticks], fontsize=8)

    # No tight_layout here: the shared-x stack already has its spacing from
    # gridspec, and savefig's bbox_inches="tight" does the trimming. Calling
    # it warns that these Axes aren't compatible and can shift the panels.
    return _save(fig)


def _span(days: list[DayMetric]) -> int:
    return max(1, max(d.high for d in days) - min(d.low for d in days))


def render_market_chart(
    points: list[tuple[float, float]],
    line: float,
    *,
    pool_total: int,
    accent: str = _DEFAULT_ACCENT,
    subtitle: str = "",
) -> bytes:
    """Implied probability of OVER across the betting window.

    An empty market renders an honest flat 50% with a "no bets yet" note
    rather than a line implying an opinion nobody has expressed.
    """
    fig, ax = plt.subplots(figsize=(10, 3.6))
    fig.patch.set_facecolor(_BG)

    if points:
        # The path starts at the 50% prior, not at the first bet's implied
        # value. One bet makes the pool 100%/0% by construction, so anchoring
        # to it would open every market pinned to an edge and make the first
        # stake look like a confident forecast rather than the only one in.
        xs = [0.0] + [p[0] for p in points]
        ys = [50.0] + [p[1] * 100 for p in points]
        ax.fill_between(xs, 0, ys, color=accent, alpha=0.18, zorder=2)
        ax.plot(xs, ys, color=accent, lw=2.1, zorder=3)
        ax.scatter([xs[-1]], [ys[-1]], s=42, color=accent, zorder=4)
        ax.annotate(
            f"  OVER  {ys[-1]:.0f}%", (xs[-1], ys[-1]), color=accent,
            fontsize=11, fontweight="bold", va="center",
        )
    else:
        ax.annotate(
            "no bets yet", (0.5, 50), color=_MUTED, fontsize=11,
            ha="center", va="center",
        )

    ax.axhline(50, color=_MUTED, ls=":", lw=1.0, zorder=1)
    ax.set_ylim(0, 100)
    ax.set_xlim(0, 1.22)
    _style(ax, bottom=True)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.set_yticklabels(["0%", "25%", "50%", "75%", "100%"])
    ax.set_xticks([0, 0.33, 0.66, 1.0])
    ax.set_xticklabels(["open", "", "", "close"], fontsize=8)
    head = f"Implied probability  ·  OVER {pools_logic.format_line(line)}"
    if pool_total:
        head += f"  ·  pool {pool_total:,}"
    if subtitle:
        head += f"  ·  {subtitle}"
    ax.set_title(
        head, color=_TEXT, fontsize=11, fontweight="bold", loc="left", pad=10
    )
    fig.tight_layout(pad=1.0)
    return _save(fig)
