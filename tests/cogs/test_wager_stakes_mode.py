"""Wager-only games are announce-only — no nickname stake.

A coin wager with no custom stakes text used to fall through to nickname
mode: creation ran the Manage Nicknames preflight and the result post
carried the rename button. The fix records WAGER_STAKES_TEXT as the game's
stakes at creation, which routes every stakes_text consumer (preflights,
rename gating, embed fallbacks) into announce-only mode. These tests drive
the real creation entrypoints (`_base_challenge` / `_base_lobby`) and the
group resolution seam.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest_asyncio

from bot_modules.cogs.hot_potato import db as hpdb
from bot_modules.cogs.hot_potato.cog import HotPotatoDuel
from bot_modules.cogs.hot_potato_group import db as hpgdb
from bot_modules.cogs.hot_potato_group.cog import HotPotatoGroupGameCog
from bot_modules.core.db_utils import open_db
from bot_modules.duels.base_game import WAGER_STAKES_TEXT
from bot_modules.services.economy_service import (
    apply_credit,
    get_balance,
    save_econ_settings,
)
from bot_modules.services.games_db import GamesDb
from tests.fakes import FakeEconGamesBot, FakeMember, fake_interaction

GUILD = 9001
CH = 100


@pytest_asyncio.fixture
async def db(sync_db_path: Path) -> GamesDb:
    return GamesDb(sync_db_path)


def _seed_economy(sync_db_path: Path, *user_ids: int, amount: int = 500) -> None:
    with open_db(sync_db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
        for uid in user_ids:
            apply_credit(conn, GUILD, uid, amount, "test_seed")


def _creation_interaction(bot: FakeEconGamesBot, user_id: int):
    """Interaction shaped for the creation entrypoints: a real channel_id
    (persisted to sqlite) and an awaitable original_response()."""
    bot.guild.me = None  # accent-color fallback; nick preflight would crash on it
    i = fake_interaction(user=FakeMember(id=user_id), guild=bot.guild, channel_id=CH)
    i.original_response = AsyncMock(return_value=SimpleNamespace(id=555))
    return i


# ── Creation: a wager with no custom stakes is recorded as the stake ───────────
# FakeGuild deliberately has no `.me`: if the nickname preflight ran on these
# wager games (the pre-fix behavior), the calls below would crash on guild.me.

async def test_wager_duel_challenge_records_pot_stakes(db, sync_db_path):
    _seed_economy(sync_db_path, 1)
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]

    await cog._base_challenge(_creation_interaction(bot, 1), FakeMember(id=2), None, wager=50)

    game = await hpdb.get_game(db, 1)
    assert game is not None and game.state == "PENDING"
    assert game.stakes_text == WAGER_STAKES_TEXT


async def test_wager_duel_keeps_explicit_custom_stakes(db, sync_db_path):
    _seed_economy(sync_db_path, 1)
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]

    await cog._base_challenge(
        _creation_interaction(bot, 1), FakeMember(id=2), "loser sings a song", wager=50
    )

    game = await hpdb.get_game(db, 1)
    assert game is not None and game.stakes_text == "loser sings a song"


async def test_wager_lobby_records_pot_stakes_and_escrows_host(db, sync_db_path):
    _seed_economy(sync_db_path, 1)
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2, 3])
    cog = HotPotatoGroupGameCog(bot)  # type: ignore[arg-type]

    await cog._base_lobby(_creation_interaction(bot, 1), None, wager=25)

    game = await hpgdb.get_game(db, 1)
    assert game is not None and game.state == "LOBBY"
    assert game.stakes_text == WAGER_STAKES_TEXT
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, 1) == 475  # host ante escrowed


# ── Resolution: wager stakes resolve announce-only, nickname mode unchanged ────

async def _resolve_group_game(db, sync_db_path, stakes_text):
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2], with_channel=True)
    cog = HotPotatoGroupGameCog(bot)  # type: ignore[arg-type]
    gid = await hpgdb.create_lobby(db, GUILD, CH, 1, stakes_text)
    await hpgdb.set_game_state(
        db, gid, "ACTIVE",
        roster=json.dumps([1, 2]), alive=json.dumps([1, 2]),
        elimination_order=json.dumps([]),
    )
    game = await hpgdb.get_game(db, gid)
    await cog._group_eliminate(game, 1, interaction=None)
    assert bot.channel is not None
    return await hpgdb.get_game(db, gid), bot.channel.sent[-1]


async def test_wager_stakes_game_resolves_without_rename_button(db, sync_db_path):
    game, sent = await _resolve_group_game(db, sync_db_path, WAGER_STAKES_TEXT)
    assert game.state == "RESOLVED_NO_NICK"
    assert "view" not in sent


async def test_nickname_game_still_gets_rename_button(db, sync_db_path):
    game, sent = await _resolve_group_game(db, sync_db_path, None)
    assert game.state == "RESOLVED"
    assert sent.get("view") is not None


# ── Duel timer path — Hot Potato's hand-rolled _explode bypasses
# _finalize_result, so its stake-mode gate needs its own pin.

async def _explode_duel(db, sync_db_path, stakes_text):
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2], with_channel=True)
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    gid = await hpdb.create_game(db, GUILD, CH, 1, 2, stakes_text)
    now = time.time()
    await hpdb.set_game_state(
        db, gid, "ACTIVE",
        holder_id=2, started_at=now - 10.0, timer_seconds=10.0,
        pass_log=json.dumps(
            [{"holder_id": 2, "received_at": now - 3.0, "passed_at": None}]
        ),
        last_action_at=now,
    )
    await cog._explode(gid)
    assert bot.channel is not None
    return await hpdb.get_game(db, gid), bot.channel.sent[-1]


async def test_explode_custom_stakes_resolves_without_rename_button(db, sync_db_path):
    game, sent = await _explode_duel(db, sync_db_path, "loser sings a song")
    assert game.state == "RESOLVED_NO_NICK"
    assert "view" not in sent


async def test_explode_wager_stakes_resolves_without_rename_button(db, sync_db_path):
    game, sent = await _explode_duel(db, sync_db_path, WAGER_STAKES_TEXT)
    assert game.state == "RESOLVED_NO_NICK"
    assert "view" not in sent


async def test_explode_nickname_mode_still_gets_rename_button(db, sync_db_path):
    game, sent = await _explode_duel(db, sync_db_path, None)
    assert game.state == "RESOLVED"
    assert sent.get("view") is not None
