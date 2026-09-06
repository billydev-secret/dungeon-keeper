"""Cog-level: the launcher at the bottom of the whisper channel.

The launcher moved onto ``core.sticky.StickyPanel`` in the 2026-09 games
review (rotation-rooms-166) — it was one of the two remaining hand-rolled
placers, with no debounce, a DB read for every message in every guild, and
delete-before-post. The debounce, the lock and the placement semantics are
covered once in ``tests/test_core_sticky.py``; what is left here is this cog's
own glue: the three sticky callbacks, the known-guilds publish, the refresh
entry point the send/share paths call, and one assertion that the listener
actually forwards.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.whisper_models import WhisperConfig
from bot_modules.services.whisper_repo import (
    get_whisper_config,
    set_whisper_config_value,
)
from tests.fakes import FakeGuild

GUILD_ID = 9001
FEED_CHANNEL_ID = 8001


def _cfg(
    *,
    channel_id: int = FEED_CHANNEL_ID,
    launcher_message_id: int = 0,
    launcher_channel_id: int = 0,
) -> WhisperConfig:
    return WhisperConfig(
        guild_id=GUILD_ID,
        role_id=7001,
        channel_id=channel_id,
        log_channel_id=8002,
        launcher_message_id=launcher_message_id,
        launcher_channel_id=launcher_channel_id,
    )


def _make_cog(db_path: str | Path = ":memory:"):
    from bot_modules.cogs.whisper_cog import WhisperCog
    bot = MagicMock()
    bot.ctx.db_path = db_path
    bot.add_view = MagicMock()
    bot.add_dynamic_items = MagicMock()
    return WhisperCog(bot)


def _make_text_channel(channel_id: int = FEED_CHANNEL_ID) -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    return channel


# ── the sticky callbacks ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("cfg", "expected"),
    [
        pytest.param(
            _cfg(launcher_message_id=555, launcher_channel_id=4242),
            (4242, 555),
            id="where-it-actually-is",
        ),
        pytest.param(
            _cfg(launcher_message_id=555, launcher_channel_id=0),
            (FEED_CHANNEL_ID, 555),
            id="legacy-launcher-falls-back-to-the-feed",
        ),
        pytest.param(
            _cfg(launcher_message_id=0, launcher_channel_id=0),
            (0, 0),
            id="not-posted-never-invents-a-channel",
        ),
    ],
)
def test_launcher_ids(cfg, expected):
    """``place`` deletes the old launcher through the stored channel, so the
    ids report where the launcher *is*, not where the feed now points. A
    launcher posted before the channel key existed falls back to the feed —
    that is where it was posted — and (0, 0) keeps the restick a no-op for a
    guild that never posted one."""
    cog = _make_cog()
    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=cfg):
        assert cog._launcher_ids(GUILD_ID) == expected


def test_save_launcher_ids_records_channel_and_message(sync_db_path: Path):
    cog = _make_cog(sync_db_path)
    cog._save_launcher_ids(GUILD_ID, 4242, 555)
    with open_db(sync_db_path) as conn:
        cfg = get_whisper_config(conn, GUILD_ID)
    assert (cfg.launcher_channel_id, cfg.launcher_message_id) == (4242, 555)


@pytest.mark.asyncio
async def test_build_launcher_carries_the_persistent_view():
    """The launcher's buttons must survive a restart, so the view the placer
    sends has to be the registered persistent one."""
    from bot_modules.cogs.whisper_cog import WhisperFeedView

    cog = _make_cog()
    with patch(
        "bot_modules.cogs.whisper_cog.safe_resolve_accent",
        AsyncMock(return_value=discord.Color.default()),
    ):
        content = await cog._build_launcher(FakeGuild(id=GUILD_ID))
    assert isinstance(content.view, WhisperFeedView)
    assert content.view.timeout is None
    assert "Whisper" in (content.embed.description or "")


# ── refresh entry point (send / share / boot) ───────────────────────────────


@pytest.mark.asyncio
async def test_refresh_places_through_the_shared_panel():
    cog = _make_cog()
    cog.launcher = MagicMock()
    cog.launcher.place = AsyncMock()
    channel = _make_text_channel()
    guild = FakeGuild(id=GUILD_ID, channels={FEED_CHANNEL_ID: channel})
    cog.bot.get_guild = MagicMock(return_value=guild)

    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()):
        await cog.refresh_whisper_launcher(GUILD_ID)

    cog.launcher.place.assert_awaited_once_with(guild, channel, only_if_buried=False)


@pytest.mark.asyncio
async def test_refresh_skips_when_channel_unset():
    cog = _make_cog()
    cog.launcher = MagicMock()
    cog.launcher.place = AsyncMock()
    cog.bot.get_guild = MagicMock(return_value=FakeGuild(id=GUILD_ID))

    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg(channel_id=0)):
        await cog.refresh_whisper_launcher(GUILD_ID)

    cog.launcher.place.assert_not_called()


# ── on_message listener ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_message_forwards_to_the_sticky_panel():
    """The only wiring worth asserting here — everything the panel then does
    (bot filter, known-guilds gate, TTL cache, debounce) is covered in
    tests/test_core_sticky.py rather than re-proved through Discord mocks."""
    cog = _make_cog()
    cog.launcher = MagicMock()
    cog.launcher.on_message = AsyncMock()
    msg = MagicMock(spec=discord.Message)

    await cog._on_message_launcher_bump(msg)

    cog.launcher.on_message.assert_awaited_once_with(msg)


@pytest.mark.asyncio
async def test_on_message_no_longer_reads_the_db_per_message():
    """Regression for rotation-rooms-166: this listener used to open a fresh
    connection for every message in every guild, before it had even looked at
    the channel."""
    cog = _make_cog()
    cog.launcher = MagicMock()
    cog.launcher.on_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._load_config") as load_cfg:
        await cog._on_message_launcher_bump(MagicMock(spec=discord.Message))

    load_cfg.assert_not_called()


@pytest.mark.asyncio
async def test_channel_delete_clears_the_launcher_ids():
    cog = _make_cog()
    cog.launcher = MagicMock()
    cog.launcher.on_channel_delete = AsyncMock()
    channel = _make_text_channel()

    await cog._forget_deleted_launcher_channel(channel)

    cog.launcher.on_channel_delete.assert_awaited_once_with(channel)


@pytest.mark.asyncio
async def test_cog_unload_cancels_the_panel_debounce():
    cog = _make_cog()
    cog.launcher = MagicMock()

    await cog.cog_unload()

    cog.launcher.cancel_all.assert_called_once()


# ── boot ─────────────────────────────────────────────────────────────────────


def test_launcher_guilds_are_the_ones_with_a_feed_channel(sync_db_path: Path):
    from bot_modules.cogs.whisper_cog import _do_launcher_guilds

    with open_db(sync_db_path) as conn:
        set_whisper_config_value(conn, 9001, "whisper_channel_id", "8001")
        set_whisper_config_value(conn, 9002, "whisper_channel_id", "0")
        set_whisper_config_value(conn, 9003, "whisper_role_id", "7001")  # no channel
    assert _do_launcher_guilds(sync_db_path) == {9001}


@pytest.mark.asyncio
async def test_cog_load_publishes_known_guilds_and_bootstraps_only_those():
    """Boot re-sticks the launcher only where a feed channel is set, and only
    if it is buried — a launcher already at the bottom is left alone rather
    than deleted and reposted on every restart."""
    cog = _make_cog()
    g1, g2 = FakeGuild(id=9001), FakeGuild(id=9002)
    cog.bot.guilds = [g1, g2]
    cog.launcher = MagicMock()
    cog.refresh_whisper_launcher = AsyncMock()  # type: ignore[method-assign]

    with (
        patch("bot_modules.cogs.whisper_cog._do_launcher_guilds", return_value={9001}),
        patch("bot_modules.cogs.whisper_cog._do_backfill_launcher_channels") as backfill,
    ):
        await cog.cog_load()

    backfill.assert_called_once_with(cog.bot.ctx.db_path, {9001})
    cog.launcher.set_known_guilds.assert_called_once_with({9001})
    cog.refresh_whisper_launcher.assert_awaited_once_with(9001, only_if_buried=True)


@pytest.mark.asyncio
async def test_cog_load_pins_a_legacy_launcher_so_a_repoint_deletes_it_in_place(
    sync_db_path: Path,
):
    """``whisper_launcher_channel_id`` shipped with no backfill and the boot
    bootstrap only writes it on a repost — a launcher already at the bottom of
    the feed kept a message id with no channel. Repointing the feed then ran
    the placer with the NEW channel already saved, so the fallback aimed the
    delete at the new channel (NotFound, swallowed) and the old launcher stayed
    live where it was. Boot pins the legacy id to the feed channel it was
    posted in, so the later repoint deletes it from the OLD channel."""
    from bot_modules.core.sticky import PanelContent

    old_feed, new_feed = _make_text_channel(8001), _make_text_channel(8003)
    for ch in (old_feed, new_feed):
        ch.last_message_id = None
        ch.send = AsyncMock(return_value=MagicMock(id=777))
        ch.get_partial_message.return_value.delete = AsyncMock()
    guild = FakeGuild(id=GUILD_ID, channels={8001: old_feed, 8003: new_feed})
    cog = _make_cog(sync_db_path)
    cog.bot.guilds = []  # no bootstrap repost — the launcher is already at the bottom
    cog.bot.get_guild = lambda gid: guild
    with open_db(sync_db_path) as conn:
        set_whisper_config_value(conn, GUILD_ID, "whisper_channel_id", "8001")
        set_whisper_config_value(conn, GUILD_ID, "whisper_launcher_message_id", "555")

    await cog.cog_load()

    with open_db(sync_db_path) as conn:
        assert get_whisper_config(conn, GUILD_ID).launcher_channel_id == 8001
        # The admin repoints the feed; the PUT commits before the cog hears.
        set_whisper_config_value(conn, GUILD_ID, "whisper_channel_id", "8003")

    with patch.object(
        cog, "_build_launcher",
        AsyncMock(return_value=PanelContent(embed=discord.Embed(description="x"))),
    ):
        await cog.on_whisper_config_change(GUILD_ID)

    new_feed.send.assert_awaited_once()
    old_feed.get_partial_message.assert_called_once_with(555)
    new_feed.get_partial_message.assert_not_called()
    with open_db(sync_db_path) as conn:
        cfg = get_whisper_config(conn, GUILD_ID)
    assert (cfg.launcher_channel_id, cfg.launcher_message_id) == (8003, 777)


@pytest.mark.parametrize(
    ("configured", "resticks"),
    [
        pytest.param({9001}, True, id="feed-channel-set-after-boot"),
        pytest.param(set(), False, id="feed-channel-unset"),
    ],
)
@pytest.mark.asyncio
async def test_config_change_republishes_known_guilds_and_resticks(configured, resticks):
    """A dashboard save dispatches ``whisper_config_change``: the known-guild
    set the on_message fast path gates on is republished, and a guild that
    now has a feed channel gets its launcher without a restart."""
    cog = _make_cog()
    cog.launcher = MagicMock()
    cog.refresh_whisper_launcher = AsyncMock()  # type: ignore[method-assign]

    with patch("bot_modules.cogs.whisper_cog._do_launcher_guilds", return_value=configured):
        await cog.on_whisper_config_change(9001)

    cog.launcher.set_known_guilds.assert_called_once_with(configured)
    if resticks:
        cog.refresh_whisper_launcher.assert_awaited_once_with(9001, only_if_buried=True)
        cog.launcher.forget.assert_not_called()
    else:
        cog.refresh_whisper_launcher.assert_not_called()
        cog.launcher.forget.assert_called_once_with(9001)


# ── S5: on_guild_remove cleanup ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_guild_remove_calls_clear_guild_config():
    cog = _make_cog()
    cog._clear_guild_config = MagicMock()  # type: ignore[method-assign]
    guild = MagicMock()
    guild.id = GUILD_ID

    await cog._on_guild_remove(guild)

    cog._clear_guild_config.assert_called_once_with(GUILD_ID)


def test_clear_guild_config_deletes_every_whisper_key(sync_db_path: Path):
    """Including the launcher's channel key, and the panel's cached ids."""
    cog = _make_cog(sync_db_path)
    cog.launcher = MagicMock()
    with open_db(sync_db_path) as conn:
        set_whisper_config_value(conn, GUILD_ID, "whisper_channel_id", "8001")
        set_whisper_config_value(conn, GUILD_ID, "whisper_role_id", "7001")
        set_whisper_config_value(conn, GUILD_ID, "whisper_launcher_channel_id", "8001")
        set_whisper_config_value(conn, GUILD_ID, "whisper_launcher_message_id", "555")

    cog._clear_guild_config(GUILD_ID)

    with open_db(sync_db_path) as conn:
        cfg = get_whisper_config(conn, GUILD_ID)
    assert (cfg.channel_id, cfg.role_id) == (0, 0)
    assert (cfg.launcher_channel_id, cfg.launcher_message_id) == (0, 0)
    cog.launcher.forget.assert_called_once_with(GUILD_ID)
