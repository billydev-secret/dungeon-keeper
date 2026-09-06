"""The pacing rules the three reactive round games share.

``games/utils/round_pacing.py`` owns the round timer, the round cap and the
scheduled-game Next unlock for Would You Rather, Never Have I Ever and Most
Likely To (vote-games-53, discovery-1, discovery-3, platform-23). These pin
the pure rules; the cogs' wiring is covered in each game's logic test.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from bot_modules.games.utils import round_pacing as rp
from bot_modules.games.utils.round_pacing import (
    DEFAULT_MAX_ROUNDS,
    SCHEDULED_NEXT_UNLOCK_SECONDS,
    RoundPacing,
    advance_at,
    clamp_max_rounds,
    clamp_round_seconds,
    has_game_host_role,
    is_scheduled_launch,
    resolve_pacing,
    round_cap_reached,
    seconds_left,
    voter_may_advance,
    voter_unlock_at,
)
from bot_modules.services.games_db import GamesDb


# ── clamps ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(None, 0, id="none-is-host-paced"),
        pytest.param("", 0, id="empty"),
        pytest.param("abc", 0, id="garbage"),
        pytest.param(-5, 0, id="negative"),
        pytest.param(45, 45, id="plain"),
        pytest.param("90", 90, id="string-int"),
        pytest.param(9999, 300, id="capped-at-five-minutes"),
    ],
)
def test_clamp_round_seconds(raw, expected):
    assert clamp_round_seconds(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(None, DEFAULT_MAX_ROUNDS, id="none-takes-default"),
        pytest.param("x", DEFAULT_MAX_ROUNDS, id="garbage-takes-default"),
        pytest.param(0, 0, id="zero-is-no-cap"),
        pytest.param(-1, 0, id="negative-is-no-cap"),
        pytest.param(7, 7, id="plain"),
        pytest.param(500, 50, id="capped-at-fifty"),
    ],
)
def test_clamp_max_rounds(raw, expected):
    assert clamp_max_rounds(raw) == expected


# ── resolve_pacing: launch option > dashboard default > built-in ─────────────


@pytest.mark.parametrize(
    ("options", "game_opts", "expected"),
    [
        pytest.param({}, {}, (0, DEFAULT_MAX_ROUNDS), id="built-ins"),
        pytest.param({}, {"round_seconds": 60, "max_rounds": 5}, (60, 5), id="dashboard-defaults"),
        pytest.param({"round_seconds": 30}, {"round_seconds": 60}, (30, DEFAULT_MAX_ROUNDS), id="slash-wins"),
        pytest.param({"round_seconds": 0}, {"round_seconds": 60}, (0, DEFAULT_MAX_ROUNDS), id="explicit-zero-wins"),
        pytest.param({"max_rounds": 0}, {"max_rounds": 8}, (0, 0), id="explicit-no-cap-wins"),
        pytest.param({"round_seconds": None, "max_rounds": None}, {"max_rounds": 3}, (0, 3), id="none-defers"),
        pytest.param(None, None, (0, DEFAULT_MAX_ROUNDS), id="nothing-at-all"),
    ],
)
def test_resolve_pacing(options, game_opts, expected):
    assert resolve_pacing(options, game_opts) == expected


@pytest.mark.parametrize(
    ("options", "host_id", "expected"),
    [
        pytest.param({}, 42, False, id="slash-launch"),
        pytest.param({"scheduled": True}, 42, True, id="scheduler-flag"),
        pytest.param({}, 0, True, id="rotation-host-zero"),
        pytest.param({"scheduled": False}, None, True, id="no-host-at-all"),
    ],
)
def test_is_scheduled_launch(options, host_id, expected):
    assert is_scheduled_launch(options, host_id) is expected


# ── round cap ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("round_num", "max_rounds", "expected"),
    [
        pytest.param(9, 10, False, id="one-to-go"),
        pytest.param(10, 10, True, id="last-round"),
        pytest.param(11, 10, True, id="past-cap"),
        pytest.param(100, 0, False, id="no-cap"),
    ],
)
def test_round_cap_reached(round_num, max_rounds, expected):
    assert round_cap_reached(round_num, max_rounds) is expected


# ── timer arithmetic ─────────────────────────────────────────────────────────


def test_advance_at_is_none_when_host_paced_or_waiting():
    assert advance_at(1000.0, 0) is None
    assert advance_at(None, 60) is None
    assert advance_at(1000.0, 60) == 1060


def test_seconds_left_counts_down_from_opened_at_and_floors_at_zero():
    assert seconds_left(1000.0, 60, now=1010.0) == 50.0
    assert seconds_left(1000.0, 60, now=2000.0) == 0.0
    assert seconds_left(1000.0, 0, now=1010.0) is None
    assert seconds_left(None, 60, now=1010.0) is None


# ── the scheduled-game Next unlock ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("scheduled", "has_voted", "opened_at", "round_seconds", "now", "expected"),
    [
        pytest.param(False, True, 1000.0, 0, 5000.0, False, id="hosted-game-never-unlocks"),
        pytest.param(True, False, 1000.0, 0, 5000.0, False, id="non-voter-never-unlocks"),
        pytest.param(True, True, None, 0, 5000.0, False, id="waiting-round-never-unlocks"),
        pytest.param(True, True, 1000.0, 0, 1000.0 + SCHEDULED_NEXT_UNLOCK_SECONDS - 1, False, id="host-paced-too-early"),
        pytest.param(True, True, 1000.0, 0, 1000.0 + SCHEDULED_NEXT_UNLOCK_SECONDS, True, id="host-paced-grace-elapsed"),
        pytest.param(True, True, 1000.0, 45, 1044.0, False, id="timed-too-early"),
        pytest.param(True, True, 1000.0, 45, 1045.0, True, id="timed-elapsed"),
    ],
)
def test_voter_may_advance(scheduled, has_voted, opened_at, round_seconds, now, expected):
    assert voter_may_advance(
        scheduled=scheduled, has_voted=has_voted, opened_at=opened_at,
        round_seconds=round_seconds, now=now,
    ) is expected


def test_voter_unlock_at_uses_the_timer_or_the_grace_window():
    assert voter_unlock_at(1000.0, 45) == 1045
    assert voter_unlock_at(1000.0, 0) == 1000 + SCHEDULED_NEXT_UNLOCK_SECONDS
    assert voter_unlock_at(None, 45) is None


# ── the Game Host role ───────────────────────────────────────────────────────


async def test_has_game_host_role_matches_the_configured_role(sync_db_path):
    db = GamesDb(sync_db_path)
    await db.execute(
        "INSERT INTO games_editor_role (guild_id, role_id, set_by) VALUES (?, ?, ?)",
        (4242, 777, 1),
    )
    assert await has_game_host_role(db, 4242, [1, 777]) is True
    assert await has_game_host_role(db, 4242, [1, 2]) is False
    assert await has_game_host_role(db, 9999, [777]) is False
    assert await has_game_host_role(db, None, [777]) is False


def test_member_role_ids_reads_roles_and_tolerates_users_without_them():
    class _Role:
        def __init__(self, rid):
            self.id = rid

    class _Member:
        roles = [_Role(1), _Role(2)]

    assert rp.member_role_ids(_Member()) == [1, 2]
    assert rp.member_role_ids(object()) == []


# ── RoundPacing: the timer fires unless Next gets there first ───────────────


async def test_timer_fires_on_timeout():
    pacing = RoundPacing(round_seconds=1)
    fired = asyncio.Event()

    async def _on_timeout():
        fired.set()

    task = pacing.start_timer(_on_timeout, seconds=0.01)
    assert task is not None
    await asyncio.wait_for(fired.wait(), timeout=1)


async def test_timer_is_an_early_skip_when_next_is_pressed_first():
    pacing = RoundPacing(round_seconds=5)
    calls: list[int] = []

    async def _on_timeout():
        calls.append(1)

    task = pacing.start_timer(_on_timeout, seconds=0.2)
    assert task is not None
    pacing.advanced.set()
    await asyncio.wait_for(task, timeout=1)
    assert calls == []


def test_host_paced_round_starts_no_timer():
    pacing = RoundPacing(round_seconds=0)

    async def _never():
        raise AssertionError("must not fire")

    assert pacing.start_timer(_never) is None
    assert pacing.timer_task is None


def test_pacing_clamps_and_opens():
    pacing = RoundPacing(round_seconds=999, max_rounds=None, scheduled=True)
    assert pacing.round_seconds == 300
    assert pacing.max_rounds == DEFAULT_MAX_ROUNDS
    assert pacing.scheduled is True
    assert pacing.advance_at() is None
    opened = pacing.open(now=1000.0)
    assert opened == 1000.0
    assert pacing.advance_at() == 1300


# ── the refusal copy and the gate ────────────────────────────────────────────


def test_advance_refusal_names_what_would_unlock_a_scheduled_round():
    assert rp.advance_refusal(scheduled=False, has_voted=True, opened_at=1000.0, round_seconds=0) == rp.ADVANCE_DENIED
    assert rp.advance_refusal(scheduled=True, has_voted=True, opened_at=None, round_seconds=0) == rp.ADVANCE_DENIED
    assert "Vote first" in rp.advance_refusal(scheduled=True, has_voted=False, opened_at=1000.0, round_seconds=0)
    assert f"<t:{1000 + SCHEDULED_NEXT_UNLOCK_SECONDS}:R>" in rp.advance_refusal(
        scheduled=True, has_voted=True, opened_at=1000.0, round_seconds=0,
    )


class _Role:
    def __init__(self, rid):
        self.id = rid


def _interaction(uid: int, *, roles=(), guild_id=4242):
    """A guild member who is not a ``discord.Member`` (so never a mod)."""
    from types import SimpleNamespace

    return SimpleNamespace(
        user=SimpleNamespace(id=uid, roles=[_Role(r) for r in roles]),
        guild=object(),
        guild_id=guild_id,
    )


async def test_advance_check_host_then_role_then_voter_unlock(sync_db_path):
    db = GamesDb(sync_db_path)
    await db.execute(
        "INSERT INTO games_editor_role (guild_id, role_id, set_by) VALUES (?, ?, ?)",
        (4242, 777, 1),
    )
    pacing = RoundPacing(round_seconds=0, scheduled=True, opened_at=1.0)  # opened long ago
    # The host.
    assert await rp.advance_check(_interaction(1), host_id=1, db=db, pacing=pacing, has_voted=False) is None
    # A Game Host role holder who is not the host.
    assert await rp.advance_check(_interaction(2, roles=[777]), host_id=1, db=db, pacing=pacing, has_voted=False) is None
    # A plain voter on a scheduled game whose grace window has elapsed.
    assert await rp.advance_check(_interaction(3), host_id=1, db=db, pacing=pacing, has_voted=True) is None
    # The same voter before the window: refused with the unlock time.
    fresh = RoundPacing(round_seconds=0, scheduled=True)
    fresh.open()
    msg = await rp.advance_check(_interaction(3), host_id=1, db=db, pacing=fresh, has_voted=True)
    assert msg is not None and "Not yet" in msg
    # A non-voter, and anyone at all on a hosted game.
    msg = await rp.advance_check(_interaction(4), host_id=1, db=db, pacing=pacing, has_voted=False)
    assert msg is not None and "Vote first" in msg
    hosted = RoundPacing(round_seconds=0, scheduled=False, opened_at=1.0)
    assert await rp.advance_check(_interaction(3), host_id=1, db=db, pacing=hosted, has_voted=True) == rp.ADVANCE_DENIED


# ── launch_pacing: the whole read a launch does, in one call ─────────────────


async def _dial_options(db: GamesDb, game_type: str, options: dict) -> None:
    await db.execute(
        "INSERT INTO games_game_config (guild_id, game_type, options) VALUES (?, ?, ?)",
        (4242, game_type, json.dumps(options)),
    )


async def test_launch_pacing_reads_the_dashboard_dials_and_the_countdown(sync_db_path):
    db = GamesDb(sync_db_path)
    await _dial_options(db, "wyr", {"round_seconds": 45, "max_rounds": 3})
    pacing = await rp.launch_pacing(db, "wyr", 4242, {"start_in": 5})
    assert (pacing.round_seconds, pacing.max_rounds) == (45, 3)
    # start_in minutes became an epoch roughly five minutes out.
    assert pacing.start_epoch is not None
    assert 4 * 60 < pacing.start_epoch - time.time() < 6 * 60
    # The row rides along so a lobby game reads its roster dials without a
    # second fetch.
    assert pacing.game_opts["max_rounds"] == 3


async def test_launch_pacing_option_beats_dial_beats_builtin_default(sync_db_path):
    db = GamesDb(sync_db_path)
    # Nothing configured at all: the built-ins, and no countdown.
    bare = await rp.launch_pacing(db, "nhie", 4242, {})
    assert (bare.round_seconds, bare.max_rounds, bare.start_epoch) == (0, DEFAULT_MAX_ROUNDS, None)
    # A game with its own pace passes it in; the dial still wins over it.
    defaulted = await rp.launch_pacing(db, "nhie", 4242, {}, default_round_seconds=45)
    assert defaulted.round_seconds == 45
    await _dial_options(db, "nhie", {"round_seconds": 20})
    dialled = await rp.launch_pacing(db, "nhie", 4242, {}, default_round_seconds=45)
    assert dialled.round_seconds == 20
    # And the launch's own option wins over the dial — even at 0 (host-paced).
    chosen = await rp.launch_pacing(db, "nhie", 4242, {"round_seconds": 0}, default_round_seconds=45)
    assert chosen.round_seconds == 0


@pytest.mark.parametrize(
    ("start_epoch", "expected"),
    [
        pytest.param(1_700_000_000, {"a": 1, "start_epoch": 1_700_000_000}, id="countdown-is-stamped"),
        pytest.param(None, {"a": 1}, id="no-countdown-leaves-no-key"),
        pytest.param(0, {"a": 1}, id="zero-is-no-countdown-not-a-stamped-zero"),
    ],
)
def test_launch_pacing_stamps_the_payload_only_when_there_is_a_countdown(start_epoch, expected):
    pacing = rp.LaunchPacing(round_seconds=0, max_rounds=10, start_epoch=start_epoch)
    payload = {"a": 1}
    assert pacing.stamp(payload) is payload
    assert payload == expected
