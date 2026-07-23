"""Guild scoping for check_allowed_channel (migration 115).

channel_id is a globally-unique snowflake, so the legacy channel-only match is
not itself a cross-guild leak; these tests lock in the optional guild_id filter
(defence-in-depth) and its guild_id = 0 wildcard for un-reconciled legacy rows.
"""

from bot_modules.games.utils.game_manager import check_allowed_channel
from bot_modules.services.games_db import GamesDb


async def _add(db, channel_id, guild_id):
    await db.execute(
        "INSERT INTO games_allowed_channels (channel_id, guild_id) VALUES (?, ?)",
        (channel_id, guild_id),
    )


async def test_channel_only_match_is_backward_compatible(sync_db_path):
    db = GamesDb(sync_db_path)
    await _add(db, 111, 42)

    assert await check_allowed_channel(db, 111) is True
    assert await check_allowed_channel(db, 999) is False
    assert await check_allowed_channel(db, None) is False


async def test_guild_scoped_match_rejects_other_guild(sync_db_path):
    db = GamesDb(sync_db_path)
    await _add(db, 111, 42)

    assert await check_allowed_channel(db, 111, guild_id=42) is True
    # A channel stamped to guild 42 must not read as allowed for guild 7.
    assert await check_allowed_channel(db, 111, guild_id=7) is False


async def test_legacy_zero_guild_is_wildcard(sync_db_path):
    db = GamesDb(sync_db_path)
    await _add(db, 111, 0)  # legacy row, not yet reconciled to a guild

    assert await check_allowed_channel(db, 111, guild_id=42) is True
    assert await check_allowed_channel(db, 111, guild_id=7) is True
