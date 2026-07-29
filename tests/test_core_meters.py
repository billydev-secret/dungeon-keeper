"""The shared ``▰▱`` meter primitive and the code-span rule it enforces."""

import pytest

from bot_modules.core.meters import BAR_EMPTY, BAR_FILLED, fill, mono


def test_fill_is_constant_width_regardless_of_progress():
    """The whole point: every fill at a given width is the same glyph count."""
    bars = [fill(n, 10, 14) for n in range(11)]
    assert {len(b) for b in bars} == {14}


@pytest.mark.parametrize(
    "current,target,width,expected",
    [
        (0, 10, 10, BAR_EMPTY * 10),
        (10, 10, 10, BAR_FILLED * 10),
        (5, 10, 10, BAR_FILLED * 5 + BAR_EMPTY * 5),
        # Non-positive target reads as empty; callers that want a different
        # policy (privacy's "nothing to do = done") decide it themselves.
        (0, 0, 6, BAR_EMPTY * 6),
        (3, -1, 6, BAR_EMPTY * 6),
        # Overshoot clamps rather than producing negative padding.
        (20, 10, 8, BAR_FILLED * 8),
        # Floats are accepted so percentage-driven meters can share the fill.
        (42.0, 100, 10, BAR_FILLED * 4 + BAR_EMPTY * 6),
    ],
)
def test_fill_values(current, target, width, expected):
    assert fill(current, target, width) == expected


def test_mono_wraps_in_a_code_span():
    """A bare ▰▱ run renders proportionally in Discord — ▱ is wider than ▰ —
    so an empty bar is visibly longer than a half-filled one. The code span
    forces a monospace advance and is what actually fixes the wobble."""
    assert mono("▰▱") == "`▰▱`"


def test_mono_is_what_keeps_equal_counts_equal_length():
    empty = mono(fill(0, 10, 14))
    half = mono(fill(5, 10, 14))
    assert len(empty) == len(half)
    assert empty.startswith("`") and empty.endswith("`")


# ── shared contract: every meter renderer in the bot ───────────────────
#
# One table rather than a backtick assertion copied into eight test files.
# A new meter adds a `case()` row here. `wrapped` is False only for the two
# primitives whose output is composed into a code span by their caller —
# wrapping those would nest backticks, which Discord renders literally.


def case(name, render, *, wrapped=True):
    return pytest.param(render, wrapped, id=name)


def _live_bar(count, total):
    from bot_modules.games.utils.live_bar import build_bar

    return build_bar(count, total)[0]


def _leaderboard(fn_name, *args, **kw):
    from bot_modules.economy import leaderboard

    return getattr(leaderboard, fn_name)(*args, **kw)


METERS = [
    case("games/live_bar", lambda: _live_bar(2, 4)),
    case("games/live_bar zero total", lambda: _live_bar(0, 0)),
    case("economy/progress_bar", lambda: _leaderboard("progress_bar", 3, 10)),
    case("economy/community_progress_bar",
         lambda: _leaderboard("community_progress_bar", 4, 10)),
    case("casino/_pool_bar",
         lambda: __import__(
             "bot_modules.cogs.casino.embeds", fromlist=["x"]
         )._pool_bar(0.62)),
    case("privacy/render_progress_bar",
         lambda: __import__(
             "bot_modules.privacy.logic", fromlist=["x"]
         ).render_progress_bar(5, 10)),
    case("pressure_cooker/gauge_bar",
         lambda: __import__(
             "bot_modules.cogs.pressure_cooker.views", fromlist=["x"]
         ).gauge_bar(42)),
    case("chicken/_meter_bar",
         lambda: __import__(
             "bot_modules.cogs.chicken.cog", fromlist=["x"]
         )._meter_bar(37.5)),
    case("quest_digest/bar_meter",
         lambda: __import__(
             "bot_modules.economy.quest_digest", fromlist=["x"]
         ).bar_meter(2196, 16635)),
    # Composed into a caller-built code span — must stay raw.
    case("economy/bar_fill", lambda: _leaderboard("bar_fill", 3, 10),
         wrapped=False),
    case("economy/progress_bar code=False",
         lambda: _leaderboard("progress_bar", 3, 10, code=False),
         wrapped=False),
]


@pytest.mark.parametrize("render,wrapped", METERS)
def test_meter_code_span_contract(render, wrapped):
    """Bare ``▰▱`` runs wobble as they fill; a code span is the fix. Every
    display-site meter must carry exactly one span, and the two composable
    primitives must carry none — nested backticks render literally."""
    out = render()
    assert "▰" in out or "▱" in out, "not a meter?"
    if wrapped:
        assert out.count("`") == 2, f"expected one code span, got {out!r}"
        assert out.startswith("`"), f"span must open the string: {out!r}"
    else:
        assert "`" not in out, f"composable primitive must stay raw: {out!r}"
    assert "``" not in out, f"nested/empty span: {out!r}"


@pytest.mark.parametrize("render,wrapped", METERS)
def test_meter_uses_the_shared_glyphs(render, wrapped):
    """No renderer smuggles in its own fill characters."""
    stripped = render().strip("`")
    assert not set(stripped) & {"█", "░", "●", "○"}


def test_quest_board_row_does_not_nest_code_spans():
    """The ``/bank quests`` status cell is padded into the row's own code
    span — the regression this guards is a doubled backtick there."""
    from bot_modules.economy.quest_views import _quest_line_status

    counted = _quest_line_status(
        {"state": "active", "progress_current": 3, "progress_target": 6}
    )
    community = _quest_line_status({"state": "community", "current": 4, "target": 10})
    for cell in (counted, community):
        assert "`" not in cell, f"cell would nest inside the row span: {cell!r}"
