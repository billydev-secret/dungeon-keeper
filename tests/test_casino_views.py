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
    """37 numbers overflow one select's 25-option cap, so the wheel splits.

    Built with on_cancel, the way the cog always builds it: a select fills a
    whole row, so the two of them leave only row 2 for Back. Constructing it
    without one is what hid a ValueError that made 🎯 Number unusable.
    """
    view = RouletteNumberView(7, on_cancel=AsyncMock())
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


def test_the_number_step_still_fits_once_back_is_on_it():
    """discord.py refuses a row over 5 wide at construction, so this is the
    whole bug: the view raised before it could ever render."""
    view = RouletteNumberView(7, on_cancel=AsyncMock())
    back = next(
        c for c in view.children if getattr(c, "label", None) == "Back"
    )
    assert back.row == 2
    selects = [c for c in view.children if isinstance(c, discord.ui.Select)]
    assert [s.row for s in selects] == [0, 1]


async def test_a_superseded_step_leaves_the_board_to_whatever_replaced_it():
    """Each view runs its own 600s timer and discord.py cancels none of them
    when one view replaces another, so an abandoned step would wake up later
    and repaint over the step the player is actually in."""
    cog = _cog()
    repaint = AsyncMock()
    cog._repaint_window = repaint  # type: ignore[method-assign]
    guild = SimpleNamespace(id=1)
    ui = SimpleNamespace(key="roulette")

    _, expiry_first = cog._window_step_handlers(guild, ui, 7)  # type: ignore[arg-type]
    _, expiry_second = cog._window_step_handlers(guild, ui, 7)  # type: ignore[arg-type]

    await expiry_first()
    repaint.assert_not_awaited()

    await expiry_second()
    repaint.assert_awaited_once()


async def test_backing_out_hands_the_claim_back():
    """Back restores the board itself, so the same step's later timeout must
    not repaint a second time over whatever is there by then."""
    cog = _cog()
    repaint = AsyncMock()
    cog._repaint_window = repaint  # type: ignore[method-assign]
    guild = SimpleNamespace(id=1)
    ui = SimpleNamespace(key="derby")
    cancel, expiry = cog._window_step_handlers(guild, ui, 3)  # type: ignore[arg-type]

    interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()))
    await cancel(interaction)
    assert repaint.await_count == 1

    await expiry()
    assert repaint.await_count == 1


async def test_a_refused_bet_puts_the_board_back(monkeypatch):
    """The step is standing where the board was, so a refusal that just
    apologised would leave the player looking at a ladder and no round."""
    import bot_modules.cogs.casino.cog as casino_cog

    monkeypatch.setattr(casino_cog, "safe_ephemeral", AsyncMock())
    cog = _cog()
    repaint = AsyncMock()
    cog._repaint_window = repaint  # type: ignore[method-assign]
    interaction = SimpleNamespace(user=SimpleNamespace(id=5))

    await cog._finish_window_bet(
        interaction,  # type: ignore[arg-type]
        SimpleNamespace(key="roulette"),  # type: ignore[arg-type]
        SimpleNamespace(id=1),  # type: ignore[arg-type]
        7, 25, "You can't cover that.", "Red",
    )

    repaint.assert_awaited_once()


async def test_back_re_renders_the_picker_a_hub_ladder_replaced():
    """Mines and coinflip choose something before the stake, so their ladder
    covers a picker rather than a board — Back has to re-render it, or a
    refused stake strands the player away from the choice they just made."""
    from bot_modules.cogs.casino.views import build_mines_risk_view

    cancel = CasinoCog._back_to(
        "How dangerous do you want it?", build_mines_risk_view
    )
    interaction = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))

    await cancel(interaction)

    kwargs = interaction.response.edit_message.await_args.kwargs
    assert kwargs["content"] == "How dangerous do you want it?"
    assert kwargs["embed"] is None
    assert len(kwargs["view"].children) == len(logic.MINES_BOMB_CHOICES)


# ── a closed table refuses at the picker, not only on the hub ─────────

from bot_modules.core.db_utils import open_db  # noqa: E402
from bot_modules.services.casino_service import save_casino_settings  # noqa: E402


def _db_cog(db_path) -> tuple[CasinoCog, AsyncMock]:
    ctx = SimpleNamespace(open_db=lambda: open_db(db_path))
    cog = CasinoCog(SimpleNamespace(ctx=ctx))  # type: ignore[arg-type]
    picker = AsyncMock()
    cog._open_amount_picker = picker  # type: ignore[method-assign]
    return cog, picker


def _table_press():
    return SimpleNamespace(
        guild=SimpleNamespace(id=900),
        user=SimpleNamespace(id=31),
        message=None,
        response=SimpleNamespace(
            send_message=AsyncMock(), is_done=lambda: False
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )


@pytest.mark.parametrize(
    ("game", "open"),
    [
        pytest.param("slots", True, id="slots-open"),
        pytest.param("slots", False, id="slots-closed"),
        pytest.param("mines", True, id="mines-open"),
        pytest.param("mines", False, id="mines-closed"),
    ],
)
async def test_a_bet_picker_reads_the_tables_own_dial(sync_db_path, game, open):
    """The hub drops a closed table's button, but a hub rendered before the
    admin unticked it still carries one — so the picker checks the same dial
    rather than trusting the button that opened it."""
    with open_db(sync_db_path) as conn:
        save_casino_settings(conn, 900, {f"{game}_enabled": open})
    cog, picker = _db_cog(sync_db_path)
    interaction = _table_press()

    if game == "mines":
        await cog.open_mines_bet_picker(interaction, 3)  # type: ignore[arg-type]
    else:
        await cog.open_bet_picker(interaction, game)  # type: ignore[arg-type]

    if open:
        picker.assert_awaited_once()
        interaction.response.send_message.assert_not_awaited()
    else:
        picker.assert_not_awaited()
        args, kwargs = interaction.response.send_message.await_args
        assert args[0] == "❌ That table is closed right now."
        assert kwargs["ephemeral"] is True
