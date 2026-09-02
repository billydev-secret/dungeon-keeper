"""The one branch in the confessions cog that mod-approve mode added.

Deliberately narrow (CLAUDE.md: cogs are glue, tested through the logic layer).
What is worth proving here is the fork itself, because the failure mode is the
feature's whole point: with approval on, a submission must *not* reach the
destination channel, and with it off nothing about today's behaviour may change.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.cogs.confessions_cog import ConfessModal
from bot_modules.core.db_utils import open_db
from bot_modules.services.confessions_service import (
    GuildConfig,
    pending_confessions,
    upsert_config,
)
from tests.db_template import migrated_db
from tests.fakes import FakeGuild, fake_interaction

GUILD_ID = 9001
AUTHOR = 424242
DEST = 777
BODY = "I ate the last biscuit"


@pytest.fixture
def db(tmp_path):
    return migrated_db(tmp_path / "test.db")


def _configure(db, *, require_approval: bool) -> None:
    upsert_config(
        db,
        GuildConfig(
            guild_id=GUILD_ID, dest_channel_id=DEST, log_channel_id=0,
            require_approval=require_approval,
        ),
    )


def _modal(db) -> tuple[ConfessModal, MagicMock]:
    bot = MagicMock()
    bot.ctx = SimpleNamespace(db_path=db, open_db=lambda: open_db(db))
    bot.todo_refresh = AsyncMock(return_value=True)
    bot.get_cog = MagicMock(
        return_value=SimpleNamespace(refresh_board=bot.todo_refresh)
    )
    cog = MagicMock()
    cog.bot = bot
    cog.publish_confession = AsyncMock(return_value=(True, ""))
    cog._safe_complete = AsyncMock()
    modal = ConfessModal(cog)
    modal.confession._value = BODY
    modal.notify_pref._value = "yes"
    return modal, cog


def _interaction() -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = DEST
    guild = FakeGuild(id=GUILD_ID, channels={DEST: channel})
    user = MagicMock(spec=discord.Member)
    user.id = AUTHOR
    return fake_interaction(guild=guild, user=user)


@pytest.mark.asyncio
async def test_approval_off_posts_immediately(db):
    _configure(db, require_approval=False)
    modal, cog = _modal(db)

    await modal.on_submit(_interaction())

    assert cog.publish_confession.await_args.kwargs["content"] == BODY
    with open_db(db) as conn:
        assert pending_confessions(conn, GUILD_ID) == []


@pytest.mark.asyncio
async def test_approval_on_queues_instead_of_posting(db):
    _configure(db, require_approval=True)
    modal, cog = _modal(db)
    interaction = _interaction()

    await modal.on_submit(interaction)

    assert cog.publish_confession.await_count == 0
    with open_db(db) as conn:
        rows = pending_confessions(conn, GUILD_ID)
    assert [r["content"] for r in rows] == [BODY]


@pytest.mark.asyncio
async def test_a_queued_member_is_told_it_is_waiting(db):
    """Not the usual silent success: a confession that just fails to appear is
    what makes people submit it again."""
    _configure(db, require_approval=True)
    modal, _ = _modal(db)
    interaction = _interaction()

    await modal.on_submit(interaction)

    msg = interaction.response.send_message.await_args.args[0]
    assert "review" in msg.lower()


@pytest.mark.asyncio
async def test_queueing_repaints_the_mods_board(db):
    _configure(db, require_approval=True)
    modal, cog = _modal(db)

    await modal.on_submit(_interaction())

    cog.bot.todo_refresh.assert_awaited_with(GUILD_ID)


@pytest.mark.asyncio
async def test_the_notify_preference_rides_along_into_the_queue(db):
    """It is set at submission and honoured at approval, so it has to survive
    the wait."""
    _configure(db, require_approval=True)
    modal, _ = _modal(db)
    modal.notify_pref._value = "no"

    await modal.on_submit(_interaction())

    with open_db(db) as conn:
        row = conn.execute(
            "SELECT notify_original_author FROM confession_pending"
        ).fetchone()
    assert row["notify_original_author"] == 0


@pytest.mark.asyncio
async def test_panic_mode_still_wins_over_the_queue(db):
    """The kill switch short-circuits ahead of the fork, so a paused server
    does not quietly accumulate a backlog."""
    upsert_config(
        db,
        GuildConfig(
            guild_id=GUILD_ID, dest_channel_id=DEST, log_channel_id=0,
            require_approval=True, panic=True,
        ),
    )
    modal, cog = _modal(db)

    await modal.on_submit(_interaction())

    assert cog.publish_confession.await_count == 0
    with open_db(db) as conn:
        assert pending_confessions(conn, GUILD_ID) == []


@pytest.mark.asyncio
async def test_a_missing_destination_channel_is_refused_not_queued(db):
    """Otherwise the queue accepts confessions it can never drain."""
    _configure(db, require_approval=True)
    modal, _ = _modal(db)
    interaction = fake_interaction(
        guild=FakeGuild(id=GUILD_ID), user=MagicMock(spec=discord.Member, id=AUTHOR)
    )

    await modal.on_submit(interaction)

    with open_db(db) as conn:
        assert pending_confessions(conn, GUILD_ID) == []


@pytest.mark.asyncio
async def test_a_blocked_member_cannot_reach_the_queue(db):
    upsert_config(
        db,
        GuildConfig(
            guild_id=GUILD_ID, dest_channel_id=DEST, log_channel_id=0,
            require_approval=True, blocked_user_ids=[AUTHOR],
        ),
    )
    modal, _ = _modal(db)

    await modal.on_submit(_interaction())

    with open_db(db) as conn:
        assert pending_confessions(conn, GUILD_ID) == []


@pytest.mark.asyncio
async def test_an_unreviewed_queue_is_swept_and_its_authors_told(db):
    """The seven-day sweep is the promise in the privacy notice, not
    housekeeping — so it runs on the cog's own cleanup loop."""
    from bot_modules.cogs.confessions_cog import ConfessionsCog
    from bot_modules.services.confessions_service import (
        PENDING_TTL_SECONDS,
        enqueue_confession,
        pending_confession_count,
    )
    import time

    pending_id = enqueue_confession(
        db, guild_id=GUILD_ID, author_id=AUTHOR, content=BODY,
        notify_original_author=1,
    )
    with open_db(db) as conn:
        conn.execute(
            "UPDATE confession_pending SET created_at = ? WHERE id = ?",
            (int(time.time()) - PENDING_TTL_SECONDS - 60, pending_id),
        )

    bot = MagicMock()
    bot.ctx = SimpleNamespace(db_path=db, open_db=lambda: open_db(db))
    bot.get_guild = MagicMock(return_value=FakeGuild(id=GUILD_ID))
    bot.get_cog = MagicMock(
        return_value=SimpleNamespace(refresh_board=AsyncMock(return_value=True))
    )
    cog = ConfessionsCog.__new__(ConfessionsCog)
    cog.bot = bot

    with patch(
        "bot_modules.cogs.confessions_cog.notify_confession_expired", new=AsyncMock()
    ) as told:
        await cog._sweep_expired_pending()

    told.assert_awaited_once()
    assert told.await_args.args[2] == AUTHOR
    with open_db(db) as conn:
        assert pending_confession_count(conn, GUILD_ID) == 0
