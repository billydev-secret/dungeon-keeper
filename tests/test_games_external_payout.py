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
        # Defaults to a guild that resolves nobody — a real discord.Guild always
        # has get_member_named, which the host lookup calls.
        guild=guild if guild is not None else _guild({}),
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


# ── Wordle + Co-ordle (2026-07-26) ───────────────────────────────────────────

WORDLE_BOT, COORDLE_BOT = 1211781489931452447, 1071892566158614608
DIGEST_ID = 6001


def _digest_message(content, guild, mid=DIGEST_ID):
    return SimpleNamespace(
        id=mid, guild=guild, channel=SimpleNamespace(id=CHAN),
        author=SimpleNamespace(id=WORDLE_BOT),
        created_at=datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc),
        content=content, embeds=[],
    )


def _members_by_id(*members):
    """A guild stub resolving by id and by name, like Discord does."""
    return SimpleNamespace(
        id=GUILD,
        get_member=lambda uid: next((m for m in members if m.id == uid), None),
        get_member_named=lambda n: next(
            (m for m in members if getattr(m, "name", None) == n), None
        ),
    )


@pytest.mark.asyncio
async def test_wordle_digest_pays_by_inverted_score_with_tied_winners(gdb):
    guild = _members_by_id(
        SimpleNamespace(id=ALICE, bot=False, name="alice"),
        SimpleNamespace(id=BOB, bot=False, name="bob"),
        SimpleNamespace(id=CAROL, bot=False, name="carol"),
    )
    content = (
        "**Your group is on a 9 day streak!** 🔥 Here are yesterday's results:\n"
        f"👑 3/6: <@{ALICE}> <@{BOB}>\n"
        f"X/6: <@{CAROL}>"
    )
    bot = MagicMock()
    bot.games_db = gdb
    cog = GamesExternalCog(bot)

    with patch(
        "bot_modules.cogs.games_external_cog.pay_cah_game_by_score", new=AsyncMock()
    ) as pay:
        await cog._pay_wordle_results(_digest_message(content, guild))
        await cog._pay_wordle_results(_digest_message(content, guild))  # replay

    pay.assert_awaited_once()
    args, kwargs = pay.await_args
    assert args[2] == {ALICE: 4, BOB: 4, CAROL: 0}  # 3/6 -> 4, X/6 -> 0
    assert sorted(args[3]) == sorted([ALICE, BOB])  # both crowned players win
    assert kwargs["game_key"] == "wordle"
    assert (await _claimed_kinds(gdb))[DIGEST_ID] == "wordle"


@pytest.mark.asyncio
async def test_wordle_resolves_players_it_printed_as_plain_names(gdb):
    # Wordle mentions some players and prints others as "@Name"; both played.
    guild = _members_by_id(
        SimpleNamespace(id=ALICE, bot=False, name="alice"),
        SimpleNamespace(id=BOB, bot=False, name="Ciccio"),
    )
    content = (
        "Here are yesterday's results:\n"
        f"👑 4/6: @Ciccio <@{ALICE}>\n"
    )
    bot = MagicMock()
    bot.games_db = gdb
    cog = GamesExternalCog(bot)

    with patch(
        "bot_modules.cogs.games_external_cog.pay_cah_game_by_score", new=AsyncMock()
    ) as pay:
        await cog._pay_wordle_results(_digest_message(content, guild))

    args, _ = pay.await_args
    assert args[2] == {ALICE: 3, BOB: 3}
    assert sorted(args[3]) == sorted([ALICE, BOB])  # the named player won too


@pytest.mark.asyncio
async def test_wordle_chatter_pays_nobody(gdb):
    guild = _members_by_id(SimpleNamespace(id=ALICE, bot=False, name="alice"))
    bot = MagicMock()
    bot.games_db = gdb
    cog = GamesExternalCog(bot)

    with patch(
        "bot_modules.cogs.games_external_cog.pay_cah_game_by_score", new=AsyncMock()
    ) as pay:
        await cog._pay_wordle_results(_digest_message("bigprop03 is playing", guild))

    pay.assert_not_awaited()


def _coordle_message(rows, guild, mid, ts=1785103200):
    embed = {
        "title": f"Co-ordle for <t:{ts}:f>",
        "description": "\n".join(rows),
    }
    return SimpleNamespace(
        id=mid, guild=guild, channel=SimpleNamespace(id=CHAN),
        author=SimpleNamespace(id=COORDLE_BOT),
        created_at=datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc),
        content="", embeds=[SimpleNamespace(to_dict=lambda e=embed: e)],
    )


_CO_MISS = "".join(f"<:gray_{c}:9464707331424461{i}>" for i, c in enumerate("spirit"))
_CO_HIT = "".join(f"<:green_{c}:9464707331424461{i}>" for i, c in enumerate("spirit"))
_CO_EMPTY = "<:white_square:946958839192891402>" * 6


@pytest.mark.asyncio
async def test_coordle_pays_once_per_round_not_once_per_guess(gdb):
    # Co-ordle posts a NEW board message for every guess, each showing the whole
    # round. Keyed on a message id this would pay the same round repeatedly, so
    # the claim is keyed on the round's scheduled timestamp instead.
    guild = _members_by_id(
        SimpleNamespace(id=ALICE, bot=False, name="alice"),
        SimpleNamespace(id=BOB, bot=False, name="bob"),
    )
    rows = [
        f"**`1.`** {_CO_MISS} <@!{ALICE}> **+1 (+2)**",
        f"**`2.`** {_CO_HIT} <@!{BOB}> **+5**",
    ]
    bot = MagicMock()
    bot.games_db = gdb
    cog = GamesExternalCog(bot)

    with patch(
        "bot_modules.cogs.games_external_cog.pay_cah_game_by_score", new=AsyncMock()
    ) as pay:
        # Two different board messages, same round — must pay exactly once.
        await cog._pay_coordle_round(_coordle_message(rows, guild, 7001))
        await cog._pay_coordle_round(_coordle_message(rows, guild, 7002))

    pay.assert_awaited_once()
    args, kwargs = pay.await_args
    assert args[2] == {ALICE: 3, BOB: 5}   # the (+2) bonus folds into ALICE's total
    assert args[3] == BOB                  # played the solving row
    assert kwargs["game_key"] == "coordle"
    assert kwargs["occurrence"] == "1785103200"
    assert (await _claimed_kinds(gdb))[1785103200] == "coordle"


@pytest.mark.asyncio
async def test_coordle_open_round_pays_nobody_yet(gdb):
    # Rows still to spare — the round is live, and paying now would settle it
    # early. There is no terminal message, so the board is the only signal.
    guild = _members_by_id(SimpleNamespace(id=ALICE, bot=False, name="alice"))
    rows = [f"**`1.`** {_CO_MISS} <@!{ALICE}> **+1**", f"**`2.`** {_CO_EMPTY}"]
    bot = MagicMock()
    bot.games_db = gdb
    cog = GamesExternalCog(bot)

    with patch(
        "bot_modules.cogs.games_external_cog.pay_cah_game_by_score", new=AsyncMock()
    ) as pay:
        await cog._pay_coordle_round(_coordle_message(rows, guild, 7003))

    pay.assert_not_awaited()
    assert await _claimed_kinds(gdb) == {}


@pytest.mark.asyncio
async def test_coordle_exhausted_round_pays_with_no_winner(gdb):
    guild = _members_by_id(SimpleNamespace(id=ALICE, bot=False, name="alice"))
    rows = [f"**`{i}.`** {_CO_MISS} <@!{ALICE}> **+1**" for i in range(1, 8)]
    bot = MagicMock()
    bot.games_db = gdb
    cog = GamesExternalCog(bot)

    with patch(
        "bot_modules.cogs.games_external_cog.pay_cah_game_by_score", new=AsyncMock()
    ) as pay:
        await cog._pay_coordle_round(_coordle_message(rows, guild, 7004))

    pay.assert_awaited_once()
    args, _ = pay.await_args
    assert args[2] == {ALICE: 7}
    assert args[3] is None  # never solved -> no win bonus


# ── host bounty for external games (2026-07-26) ──────────────────────────────

@pytest.mark.asyncio
async def test_gamebot_game_passes_its_lobby_host_to_the_payout(gdb):
    # External games never passed a host, so they paid the host bounty native
    # party games have always paid — nothing at all. The lobby names them.
    await _bank(gdb, 4000, "2026-07-21T01:07:40",
                _embeds_lobby("Cards Against Humanity", [ALICE, BOB]))
    await _bank(gdb, 4002, "2026-07-21T01:08:20", _embeds_standings({ALICE: 5, BOB: 1}))
    await _bank(gdb, OVER_ID, "2026-07-21T01:08:36", _embeds_game_over(ALICE))

    members = {
        "host": SimpleNamespace(id=CAROL, bot=False),
        "alice": SimpleNamespace(id=ALICE, bot=False),
    }
    bot = MagicMock()
    bot.games_db = gdb
    cog = GamesExternalCog(bot)

    with patch(
        "bot_modules.cogs.games_external_cog.pay_cah_game_by_score", new=AsyncMock()
    ) as pay:
        await cog._pay_gamebot_game(_over_message(_guild(members)))

    _args, kwargs = pay.await_args
    assert kwargs["host_id"] == CAROL   # resolved from "host is starting a …"


@pytest.mark.asyncio
async def test_connect4_and_anagrams_pass_their_host_too(gdb):
    await _bank(gdb, 4101, "2026-07-21T01:08:00", _embeds_c4_start([ALICE, BOB]))
    await _bank(gdb, OVER_ID, "2026-07-21T01:08:36", _embeds_c4_game_over(ALICE))

    members = {"host": SimpleNamespace(id=CAROL, bot=False)}
    bot = MagicMock()
    bot.games_db = gdb
    cog = GamesExternalCog(bot)

    with patch(
        "bot_modules.cogs.games_external_cog.pay_game_rewards", new=AsyncMock()
    ) as pay:
        await cog._pay_gamebot_game(_over_message(_guild(members)))

    _args, kwargs = pay.await_args
    assert kwargs["host_id"] == CAROL


@pytest.mark.asyncio
async def test_unresolvable_host_is_skipped_not_guessed(gdb):
    # A host who has left or renamed pays nobody rather than mis-crediting.
    await _bank(gdb, 4000, "2026-07-21T01:07:40",
                _embeds_lobby("Cards Against Humanity", [ALICE, BOB]))
    await _bank(gdb, 4002, "2026-07-21T01:08:20", _embeds_standings({ALICE: 5, BOB: 1}))
    await _bank(gdb, OVER_ID, "2026-07-21T01:08:36", _embeds_game_over(ALICE))

    bot = MagicMock()
    bot.games_db = gdb
    cog = GamesExternalCog(bot)

    with patch(
        "bot_modules.cogs.games_external_cog.pay_cah_game_by_score", new=AsyncMock()
    ) as pay:
        await cog._pay_gamebot_game(_over_message(_guild({})))  # nobody resolves

    _args, kwargs = pay.await_args
    assert kwargs["host_id"] is None
