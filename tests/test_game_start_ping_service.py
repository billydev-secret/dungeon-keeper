"""Start-countdown host nudge: pure predicates, copy, and the sweep's branches.

Covers the guards that decide whether a host gets tapped on the shoulder —
countdown present, moment arrived, not already nudged — plus the sweep's
channel-unreachable and send-failure paths, over a real schema + GamesDb.
"""

import asyncio
import json


import discord
import pytest

from bot_modules.games.constants import (
    GAME_NAMES,
    LOBBY_GAME_TYPES,
    LOBBY_START_BUTTON,
)
from bot_modules.games.utils.game_manager import create_game, get_game_payload
from bot_modules.services import game_start_ping_service as svc
from bot_modules.services.games_db import GamesDb

NOW = 1_000_000.0
HOST = 5150
CHAN = 4242


# ── extract_start_epoch ─────────────────────────────────────────────────────

def test_extract_start_epoch_reads_top_level():
    assert svc.extract_start_epoch({"start_epoch": 1234}) == 1234


def test_extract_start_epoch_falls_back_to_clapbacks_nested_config():
    # Clapback predates this feature and keeps its epoch under config, where
    # its lobby-view timeout and embed both read it. The fallback is what lets
    # us avoid writing the same value into two places that can drift.
    payload = {"config": {"start_epoch": 999, "rounds": 5}}
    assert svc.extract_start_epoch(payload) == 999


def test_extract_start_epoch_prefers_top_level_over_nested():
    payload = {"start_epoch": 111, "config": {"start_epoch": 222}}
    assert svc.extract_start_epoch(payload) == 111


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({}, id="empty"),
        pytest.param({"config": {}}, id="config-without-epoch"),
        pytest.param({"config": "not-a-dict"}, id="config-not-a-dict"),
        pytest.param({"start_epoch": None}, id="explicit-none"),
        pytest.param({"start_epoch": "soon"}, id="unparseable"),
        pytest.param({"start_epoch": 0}, id="zero"),
        pytest.param({"start_epoch": -5}, id="negative"),
    ],
)
def test_extract_start_epoch_returns_none_for_no_usable_countdown(payload):
    # A malformed payload must read as "no countdown", never raise — one bad
    # lobby cannot be allowed to wedge the sweep for every other lobby.
    assert svc.extract_start_epoch(payload) is None


# ── resolve_start_epoch ─────────────────────────────────────────────────────

def test_resolve_start_epoch_converts_minutes_to_an_absolute_epoch():
    assert svc.resolve_start_epoch({"start_in": 10}, now=NOW) == int(NOW + 600)


def test_resolve_start_epoch_accepts_a_numeric_string():
    # A stored schedule row round-trips through JSON and can arrive as a string.
    assert svc.resolve_start_epoch({"start_in": "5"}, now=NOW) == int(NOW + 300)


def test_resolve_start_epoch_clamps_over_long_countdowns():
    # The slash param caps at 60, but a stored schedule row never went through
    # that validation — clamp rather than advertise a start two days out.
    assert svc.resolve_start_epoch({"start_in": 5000}, now=NOW) == int(
        NOW + svc.START_IN_MAX_MINUTES * 60
    )


@pytest.mark.parametrize(
    "options",
    [
        pytest.param({}, id="absent"),
        pytest.param({"start_in": None}, id="none"),
        pytest.param({"start_in": ""}, id="blank"),
        pytest.param({"start_in": "later"}, id="unparseable"),
        pytest.param({"start_in": 0}, id="zero"),
        pytest.param({"start_in": -3}, id="negative"),
    ],
)
def test_resolve_start_epoch_returns_none_when_no_countdown_asked_for(options):
    # No start_in ⇒ no countdown ⇒ no nudge. The manual path is strictly opt-in.
    assert svc.resolve_start_epoch(options, now=NOW) is None


# ── start_ping_due ──────────────────────────────────────────────────────────

def test_start_ping_due_when_moment_arrived():
    assert svc.start_ping_due({"start_epoch": NOW}, NOW) is True


def test_start_ping_due_at_exact_epoch_is_due():
    # Boundary: >= not >, so a tick landing exactly on the second still fires.
    assert svc.start_ping_due({"start_epoch": int(NOW)}, float(int(NOW))) is True


def test_start_ping_not_due_before_the_moment():
    assert svc.start_ping_due({"start_epoch": NOW + 60}, NOW) is False


def test_start_ping_not_due_when_already_sent():
    payload = {"start_epoch": NOW - 60, "start_ping_sent": True}
    assert svc.start_ping_due(payload, NOW) is False


def test_start_ping_not_due_without_a_countdown():
    # The manual path is opt-in: no start_in, no start_epoch, no nudge ever.
    assert svc.start_ping_due({"players": []}, NOW) is False


# ── build_start_ping ────────────────────────────────────────────────────────

@pytest.mark.parametrize("game_type", sorted(LOBBY_GAME_TYPES))
def test_build_start_ping_names_host_game_and_real_button(game_type):
    text = svc.build_start_ping(game_type, HOST)
    assert f"<@{HOST}>" in text
    assert GAME_NAMES[game_type] in text
    assert LOBBY_START_BUTTON[game_type] in text


def test_build_start_ping_unknown_game_degrades_to_generic_button():
    # Better a vague nudge than one naming a button that isn't there.
    text = svc.build_start_ping("not_a_game", HOST)
    assert "the start button" in text
    assert f"<@{HOST}>" in text


def test_lobby_start_button_covers_every_lobby_game():
    # Contract: adding a lobby game without a label would silently ship the
    # generic fallback to real hosts.
    assert set(LOBBY_START_BUTTON) == set(LOBBY_GAME_TYPES)


# ── host_only_mentions ──────────────────────────────────────────────────────

def test_host_only_mentions_allow_lists_exactly_the_host():
    am = svc.host_only_mentions(HOST)
    assert am.everyone is False
    assert am.roles is False
    assert [u.id for u in am.users] == [HOST]


# ── send_start_ping ─────────────────────────────────────────────────────────

class _Chan:
    def __init__(self, cid=CHAN, fail=False):
        self.id = cid
        self.name = "games"
        self.sends = []
        self.mentions = []
        self._fail = fail

    async def send(self, content=None, **kwargs):
        if self._fail:
            raise discord.HTTPException(_Resp(), "no perms")
        self.sends.append(content)
        self.mentions.append(kwargs.get("allowed_mentions"))
        return object()


class _Resp:
    status = 403
    reason = "Forbidden"


async def test_send_start_ping_posts_with_host_allow_list():
    chan = _Chan()
    assert await svc.send_start_ping(chan, "rushmore", HOST) is True
    assert "Start Draft" in chan.sends[0]
    assert [u.id for u in chan.mentions[0].users] == [HOST]
    assert chan.mentions[0].everyone is False


async def test_send_start_ping_swallows_send_failure():
    # A lobby we can't nudge is not worth crashing the sweep over.
    assert await svc.send_start_ping(_Chan(fail=True), "mlt", HOST) is False


# ── the sweep ───────────────────────────────────────────────────────────────

class _Bot:
    def __init__(self, games_db, channels):
        self.games_db = games_db
        self._channels = channels
        self._closed = False

    def get_channel(self, cid):
        return self._channels.get(cid)

    async def fetch_channel(self, cid):
        if cid in self._channels:
            return self._channels[cid]
        raise RuntimeError("not found")

    async def wait_until_ready(self):
        return None

    def is_closed(self):
        # One sweep, then stop.
        was = self._closed
        self._closed = True
        return was


async def _make_lobby(db, *, game_type="clapback", payload=None, state="joining"):
    return await create_game(
        db, CHAN, HOST, game_type, state=state, payload=payload or {},
    )


async def test_process_lobby_pings_and_marks_sent(sync_db_path):
    db = GamesDb(sync_db_path)
    chan = _Chan()
    bot = _Bot(db, {CHAN: chan})
    gid = await _make_lobby(db, payload={"start_epoch": NOW - 5})

    row = await db.fetchone("SELECT * FROM games_active_games WHERE game_id = ?", (gid,))
    await svc._process_lobby(bot, db, row, NOW)

    assert len(chan.sends) == 1
    assert "Clapback" in chan.sends[0]
    assert (await get_game_payload(db, gid))["start_ping_sent"] is True


async def test_process_lobby_is_quiet_before_the_moment(sync_db_path):
    db = GamesDb(sync_db_path)
    chan = _Chan()
    bot = _Bot(db, {CHAN: chan})
    gid = await _make_lobby(db, payload={"start_epoch": NOW + 600})

    row = await db.fetchone("SELECT * FROM games_active_games WHERE game_id = ?", (gid,))
    await svc._process_lobby(bot, db, row, NOW)

    assert chan.sends == []
    assert "start_ping_sent" not in await get_game_payload(db, gid)


async def test_process_lobby_never_double_pings(sync_db_path):
    db = GamesDb(sync_db_path)
    chan = _Chan()
    bot = _Bot(db, {CHAN: chan})
    gid = await _make_lobby(db, payload={"start_epoch": NOW - 5})

    for _ in range(3):
        row = await db.fetchone(
            "SELECT * FROM games_active_games WHERE game_id = ?", (gid,)
        )
        await svc._process_lobby(bot, db, row, NOW)

    assert len(chan.sends) == 1


async def test_process_lobby_unreachable_channel_stops_retrying(sync_db_path):
    # Otherwise we'd re-attempt every 15s for the whole life of the lobby.
    db = GamesDb(sync_db_path)
    bot = _Bot(db, {})
    gid = await _make_lobby(db, payload={"start_epoch": NOW - 5})

    row = await db.fetchone("SELECT * FROM games_active_games WHERE game_id = ?", (gid,))
    await svc._process_lobby(bot, db, row, NOW)

    assert (await get_game_payload(db, gid))["start_ping_sent"] is True


async def test_process_lobby_reads_clapback_nested_config(sync_db_path):
    db = GamesDb(sync_db_path)
    chan = _Chan()
    bot = _Bot(db, {CHAN: chan})
    gid = await _make_lobby(
        db, payload={"config": {"start_epoch": int(NOW - 5), "rounds": 5}}
    )

    row = await db.fetchone("SELECT * FROM games_active_games WHERE game_id = ?", (gid,))
    await svc._process_lobby(bot, db, row, NOW)

    assert len(chan.sends) == 1


async def test_loop_skips_started_games_and_non_lobby_types(sync_db_path, monkeypatch):
    # The sweep's WHERE clause is the guard that keeps a running game — or a
    # game with no start button at all — from getting a "time to start" nudge.
    # 'playing' is only a real value because every lobby game's start handler
    # now writes it; clapback/mlt/story used to sit in 'joining' for their whole
    # run, so this guard passed here while doing nothing in prod. The handlers
    # are pinned in tests/cogs/test_games_lobby_start_state.py.
    db = GamesDb(sync_db_path)
    chan = _Chan()
    bot = _Bot(db, {CHAN: chan})
    due = {"start_epoch": NOW - 5}

    await _make_lobby(db, game_type="clapback", payload=due, state="playing")
    await _make_lobby(db, game_type="wyr", payload=due)  # no lobby, not swept
    keeper = await _make_lobby(db, game_type="story", payload=due)

    monkeypatch.setattr(svc.time, "time", lambda: NOW)
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    await svc.game_start_ping_loop(bot)

    assert len(chan.sends) == 1
    assert "Story Builder" in chan.sends[0]
    assert (await get_game_payload(db, keeper))["start_ping_sent"] is True


async def test_loop_survives_a_malformed_payload(sync_db_path, monkeypatch):
    # One corrupt row must not cost every other host their nudge.
    db = GamesDb(sync_db_path)
    chan = _Chan()
    bot = _Bot(db, {CHAN: chan})

    broken = await _make_lobby(db, game_type="mlt", payload={})
    await db.execute(
        "UPDATE games_active_games SET payload = ? WHERE game_id = ?",
        ("{not json", broken),
    )
    await _make_lobby(db, game_type="mfk", payload={"start_epoch": NOW - 5})

    monkeypatch.setattr(svc.time, "time", lambda: NOW)
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    await svc.game_start_ping_loop(bot)

    assert len(chan.sends) == 1
    assert GAME_NAMES["mfk"] in chan.sends[0]


async def _noop_sleep(_seconds):
    return None


async def test_mark_start_ping_sent_preserves_concurrent_payload_writes(sync_db_path):
    # Targeted json_set, not read-modify-write: the lobby's own writers (mlt
    # join/leave, story, clapback) mutate the payload without taking
    # payload_lock, so a read-modify-write here could drop one of them.
    db = GamesDb(sync_db_path)
    gid = await _make_lobby(db, payload={"start_epoch": NOW - 5, "players": [1]})

    # Simulate a join landing between a would-be read and write.
    payload = await get_game_payload(db, gid)
    payload["players"] = [1, 2, 3]
    await db.execute(
        "UPDATE games_active_games SET payload = ? WHERE game_id = ?",
        (json.dumps(payload), gid),
    )
    await svc.mark_start_ping_sent(db, gid)

    after = await get_game_payload(db, gid)
    assert after["start_ping_sent"] is True
    assert after["players"] == [1, 2, 3]      # the join survived
    assert after["start_epoch"] == NOW - 5    # and so did the countdown


async def test_mark_start_ping_sent_survives_a_corrupt_payload(sync_db_path):
    db = GamesDb(sync_db_path)
    gid = await _make_lobby(db, payload={})
    await db.execute(
        "UPDATE games_active_games SET payload = ? WHERE game_id = ?",
        ("{not json", gid),
    )
    await svc.mark_start_ping_sent(db, gid)  # must not raise
