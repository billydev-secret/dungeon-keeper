"""Cog-level tests for the /guess setup command — channel/role validation."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests.fakes import FakeGuild, FakeMember, FakeRole, fake_interaction

GUESS_ROLE_ID = 7001
GUESS_CHANNEL_ID = 8001
GUILD_ID = 9001


def _make_cog(db_path: str = ":memory:"):
    from bot_modules.cogs.guess_cog import GuessCog
    bot = MagicMock()
    bot.ctx.db_path = db_path
    return GuessCog(bot)


def _channel(channel_id: int = GUESS_CHANNEL_ID, *, nsfw: bool = True) -> MagicMock:
    ch = MagicMock()
    ch.id = channel_id
    ch.is_nsfw = lambda: nsfw
    ch.mention = f"<#{channel_id}>"
    return ch


async def _setup(cog: Any, interaction: Any, channel: Any, role: Any) -> None:
    await cog.guess_setup.callback(cog, interaction, channel, role)


@pytest.mark.asyncio
async def test_setup_rejects_non_nsfw_channel():
    member = FakeMember(id=1001)
    guild = FakeGuild(id=GUILD_ID)
    guild.members[member.id] = member
    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID

    role = FakeRole(id=GUESS_ROLE_ID)
    cog = _make_cog()

    with patch("bot_modules.cogs.guess_cog._do_set_config") as set_cfg:
        await _setup(cog, interaction, _channel(nsfw=False), role)

    set_cfg.assert_not_called()
    msg = interaction.followup.send.call_args.args[0]
    assert "nsfw" in msg.lower() or "age" in msg.lower()


@pytest.mark.asyncio
async def test_setup_writes_both_channel_and_role():
    member = FakeMember(id=1001)
    guild = FakeGuild(id=GUILD_ID)
    guild.members[member.id] = member
    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID

    role = FakeRole(id=GUESS_ROLE_ID)
    cog = _make_cog()

    with patch("bot_modules.cogs.guess_cog._do_set_config") as set_cfg:
        await _setup(cog, interaction, _channel(nsfw=True), role)

    keys_written = {call.args[2] for call in set_cfg.call_args_list}
    assert "guess_channel_id" in keys_written
    assert "guess_role_id" in keys_written
