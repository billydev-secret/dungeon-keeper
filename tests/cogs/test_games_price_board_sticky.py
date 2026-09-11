"""Name Your Price's "keep the board at the bottom" sticky panel.

Second site on the shared ``games_board_sticky_enabled`` dial (see
docs/plans/sticky-panel-extraction.md, Group E) — same dial Mt. Rushmore
Draft uses, no per-game override. The debounce/repost/self-chase-avoidance
machinery is core.sticky.StickyPanel, already covered generically by
tests/test_core_sticky.py, and the shared shape (post-before-delete, the
resurrection guard, dropping cached ids on retire) already pinned for
Rushmore in tests/cogs/test_games_rushmore_board_sticky.py.

What's Price-specific, and what these pin: the panel is scoped to one
*round's submission window*, not the whole game (a fresh panel per round,
keyed by game_id since only one round is ever live at a time); and
``_retire_board`` hands back wherever the board actually ended up, since
everything downstream (reveal, the next round's own post) targets that, not
whatever local ``msg`` the caller started the round with — a restick that
moved the board mid-round would otherwise leave the reveal edit silently
404ing against a buried message.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.cogs.games_price_cog import PriceCog, PriceGameView
from bot_modules.core.db_utils import open_db
from bot_modules.games.utils.game_manager import create_game
from bot_modules.services.game_board_sticky_service import set_board_sticky_enabled
from bot_modules.services.games_db import GamesDb
from bot_modules.services.game_board_sticky_service import (
    board_location,
    build_board_content,
)

GUILD = 4242
CHAN_A = 778
CHAN_B = 779
HOST = 1


class _FakeGameView:
    """Stands in for PriceGameView: _board_content and _retire_board only
    ever touch these five things."""

    def __init__(self, game_id: str, guild=None):
        self.game_id = game_id
        self.round_num = 1
        self.guild = guild
        self._board_retired = False
        self._panel = None
        self.render_count = 0

    def _build_embed(self) -> discord.Embed:
        self.render_count += 1
        return discord.Embed(title=f"price board {self.game_id} #{self.render_count}")


def _ctx(sync_db_path):
    stub = MagicMock()
    stub.db_path = sync_db_path
    stub.open_db = lambda: open_db(sync_db_path)
    return stub


def _cog(sync_db_path) -> PriceCog:
    bot = SimpleNamespace(
        games_db=GamesDb(sync_db_path),
        active_views={},
        ctx=_ctx(sync_db_path),
        # StickyPanel.refresh/_delayed_restick resolve the guild through the
        # bot rather than the guild object a caller passed to place() —
        # tests that exercise refresh() point this at a guild that knows how
        # to resolve their channel (see _guild()'s channel param).
        get_guild=lambda gid: None,
    )
    return PriceCog(bot)  # type: ignore[arg-type]


def _channel(channel_id: int):
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.last_message_id = None

    def _make_message(msg_id: int) -> MagicMock:
        m = MagicMock(spec=discord.Message)
        m.id = msg_id
        m.edit = AsyncMock()
        m.delete = AsyncMock()
        return m

    channel._next_id = 1000
    channel._make_message = _make_message

    async def _send(**kwargs):
        channel._next_id += 1
        return _make_message(channel._next_id)

    channel.send = AsyncMock(side_effect=_send)
    # Used by StickyPanel.refresh() (edit in place), _delete_old() (drop the
    # replaced panel), and PriceCog._retire_board (hand back the board's
    # current location) — the exact id is asserted on where a test cares.
    partial = _make_message(0)
    channel.get_partial_message = MagicMock(return_value=partial)
    return channel


def _guild(channel=None):
    guild = MagicMock(spec=discord.Guild)
    guild.id = GUILD
    if channel is not None:
        guild.get_channel_or_thread = MagicMock(return_value=channel)
    return guild


async def _seed_game(cog: PriceCog, channel_id: int, message_id: int = 0) -> str:
    """Seed a live game row. Production always records whatever message the
    board currently occupies here — the lobby message at round 1
    (``update_game_message`` at launch), or the previous round's board
    thereafter — so ``place()`` finds it as "the old panel" to replace."""
    return await create_game(
        cog.db, channel_id, HOST, "price",
        message_id=message_id, state="playing", guild_id=GUILD,
    )


def _anchor_msg(msg_id: int) -> MagicMock:
    m = MagicMock(spec=discord.Message, id=msg_id)
    m.edit = AsyncMock()
    m.delete = AsyncMock()
    return m


# ── the dial: off by default, changes nothing ───────────────────────────────


@pytest.mark.asyncio
async def test_dial_off_by_default_open_board_edits_in_place(sync_db_path):
    cog = _cog(sync_db_path)
    game_id = await _seed_game(cog, CHAN_A)
    game_view = _FakeGameView(game_id)
    channel = _channel(CHAN_A)
    anchor = _anchor_msg(1)

    result = await cog._open_board(game_id, game_view, channel, _guild(), anchor)

    anchor.edit.assert_awaited_once()
    anchor.delete.assert_not_awaited()
    channel.send.assert_not_awaited()
    assert result is anchor
    assert game_view._panel is None
    assert cog._boards == {}


@pytest.mark.asyncio
async def test_dial_on_but_guild_none_falls_through_to_in_place_edit(sync_db_path):
    """``guild`` comes from ``getattr(channel, "guild", None)`` and a games
    channel always has one, so this is unreachable today — but
    ``StickyPanel.place`` takes a non-optional guild and dereferences
    ``guild.id`` immediately, so a ``None`` here must never reach it. Turn
    the dial on at the guild-0 fallback row (what the old ``guild.id if
    guild else 0`` read would have consulted) to prove the None case can no
    longer walk into that crash even when that row says on."""
    cog = _cog(sync_db_path)
    with cog.bot.ctx.open_db() as conn:
        set_board_sticky_enabled(conn, True, 0)
    game_id = await _seed_game(cog, CHAN_A)
    game_view = _FakeGameView(game_id)
    channel = _channel(CHAN_A)
    anchor = _anchor_msg(1)

    result = await cog._open_board(game_id, game_view, channel, None, anchor)

    anchor.edit.assert_awaited_once()
    anchor.delete.assert_not_awaited()
    channel.send.assert_not_awaited()
    assert result is anchor
    assert game_view._panel is None
    assert cog._boards == {}


# ── dial on: sticks, and follows post-before-delete ─────────────────────────


@pytest.mark.asyncio
async def test_dial_on_posts_sticky_board_and_saves_ids(sync_db_path):
    cog = _cog(sync_db_path)
    with cog.bot.ctx.open_db() as conn:
        set_board_sticky_enabled(conn, True, GUILD)
    anchor = _anchor_msg(1)
    game_id = await _seed_game(cog, CHAN_A, message_id=anchor.id)
    game_view = _FakeGameView(game_id)
    channel = _channel(CHAN_A)
    guild = _guild(channel)

    result = await cog._open_board(game_id, game_view, channel, guild, anchor)

    # place() replaces "the old panel" it reads from the game row — the
    # anchor's own id, recorded there before this round started — via the
    # channel's partial-message path, post *after* the new board is up. It
    # never touches ``anchor`` directly.
    channel.get_partial_message.assert_any_call(anchor.id)
    channel.get_partial_message.return_value.delete.assert_awaited_once()
    anchor.delete.assert_not_awaited()
    anchor.edit.assert_not_awaited()
    channel.send.assert_awaited_once()
    assert game_id in cog._boards
    assert game_view._panel is cog._boards[game_id]
    assert board_location(cog.bot, game_id) == (CHAN_A, result.id)


@pytest.mark.asyncio
async def test_placement_failure_leaves_anchor_message_alone(sync_db_path):
    """A placement failure (e.g. the bot lost Send Messages) must never have
    deleted whatever message the board is replacing first — that would leave
    the round with no board and no way to submit a price."""
    cog = _cog(sync_db_path)
    with cog.bot.ctx.open_db() as conn:
        set_board_sticky_enabled(conn, True, GUILD)
    anchor = _anchor_msg(1)
    game_id = await _seed_game(cog, CHAN_A, message_id=anchor.id)
    game_view = _FakeGameView(game_id)
    channel = _channel(CHAN_A)
    channel.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "no perms"))
    guild = _guild(channel)

    result = await cog._open_board(game_id, game_view, channel, guild, anchor)

    anchor.delete.assert_not_awaited()
    channel.get_partial_message.return_value.delete.assert_not_awaited()
    # Falls through to the ordinary non-sticky path.
    anchor.edit.assert_awaited_once()
    assert result is anchor
    assert game_id not in cog._boards
    assert game_view._panel is None


# ── build refuses once the round's board is retired (the resurrection guard) ─


@pytest.mark.asyncio
async def test_board_content_renders_while_live():
    game_view = _FakeGameView("g1")
    content = build_board_content("price board", game_view)
    assert content.embed.title == "price board g1 #1"


@pytest.mark.asyncio
async def test_board_content_refuses_once_retired():
    game_view = _FakeGameView("g1")
    game_view._board_retired = True
    with pytest.raises(RuntimeError):
        build_board_content("price board", game_view)


# ── retiring: no pending restick survives, cached ids don't outlive it, and ──
# ── the caller gets back wherever the board actually ended up ───────────────


@pytest.mark.asyncio
async def test_retire_board_cancels_pending_restick_and_drops_panel(sync_db_path):
    cog = _cog(sync_db_path)
    with cog.bot.ctx.open_db() as conn:
        set_board_sticky_enabled(conn, True, GUILD)
    anchor = _anchor_msg(1)
    game_id = await _seed_game(cog, CHAN_A, message_id=anchor.id)
    guild = _guild()
    game_view = _FakeGameView(game_id, guild=guild)
    channel = _channel(CHAN_A)
    await cog._open_board(game_id, game_view, channel, guild, anchor)
    panel = cog._boards[game_id]
    panel.schedule_restick(guild.id)
    task = panel._restick_tasks[guild.id]

    cog._retire_board(game_view, channel)

    assert game_view._board_retired is True
    assert game_id not in cog._boards
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_retire_board_forgets_cached_ids(sync_db_path):
    """A message landing between the round closing and _retire_board running
    could otherwise read a stale cached (channel, message) pair through
    on_message and schedule a restick nothing cancels — reaching
    _board_content's refusal and logging it as an error for an ordinary round
    ending. forget() (mirroring RushmoreCog._retire_board / the auction
    card's _release_panel at close) forces the next read to miss the cache."""
    cog = _cog(sync_db_path)
    with cog.bot.ctx.open_db() as conn:
        set_board_sticky_enabled(conn, True, GUILD)
    anchor = _anchor_msg(1)
    game_id = await _seed_game(cog, CHAN_A, message_id=anchor.id)
    channel = _channel(CHAN_A)
    guild = _guild(channel)
    game_view = _FakeGameView(game_id, guild=guild)
    await cog._open_board(game_id, game_view, channel, guild, anchor)
    panel = cog._boards[game_id]
    # Warm the ref cache the way a live round's own resticks/on_message calls
    # would.
    await panel._cached_ids(guild.id)
    assert guild.id in panel._ref

    cog._retire_board(game_view, channel)

    assert guild.id not in panel._ref


@pytest.mark.asyncio
async def test_retire_board_returns_none_when_dial_was_off(sync_db_path):
    cog = _cog(sync_db_path)
    game_id = await _seed_game(cog, CHAN_A, message_id=1)
    game_view = _FakeGameView(game_id)
    channel = _channel(CHAN_A)
    anchor = _anchor_msg(1)
    await cog._open_board(game_id, game_view, channel, _guild(), anchor)

    result = cog._retire_board(game_view, channel)

    assert result is None
    assert game_view._board_retired is True


@pytest.mark.asyncio
async def test_retire_board_hands_back_a_repost_not_the_original_anchor(sync_db_path):
    """The whole reason _retire_board returns a message: if chat buried the
    board mid-round and a restick moved it, the reveal edit (and the next
    round's own post) must target the *new* location, not the message the
    round started with — an edit against the old one would silently 404."""
    cog = _cog(sync_db_path)
    with cog.bot.ctx.open_db() as conn:
        set_board_sticky_enabled(conn, True, GUILD)
    anchor = _anchor_msg(1)
    game_id = await _seed_game(cog, CHAN_A, message_id=anchor.id)
    guild = _guild()
    game_view = _FakeGameView(game_id, guild=guild)
    channel = _channel(CHAN_A)
    first = await cog._open_board(game_id, game_view, channel, guild, anchor)
    assert board_location(cog.bot, game_id) == (CHAN_A, first.id)

    # Chat buried it; an explicit repost simulates the debounced restick
    # landing (core.sticky's own debounce timing is covered generically in
    # tests/test_core_sticky.py) — what matters here is that the round loop
    # follows the repost rather than the message it started with.
    panel = cog._boards[game_id]
    second = await panel.place(guild, channel)
    assert second is not None and second.id != first.id

    current = cog._retire_board(game_view, channel)

    # channel.get_partial_message is the shared stand-in _channel() gives
    # every test (its return value doesn't vary with the id passed in, the
    # same way the Rushmore sticky tests use it to assert deletes) — what
    # matters here is which id _retire_board asked for: the repost's, not
    # the round's original anchor.
    assert current is not None
    assert current is channel.get_partial_message.return_value
    channel.get_partial_message.assert_any_call(second.id)
    assert second.id != anchor.id


@pytest.mark.asyncio
async def test_cog_unload_cancels_every_boards_pending_restick(sync_db_path):
    cog = _cog(sync_db_path)
    with cog.bot.ctx.open_db() as conn:
        set_board_sticky_enabled(conn, True, GUILD)
    game_a = await _seed_game(cog, CHAN_A, message_id=1)
    game_b = await _seed_game(cog, CHAN_B, message_id=2)
    guild = _guild()
    view_a, view_b = _FakeGameView(game_a, guild=guild), _FakeGameView(game_b, guild=guild)
    await cog._open_board(game_a, view_a, _channel(CHAN_A), guild, _anchor_msg(1))
    await cog._open_board(game_b, view_b, _channel(CHAN_B), guild, _anchor_msg(2))
    panel_a, panel_b = cog._boards[game_a], cog._boards[game_b]
    panel_a.schedule_restick(guild.id)
    panel_b.schedule_restick(guild.id)
    task_a, task_b = panel_a._restick_tasks[guild.id], panel_b._restick_tasks[guild.id]

    await cog.cog_unload()

    assert cog._boards == {}
    await asyncio.sleep(0)
    assert task_a.cancelled() or task_a.done()
    assert task_b.cancelled() or task_b.done()


# ── two games in different channels don't interfere ─────────────────────────


@pytest.mark.asyncio
async def test_two_games_in_different_channels_do_not_interfere(sync_db_path):
    cog = _cog(sync_db_path)
    with cog.bot.ctx.open_db() as conn:
        set_board_sticky_enabled(conn, True, GUILD)
    game_a = await _seed_game(cog, CHAN_A, message_id=1)
    game_b = await _seed_game(cog, CHAN_B, message_id=2)
    guild = _guild()
    view_a, view_b = _FakeGameView(game_a, guild=guild), _FakeGameView(game_b, guild=guild)
    channel_a, channel_b = _channel(CHAN_A), _channel(CHAN_B)

    msg_a = await cog._open_board(game_a, view_a, channel_a, guild, _anchor_msg(1))
    msg_b = await cog._open_board(game_b, view_b, channel_b, guild, _anchor_msg(2))

    assert board_location(cog.bot, game_a) == (CHAN_A, msg_a.id)
    assert board_location(cog.bot, game_b) == (CHAN_B, msg_b.id)

    # Reposting A must not touch B's stored ids or panel.
    panel_a = cog._boards[game_a]
    await panel_a.place(guild, channel_a)
    assert board_location(cog.bot, game_b) == (CHAN_B, msg_b.id)

    # Retiring A must not retire or drop B.
    cog._retire_board(view_a, channel_a)
    assert game_a not in cog._boards
    assert game_b in cog._boards
    assert view_b._board_retired is False


# ── wiring: the real _run_round and end_early actually call the above ───────


@pytest.mark.asyncio
async def test_run_round_retires_board_on_forced_end_while_submissions_open(sync_db_path):
    """/games end (or the host's own End Game button, racing concurrently —
    see end_early below) can land while the submission window is still
    open. Whichever fired, _run_round's own wake-up must retire the
    round's board before returning: the promptness rule fix 3 established
    for Rushmore (forget() on retire), applied at the one place Price's
    recursive round function actually checks ``_closed``."""
    cog = _cog(sync_db_path)
    with cog.bot.ctx.open_db() as conn:
        set_board_sticky_enabled(conn, True, GUILD)
    anchor = _anchor_msg(1)
    game_id = await _seed_game(cog, CHAN_A, message_id=anchor.id)
    channel = _channel(CHAN_A)
    guild = _guild(channel)
    cog._get_scenario = AsyncMock(return_value="Eat a bug")  # type: ignore[method-assign]

    task = asyncio.create_task(
        cog._run_round(
            game_id, HOST, "Host", channel, guild, 1,
            {"rounds": 1, "timer": 30}, anchor,
        )
    )
    try:
        # Real (short) sleeps, not asyncio.sleep(0): the game row and payload
        # reads go through the real (thread-backed) db, so a bare-yield spin
        # can outrun them before active_views is ever populated.
        game_view = None
        for _ in range(500):
            game_view = cog.bot.active_views.get(game_id)
            if game_view is not None and game_view._timer is not None:
                break
            if task.done():
                task.result()  # surface a real failure instead of timing out below
            await asyncio.sleep(0.01)
        assert game_view is not None and game_view._timer is not None, (
            "_run_round never reached the submission wait"
        )
        # The board's up and sticky before the forced end lands.
        assert game_view._panel is cog._boards.get(game_id)

        # Simulate the forced end: something else (force_end_active_game,
        # or end_early below) sets _closed and skips the timer concurrently
        # with _run_round's own await.
        game_view._closed = True
        game_view.skip_timer()

        await asyncio.wait_for(task, timeout=5)
    finally:
        if not task.done():
            task.cancel()

    assert game_view._board_retired is True
    assert game_id not in cog._boards


@pytest.mark.asyncio
async def test_end_early_retires_sticky_board_via_panel_refresh(sync_db_path):
    """The host's own End Game button races the round loop's wake-up (two
    concurrent coroutines both reacting to ``_closed``) — a wrinkle
    Rushmore doesn't have, since it has no in-view End Game button. end_early
    must retire the round's board itself rather than leave it to the loop's
    own turn, and its disabled-frame render must go through the panel
    (embed *and* view) rather than a plain edit against a message a restick
    may have already moved."""
    cog = _cog(sync_db_path)
    with cog.bot.ctx.open_db() as conn:
        set_board_sticky_enabled(conn, True, GUILD)
    anchor = _anchor_msg(1)
    game_id = await _seed_game(cog, CHAN_A, message_id=anchor.id)
    channel = _channel(CHAN_A)
    guild = _guild(channel)
    # StickyPanel.refresh resolves the guild through the bot, not through
    # whatever guild object a caller happens to hold.
    cog.bot.get_guild = lambda gid: guild

    game_view = PriceGameView(
        game_id=game_id, host_id=HOST, host_name="Host", scenario="Eat a bug",
        round_num=1, total_rounds=3, timer_secs=30, db=cog.db, bot=cog.bot,
        cog=cog, expected_ids=set(), settings={"rounds": 3}, guild=guild,
    )
    await cog._open_board(game_id, game_view, channel, guild, anchor)
    assert game_view._panel is not None
    cog._show_recap = AsyncMock()  # type: ignore[method-assign]

    await cog.end_early(game_view, channel, guild)

    assert game_view._closed is True
    assert game_view._board_retired is True
    assert game_id not in cog._boards
    # Disabled through the panel, not a bare _msg.edit — edit_kwargs always
    # carries the (disabled) view alongside the embed, so both survive.
    channel.get_partial_message.return_value.edit.assert_awaited()
    edit_kwargs = channel.get_partial_message.return_value.edit.await_args.kwargs
    assert "view" in edit_kwargs and "embed" in edit_kwargs
    cog._show_recap.assert_awaited_once()


# ── a submission's redraw racing the round's own retirement ────────────────


@pytest.mark.asyncio
async def test_refresh_embed_swallows_a_retired_panels_refusal(sync_db_path):
    """``PriceModal.on_submit`` calls ``refresh_embed`` directly, with nothing
    synchronising it against the round closing: two players submitting in the
    same moment means one submission completes the round (``skip_timer`` →
    ``_retire_board``) while the other's redraw is still in flight. That
    redraw then reaches ``_board_content``'s deliberate resurrection refusal,
    which ``StickyPanel.refresh`` does not guard — so it used to surface as an
    unhandled RuntimeError traceback for a perfectly ordinary round ending."""
    cog = _cog(sync_db_path)
    guild = _guild()
    game_view = PriceGameView(
        game_id="g", host_id=HOST, host_name="Host", scenario="Eat a bug",
        round_num=1, total_rounds=3, timer_secs=30, db=cog.db, bot=cog.bot,
        cog=cog, expected_ids=set(), settings={"rounds": 3}, guild=guild,
    )
    panel = MagicMock()
    panel.refresh = AsyncMock(side_effect=RuntimeError("board retired"))
    game_view._panel = panel

    await game_view.refresh_embed()

    panel.refresh.assert_awaited_once_with(GUILD)
