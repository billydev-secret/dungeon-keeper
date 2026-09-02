"""Casino view construction — the pieces testable without a gateway.

The hub-view filter is the enforcement of the dashboard's promise that
"unchecked games disappear from the hub panel"; the derby button template
is the bounds guard for stale DynamicItems.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.cogs.casino.views import (
    AmountPickerView,
    DerbyBetButton,
    RouletteBetModal,
    RouletteNumberView,
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
        # Mines (2026-08-16) made it ten, which is exactly where the hub
        # runs out of room: four game rows plus the utility row IS
        # Discord's five, with nothing spare. The nine-table shape still
        # has to be the 3/3/3 todo #87 chose.
        pytest.param((), [3, 3, 2, 2], id="full-house-of-ten"),
        pytest.param(("mines",), [3, 3, 3], id="nine-keeps-3-3-3"),
        # Todo #98: rows come from the enabled set, so a closed table
        # shortens two rows by one rather than leaving one row alone.
        pytest.param(("mines", "keno"), [3, 3, 2], id="eight"),
        pytest.param(("mines", "war", "keno"), [3, 2, 2], id="seven"),
        pytest.param(("mines", "dice", "war", "keno"), [3, 3], id="six"),
        # Billy's report: derby + baccarat closed used to leave Roulette
        # full-width and alone on row 1.
        pytest.param(
            ("mines", "derby", "baccarat"), [3, 2, 2], id="lone-roulette"
        ),
        pytest.param(
            ("mines", "derby", "baccarat", "dice", "war", "keno"),
            [2, 2],
            id="four-tables-open",
        ),
        pytest.param(
            ("mines", "slots", "blackjack", "roulette", "derby", "baccarat",
             "dice", "war", "keno"),
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


# ── the amount ladder (ephemeral-UI audit M2 / M3) ───────────────────

from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402


def _fake_modal_factory():
    """A stand-in for the typed modal, recording what got placed through it."""
    placed: list[int] = []
    modal = SimpleNamespace(_place=AsyncMock(side_effect=lambda c, i, a: placed.append(a)))
    return (lambda: modal), placed, modal


def _picker_interaction():
    return SimpleNamespace(
        client=SimpleNamespace(get_cog=lambda name: object()),
        response=SimpleNamespace(send_modal=AsyncMock(), send_message=AsyncMock()),
    )


def _options(*amounts):
    return [logic.BetOption(f"{a}", a) for a in amounts]


def test_amount_picker_renders_a_rung_per_option_plus_custom():
    make_modal, _, _ = _fake_modal_factory()
    view = AmountPickerView(make_modal, _options(25, 12, 50, 100))
    assert [c.label for c in view.children] == [
        "25", "12", "50", "100", "Custom…",
    ]
    # Rungs on one row, the escape hatch below it.
    assert {c.row for c in view.children if c.label != "Custom…"} == {0}


def test_amount_picker_only_offers_back_when_there_is_a_board_to_return_to():
    make_modal, _, _ = _fake_modal_factory()
    plain = AmountPickerView(make_modal, _options(25))
    assert "Back" not in [c.label for c in plain.children]
    with_board = AmountPickerView(make_modal, _options(25), on_cancel=AsyncMock())
    assert "Back" in [c.label for c in with_board.children]


async def test_a_rung_places_its_own_amount_through_the_modals_route():
    """Tap and typed path settle identically — one _place, two front doors."""
    make_modal, placed, _ = _fake_modal_factory()
    view = AmountPickerView(make_modal, _options(25, 50))
    fifty = next(c for c in view.children if c.label == "50")

    await fifty.callback(_picker_interaction())

    assert placed == [50]


async def test_custom_still_opens_the_typed_box():
    make_modal, placed, modal = _fake_modal_factory()
    view = AmountPickerView(make_modal, _options(25))
    custom = next(c for c in view.children if c.label == "Custom…")
    interaction = _picker_interaction()

    await custom.callback(interaction)

    assert interaction.response.send_modal.await_args.args[0] is modal
    assert placed == []


async def test_back_hands_control_to_the_boards_restorer():
    cancel = AsyncMock()
    make_modal, _, _ = _fake_modal_factory()
    view = AmountPickerView(make_modal, _options(25), on_cancel=cancel)
    back = next(c for c in view.children if c.label == "Back")
    interaction = _picker_interaction()

    await back.callback(interaction)

    cancel.assert_awaited_once_with(interaction)


async def test_a_timed_out_step_puts_the_board_back():
    """The step replaces the round's board, so an abandoned one must not
    leave the player holding buttons that no longer answer."""
    expiry = AsyncMock()
    make_modal, _, _ = _fake_modal_factory()
    await AmountPickerView(make_modal, _options(25), on_expiry=expiry).on_timeout()
    expiry.assert_awaited_once()


def test_roulette_number_view_covers_the_whole_wheel_in_two_selects():
    """37 numbers overflow one select's 25-option cap, so the wheel splits."""
    view = RouletteNumberView(7)
    selects = [c for c in view.children if isinstance(c, discord.ui.Select)]
    assert len(selects) == 2
    values = [int(o.value) for s in selects for o in s.options]
    assert values == list(range(37))
    assert all(len(s.options) <= 25 for s in selects)


async def test_picking_a_number_carries_it_into_the_amount_step():
    view = RouletteNumberView(7)
    low = [c for c in view.children if isinstance(c, discord.ui.Select)][0]
    low._values = ["17"]
    cog = SimpleNamespace(open_roulette_amount_picker=AsyncMock())
    interaction = SimpleNamespace(
        client=SimpleNamespace(get_cog=lambda name: cog),
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    await low.callback(interaction)

    cog.open_roulette_amount_picker.assert_awaited_once_with(
        interaction, 7, "number", 17
    )


def test_roulette_modal_no_longer_asks_for_a_typed_number():
    """The number is decided before the modal exists, so only the stake is
    left to type — and there is no 0–36 validation branch to fail."""
    modal = RouletteBetModal(7, "number", 17)
    assert len(modal.children) == 1
    assert modal.title == "Back Straight 17"


# ── where a bet step renders (the #95 promise) ───────────────────────

from bot_modules.cogs.casino.cog import CasinoCog  # noqa: E402


def _cog() -> CasinoCog:
    return CasinoCog(SimpleNamespace(ctx=None))  # type: ignore[arg-type]


def _press_from(*, ephemeral: bool | None):
    """An interaction whose press came from an ephemeral surface, a public
    one, or (None) from a modal, which carries no message at all."""
    message = (
        None if ephemeral is None
        else SimpleNamespace(flags=SimpleNamespace(ephemeral=ephemeral))
    )
    return SimpleNamespace(
        message=message,
        response=SimpleNamespace(
            send_modal=AsyncMock(), send_message=AsyncMock(), edit_message=AsyncMock()
        ),
    )


async def test_a_hub_press_opens_the_ladder_privately():
    make_modal, _, _ = _fake_modal_factory()
    interaction = _press_from(ephemeral=False)

    await _cog()._open_amount_picker(
        interaction, make_modal, _options(25), prompt="**Blackjack** — how much?"
    )

    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert kwargs["content"] == "**Blackjack** — how much?"


async def test_a_press_inside_a_private_surface_replaces_it_in_place():
    """The whole point of the private windows: a wager costs no new message,
    and the board's embed comes off so the step stands alone."""
    make_modal, _, _ = _fake_modal_factory()
    interaction = _press_from(ephemeral=True)

    await _cog()._open_amount_picker(
        interaction, make_modal, _options(25), prompt="**Red** — how much?"
    )

    interaction.response.send_message.assert_not_awaited()
    kwargs = interaction.response.edit_message.await_args.kwargs
    assert kwargs["content"] == "**Red** — how much?"
    assert kwargs["embed"] is None


async def test_no_legal_stake_falls_back_to_the_typed_box():
    """A ladder with no rungs would be a dead end; the modal's own service
    call is what tells a broke or capped-out player which it is."""
    make_modal, _, modal = _fake_modal_factory()
    interaction = _press_from(ephemeral=False)

    await _cog()._open_amount_picker(
        interaction, make_modal, [], prompt="**Blackjack** — how much?"
    )

    assert interaction.response.send_modal.await_args.args[0] is modal
    interaction.response.send_message.assert_not_awaited()
