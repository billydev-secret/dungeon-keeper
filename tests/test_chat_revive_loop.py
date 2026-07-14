"""Tests for the Chat Revive monitor loop — fires, refuses, never embarrasses.

Economy-loop style: hand-rolled bot/guild/channel stubs, explicit ``now_ts``
injection, a real migrated DB.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.chat_revive_loop import consider_channel, run_tick
from bot_modules.services.chat_revive_service import (
    ChannelConfig,
    GuildConfig,
    add_question,
    save_channel_config,
    save_guild_config,
)
from migrations import apply_migrations_sync

GID, CID, BOT_ID = 100, 200, 999


def _ts(day: int, hour: int, minute: int = 0) -> float:
    return datetime(2026, 6, day, hour, minute, tzinfo=timezone.utc).timestamp()


NOW = _ts(30, 19)  # 19:00 local (offset 0), a normally-lively evening hour


class FakeMessage(SimpleNamespace):
    pass


def _history_msg(ts: float, author_id: int) -> FakeMessage:
    return FakeMessage(
        author=SimpleNamespace(id=author_id),
        created_at=datetime.fromtimestamp(ts, tz=timezone.utc),
    )


class FakeChannel:
    """Just enough discord.TextChannel for the loop; isinstance-compatible."""

    __class__ = discord.TextChannel  # type: ignore[assignment]

    def __init__(self, history_msgs: list[FakeMessage] | None = None) -> None:
        self.id = CID
        self.name = "general"
        self.slowmode_delay = 0
        self._history_msgs = history_msgs or []
        self.send = AsyncMock(return_value=SimpleNamespace(id=777))

    def is_nsfw(self) -> bool:
        return False

    def history(self, limit: int = 1):
        msgs = self._history_msgs[:limit]

        async def _aiter():
            for m in msgs:
                yield m

        return _aiter()


def _bot(channel: FakeChannel) -> SimpleNamespace:
    guild = SimpleNamespace(
        id=GID, get_channel=lambda cid: channel if cid == channel.id else None
    )
    return SimpleNamespace(
        user=SimpleNamespace(id=BOT_ID),
        get_guild=lambda gid: guild if gid == GID else None,
        games_db=None,
        game_busy_checks={},
    )


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    apply_migrations_sync(db_path)
    return db_path


_mid = iter(range(1, 1_000_000))


def _seed_lively_history(conn, *, days: int = 29, last_gap: float = 3000.0) -> float:
    """An evening channel (18:00-22:00 every 10m) whose last message was
    ``last_gap`` seconds before NOW. Returns the last human timestamp."""
    for day in range(1, days + 1):
        for m in range(24):
            ts = _ts(day, 18) + m * 600
            if ts >= NOW - last_gap:
                continue
            conn.execute(
                "INSERT INTO processed_messages (guild_id, message_id, channel_id,"
                " user_id, created_at, processed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (GID, next(_mid), CID, 1 + m % 5, ts, ts),
            )
    row = conn.execute(
        "SELECT MAX(created_at) AS ts FROM processed_messages "
        "WHERE guild_id = ? AND channel_id = ?",
        (GID, CID),
    ).fetchone()
    return row["ts"]


def _enable(conn, **channel_overrides) -> ChannelConfig:
    save_guild_config(conn, GuildConfig(guild_id=GID, enabled=True, role_id=555))
    cfg = ChannelConfig(guild_id=GID, channel_id=CID, **channel_overrides)
    save_channel_config(conn, cfg)
    add_question(conn, GID, "Spark?", created_by=1, now_ts=NOW - 40 * 86400)
    return cfg


async def test_fires_on_genuine_lull_and_records(db):
    with open_db(db) as conn:
        cfg = _enable(conn)
        last_human = _seed_lively_history(conn)
    channel = FakeChannel([_history_msg(last_human, author_id=1)])
    fired = await consider_channel(_bot(channel), db, cfg, NOW)
    assert fired
    channel.send.assert_awaited_once()
    text = channel.send.await_args.args[0]
    assert "Spark?" in text
    assert channel.send.await_args.kwargs["allowed_mentions"] is not None
    with open_db(db) as conn:
        ev = conn.execute("SELECT * FROM revive_events").fetchone()
        q = conn.execute("SELECT use_count FROM revive_questions").fetchone()
    assert ev["trigger_kind"] == "auto"
    assert ev["message_id"] == 777
    assert ev["pinged"] == 0  # ping_enabled defaults off
    assert q["use_count"] == 1


async def test_pings_when_enabled_and_scarce(db):
    with open_db(db) as conn:
        cfg = _enable(conn, ping_enabled=True)
        last_human = _seed_lively_history(conn)
    channel = FakeChannel([_history_msg(last_human, author_id=1)])
    assert await consider_channel(_bot(channel), db, cfg, NOW)
    text = channel.send.await_args.args[0]
    assert "<@&555>" in text
    with open_db(db) as conn:
        assert conn.execute("SELECT pinged FROM revive_events").fetchone()[0] == 1


async def test_never_fires_twice_channel_rest(db):
    with open_db(db) as conn:
        cfg = _enable(conn)
        last_human = _seed_lively_history(conn)
    channel = FakeChannel([_history_msg(last_human, author_id=1)])
    bot = _bot(channel)
    assert await consider_channel(bot, db, cfg, NOW)
    # Same tick evaluated again, and a tick 2 minutes later: both refuse.
    assert not await consider_channel(bot, db, cfg, NOW)
    assert not await consider_channel(bot, db, cfg, NOW + 120)
    channel.send.assert_awaited_once()


async def test_aborts_when_history_shows_newer_message(db):
    """Ingest lag: someone spoke seconds ago but the ledger hasn't caught up."""
    with open_db(db) as conn:
        cfg = _enable(conn)
        _seed_lively_history(conn)
    channel = FakeChannel([_history_msg(NOW - 5, author_id=7)])
    assert not await consider_channel(_bot(channel), db, cfg, NOW)
    channel.send.assert_not_awaited()
    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM revive_events").fetchone()[0] == 0


async def test_aborts_when_newest_message_is_our_own(db):
    with open_db(db) as conn:
        cfg = _enable(conn)
        last_human = _seed_lively_history(conn)
    channel = FakeChannel([_history_msg(last_human + 1, author_id=BOT_ID)])
    assert not await consider_channel(_bot(channel), db, cfg, NOW)
    channel.send.assert_not_awaited()


async def test_send_failure_records_nothing(db):
    with open_db(db) as conn:
        cfg = _enable(conn)
        last_human = _seed_lively_history(conn)
    channel = FakeChannel([_history_msg(last_human, author_id=1)])
    channel.send = AsyncMock(
        side_effect=discord.HTTPException(
            SimpleNamespace(status=403, reason="Forbidden"), "no"
        )
    )
    assert not await consider_channel(_bot(channel), db, cfg, NOW)
    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM revive_events").fetchone()[0] == 0


async def test_refuses_when_guild_disabled(db):
    with open_db(db) as conn:
        cfg = _enable(conn)
        save_guild_config(conn, GuildConfig(guild_id=GID, enabled=False))
        last_human = _seed_lively_history(conn)
    channel = FakeChannel([_history_msg(last_human, author_id=1)])
    assert not await consider_channel(_bot(channel), db, cfg, NOW)


async def test_refuses_when_game_busy(db):
    with open_db(db) as conn:
        cfg = _enable(conn)
        last_human = _seed_lively_history(conn)
    channel = FakeChannel([_history_msg(last_human, author_id=1)])
    bot = _bot(channel)

    async def busy_check(channel_id: int) -> bool:
        return channel_id == CID

    bot.game_busy_checks = {"risky_roll": busy_check}
    assert not await consider_channel(bot, db, cfg, NOW)


async def test_run_tick_measures_follow_up(db):
    with open_db(db) as conn:
        cfg = _enable(conn)
        last_human = _seed_lively_history(conn)
    channel = FakeChannel([_history_msg(last_human, author_id=1)])
    bot = _bot(channel)
    assert await consider_channel(bot, db, cfg, NOW)
    # Three people answer within the half hour.
    with open_db(db) as conn:
        for uid, dt in [(11, 60), (12, 120), (13, 500)]:
            conn.execute(
                "INSERT INTO processed_messages (guild_id, message_id, channel_id,"
                " user_id, created_at, processed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (GID, next(_mid), CID, uid, NOW + dt, NOW + dt),
            )
    await run_tick(bot, db, NOW + 1900)
    with open_db(db) as conn:
        ev = conn.execute("SELECT * FROM revive_events").fetchone()
    assert ev["success"] == 1
    assert ev["follow_msgs"] == 3
    assert ev["follow_authors"] == 3


async def test_run_tick_isolates_channel_errors(db):
    """A channel the bot can't resolve doesn't break the sweep for others."""
    with open_db(db) as conn:
        _enable(conn)
        save_channel_config(
            conn, ChannelConfig(guild_id=GID, channel_id=CID + 1)  # unresolvable
        )
        last_human = _seed_lively_history(conn)
    channel = FakeChannel([_history_msg(last_human, author_id=1)])
    await run_tick(_bot(channel), db, NOW)
    channel.send.assert_awaited_once()
