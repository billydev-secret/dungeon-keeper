"""Tests for economy/game_rewards.py and the game_manager.end_game payout hook."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.economy.game_rewards import (
    pay_cah_game_by_score,
    pay_game_rewards,
    pay_mention_award,
    resolve_winners,
)
from bot_modules.games.utils.game_manager import (
    create_game,
    end_game,
    force_end_active_game,
    get_active_game_by_id,
)
from bot_modules.services.economy_quests_service import create_quest, set_quest_active
from bot_modules.services.economy_service import get_balance, save_econ_settings
from bot_modules.services.games_db import GamesDb
from tests.db_template import migrated_db
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
        # ttl — best liar (fooled the most) + best guesser, ties included.
        ("ttl", {"scores": {"1": {"fooled": 2}, "2": {"fooled": 4}}}, [2]),
        ("ttl", {"scores": {}}, []),
        # All-zero entry: nobody was fooled and nobody guessed — no payout.
        ("ttl", {"scores": {"9": {}}}, []),
        # Best guesser pays alongside the liar (guesser 3 never played a round).
        ("ttl", {
            "played": ["1", "2"],
            "scores": {
                "1": {"fooled": 3, "correct_guesses": 0, "total_guessers": 4},
                "2": {"fooled": 1, "correct_guesses": 2, "total_guessers": 4},
                "3": {"fooled": 0, "correct_guesses": 5, "total_guessers": 0},
            },
        }, [1, 3]),
        # Liar tie → both pay (plus the top guesser).
        ("ttl", {
            "played": ["1", "2"],
            "scores": {
                "1": {"fooled": 3, "correct_guesses": 1, "total_guessers": 4},
                "2": {"fooled": 3, "correct_guesses": 0, "total_guessers": 4},
            },
        }, [1, 2]),
        # hottakes — author of the highest-rated take.
        ("hottakes", {"results": [{"avg": 2.0, "author": 10}, {"avg": 3.5, "author": 20}]}, [20]),
        ("hottakes", {"results": [{"avg": 1.0}]}, []),  # author missing
        ("hottakes", {"results": []}, []),
        # rushmore — most votes for best board (history stores str→str).
        ("rushmore", {"votes": {"1": "7", "2": "7", "3": "8"}}, [7]),
        ("rushmore", {"votes": {"1": "7", "2": "8"}}, [7, 8]),  # tie
        ("rushmore", {"votes": {}}, []),
        ("rushmore", {}, []),
        # clapback — highest score; an all-zero board pays nobody.
        ("clapback", {"scores": {"1": 325, "2": 300, "3": 125}}, [1]),
        ("clapback", {"scores": {"1": 0, "2": 0}}, []),
        ("clapback", {"scores": {"1": 100, "2": 100}}, [1, 2]),  # tie
        # mlt — most round crowns.
        ("mlt", {"crowns": {"5": 2, "6": 1}}, [5]),
        ("mlt", {"crowns": {}}, []),
        # price — Most Reasonable (overall): most reasonable-round wins.
        ("price", {"scores": {"reasonable_wins": {"4": 2, "5": 1}, "unhinged_wins": {"5": 3}}}, [4]),
        ("price", {"scores": {"reasonable_wins": {}}}, []),
        ("price", {}, []),
        # wyr's "most divisive" is a question, not a player → no winner.
        ("wyr", {"rounds": {"1": {"a": [1], "b": [2]}}}, []),
        # unknown / no-winner types.
        ("mfk", {"anything": 1}, []),
        ("story", {}, []),
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
    migrated_db(p)
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


def _add_active_quest(db_path, *, trigger_kind: str, reward: int) -> None:
    with open_db(db_path) as conn:
        # Daily: these tests assert a single game completion pays once, which
        # is one-shot behaviour — the only cadence that stays one-shot on a
        # game trigger (weekly/monthly triggered quests must count progress).
        qid = create_quest(
            conn, GUILD, title=trigger_kind, description="", qtype="daily",
            reward=reward, signoff=0, criteria="", starts_at=None, ends_at=None,
            rotate_tag="", community_target=None, created_by=None,
            trigger_kind=trigger_kind,
        )
        set_quest_active(conn, GUILD, qid, True)


async def test_duel_lose_pays_only_non_winners(db_path):
    # A duel resolving fires duel_lose for every participant who didn't win.
    _enable(db_path)
    _add_active_quest(db_path, trigger_kind="duel_lose", reward=30)
    bot: Any = _Bot(db_path, [_member(1), _member(2)])
    await pay_game_rewards(bot, GUILD, [1, 2], [1], "chicken", occurrence="7")
    # winner 1: 5 participation + 20 win, no duel_lose
    assert _bal(db_path, 1) == 25
    # loser 2: 5 participation + 30 duel_lose quest
    assert _bal(db_path, 2) == 35


async def test_duel_lose_not_fired_for_party_games(db_path):
    _enable(db_path)
    _add_active_quest(db_path, trigger_kind="duel_lose", reward=30)
    bot: Any = _Bot(db_path, [_member(1), _member(2)])
    # 'ttl' is a party game, not a duel — nobody "loses" a duel here.
    await pay_game_rewards(bot, GUILD, [1, 2], [1], "ttl", occurrence="7")
    assert _bal(db_path, 2) == 5  # participation only, no duel_lose


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


# ── host bounty ───────────────────────────────────────────────────────────────


async def test_host_bounty_pays_the_host_per_joiner(db_path):
    _enable(db_path, host_bounty_per_joiner=4, host_bounty_cap=5)
    bot: Any = _Bot(db_path, [_member(9), _member(1), _member(2)])
    # Host 9 ran the game; 1 and 2 joined. joiners excludes the host → 2.
    await pay_game_rewards(
        bot, GUILD, [1, 2], [1], "ttl", occurrence="7", host_id=9
    )
    assert _bal(db_path, 9) == 8  # 4 per joiner × 2
    assert _bal(db_path, 1) == 25  # participation + win, unaffected
    assert _bal(db_path, 2) == 5


async def test_host_bounty_stops_when_the_income_source_is_disabled(db_path):
    # The game_host income-source toggle must gate the coin faucet, not just the
    # quest — flipping it off is the documented off-switch for the payout.
    from bot_modules.core.db_utils import open_db as _open_db
    from bot_modules.services.economy_quests_service import set_income_source

    _enable(db_path, host_bounty_per_joiner=4, host_bounty_cap=5)
    with _open_db(db_path) as conn:
        set_income_source(conn, GUILD, "game_host", False)
    bot: Any = _Bot(db_path, [_member(9), _member(1), _member(2)])
    await pay_game_rewards(
        bot, GUILD, [1, 2], [1], "ttl", occurrence="7", host_id=9
    )
    assert _bal(db_path, 9) == 0  # bounty suppressed with the source off
    assert _bal(db_path, 1) == 25  # participation/win still pay (own faucet)


async def test_host_bounty_counts_a_host_who_also_played(db_path):
    _enable(db_path, host_bounty_per_joiner=4, host_bounty_cap=5)
    bot: Any = _Bot(db_path, [_member(9), _member(1)])
    # Host is also on the roster; joiners still excludes them → 1 joiner.
    await pay_game_rewards(
        bot, GUILD, [9, 1], [1], "ttl", occurrence="7", host_id=9
    )
    # 5 participation + 4 host bounty (1 joiner)
    assert _bal(db_path, 9) == 9


async def test_host_bounty_is_the_anti_farm_gate(db_path):
    # A host who started a game nobody joined earns nothing — the whole point.
    _enable(db_path, host_bounty_per_joiner=4, host_bounty_cap=5)
    bot: Any = _Bot(db_path, [_member(9)])
    await pay_game_rewards(
        bot, GUILD, [9], [], "ttl", occurrence="7", host_id=9
    )
    assert _bal(db_path, 9) == 5  # participation only, no host bounty


async def test_host_bounty_absent_without_a_host_id(db_path):
    # Duels and external games pass no host_id — no bounty, no game_host quest.
    _enable(db_path, host_bounty_per_joiner=4, host_bounty_cap=5)
    _add_active_quest(db_path, trigger_kind="game_host", reward=50)
    bot: Any = _Bot(db_path, [_member(1), _member(2)])
    await pay_game_rewards(bot, GUILD, [1, 2], [1], "chicken", occurrence="7")
    assert _bal(db_path, 1) == 25  # no game_host quest reward leaked in
    assert _bal(db_path, 2) == 5


async def test_host_bounty_denied_to_a_bot_host(db_path):
    # The _valid(host) gate must reject a bot (or a departed/unresolvable) host
    # so a bounty can't be minted to a bot that "hosted" a game.
    _enable(db_path, host_bounty_per_joiner=4, host_bounty_cap=5)
    _add_active_quest(db_path, trigger_kind="game_host", reward=50)
    bot: Any = _Bot(db_path, [_member(9, bot=True), _member(1), _member(2)])
    await pay_game_rewards(
        bot, GUILD, [1, 2], [1], "ttl", occurrence="7", host_id=9
    )
    assert _bal(db_path, 9) == 0  # bot host: no bounty, no game_host quest
    assert _bal(db_path, 1) == 25  # players unaffected


async def test_host_bounty_denied_to_a_host_who_left_the_guild(db_path):
    # host_id 404 isn't a member of the guild → _valid fails → no bounty.
    _enable(db_path, host_bounty_per_joiner=4, host_bounty_cap=5)
    bot: Any = _Bot(db_path, [_member(1), _member(2)])  # no member 404
    await pay_game_rewards(
        bot, GUILD, [1, 2], [1], "ttl", occurrence="7", host_id=404
    )
    assert _bal(db_path, 404) == 0
    assert _bal(db_path, 1) == 25


async def test_host_bounty_fires_the_game_host_quest(db_path):
    # The host quest fires only for the host, and only with a joiner present.
    _enable(db_path)  # bounty dark; the quest still fires
    _add_active_quest(db_path, trigger_kind="game_host", reward=50)
    bot: Any = _Bot(db_path, [_member(9), _member(1)])
    await pay_game_rewards(
        bot, GUILD, [1], [1], "ttl", occurrence="7", host_id=9
    )
    assert _bal(db_path, 9) == 50  # host quest paid, no participation (not a player)
    assert _bal(db_path, 1) == 25  # player got participation + win, no host quest


# ── pay_cah_game_by_score ───────────────────────────────────────────────────────

async def test_cah_score_payout_scales_by_ratio(db_path):
    _enable(db_path)  # reward_cah_win_max defaults to 50
    bot: Any = _Bot(db_path, [_member(1), _member(2), _member(3), _member(4)])
    await pay_cah_game_by_score(
        bot, GUILD, {1: 5, 2: 3, 3: 1, 4: 0}, 1, occurrence="9001"
    )
    assert _bal(db_path, 1) == 50  # winner (top score): the full cap
    assert _bal(db_path, 2) == 30  # 50 * 3/5
    assert _bal(db_path, 3) == 10  # 50 * 1/5
    assert _bal(db_path, 4) == 0   # share rounds to 0 -> no credit at all


async def test_cah_score_payout_uses_configured_cap(db_path):
    _enable(db_path, reward_cah_win_max=100)
    bot: Any = _Bot(db_path, [_member(1), _member(2)])
    await pay_cah_game_by_score(bot, GUILD, {1: 4, 2: 2}, 1)
    assert _bal(db_path, 1) == 100
    assert _bal(db_path, 2) == 50


async def test_cah_score_payout_off_when_cap_is_zero(db_path):
    _enable(db_path, reward_cah_win_max=0)
    bot: Any = _Bot(db_path, [_member(1)])
    await pay_cah_game_by_score(bot, GUILD, {1: 5}, 1)
    assert _bal(db_path, 1) == 0


async def test_cah_score_payout_noop_when_disabled(db_path):
    bot: Any = _Bot(db_path, [_member(1)])
    await pay_cah_game_by_score(bot, GUILD, {1: 5}, 1)
    assert _bal(db_path, 1) == 0


async def test_cah_score_payout_all_zero_scores_pays_nobody(db_path):
    # A degenerate game with no points scored has no ratio to scale by.
    _enable(db_path)
    bot: Any = _Bot(db_path, [_member(1), _member(2)])
    await pay_cah_game_by_score(bot, GUILD, {1: 0, 2: 0}, None)
    assert _bal(db_path, 1) == 0
    assert _bal(db_path, 2) == 0


async def test_cah_score_payout_filters_bots_and_unresolvable(db_path):
    _enable(db_path)
    bot: Any = _Bot(db_path, [_member(1), _member(2, bot=True)])
    await pay_cah_game_by_score(bot, GUILD, {1: 5, 2: 3, 999: 1}, 1)
    assert _bal(db_path, 1) == 50
    assert _bal(db_path, 2) == 0
    assert _bal(db_path, 999) == 0


async def test_cah_score_payout_booster_ceils(db_path):
    _enable(db_path)  # booster_multiplier defaults to 1.5
    bot: Any = _Bot(db_path, [_member(1, booster=True)])
    await pay_cah_game_by_score(bot, GUILD, {1: 5}, 1)
    assert _bal(db_path, 1) == 75  # ceil(50 * 1.5)


async def test_cah_score_payout_coerces_string_scores(db_path):
    # The parser hands back int keys, but the coercion mirrors pay_game_rewards
    # for defence-in-depth against a JSON-payload-shaped caller.
    _enable(db_path)
    bot: Any = _Bot(db_path, [_member(1), _member(2)])
    await pay_cah_game_by_score(bot, GUILD, {"1": "5", "2": "3"}, "1")
    assert _bal(db_path, 1) == 50
    assert _bal(db_path, 2) == 30


async def test_cah_score_payout_fires_party_game_and_game_win_triggers(db_path):
    # Checked via the claim table rather than dollar totals — the weekly
    # ⚡ spotlight kind can double a quest's payout, which would make an
    # amount-based assertion flaky depending on the calendar week it runs in.
    _enable(db_path)
    with open_db(db_path) as conn:
        pg_id = create_quest(
            conn, GUILD, title="party_game", description="", qtype="daily",
            reward=7, signoff=0, criteria="", starts_at=None, ends_at=None,
            rotate_tag="", community_target=None, created_by=None,
            trigger_kind="party_game",
        )
        set_quest_active(conn, GUILD, pg_id, True)
        gw_id = create_quest(
            conn, GUILD, title="game_win", description="", qtype="daily",
            reward=13, signoff=0, criteria="", starts_at=None, ends_at=None,
            rotate_tag="", community_target=None, created_by=None,
            trigger_kind="game_win",
        )
        set_quest_active(conn, GUILD, gw_id, True)
    bot: Any = _Bot(db_path, [_member(1), _member(2)])
    # player 2 scores 0 -> no direct coins, but still "played" for the quest.
    await pay_cah_game_by_score(bot, GUILD, {1: 5, 2: 0}, 1, occurrence="9002")
    with open_db(db_path) as conn:
        claims = {
            (row["quest_id"], row["user_id"])
            for row in conn.execute(
                "SELECT quest_id, user_id FROM econ_quest_claims WHERE guild_id = ?",
                (GUILD,),
            )
        }
    assert (pg_id, 1) in claims and (pg_id, 2) in claims  # both "played"
    assert (gw_id, 1) in claims and (gw_id, 2) not in claims  # only the winner
    assert _bal(db_path, 1) >= 50  # at least the direct score credit


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


class _RaceDb(GamesDb):
    """Simulates losing the end_game claim race: the active-game row is deleted
    by a 'concurrent' call in the window between end_game's SELECT and its own
    DELETE. Exercises the exactly-once claim without real threads."""

    def __init__(self, db_path, steal_gid):
        super().__init__(db_path)
        self._steal_gid = steal_gid
        self._stolen = False

    async def fetchone(self, query, params=()):
        row = await super().fetchone(query, params)
        if (
            not self._stolen
            and "SELECT * FROM games_active_games" in query
            and row is not None
            and self._steal_gid in params
        ):
            self._stolen = True  # a concurrent end_game wins the claim first
            await super().execute(
                "DELETE FROM games_active_games WHERE game_id = ?",
                (self._steal_gid,),
            )
        return row


async def test_end_game_does_not_pay_when_it_loses_the_claim_race(db_path):
    # Bug-fix-first: two concurrent end_game calls both pass `if not row`; only
    # the one that actually deletes the active row may pay. The loser (its
    # DELETE claims 0 rows) must credit nothing — participation, host bounty,
    # and the game_host community bump alike.
    _enable(db_path, host_bounty_per_joiner=4, host_bounty_cap=5)
    setup_db = GamesDb(db_path)
    payload = {"guilt_scores": {"1": 1, "2": 3, "3": 0}}
    gid = await create_game(setup_db, CH, 9, "nhie", payload=payload)  # host 9
    bot: Any = _EndBot(db_path, [_member(9), _member(1), _member(2), _member(3)])

    race_db = _RaceDb(db_path, gid)
    await end_game(race_db, gid, payload=payload, bot=bot, player_ids=[1, 2, 3])

    # This call lost the race, so it paid nobody.
    assert _bal(db_path, 1) == 0
    assert _bal(db_path, 2) == 0
    assert _bal(db_path, 3) == 0
    assert _bal(db_path, 9) == 0  # no host bounty either


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


# ── append_payout_footer ──────────────────────────────────────────────────────

def _embed():
    import discord
    return discord.Embed(title="RECAP")


async def test_footer_noop_when_economy_disabled(db_path):
    from bot_modules.economy.game_rewards import append_payout_footer
    bot: Any = _Bot(db_path, [])
    embed = _embed()
    await append_payout_footer(bot, embed, GUILD, "ttl")
    assert embed.footer.text is None


async def test_footer_winner_game_lists_both_amounts(db_path):
    from bot_modules.economy.game_rewards import append_payout_footer
    _enable(db_path)
    bot: Any = _Bot(db_path, [])
    embed = _embed()
    await append_payout_footer(bot, embed, GUILD, "ttl")
    assert embed.footer.text == "🪙 +20 to winners • +5 to everyone who played"


async def test_footer_no_winner_game_lists_participation_only(db_path):
    from bot_modules.economy.game_rewards import append_payout_footer
    _enable(db_path)
    bot: Any = _Bot(db_path, [])
    embed = _embed()
    await append_payout_footer(bot, embed, GUILD, "story")
    assert embed.footer.text == "🪙 +5 to everyone who played"


async def test_footer_appends_below_existing_footer(db_path):
    from bot_modules.economy.game_rewards import append_payout_footer
    _enable(db_path)
    bot: Any = _Bot(db_path, [])
    embed = _embed()
    embed.set_footer(text="🗿 Mt. Rushmore Draft • Hosted by Billy")
    await append_payout_footer(bot, embed, GUILD, "rushmore")
    assert embed.footer.text == (
        "🗿 Mt. Rushmore Draft • Hosted by Billy\n"
        "🪙 +20 to winners • +5 to everyone who played"
    )


async def test_footer_drops_custom_currency_emoji(db_path):
    """A custom <:coin:id> renders as raw text in a footer, so it's dropped."""
    from bot_modules.economy.game_rewards import append_payout_footer
    _enable(db_path, currency_emoji="<:doubloon:999>")
    bot: Any = _Bot(db_path, [])
    embed = _embed()
    await append_payout_footer(bot, embed, GUILD, "ttl")
    assert embed.footer.text == "+20 to winners • +5 to everyone who played"
    assert "<:doubloon:999>" not in (embed.footer.text or "")


def test_footer_emoji_passes_unicode_and_strips_custom():
    from bot_modules.services.embeds import footer_emoji

    assert footer_emoji("🪙") == "🪙"
    assert footer_emoji("<:doubloon:999>") == ""
    assert footer_emoji("<a:spin:12345>", "⭐") == "⭐"
    assert footer_emoji("⭐", "🪙") == "⭐"


async def test_footer_respects_configured_amounts(db_path):
    from bot_modules.economy.game_rewards import append_payout_footer
    _enable(db_path, reward_game_win=50, reward_game_participation=0)
    bot: Any = _Bot(db_path, [])
    embed = _embed()
    await append_payout_footer(bot, embed, GUILD, "rushmore")
    assert embed.footer.text == "🪙 +50 to winners"


# ── force_end_active_game payout ──────────────────────────────────────────────

class _ForceBot(_EndBot):
    """_EndBot plus the active_views map force_end pokes and pops."""

    def __init__(self, db_path, members):
        super().__init__(db_path, members)
        self.active_views: dict = {}


async def test_force_end_pays_the_room(db_path):
    """`/games end` is the normal close for several games, not just an abort, so
    it credits the same roster the game's own completion site would have.
    """
    _enable(db_path)
    db = GamesDb(db_path)
    payload = {"players": [1, 2, 3], "round_history": [{}, {}]}
    gid = await create_game(db, CH, 1, "clapback", payload=payload)
    bot: Any = _ForceBot(db_path, [_member(1), _member(2), _member(3)])

    await force_end_active_game(bot, db, gid)

    assert _bal(db_path, 2) == 5
    assert _bal(db_path, 3) == 5
    assert await get_active_game_by_id(db, gid) is None


async def test_force_end_archives_the_reconstructed_roster(db_path):
    """The history row must reflect what was paid, not the zeros the bare
    end_game call used to write.
    """
    _enable(db_path)
    db = GamesDb(db_path)
    payload = {"rounds": {"1": {"a": [1, 2], "b": [3]}}}
    gid = await create_game(db, CH, 1, "wyr", payload=payload)
    bot: Any = _ForceBot(db_path, [_member(1), _member(2), _member(3)])

    await force_end_active_game(bot, db, gid)

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT player_count, round_count, guild_id FROM games_game_history "
            "WHERE game_id = ?", (gid,),
        ).fetchone()
    assert row["player_count"] == 3
    assert row["round_count"] == 1
    assert row["guild_id"] == GUILD


async def test_force_end_pays_nobody_for_a_game_that_never_got_going(db_path):
    """Aborting an empty lobby still costs nothing — the roster is empty, which
    is the whole gate.
    """
    _enable(db_path)
    db = GamesDb(db_path)
    gid = await create_game(db, CH, 1, "clapback", payload={"players": []})
    bot: Any = _ForceBot(db_path, [_member(1)])

    await force_end_active_game(bot, db, gid)

    assert _bal(db_path, 1) == 0
    assert await get_active_game_by_id(db, gid) is None


async def test_force_end_pays_nobody_for_a_prompt_style_game(db_path):
    """ffa/photo have no joined roster, so force-ending one credits nobody."""
    _enable(db_path)
    db = GamesDb(db_path)
    gid = await create_game(db, CH, 1, "ffa", payload={"prompt": "x"})
    bot: Any = _ForceBot(db_path, [_member(1)])

    await force_end_active_game(bot, db, gid)

    assert _bal(db_path, 1) == 0


async def test_force_end_racing_the_games_own_close_pays_once(db_path):
    """A host pressing End as a mod runs /games end: end_game's DELETE claim
    means whichever lands first pays, and the loser is a no-op.
    """
    _enable(db_path)
    db = GamesDb(db_path)
    payload = {"players": [1, 2]}
    gid = await create_game(db, CH, 1, "clapback", payload=payload)
    bot: Any = _ForceBot(db_path, [_member(1), _member(2)])

    await force_end_active_game(bot, db, gid)
    before = _bal(db_path, 2)
    await end_game(db, gid, payload=payload, bot=bot, player_ids=[1, 2])
    assert _bal(db_path, 2) == before


async def test_force_end_survives_a_corrupt_payload(db_path):
    """An unreadable payload costs that game its roster, not the teardown."""
    _enable(db_path)
    db = GamesDb(db_path)
    gid = await create_game(db, CH, 1, "clapback", payload={"players": [1, 2]})
    await db.execute(
        "UPDATE games_active_games SET payload = ? WHERE game_id = ?", ("{not json", gid),
    )
    bot: Any = _ForceBot(db_path, [_member(1), _member(2)])

    await force_end_active_game(bot, db, gid)

    assert await get_active_game_by_id(db, gid) is None
    assert _bal(db_path, 2) == 0


# ── pay_mention_award return contract ────────────────────────────────
#
# The cog burns the one-time payout claim BEFORE calling this faucet, so it
# must be able to tell whether a credit actually landed — a silent no-op
# (economy off, member unresolvable) would otherwise leave the claim row in
# place and the member permanently unpaid (2026-08 review finding).


async def test_mention_award_pays_and_reports_true(db_path):
    _enable(db_path)
    bot: Any = _Bot(db_path, [_member(1)])
    paid = await pay_mention_award(
        bot, GUILD, 1, coins=250, rule_id=7, occurrence="mention_award:1"
    )
    assert paid is True
    assert _bal(db_path, 1) == 250


async def test_mention_award_reports_false_when_disabled(db_path):
    bot: Any = _Bot(db_path, [_member(1)])
    paid = await pay_mention_award(
        bot, GUILD, 1, coins=250, rule_id=7, occurrence="mention_award:1"
    )
    assert paid is False
    assert _bal(db_path, 1) == 0


async def test_mention_award_reports_false_for_unresolvable_member(db_path):
    _enable(db_path)
    bot: Any = _Bot(db_path, [_member(1)])
    assert await pay_mention_award(
        bot, GUILD, 999, coins=250, rule_id=7, occurrence="mention_award:2"
    ) is False


async def test_mention_award_reports_false_for_bot_member(db_path):
    _enable(db_path)
    bot: Any = _Bot(db_path, [_member(1, bot=True)])
    assert await pay_mention_award(
        bot, GUILD, 1, coins=250, rule_id=7, occurrence="mention_award:3"
    ) is False
    assert _bal(db_path, 1) == 0
