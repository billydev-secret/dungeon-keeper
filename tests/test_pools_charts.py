"""Tests for services/pools_charts.py.

The chart's *data* is logic and is pinned in test_pools_logic.py. What is
worth testing here is that the renderers survive the shapes real rounds
actually produce — a warmup week with no line, a single day, a flat day,
an empty market — because a chart that raises takes the panel down with it.

Rendering itself is not asserted pixel-wise; these check the PNG comes back
and matplotlib closed its figures.
"""

from __future__ import annotations

import matplotlib.pyplot as plt
import pytest

from bot_modules.services import pools_charts as pc
from bot_modules.services import pools_logic as L
from bot_modules.services.pools_service import DayMetric

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _day(i: int, open_: int, close: int, *, vol: int = 10) -> DayMetric:
    lo, hi = min(open_, close), max(open_, close)
    return DayMetric(
        day=f"2026-07-{i:02d}", mint=max(0, close - open_), burn=0, hold=0,
        net=close - open_, open=open_, high=hi + 50, low=lo - 50,
        close=close, volume=vol,
    )


def _week() -> list[DayMetric]:
    level, out = 0, []
    for i, delta in enumerate([300, 450, -120, 700, 210, 380, 640, 90], start=1):
        out.append(_day(i, level, level + delta))
        level += delta
    return out


def test_instrument_chart_renders_a_png():
    png = pc.render_instrument_chart(_week(), 380.5)
    assert png.startswith(PNG_MAGIC)
    assert len(png) > 1000


def test_instrument_chart_without_a_line():
    """During the 7-day warmup there is no line to draw, and the chart must
    still render — that is exactly when people are watching it fill up."""
    png = pc.render_instrument_chart(_week()[:4], None)
    assert png.startswith(PNG_MAGIC)


def test_instrument_chart_with_a_single_day():
    png = pc.render_instrument_chart([_day(1, 0, 500)], None)
    assert png.startswith(PNG_MAGIC)


def test_instrument_chart_with_a_flat_day():
    """A zero-height body would vanish; it is floored to stay visible."""
    png = pc.render_instrument_chart([_day(1, 100, 100, vol=0)], None)
    assert png.startswith(PNG_MAGIC)


def test_instrument_chart_needs_at_least_one_day():
    with pytest.raises(ValueError):
        pc.render_instrument_chart([], 100.5)


def test_market_chart_renders_a_png():
    bets = [
        {"id": 1, "side": L.OVER, "amount": 50, "created_at": 100.0},
        {"id": 2, "side": L.UNDER, "amount": 80, "created_at": 300.0},
        {"id": 3, "side": L.OVER, "amount": 120, "created_at": 700.0},
    ]
    points = L.probability_series(bets, 0.0, 1000.0)
    png = pc.render_market_chart(points, 380.5, pool_total=250)
    assert png.startswith(PNG_MAGIC)


def test_market_chart_with_no_bets():
    """An empty market renders a flat prior and says so, rather than a line
    implying an opinion nobody has expressed."""
    png = pc.render_market_chart([], 380.5, pool_total=0)
    assert png.startswith(PNG_MAGIC)


def test_renderers_close_their_figures():
    """A leaked figure per bet would grow unboundedly on a panel that
    redraws all day."""
    plt.close("all")
    pc.render_instrument_chart(_week(), 380.5)
    pc.render_market_chart([(0.5, 0.6)], 380.5, pool_total=10)
    assert plt.get_fignums() == []


def test_probability_series_starts_from_the_first_bet():
    bets = [
        {"id": 1, "side": L.OVER, "amount": 100, "created_at": 500.0},
        {"id": 2, "side": L.UNDER, "amount": 100, "created_at": 750.0},
    ]
    pts = L.probability_series(bets, 0.0, 1000.0)
    assert pts[0] == (0.5, 1.0)      # one bet: the pool implies certainty
    assert pts[1] == (0.75, 0.5)     # matched: back to even


def test_probability_series_replays_in_placement_order():
    """Rows come back in whatever order the query gives; the path is only
    meaningful in the order money actually arrived."""
    bets = [
        {"id": 3, "side": L.UNDER, "amount": 100, "created_at": 900.0},
        {"id": 1, "side": L.OVER, "amount": 100, "created_at": 100.0},
    ]
    pts = L.probability_series(bets, 0.0, 1000.0)
    assert [round(x, 2) for x, _ in pts] == [0.1, 0.9]


def test_probability_series_clamps_a_late_bet_into_the_window():
    bets = [{"id": 1, "side": L.OVER, "amount": 10, "created_at": 9_999.0}]
    assert L.probability_series(bets, 0.0, 1000.0)[0][0] == 1.0


def test_probability_series_of_an_empty_round():
    assert L.probability_series([], 0.0, 1000.0) == []
