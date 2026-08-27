"""Chart conventions that are easy to undo and hard to notice.

The data-visualisation guidance names dual-axis charts as the single most
common charting mistake, and the Activity panel had one: XP on a left scale and
"Unique Members" on a right one, over the same bars. Two independent scales mean
the line's position relative to the bars is an artefact of autoscaling — so the
chart implies "members tracked XP" or "they diverged" from what is really just
how the two axes happened to fit. It is now two charts sharing an x-axis.

Also pinned here: the canvas is not where a chart's chrome belongs. Chart.js
paints its own title and legend onto the bitmap, in the canvas font, where the
text cannot be selected, cannot use the page's typefaces, and does not exist for
a screen reader. Activity draws both in HTML instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_JS = _ROOT / "src" / "web_server" / "static" / "js"
_PANELS = _JS / "panels"


def _code(path: Path) -> str:
    """Source with comments stripped — these files discuss charts at length."""
    text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
    return "\n".join(ln for ln in text.splitlines() if not re.match(r"\s*(//|\*)", ln))


def _chart_sources() -> list[Path]:
    files = sorted(_PANELS.glob("*.js")) + [_JS / "charts.js"]
    return [p for p in files if "Chart(" in p.read_text(encoding="utf-8") or "chart" in p.name]


def test_no_chart_uses_a_second_y_axis():
    """Two measures of different scale get two charts, not two scales."""
    offenders = []
    for path in _chart_sources():
        src = _code(path)
        # A second scale is declared as a `y1:` key or an axis id of "y1".
        if re.search(r"^\s*y1\s*:", src, re.M) or 'yAxisID: "y1"' in src:
            offenders.append(path.name)
    assert not offenders, (
        f"dual-axis chart(s) in {offenders} — two y-scales on one plot make the "
        f"relationship between the series an artefact of autoscaling. Use two "
        f"charts sharing an x-axis, small multiples, or index to a common base."
    )


def test_activity_draws_its_chrome_in_html_not_on_the_canvas():
    src = _code(_PANELS / "activity.js")
    assert 'title: { display: false }' in src, "canvas title is back on the Activity chart"
    assert 'legend: { display: false }' in src, "canvas legend is back on the Activity chart"
    assert "renderChartLegend" in src, "the HTML legend is gone"
    assert "renderChartTable" in src, "the table view is gone"


def test_stacked_segments_keep_their_surface_gap():
    """The palette's weakest pair is only legal WITH secondary encoding.

    teal/orchid measure ΔE 6.2 under deuteranopia, inside the 6–8 band the
    guidance permits only alongside direct labels, gaps or texture. The API
    orders series by magnitude, so those two do sit adjacent on a normal week —
    the 2px gap is what makes the chart legal, not the slot ordering.
    """
    src = _code(_PANELS / "activity.js")
    assert "CHART_SURFACE" in src, "the segment gap no longer uses the surface colour"
    assert re.search(r"borderWidth:\s*\{\s*top:\s*2\s*\}", src), (
        "the 2px gap between stacked segments is gone — without it the weakest "
        "colour pair has no secondary encoding"
    )


def test_the_members_series_has_a_usable_hit_target():
    src = _code(_PANELS / "activity.js")
    radius = re.search(r"pointRadius:\s*(\d+)", src)
    assert radius and int(radius.group(1)) >= 4, (
        "marker is under 8px across — the guidance's floor; it was 2px (a 4px "
        "dot you had to land on dead centre)"
    )
    assert re.search(r"pointHitRadius:\s*(\d+)", src), "no enlarged hit radius on the line"


@pytest.mark.parametrize("token", ["ROLE_COLORS", "seriesColor"])
def test_charts_expose_the_shared_palette(token):
    assert token in (_JS / "charts.js").read_text(encoding="utf-8")


def test_chart_text_uses_the_dashboard_typeface():
    """Canvas cannot read a CSS variable, so the family is restated in JS."""
    src = _code(_JS / "charts.js")
    m = re.search(r"Chart\.defaults\.font\.family\s*=\s*([^;]+);", src)
    assert m, "chart font default is gone"
    assert "Public Sans" in m.group(1), (
        "charts are back on a system font while the page around them is not"
    )


# ── found by an audit of the chart fan-out, worth pinning ──────────────────


def test_no_panel_cycles_role_colors_by_hand():
    """`ROLE_COLORS[i % ROLE_COLORS.length]` recreates, at the call site, the
    exact cycling bug charts.js's own builders were fixed to never do.

    Found twice independently in the same fan-out (health-mod-workload.js's
    action-type doughnut, health-mod-engagement.js's two reach/messages bar
    charts) — each colours an unbounded, server-supplied list by index modulo
    6, so item 7 silently gets item 1's colour. `seriesColor(i)` is the
    already-exported fix: past the palette's length it folds to
    SERIES_OVERFLOW instead of repeating a hue.
    """
    offenders = []
    for path in sorted(_PANELS.glob("*.js")):
        src = _code(path)
        if re.search(r"ROLE_COLORS\[[^\]]*%\s*ROLE_COLORS\.length\]", src):
            offenders.append(path.name)
    assert not offenders, (
        f"{offenders} index into ROLE_COLORS with a hand-rolled modulo — use "
        f"seriesColor(i) instead, which folds to SERIES_OVERFLOW past the "
        f"palette's length rather than silently repeating a hue"
    )


def test_channels_compare_chart_has_its_own_scoped_host():
    """Regression for a real bug an audit of the fan-out caught.

    channels.js renders the health section's two chart-wraps (Status
    Breakdown, Score Distribution) BEFORE the Compare Channels chart in the
    DOM. `refreshCompare()` used to find its own chart via a bare
    `container.querySelector(".chart-wrap")` / `"[data-chart]"` — a class
    match returns the FIRST element in document order, so once the health
    section finished loading, every subsequent refreshCompare() (the user
    changes Days or Metric) overwrote the Status Breakdown doughnut's wrap
    instead of the comparison chart's own. The fix is a unique attribute on
    the comparison chart's wrap specifically; this pins that it stays unique
    and that the ambiguous lookups don't come back.
    """
    src = _code(_PANELS / "channels.js")
    assert 'data-compare-wrap' in src, "the comparison chart's wrap lost its unique attribute"
    assert 'querySelector(".chart-wrap")' not in src, (
        "channels.js is back to a bare .chart-wrap lookup, which is ambiguous "
        "once the health section's own chart-wraps are in the DOM"
    )
