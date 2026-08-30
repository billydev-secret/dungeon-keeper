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
    list_pool,
    list_pool_state,
    mark_hidden,
    mark_visible,
    rotation_day_for,
    save_config,
    upsert_room,
)
from bot_modules.services.economy_quests_service import assigned_board_ids

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
    assert cfg.tz_offset_hours == -7


def test_config_round_trips(conn):
    save_config(
        conn,
        RotationConfig(
            guild_id=GUILD, enabled=True, announce_channel_id=55,
            announce_hour=11, tz_offset_hours=-5, rooms_per_day=2,
        ),
    )
    cfg = get_config(conn, GUILD)
    assert (cfg.enabled, cfg.announce_channel_id, cfg.announce_hour) == (True, 55, 11)
    assert (cfg.tz_offset_hours, cfg.rooms_per_day) == (-5, 2)


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
