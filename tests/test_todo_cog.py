"""Tests for cogs/todo_cog.py — the /todo mod gate and the sticky board glue."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.cogs.todo_cog import (
    TodoAddModal,
    TodoBoardView,
    TodoCog,
)
from bot_modules.core.db_utils import open_db
from bot_modules.services.todo_service import (
    create_todo,
    get_board,
    save_board,
)
from tests.db_template import migrated_db


def _member(*, mod: bool) -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.id = 42
    m.guild_permissions = MagicMock(
        administrator=mod, manage_guild=False, manage_channels=False
    )
    return m


def _interaction(user: MagicMock) -> MagicMock:
    i = MagicMock(spec=discord.Interaction)
    i.user = user
    i.guild = MagicMock(id=123)
    i.response = MagicMock()
    i.response.send_message = AsyncMock()
    return i


def _cog() -> TodoCog:
    _bot = MagicMock()
    _bot.ctx = MagicMock()
    _bot.ctx.is_mod = lambda ix: bool(
        getattr(ix.user.guild_permissions, "administrator", False)
    )
    return TodoCog(_bot)


@pytest.mark.asyncio
async def test_todo_rejects_non_mods():
    """A non-mod can't add to the list — the gate short-circuits before any write."""
    cog = _cog()
    interaction = _interaction(_member(mod=False))
    with patch("bot_modules.cogs.todo_cog.create_todo") as create:
        await cog.todo.callback(cog, interaction, "clean up the channels")
    create.assert_not_called()
    msg = interaction.response.send_message.await_args.args[0]
    assert "moderator" in msg.lower()


@pytest.mark.asyncio
async def test_mod_gate_delegates_to_app_context():
    """The one wiring assertion: the gate asks ctx.is_mod, nothing else.

    It used to ask the *games* cogs' has_mod_or_admin_permissions, which knows
    nothing about the guild's configured mod role and refused real moderators
    the dashboard let in. Pinning the delegation here keeps the rule in one
    place; what that rule decides is tests/test_todo_mod_tier_parity.py.
    """
    cog = _cog()
    cog.bot.ctx.is_mod = MagicMock(return_value=False)
    interaction = _interaction(_member(mod=True))  # elevated bits, not on the team
    with patch("bot_modules.cogs.todo_cog.create_todo") as create:
        await cog.todo.callback(cog, interaction, "clean up the channels")
    cog.bot.ctx.is_mod.assert_called_once_with(interaction)
    create.assert_not_called()


@pytest.mark.asyncio
async def test_todo_allows_mods():
    """A mod's task reaches the service layer."""
    cog = _cog()
    interaction = _interaction(_member(mod=True))
    with patch("bot_modules.cogs.todo_cog.create_todo", return_value=7) as create:
        await cog.todo.callback(cog, interaction, "clean up the channels")
    create.assert_called_once()
    assert "#7" in interaction.response.send_message.await_args.args[0]


# ── board buttons: the moderator gate ────────────────────────────────


def _button_interaction(user: MagicMock) -> MagicMock:
    i = _interaction(user)
    i.response.send_modal = AsyncMock()
    i.client = MagicMock()
    return i


@pytest.mark.asyncio
async def test_add_button_rejects_non_mods():
    """The board is public in its channel — the Add button must gate on mod."""
    view = TodoBoardView()
    interaction = _button_interaction(_member(mod=False))
    with patch("bot_modules.cogs.todo_cog._resolve_cog", return_value=_cog()):
        await view._add.callback(interaction)
    interaction.response.send_modal.assert_not_awaited()
    assert "moderator" in interaction.response.send_message.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_add_button_opens_modal_for_mods():
    view = TodoBoardView()
    interaction = _button_interaction(_member(mod=True))
    with patch("bot_modules.cogs.todo_cog._resolve_cog", return_value=_cog()):
        await view._add.callback(interaction)
    interaction.response.send_modal.assert_awaited_once()
    assert isinstance(
        interaction.response.send_modal.await_args.args[0], TodoAddModal
    )


@pytest.mark.asyncio
async def test_complete_button_rejects_non_mods():
    view = TodoBoardView()
    interaction = _button_interaction(_member(mod=False))
    with patch("bot_modules.cogs.todo_cog._resolve_cog", return_value=_cog()), \
         patch("bot_modules.cogs.todo_cog.pending_todos") as pending:
        await view._complete.callback(interaction)
    pending.assert_not_called()
    assert "moderator" in interaction.response.send_message.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_add_modal_rejects_blank_task():
    """Discord lets a required field through as whitespace; re-check server-side."""
    modal = TodoAddModal()
    modal.task._value = "   "
    modal.notes._value = ""
    interaction = _button_interaction(_member(mod=True))
    cog = _cog()
    with patch("bot_modules.cogs.todo_cog._resolve_cog", return_value=cog), \
         patch("bot_modules.cogs.todo_cog.create_todo") as create:
        await modal.on_submit(interaction)
    create.assert_not_called()
    assert "empty" in interaction.response.send_message.await_args.args[0].lower()


# ── board refresh ────────────────────────────────────────────────────


class _FakeCtx:
    def __init__(self, db_path):
        self.db_path = db_path

    def open_db(self):
        return open_db(self.db_path)

    def is_mod(self, interaction):
        """Stand in for AppContext.is_mod, which the todo gate delegates to.

        The real rule (elevated bits, then the guild's configured mod/admin
        roles) and its agreement with the dashboard are pinned in
        tests/test_todo_mod_tier_parity.py. Here it only has to answer for the
        ``_member(mod=...)`` fakes, so the cog tests stay glue tests.
        """
        return bool(getattr(interaction.user.guild_permissions, "administrator", False))


def _board_cog(db_path, guild):
    bot = MagicMock()
    bot.get_guild.return_value = guild
    bot.ctx = _FakeCtx(db_path)
    cog = TodoCog(bot)
    return cog


def _fake_guild(channel):
    guild = MagicMock(spec=discord.Guild)
    guild.id = 123
    guild.get_channel.return_value = channel
    # core.sticky resolves panel ids through get_channel_or_thread so a panel
    # can opt into threads (see target_types).
    guild.get_channel_or_thread.return_value = channel
    guild.me = None
    return guild


def _fake_channel():
    """A channel whose partial-message handle is the same mock `send` returns,
    so a test can assert on edits and deletes through one object."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 555
    message = MagicMock(spec=discord.Message)
    message.id = 666
    message.edit = AsyncMock()
    message.delete = AsyncMock()
    channel.get_partial_message = MagicMock(return_value=message)
    channel.send = AsyncMock(return_value=message)
    return channel, message


@pytest.fixture
def board_db(tmp_path):
    return migrated_db(tmp_path / "board.db")


@pytest.mark.asyncio
async def test_refresh_noop_without_a_posted_board(board_db):
    channel, _ = _fake_channel()
    cog = _board_cog(board_db, _fake_guild(channel))
    assert await cog.refresh_board(123) is False
    channel.get_partial_message.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_edits_then_skips_when_unchanged(board_db):
    """An unchanged board must cost no API call — ages tick client-side."""
    channel, message = _fake_channel()
    cog = _board_cog(board_db, _fake_guild(channel))
    with open_db(board_db) as conn:
        save_board(conn, 123, 555, 666)
        create_todo(conn, 123, 42, "Post QOTD")

    with patch("bot_modules.core.branding.resolve_accent_color",
               new=AsyncMock(return_value=discord.Color.blurple())):
        assert await cog.refresh_board(123) is True
        assert message.edit.await_count == 1
        assert await cog.refresh_board(123) is False
        assert message.edit.await_count == 1

        # A new task changes the signature, so the next refresh does edit.
        with open_db(board_db) as conn:
            create_todo(conn, 123, 42, "Another task")
        assert await cog.refresh_board(123) is True
        assert message.edit.await_count == 2


@pytest.mark.asyncio
async def test_refresh_reposts_when_the_board_was_deleted(board_db):
    """A board deleted by hand heals itself instead of going quietly dead."""
    channel, _ = _fake_channel()
    gone = MagicMock()
    gone.edit = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    # The self-heal path reposts, which also deletes the (already gone) old one.
    gone.delete = AsyncMock()
    channel.get_partial_message = MagicMock(return_value=gone)
    cog = _board_cog(board_db, _fake_guild(channel))
    with open_db(board_db) as conn:
        save_board(conn, 123, 555, 666)

    with patch("bot_modules.core.branding.resolve_accent_color",
               new=AsyncMock(return_value=discord.Color.blurple())):
        assert await cog.refresh_board(123) is True
    channel.send.assert_awaited_once()
    with open_db(board_db) as conn:
        assert get_board(conn, 123).message_id == 666


@pytest.mark.asyncio
async def test_place_board_persists_ids_and_renders_tasks(board_db):
    channel, message = _fake_channel()
    guild = _fake_guild(channel)
    cog = _board_cog(board_db, guild)
    with open_db(board_db) as conn:
        create_todo(conn, 123, 42, "Post QOTD")

    with patch("bot_modules.core.branding.resolve_accent_color",
               new=AsyncMock(return_value=discord.Color.blurple())):
        assert await cog.place_board(guild, channel) is message

    embed = channel.send.await_args.kwargs["embed"]
    assert "Post QOTD" in embed.description
    with open_db(board_db) as conn:
        board = get_board(conn, 123)
    assert (board.channel_id, board.message_id) == (555, 666)


@pytest.mark.asyncio
async def test_place_board_deletes_the_previous_one(board_db):
    """Delete-and-repost is how the board stays last; the old one must go."""
    channel, message = _fake_channel()
    guild = _fake_guild(channel)
    cog = _board_cog(board_db, guild)
    with open_db(board_db) as conn:
        save_board(conn, 123, 555, 111)

    with patch("bot_modules.core.branding.resolve_accent_color",
               new=AsyncMock(return_value=discord.Color.blurple())):
        await cog.place_board(guild, channel)
    channel.get_partial_message.assert_called_once_with(111)
    message.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_unpost_board_clears_placement(board_db):
    channel, message = _fake_channel()
    guild = _fake_guild(channel)
    cog = _board_cog(board_db, guild)
    with open_db(board_db) as conn:
        save_board(conn, 123, 555, 666)

    assert await cog.unpost_board(guild) is True
    message.delete.assert_awaited_once()
    with open_db(board_db) as conn:
        assert not get_board(conn, 123).posted


@pytest.mark.asyncio
async def test_restick_ignores_bot_messages(board_db):
    """Re-sticking under our own repost would self-loop forever."""
    channel, _ = _fake_channel()
    cog = _board_cog(board_db, _fake_guild(channel))
    msg = MagicMock(spec=discord.Message)
    msg.guild = MagicMock(id=123)
    msg.author = MagicMock(bot=True)
    with patch.object(cog.board, "schedule_restick") as sched:
        await cog._restick_board(msg)
    sched.assert_not_called()


@pytest.mark.asyncio
async def test_restick_arms_on_member_message_in_board_channel(board_db):
    channel, _ = _fake_channel()
    cog = _board_cog(board_db, _fake_guild(channel))
    with open_db(board_db) as conn:
        save_board(conn, 123, 555, 666)

    msg = MagicMock(spec=discord.Message)
    msg.guild = MagicMock(id=123)
    msg.author = MagicMock(bot=False)
    msg.channel = MagicMock(id=555)
    msg.id = 777
    with patch.object(cog.board, "schedule_restick") as sched:
        await cog._restick_board(msg)
    sched.assert_called_once_with(123)


@pytest.mark.asyncio
async def test_restick_ignores_other_channels(board_db):
    channel, _ = _fake_channel()
    cog = _board_cog(board_db, _fake_guild(channel))
    with open_db(board_db) as conn:
        save_board(conn, 123, 555, 666)

    msg = MagicMock(spec=discord.Message)
    msg.guild = MagicMock(id=123)
    msg.author = MagicMock(bot=False)
    msg.channel = MagicMock(id=999)
    msg.id = 777
    with patch.object(cog.board, "schedule_restick") as sched:
        await cog._restick_board(msg)
    sched.assert_not_called()


@pytest.mark.asyncio
async def test_place_board_keeps_the_old_one_when_posting_fails(board_db):
    """Regression: the old board was deleted before the new post was attempted,
    so moving to a channel the bot can't post in destroyed a working board and
    left the DB pointing at a dead message."""
    channel, _ = _fake_channel()
    old = MagicMock(spec=discord.Message)
    old.delete = AsyncMock()
    channel.fetch_message = AsyncMock(return_value=old)
    channel.send = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "no perms")
    )
    guild = _fake_guild(channel)
    cog = _board_cog(board_db, guild)
    with open_db(board_db) as conn:
        save_board(conn, 123, 555, 111)

    with patch("bot_modules.core.branding.resolve_accent_color",
               new=AsyncMock(return_value=discord.Color.blurple())):
        assert await cog.place_board(guild, channel) is None

    old.delete.assert_not_awaited()
    with open_db(board_db) as conn:
        board = get_board(conn, 123)
    assert (board.channel_id, board.message_id) == (555, 111)


@pytest.mark.asyncio
async def test_place_board_survives_non_forbidden_errors(board_db):
    """Only Forbidden was caught, so a rate-limit or 50035 escaped the cog and
    surfaced as an unhandled 500 from the dashboard route."""
    channel, _ = _fake_channel()
    channel.send = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=500), "boom")
    )
    guild = _fake_guild(channel)
    cog = _board_cog(board_db, guild)

    with patch("bot_modules.core.branding.resolve_accent_color",
               new=AsyncMock(return_value=discord.Color.blurple())):
        assert await cog.place_board(guild, channel) is None


@pytest.mark.asyncio
async def test_todo_answers_the_interaction_before_repainting():
    """A repaint is a REST edit that can block on a rate limit for longer than
    Discord's three-second interaction window — respond first."""
    cog = _cog()
    interaction = _interaction(_member(mod=True))
    order: list[str] = []
    interaction.response.send_message = AsyncMock(
        side_effect=lambda *a, **k: order.append("reply")
    )
    with patch("bot_modules.cogs.todo_cog.create_todo", return_value=7), \
         patch.object(cog, "refresh_board",
                      new=AsyncMock(side_effect=lambda _g: order.append("repaint"))):
        await cog.todo.callback(cog, interaction, "clean up the channels")
    assert order == ["reply", "repaint"]


# ── the one board (glue only — rendering is covered in board_logic) ──


@pytest.mark.asyncio
async def test_the_board_keeps_one_placement(board_db):
    """Migration 180 collapsed the two placements into one row per guild."""
    channel, _ = _fake_channel()
    cog = _board_cog(board_db, _fake_guild(channel))

    cog._write_ids(123, 111, 222)
    assert cog._read_ids(123) == (111, 222)
    with open_db(board_db) as conn:
        assert get_board(conn, 123).channel_id == 111


@pytest.mark.asyncio
async def test_on_message_is_forwarded_to_the_panel(board_db):
    """A miss here leaves the board unable to re-stick at all."""
    channel, _ = _fake_channel()
    cog = _board_cog(board_db, _fake_guild(channel))
    cog.board.on_message = AsyncMock()

    message = MagicMock(spec=discord.Message)
    await cog._restick_board(message)

    cog.board.on_message.assert_awaited_once_with(message)


@pytest.mark.asyncio
async def test_channel_delete_is_forwarded_to_the_panel(board_db):
    channel, _ = _fake_channel()
    cog = _board_cog(board_db, _fake_guild(channel))
    cog.board.on_channel_delete = AsyncMock()

    deleted = MagicMock(spec=discord.TextChannel)
    await cog._forget_deleted_board_channel(deleted)

    cog.board.on_channel_delete.assert_awaited_once_with(deleted)


@pytest.mark.asyncio
async def test_complete_sees_every_chore_the_board_renders(board_db):
    """The button read a shorter slice than the board drew, so a guild whose
    first 25 chores were all done got "everything is ticked off" while open
    rows were visible in the message directly above it."""
    from bot_modules.services.todo_recurring_service import create_recurring, run_now
    from bot_modules.services.todo_service import complete_todo

    with open_db(board_db) as conn:
        for n in range(30):
            rid = create_recurring(
                conn, 123, task=f"Chore {n:02d}", recurrence="daily",
                time_of_day=n, created_by=1, now_ts=0.0,
            )
            run_now(conn, rid, 123, now_ts=0.0)
        # Tick off the first 25 by time_of_day — the board's own ordering.
        for row in conn.execute(
            "SELECT id FROM todos ORDER BY id LIMIT 25"
        ).fetchall():
            complete_todo(conn, row["id"], 123, 42)

    channel, _ = _fake_channel()
    cog = _board_cog(board_db, _fake_guild(channel))
    view = TodoBoardView()
    interaction = _button_interaction(_member(mod=True))
    with patch("bot_modules.cogs.todo_cog._resolve_cog", return_value=cog):
        await view._complete.callback(interaction)

    # A picker, not "everything is done".
    kwargs = interaction.response.send_message.await_args.kwargs
    assert "What did you finish" in interaction.response.send_message.await_args.args[0]
    assert len(kwargs["view"].children[0].options) == 5


@pytest.mark.asyncio
async def test_complete_offers_chores_and_tasks_together(board_db):
    """One button over both sections — a mod no longer has to know which list
    a row was on before they can tick it."""
    from bot_modules.services.todo_recurring_service import create_recurring, run_now
    from bot_modules.services.todo_service import create_todo as _create

    with open_db(board_db) as conn:
        rid = create_recurring(
            conn, 123, task="Do a QOTD", recurrence="daily",
            time_of_day=0, created_by=1, now_ts=0.0,
        )
        run_now(conn, rid, 123, now_ts=0.0)
        _create(conn, 123, 42, "fix the quote bot", now_ts=1.0)

    channel, _ = _fake_channel()
    cog = _board_cog(board_db, _fake_guild(channel))
    view = TodoBoardView()
    interaction = _button_interaction(_member(mod=True))
    with patch("bot_modules.cogs.todo_cog._resolve_cog", return_value=cog):
        await view._complete.callback(interaction)

    options = interaction.response.send_message.await_args.kwargs["view"].children[0].options
    labels = [o.label for o in options]
    assert any("Do a QOTD" in label for label in labels)
    assert any("fix the quote bot" in label for label in labels)
    # Chores first: they are what the board lists first.
    assert "Do a QOTD" in labels[0]
