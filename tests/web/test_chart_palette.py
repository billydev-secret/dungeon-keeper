"""The dashboard's categorical chart palette is computed, never eyeballed.

The palette this replaced was chosen by eye and was wrong in a way nobody could
have seen without measuring: gold sat at OKLCH hue 85.6° and "shadow amber" at
81.2°, four of its six hues fell inside a 90° wedge, and the moss/amber pair
measured ΔE 1.8 under simulated protanopia and 8.7 under NORMAL colour vision —
against a floor of 15. Two of the six series colours were indistinguishable to
everybody, and identical to a red-blind reader. The Activity chart stacked them
adjacently, so those two segments shared an edge with no gap between them.

That is not a class of bug review catches, so it is arithmetic here instead.
These are the same checks, thresholds and CVD model the data-visualisation
guidance specifies:

  * lightness band     OKLCH L inside the mode's band, or marks read washed out
                       against the surface / too close to each other in value
  * chroma floor       OKLCH C >= 0.10, below which a "hue" just reads grey
  * CVD separation     OKLab ΔE between slots under Machado-Oliveira-Fernandes
                       (2009) severity-1.0 protan/deutan simulation
  * normal-vision floor the same ΔE unsimulated — a pair below 15 is hard to
                       tell apart even with full colour vision, and unlike the
                       CVD floor this one cannot be excused by direct labels
  * contrast           WCAG vs the chart surface; below 3:1 obliges visible
                       labels or a table view

Adjacent pairs are the default. All-pairs is also checked because a *stacked*
chart shows every series at once, so non-adjacent slots still meet.
"""

from __future__ import annotations

import math
import re
from itertools import combinations
from pathlib import Path

import pytest

_CHARTS = Path(__file__).resolve().parents[2] / "src" / "web_server" / "static" / "js" / "charts.js"

# The chart surface: --bg-alt, the card these are drawn on.
SURFACE = "#2b2d31"
BAND = (0.48, 0.67)          # OKLCH L, dark mode
CHROMA_FLOOR = 0.10
CVD_FLOOR = 6.0              # adjacent pairs, min(protan, deutan)
NORMAL_FLOOR = 15.0
CONTRAST_MIN = 3.0

# Machado, Oliveira & Fernandes (2009), severity 1.0, on linear RGB.
MACHADO = {
    "protan": ((0.152286, 1.052583, -0.204868),
               (0.114503, 0.786281, 0.099216),
               (-0.003882, -0.048116, 1.051998)),
    "deutan": ((0.367322, 0.860646, -0.227968),
               (0.280085, 0.672501, 0.047413),
               (-0.011820, 0.042940, 0.968881)),
}


def _lin(hex_colour: str) -> tuple[float, float, float]:
    h = hex_colour.lstrip("#")
    out = []
    for i in (0, 2, 4):
        c = int(h[i : i + 2], 16) / 255
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return tuple(out)


def _oklab(r: float, g: float, b: float) -> tuple[float, float, float]:
    # l_/m_/s_ are the cube roots of the LMS cone responses. Named with the
    # trailing underscore the reference implementations use — a bare `l` is
    # unreadable next to `1` and ruff rejects it (E741).
    l_ = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m_ = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s_ = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def _oklch(hex_colour: str) -> tuple[float, float]:
    L, a, b = _oklab(*_lin(hex_colour))
    return L, math.hypot(a, b)


def _simulate(hex_colour: str, kind: str) -> tuple[float, float, float]:
    r, g, b = _lin(hex_colour)
    m = MACHADO[kind]
    return tuple(
        max(0.0, min(1.0, m[i][0] * r + m[i][1] * g + m[i][2] * b)) for i in range(3)
    )


def _delta_e(a: str, b: str, kind: str | None = None) -> float:
    pa = _oklab(*(_simulate(a, kind) if kind else _lin(a)))
    pb = _oklab(*(_simulate(b, kind) if kind else _lin(b)))
    return 100 * math.dist(pa, pb)


def _luminance(hex_colour: str) -> float:
    r, g, b = _lin(hex_colour)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _palette() -> list[str]:
    src = _CHARTS.read_text(encoding="utf-8")
    block = re.search(r"export const ROLE_COLORS = \[(.*?)\];", src, re.S)
    assert block, "ROLE_COLORS is gone from charts.js"
    colours = re.findall(r'"(#[0-9A-Fa-f]{6})"', block.group(1))
    assert len(colours) >= 3, f"palette scrape found {colours}"
    return colours


# ── the five checks ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("colour", _palette())
def test_slot_is_inside_the_lightness_band(colour):
    L, _ = _oklch(colour)
    assert BAND[0] <= L <= BAND[1], (
        f"{colour} has OKLCH L={L:.3f}, outside {BAND} for the dark surface — "
        f"it will read washed out or muddy against {SURFACE}"
    )


@pytest.mark.parametrize("colour", _palette())
def test_slot_carries_enough_chroma_to_read_as_a_hue(colour):
    _, C = _oklch(colour)
    assert C >= CHROMA_FLOOR, (
        f"{colour} has OKLCH C={C:.3f}, below {CHROMA_FLOOR} — it reads as grey, "
        f"so it cannot carry a series identity"
    )


def test_adjacent_slots_separate_under_colourblindness():
    pal = _palette()
    worst = min(
        (
            (min(_delta_e(a, b, "protan"), _delta_e(a, b, "deutan")), a, b)
            for a, b in zip(pal, pal[1:])
        ),
        key=lambda t: t[0],
    )
    d, a, b = worst
    assert d >= CVD_FLOOR, (
        f"{a} and {b} are ΔE {d:.1f} apart under simulated colourblindness "
        f"(floor {CVD_FLOOR}) — a red-blind reader cannot tell those two series apart"
    )


@pytest.mark.parametrize("pairs", ["adjacent", "all"])
def test_slots_separate_for_normal_vision(pairs):
    """The one floor that direct labels and gaps cannot excuse."""
    pal = _palette()
    couples = list(zip(pal, pal[1:])) if pairs == "adjacent" else list(combinations(pal, 2))
    worst = min(((_delta_e(a, b), a, b) for a, b in couples), key=lambda t: t[0])
    d, a, b = worst
    assert d >= NORMAL_FLOOR, (
        f"{a} and {b} are ΔE {d:.1f} apart ({pairs} pairs, floor {NORMAL_FLOOR}) — "
        f"hard to tell apart even with full colour vision"
    )


def test_low_contrast_slots_are_declared_so_relief_is_not_forgotten():
    """Below 3:1 is allowed, but only against visible labels or a table view.

    Rather than fail — the palette cannot satisfy the band, the chroma floor and
    3:1 simultaneously on this surface — this pins WHICH slots are in that state,
    so adding one silently is what breaks the build. Anything listed here has to
    keep its relief on the charts that use it.
    """
    low = sorted(c for c in _palette() if _contrast(c, SURFACE) < CONTRAST_MIN)
    assert low == ["#2167A1", "#4A7023", "#97435C"], (
        f"the set of sub-3:1 series colours changed to {low} — each one needs "
        f"visible labels or a table view on every chart that uses it"
    )


# ── the rule that made the old palette worse than it had to be ──────────────


def _token(name: str) -> str:
    src = _CHARTS.read_text(encoding="utf-8")
    m = re.search(rf'export const {name}\s*=\s*"(#[0-9A-Fa-f]{{6}})"', src)
    assert m, f"{name} is gone from charts.js"
    return m.group(1)


# ── CHART_BAR / CHART_ACCENT: the single-series defaults, checked separately
# from ROLE_COLORS because it is exactly the gap that let them go unvalidated
# once already — the palette migration fixed ROLE_COLORS/GENDER_COLORS and
# missed these two, which sat at their old "poppy gold"/"warm mauve" values
# (including activity.js's own members line) until an audit of a LATER,
# unrelated fan-out happened to notice. ─────────────────────────────────────


def test_chart_bar_and_accent_are_on_the_current_palette():
    """They should be ROLE_COLORS members, not a third, parallel colour pick.

    Reusing ROLE_COLORS[i] means these two can never drift out of validation
    independently of the categorical set — there is only one place left to
    check.
    """
    palette = _palette()
    for name in ("CHART_BAR", "CHART_ACCENT"):
        value = _token(name)
        assert value in palette, (
            f"{name} = {value!r} is not a ROLE_COLORS member — it can drift "
            f"out of validation on its own, the way it already did once"
        )


def test_chart_bar_and_accent_pass_the_same_checks_as_the_rest():
    for name in ("CHART_BAR", "CHART_ACCENT"):
        value = _token(name)
        L, C = _oklch(value)
        assert BAND[0] <= L <= BAND[1], f"{name} L={L:.3f} outside {BAND}"
        assert C >= CHROMA_FLOOR, f"{name} C={C:.3f} below {CHROMA_FLOOR}"


def test_chart_bar_and_accent_separate_from_each_other():
    """They appear adjacent (e.g. a bar plus an overlay line on one chart)."""
    bar, accent = _token("CHART_BAR"), _token("CHART_ACCENT")
    for kind in ("protan", "deutan"):
        d = _delta_e(bar, accent, kind)
        assert d >= CVD_FLOOR, f"CHART_BAR vs CHART_ACCENT under {kind}: ΔE {d:.1f}"
    normal = _delta_e(bar, accent)
    assert normal >= NORMAL_FLOOR, f"CHART_BAR vs CHART_ACCENT: ΔE {normal:.1f} (normal)"


def test_the_palette_is_not_cycled():
    """`ROLE_COLORS[i % length]` silently gives series 7 series 1's identity."""
    src = _CHARTS.read_text(encoding="utf-8")
    src = re.sub(r"//.*", "", src)
    assert "% ROLE_COLORS.length" not in src, (
        "the palette is being cycled — past six slots fold the tail into "
        "'Other', facet, or switch to a table; never reuse a hue"
    )
