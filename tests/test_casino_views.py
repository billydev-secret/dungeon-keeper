"""Casino view construction — the pieces testable without a gateway.

The hub-view filter is the enforcement of the dashboard's promise that
"unchecked games disappear from the hub panel"; the derby button template
is the bounds guard for stale DynamicItems.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.cogs.casino.views import (
    DerbyBetButton,
    RoundResolveButton,
    build_hub_view,
)
from bot_modules.services import casino_logic as logic
from bot_modules.services.casino_service import GAMES, CasinoSettings


def _custom_ids(view) -> set[str]:
    return {getattr(item, "custom_id", "") or "" for item in view.children}


def test_hub_view_drops_disabled_tables():
    view = build_hub_view(
        CasinoSettings(derby_enabled=False, slots_enabled=False)
    )
    ids = _custom_ids(view)
    assert "casino:derby" not in ids and "casino:slots" not in ids
    assert {"casino:coinflip", "casino:blackjack", "casino:roulette"} <= ids
    # non-game buttons always stay
    assert {"casino:stats", "casino:help"} <= ids


def test_hub_view_full_house_by_default():
    ids = _custom_ids(build_hub_view(CasinoSettings()))
    assert {f"casino:{game}" for game in GAMES} <= ids


def _rows(view) -> list[list[str]]:
    """The view's buttons grouped by row, in row order."""
    rows: dict[int, list[str]] = {}
    for item in view.children:
        rows.setdefault(item.row, []).append(getattr(item, "label", ""))
    return [rows[r] for r in sorted(rows)]


@pytest.mark.parametrize(
    ("closed", "game_rows"),
    [
        # Todo #87: a 5-wide row wraps on narrow clients and drops a short
        # "derby line"; three per row fits every client. The full house must
        # keep that shape through the #98 repack.
        pytest.param((), [3, 3, 3], id="full-house-keeps-3-3-3"),
        # Todo #98: rows come from the enabled set, so a closed table
        # shortens two rows by one rather than leaving one row alone.
        pytest.param(("keno",), [3, 3, 2], id="eight"),
        pytest.param(("war", "keno"), [3, 2, 2], id="seven"),
        pytest.param(("dice", "war", "keno"), [3, 3], id="six"),
        # Billy's report: derby + baccarat closed used to leave Roulette
        # full-width and alone on row 1.
        pytest.param(("derby", "baccarat"), [3, 2, 2], id="lone-roulette"),
        pytest.param(
            ("derby", "baccarat", "dice", "war", "keno"),
            [2, 2],
            id="four-tables-open",
        ),
        pytest.param(
            ("slots", "blackjack", "roulette", "derby", "baccarat", "dice",
             "war", "keno"),
            [1],
            id="one-table-open",
        ),
        pytest.param(GAMES, [], id="all-tables-closed"),
    ],
)
def test_hub_view_packs_rows_from_the_enabled_set(closed, game_rows):
    settings = CasinoSettings(**{f"{game}_enabled": False for game in closed})
    rows = _rows(build_hub_view(settings))

    assert [len(row) for row in rows[:-1]] == game_rows
    # No row is ever shorter than one below it — a short row beside full
    # ones is the ragged render #98 is about.
    assert game_rows == sorted(game_rows, reverse=True)
    # The utility buttons follow the games immediately, gap or no games.
    assert rows[-1] == ["My Stats", "How It Works"]
    assert len(rows) <= 5, "Discord allows at most five action rows"


def test_hub_view_repack_preserves_game_order():
    """Repacking reflows rows; it must never move a game past a neighbour."""
    closed = {"slots", "derby", "keno"}
    settings = CasinoSettings(**{f"{game}_enabled": False for game in closed})
    packed = [label for row in _rows(build_hub_view(settings))[:-1] for label in row]

    expected = [g.capitalize() for g in GAMES if g not in closed]
    assert packed == expected


def test_derby_button_template_is_bounds_anchored():
    template = DerbyBetButton.__discord_ui_compiled_template__
    assert template.fullmatch(f"casino_dy:{len(logic.DERBY_FIELD) - 1}:12")
    assert template.fullmatch(f"casino_dy:{len(logic.DERBY_FIELD)}:12") is None


def test_resolve_button_template_matches_exactly_the_five_games():
    template = RoundResolveButton.__discord_ui_compiled_template__
    for game in ("roulette", "derby", "baccarat", "dice", "keno"):
        assert template.fullmatch(f"casino_go:{game}:12"), game
    # A stale id for a game that no longer exists must fail the match
    # rather than reach the cog's lookup.
    assert template.fullmatch("casino_go:pools:12") is None
    assert template.fullmatch("casino_go:blackjack:12") is None


def test_every_dynamic_item_is_registered_at_cog_load():
    """A DynamicItem that is never handed to ``add_dynamic_items`` builds
    and renders perfectly and then silently fails to route on click.

    This is glue, not behaviour, which is exactly why it is worth one
    assertion: RoundResolveButton shipped unregistered and every Spin /
    Race / Deal / Roll / Draw press would have done nothing at all.
    """
    import inspect

    from bot_modules.cogs.casino import cog as casino_cog
    from bot_modules.cogs.casino import views as casino_views

    defined = {
        name
        for name, obj in inspect.getmembers(casino_views, inspect.isclass)
        if issubclass(obj, discord.ui.DynamicItem)
        and obj is not discord.ui.DynamicItem
        and obj.__module__ == casino_views.__name__
    }
    source = inspect.getsource(casino_cog.CasinoCog.cog_load)
    registered = {name for name in defined if name in source}
    assert defined == registered, (
        "casino DynamicItems missing from add_dynamic_items (their buttons "
        f"would render but never route): {sorted(defined - registered)}"
    )
