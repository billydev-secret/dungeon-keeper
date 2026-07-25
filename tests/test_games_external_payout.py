"""Integration test for the Gamebot CAH/Connect 4 payout wiring (#70).

Banks a full game's messages, then drives GamesExternalCog._pay_cah_game /
_pay_connect4_game and asserts each calls the right payout with the right
roster/scores/winner exactly once.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot_modules.cogs.games_external_cog import GamesExternalCog
from bot_modules.services.games_db import GamesDb
from tests.db_template import migrated_db

GUILD, CHAN, GAMEBOT = 111, 900, 620307267241377793
ALICE, BOB, CAROL = 11, 22, 33
OVER_ID = 5001


def _embeds_standings(scores):
    desc = "\n".join(f"<@{u}>: {n}" for u, n in scores.items())
    return [{"title": "Current Standings", "description": desc}]


def _embeds_submissions(uids):
    desc = "\n".join(f"✅ <@{u}> Submitted!" for u in uids)
    return [{"title": "Submission status", "description": desc}]


def _embeds_game_over(winner):
    return [{"title": "Game over!", "description": f"<@{winner}> is the winner!"}]


def _embeds_c4_start(joined):
    return [{
        "title": "host is starting a Connect 4 game!",
        "description": 'Click "Join" below to join in the next **120 seconds**.',
        "fields": [{
            "name": "Joined Players",
            "value": ", ".join(f"<@{u}>" for u in joined),
            "inline": False,
        }],
    }]


def _embeds_c4_game_over(winner):
    return [{"title": "Game over!", "description": f"<@{winner}> has won! ```board```"}]


async def _bank(gdb, mid, ts, embeds):
    await gdb.execute(
        "INSERT INTO games_external_messages "
        "(message_id, guild_id, channel_id, author_id, created_at, embeds_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (mid, GUILD, CHAN, GAMEBOT, ts, json.dumps(embeds)),
    )


def _over_message():
    return SimpleNamespace(
        id=OVER_ID,
        guild=SimpleNamespace(id=GUILD),
        channel=SimpleNamespace(id=CHAN),
        author=SimpleNamespace(id=GAMEBOT),
        created_at=datetime(2026, 7, 21, 1, 8, 36, tzinfo=timezone.utc),
        embeds=[],
    )


@pytest.fixture
def gdb(tmp_path):
    db_path = tmp_path / "t.db"
    migrated_db(db_path)
    return GamesDb(db_path)


@pytest.mark.asyncio
async def test_cah_payout_pays_roster_and_winner_once(gdb):
    await _bank(gdb, 4001, "2026-07-21T01:08:00", _embeds_submissions([ALICE, BOB, CAROL]))
    await _bank(gdb, 4002, "2026-07-21T01:08:20", _embeds_standings({ALICE: 5, BOB: 1, CAROL: 1}))
    await _bank(gdb, OVER_ID, "2026-07-21T01:08:36", _embeds_game_over(ALICE))

    bot = MagicMock()
    bot.games_db = gdb
    cog = GamesExternalCog(bot)

    with patch(
        "bot_modules.cogs.games_external_cog.pay_cah_game_by_score", new=AsyncMock()
    ) as pay:
        await cog._pay_cah_game(_over_message())
        await cog._pay_cah_game(_over_message())  # replayed edit — must not re-pay

    pay.assert_awaited_once()
    args, kwargs = pay.await_args
    assert args[1] == GUILD
    assert args[2] == {ALICE: 5, BOB: 1, CAROL: 1}  # full roster + scores
    assert args[3] == ALICE                          # winner
    assert kwargs["occurrence"] == str(OVER_ID)


@pytest.mark.asyncio
async def test_cah_payout_lone_game_over_pays_the_winner(gdb):
    # A Game over! with no preceding standings still pays: the winner is folded
    # into the roster at score 0.
    await _bank(gdb, OVER_ID, "2026-07-21T01:08:36", _embeds_game_over(ALICE))

    bot = MagicMock()
    bot.games_db = gdb
    cog = GamesExternalCog(bot)

    with patch(
        "bot_modules.cogs.games_external_cog.pay_cah_game_by_score", new=AsyncMock()
    ) as pay:
        await cog._pay_cah_game(_over_message())

    pay.assert_awaited_once()
    args, _ = pay.await_args
    assert args[2] == {ALICE: 0}
    assert args[3] == ALICE


# ── Connect 4 payout (#70 follow-up) ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_connect4_payout_pays_roster_and_winner_once(gdb):
    await _bank(gdb, 4101, "2026-07-21T01:08:00", _embeds_c4_start([ALICE, BOB]))
    await _bank(gdb, OVER_ID, "2026-07-21T01:08:36", _embeds_c4_game_over(ALICE))

    bot = MagicMock()
    bot.games_db = gdb
    cog = GamesExternalCog(bot)

    with patch(
        "bot_modules.cogs.games_external_cog.pay_game_rewards", new=AsyncMock()
    ) as pay:
        await cog._pay_connect4_game(_over_message())
        await cog._pay_connect4_game(_over_message())  # replayed edit — must not re-pay

    pay.assert_awaited_once()
    args, kwargs = pay.await_args
    assert args[1] == GUILD
    assert set(args[2]) == {ALICE, BOB}
    assert args[3] == [ALICE]
    assert args[4] == "connect4"
    assert kwargs["occurrence"] == str(OVER_ID)


@pytest.mark.asyncio
async def test_connect4_payout_unrecognised_finish_pays_participation_only(gdb):
    # A draw's exact wording isn't confirmed yet — the unmatched "Game over!"
    # still pays the roster, just with no winner.
    await _bank(gdb, 4101, "2026-07-21T01:08:00", _embeds_c4_start([ALICE, BOB]))
    draw = [{"title": "Game over!", "description": "It's a draw! ```board```"}]
    await _bank(gdb, OVER_ID, "2026-07-21T01:08:36", draw)

    bot = MagicMock()
    bot.games_db = gdb
    cog = GamesExternalCog(bot)

    with patch(
        "bot_modules.cogs.games_external_cog.pay_game_rewards", new=AsyncMock()
    ) as pay:
        await cog._pay_connect4_game(_over_message())

    pay.assert_awaited_once()
    args, _ = pay.await_args
    assert set(args[2]) == {ALICE, BOB}
    assert args[3] == []  # no winner recognised -> no win bonus


# ── dispatch: one 'gamebot' watch tells CAH and Connect 4 apart ───────────────

def _live_message(mid, embeds_dicts):
    return SimpleNamespace(
        id=mid,
        guild=SimpleNamespace(id=GUILD),
        channel=SimpleNamespace(id=CHAN),
        author=SimpleNamespace(id=GAMEBOT),
        created_at=datetime(2026, 7, 21, 3, 0, 0, tzinfo=timezone.utc),
        edited_at=None,
        content="",
        embeds=[SimpleNamespace(to_dict=lambda d=d: d) for d in embeds_dicts],
    )


@pytest.mark.asyncio
async def test_capture_dispatches_cah_and_connect4_from_one_gamebot_kind(gdb):
    # Both games share a single bot_user_id (and so a single watch row/kind
    # under UNIQUE(guild_id, bot_user_id)) — _capture must tell them apart
    # per-message rather than needing separate watches.
    bot = MagicMock()
    bot.games_db = gdb
    cog = GamesExternalCog(bot)

    with (
        patch.object(cog, "_pay_cah_game", new=AsyncMock()) as pay_cah,
        patch.object(cog, "_pay_connect4_game", new=AsyncMock()) as pay_c4,
    ):
        await cog._capture(_live_message(9001, _embeds_game_over(ALICE)), "gamebot")
        await cog._capture(_live_message(9002, _embeds_c4_game_over(ALICE)), "gamebot")
        # A non-terminal message (mid-game standings) triggers neither payout.
        await cog._capture(
            _live_message(9003, _embeds_standings({ALICE: 1})), "gamebot"
        )

    pay_cah.assert_awaited_once()
    pay_c4.assert_awaited_once()


# ── Cat Bot payout (#65) ──────────────────────────────────────────────────────

CATCHER = 1284869710847934544
CATCH_MSG_ID = 7001

_CAT_CATCH = (
    "efficientpanic cought <:wildcat:1279106513129967750> Wild cat!!!!1!\n"
    "You now have 138 cats of dat type!!!"
)


def _catch_message(content: str, member):
    guild = SimpleNamespace(
        id=GUILD, get_member_named=lambda name: member
    )
    return SimpleNamespace(
        id=CATCH_MSG_ID, guild=guild,
        channel=SimpleNamespace(id=CHAN), author=SimpleNamespace(id=966695034340663367),
        created_at=datetime(2026, 7, 21, 4, 10, tzinfo=timezone.utc),
        content=content, embeds=[],
    )


@pytest.mark.asyncio
async def test_cat_catch_pays_the_resolved_catcher_once(gdb):
    member = SimpleNamespace(id=CATCHER, bot=False)
    bot = MagicMock()
    bot.games_db = gdb
    cog = GamesExternalCog(bot)

    with patch(
        "bot_modules.cogs.games_external_cog.pay_cat_catch", new=AsyncMock()
    ) as pay:
        await cog._pay_cat_catch(_catch_message(_CAT_CATCH, member))
        await cog._pay_cat_catch(_catch_message(_CAT_CATCH, member))  # replay/edit

    pay.assert_awaited_once()
    args, kwargs = pay.await_args
    assert args[1] == GUILD
    assert args[2] == CATCHER
    assert kwargs["rarity"] == "wild"
    assert kwargs["coins"] == 3            # uncommon, not blessed here
    assert kwargs["occurrence"] == str(CATCH_MSG_ID)


@pytest.mark.asyncio
async def test_cat_catch_unresolved_user_pays_nobody(gdb):
    bot = MagicMock()
    bot.games_db = gdb
    cog = GamesExternalCog(bot)

    with patch(
        "bot_modules.cogs.games_external_cog.pay_cat_catch", new=AsyncMock()
    ) as pay:
        await cog._pay_cat_catch(_catch_message(_CAT_CATCH, None))  # name not in guild

    pay.assert_not_awaited()


@pytest.mark.asyncio
async def test_spawn_message_pays_nobody(gdb):
    member = SimpleNamespace(id=CATCHER, bot=False)
    bot = MagicMock()
    bot.games_db = gdb
    cog = GamesExternalCog(bot)

    spawn = "** A <:finecat:1279106515894141019> @Cats! has appeared**\nCatch Fine!"
    with patch(
        "bot_modules.cogs.games_external_cog.pay_cat_catch", new=AsyncMock()
    ) as pay:
        await cog._pay_cat_catch(_catch_message(spawn, member))

    pay.assert_not_awaited()
