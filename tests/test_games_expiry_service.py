"""Tests for the 24-hour expiry sweep — the path that actually ends most games.

The regression these cover: a Truth or Dare game left open by its host was
reaped by the sweep with a bare ``end_game`` call, so its roster was never paid.
Every traditional game in prod history (18 of 18) ended this way.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import discord
import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.games.utils.expiry_service import archived_notice, sweep_expired_games
from bot_modules.games.utils.game_manager import create_game, get_active_game_by_id
from bot_modules.services.economy_service import get_balance, save_econ_settings
from bot_modules.services.games_db import GamesDb
from tests.db_template import migrated_db
from tests.fakes import FakeGuild

GUILD = 4242
CH = 700


def _member(uid: int):
    return SimpleNamespace(id=uid, bot=False, premium_since=None, display_name=f"U{uid}")


class _Bot:
    """Minimal bot double: the sweep needs a channel→guild map, an active_views
    dict to evict from, and get_cog for the ama hand-off.
    """

    def __init__(self, db_path, members):
        self.ctx = SimpleNamespace(db_path=db_path)
        self._guild = FakeGuild(id=GUILD, members={m.id: m for m in members})
        self.active_views: dict[str, Any] = {}
        self.send = AsyncMock()

    def get_guild(self, gid):
        return self._guild if gid == GUILD else None

    def get_channel(self, cid):
        return SimpleNamespace(id=CH, guild=self._guild, send=self.send) if cid == CH else None

    def get_cog(self, name):
        return None


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "test.db"
    migrated_db(p)
    return p


def _enable(db_path):
    with open_db(db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})


def _bal(db_path, uid: int) -> int:
    with open_db(db_path) as conn:
        return get_balance(conn, GUILD, uid)


async def _age(db, game_id: str, hours: int = 30) -> None:
    """Backdate a game so the sweep considers it expired."""
    await db.execute(
        "UPDATE games_active_games SET created_at = datetime('now', ?) WHERE game_id = ?",
        (f"-{hours} hours", game_id),
    )


# Per-type roster reconstruction is covered in tests/test_game_roster.py.


# ── sweep_expired_games ───────────────────────────────────────────────────────

async def test_sweep_pays_the_roster_of_an_abandoned_traditional_game(db_path):
    """The regression. A host opens Truth or Dare, the room plays, the host
    never presses End Game, and 24h later the sweep reaps it. Before the fix
    the sweep archived with a bare end_game and everyone — players and host —
    got nothing.
    """
    _enable(db_path)
    db = GamesDb(db_path)
    payload = {"participants": [1, 2, 3], "asked": {"1": "q1", "2": "q2"}}
    gid = await create_game(db, CH, 1, "traditional", payload=payload)
    await _age(db, gid)
    bot: Any = _Bot(db_path, [_member(1), _member(2), _member(3)])
    bot.active_views[gid] = object()

    assert await sweep_expired_games(bot, db) == 1

    assert _bal(db_path, 2) == 5  # participation
    assert _bal(db_path, 3) == 5
    assert _bal(db_path, 1) >= 5  # host played too, plus any host bounty
    assert await get_active_game_by_id(db, gid) is None
    assert gid not in bot.active_views


async def test_sweep_archives_the_real_roster_not_zero(db_path):
    """The history row is what the dashboard and any later backfill read, so the
    sweep must record the roster it paid — not the player_count=0 / payload={}
    the bare call wrote.
    """
    _enable(db_path)
    db = GamesDb(db_path)
    payload = {"participants": [1, 2, 3], "asked": {"1": "q1", "2": "q2"}}
    gid = await create_game(db, CH, 1, "traditional", payload=payload)
    await _age(db, gid)
    bot: Any = _Bot(db_path, [_member(1), _member(2), _member(3)])

    await sweep_expired_games(bot, db)

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT player_count, round_count, payload, guild_id "
            "FROM games_game_history WHERE game_id = ?",
            (gid,),
        ).fetchone()
    assert row["player_count"] == 3
    assert row["round_count"] == 2
    assert "participants" in row["payload"]
    # bot= is now passed, so the guild resolves instead of falling back to 0.
    assert row["guild_id"] == GUILD


async def test_sweep_pays_once_when_it_runs_twice(db_path):
    """end_game's DELETE claim is the exactly-once gate; a second sweep pass
    (or a sweep racing a host who finally presses End Game) must not re-credit.
    """
    _enable(db_path)
    db = GamesDb(db_path)
    payload = {"participants": [1, 2], "asked": {"1": "q"}}
    gid = await create_game(db, CH, 1, "traditional", payload=payload)
    await _age(db, gid)
    bot: Any = _Bot(db_path, [_member(1), _member(2)])

    await sweep_expired_games(bot, db)
    before = _bal(db_path, 2)
    assert await sweep_expired_games(bot, db) == 0
    assert _bal(db_path, 2) == before


async def test_sweep_leaves_fresh_games_running(db_path):
    """A game inside the 24h window is still being played — never touch it."""
    _enable(db_path)
    db = GamesDb(db_path)
    gid = await create_game(
        db, CH, 1, "traditional", payload={"participants": [1, 2]},
    )
    bot: Any = _Bot(db_path, [_member(1), _member(2)])

    assert await sweep_expired_games(bot, db) == 0
    assert await get_active_game_by_id(db, gid) is not None
    assert _bal(db_path, 2) == 0


async def test_sweep_pays_nobody_for_a_prompt_style_game(db_path):
    """ffa/photo have no joined roster. They must archive (the row still has to
    leave games_active_games) without inventing a payout.
    """
    _enable(db_path)
    db = GamesDb(db_path)
    gid = await create_game(db, CH, 1, "ffa", payload={"prompt": "x", "mode": "embed"})
    await _age(db, gid)
    bot: Any = _Bot(db_path, [_member(1)])

    assert await sweep_expired_games(bot, db) == 1
    assert await get_active_game_by_id(db, gid) is None
    assert _bal(db_path, 1) == 0


async def test_sweep_pays_nobody_for_an_abandoned_empty_lobby(db_path):
    """The anti-farm case: a host opens a game, nobody joins, it rots for a day.
    An empty roster is the whole guard — no separate "did they play" gate.
    """
    _enable(db_path)
    db = GamesDb(db_path)
    gid = await create_game(db, CH, 1, "traditional", payload={"participants": []})
    await _age(db, gid)
    bot: Any = _Bot(db_path, [_member(1)])

    assert await sweep_expired_games(bot, db) == 1
    assert _bal(db_path, 1) == 0


async def test_sweep_continues_past_a_failing_game(db_path):
    """One bad row must not strand every later game in the sweep."""
    _enable(db_path)
    db = GamesDb(db_path)
    bad = await create_game(db, CH, 1, "traditional", payload={"participants": [1]})
    good = await create_game(db, CH, 1, "traditional", payload={"participants": [2]})
    await _age(db, bad)
    await _age(db, good)
    # Corrupt the first row's payload so json.loads would raise on it.
    await db.execute(
        "UPDATE games_active_games SET payload = ? WHERE game_id = ?", ("{not json", bad),
    )
    bot: Any = _Bot(db_path, [_member(1), _member(2)])

    assert await sweep_expired_games(bot, db) == 2
    assert await get_active_game_by_id(db, good) is None
    assert _bal(db_path, 2) == 5


# ── the in-channel notice (platform-21, decision D2) ─────────────────────────

async def test_sweep_says_what_it_paid_in_the_channel(db_path):
    """The payout used to land a day later with nothing in the room to show
    for it. One line, no recap, no pings."""
    _enable(db_path)
    db = GamesDb(db_path)
    payload = {"participants": [1, 2, 3], "asked": {"1": "q1"}}
    gid = await create_game(db, CH, 1, "traditional", payload=payload, guild_id=GUILD)
    await _age(db, gid)
    bot: Any = _Bot(db_path, [_member(1), _member(2), _member(3)])

    await sweep_expired_games(bot, db)

    paid = sum(_bal(db_path, uid) for uid in (1, 2, 3))
    assert paid > 0
    bot.send.assert_awaited_once()
    text = bot.send.await_args.args[0]
    assert text == archived_notice("traditional", paid, 3)
    assert text == f"📦 Truth or Dare archived after 24h — {paid} coins paid to 3 players."
    mentions = bot.send.await_args.kwargs["allowed_mentions"]
    assert (mentions.everyone, mentions.users, mentions.roles) == (False, False, False)


def test_notice_copy_handles_one_player_and_big_numbers():
    assert archived_notice("wyr", 1250, 1) == (
        "📦 Would You Rather archived after 24h — 1,250 coins paid to 1 player."
    )


async def test_sweep_posts_nothing_for_a_game_that_paid_nobody(db_path):
    """An empty lobby, a bot post, or an economy that is off: no line."""
    db = GamesDb(db_path)  # economy not enabled
    payload = {"participants": [1, 2], "asked": {"1": "q"}}
    played = await create_game(db, CH, 1, "traditional", payload=payload, guild_id=GUILD)
    empty = await create_game(db, CH, 1, "traditional", payload={"participants": []}, guild_id=GUILD)
    post = await create_game(db, CH, 1, "ffa", payload={"prompt": "x"}, guild_id=GUILD)
    for gid in (played, empty, post):
        await _age(db, gid)
    bot: Any = _Bot(db_path, [_member(1), _member(2)])

    assert await sweep_expired_games(bot, db) == 3
    bot.send.assert_not_awaited()


async def test_sweep_survives_a_channel_it_cannot_post_in(db_path):
    _enable(db_path)
    db = GamesDb(db_path)
    payload = {"participants": [1, 2], "asked": {"1": "q"}}
    gid = await create_game(db, CH, 1, "traditional", payload=payload, guild_id=GUILD)
    await _age(db, gid)
    bot: Any = _Bot(db_path, [_member(1), _member(2)])
    bot.send.side_effect = discord.HTTPException(
        SimpleNamespace(status=403, reason="Forbidden"),  # type: ignore[arg-type]
        "Missing Access",
    )

    assert await sweep_expired_games(bot, db) == 1
    assert await get_active_game_by_id(db, gid) is None
    assert _bal(db_path, 2) == 5  # the payout is not tied to the notice


async def test_sweep_survives_a_channel_that_is_gone(db_path):
    """The guild is stamped on the row, so the archive and payout no longer
    need the channel either — only the notice does, and it is skipped."""
    _enable(db_path)
    db = GamesDb(db_path)
    payload = {"participants": [1, 2], "asked": {"1": "q"}}
    gid = await create_game(db, CH, 1, "traditional", payload=payload, guild_id=GUILD)
    await _age(db, gid)
    bot: Any = _Bot(db_path, [_member(1), _member(2)])
    bot.get_channel = lambda cid: None  # type: ignore[method-assign]

    assert await sweep_expired_games(bot, db) == 1
    bot.send.assert_not_awaited()
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT guild_id, payload FROM games_game_history WHERE game_id = ?", (gid,),
        ).fetchone()
    assert row["guild_id"] == GUILD
    assert json.loads(row["payload"])["reason"] == "expired"
