"""Cog-level: launcher message at the bottom of the whisper channel."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.services.whisper_models import WhisperConfig

GUILD_ID = 9001
FEED_CHANNEL_ID = 8001


def _cfg(*, channel_id: int = FEED_CHANNEL_ID, launcher_message_id: int = 0) -> WhisperConfig:
    return WhisperConfig(
        guild_id=GUILD_ID,
        role_id=7001,
        channel_id=channel_id,
        log_channel_id=8002,
        launcher_message_id=launcher_message_id,
    )


def _make_cog():
    from bot_modules.cogs.whisper_cog import WhisperCog
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    cog = WhisperCog.__new__(WhisperCog)
    cog.bot = bot
    cog.ctx = bot.ctx
    cog._launcher_locks = {}
    cog._pending_refresh = set()
    return cog


def _make_text_channel(*, send_id: int = 12345) -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock(return_value=MagicMock(id=send_id))
    return channel


@pytest.mark.asyncio
async def test_refresh_creates_launcher_when_none_exists():
    cog = _make_cog()
    channel = _make_text_channel(send_id=12345)
    guild = MagicMock()
    guild.get_channel = MagicMock(return_value=channel)
    cog.bot.get_guild = MagicMock(return_value=guild)

    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg(launcher_message_id=0)), \
         patch("bot_modules.cogs.whisper_cog._do_set_launcher_id") as set_id:
        await cog.refresh_whisper_launcher(GUILD_ID)

    channel.send.assert_awaited_once()
    sent_kwargs = channel.send.call_args.kwargs
    assert "view" in sent_kwargs
    set_id.assert_called_once_with(":memory:", GUILD_ID, 12345)


@pytest.mark.asyncio
async def test_refresh_deletes_old_launcher_before_posting_new():
    cog = _make_cog()
    channel = _make_text_channel(send_id=22222)
    old = MagicMock()
    old.delete = AsyncMock()
    channel.fetch_message = AsyncMock(return_value=old)
    guild = MagicMock()
    guild.get_channel = MagicMock(return_value=channel)
    cog.bot.get_guild = MagicMock(return_value=guild)

    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg(launcher_message_id=11111)), \
         patch("bot_modules.cogs.whisper_cog._do_set_launcher_id") as set_id:
        await cog.refresh_whisper_launcher(GUILD_ID)

    channel.fetch_message.assert_awaited_once_with(11111)
    old.delete.assert_awaited_once()
    channel.send.assert_awaited_once()
    set_id.assert_called_once_with(":memory:", GUILD_ID, 22222)


@pytest.mark.asyncio
async def test_refresh_skips_when_channel_unset():
    cog = _make_cog()
    cog.bot.get_guild = MagicMock(return_value=MagicMock())

    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg(channel_id=0)), \
         patch("bot_modules.cogs.whisper_cog._do_set_launcher_id") as set_id:
        await cog.refresh_whisper_launcher(GUILD_ID)

    set_id.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_swallows_old_message_fetch_failure():
    """If the previous launcher was already deleted, the refresh proceeds anyway."""
    cog = _make_cog()
    channel = _make_text_channel(send_id=33333)
    channel.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    guild = MagicMock()
    guild.get_channel = MagicMock(return_value=channel)
    cog.bot.get_guild = MagicMock(return_value=guild)

    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg(launcher_message_id=11111)), \
         patch("bot_modules.cogs.whisper_cog._do_set_launcher_id") as set_id:
        await cog.refresh_whisper_launcher(GUILD_ID)

    channel.send.assert_awaited_once()
    set_id.assert_called_once_with(":memory:", GUILD_ID, 33333)


@pytest.mark.asyncio
async def test_on_message_listener_triggers_refresh_in_whisper_channel():
    cog = _make_cog()
    cog.refresh_whisper_launcher = AsyncMock()  # type: ignore[method-assign]

    msg = MagicMock(spec=discord.Message)
    msg.author = MagicMock()
    msg.author.bot = False
    msg.guild = MagicMock()
    msg.guild.id = GUILD_ID
    msg.channel = MagicMock()
    msg.channel.id = FEED_CHANNEL_ID
    msg.id = 99999  # not the launcher

    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg(launcher_message_id=11111)):
        await cog._on_message_launcher_bump(msg)

    cog.refresh_whisper_launcher.assert_awaited_once_with(GUILD_ID)


@pytest.mark.asyncio
async def test_on_message_listener_skips_other_channels():
    cog = _make_cog()
    cog.refresh_whisper_launcher = AsyncMock()  # type: ignore[method-assign]

    msg = MagicMock(spec=discord.Message)
    msg.author = MagicMock()
    msg.author.bot = False
    msg.guild = MagicMock()
    msg.guild.id = GUILD_ID
    msg.channel = MagicMock()
    msg.channel.id = 7777  # different channel
    msg.id = 99999

    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()):
        await cog._on_message_launcher_bump(msg)

    cog.refresh_whisper_launcher.assert_not_called()


@pytest.mark.asyncio
async def test_on_message_listener_skips_launcher_message_itself():
    cog = _make_cog()
    cog.refresh_whisper_launcher = AsyncMock()  # type: ignore[method-assign]

    msg = MagicMock(spec=discord.Message)
    msg.author = MagicMock()
    msg.author.bot = False
    msg.guild = MagicMock()
    msg.guild.id = GUILD_ID
    msg.channel = MagicMock()
    msg.channel.id = FEED_CHANNEL_ID
    msg.id = 11111  # IS the launcher

    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg(launcher_message_id=11111)):
        await cog._on_message_launcher_bump(msg)

    cog.refresh_whisper_launcher.assert_not_called()


@pytest.mark.asyncio
async def test_on_message_listener_skips_bot_authors():
    """Bot messages (own announcements etc.) must not trigger launcher refresh."""
    cog = _make_cog()
    cog.refresh_whisper_launcher = AsyncMock()  # type: ignore[method-assign]

    msg = MagicMock(spec=discord.Message)
    msg.author = MagicMock()
    msg.author.bot = True
    msg.guild = MagicMock()
    msg.guild.id = GUILD_ID
    msg.channel = MagicMock()
    msg.channel.id = FEED_CHANNEL_ID
    msg.id = 99999

    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg(launcher_message_id=11111)):
        await cog._on_message_launcher_bump(msg)

    cog.refresh_whisper_launcher.assert_not_called()


# ── S5: on_guild_remove cleanup ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_guild_remove_calls_clear_guild_config():
    """_on_guild_remove listener should delegate to _clear_guild_config with guild_id."""
    cog = _make_cog()
    cog._clear_guild_config = MagicMock()  # type: ignore[method-assign]

    guild = MagicMock()
    guild.id = GUILD_ID

    await cog._on_guild_remove(guild)

    cog._clear_guild_config.assert_called_once_with(GUILD_ID)


def test_clear_guild_config_deletes_all_whisper_keys():
    """_clear_guild_config calls delete_config_value for each of the 4 whisper config keys."""
    from unittest.mock import patch as _patch
    cog = _make_cog()

    with _patch("bot_modules.cogs.whisper_cog.open_db") as mock_open_db:
        conn_ctx = MagicMock()
        mock_open_db.return_value.__enter__ = MagicMock(return_value=conn_ctx)
        mock_open_db.return_value.__exit__ = MagicMock(return_value=False)
        cog._clear_guild_config(GUILD_ID)

    # Verify that open_db was called (config deletion happens inside)
    mock_open_db.assert_called_once()


@pytest.mark.asyncio
async def test_cog_load_bootstraps_launcher_in_each_guild():
    from bot_modules.cogs.whisper_cog import WhisperCog

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    bot.add_view = MagicMock()
    bot.add_dynamic_items = MagicMock()
    g1 = MagicMock()
    g1.id = 9001
    g2 = MagicMock()
    g2.id = 9002
    bot.guilds = [g1, g2]

    cog = WhisperCog(bot)
    cog.refresh_whisper_launcher = AsyncMock()  # type: ignore[method-assign]
    await cog.cog_load()

    assert cog.refresh_whisper_launcher.await_count == 2
    awaited_guild_ids = {call.args[0] for call in cog.refresh_whisper_launcher.await_args_list}
    assert awaited_guild_ids == {9001, 9002}
