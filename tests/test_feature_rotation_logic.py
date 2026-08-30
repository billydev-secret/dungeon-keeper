"""Pure-logic tests for the daily feature-channel rotation.

Everything here is a function of its arguments — no guild, no database, no
clock — which is the point of keeping the rotation's decisions in
``feature_rotation/logic.py``. The DB and quest-board coupling live in
``test_feature_rotation_store.py``.
"""

from __future__ import annotations


import pytest

from bot_modules.economy import quests
from bot_modules.feature_rotation.logic import (
    Room,
    blocked_quest_kinds,
    build_announcement,
    day_ordinal,
    featured_channel_ids,
    featured_indices,
    featured_quest_kinds,
    format_kinds,
    format_launch_options,
    local_day,
    local_hour,
    parse_kinds,
    parse_launch_options,
    plan_games,
    plan_visibility,
    resolve_day,
    restore_all,
    rotating_rooms,
)

WHISPER, GUESS, CONF, RISKY, AMA = 101, 102, 103, 104, 105


def _pool() -> list[Room]:
    return [
        Room(WHISPER, position=1, label="Whisper", quest_kinds=("whisper", "whisper_guess")),
        Room(GUESS, position=2, label="Guess Who",
             quest_kinds=("guess", "guess_win", "guess_post"),
             blocked_kinds=("guess", "guess_win")),
        Room(CONF, position=3, label="Confessions",
             quest_kinds=("confession", "confession_reply"),
             blocked_kinds=("confession_reply",)),
    ]


# ── clock ────────────────────────────────────────────────────────────────────


def test_local_day_and_hour_apply_the_offset():
    # 2026-08-29 03:00 UTC at UTC-7 is still 2026-08-28, 20:00 local.
    ts = 1787972400.0  # 2026-08-29T03:00:00Z
    assert local_day(ts, -7) == "2026-08-28"
    assert local_hour(ts, -7) == 20
    assert local_day(ts, 0) == "2026-08-29"
    assert local_hour(ts, 0) == 3


def test_day_ordinal_matches_the_quest_boards_period_index():
    """The whole design rests on these two being the same integer."""
    for day in ("2026-01-01", "2026-08-29", "2027-03-15"):
        assert day_ordinal(day) == quests.period_index("daily", day)


# ── the walk ─────────────────────────────────────────────────────────────────


def test_rotating_rooms_excludes_opted_out_and_orders_stably():
    rooms = [
        Room(3, position=1),
        Room(1, position=1),               # same position, lower id sorts first
        Room(9, position=0, in_rotation=False),
    ]
    assert [r.channel_id for r in rotating_rooms(rooms)] == [1, 3]


@pytest.mark.parametrize(
    ("ordinal", "expected"),
    [(0, [0]), (1, [1]), (2, [2]), (3, [0]), (4, [1])],
)
def test_featured_indices_cycle_one_per_day(ordinal, expected):
    assert featured_indices(ordinal, 1, 3) == expected


def test_featured_indices_two_per_day_walks_two_at_a_time():
    assert featured_indices(0, 2, 4) == [0, 1]
    assert featured_indices(1, 2, 4) == [2, 3]
    assert featured_indices(2, 2, 4) == [0, 1]


def test_featured_indices_wrap_around_the_end_of_the_pool():
    # start 2 of a 3-pool with 2 rooms/day wraps back to index 0.
    assert featured_indices(1, 2, 3) == [2, 0]


@pytest.mark.parametrize(
    ("rooms_per_day", "pool_len", "expected_len"),
    [(5, 3, 3), (1, 1, 1), (0, 3, 0), (2, 0, 0)],
)
def test_featured_indices_degrade_gracefully(rooms_per_day, pool_len, expected_len):
    got = featured_indices(7, rooms_per_day, pool_len)
    assert len(got) == expected_len
    assert len(set(got)) == len(got)


def test_every_room_is_featured_within_one_cycle():
    rooms = _pool()
    seen: set[int] = set()
    base = day_ordinal("2026-08-29")
    for offset in range(len(rooms)):
        seen.update(featured_channel_ids(rooms, base + offset, 1))
    assert seen == {WHISPER, GUESS, CONF}


def test_consecutive_days_never_repeat_the_same_room():
    rooms = _pool()
    base = day_ordinal("2026-08-29")
    days = [featured_channel_ids(rooms, base + i, 1) for i in range(6)]
    for a, b in zip(days, days[1:]):
        assert a != b


# ── visibility ───────────────────────────────────────────────────────────────


def test_plan_hides_everything_except_the_featured_room():
    plan = plan_visibility(_pool(), [GUESS])
    assert plan.show == (GUESS,)
    assert set(plan.hide) == {WHISPER, CONF}


def test_a_room_that_opted_out_of_hiding_is_never_hidden():
    rooms = _pool()
    rooms[0] = Room(WHISPER, position=1, hide_when_off=False)
    plan = plan_visibility(rooms, [GUESS])
    assert WHISPER in plan.show
    assert WHISPER not in plan.hide


def test_the_announcement_channel_is_never_hidden_even_when_pooled():
    """Hiding the room the announcement posts into would hide the announcement."""
    rooms = [*_pool(), Room(999, position=4, label="Main")]
    plan = plan_visibility(rooms, [GUESS], protected={999})
    assert 999 in plan.show
    assert 999 not in plan.hide


def test_a_room_out_of_rotation_is_reopened_not_skipped():
    """Un-ticking "in rotation" on a hidden room has to bring it back.

    It used to land in neither list, so a room that happened to be hidden the
    day an admin took it out of the rotation stayed hidden forever — nothing
    would ever plan its return.
    """
    rooms = [*_pool(), Room(555, position=9, in_rotation=False)]
    plan = plan_visibility(rooms, [GUESS])
    assert 555 in plan.show
    assert 555 not in plan.hide
    # ...and it still takes no turn at being the featured room.
    assert 555 not in featured_channel_ids(rooms, day_ordinal("2026-08-29"), 1)


def test_restore_all_reopens_the_whole_pool_including_non_rotating_rooms():
    """What "the rotation is off" has to mean, and the way back out."""
    rooms = [*_pool(), Room(555, position=9, in_rotation=False)]
    plan = restore_all(rooms)
    assert set(plan.show) == {r.channel_id for r in rooms}
    assert plan.hide == ()


# ── quest kinds ──────────────────────────────────────────────────────────────


def test_blocked_kinds_come_only_from_hidden_rooms():
    assert blocked_quest_kinds(_pool(), [GUESS, CONF]) == frozenset(
        {"guess", "guess_win", "confession_reply"}
    )
    assert blocked_quest_kinds(_pool(), []) == frozenset()


def test_a_hidden_room_with_no_channel_bound_quests_blocks_nothing():
    """Whisper is playable from an ephemeral panel, so hiding it costs no quest."""
    assert blocked_quest_kinds(_pool(), [WHISPER]) == frozenset()


def test_featured_kinds_are_the_open_rooms_quests():
    assert featured_quest_kinds(_pool(), [WHISPER]) == frozenset(
        {"whisper", "whisper_guess"}
    )


def test_resolve_day_agrees_with_its_parts():
    day = resolve_day(_pool(), local_day_str="2026-08-29", rooms_per_day=1)
    assert len(day.featured) == 1
    assert set(day.plan.show) == set(day.featured)
    assert set(day.plan.hide) == {WHISPER, GUESS, CONF} - set(day.featured)
    assert day.blocked_quest_kinds == blocked_quest_kinds(_pool(), list(day.plan.hide))
    assert day.featured_quest_kinds == featured_quest_kinds(_pool(), list(day.featured))


def test_resolve_day_is_stable_for_the_same_day():
    a = resolve_day(_pool(), local_day_str="2026-08-29", rooms_per_day=1)
    b = resolve_day(_pool(), local_day_str="2026-08-29", rooms_per_day=1)
    assert a == b


# ── kind lists ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a,b", ("a", "b")),
        (" a , b ", ("a", "b")),
        ("a,,b", ("a", "b")),
        ("a,a,b", ("a", "b")),
        ("", ()),
        ("   ", ()),
    ],
)
def test_parse_kinds(raw, expected):
    assert parse_kinds(raw) == expected


def test_format_kinds_round_trips():
    assert parse_kinds(format_kinds(("guess", "guess_win"))) == ("guess", "guess_win")
    assert format_kinds("a, b") == "a,b"


# ── announcement copy ────────────────────────────────────────────────────────


def test_announcement_names_the_single_open_room():
    title, body = build_announcement(_pool(), [GUESS])
    assert title == "Today's feature"
    assert "Guess Who" in body


def test_announcement_pluralises_for_two_rooms():
    title, body = build_announcement(_pool(), [GUESS, CONF])
    assert title == "Today's features"
    assert "Guess Who" in body and "Confessions" in body


def test_announcement_includes_blurbs():
    rooms = [Room(GUESS, label="Guess Who", blurb="Crop a photo, name the face.")]
    _, body = build_announcement(rooms, [GUESS])
    assert "Crop a photo" in body


def test_a_room_with_announce_off_says_nothing():
    rooms = [Room(GUESS, label="Guess Who", announce=False)]
    assert build_announcement(rooms, [GUESS]) is None


def test_an_unlabelled_room_falls_back_to_a_channel_mention():
    _, body = build_announcement([Room(GUESS)], [GUESS])
    assert f"<#{GUESS}>" in body


def test_day_ordinal_rejects_a_non_date():
    with pytest.raises(ValueError):
        day_ordinal("not-a-day")


# ── the game lifecycle ───────────────────────────────────────────────────────
#
# A room's game is started on the day it is featured and ended on every day it
# is not. Rooms that name no game are never touched either way, which is what
# keeps whisper/confessions/guess-who on the "out of sight, still running"
# behaviour the rotation shipped with.


def _game_pool() -> list[Room]:
    """The pool with one room owning a game, one owning none."""
    return [
        Room(WHISPER, position=1, label="Whisper"),
        Room(AMA, position=2, label="AMA", launch_game="ama"),
        Room(CONF, position=3, label="Confessions"),
    ]


def _featured_on(rooms, ordinal, rooms_per_day=1):
    return featured_channel_ids(rooms, ordinal, rooms_per_day)


def test_the_featured_rooms_game_starts_and_no_other():
    rooms = _game_pool()
    # Find the ordinal on which the AMA room is the featured one.
    ordinal = next(o for o in range(3) if AMA in _featured_on(rooms, o))
    games = plan_games(rooms, _featured_on(rooms, ordinal))
    assert games.start == ((AMA, "ama"),)
    assert games.end == ()


def test_a_room_that_is_not_featured_has_its_game_ended():
    rooms = _game_pool()
    ordinal = next(o for o in range(3) if AMA not in _featured_on(rooms, o))
    games = plan_games(rooms, _featured_on(rooms, ordinal))
    assert games.start == ()
    assert games.end == ((AMA, "ama"),)


def test_rooms_without_a_game_are_never_started_or_ended():
    # Whisper and confessions carry no launch_game, so neither list can name
    # them on any day of the cycle — the rotation manages only what it was told
    # about, and reaching into a room it wasn't would end a game it never began.
    rooms = _game_pool()
    for ordinal in range(6):
        games = plan_games(rooms, _featured_on(rooms, ordinal))
        touched = {cid for cid, _ in games.end} | {cid for cid, _ in games.start}
        assert touched <= {AMA}


def test_a_room_that_never_hides_still_has_its_game_ended():
    # hide_when_off off means the room is never in the hide plan, so keying the
    # end off "this flip hid it" would strand its game open forever. The end is
    # keyed off "not featured today" precisely so this case works.
    rooms = [
        Room(WHISPER, position=1),
        Room(AMA, position=2, launch_game="ama", hide_when_off=False),
    ]
    ordinal = next(o for o in range(4) if AMA not in _featured_on(rooms, o))
    plan = plan_visibility(rooms, _featured_on(rooms, ordinal))
    assert AMA not in plan.hide          # never hidden …
    # … and still ended:
    assert plan_games(rooms, _featured_on(rooms, ordinal)).end == ((AMA, "ama"),)


def test_a_room_taken_out_of_rotation_still_has_its_game_ended():
    # Un-ticking "in rotation" on a room whose game is running must bring it to
    # a defined state, the same reasoning plan_visibility uses to *show* it.
    rooms = [
        Room(WHISPER, position=1),
        Room(AMA, position=2, launch_game="ama", in_rotation=False),
    ]
    games = plan_games(rooms, _featured_on(rooms, 0))
    assert games.end == ((AMA, "ama"),)
    assert games.start == ()


def test_two_featured_rooms_both_start_their_games():
    rooms = [
        Room(WHISPER, position=1, launch_game="ama"),
        Room(AMA, position=2, launch_game="ama"),
        Room(CONF, position=3),
    ]
    featured = _featured_on(rooms, 0, rooms_per_day=2)
    games = plan_games(rooms, featured)
    assert len(games.start) == 2
    assert {cid for cid, _ in games.start} == set(featured)


def test_resolve_day_carries_the_game_plan():
    rooms = _game_pool()
    day = resolve_day(rooms, local_day_str="2026-08-30", rooms_per_day=1)
    assert day.games == plan_games(rooms, list(day.featured))


def test_a_pool_with_no_games_plans_nothing():
    day = resolve_day(_pool(), local_day_str="2026-08-30", rooms_per_day=1)
    assert day.games.is_empty()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", {}),
        ("{}", {}),
        ('{"mode": "screened"}', {"mode": "screened"}),
        ("not json", {}),          # corrupt costs the options, never the launch
        ('["mode"]', {}),          # a list is not an options dict
        ("null", {}),
    ],
)
def test_parse_launch_options_is_lenient(raw, expected):
    assert parse_launch_options(raw) == expected


def test_launch_options_round_trip():
    options = {"mode": "screened", "format": "panel"}
    assert parse_launch_options(format_launch_options(options)) == options


def test_empty_launch_options_store_as_blank():
    # '' rather than '{}' so "no options" is one value in the column, not two.
    assert format_launch_options({}) == ""
    assert format_launch_options("") == ""


def test_the_end_plan_names_each_rooms_game():
    """The key is what tells the ender which registered closer to consult —
    Risky Rolls keeps its rounds in memory, invisible to a table lookup."""
    rooms = [
        Room(WHISPER, position=1, launch_game="risky_roll"),
        Room(AMA, position=2, launch_game="ama"),
    ]
    featured = _featured_on(rooms, 0)
    ended = dict(plan_games(rooms, featured).end)
    for channel_id, key in ended.items():
        assert key == next(r.launch_game for r in rooms if r.channel_id == channel_id)
