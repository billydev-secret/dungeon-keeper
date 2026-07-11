"""Tests for economy/game_rewards.py and the game_manager.end_game payout hook."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.economy.game_rewards import pay_game_rewards, resolve_winners
from bot_modules.games.utils.game_manager import (
    create_game,
    end_game,
    get_active_game_by_id,
)
from bot_modules.services.economy_service import get_balance, save_econ_settings
from bot_modules.services.games_db import GamesDb
from migrations import apply_migrations_sync
from tests.fakes import FakeGuild

GUILD = 4242
CH = 700


# ── resolve_winners (table-driven) ────────────────────────────────────────────

@pytest.mark.parametrize(
    "game_type, payload, expected",
    [
        # nhie — guiltiest player (highest guilt score); JSON keys are strings.
        ("nhie", {"guilt_scores": {"1": 3, "2": 5, "3": 1}}, [2]),
        ("nhie", {"guilt_scores": {}}, []),
        ("nhie", {}, []),
        ("nhie", {"guilt_scores": "broken"}, []),
        # ttl — best liar (fooled the most).
        ("ttl", {"scores": {"1": {"fooled": 2}, "2": {"fooled": 4}}}, [2]),
        ("ttl", {"scores": {}}, []),
        ("ttl", {"scores": {"9": {}}}, [9]),
        # hottakes — author of the highest-rated take.
        ("hottakes", {"results": [{"avg": 2.0, "author": 10}, {"avg": 3.5, "author": 20}]}, [20]),
        ("hottakes", {"results": [{"avg": 1.0}]}, []),  # author missing
        ("hottakes", {"results": []}, []),
        # wyr's "most divisive" is a question, not a player → no winner.
        ("wyr", {"rounds": {"1": {"a": [1], "b": [2]}}}, []),
        # unknown / no-winner types.
        ("mfk", {"anything": 1}, []),
        ("price", {}, []),
    ],
)
def test_resolve_winners(game_type, payload, expected):
    assert resolve_winners(game_type, payload) == expected


def test_resolve_winners_none_payload():
    assert resolve_winners("nhie", None) == []  # type: ignore[arg-type]


# ── pay_game_rewards ──────────────────────────────────────────────────────────

def _member(uid: int, *, bot: bool = False, booster: bool = False):
    return SimpleNamespace(
        id=uid,
        bot=bot,
        premium_since=object() if booster else None,
        display_name=f"U{uid}",
    )


class _Bot:
    def __init__(self, db_path, members):
        self.ctx = SimpleNamespace(db_path=db_path)
        self._guild = FakeGuild(id=GUILD, members={m.id: m for m in members})

    def get_guild(self, gid):
        return self._guild if gid == GUILD else None

    def get_channel(self, cid):
        chan = self._guild.channels.get(cid)
        return chan


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "test.db"
    apply_migrations_sync(p)
    return p


def _enable(db_path, **overrides):
    with open_db(db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True, **overrides})


def _bal(db_path, uid: int) -> int:
    with open_db(db_path) as conn:
        return get_balance(conn, GUILD, uid)


async def test_pay_splits_participation_and_win(db_path):
    _enable(db_path)
    bot: Any = _Bot(db_path, [_member(1), _member(2), _member(3)])
    await pay_game_rewards(bot, GUILD, [1, 2, 3], [1], "chicken")
    assert _bal(db_path, 1) == 25  # 5 participation + 20 win
    assert _bal(db_path, 2) == 5
    assert _bal(db_path, 3) == 5


async def test_pay_noop_when_disabled(db_path):
    bot: Any = _Bot(db_path, [_member(1), _member(2)])
    await pay_game_rewards(bot, GUILD, [1, 2], [1], "chicken")
    assert _bal(db_path, 1) == 0
    assert _bal(db_path, 2) == 0


async def test_pay_filters_bots_and_invalid_ids(db_path):
    _enable(db_path)
    bot: Any = _Bot(db_path, [_member(1), _member(2, bot=True), _member(3)])
    # 2 is a bot, 0 is non-positive, 999 is unresolvable.
    await pay_game_rewards(bot, GUILD, [1, 2, 0, 999, 3], [], "chicken")
    assert _bal(db_path, 1) == 5
    assert _bal(db_path, 2) == 0
    assert _bal(db_path, 3) == 5
    assert _bal(db_path, 999) == 0


async def test_pay_winner_must_be_participant(db_path):
    _enable(db_path)
    bot: Any = _Bot(db_path, [_member(1), _member(2)])
    # 2 is in the guild but not a participant → win bonus dropped, no participation.
    await pay_game_rewards(bot, GUILD, [1], [2], "chicken")
    assert _bal(db_path, 1) == 5
    assert _bal(db_path, 2) == 0


async def test_pay_booster_ceils(db_path):
    _enable(db_path)  # booster_multiplier defaults to 1.5
    bot: Any = _Bot(db_path, [_member(1, booster=True)])
    await pay_game_rewards(bot, GUILD, [1], [1], "chicken")
    # ceil(5*1.5)=8 participation + ceil(20*1.5)=30 win
    assert _bal(db_path, 1) == 38


async def test_pay_dedupes_participants(db_path):
    _enable(db_path)
    bot: Any = _Bot(db_path, [_member(1)])
    await pay_game_rewards(bot, GUILD, [1, 1, 1], [], "chicken")
    assert _bal(db_path, 1) == 5


async def test_pay_coerces_string_ids(db_path):
    # ttl passes stringified user ids (JSON payload keys).
    _enable(db_path)
    bot: Any = _Bot(db_path, [_member(1), _member(2)])
    await pay_game_rewards(bot, GUILD, ["1", "2"], [2], "ttl")
    assert _bal(db_path, 1) == 5
    assert _bal(db_path, 2) == 25


async def test_pay_noop_unknown_guild(db_path):
    _enable(db_path)
    bot: Any = _Bot(db_path, [_member(1)])
    await pay_game_rewards(bot, 999999, [1], [1], "chicken")
    assert _bal(db_path, 1) == 0


# ── end_game payout hook ──────────────────────────────────────────────────────

class _EndBot(_Bot):
    def get_channel(self, cid):
        if cid == CH:
            return SimpleNamespace(id=CH, guild=self._guild)
        return None


async def test_end_game_pays_once_with_resolved_winner(db_path):
    _enable(db_path)
    db = GamesDb(db_path)
    payload = {"guilt_scores": {"1": 1, "2": 3, "3": 0}}  # guiltiest = 2
    gid = await create_game(db, CH, 1, "nhie", payload=payload)
    bot: Any = _EndBot(db_path, [_member(1), _member(2), _member(3)])

    await end_game(db, gid, payload=payload, bot=bot, player_ids=[1, 2, 3])

    assert _bal(db_path, 2) == 25  # participation + win
    assert _bal(db_path, 1) == 5
    assert _bal(db_path, 3) == 5
    assert await get_active_game_by_id(db, gid) is None

    # Second call finds no row → no double payout.
    await end_game(db, gid, payload=payload, bot=bot, player_ids=[1, 2, 3])
    assert _bal(db_path, 2) == 25


async def test_end_game_no_payout_without_bot(db_path):
    _enable(db_path)
    db = GamesDb(db_path)
    gid = await create_game(db, CH, 1, "nhie", payload={})
    await end_game(db, gid, player_ids=[1, 2, 3])  # bot defaults to None
    assert _bal(db_path, 1) == 0


async def test_end_game_no_payout_when_disabled(db_path):
    db = GamesDb(db_path)  # economy left disabled
    payload = {"guilt_scores": {"1": 1, "2": 3}}
    gid = await create_game(db, CH, 1, "nhie", payload=payload)
    bot: Any = _EndBot(db_path, [_member(1), _member(2)])
    await end_game(db, gid, payload=payload, bot=bot, player_ids=[1, 2])
    assert _bal(db_path, 1) == 0
    assert _bal(db_path, 2) == 0
