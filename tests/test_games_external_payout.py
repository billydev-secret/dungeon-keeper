"""Integration test for the Gamebot payout wiring (#70).

Banks a full game's messages, then drives GamesExternalCog._pay_gamebot_game —
the single entry point every *Game over!* goes through — and asserts it
identifies the sub-game from its lobby embed and calls the right payout with
the right roster/scores/winner exactly once.
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


def _embeds_lobby(game, joined):
    """Gamebot's join-phase embed — the one that names which game this is."""
    return [{
        "title": f"host is starting a {game} game!",
        "description": 'Click "Join" below to join in the next **120 seconds**.',
        "fields": [{
            "name": "Joined Players",
            "value": ", ".join(f"<@{u}>" for u in joined),
            "inline": False,
        }],
    }]


def _embeds_c4_start(joined):
    return _embeds_lobby("Connect 4", joined)


def _embeds_c4_game_over(winner):
    return [{"title": "Game over!", "description": f"<@{winner}> has won! ```board```"}]


def _embeds_scoreboard(points):
    """Anagrams' Scoreboard — scores live in the *field names*, by username."""
    return [{
        "title": "Scoreboard",
        "fields": [
            {"name": f"{name} - {n} POINTS", "value": "WORDS", "inline": False}
            for name, n in points.items()
        ] + [{"name": "Pangram", "value": "The pangram was CLEANUP.", "inline": False}],
    }]


def _embeds_not_enough_players():
    return [{"title": "Time's up!", "description": "Not enough players joined the game!"}]


def _guild(members=None):
    """A guild stub that resolves Anagrams' usernames the way Discord does."""
    by_name = members or {}
    return SimpleNamespace(
        id=GUILD,
        get_member_named=lambda name: by_name.get(name),
        get_member=lambda uid: next(
            (m for m in by_name.values() if m.id == uid), None
        ),
    )


async def _bank(gdb, mid, ts, embeds):
    await gdb.execute(
        "INSERT INTO games_external_messages "
        "(message_id, guild_id, channel_id, author_id, created_at, embeds_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (mid, GUILD, CHAN, GAMEBOT, ts, json.dumps(embeds)),
    )


def _over_message(guild=None):
    return SimpleNamespace(
        id=OVER_ID,
        guild=guild or SimpleNamespace(id=GUILD),
        channel=SimpleNamespace(id=CHAN),
        author=SimpleNamespace(id=GAMEBOT),
        created_at=datetime(2026, 7, 21, 1, 8, 36, tzinfo=timezone.utc),
        embeds=[],
    )


async def _claimed_kinds(gdb):
    rows = await gdb.fetchall(
        "SELECT message_id, kind FROM games_external_payouts", ()
    )
    return {int(r["message_id"]): str(r["kind"]) for r in rows}


@pytest.fixture
def gdb(tmp_path):
    db_path = tmp_path / "t.db"
    migrated_db(db_path)
    return GamesDb(db_path)


@pytest.mark.asyncio
async def test_cah_payout_pays_roster_and_winner_once(gdb):
    await _bank(gdb, 4000, "2026-07-21T01:07:40", _embeds_lobby("Cards Against Humanity", [ALICE, BOB]))
    await _bank(gdb, 4001, "2026-07-21T01:08:00", _embeds_submissions([ALICE, BOB, CAROL]))
    await _bank(gdb, 4002, "2026-07-21T01:08:20", _embeds_standings({ALICE: 5, BOB: 1, CAROL: 1}))
    await _bank(gdb, OVER_ID, "2026-07-21T01:08:36", _embeds_game_over(ALICE))

    bot = MagicMock()
    bot.games_db = gdb
    cog = GamesExternalCog(bot)

    with patch(
        "bot_modules.cogs.games_external_cog.pay_cah_game_by_score", new=AsyncMock()
    ) as pay:
        await cog._pay_gamebot_game(_over_message())
        await cog._pay_gamebot_game(_over_message())  # replayed edit — must not re-pay

    pay.assert_awaited_once()
    args, kwargs = pay.await_args
    assert args[1] == GUILD
    assert args[2] == {ALICE: 5, BOB: 1, CAROL: 1}  # full roster + scores
    assert args[3] == ALICE                          # winner
    assert kwargs["occurrence"] == str(OVER_ID)
    assert (await _claimed_kinds(gdb))[OVER_ID] == "gamebot_cah"


@pytest.mark.asyncio
async def test_cah_payout_lone_game_over_pays_the_winner(gdb):
    # A Game over! with no lobby and no standings left in the banked slice.
    # Nothing identifies the game but the "is the winner!" wording, which CAH
    # shares with Anagrams — CAH is the safe assumption, and the winner is
    # folded into the roster at score 0.
    await _bank(gdb, OVER_ID, "2026-07-21T01:08:36", _embeds_game_over(ALICE))

    bot = MagicMock()
    bot.games_db = gdb
    cog = GamesExternalCog(bot)

    with patch(
        "bot_modules.cogs.games_external_cog.pay_cah_game_by_score", new=AsyncMock()
    ) as pay:
        await cog._pay_gamebot_game(_over_message())

    pay.assert_awaited_once()
    args, _ = pay.await_args
    assert args[2] == {ALICE: 0}
    assert args[3] == ALICE


@pytest.mark.asyncio
async def test_cah_window_stops_at_its_own_lobby(gdb):
    # A previous game's standings sitting in the same channel must not bleed
    # into this game's roster: the backward scan stops at this game's lobby.
    await _bank(gdb, 3900, "2026-07-21T01:00:00", _embeds_standings({CAROL: 9}))
    await _bank(gdb, 4000, "2026-07-21T01:07:40", _embeds_lobby("Cards Against Humanity", [ALICE, BOB]))
    await _bank(gdb, 4002, "2026-07-21T01:08:20", _embeds_standings({ALICE: 5, BOB: 1}))
    await _bank(gdb, OVER_ID, "2026-07-21T01:08:36", _embeds_game_over(ALICE))

    bot = MagicMock()
    bot.games_db = gdb
    cog = GamesExternalCog(bot)

    with patch(
        "bot_modules.cogs.games_external_cog.pay_cah_game_by_score", new=AsyncMock()
    ) as pay:
        await cog._pay_gamebot_game(_over_message())

    args, _ = pay.await_args
    assert args[2] == {ALICE: 5, BOB: 1}  # CAROL's stale 9 is not in this game


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
        await cog._pay_gamebot_game(_over_message())
        await cog._pay_gamebot_game(_over_message())  # replayed edit — must not re-pay

    pay.assert_awaited_once()
    args, kwargs = pay.await_args
    assert args[1] == GUILD
    assert set(args[2]) == {ALICE, BOB}
    assert args[3] == [ALICE]
    assert args[4] == "connect4"
    assert kwargs["occurrence"] == str(OVER_ID)
    assert (await _claimed_kinds(gdb))[OVER_ID] == "gamebot_connect4"


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
        await cog._pay_gamebot_game(_over_message())

    pay.assert_awaited_once()
    args, _ = pay.await_args
    assert set(args[2]) == {ALICE, BOB}
    assert args[3] == []  # no winner recognised -> no win bonus


# ── Anagrams (2026-07-26) ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_anagrams_pays_by_points_not_as_a_one_player_cah_game(gdb):
    # Anagrams ends with the *identical* "<@id> is the winner!" wording CAH
    # uses, so dispatching on the terminal message credited it as a CAH game
    # with a single player at score 0. The lobby embed is what tells them
    # apart. Regression for that bug.
    await _bank(gdb, 4200, "2026-07-21T01:07:00", _embeds_lobby("Anagrams", [ALICE, BOB]))
    await _bank(gdb, 4201, "2026-07-21T01:08:30",
                _embeds_scoreboard({"alice": 900, "bob": 500, "carol": 0}))
    await _bank(gdb, OVER_ID, "2026-07-21T01:08:36", _embeds_game_over(ALICE))

    members = {
        "alice": SimpleNamespace(id=ALICE, bot=False),
        "bob": SimpleNamespace(id=BOB, bot=False),
        "carol": SimpleNamespace(id=CAROL, bot=False),
    }
    bot = MagicMock()
    bot.games_db = gdb
    cog = GamesExternalCog(bot)

    with patch(
        "bot_modules.cogs.games_external_cog.pay_cah_game_by_score", new=AsyncMock()
    ) as pay:
        await cog._pay_gamebot_game(_over_message(_guild(members)))
        await cog._pay_gamebot_game(_over_message(_guild(members)))  # replay

    pay.assert_awaited_once()
    args, kwargs = pay.await_args
    assert args[2] == {ALICE: 900, BOB: 500, CAROL: 0}  # resolved by username
    assert args[3] == ALICE
    assert kwargs["game_key"] == "anagrams"
    assert (await _claimed_kinds(gdb))[OVER_ID] == "gamebot_anagrams"


@pytest.mark.asyncio
async def test_anagrams_unresolvable_players_are_skipped_not_guessed(gdb):
    await _bank(gdb, 4200, "2026-07-21T01:07:00", _embeds_lobby("Anagrams", [ALICE]))
    await _bank(gdb, 4201, "2026-07-21T01:08:30",
                _embeds_scoreboard({"alice": 900, "someone_who_left": 500}))
    await _bank(gdb, OVER_ID, "2026-07-21T01:08:36", _embeds_game_over(ALICE))

    members = {"alice": SimpleNamespace(id=ALICE, bot=False)}
    bot = MagicMock()
    bot.games_db = gdb
    cog = GamesExternalCog(bot)

    with patch(
        "bot_modules.cogs.games_external_cog.pay_cah_game_by_score", new=AsyncMock()
    ) as pay:
        await cog._pay_gamebot_game(_over_message(_guild(members)))

    args, _ = pay.await_args
    assert args[2] == {ALICE: 900}  # the departed player pays nobody


# ── abandoned lobbies and games we don't parse ───────────────────────────────

@pytest.mark.asyncio
async def test_abandoned_lobby_pays_nobody(gdb):
    # "Not enough players joined the game!" still gets a *Game over!* from
    # Gamebot. Nobody played, so nobody is paid — and critically the CAH
    # lobby's Joined Players must not leak into the Connect 4 roster, which is
    # exactly what used to happen.
    await _bank(gdb, 4300, "2026-07-21T01:07:00", _embeds_lobby("Cards Against Humanity", [ALICE, BOB]))
    await _bank(gdb, 4301, "2026-07-21T01:08:00", _embeds_not_enough_players())
    await _bank(gdb, OVER_ID, "2026-07-21T01:08:36",
                [{"title": "Game over!", "description": "To play fun games…"}])

    bot = MagicMock()
    bot.games_db = gdb
    cog = GamesExternalCog(bot)

    with (
        patch("bot_modules.cogs.games_external_cog.pay_game_rewards", new=AsyncMock()) as c4,
        patch("bot_modules.cogs.games_external_cog.pay_cah_game_by_score", new=AsyncMock()) as cah,
    ):
        await cog._pay_gamebot_game(_over_message())

    c4.assert_not_awaited()
    cah.assert_not_awaited()
    # Claimed anyway, so it's never reconsidered.
    assert (await _claimed_kinds(gdb))[OVER_ID] == "gamebot_abandoned"


@pytest.mark.asyncio
async def test_unparsed_gamebot_game_pays_nobody_and_stays_unclaimed(gdb):
    # Chess/Poker/Othello all end in "Game over!" too. Without a parser they
    # must pay nobody — and stay unclaimed, so adding one later can replay them.
    await _bank(gdb, 4400, "2026-07-21T01:07:00", _embeds_lobby("Chess", [ALICE, BOB]))
    await _bank(gdb, OVER_ID, "2026-07-21T01:08:36",
                [{"title": "Game over!", "description": "Good game!"}])

    bot = MagicMock()
    bot.games_db = gdb
    cog = GamesExternalCog(bot)

    with (
        patch("bot_modules.cogs.games_external_cog.pay_game_rewards", new=AsyncMock()) as c4,
        patch("bot_modules.cogs.games_external_cog.pay_cah_game_by_score", new=AsyncMock()) as cah,
    ):
        await cog._pay_gamebot_game(_over_message())

    c4.assert_not_awaited()
    cah.assert_not_awaited()
    assert OVER_ID not in await _claimed_kinds(gdb)


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
async def test_capture_routes_every_terminal_through_one_handler(gdb):
    # Every Gamebot sub-game shares a single bot_user_id and so a single watch
    # kind. _capture routes any *Game over!* — of any sub-game — to
    # _pay_gamebot_game, which does the identifying; non-terminal messages are
    # banked but trigger no payout at all.
    bot = MagicMock()
    bot.games_db = gdb
    cog = GamesExternalCog(bot)

    with patch.object(cog, "_pay_gamebot_game", new=AsyncMock()) as pay:
        await cog._capture(_live_message(9001, _embeds_game_over(ALICE)), "gamebot")
        await cog._capture(_live_message(9002, _embeds_c4_game_over(ALICE)), "gamebot")
        await cog._capture(
            _live_message(9003, _embeds_standings({ALICE: 1})), "gamebot"
        )

    assert pay.await_count == 2


@pytest.mark.asyncio
async def test_watch_cache_is_keyed_on_bot_and_channel(gdb):
    # Keyed on the bot alone, a bot playing in a second channel was silently
    # ignored there. Both channels must resolve; a third must not.
    bot = MagicMock()
    bot.games_db = gdb
    cog = GamesExternalCog(bot)
    cog._watch[GUILD] = {(GAMEBOT, CHAN): "gamebot", (GAMEBOT, CHAN + 1): "gamebot"}

    def _msg(channel_id):
        return SimpleNamespace(
            guild=SimpleNamespace(id=GUILD),
            channel=SimpleNamespace(id=channel_id),
            author=SimpleNamespace(id=GAMEBOT),
        )

    assert cog._watched_kind(_msg(CHAN)) == "gamebot"
    assert cog._watched_kind(_msg(CHAN + 1)) == "gamebot"
    assert cog._watched_kind(_msg(CHAN + 2)) is None


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
