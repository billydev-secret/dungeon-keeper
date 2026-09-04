"""What ``end_game`` records — and what it does not pay.

Three review findings live here. platform-19: every bare ``end_game`` (no
bot) archived ``guild_id = 0`` — 51 prod rows invisible to the dashboard — so
the guild is now stamped at ``create_game`` and copied on archive. platform-20:
a bare call archived ``player_count = 0`` and an empty payload even though the
stored payload was right there, so the roster is read back out of it —
recording only, never paying. platform-22: ``/recap`` saw one game and one
player, because only the host ever reached the session and the 30-minute
window never moved during a live game.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.games.utils import game_manager
from bot_modules.games.utils.game_manager import (
    GameEnd,
    create_game,
    end_game,
    get_active_game_by_id,
    modify_payload,
    touch_session,
    update_session,
)
from bot_modules.services.economy_service import get_balance, save_econ_settings
from bot_modules.services.games_db import GamesDb
from tests.db_template import migrated_db
from tests.fakes import FakeGuild

GUILD = 4242
CH = 700
OTHER_CH = 701


def _member(uid: int):
    return SimpleNamespace(id=uid, bot=False, premium_since=None, display_name=f"U{uid}")


class _Bot:
    def __init__(self, db_path, members):
        self.ctx = SimpleNamespace(db_path=db_path)
        self._guild = FakeGuild(id=GUILD, members={m.id: m for m in members})

    def get_guild(self, gid):
        return self._guild if gid == GUILD else None

    def get_channel(self, cid):
        return SimpleNamespace(id=cid, guild=self._guild) if cid == CH else None


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "test.db"
    migrated_db(p)
    return p


@pytest.fixture(autouse=True)
def _fresh_touch_cache():
    game_manager._session_touched.clear()
    yield
    game_manager._session_touched.clear()


def _enable(db_path):
    with open_db(db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})


def _bal(db_path, uid: int) -> int:
    with open_db(db_path) as conn:
        return get_balance(conn, GUILD, uid)


def _history(db_path, gid: str):
    with open_db(db_path) as conn:
        return conn.execute(
            "SELECT * FROM games_game_history WHERE game_id = ?", (gid,)
        ).fetchone()


def _sessions(db_path, channel_id: int = CH):
    with open_db(db_path) as conn:
        return conn.execute(
            "SELECT * FROM games_session_tracker WHERE channel_id = ?"
            " ORDER BY last_game_at", (channel_id,),
        ).fetchall()


async def _allow(db: GamesDb, channel_id: int = CH) -> None:
    await db.execute(
        "INSERT INTO games_allowed_channels (channel_id, guild_id) VALUES (?, ?)",
        (channel_id, GUILD),
    )


async def _row_guild(db: GamesDb, gid: str) -> int:
    row = await get_active_game_by_id(db, gid)
    assert row is not None
    return int(row["guild_id"])


# ── platform-19: the guild is stamped at creation ────────────────────────────


async def test_create_game_stamps_the_guild_it_is_given(db_path):
    db = GamesDb(db_path)
    gid = await create_game(db, CH, 1, "wyr", guild_id=GUILD)
    assert await _row_guild(db, gid) == GUILD


async def test_create_game_falls_back_to_the_channel_allowlist(db_path):
    """An untouched launcher that passes no guild still lands the right one
    when the channel is on the allowlist — which every games channel is."""
    db = GamesDb(db_path)
    await _allow(db)
    gid = await create_game(db, CH, 1, "wyr")
    assert await _row_guild(db, gid) == GUILD


async def test_create_game_records_zero_when_nothing_knows_the_guild(db_path):
    db = GamesDb(db_path)
    gid = await create_game(db, OTHER_CH, 1, "wyr")
    assert await _row_guild(db, gid) == 0


async def test_a_bare_end_game_copies_the_stored_guild(db_path):
    """The regression: no bot, no lookup — and still not 0."""
    db = GamesDb(db_path)
    gid = await create_game(db, OTHER_CH, 1, "wyr", guild_id=GUILD)
    await end_game(db, gid)
    assert _history(db_path, gid)["guild_id"] == GUILD


async def test_a_zero_guild_row_is_rederived_from_the_bot(db_path):
    db = GamesDb(db_path)
    gid = await create_game(db, CH, 1, "wyr")  # nothing knows the guild yet
    assert await _row_guild(db, gid) == 0
    await end_game(db, gid, bot=_Bot(db_path, []))
    assert _history(db_path, gid)["guild_id"] == GUILD


async def test_a_zero_guild_row_is_rederived_from_the_allowlist_without_a_bot(db_path):
    db = GamesDb(db_path)
    gid = await create_game(db, CH, 1, "wyr")
    await _allow(db)  # allow-listed after creation, so the row still says 0
    assert await _row_guild(db, gid) == 0
    await end_game(db, gid)
    assert _history(db_path, gid)["guild_id"] == GUILD


# ── platform-20: a bare end records what the payload says ────────────────────


async def test_a_bare_end_game_archives_the_stored_payload_and_roster(db_path):
    db = GamesDb(db_path)
    payload = {"participants": [1, 2, 3], "asked": {"1": "q1", "2": "q2"}}
    gid = await create_game(db, CH, 1, "traditional", payload=payload, guild_id=GUILD)

    ended = await end_game(db, gid)

    row = _history(db_path, gid)
    assert row["player_count"] == 3
    assert row["round_count"] == 2
    assert json.loads(row["payload"])["participants"] == [1, 2, 3]
    assert ended == GameEnd(
        game_id=gid, game_type="traditional", guild_id=GUILD,
        player_count=3, round_count=2, coins_paid=0,
    )


async def test_recording_the_roster_never_pays_it(db_path):
    """The whole point of the split: a lobby timeout or crash cleanup that
    passes a bot but no explicit roster records the players and pays nobody."""
    _enable(db_path)
    db = GamesDb(db_path)
    payload = {"participants": [1, 2, 3], "asked": {"1": "q1"}}
    gid = await create_game(db, CH, 1, "traditional", payload=payload, guild_id=GUILD)
    bot: Any = _Bot(db_path, [_member(1), _member(2), _member(3)])

    ended = await end_game(db, gid, bot=bot)

    assert _history(db_path, gid)["player_count"] == 3
    assert ended is not None and ended.coins_paid == 0
    assert _bal(db_path, 1) == _bal(db_path, 2) == _bal(db_path, 3) == 0


async def test_an_explicit_count_is_kept_as_given(db_path):
    db = GamesDb(db_path)
    payload = {"participants": [1, 2, 3]}
    gid = await create_game(db, CH, 1, "traditional", payload=payload, guild_id=GUILD)
    await end_game(db, gid, player_count=2, round_count=7)
    row = _history(db_path, gid)
    assert (row["player_count"], row["round_count"]) == (2, 7)


async def test_an_explicit_empty_roster_records_zero(db_path):
    """``player_ids=[]`` is a statement (nobody played), not an omission."""
    db = GamesDb(db_path)
    payload = {"participants": [1, 2]}
    gid = await create_game(db, CH, 1, "traditional", payload=payload, guild_id=GUILD)
    await end_game(db, gid, player_ids=[])
    assert _history(db_path, gid)["player_count"] == 0


async def test_a_passed_payload_wins_over_the_stored_one(db_path):
    db = GamesDb(db_path)
    gid = await create_game(db, CH, 1, "traditional", payload={"participants": [1]}, guild_id=GUILD)
    await end_game(db, gid, payload={"participants": [1, 2, 3, 4]})
    row = _history(db_path, gid)
    assert row["player_count"] == 4
    assert json.loads(row["payload"])["participants"] == [1, 2, 3, 4]


async def test_a_corrupt_stored_payload_still_archives(db_path):
    db = GamesDb(db_path)
    gid = await create_game(db, CH, 1, "traditional", guild_id=GUILD)
    await db.execute(
        "UPDATE games_active_games SET payload = ? WHERE game_id = ?", ("{nope", gid),
    )
    ended = await end_game(db, gid)
    assert ended is not None and ended.player_count == 0
    assert json.loads(_history(db_path, gid)["payload"]) == {}


# ── clapback-9: the reason lands in the archive ──────────────────────────────


@pytest.mark.parametrize("reason", ["lobby_timeout", "crash", "expired"])
async def test_the_reason_is_archived_without_touching_the_callers_dict(db_path, reason):
    db = GamesDb(db_path)
    payload = {"players": [1, 2]}
    gid = await create_game(db, CH, 1, "clapback", payload=payload, guild_id=GUILD)
    await end_game(db, gid, payload=payload, reason=reason)
    archived = json.loads(_history(db_path, gid)["payload"])
    assert archived["reason"] == reason
    assert archived["players"] == [1, 2]
    assert "reason" not in payload


async def test_no_reason_means_no_key(db_path):
    db = GamesDb(db_path)
    gid = await create_game(db, CH, 1, "clapback", payload={"players": [1]}, guild_id=GUILD)
    await end_game(db, gid)
    assert "reason" not in json.loads(_history(db_path, gid)["payload"])


# ── the return value ─────────────────────────────────────────────────────────


async def test_a_paying_end_reports_the_coins_it_paid(db_path):
    _enable(db_path)
    db = GamesDb(db_path)
    payload = {"participants": [1, 2, 3]}
    gid = await create_game(db, CH, 1, "traditional", payload=payload, guild_id=GUILD)
    bot: Any = _Bot(db_path, [_member(1), _member(2), _member(3)])

    ended = await end_game(db, gid, payload=payload, bot=bot, player_ids=[1, 2, 3])

    assert ended is not None
    paid = _bal(db_path, 1) + _bal(db_path, 2) + _bal(db_path, 3)
    assert paid > 0
    assert ended.coins_paid == paid
    assert ended.player_count == 3


async def test_the_loser_of_the_claim_race_gets_none(db_path):
    db = GamesDb(db_path)
    gid = await create_game(db, CH, 1, "traditional", guild_id=GUILD)
    assert await end_game(db, gid) is not None
    assert await end_game(db, gid) is None


# ── platform-22: the session is the memory of the night ──────────────────────


async def test_a_bare_end_merges_the_roster_into_the_session(db_path):
    """The start-time call only knows the host; the end used to add nobody
    unless the game paid. Now every rostered end merges the room."""
    db = GamesDb(db_path)
    gid = await create_game(db, CH, 1, "wyr", guild_id=GUILD)
    await update_session(db, CH, gid, [1])
    await db.execute(
        "UPDATE games_active_games SET payload = ? WHERE game_id = ?",
        (json.dumps({"rounds": {"1": {"a": [1, 2], "b": [3]}}}), gid),
    )

    await end_game(db, gid)

    rows = _sessions(db_path)
    assert len(rows) == 1
    assert sorted(json.loads(rows[0]["player_ids"])) == [1, 2, 3]
    assert json.loads(rows[0]["game_ids"]) == [gid]


async def test_an_end_with_no_roster_opens_no_session(db_path):
    db = GamesDb(db_path)
    gid = await create_game(db, CH, 1, "wyr", guild_id=GUILD)
    await end_game(db, gid)
    assert _sessions(db_path) == []


@pytest.mark.parametrize("game_type", ["photo", "ffa"])
async def test_a_bot_post_never_opens_a_session(db_path, game_type):
    """The daily photo post used to open a one-player 'session' every day."""
    db = GamesDb(db_path)
    gid = await create_game(db, CH, 1, game_type, payload={"prompt": "x"}, guild_id=GUILD)
    assert await update_session(db, CH, gid, [1]) is None  # the start-time call
    await end_game(db, gid)
    assert _sessions(db_path) == []


async def test_update_session_takes_the_type_when_the_row_is_gone(db_path):
    db = GamesDb(db_path)
    assert await update_session(db, CH, "archived-photo", [1], game_type="photo") is None
    assert await update_session(db, CH, "archived-wyr", [1], game_type="wyr") is not None


async def test_activity_opens_the_session_with_the_live_roster(db_path):
    """A /recap forty minutes into a game used to say 'No active session'."""
    db = GamesDb(db_path)
    gid = await create_game(db, CH, 1, "mlt", payload={"players": [1]}, guild_id=GUILD)
    assert _sessions(db_path) == []

    def _join(p):
        p["players"].append(2)

    await modify_payload(db, gid, _join)

    rows = _sessions(db_path)
    assert len(rows) == 1
    assert json.loads(rows[0]["game_ids"]) == [gid]
    # mlt's roster is whoever voted; nobody has, so the session holds the game
    # but no players yet — the end-of-game merge fills it in.
    assert json.loads(rows[0]["player_ids"]) == []


async def test_activity_moves_the_window_forward_and_merges_joiners(db_path):
    db = GamesDb(db_path)
    gid = await create_game(db, CH, 1, "clapback", payload={"players": [1]}, guild_id=GUILD)
    await update_session(db, CH, gid, [1])
    await db.execute(
        "UPDATE games_session_tracker SET last_game_at = '2000-01-01T00:00:00'"
    )

    def _join(p):
        p["players"].append(2)

    await modify_payload(db, gid, _join)

    rows = _sessions(db_path)
    # Not a second session: the stale one was 26 years old, but the touch
    # looks the game up rather than the window, so it lands in a fresh row
    # holding the current roster — the old row is history.
    fresh = rows[-1]
    assert json.loads(fresh["game_ids"]) == [gid]
    assert sorted(json.loads(fresh["player_ids"])) == [1, 2]
    assert fresh["last_game_at"] > "2020"


async def test_touches_are_rate_limited_per_game(db_path):
    db = GamesDb(db_path)
    gid = await create_game(db, CH, 1, "clapback", payload={"players": [1, 2]}, guild_id=GUILD)
    assert await touch_session(db, gid, {"players": [1, 2]}) is not None
    assert await touch_session(db, gid, {"players": [1, 2, 3]}) is None
    assert await touch_session(db, gid, {"players": [1, 2, 3]}, min_interval=0) is not None
    assert sorted(json.loads(_sessions(db_path)[0]["player_ids"])) == [1, 2, 3]


async def test_a_bot_post_is_never_touched(db_path):
    db = GamesDb(db_path)
    gid = await create_game(db, CH, 1, "photo", payload={"prompt": "x"}, guild_id=GUILD)
    assert await touch_session(db, gid, {"prompt": "y"}) is None
    assert _sessions(db_path) == []


async def test_a_touch_on_an_archived_game_is_a_no_op(db_path):
    db = GamesDb(db_path)
    assert await touch_session(db, "gone", {"players": [1]}) is None
    assert _sessions(db_path) == []
