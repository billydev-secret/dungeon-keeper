"""External game tracking routes — the replacement for /games track *.

The five commands went on 2026-07-28, so these routes are now the only way to
configure which bot gets banked in which channel. Two things carry real risk and
are covered here: the in-memory watch cache the message listener actually reads
(a write that doesn't refresh it is a silent no-op until restart), and the
per-channel scoping of the banked counts.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from bot_modules.core.db_utils import open_db


def _seed_watch(db_path, *, guild_id, channel_id, bot_id, kind="gamebot", enabled=1):
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO games_external_watch "
            "(guild_id, channel_id, bot_user_id, kind, enabled, set_by, set_at) "
            "VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)",
            (guild_id, channel_id, bot_id, kind, enabled, 1),
        )


def _seed_message(db_path, *, guild_id, channel_id, author_id, message_id):
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO games_external_messages "
            "(message_id, guild_id, channel_id, author_id, created_at, content, embeds_json) "
            "VALUES (?,?,?,?,'2026-01-01T00:00:00+00:00','hello','[]')",
            (message_id, guild_id, channel_id, author_id),
        )


@pytest.fixture
def cog(fake_ctx):
    """Attach a stand-in bot whose cog records cache refreshes."""
    c = MagicMock()
    c.refresh_watch_cache = AsyncMock()
    bot = MagicMock()
    bot.get_cog.return_value = c
    bot.get_guild.return_value = None  # no live guild → skip the is-a-bot check
    fake_ctx.bot = bot
    return c


def test_listing_is_empty_before_anything_is_configured(authed_client):
    r = authed_client.get("/api/games-external")
    assert r.status_code == 200
    assert r.json()["watches"] == []


def test_listing_offers_the_parser_kinds(authed_client):
    """The panel renders these as the format dropdown; an empty list would ship
    a picker with nothing in it."""
    kinds = authed_client.get("/api/games-external").json()["kinds"]
    assert kinds
    assert all(k["value"] and k["label"] for k in kinds)


def test_listing_reports_per_channel_banked_counts(authed_client, fake_ctx):
    """A bot watched in two channels has a row each. An unscoped count would
    report the bot's whole total against both."""
    gid = fake_ctx.guild_id
    _seed_watch(fake_ctx.db_path, guild_id=gid, channel_id=10, bot_id=99)
    _seed_watch(fake_ctx.db_path, guild_id=gid, channel_id=20, bot_id=99)
    for mid, ch in ((1, 10), (2, 10), (3, 20)):
        _seed_message(fake_ctx.db_path, guild_id=gid, channel_id=ch, author_id=99, message_id=mid)

    watches = {w["channel_id"]: w for w in authed_client.get("/api/games-external").json()["watches"]}
    assert watches["10"]["banked"] == 2
    assert watches["20"]["banked"] == 1


def test_adding_a_watch_refreshes_the_listener_cache(authed_client, cog):
    """The listener matches messages against an in-memory map. Without this the
    new channel is ignored until the next restart."""
    r = authed_client.post(
        "/api/games-external/watches",
        json={"channel_id": "10", "bot_id": "99", "kind": "gamebot"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["live"] is True
    cog.refresh_watch_cache.assert_awaited_once()


def test_adding_a_watch_reports_when_the_bot_is_offline(authed_client, fake_ctx):
    """Saved either way — but a caller who isn't told would think it was live."""
    fake_ctx.bot = None
    r = authed_client.post(
        "/api/games-external/watches",
        json={"channel_id": "10", "bot_id": "99", "kind": "gamebot"},
    )
    assert r.status_code == 200
    assert r.json()["live"] is False
    # and it really did persist
    assert authed_client.get("/api/games-external").json()["watches"]


def test_an_unknown_parser_kind_is_refused(authed_client, cog):
    r = authed_client.post(
        "/api/games-external/watches",
        json={"channel_id": "10", "bot_id": "99", "kind": "not-a-parser"},
    )
    assert r.status_code == 400


def test_watching_a_human_is_refused_when_the_guild_is_visible(authed_client, fake_ctx):
    """Watching a person banks their messages and parses nothing. Refuse when we
    can actually tell — with the bot offline we can't, and configuration
    shouldn't be blocked on that."""
    human = MagicMock(bot=False, display_name="Alice")
    guild = MagicMock()
    guild.get_member.return_value = human
    bot = MagicMock()
    bot.get_guild.return_value = guild
    bot.get_cog.return_value = MagicMock(refresh_watch_cache=AsyncMock())
    fake_ctx.bot = bot

    r = authed_client.post(
        "/api/games-external/watches",
        json={"channel_id": "10", "bot_id": "5", "kind": "gamebot"},
    )
    assert r.status_code == 400
    assert "isn't a bot account" in r.json()["detail"]


def test_toggling_pauses_every_channel_for_that_bot_by_default(authed_client, fake_ctx, cog):
    """Pausing a chatty bot shouldn't mean naming its channels one by one."""
    gid = fake_ctx.guild_id
    _seed_watch(fake_ctx.db_path, guild_id=gid, channel_id=10, bot_id=99)
    _seed_watch(fake_ctx.db_path, guild_id=gid, channel_id=20, bot_id=99)

    r = authed_client.post(
        "/api/games-external/watches/toggle", json={"bot_id": "99", "enabled": False}
    )
    assert r.status_code == 200, r.text
    watches = authed_client.get("/api/games-external").json()["watches"]
    assert all(w["enabled"] is False for w in watches)
    cog.refresh_watch_cache.assert_awaited()


def test_toggling_one_channel_leaves_the_others_alone(authed_client, fake_ctx, cog):
    gid = fake_ctx.guild_id
    _seed_watch(fake_ctx.db_path, guild_id=gid, channel_id=10, bot_id=99)
    _seed_watch(fake_ctx.db_path, guild_id=gid, channel_id=20, bot_id=99)

    authed_client.post(
        "/api/games-external/watches/toggle",
        json={"bot_id": "99", "channel_id": "10", "enabled": False},
    )
    watches = {w["channel_id"]: w for w in authed_client.get("/api/games-external").json()["watches"]}
    assert watches["10"]["enabled"] is False
    assert watches["20"]["enabled"] is True


def test_toggling_an_unconfigured_bot_is_a_404(authed_client, cog):
    r = authed_client.post(
        "/api/games-external/watches/toggle", json={"bot_id": "12345", "enabled": True}
    )
    assert r.status_code == 404


def test_sample_returns_banked_messages_verbatim(authed_client, fake_ctx):
    """The point is checking a parser against real output, so content comes back
    unprettified."""
    gid = fake_ctx.guild_id
    _seed_message(fake_ctx.db_path, guild_id=gid, channel_id=10, author_id=99, message_id=1)

    r = authed_client.get(
        "/api/games-external/sample", params={"channel_id": "10", "bot_id": "99"}
    )
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["content"] == "hello"
    # snowflakes as strings — they exceed 2^53 in production
    assert isinstance(msgs[0]["message_id"], str)


def test_sample_is_empty_for_an_unwatched_pair(authed_client):
    r = authed_client.get(
        "/api/games-external/sample", params={"channel_id": "1", "bot_id": "2"}
    )
    assert r.status_code == 200
    assert r.json()["messages"] == []


def test_sample_count_is_bounded(authed_client):
    """An unbounded dump would hand the browser the whole table."""
    r = authed_client.get(
        "/api/games-external/sample",
        params={"channel_id": "1", "bot_id": "2", "count": 5000},
    )
    assert r.status_code == 422


# ── the cache the listener actually reads ────────────────────────────


@pytest.mark.asyncio
async def test_refresh_watch_cache_rebuilds_from_the_database(fake_ctx):
    """The route calls this after every write. It rebuilds the guild's whole
    entry rather than patching one key, so a pause spanning several channels and
    a re-point to a new channel both land without the caller knowing which
    happened. Paused rows must drop out — a stale key keeps banking a bot the
    admin just switched off."""
    from bot_modules.cogs.games_external_cog import GamesExternalCog
    from bot_modules.services.games_db import GamesDb

    gid = fake_ctx.guild_id
    _seed_watch(fake_ctx.db_path, guild_id=gid, channel_id=10, bot_id=99, enabled=1)
    _seed_watch(fake_ctx.db_path, guild_id=gid, channel_id=20, bot_id=99, enabled=0)

    cog = GamesExternalCog.__new__(GamesExternalCog)
    cog._watch = {gid: {(1, 2): "stale"}}
    cog.bot = MagicMock(games_db=GamesDb(fake_ctx.db_path))

    await cog.refresh_watch_cache(gid)

    assert cog._watch[gid] == {(99, 10): "gamebot"}
