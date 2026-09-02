"""Tests for cogs/mod_stats_cog.py — the glue, not the numbers.

The arithmetic and the text block are covered in test_mod_stats_service.py and
the renderer in test_activity_graphs.py. What is left here is wiring that only
breaks in Discord: an embed whose image points somewhere the attachment isn't
renders as a blank grey box, and nothing else in the suite would notice.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.cogs.mod_stats_cog import CHANNEL_KEY, MESSAGE_KEY, ModStatsCog
from bot_modules.core.db_utils import open_db, set_config_value
from tests.db_template import migrated_db

GUILD = 123


class _FakeGuildConfig:
    def __init__(self, mod_role_ids=(), admin_role_ids=()):
        self.mod_role_ids = frozenset(mod_role_ids)
        self.admin_role_ids = frozenset(admin_role_ids)


class _FakeCtx:
    def __init__(self, db_path, guild_config=None):
        self.db_path = db_path
        self._guild_config = guild_config or _FakeGuildConfig()

    def open_db(self):
        return open_db(self.db_path)

    def guild_config(self, _guild_id):
        return self._guild_config


@pytest.fixture
def cog(tmp_path):
    db_path = migrated_db(tmp_path / "modstats.db")
    bot = MagicMock()
    bot.ctx = _FakeCtx(db_path)
    return ModStatsCog(bot)


@pytest.mark.asyncio
async def test_panel_embed_points_at_the_attachment_it_carries(cog):
    """An embed image must name the file travelling with the same message.

    ``attachment://…`` is resolved against that message's own attachments, so a
    filename mismatch here is a blank grey box in the mod channel and a green
    test suite everywhere else.
    """
    guild = MagicMock(spec=discord.Guild)
    guild.id = GUILD

    content = await cog.build_panel(guild)

    assert content.image is not None
    assert content.embed.image.url == f"attachment://{content.image.filename}"
    assert content.image.data.startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.asyncio
async def test_a_repost_reuses_the_rendered_chart(cog):
    """A busy mod channel re-sticks the panel every few seconds. Re-running
    matplotlib for a picture identical to the one already on screen would put
    that cost on the process-wide render lock every time."""
    guild = MagicMock(spec=discord.Guild)
    guild.id = GUILD

    first = await cog.build_panel(guild)
    second = await cog.build_panel(guild)

    assert first.image.data is second.image.data


def test_stored_ids_are_never_borrowed_from_another_guild(cog):
    """Panel ids are strictly guild-scoped. The config table's legacy
    ``guild_id=0`` fallback would otherwise hand a second server the home
    guild's message to edit — in the home guild's channel."""
    with cog.bot.ctx.open_db() as conn:
        set_config_value(conn, CHANNEL_KEY, "555", 0)
        set_config_value(conn, MESSAGE_KEY, "666", 0)

    assert cog._read_ids(GUILD) == (0, 0)


def test_only_guilds_with_a_channel_are_refreshed(cog):
    """Retiring a panel writes 0 rather than deleting the row, so a guild whose
    panel was deleted must not be read back as still having one."""
    with cog.bot.ctx.open_db() as conn:
        set_config_value(conn, CHANNEL_KEY, "555", GUILD)
        set_config_value(conn, CHANNEL_KEY, "0", 999)

    assert cog._panel_guilds() == {GUILD}


@pytest.mark.asyncio
async def test_a_deleted_panel_is_retired_rather_than_reposted(cog):
    """Deleting the message is the only remove gesture staff have — the shared
    post control carries no Remove button — so the hourly refresh must not put
    it straight back."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 555
    message = MagicMock(spec=discord.Message)
    message.edit = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "x"))
    channel.get_partial_message = MagicMock(return_value=message)
    channel.send = AsyncMock()

    guild = MagicMock(spec=discord.Guild)
    guild.id = GUILD
    guild.get_channel_or_thread.return_value = channel
    cog.bot.get_guild.return_value = guild

    with cog.bot.ctx.open_db() as conn:
        set_config_value(conn, CHANNEL_KEY, "555", GUILD)
        set_config_value(conn, MESSAGE_KEY, "666", GUILD)

    await cog.refresh_loop()

    channel.send.assert_not_called()
    assert cog._read_ids(GUILD) == (0, 0)


def test_mod_ids_union_the_mod_and_admin_roles_and_drop_bots(tmp_path):
    """The one piece of glue that is not service logic: who the panel treats as
    a moderator is read live off Discord, because ``role_events`` is an
    append-only log of grants and cannot say who holds a role *now*."""
    db_path = migrated_db(tmp_path / "modstats-roles.db")
    bot = MagicMock()
    bot.ctx = _FakeCtx(db_path, _FakeGuildConfig(mod_role_ids=[1], admin_role_ids=[2]))
    cog = ModStatsCog(bot)

    def _member(member_id, *, bot_account=False):
        member = MagicMock()
        member.id = member_id
        member.bot = bot_account
        return member

    roles = {
        1: MagicMock(members=[_member(10), _member(11)]),
        2: MagicMock(members=[_member(11), _member(12), _member(99, bot_account=True)]),
    }
    guild = MagicMock(spec=discord.Guild)
    guild.id = GUILD
    guild.get_role = roles.get

    # 11 holds both roles and is counted once; the bot account is dropped.
    assert cog._mod_ids(guild) == {10, 11, 12}


def test_mod_ids_is_empty_when_no_role_is_configured(tmp_path):
    db_path = migrated_db(tmp_path / "modstats-noroles.db")
    bot = MagicMock()
    bot.ctx = _FakeCtx(db_path)
    cog = ModStatsCog(bot)
    guild = MagicMock(spec=discord.Guild)
    guild.id = GUILD

    assert cog._mod_ids(guild) == set()
