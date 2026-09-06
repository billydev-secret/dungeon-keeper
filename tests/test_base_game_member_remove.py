"""A member who leaves the server is taken out of every game they were in
(duels-party-127).

The economy cog already refunded a leaver's escrow; what nobody did was drop
them from the roster, so a lobby or a live round that included them stalled
until the abandonment sweep. ``BaseGame.on_member_remove`` now: refunds and
removes them from a lobby (closing it if they hosted), removes them from a
live round (resolving it if one player is left), voids a duel they were in,
expires a challenge they were party to, and concludes an unnamed result they
were the winner or loser of — saying why.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import discord
import pytest
import pytest_asyncio

from bot_modules.cogs.chicken import db as chdb
from bot_modules.cogs.chicken.cog import ChickenCog
from bot_modules.cogs.hot_potato import db as hpdb
from bot_modules.cogs.hot_potato.cog import HotPotatoDuel
from bot_modules.cogs.hot_potato_group import db as hpgdb
from bot_modules.cogs.hot_potato_group.cog import HotPotatoGroupGameCog
from bot_modules.core.db_utils import open_db
from bot_modules.duels import db as duels_db
from bot_modules.services import economy_wager_service as wager_svc
from bot_modules.services.economy_service import apply_credit, get_balance, save_econ_settings
from bot_modules.services.games_db import GamesDb
from tests.fakes import FakeEconGamesBot

GUILD = 9001
CH = 100


@pytest_asyncio.fixture
async def db(sync_db_path: Path) -> GamesDb:
    return GamesDb(sync_db_path)


@pytest.fixture(autouse=True)
def _stub_accent():
    with patch(
        "bot_modules.core.branding.resolve_accent_color",
        new=AsyncMock(return_value=discord.Color.blurple()),
    ):
        yield


def _fund_and_stake(db_path: Path, game_type: str, game_id: int, *users: int, amount: int = 50):
    with open_db(db_path) as conn:
        save_econ_settings(
            conn, GUILD,
            {"enabled": True, "reward_game_participation": 0, "reward_game_win": 0},
        )
        for uid in users:
            apply_credit(conn, GUILD, uid, 100, "test_seed")
            wager_svc.hold_stake(conn, GUILD, game_type, game_id, uid, amount)


def _balances(db_path: Path, *users: int) -> list[int]:
    with open_db(db_path) as conn:
        return [get_balance(conn, GUILD, u) for u in users]


# ── lobbies ───────────────────────────────────────────────────────────────────


async def test_a_leaver_is_dropped_from_the_lobby_and_refunded(db, sync_db_path):
    bot = FakeEconGamesBot(db, sync_db_path, [1, 3])
    cog = HotPotatoGroupGameCog(bot)  # type: ignore[arg-type]
    gid = await hpgdb.create_lobby(db, GUILD, CH, 1, None)
    await hpgdb.set_game_state(db, gid, "LOBBY", message_id=555, roster=json.dumps([1, 2, 3]))
    _fund_and_stake(sync_db_path, "hot_potato_group", gid, 1, 2, 3)

    await cog._drop_leaver(GUILD, 2)

    game = await hpgdb.get_game(db, gid)
    assert game.state == "LOBBY" and game.roster == [1, 3]
    assert _balances(sync_db_path, 1, 2, 3) == [50, 100, 50]


async def test_the_host_leaving_closes_the_lobby_and_refunds_everyone(db, sync_db_path):
    bot = FakeEconGamesBot(db, sync_db_path, [2, 3])
    cog = HotPotatoGroupGameCog(bot)  # type: ignore[arg-type]
    gid = await hpgdb.create_lobby(db, GUILD, CH, 1, None)
    await hpgdb.set_game_state(db, gid, "LOBBY", message_id=555, roster=json.dumps([1, 2, 3]))
    _fund_and_stake(sync_db_path, "hot_potato_group", gid, 1, 2, 3)

    await cog._drop_leaver(GUILD, 1)

    assert (await hpgdb.get_game(db, gid)).state == "EXPIRED_LOBBY"
    assert _balances(sync_db_path, 1, 2, 3) == [100, 100, 100]


async def test_a_leaver_who_was_not_in_the_lobby_changes_nothing(db, sync_db_path):
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoGroupGameCog(bot)  # type: ignore[arg-type]
    gid = await hpgdb.create_lobby(db, GUILD, CH, 1, None)
    await hpgdb.set_game_state(db, gid, "LOBBY", message_id=555, roster=json.dumps([1, 2]))
    await cog._drop_leaver(GUILD, 9)
    assert (await hpgdb.get_game(db, gid)).roster == [1, 2]


# ── live rounds ───────────────────────────────────────────────────────────────


async def _climbing(db, roster: list[int], *, stakes: str | None = None):
    gid = await chdb.create_lobby(db, GUILD, CH, roster[0], stakes)
    await chdb.set_game_state(
        db, gid, "ACTIVE", phase="CLIMBING",
        roster=json.dumps(roster), alive=json.dumps(roster), bail_log="[]",
        climb_started_at=time.time() - 5.0, climb_duration=25.0,
    )
    return gid


async def test_a_leaver_is_dropped_from_a_live_round_that_continues(db, sync_db_path):
    bot = FakeEconGamesBot(db, sync_db_path, [1, 3])
    cog = ChickenCog(bot)  # type: ignore[arg-type]
    gid = await _climbing(db, [1, 2, 3])

    await cog._drop_leaver(GUILD, 2)

    game = await chdb.get_game(db, gid)
    assert game.state == "ACTIVE"
    assert game.alive == [1, 3] and game.elimination_order == [2]


@pytest.mark.parametrize(
    ("stakes", "final_state", "reason"),
    [
        pytest.param("loser sings", "RESOLVED_NO_NICK", None, id="custom-stakes"),
        pytest.param(None, "NO_NICK_SET", "loser_left", id="nickname-game"),
    ],
)
async def test_the_last_player_standing_wins_when_the_other_leaves(
    db, sync_db_path, stakes, final_state, reason
):
    bot = FakeEconGamesBot(db, sync_db_path, [1])
    cog = ChickenCog(bot)  # type: ignore[arg-type]
    gid = await _climbing(db, [1, 2], stakes=stakes)
    _fund_and_stake(sync_db_path, "chicken", gid, 1, 2)

    await cog._drop_leaver(GUILD, 2)

    game = await chdb.get_game(db, gid)
    assert game.state == final_state
    assert (game.winner_id, game.loser_id) == (1, 2)
    assert await duels_db.get_nick_reason(db, "chicken", gid) == reason
    # The leaver's stake came back to them; the winner's own came back via the pot.
    assert _balances(sync_db_path, 1, 2) == [100, 100]


# ── duels ─────────────────────────────────────────────────────────────────────


async def test_a_duelist_leaving_calls_the_game_off_and_refunds_both(db, sync_db_path):
    bot = FakeEconGamesBot(db, sync_db_path, [2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    gid = await hpdb.create_game(db, GUILD, CH, 1, 2, None)
    await hpdb.set_game_state(
        db, gid, "ACTIVE", holder_id=1, started_at=time.time(), timer_seconds=30.0,
        last_action_at=time.time(),
    )
    _fund_and_stake(sync_db_path, "hot_potato", gid, 1, 2)

    await cog._drop_leaver(GUILD, 1)

    assert (await hpdb.get_game(db, gid)).state == "VOID"
    assert _balances(sync_db_path, 1, 2) == [100, 100]


async def test_a_pending_challenge_expires_when_either_party_leaves(db, sync_db_path):
    bot = FakeEconGamesBot(db, sync_db_path, [1])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    gid = await hpdb.create_game(db, GUILD, CH, 1, 2, None)
    await hpdb.set_game_state(db, gid, "PENDING", message_id=555)

    await cog._drop_leaver(GUILD, 2)

    assert (await hpdb.get_game(db, gid)).state == "EXPIRED_PENDING"


@pytest.mark.parametrize(
    ("leaver", "reason"),
    [
        pytest.param(2, "loser_left", id="loser-leaves"),
        pytest.param(1, "winner_left", id="winner-leaves"),
    ],
)
async def test_an_unnamed_result_concludes_when_a_party_leaves(db, sync_db_path, leaver, reason):
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    gid = await hpdb.create_game(db, GUILD, CH, 1, 2, None, nick_stake=True)
    await hpdb.set_game_state(
        db, gid, "RESOLVED", winner_id=1, loser_id=2, resolved_at=time.time(),
    )

    await cog._drop_leaver(GUILD, leaver)

    assert (await hpdb.get_game(db, gid)).state == "NO_NICK_SET"
    assert await duels_db.get_nick_reason(db, "hot_potato", gid) == reason


async def test_a_leaver_in_another_guild_is_left_alone(db, sync_db_path):
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    gid = await hpdb.create_game(db, GUILD, CH, 1, 2, None)
    await hpdb.set_game_state(db, gid, "ACTIVE", holder_id=1, last_action_at=time.time())

    await cog._drop_leaver(GUILD + 1, 1)

    assert (await hpdb.get_game(db, gid)).state == "ACTIVE"


async def test_a_leaving_holder_hands_the_bomb_on(db, sync_db_path):
    """Hot Potato (Group): the round state points at the holder, so a leaver
    who was holding would have left the bomb in nobody's hands until the
    fuse blew — and the detonation would have eliminated them a second time."""
    bot = FakeEconGamesBot(db, sync_db_path, [1, 3])
    cog = HotPotatoGroupGameCog(bot)  # type: ignore[arg-type]
    gid = await hpgdb.create_lobby(db, GUILD, CH, 1, None)
    now = time.time()
    await hpgdb.set_game_state(
        db, gid, "ACTIVE", roster=json.dumps([1, 2, 3]), alive=json.dumps([1, 2, 3]),
        holder_id=2, fuse_seconds=30.0, phase_started_at=now, last_action_at=now,
        pass_log=json.dumps([{"holder_id": 2, "received_at": now, "passed_at": None}]),
    )

    await cog._drop_leaver(GUILD, 2)

    game = await hpgdb.get_game(db, gid)
    assert game.state == "ACTIVE"
    assert game.alive == [1, 3] and game.elimination_order == [2]
    assert game.holder_id == 3  # the next player clockwise from the leaver
    assert game.pass_log[-1]["holder_id"] == 3
    assert game.pass_log[-2]["passed_at"] is not None


async def test_a_leaver_who_was_not_holding_leaves_the_bomb_where_it_is(db, sync_db_path):
    bot = FakeEconGamesBot(db, sync_db_path, [1, 3])
    cog = HotPotatoGroupGameCog(bot)  # type: ignore[arg-type]
    gid = await hpgdb.create_lobby(db, GUILD, CH, 1, None)
    now = time.time()
    await hpgdb.set_game_state(
        db, gid, "ACTIVE", roster=json.dumps([1, 2, 3]), alive=json.dumps([1, 2, 3]),
        holder_id=1, fuse_seconds=30.0, phase_started_at=now, last_action_at=now,
        pass_log=json.dumps([{"holder_id": 1, "received_at": now, "passed_at": None}]),
    )

    await cog._drop_leaver(GUILD, 2)

    game = await hpgdb.get_game(db, gid)
    assert game.alive == [1, 3] and game.holder_id == 1
    assert len(game.pass_log) == 1
