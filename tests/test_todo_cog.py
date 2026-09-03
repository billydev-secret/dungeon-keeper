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
from bot_modules.services.economy_quests_service import (
    claim_quest,
    create_quest,
    set_quest_active,
)
from bot_modules.services.economy_service import (
    apply_credit,
    load_econ_settings,
    save_econ_settings,
)
from bot_modules.services.economy_theme_service import deny as deny_theme
from bot_modules.services.economy_theme_service import submit_theme
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


# ── the refresh loop's boot pass ──────────────────────────────────────


def _loop_bot(cog):
    bot = MagicMock()
    bot.ctx = MagicMock()
    bot.wait_until_ready = AsyncMock()
    bot.is_closed = MagicMock(side_effect=[False, False, True])
    bot.get_cog = MagicMock(return_value=cog)
    return bot


def _loop_cog():
    cog = MagicMock()
    cog.board.take_retries = MagicMock(return_value=set())
    cog.board.set_known_guilds = MagicMock()
    cog.refresh_board = AsyncMock(return_value=True)
    return cog


@pytest.mark.asyncio
async def test_the_first_tick_repaints_every_board_even_with_no_changes():
    """A board posted by a previous release can carry a view this one no longer
    registers — after the 180 merge the surviving message is the old chore
    board's, whose button would answer "This interaction failed" until
    something repainted it. An edit replaces the view with the content."""
    from bot_modules.cogs import todo_cog as mod

    cog = _loop_cog()
    bot = _loop_bot(cog)
    with patch.object(mod, "_tick", return_value=({7}, set())), \
         patch.object(mod.asyncio, "sleep", new=AsyncMock()):
        await mod.todo_board_loop(bot)

    assert cog.refresh_board.await_args_list[0].args == (7,)


@pytest.mark.asyncio
async def test_later_ticks_only_repaint_what_changed():
    """The boot pass must not become a per-minute repaint of every guild."""
    from bot_modules.cogs import todo_cog as mod

    cog = _loop_cog()
    bot = _loop_bot(cog)
    with patch.object(mod, "_tick", return_value=({7}, set())), \
         patch.object(mod.asyncio, "sleep", new=AsyncMock()):
        await mod.todo_board_loop(bot)

    # Two iterations ran; only the first had work.
    assert cog.refresh_board.await_count == 1


# ── quest sign-offs on the board ──────────────────────────────────────


def _pending_claim(db_path, *, title="Post a selfie", reward=500, user_id=777) -> int:
    """A claim waiting on a mod, in the guild the board fixtures use."""
    with open_db(db_path) as conn:
        save_econ_settings(conn, 123, {"enabled": True, "currency_emoji": "🪙"})
        qid = create_quest(
            conn, 123, title=title, description="", qtype="weekly", reward=reward,
            signoff=1, criteria="Show a screenshot", starts_at=None, ends_at=None,
            rotate_tag="", community_target=None, created_by=1,
        )
        set_quest_active(conn, 123, qid, True)
        settings = load_econ_settings(conn, 123)
        out = claim_quest(
            conn, settings, 123, qid, user_id, period="2026-W28", booster=False
        )
    return out.claim_id


@pytest.mark.asyncio
async def test_the_board_shows_who_is_waiting_on_a_sign_off(board_db):
    channel, _ = _fake_channel()
    guild = _fake_guild(channel)
    claimant = MagicMock()
    claimant.display_name = "Alex"
    guild.get_member = MagicMock(return_value=claimant)
    cog = _board_cog(board_db, guild)
    _pending_claim(board_db)
    with open_db(board_db) as conn:
        create_todo(conn, 123, 42, "Post QOTD")

    with patch("bot_modules.core.branding.resolve_accent_color",
               new=AsyncMock(return_value=discord.Color.blurple())):
        await cog.place_board(guild, channel)

    embed = channel.send.await_args.kwargs["embed"]
    assert "Alex" in embed.description
    assert "Post a selfie" in embed.description
    assert "🪙 500" in embed.description  # the guild's own currency emoji
    assert "Post QOTD" in embed.description  # the task list is still there
    assert "1 sign-off waiting" in embed.footer.text


@pytest.mark.asyncio
async def test_a_resolved_claim_leaves_the_board(board_db):
    """Only pending claims are read, so resolving one is what removes it —
    there is no mirrored todo row to clean up."""
    channel, _ = _fake_channel()
    guild = _fake_guild(channel)
    cog = _board_cog(board_db, guild)
    claim_id = _pending_claim(board_db)
    with open_db(board_db) as conn:
        conn.execute(
            "UPDATE econ_quest_claims SET state = 'denied' WHERE id = ?", (claim_id,)
        )

    with patch("bot_modules.core.branding.resolve_accent_color",
               new=AsyncMock(return_value=discord.Color.blurple())):
        await cog.place_board(guild, channel)

    embed = channel.send.await_args.kwargs["embed"]
    assert "Post a selfie" not in embed.description
    assert "sign-off" not in embed.footer.text


@pytest.mark.asyncio
async def test_a_sign_off_is_never_offered_to_the_complete_button(board_db):
    """A claim is approved, not ticked off. It has no todo row, so the picker
    that lists todos can't reach it."""
    channel, _ = _fake_channel()
    guild = _fake_guild(channel)
    cog = _board_cog(board_db, guild)
    _pending_claim(board_db)
    interaction = _interaction(_member(mod=True))
    interaction.guild = guild
    interaction.client = MagicMock()
    interaction.client.get_cog = MagicMock(return_value=cog)

    await TodoBoardView()._complete.callback(interaction)

    msg = interaction.response.send_message.await_args.args[0]
    assert "already clear" in msg  # the claim is not on offer


@pytest.mark.asyncio
async def test_the_signoffs_button_hands_off_to_the_economy(board_db):
    """The button is only a door: the gate and the flow behind it are the
    economy's, because approving pays real currency."""
    channel, _ = _fake_channel()
    cog = _board_cog(board_db, _fake_guild(channel))
    interaction = _interaction(_member(mod=False))
    interaction.client = MagicMock()
    interaction.client.get_cog = MagicMock(return_value=cog)

    with patch(
        "bot_modules.cogs.todo_cog.open_signoff_picker", new=AsyncMock()
    ) as picker:
        await TodoBoardView()._signoffs.callback(interaction)

    picker.assert_awaited_once_with(interaction)


# ── paid requests on the board ────────────────────────────────────────


def _pending_theme(db_path, *, title="Cursed Cooking", price=300, user_id=778) -> int:
    """A paid themed day waiting on a mod, in the guild the board fixtures use."""
    with open_db(db_path) as conn:
        save_econ_settings(
            conn,
            123,
            {
                "enabled": True,
                "currency_emoji": "🪙",
                "flash_theme_enabled": True,
                "price_flash_theme": price,
                "theme_channel_id": 6666,
            },
        )
        apply_credit(conn, 123, user_id, price * 2, "grant", actor_id=1)
        settings = load_econ_settings(conn, 123)
        return submit_theme(
            conn, settings, 123, user_id, title, "The Idea"
        ).submission_id


@pytest.mark.asyncio
async def test_the_board_shows_who_is_waiting_on_a_paid_request(board_db):
    """The bug: this card used to be posted publicly to the bank channel,
    which in the main guild is a member-facing explainer."""
    channel, _ = _fake_channel()
    guild = _fake_guild(channel)
    requester = MagicMock()
    requester.display_name = "Alex"
    guild.get_member = MagicMock(return_value=requester)
    cog = _board_cog(board_db, guild)
    _pending_theme(board_db)

    with patch("bot_modules.core.branding.resolve_accent_color",
               new=AsyncMock(return_value=discord.Color.blurple())):
        await cog.place_board(guild, channel)

    embed = channel.send.await_args.kwargs["embed"]
    assert "Alex" in embed.description
    assert "Cursed Cooking" in embed.description
    assert "🪙 300" in embed.description  # the guild's own currency emoji
    assert "1 paid request waiting" in embed.footer.text


@pytest.mark.asyncio
async def test_a_resolved_paid_request_leaves_the_board(board_db):
    """Only pending rows are read, so resolving one is what removes it —
    there is no mirrored todo row to clean up."""
    channel, _ = _fake_channel()
    guild = _fake_guild(channel)
    cog = _board_cog(board_db, guild)
    submission_id = _pending_theme(board_db)
    with open_db(board_db) as conn:
        deny_theme(conn, submission_id, resolver_id=1, deny_reason="too close")

    with patch("bot_modules.core.branding.resolve_accent_color",
               new=AsyncMock(return_value=discord.Color.blurple())):
        await cog.place_board(guild, channel)

    embed = channel.send.await_args.kwargs["embed"]
    assert "Cursed Cooking" not in embed.description
    assert "paid request" not in embed.footer.text


@pytest.mark.asyncio
async def test_a_paid_request_is_never_offered_to_the_complete_button(board_db):
    """A request is approved, not ticked off. It has no todo row, so the
    picker that lists todos can't reach it."""
    channel, _ = _fake_channel()
    guild = _fake_guild(channel)
    cog = _board_cog(board_db, guild)
    _pending_theme(board_db)
    interaction = _interaction(_member(mod=True))
    interaction.guild = guild
    interaction.client = MagicMock()
    interaction.client.get_cog = MagicMock(return_value=cog)

    await TodoBoardView()._complete.callback(interaction)

    msg = interaction.response.send_message.await_args.args[0]
    assert "already clear" in msg


@pytest.mark.asyncio
async def test_the_approvals_button_hands_off_to_the_economy(board_db):
    """Same as Sign-Offs: the button is only a door, because every decision
    behind it moves currency and a denial refunds it."""
    channel, _ = _fake_channel()
    cog = _board_cog(board_db, _fake_guild(channel))
    interaction = _interaction(_member(mod=False))
    interaction.client = MagicMock()
    interaction.client.get_cog = MagicMock(return_value=cog)

    with patch(
        "bot_modules.cogs.todo_cog.open_approvals_picker", new=AsyncMock()
    ) as picker:
        await TodoBoardView()._approvals.callback(interaction)

    picker.assert_awaited_once_with(interaction)


@pytest.mark.asyncio
async def test_the_confessions_button_hands_off_to_the_picker(board_db):
    """A door, like the other two — but the gate behind it is the board's own
    moderator check, applied inside the picker, since no currency moves."""
    channel, _ = _fake_channel()
    cog = _board_cog(board_db, _fake_guild(channel))
    interaction = _interaction(_member(mod=False))
    interaction.client = MagicMock()
    interaction.client.get_cog = MagicMock(return_value=cog)

    with patch(
        "bot_modules.cogs.todo_cog.open_confessions_picker", new=AsyncMock()
    ) as picker:
        await TodoBoardView()._confessions.callback(interaction)

    picker.assert_awaited_once_with(interaction)


def test_the_board_fits_discords_five_buttons_to_a_row():
    """Confessions is the fifth and last button the board can carry. A sixth
    would silently wrap onto a second action row."""
    assert len(TodoBoardView().children) == 5
