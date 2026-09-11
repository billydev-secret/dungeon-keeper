"""Mt. Rushmore's "keep the board at the bottom" sticky panel.

The debounce/repost/self-chase-avoidance machinery is core.sticky.StickyPanel,
already covered generically by tests/test_core_sticky.py. What's Rushmore-
specific — and what these pin — is: the dial defaults off and changes nothing
until it's on; how the panel's ids map onto the live-game row (the same one
the busy-check's jump link and ``/games end`` read); that a retired board
refuses to render again (the resurrection guard); that every teardown path
(natural end, forced end via the try/finally around both draft loops, and
cog unload) leaves no pending restick; and that two games in different
channels of one guild don't share state.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.cogs.games_rushmore_cog import RushmoreCog
from bot_modules.core.db_utils import open_db
from bot_modules.games.utils.game_manager import create_game
from bot_modules.services.game_board_sticky_service import set_board_sticky_enabled
from bot_modules.services.games_db import GamesDb

GUILD = 4242
CHAN_A = 778
CHAN_B = 779
HOST = 1


class _FakeDraftView:
    """Stands in for RushmoreDraftView: _board_content and _retire_board only
    ever touch these three things."""

    def __init__(self, game_id: str):
        self.game_id = game_id
        self._board_retired = False
        self._panel = None
        self.render_count = 0

    def _build_embed(self) -> discord.Embed:
        self.render_count += 1
        return discord.Embed(title=f"board {self.game_id} #{self.render_count}")


def _ctx(sync_db_path):
    stub = MagicMock()
    stub.db_path = sync_db_path
    stub.open_db = lambda: open_db(sync_db_path)
    return stub


def _cog(sync_db_path) -> RushmoreCog:
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
    return RushmoreCog(bot)  # type: ignore[arg-type]


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
    # Used by StickyPanel.refresh() (edit in place) and _delete_old() (drop
    # the replaced panel) — the exact target id is never asserted on here.
    partial = _make_message(0)
    channel.get_partial_message = MagicMock(return_value=partial)
    return channel


def _guild(channel=None):
    guild = MagicMock(spec=discord.Guild)
    guild.id = GUILD
    if channel is not None:
        guild.get_channel_or_thread = MagicMock(return_value=channel)
    return guild


async def _seed_game(cog: RushmoreCog, channel_id: int) -> str:
    return await create_game(
        cog.db, channel_id, HOST, "rushmore",
        message_id=0, state="playing", guild_id=GUILD,
    )


# ── the dial: off by default, changes nothing ───────────────────────────────


@pytest.mark.asyncio
async def test_dial_off_by_default_open_board_edits_in_place(sync_db_path):
    cog = _cog(sync_db_path)
    game_id = await _seed_game(cog, CHAN_A)
    draft_view = _FakeDraftView(game_id)
    channel = _channel(CHAN_A)
    lobby_msg = MagicMock(spec=discord.Message, id=1)
    lobby_msg.edit = AsyncMock()
    lobby_msg.delete = AsyncMock()

    result = await cog._open_board(game_id, draft_view, channel, _guild(), lobby_msg)

    lobby_msg.edit.assert_awaited_once()
    lobby_msg.delete.assert_not_awaited()
    channel.send.assert_not_awaited()
    assert result is lobby_msg
    assert draft_view._panel is None
    assert cog._boards == {}


# ── dial on: sticks, and a repost re-points the stored id ──────────────────


@pytest.mark.asyncio
async def test_dial_on_posts_sticky_board_and_saves_ids(sync_db_path):
    cog = _cog(sync_db_path)
    with cog.bot.ctx.open_db() as conn:
        set_board_sticky_enabled(conn, True, GUILD)
    game_id = await _seed_game(cog, CHAN_A)
    draft_view = _FakeDraftView(game_id)
    channel = _channel(CHAN_A)
    lobby_msg = MagicMock(spec=discord.Message, id=1)
    lobby_msg.edit = AsyncMock()
    lobby_msg.delete = AsyncMock()
    guild = _guild()

    result = await cog._open_board(game_id, draft_view, channel, guild, lobby_msg)

    lobby_msg.delete.assert_awaited_once()  # old lobby message dropped
    lobby_msg.edit.assert_not_awaited()
    channel.send.assert_awaited_once()
    assert game_id in cog._boards
    assert draft_view._panel is cog._boards[game_id]
    assert cog._board_ids(game_id) == (CHAN_A, result.id)


@pytest.mark.asyncio
async def test_repost_repoints_the_stored_id_and_anchor(sync_db_path):
    cog = _cog(sync_db_path)
    with cog.bot.ctx.open_db() as conn:
        set_board_sticky_enabled(conn, True, GUILD)
    game_id = await _seed_game(cog, CHAN_A)
    draft_view = _FakeDraftView(game_id)
    channel = _channel(CHAN_A)
    lobby_msg = MagicMock(spec=discord.Message, id=1)
    lobby_msg.edit = AsyncMock()
    lobby_msg.delete = AsyncMock()
    guild = _guild(channel)
    cog.bot.get_guild = lambda gid: guild if gid == GUILD else None

    first = await cog._open_board(game_id, draft_view, channel, guild, lobby_msg)
    assert cog._board_ids(game_id) == (CHAN_A, first.id)

    # Chat buried it; an explicit repost simulates the debounced restick
    # landing (core.sticky's own debounce timing is covered generically in
    # tests/test_core_sticky.py — what matters here is that Rushmore's ids
    # follow the repost).
    panel = cog._boards[game_id]
    second = await panel.place(guild, channel)

    assert second is not None
    assert second.id != first.id
    assert cog._board_ids(game_id) == (CHAN_A, second.id)

    # refresh_board's seam (panel.refresh) reads the *current* anchor, not a
    # stale cached one.
    await panel.refresh(guild.id)
    assert draft_view.render_count >= 1


# ── build refuses once the game is over (the resurrection guard) ───────────


@pytest.mark.asyncio
async def test_board_content_renders_while_live(sync_db_path):
    cog = _cog(sync_db_path)
    draft_view = _FakeDraftView("g1")
    content = await cog._board_content(draft_view, _guild())
    assert content.embed.title == "board g1 #1"


@pytest.mark.asyncio
async def test_board_content_refuses_once_retired(sync_db_path):
    cog = _cog(sync_db_path)
    draft_view = _FakeDraftView("g1")
    draft_view._board_retired = True
    with pytest.raises(RuntimeError):
        await cog._board_content(draft_view, _guild())


# ── teardown: no pending restick survives ───────────────────────────────────


@pytest.mark.asyncio
async def test_retire_board_cancels_pending_restick_and_drops_panel(sync_db_path):
    cog = _cog(sync_db_path)
    with cog.bot.ctx.open_db() as conn:
        set_board_sticky_enabled(conn, True, GUILD)
    game_id = await _seed_game(cog, CHAN_A)
    draft_view = _FakeDraftView(game_id)
    channel = _channel(CHAN_A)
    lobby_msg = MagicMock(spec=discord.Message, id=1)
    lobby_msg.edit = AsyncMock()
    lobby_msg.delete = AsyncMock()
    guild = _guild()
    await cog._open_board(game_id, draft_view, channel, guild, lobby_msg)
    panel = cog._boards[game_id]
    panel.schedule_restick(guild.id)
    task = panel._restick_tasks[guild.id]

    cog._retire_board(draft_view)

    assert draft_view._board_retired is True
    assert game_id not in cog._boards
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_cog_unload_cancels_every_boards_pending_restick(sync_db_path):
    cog = _cog(sync_db_path)
    with cog.bot.ctx.open_db() as conn:
        set_board_sticky_enabled(conn, True, GUILD)
    game_a = await _seed_game(cog, CHAN_A)
    game_b = await _seed_game(cog, CHAN_B)
    view_a, view_b = _FakeDraftView(game_a), _FakeDraftView(game_b)
    guild = _guild()
    await cog._open_board(game_a, view_a, _channel(CHAN_A), guild, MagicMock(spec=discord.Message, id=1, edit=AsyncMock(), delete=AsyncMock()))
    await cog._open_board(game_b, view_b, _channel(CHAN_B), guild, MagicMock(spec=discord.Message, id=2, edit=AsyncMock(), delete=AsyncMock()))
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
    game_a = await _seed_game(cog, CHAN_A)
    game_b = await _seed_game(cog, CHAN_B)
    view_a, view_b = _FakeDraftView(game_a), _FakeDraftView(game_b)
    channel_a, channel_b = _channel(CHAN_A), _channel(CHAN_B)
    guild = _guild()

    msg_a = await cog._open_board(
        game_a, view_a, channel_a, guild,
        MagicMock(spec=discord.Message, id=1, edit=AsyncMock(), delete=AsyncMock()),
    )
    msg_b = await cog._open_board(
        game_b, view_b, channel_b, guild,
        MagicMock(spec=discord.Message, id=2, edit=AsyncMock(), delete=AsyncMock()),
    )

    assert cog._board_ids(game_a) == (CHAN_A, msg_a.id)
    assert cog._board_ids(game_b) == (CHAN_B, msg_b.id)

    # Reposting A must not touch B's stored ids or panel.
    panel_a = cog._boards[game_a]
    await panel_a.place(guild, channel_a)
    assert cog._board_ids(game_b) == (CHAN_B, msg_b.id)

    # Retiring A must not retire or drop B.
    cog._retire_board(view_a)
    assert game_a not in cog._boards
    assert game_b in cog._boards
    assert view_b._board_retired is False


# ── the loops retire the board on every exit path, not just natural end ────


@pytest.mark.asyncio
async def test_run_draft_loop_retires_board_on_forced_end(sync_db_path):
    cog = _cog(sync_db_path)
    with cog.bot.ctx.open_db() as conn:
        set_board_sticky_enabled(conn, True, GUILD)
    game_id = await _seed_game(cog, CHAN_A)
    draft_view = _FakeDraftView(game_id)
    channel = _channel(CHAN_A)
    guild = _guild()
    await cog._open_board(
        game_id, draft_view, channel, guild,
        MagicMock(spec=discord.Message, id=1, edit=AsyncMock(), delete=AsyncMock()),
    )
    assert game_id in cog._boards

    # A forced end (/games end) sets _closed before the loop is re-entered;
    # snake mode's while-loop needs these to reach that check without
    # touching anything else.
    draft_view._msg = MagicMock()
    draft_view._closed = True
    draft_view.current_pick_index = 0
    draft_view.draft_order = [(1, HOST)]

    await cog._run_draft_loop(draft_view, channel, guild, {})

    assert draft_view._board_retired is True
    assert game_id not in cog._boards


@pytest.mark.asyncio
async def test_run_blitz_loop_retires_board_on_forced_end(sync_db_path):
    cog = _cog(sync_db_path)
    with cog.bot.ctx.open_db() as conn:
        set_board_sticky_enabled(conn, True, GUILD)
    game_id = await _seed_game(cog, CHAN_A)
    draft_view = _FakeDraftView(game_id)
    channel = _channel(CHAN_A)
    guild = _guild()
    await cog._open_board(
        game_id, draft_view, channel, guild,
        MagicMock(spec=discord.Message, id=1, edit=AsyncMock(), delete=AsyncMock()),
    )
    assert game_id in cog._boards

    draft_view._msg = MagicMock()
    draft_view._closed = True

    await cog._run_blitz_loop(draft_view, channel, guild, {})

    assert draft_view._board_retired is True
    assert game_id not in cog._boards
