"""Start-countdown host nudge: pure predicates, copy, and the sweep's branches.

Covers the guards that decide whether a host gets tapped on the shoulder —
countdown present, moment arrived, not already nudged — plus the sweep's
channel-unreachable and send-failure paths, over a real schema + GamesDb.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import discord
import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.games.constants import (
    GAME_NAMES,
    LOBBY_GAME_TYPES,
    LOBBY_MIN_PLAYERS,
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


# ── idle lobbies (discovery-6) ──────────────────────────────────────────────
#
# A lobby opened without start_in was never nudged and sat until the 24 h
# sweep. Now a joining lobby is nudged once after the configured idle time and
# closed — no payout — once it has sat for the configured hour with fewer
# than the game's minimum roster. Both dials live on Games Global Config; 0
# switches a step off.

GUILD = 9001
DIALS = svc.IdleLobbyDials(nudge_seconds=20 * 60, cancel_seconds=60 * 60)


def test_lobby_min_players_covers_every_lobby_game():
    assert set(LOBBY_MIN_PLAYERS) == set(LOBBY_GAME_TYPES)


def test_lobby_min_players_matches_the_cogs_own_floors():
    # The sweep must not close a lobby its start button would have accepted.
    from bot_modules.games_clapback.logic import MIN_PLAYERS as clapback_min
    from bot_modules.games_mlt.logic import MIN_PLAYERS as mlt_min
    from bot_modules.games_rushmore.logic import MIN_PLAYERS as rushmore_min

    assert LOBBY_MIN_PLAYERS["clapback"] == clapback_min
    assert LOBBY_MIN_PLAYERS["mlt"] == mlt_min
    assert LOBBY_MIN_PLAYERS["rushmore"] == rushmore_min


@pytest.mark.parametrize(
    "game_type, payload, expected",
    [
        pytest.param("clapback", {}, 3, id="registry-default"),
        pytest.param("mlt", {"min_players": 5}, 5, id="mlt-payload-floor"),
        pytest.param("rushmore", {"settings": {"min_players": 4}}, 4, id="rushmore-settings-floor"),
        pytest.param("mlt", {"min_players": "junk"}, 3, id="unreadable-falls-back"),
        pytest.param("not_a_game", {}, 2, id="unknown-type-assumes-two"),
    ],
)
def test_lobby_min_players_reads_the_games_own_floor(game_type, payload, expected):
    assert svc.lobby_min_players(game_type, payload) == expected


@pytest.mark.parametrize(
    "payload, expected",
    [
        pytest.param({"players": [1, 2, "3"]}, 3, id="players"),
        pytest.param({"participants": [7, 7, 8]}, 2, id="participants-deduped"),
        pytest.param({}, 0, id="empty"),
        pytest.param({"players": "nope"}, 0, id="malformed"),
    ],
)
def test_lobby_roster_size(payload, expected):
    assert svc.lobby_roster_size(payload) == expected


@pytest.mark.parametrize(
    "nudge_raw, cancel_raw, nudge, cancel",
    [
        pytest.param(None, None, 20 * 60, 60 * 60, id="defaults"),
        pytest.param("5", "30", 5 * 60, 30 * 60, id="stored"),
        pytest.param("0", "0", 0, 0, id="both-off"),
        pytest.param("junk", "-4", 20 * 60, 0, id="junk-defaults-negative-off"),
    ],
)
def test_parse_idle_dials(nudge_raw, cancel_raw, nudge, cancel):
    dials = svc.parse_idle_dials(nudge_raw, cancel_raw)
    assert (dials.nudge_seconds, dials.cancel_seconds) == (nudge, cancel)


@pytest.mark.parametrize(
    "payload, idle, due",
    [
        pytest.param({}, 20 * 60, True, id="idle-long-enough"),
        pytest.param({}, 19 * 60, False, id="not-yet"),
        pytest.param({"start_epoch": NOW + 600}, 40 * 60, False, id="countdown-lobbies-use-their-own-nudge"),
        pytest.param({"start_ping_sent": True}, 40 * 60, False, id="already-nudged"),
    ],
)
def test_idle_nudge_due(payload, idle, due):
    assert svc.idle_nudge_due(payload, NOW - idle, NOW, DIALS) is due


def test_idle_nudge_off_when_dial_is_zero():
    off = svc.IdleLobbyDials(nudge_seconds=0, cancel_seconds=3600)
    assert svc.idle_nudge_due({}, NOW - 9999, NOW, off) is False


@pytest.mark.parametrize(
    "payload, idle, due",
    [
        pytest.param({"players": [1, 2]}, 60 * 60, True, id="under-the-floor-for-an-hour"),
        pytest.param({"players": [1, 2, 3]}, 60 * 60, False, id="enough-to-start-is-the-hosts-call"),
        pytest.param({"players": [1, 2]}, 59 * 60, False, id="not-yet"),
        # A countdown lobby's hour starts at its advertised start, not at open.
        pytest.param({"players": [1], "start_epoch": NOW - 1800}, 3 * 3600, False, id="countdown-clock-starts-at-start"),
        pytest.param({"players": [1], "start_epoch": NOW - 3600}, 3 * 3600, True, id="countdown-then-an-idle-hour"),
    ],
)
def test_idle_cancel_due(payload, idle, due):
    assert svc.idle_cancel_due("clapback", payload, NOW - idle, NOW, DIALS) is due


def test_idle_cancel_off_when_dial_is_zero():
    off = svc.IdleLobbyDials(nudge_seconds=1200, cancel_seconds=0)
    assert svc.idle_cancel_due("clapback", {}, NOW - 99999, NOW, off) is False


def test_build_idle_nudge_names_button_floor_and_deadline():
    text = svc.build_idle_nudge("rushmore", HOST, idle_minutes=20, dials=DIALS, min_players=3)
    assert f"<@{HOST}>" in text
    assert "Start Draft" in text
    assert "20 minutes" in text
    assert "3 players" in text and "60 minutes" in text


def test_build_idle_nudge_omits_the_deadline_when_cancel_is_off():
    dials = svc.IdleLobbyDials(nudge_seconds=1200, cancel_seconds=0)
    text = svc.build_idle_nudge("story", HOST, idle_minutes=20, dials=dials, min_players=2)
    assert "closes" not in text


async def _aged_lobby(db, *, game_type="clapback", payload=None, age_seconds=0, state="joining"):
    gid = await _make_lobby(db, game_type=game_type, payload=payload, state=state)
    await db.execute(
        "UPDATE games_active_games SET created_at = datetime(?, 'unixepoch'), guild_id = ?"
        " WHERE game_id = ?",
        (int(NOW - age_seconds), GUILD, gid),
    )
    return gid


async def _live(db, gid):
    return await db.fetchone("SELECT 1 FROM games_active_games WHERE game_id = ?", (gid,))


async def test_read_idle_dials_falls_back_to_defaults(sync_db_path):
    dials = await svc.read_idle_dials(GamesDb(sync_db_path), GUILD)
    assert dials == svc.IdleLobbyDials(
        nudge_seconds=svc.IDLE_NUDGE_DEFAULT_MINUTES * 60,
        cancel_seconds=svc.IDLE_CANCEL_DEFAULT_MINUTES * 60,
    )


async def test_read_idle_dials_reads_the_guilds_config(sync_db_path):
    with open_db(sync_db_path) as conn:
        set_config_value(conn, svc.IDLE_NUDGE_KEY, "7", GUILD)
        set_config_value(conn, svc.IDLE_CANCEL_KEY, "0", GUILD)
    dials = await svc.read_idle_dials(GamesDb(sync_db_path), GUILD)
    assert (dials.nudge_seconds, dials.cancel_seconds) == (420, 0)


async def test_idle_lobby_is_nudged_once(sync_db_path):
    db = GamesDb(sync_db_path)
    chan = _Chan()
    bot = _Bot(db, {CHAN: chan})
    gid = await _aged_lobby(db, payload={"players": [HOST]}, age_seconds=25 * 60)

    for _ in range(3):
        row = await db.fetchone("SELECT * FROM games_active_games WHERE game_id = ?", (gid,))
        await svc._process_lobby(bot, db, row, NOW, dials=DIALS)

    assert len(chan.sends) == 1
    assert "Clapback" in chan.sends[0] and f"<@{HOST}>" in chan.sends[0]
    assert [u.id for u in chan.mentions[0].users] == [HOST]
    assert (await get_game_payload(db, gid))["start_ping_sent"] is True
    assert await _live(db, gid) is not None  # nudged, not closed


async def test_fresh_lobby_is_left_alone(sync_db_path):
    db = GamesDb(sync_db_path)
    chan = _Chan()
    bot = _Bot(db, {CHAN: chan})
    gid = await _aged_lobby(db, payload={"players": [HOST]}, age_seconds=5 * 60)
    row = await db.fetchone("SELECT * FROM games_active_games WHERE game_id = ?", (gid,))
    await svc._process_lobby(bot, db, row, NOW, dials=DIALS)
    assert chan.sends == []


class _LobbyMsg:
    def __init__(self):
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class _ChanWithMessage(_Chan):
    def __init__(self):
        super().__init__()
        self.message = _LobbyMsg()

    async def fetch_message(self, mid):
        return self.message


class _View:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


async def test_under_populated_lobby_is_closed_after_the_hour_without_pay(sync_db_path, monkeypatch):
    db = GamesDb(sync_db_path)
    chan = _ChanWithMessage()
    bot = _Bot(db, {CHAN: chan})
    view = _View()
    bot.active_views = {}
    gid = await _aged_lobby(db, payload={"players": [HOST, 2]}, age_seconds=61 * 60)
    bot.active_views[gid] = view
    await db.execute("UPDATE games_active_games SET message_id = 555 WHERE game_id = ?", (gid,))

    paid = []
    from bot_modules.games.utils import game_manager as gm

    async def _no_pay(*a, **k):
        paid.append(1)
        return 0

    monkeypatch.setattr(gm, "_pay_party_rewards", _no_pay)

    row = await db.fetchone("SELECT * FROM games_active_games WHERE game_id = ?", (gid,))
    await svc._process_lobby(bot, db, row, NOW, dials=DIALS)

    assert await _live(db, gid) is None
    hist = await db.fetchone(
        "SELECT player_count, payload FROM games_game_history WHERE game_id = ?", (gid,)
    )
    assert json.loads(hist["payload"])["reason"] == "lobby_timeout"
    assert hist["player_count"] == 2          # recorded, not paid
    assert paid == []
    assert view.stopped and gid not in bot.active_views
    assert chan.message.edits and "timed out" in chan.message.edits[0]["content"]
    assert chan.message.edits[0]["view"] is None


async def test_a_lobby_that_could_start_is_never_closed(sync_db_path):
    # Three joined Clapback: the host's call, however long they sit on it.
    db = GamesDb(sync_db_path)
    chan = _ChanWithMessage()
    bot = _Bot(db, {CHAN: chan})
    gid = await _aged_lobby(db, payload={"players": [HOST, 2, 3]}, age_seconds=5 * 3600)
    row = await db.fetchone("SELECT * FROM games_active_games WHERE game_id = ?", (gid,))
    await svc._process_lobby(bot, db, row, NOW, dials=DIALS)
    assert await _live(db, gid) is not None
    assert chan.message.edits == []


async def test_loop_reads_each_guilds_dials(sync_db_path, monkeypatch):
    # Dial set to 0 for this guild: the lobby is neither nudged nor closed.
    db = GamesDb(sync_db_path)
    chan = _ChanWithMessage()
    bot = _Bot(db, {CHAN: chan})
    with open_db(sync_db_path) as conn:
        set_config_value(conn, svc.IDLE_NUDGE_KEY, "0", GUILD)
        set_config_value(conn, svc.IDLE_CANCEL_KEY, "0", GUILD)
    gid = await _aged_lobby(db, payload={"players": [HOST]}, age_seconds=5 * 3600)

    monkeypatch.setattr(svc.time, "time", lambda: NOW)
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    await svc.game_start_ping_loop(bot)

    assert chan.sends == []
    assert await _live(db, gid) is not None


# ── countdown auto-start (clapback-8) ───────────────────────────────────────
#
# A game whose cog registers an auto-starter is started by the sweep at its
# advertised moment when the roster is at the floor; short of it the host is
# nudged once with what the lobby is waiting on, and the game still starts
# itself the tick the floor is reached. A game with no auto-starter keeps the
# nudge contract the tests above pin.


@pytest.mark.parametrize(
    "payload, due",
    [
        pytest.param({"players": [1, 2, 3], "start_epoch": NOW - 1}, True, id="due-at-the-floor"),
        pytest.param({"players": [1, 2, 3], "start_epoch": NOW + 60}, False, id="not-yet"),
        pytest.param({"players": [1, 2], "start_epoch": NOW - 1}, False, id="short-roster"),
        pytest.param({"players": [1, 2, 3]}, False, id="no-countdown"),
        pytest.param({"players": [1, 2, 3], "start_epoch": NOW - 1, "start_ping_sent": True}, True,
                     id="a-nudged-lobby-still-starts-when-the-floor-arrives"),
        pytest.param({"players": [1, 2, 3], "start_epoch": NOW - 1, "auto_start_failed": True}, False,
                     id="never-retried-after-a-crash"),
    ],
)
def test_auto_start_due(payload, due):
    assert svc.auto_start_due("clapback", payload, NOW) is due


def test_build_short_roster_nudge_says_what_it_waits_on():
    text = svc.build_short_roster_nudge("clapback", HOST, joined=2, min_players=3, dials=DIALS)
    assert f"<@{HOST}>" in text and "Clapback" in text
    assert "2 of the 3" in text and "starts on its own" in text
    assert "60 minutes" in text
    assert "Hit" not in text  # the button would refuse; don't point at it


def test_build_short_roster_nudge_without_the_close():
    text = svc.build_short_roster_nudge("clapback", HOST, joined=1, min_players=3)
    assert "closes" not in text and "1 of the 3 it needs has joined" in text


class _StarterBot(_Bot):
    """A bot whose clapback cog registered an auto-starter."""

    def __init__(self, games_db, channels, *, result=True, raise_=False):
        super().__init__(games_db, channels)
        self.calls = []

        async def _start(row, payload, channel):
            self.calls.append((row["game_id"], payload.get("players"), channel))
            if raise_:
                raise RuntimeError("bracket exploded")
            return result

        self.lobby_auto_starters = {"clapback": _start}


async def test_sweep_starts_the_game_at_the_countdown_and_does_not_nudge(sync_db_path):
    db = GamesDb(sync_db_path)
    chan = _Chan()
    bot = _StarterBot(db, {CHAN: chan})
    gid = await _make_lobby(db, payload={"players": [HOST, 2, 3], "config": {"start_epoch": int(NOW - 5)}})

    row = await db.fetchone("SELECT * FROM games_active_games WHERE game_id = ?", (gid,))
    await svc._process_lobby(bot, db, row, NOW, dials=DIALS)

    assert [c[0] for c in bot.calls] == [gid]
    assert bot.calls[0][2] is chan
    assert chan.sends == []
    assert "start_ping_sent" not in await get_game_payload(db, gid)


async def test_sweep_leaves_a_countdown_alone_until_it_runs_out(sync_db_path):
    db = GamesDb(sync_db_path)
    bot = _StarterBot(db, {CHAN: _Chan()})
    gid = await _make_lobby(db, payload={"players": [HOST, 2, 3], "start_epoch": NOW + 300})
    row = await db.fetchone("SELECT * FROM games_active_games WHERE game_id = ?", (gid,))
    await svc._process_lobby(bot, db, row, NOW, dials=DIALS)
    assert bot.calls == []


async def test_short_roster_at_the_countdown_is_nudged_once_then_starts_when_full(sync_db_path):
    db = GamesDb(sync_db_path)
    chan = _Chan()
    bot = _StarterBot(db, {CHAN: chan})
    gid = await _make_lobby(db, payload={"players": [HOST, 2], "start_epoch": NOW - 5})

    for _ in range(3):
        row = await db.fetchone("SELECT * FROM games_active_games WHERE game_id = ?", (gid,))
        await svc._process_lobby(bot, db, row, NOW, dials=DIALS)

    assert bot.calls == []
    assert len(chan.sends) == 1
    assert "2 of the 3" in chan.sends[0] and "starts on its own" in chan.sends[0]
    assert [u.id for u in chan.mentions[0].users] == [HOST]

    # A third joins two minutes later: no press needed.
    payload = await get_game_payload(db, gid)
    payload["players"] = [HOST, 2, 3]
    await db.execute(
        "UPDATE games_active_games SET payload = ? WHERE game_id = ?", (json.dumps(payload), gid)
    )
    row = await db.fetchone("SELECT * FROM games_active_games WHERE game_id = ?", (gid,))
    await svc._process_lobby(bot, db, row, NOW + 120, dials=DIALS)
    assert [c[0] for c in bot.calls] == [gid]
    assert len(chan.sends) == 1


async def test_a_starter_that_refuses_falls_back_to_the_ordinary_nudge(sync_db_path):
    # Three joined but the no-contact floor says two: Start would refuse, so
    # the host gets the plain "time to start" and the refusal line from there.
    db = GamesDb(sync_db_path)
    chan = _Chan()
    bot = _StarterBot(db, {CHAN: chan}, result=False)
    gid = await _make_lobby(db, payload={"players": [HOST, 2, 3], "start_epoch": NOW - 5})

    for _ in range(2):
        row = await db.fetchone("SELECT * FROM games_active_games WHERE game_id = ?", (gid,))
        await svc._process_lobby(bot, db, row, NOW, dials=DIALS)

    assert len(chan.sends) == 1
    assert "time to start" in chan.sends[0] and "Start" in chan.sends[0]


async def test_a_starter_that_crashes_is_not_retried_every_tick(sync_db_path):
    db = GamesDb(sync_db_path)
    chan = _Chan()
    bot = _StarterBot(db, {CHAN: chan}, raise_=True)
    gid = await _make_lobby(db, payload={"players": [HOST, 2, 3], "start_epoch": NOW - 5})

    for _ in range(3):
        row = await db.fetchone("SELECT * FROM games_active_games WHERE game_id = ?", (gid,))
        await svc._process_lobby(bot, db, row, NOW, dials=DIALS)

    assert len(bot.calls) == 1
    assert (await get_game_payload(db, gid))["auto_start_failed"] is True
    assert len(chan.sends) == 1 and "time to start" in chan.sends[0]


async def test_a_game_without_an_auto_starter_keeps_the_nudge(sync_db_path):
    db = GamesDb(sync_db_path)
    chan = _Chan()
    bot = _StarterBot(db, {CHAN: chan})
    gid = await _make_lobby(db, game_type="story", payload={"players": [HOST, 2], "start_epoch": NOW - 5})
    row = await db.fetchone("SELECT * FROM games_active_games WHERE game_id = ?", (gid,))
    await svc._process_lobby(bot, db, row, NOW, dials=DIALS)
    assert bot.calls == []
    assert len(chan.sends) == 1 and "Start Story" in chan.sends[0]


def test_set_payload_flag_refuses_caller_text():
    with pytest.raises(ValueError):
        asyncio.run(svc.set_payload_flag(None, "g", "players"))


# ── the Game Night ping (discovery-2 / clapback-11) ─────────────────────────
#
# One line per lobby, the first tick the board exists, mentioning the guild's
# opt-in Game Night role — content with a role allow-list and a jump link.

ROLE = 777_000


def test_build_game_night_ping_mentions_role_game_time_and_board():
    text = svc.build_game_night_ping(
        "clapback", role_id=ROLE, guild_id=GUILD, channel_id=CHAN, message_id=555,
        start_epoch=int(NOW),
    )
    assert text.startswith(f"<@&{ROLE}> ")
    assert "Clapback" in text and f"<t:{int(NOW)}:R>" in text
    assert f"https://discord.com/channels/{GUILD}/{CHAN}/555" in text


def test_build_game_night_ping_without_a_countdown_names_no_time():
    text = svc.build_game_night_ping("mlt", role_id=ROLE, guild_id=GUILD, channel_id=CHAN, message_id=1)
    assert "<t:" not in text and "Most Likely To" in text


def test_role_only_mentions_allow_lists_exactly_the_role():
    am = svc.role_only_mentions(ROLE)
    assert [r.id for r in am.roles] == [ROLE]  # type: ignore[union-attr]
    assert am.everyone is False and am.users is False


async def test_resolve_game_night_role_is_unresolved_without_a_guild(sync_db_path):
    # The sweep's own bot double has no guild cache: nothing pings, nothing
    # is flagged, and the lobby is looked at again next tick.
    assert await svc.resolve_game_night_role(_Bot(GamesDb(sync_db_path), {}), GUILD) == (False, None)


async def _board(db, *, payload=None, game_type="clapback"):
    gid = await _aged_lobby(db, game_type=game_type, payload=payload or {"players": [HOST]}, age_seconds=10)
    await db.execute("UPDATE games_active_games SET message_id = 555 WHERE game_id = ?", (gid,))
    return gid


async def test_game_night_ping_goes_out_once_per_lobby(sync_db_path, monkeypatch):
    db = GamesDb(sync_db_path)
    chan = _Chan()
    bot = _Bot(db, {CHAN: chan})
    looked_up = []

    async def _role(bot_, guild_id):
        looked_up.append(guild_id)
        return True, ROLE

    monkeypatch.setattr(svc, "resolve_game_night_role", _role)
    gid = await _board(db, payload={"players": [HOST], "start_epoch": NOW + 600})

    for _ in range(3):
        row = await db.fetchone("SELECT * FROM games_active_games WHERE game_id = ?", (gid,))
        await svc._process_lobby(bot, db, row, NOW, dials=DIALS)

    assert len(chan.sends) == 1
    assert chan.sends[0].startswith(f"<@&{ROLE}> ") and "/555" in chan.sends[0]
    assert [r.id for r in chan.mentions[0].roles] == [ROLE]
    assert chan.mentions[0].users is False
    assert (await get_game_payload(db, gid))["game_night_pinged"] is True
    assert looked_up == [GUILD]


async def test_game_night_ping_waits_for_the_board(sync_db_path, monkeypatch):
    # No message_id yet: the launcher is still posting. Nothing to link at.
    db = GamesDb(sync_db_path)
    chan = _Chan()
    bot = _Bot(db, {CHAN: chan})
    monkeypatch.setattr(svc, "resolve_game_night_role", AsyncMock(return_value=(True, ROLE)))
    gid = await _aged_lobby(db, payload={"players": [HOST]}, age_seconds=10)
    row = await db.fetchone("SELECT * FROM games_active_games WHERE game_id = ?", (gid,))
    await svc._process_lobby(bot, db, row, NOW, dials=DIALS)
    assert chan.sends == []
    assert "game_night_pinged" not in await get_game_payload(db, gid)


async def test_game_night_ping_honours_none(sync_db_path, monkeypatch):
    # An admin chose "(none)": flagged so it isn't re-asked, and silent.
    db = GamesDb(sync_db_path)
    chan = _Chan()
    bot = _Bot(db, {CHAN: chan})
    monkeypatch.setattr(svc, "resolve_game_night_role", AsyncMock(return_value=(True, None)))
    gid = await _board(db)
    row = await db.fetchone("SELECT * FROM games_active_games WHERE game_id = ?", (gid,))
    await svc._process_lobby(bot, db, row, NOW, dials=DIALS)
    assert chan.sends == []
    assert (await get_game_payload(db, gid))["game_night_pinged"] is True


async def test_game_night_ping_is_not_flagged_while_the_guild_is_unreachable(sync_db_path, monkeypatch):
    db = GamesDb(sync_db_path)
    chan = _Chan()
    bot = _Bot(db, {CHAN: chan})
    monkeypatch.setattr(svc, "resolve_game_night_role", AsyncMock(return_value=(False, None)))
    gid = await _board(db)
    row = await db.fetchone("SELECT * FROM games_active_games WHERE game_id = ?", (gid,))
    await svc._process_lobby(bot, db, row, NOW, dials=DIALS)
    assert chan.sends == []
    assert "game_night_pinged" not in await get_game_payload(db, gid)


async def test_game_night_ping_is_skipped_for_a_lobby_the_scheduler_announced(sync_db_path, monkeypatch):
    db = GamesDb(sync_db_path)
    chan = _Chan()
    bot = _Bot(db, {CHAN: chan})
    monkeypatch.setattr(svc, "resolve_game_night_role", AsyncMock(return_value=(True, ROLE)))
    gid = await _board(db)
    await svc.claim_game_night_ping(db, gid)
    row = await db.fetchone("SELECT * FROM games_active_games WHERE game_id = ?", (gid,))
    await svc._process_lobby(bot, db, row, NOW, dials=DIALS)
    assert chan.sends == []


async def test_game_night_ping_stands_down_when_a_launch_claims_it_mid_tick(sync_db_path, monkeypatch):
    """The sweep reads its rows at the top of the tick, then does slow work
    (role lookup) before sending. A schedule announcing the same launch in
    that window claims the line — the sweep must re-check at send time, or
    the room is pinged twice for one game opening."""
    db = GamesDb(sync_db_path)
    chan = _Chan()
    bot = _Bot(db, {CHAN: chan})
    gid = await _board(db)
    row = await db.fetchone("SELECT * FROM games_active_games WHERE game_id = ?", (gid,))

    async def _role(_bot, _guild_id):
        # Stands in for the announcement landing while the role is resolved.
        await svc.claim_game_night_ping(db, gid)
        return True, ROLE

    monkeypatch.setattr(svc, "resolve_game_night_role", _role)
    await svc._process_lobby(bot, db, row, NOW, dials=DIALS)

    assert chan.sends == []


async def test_claim_game_night_ping_is_won_once(sync_db_path):
    db = GamesDb(sync_db_path)
    gid = await _board(db)
    assert await svc.claim_game_night_ping(db, gid) is True
    assert await svc.claim_game_night_ping(db, gid) is False
    # An in-memory game (risky_roll) has no row to flag: nothing else can
    # have called that room, so the launcher's own announcement goes out.
    assert await svc.claim_game_night_ping(db, "no-such-game") is False
    assert await svc.claim_game_night_ping(db, "no-such-game", no_row_wins=True) is True


async def test_loop_resolves_the_role_once_per_guild_per_tick(sync_db_path, monkeypatch):
    db = GamesDb(sync_db_path)
    chan = _Chan()
    bot = _Bot(db, {CHAN: chan})
    calls = []

    async def _role(bot_, guild_id):
        calls.append(guild_id)
        return True, ROLE

    monkeypatch.setattr(svc, "resolve_game_night_role", _role)
    await _board(db)
    await _board(db, game_type="story")

    monkeypatch.setattr(svc.time, "time", lambda: NOW)
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    await svc.game_start_ping_loop(bot)

    assert len(chan.sends) == 2
    assert calls == [GUILD]
