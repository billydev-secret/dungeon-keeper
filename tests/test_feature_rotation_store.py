"""Storage and quest-board coupling for the daily feature-channel rotation.

Two halves. The first covers ``feature_rotation/store.py``: settings defaults,
the exactly-once day claims, and the pool round-trip. The second is the part
that actually changes member-visible behaviour — that a hidden room's
channel-bound quests leave the daily board, that everything else is untouched,
and that the open room gets its reserved slot.

The load-bearing assertion is ``test_rotation_off_leaves_the_board_untouched``:
every guild that never turns this on must draw exactly the board it drew
before the feature existed.
"""

from __future__ import annotations

import json


import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.feature_rotation.logic import Room, day_ordinal, featured_channel_ids
from bot_modules.feature_rotation.store import (
    RotationConfig,
    blocked_quest_kinds_on,
    claim_announce,
    claim_flip,
    delete_room,
    featured_quest_kinds_on,
    get_config,
    get_room_snapshot,
    is_hidden_by_rotation,
    list_pool,
    list_pool_state,
    mark_hidden,
    mark_visible,
    release_announce,
    release_flip,
    rotation_day_for,
    rotation_tz,
    save_config,
    upsert_room,
)
from bot_modules.services.economy_quests_service import (
    _pin_featured_room,
    assigned_board_ids,
)
from bot_modules.services.economy_service import EconSettings

GUILD = 4242
USER = 777
DAY = "2026-08-29"

WHISPER, GUESS = 101, 102


@pytest.fixture
def conn(sync_db_path):
    with open_db(sync_db_path) as c:
        yield c


def _enable(c, rooms, *, rooms_per_day=1, announce_channel_id=0):
    save_config(
        c,
        RotationConfig(
            guild_id=GUILD,
            enabled=True,
            rooms_per_day=rooms_per_day,
            announce_channel_id=announce_channel_id,
        ),
    )
    for room in rooms:
        upsert_room(c, GUILD, room)


def _add_quest(c, *, qid, kind, qtype="daily", active=1):
    c.execute(
        "INSERT INTO econ_quests (id, guild_id, title, qtype, active, "
        "trigger_kind, pair_tag, created_at) VALUES (?, ?, ?, ?, ?, ?, '', 0)",
        (qid, GUILD, f"q{qid}", qtype, active, kind),
    )


# ── settings ─────────────────────────────────────────────────────────────────


def test_config_defaults_when_the_guild_has_no_row(conn):
    cfg = get_config(conn, GUILD)
    assert cfg.enabled is False
    assert cfg.rooms_per_day == 1
    assert cfg.announce_hour == 9


def test_config_round_trips(conn):
    save_config(
        conn,
        RotationConfig(
            guild_id=GUILD, enabled=True, announce_channel_id=55,
            announce_hour=11, rooms_per_day=2,
        ),
    )
    cfg = get_config(conn, GUILD)
    assert (cfg.enabled, cfg.announce_channel_id, cfg.announce_hour) == (True, 55, 11)
    assert cfg.rooms_per_day == 2


def test_the_rotation_reads_the_guilds_shared_timezone(conn):
    """One clock, not two.

    The flip has to land on the same midnight the quest board freezes its pool
    on. A dial of the rotation's own could be set to a different value, and the
    room would then change hours away from the board that describes it.
    """
    conn.execute(
        "INSERT INTO config (guild_id, key, value) VALUES (?, 'tz_offset_hours', '-4')",
        (GUILD,),
    )
    assert rotation_tz(conn, GUILD) == -4
    assert not hasattr(RotationConfig(guild_id=GUILD), "tz_offset_hours")


def test_a_failed_flip_hands_the_day_back(conn):
    """A claim is taken before the Discord call, so a failure must release it.

    Otherwise one missing permission costs the whole day: the guard only fires
    again tomorrow, and the rooms sit in yesterday's arrangement until then.
    """
    assert claim_flip(conn, GUILD, DAY) is True
    assert claim_flip(conn, GUILD, DAY) is False
    release_flip(conn, GUILD, DAY)
    assert claim_flip(conn, GUILD, DAY) is True


def test_releasing_never_clobbers_a_later_claim(conn):
    """The release is conditional on the day still being ours."""
    assert claim_flip(conn, GUILD, DAY) is True
    release_flip(conn, GUILD, "2026-08-28")
    assert get_config(conn, GUILD).last_flip_date == DAY


def test_a_failed_announcement_hands_the_day_back(conn):
    assert claim_announce(conn, GUILD, DAY) is True
    release_announce(conn, GUILD, DAY)
    assert claim_announce(conn, GUILD, DAY) is True


def test_saving_settings_cannot_replay_todays_flip(conn):
    """An admin pressing Save must not re-trigger a flip already claimed."""
    assert claim_flip(conn, GUILD, DAY) is True
    save_config(conn, RotationConfig(guild_id=GUILD, enabled=True))
    assert get_config(conn, GUILD).last_flip_date == DAY
    assert claim_flip(conn, GUILD, DAY) is False


def test_rooms_per_day_is_floored_at_one(conn):
    save_config(conn, RotationConfig(guild_id=GUILD, rooms_per_day=0))
    assert get_config(conn, GUILD).rooms_per_day == 1


# ── exactly-once claims ──────────────────────────────────────────────────────


def test_flip_is_claimed_once_per_day(conn):
    assert claim_flip(conn, GUILD, DAY) is True
    assert claim_flip(conn, GUILD, DAY) is False
    assert claim_flip(conn, GUILD, "2026-08-30") is True


def test_a_stale_claim_does_not_let_yesterday_run_again(conn):
    assert claim_flip(conn, GUILD, "2026-08-30") is True
    assert claim_flip(conn, GUILD, DAY) is False


def test_announce_and_flip_claims_are_independent(conn):
    assert claim_flip(conn, GUILD, DAY) is True
    assert claim_announce(conn, GUILD, DAY) is True
    assert claim_announce(conn, GUILD, DAY) is False


# ── pool ─────────────────────────────────────────────────────────────────────


def test_pool_round_trips_including_kind_lists(conn):
    upsert_room(
        conn, GUILD,
        Room(GUESS, position=2, label="Guess Who", blurb="Name the face",
             quest_kinds=("guess", "guess_win"), blocked_kinds=("guess",)),
    )
    (room,) = list_pool(conn, GUILD)
    assert room.channel_id == GUESS
    assert room.label == "Guess Who"
    assert room.quest_kinds == ("guess", "guess_win")
    assert room.blocked_kinds == ("guess",)


def test_launch_game_and_options_round_trip(conn):
    upsert_room(
        conn, GUILD,
        Room(GUESS, launch_game="ama",
             launch_options='{"mode": "screened", "format": "panel"}'),
    )
    (room,) = list_pool(conn, GUILD)
    assert room.launch_game == "ama"
    assert json.loads(room.launch_options) == {"mode": "screened", "format": "panel"}


def test_a_room_with_no_game_stores_blank_not_null(conn):
    # Every row written before migration 197 reads as "no game", which is the
    # pre-existing behaviour and the safe default.
    upsert_room(conn, GUILD, Room(GUESS))
    (room,) = list_pool(conn, GUILD)
    assert room.launch_game == ""
    assert room.launch_options == ""


def test_clearing_a_rooms_game_clears_it(conn):
    upsert_room(conn, GUILD, Room(GUESS, launch_game="ama", launch_options='{"mode": "screened"}'))
    upsert_room(conn, GUILD, Room(GUESS))
    (room,) = list_pool(conn, GUILD)
    assert room.launch_game == ""


def test_is_hidden_by_rotation_tracks_the_observed_state(conn):
    # The scheduler consults this to decide whether to launch into a room, so
    # it must answer for the state members actually see, not a derived plan.
    upsert_room(conn, GUILD, Room(GUESS))
    assert is_hidden_by_rotation(conn, GUILD, GUESS) is False

    mark_hidden(conn, GUILD, GUESS, [], 123.0)
    assert is_hidden_by_rotation(conn, GUILD, GUESS) is True

    mark_visible(conn, GUILD, GUESS)
    assert is_hidden_by_rotation(conn, GUILD, GUESS) is False


def test_a_channel_outside_the_pool_is_never_hidden_by_the_rotation(conn):
    # This is what makes the scheduler safe to gate unconditionally: a guild
    # that never turns the rotation on can never have a game silently skipped.
    upsert_room(conn, GUILD, Room(GUESS))
    mark_hidden(conn, GUILD, GUESS, [], 123.0)
    assert is_hidden_by_rotation(conn, GUILD, WHISPER) is False
    assert is_hidden_by_rotation(conn, GUILD + 1, GUESS) is False


def test_upsert_updates_rather_than_duplicates(conn):
    upsert_room(conn, GUILD, Room(GUESS, label="old"))
    upsert_room(conn, GUILD, Room(GUESS, label="new"))
    pool = list_pool(conn, GUILD)
    assert len(pool) == 1 and pool[0].label == "new"


def test_hidden_state_round_trips_and_upsert_does_not_clear_it(conn):
    """Editing a room's label mid-hide must not lose its saved permissions."""
    upsert_room(conn, GUILD, Room(GUESS))
    mark_hidden(conn, GUILD, GUESS, [{"id": 1, "type": "role", "allow": 0, "deny": 1024}], 123.0)
    assert list_pool_state(conn, GUILD) == {GUESS: True}

    upsert_room(conn, GUILD, Room(GUESS, label="renamed"))
    stored, hidden = get_room_snapshot(conn, GUILD, GUESS)
    assert hidden is True
    assert stored == [{"id": 1, "type": "role", "allow": 0, "deny": 1024}]

    mark_visible(conn, GUILD, GUESS)
    assert get_room_snapshot(conn, GUILD, GUESS) == ([], False)


def test_an_unreadable_snapshot_reads_as_empty_rather_than_raising(conn):
    upsert_room(conn, GUILD, Room(GUESS))
    conn.execute(
        "UPDATE feature_rotation_pool SET stored_overwrites = 'not json', "
        "hidden_at = 1 WHERE guild_id = ? AND channel_id = ?",
        (GUILD, GUESS),
    )
    stored, hidden = get_room_snapshot(conn, GUILD, GUESS)
    assert stored == [] and hidden is True


def test_delete_room(conn):
    upsert_room(conn, GUILD, Room(GUESS))
    assert delete_room(conn, GUILD, GUESS) is True
    assert delete_room(conn, GUILD, GUESS) is False
    assert list_pool(conn, GUILD) == []


# ── derived day ──────────────────────────────────────────────────────────────


def test_rotation_day_is_none_when_disabled(conn):
    upsert_room(conn, GUILD, Room(GUESS))
    assert rotation_day_for(conn, GUILD, DAY) is None


def test_rotation_day_is_none_with_an_empty_pool(conn):
    save_config(conn, RotationConfig(guild_id=GUILD, enabled=True))
    assert rotation_day_for(conn, GUILD, DAY) is None


def test_rotation_day_features_exactly_one_room(conn):
    _enable(conn, [Room(WHISPER, position=1), Room(GUESS, position=2)])
    day = rotation_day_for(conn, GUILD, DAY)
    assert day is not None
    assert len(day.featured) == 1
    assert set(day.plan.hide) == {WHISPER, GUESS} - set(day.featured)


def test_the_announce_channel_is_protected_from_hiding(conn):
    _enable(
        conn,
        [Room(WHISPER, position=1), Room(GUESS, position=2)],
        announce_channel_id=WHISPER,
    )
    day = rotation_day_for(conn, GUILD, DAY)
    assert day is not None
    assert WHISPER not in day.plan.hide


def test_blocked_and_featured_kinds_reach_callers(conn):
    _enable(
        conn,
        [
            Room(WHISPER, position=1, quest_kinds=("whisper",)),
            Room(GUESS, position=2, quest_kinds=("guess",), blocked_kinds=("guess",)),
        ],
    )
    featured = featured_channel_ids(list_pool(conn, GUILD), day_ordinal(DAY), 1)
    if featured == [GUESS]:
        assert blocked_quest_kinds_on(conn, GUILD, DAY) == frozenset()
        assert featured_quest_kinds_on(conn, GUILD, DAY) == frozenset({"guess"})
    else:
        assert blocked_quest_kinds_on(conn, GUILD, DAY) == frozenset({"guess"})
        assert featured_quest_kinds_on(conn, GUILD, DAY) == frozenset({"whisper"})


def test_kind_queries_are_empty_when_the_rotation_is_off(conn):
    upsert_room(conn, GUILD, Room(GUESS, blocked_kinds=("guess",)))
    assert blocked_quest_kinds_on(conn, GUILD, DAY) == frozenset()
    assert featured_quest_kinds_on(conn, GUILD, DAY) == frozenset()


# ── quest-board coupling ─────────────────────────────────────────────────────


def test_rotation_off_leaves_the_board_untouched(conn):
    """The regression guard: no rotation row ⇒ the board of before the feature."""
    for qid, kind in ((1, "guess"), (2, "whisper"), (3, "confession")):
        _add_quest(conn, qid=qid, kind=kind)
    before = assigned_board_ids(conn, GUILD, USER, "daily", DAY)

    upsert_room(conn, GUILD, Room(GUESS, quest_kinds=("guess",), blocked_kinds=("guess",)))
    conn.execute("DELETE FROM econ_quest_pool_snapshots")
    after = assigned_board_ids(conn, GUILD, USER, "daily", DAY)
    assert before == after


def test_a_hidden_rooms_channel_bound_quest_leaves_the_daily_board(conn):
    for qid, kind in ((1, "guess"), (2, "whisper"), (3, "confession")):
        _add_quest(conn, qid=qid, kind=kind)
    _enable(
        conn,
        [
            Room(WHISPER, position=1, quest_kinds=("whisper",)),
            Room(GUESS, position=2, quest_kinds=("guess",), blocked_kinds=("guess",)),
        ],
    )
    day = rotation_day_for(conn, GUILD, DAY)
    assert day is not None
    board = assigned_board_ids(conn, GUILD, USER, "daily", DAY)
    if GUESS in day.plan.hide:
        assert 1 not in board, "guess quest should be gone while its room is shut"
    else:
        assert 1 in board or len(board) == 2


def test_a_hidden_rooms_panel_driven_quest_stays_on_the_board(conn):
    """Whisper is reachable from an ephemeral panel, so hiding it costs nothing."""
    _add_quest(conn, qid=1, kind="whisper")
    _add_quest(conn, qid=2, kind="confession")
    _enable(
        conn,
        [
            Room(WHISPER, position=1, quest_kinds=("whisper",)),   # no blocked_kinds
            Room(GUESS, position=2, quest_kinds=("guess",)),
        ],
    )
    board = assigned_board_ids(conn, GUILD, USER, "daily", DAY)
    assert 1 in board


def test_the_weekly_board_ignores_todays_rotation(conn):
    """A week spans every room; one day's shut door must not close a week."""
    _add_quest(conn, qid=10, kind="guess", qtype="weekly")
    _add_quest(conn, qid=11, kind="confession", qtype="weekly")
    _enable(
        conn,
        [
            Room(WHISPER, position=1, quest_kinds=("whisper",)),
            Room(GUESS, position=2, quest_kinds=("guess",), blocked_kinds=("guess",)),
        ],
    )
    assert assigned_board_ids(conn, GUILD, USER, "weekly", DAY) == {10, 11}


def test_the_featured_room_gets_a_reserved_slot(conn):
    """A single-room pool is always featured, so the pin is deterministic."""
    _add_quest(conn, qid=1, kind="guess")
    for qid in range(2, 8):
        _add_quest(conn, qid=qid, kind="message_sent")
    _enable(conn, [Room(GUESS, position=1, quest_kinds=("guess",))])
    board = assigned_board_ids(conn, GUILD, USER, "daily", DAY)
    assert 1 in board, "the open room's quest should be pinned onto the board"


def test_the_featured_pin_takes_only_one_slot(conn):
    """Three quests share the featured kind; at most one may be pinned."""
    for qid in (1, 2, 3):
        _add_quest(conn, qid=qid, kind="guess")
    for qid in range(4, 10):
        _add_quest(conn, qid=qid, kind="message_sent")
    _enable(conn, [Room(GUESS, position=1, quest_kinds=("guess",))])
    board = assigned_board_ids(conn, GUILD, USER, "daily", DAY)
    assert len(board & {1, 2, 3}) == 1
    assert len(board) == 2, "the board must not grow to make room for the pin"


def test_the_featured_pin_is_stable_within_a_day_and_moves_between_days(conn):
    for qid in (1, 2, 3):
        _add_quest(conn, qid=qid, kind="guess")
    for qid in range(4, 10):
        _add_quest(conn, qid=qid, kind="message_sent")
    _enable(conn, [Room(GUESS, position=1, quest_kinds=("guess",))])
    first = assigned_board_ids(conn, GUILD, USER, "daily", DAY)
    assert first == assigned_board_ids(conn, GUILD, USER, "daily", DAY)
    later = assigned_board_ids(conn, GUILD, USER, "daily", "2026-09-05")
    assert (first & {1, 2, 3}) != (later & {1, 2, 3})


def test_the_pin_never_surfaces_an_inactive_quest(conn):
    _add_quest(conn, qid=1, kind="guess", active=0)
    for qid in range(2, 8):
        _add_quest(conn, qid=qid, kind="message_sent")
    _enable(conn, [Room(GUESS, position=1, quest_kinds=("guess",))])
    assert 1 not in assigned_board_ids(conn, GUILD, USER, "daily", DAY)


def test_a_missing_rotation_table_does_not_break_the_board(conn):
    """A board read must never be what surfaces an unapplied migration."""
    _add_quest(conn, qid=1, kind="guess")
    _add_quest(conn, qid=2, kind="whisper")
    conn.execute("DROP TABLE feature_rotation_config")
    assert assigned_board_ids(conn, GUILD, USER, "daily", DAY) == {1, 2}


def test_the_reserved_slot_is_not_held_for_a_quest_already_drawn(conn):
    """The board must never come up a quest short because of the pin.

    A slot was reserved whenever the open room owned *any* quest, so on a day
    the plain draw already produced that very quest the reservation bought
    nothing and the member spent the day looking at n-1 quests. Swept across
    members because which draw collides is per-member.
    """
    settings = EconSettings(enabled=True, quest_board_daily=3)
    _add_quest(conn, qid=1, kind="bio_set")           # a pending setup pin
    for qid in (2, 3, 4):
        _add_quest(conn, qid=qid, kind="guess")       # the open room's quests
    for qid in range(5, 14):
        _add_quest(conn, qid=qid, kind="message_sent")
    _enable(conn, [Room(GUESS, position=1, quest_kinds=("guess",))])
    for user_id in range(1, 40):
        conn.execute("DELETE FROM econ_quest_pool_snapshots")
        board = assigned_board_ids(conn, GUILD, user_id, "daily", DAY, settings)
        assert len(board) == 3, f"user {user_id} drew a short board: {board}"


def test_the_pin_stands_down_rather_than_shrink_a_board(conn):
    """A locked pair can't be split, so on a 2-slot board there is no room.

    Eviction drops a producer/consumer pair whole rather than half of it, so
    seating the pin on a two-slot board whose draw *was* a pair would leave
    one quest where there were two. Advertising the open room isn't worth a
    smaller board.
    """
    pairs = {1: 2, 2: 1}
    assert _pin_featured_room({1, 2}, {9}, 2, pairs) == {1, 2}
    # ...but with a spare singleton to evict it seats normally.
    assert _pin_featured_room({1, 2, 3}, {9}, 3, pairs) == {1, 2, 9}


def test_the_featured_pin_is_frozen_against_a_mid_day_edit(conn):
    """An admin editing a room at noon must not move anyone's board.

    The blocked kinds were captured in the period snapshot from the start; the
    featured candidates were re-read live on every board view, so a mid-period
    edit moved the reserved slot under everyone who reloaded — and could push
    a quest a member had already made progress on off the board.
    """
    for qid in (1, 2):
        _add_quest(conn, qid=qid, kind="guess")
    for qid in range(3, 10):
        _add_quest(conn, qid=qid, kind="message_sent")
    _enable(conn, [Room(GUESS, position=1, quest_kinds=("guess",))])
    first = assigned_board_ids(conn, GUILD, USER, "daily", DAY)
    assert first & {1, 2}, "the open room should have taken its slot"

    upsert_room(conn, GUILD, Room(GUESS, position=1, quest_kinds=()))
    assert assigned_board_ids(conn, GUILD, USER, "daily", DAY) == first
    # The next period is free to reflect the edit: nothing is pinned any more.
    # (The quests themselves stay in the pool, so the ordinary draw may still
    # land one — what has to be gone is the *reservation*.)
    assigned_board_ids(conn, GUILD, USER, "daily", "2026-08-30")
    row = conn.execute(
        "SELECT featured_json FROM econ_quest_pool_snapshots "
        "WHERE guild_id = ? AND qtype = 'daily' AND period_idx = ?",
        (GUILD, day_ordinal("2026-08-30")),
    ).fetchone()
    assert json.loads(row["featured_json"]) == []
