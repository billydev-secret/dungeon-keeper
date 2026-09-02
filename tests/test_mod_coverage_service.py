"""Unit tests for bot_modules.services.mod_coverage_service.

The gap half is seeded at exact local hours and asserted on arithmetic; the
hero half is asserted structurally, because ``query_activity_overlay`` reads
the wall clock and cannot be handed a fake ``now``.
"""

from __future__ import annotations

import time

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services import mod_coverage_service as mcs
from bot_modules.services.activity_graphs import query_activity_overlay
from tests.db_template import migrated_db

GUILD = 77
MOD_A, MOD_B, MEMBER, BOT = 100, 101, 200, 999


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "cov.db"
    migrated_db(path)
    with open_db(path) as c:
        c.execute(
            "INSERT INTO known_users (guild_id, user_id, username, is_bot) "
            "VALUES (?,?,?,1)",
            (GUILD, BOT, "spambot"),
        )
        c.commit()
        yield c


def _msg(conn, *, user_id: int, ts: float, mid: int) -> None:
    conn.execute(
        "INSERT INTO processed_messages "
        "(guild_id, message_id, channel_id, user_id, created_at, processed_at) "
        "VALUES (?,?,?,?,?,?)",
        (GUILD, mid, 5, user_id, int(ts), int(ts)),
    )


def _seed(conn, midnight: float, plan) -> None:
    """Seed messages at given (days_ago, local_hour, user_id) triples.

    ``midnight`` is a UTC epoch that is midnight on the guild's own clock, so
    ``local_hour`` lands in exactly the bucket the service reads it into.
    Anchoring on the caller's ``now`` instead would push every row forward by
    however far into the day ``now`` sits — and past the current hour the
    overlay reports None, because those hours are unlived rather than empty.
    """
    mid = 1
    for days_ago, hour, user_id in plan:
        _msg(conn, user_id=user_id, ts=midnight - days_ago * 86400 + hour * 3600, mid=mid)
        mid += 1
    conn.commit()


def _lived_hour(offset_hours: float = 0.0) -> int:
    """An hour of the local clock that has already happened today.

    The overlay's current-period line stops at the hour we are actually in, so
    a test that seeds "today at 09:00" and runs at 04:00 reads None and fails
    for reasons that have nothing to do with the code under test.
    """
    return int((time.time() + offset_hours * 3600) % 86400 // 3600)


def _local_midnight(offset_hours: float = 0.0) -> float:
    """A recent UTC epoch that is midnight on the guild's own clock."""
    now = time.time()
    shifted = now + offset_hours * 3600
    return (shifted - shifted % 86400) - offset_hours * 3600


def _hour(data, h):
    return next(r for r in data["hours"] if r["hour"] == h)


# ── Coverage arithmetic ──────────────────────────────────────────────


def test_covered_hour_counts_days_not_messages(conn):
    """One mod message a day is presence; forty in one day is still one day."""
    mid = _local_midnight()
    now = mid + 12 * 3600
    plan = []
    for d in range(1, 5):
        plan.append((d, 9, MEMBER))
        plan.append((d, 9, MOD_A))
    # A single day where one mod says a great deal. It must not out-vote the
    # other days, or a mod who binges once looks like permanent cover.
    plan += [(2, 14, MEMBER)] + [(2, 14, MOD_A)] * 40
    _seed(conn, mid, plan)

    data = mcs.compute_mod_coverage(
        conn, GUILD, mod_ids=[MOD_A, MOD_B], gap_days=10, now=now
    )

    nine = _hour(data, 9)
    assert nine["days_observed"] == 4
    assert nine["days_with_mod"] == 4
    assert nine["coverage_pct"] == 100.0
    assert nine["gap"] is False

    fourteen = _hour(data, 14)
    assert fourteen["days_observed"] == 1
    assert fourteen["days_with_mod"] == 1
    assert fourteen["server_messages"] == 41


def test_uncovered_hour_is_a_gap(conn):
    mid = _local_midnight()
    now = mid + 12 * 3600
    # 3am: members talk on four days, a mod only on one — 25%, under the bar.
    plan = [(d, 3, MEMBER) for d in range(1, 5)] + [(1, 3, MOD_A)]
    _seed(conn, mid, plan)

    data = mcs.compute_mod_coverage(conn, GUILD, mod_ids=[MOD_A], gap_days=10, now=now)

    three = _hour(data, 3)
    assert three["coverage_pct"] == 25.0
    assert three["gap"] is True
    assert data["busiest_uncovered"]["hour"] == 3


def test_quiet_hour_is_not_a_gap(conn):
    """Nothing happened, so nothing was missed."""
    mid = _local_midnight()
    now = mid + 12 * 3600
    _seed(conn, mid, [(1, 9, MEMBER), (1, 9, MOD_A)])

    data = mcs.compute_mod_coverage(conn, GUILD, mod_ids=[MOD_A], gap_days=10, now=now)

    silent = _hour(data, 4)
    assert silent["days_observed"] == 0
    assert silent["gap"] is False
    assert data["longest_gap"] is None
    assert data["busiest_uncovered"] is None


def test_bots_excluded_from_the_server_line(conn):
    mid = _local_midnight()
    now = mid + 12 * 3600
    _seed(conn, mid, [(1, 9, MEMBER)] + [(1, 9, BOT)] * 20)

    data = mcs.compute_mod_coverage(conn, GUILD, mod_ids=[MOD_A], gap_days=10, now=now)

    assert _hour(data, 9)["server_messages"] == 1


@pytest.mark.parametrize("offset", [0.0, -7.0, 5.5])
def test_hour_buckets_follow_the_guild_clock(conn, offset):
    """A message at local 09:00 lands in bucket 9 whatever the offset is."""
    mid = _local_midnight(offset)
    now = mid + 12 * 3600
    _seed(conn, mid, [(1, 9, MEMBER), (1, 9, MOD_A)])

    data = mcs.compute_mod_coverage(
        conn, GUILD, mod_ids=[MOD_A], gap_days=10, now=now, utc_offset_hours=offset
    )

    assert _hour(data, 9)["server_messages"] == 2
    assert _hour(data, 9)["days_with_mod"] == 1


# ── The longest-gap scan ─────────────────────────────────────────────


def _rows(gap_hours, *, active=range(24)):
    return [
        {
            "hour": h,
            "label": "",
            "server_messages": 10 if h in active else 0,
            "days_observed": 4 if h in active else 0,
            "days_with_mod": 0 if h in gap_hours else 4,
            "coverage_pct": 0.0 if h in gap_hours else 100.0,
            "busy": False,
            "gap": h in gap_hours and h in active,
        }
        for h in range(24)
    ]


def test_longest_gap_wraps_past_midnight():
    """22:00–03:00 is one six-hour hole, not two unremarkable ones."""
    got = mcs._longest_gap(_rows({22, 23, 0, 1, 2, 3}))
    assert got == {"start_hour": 22, "end_hour": 3, "hours": 6}


def test_longest_gap_picks_the_longer_of_two():
    got = mcs._longest_gap(_rows({1, 2, 8, 9, 10, 11}))
    assert got == {"start_hour": 8, "end_hour": 11, "hours": 4}


def test_longest_gap_all_day_is_capped_at_24():
    got = mcs._longest_gap(_rows(set(range(24))))
    assert got == {"start_hour": 0, "end_hour": 23, "hours": 24}


def test_longest_gap_none_when_everything_is_covered():
    assert mcs._longest_gap(_rows(set())) is None


def test_quiet_hour_breaks_a_run():
    """An hour with no traffic is not a gap, so it splits the stretch."""
    got = mcs._longest_gap(_rows({1, 2, 3, 4, 5}, active={1, 2, 4, 5}))
    assert got["hours"] == 2


# ── The moderator population ─────────────────────────────────────────


def test_no_mods_still_draws_the_server_side(conn):
    """A report that renders half a chart beats one that renders an error."""
    mid = _local_midnight()
    now = mid + 12 * 3600
    _seed(conn, mid, [(1, 9, MEMBER)])

    data = mcs.compute_mod_coverage(conn, GUILD, mod_ids=[], gap_days=10, now=now)

    assert data["mod_count"] == 0
    # Never the server's own line relabelled as the moderators'.
    assert data["mod_current"] == [None] * 24
    assert _hour(data, 9)["server_messages"] == 1
    assert _hour(data, 9)["days_with_mod"] == 0
    assert data["peak_coverage_pct"] == 0.0


def test_busy_quartile_ignores_dead_hours(conn):
    """A run of silent overnight hours must not drag the peak cut-off down."""
    mid = _local_midnight()
    now = mid + 23 * 3600
    plan = []
    mid_hours = [9, 10, 11, 12]
    for h in mid_hours:
        # Four live hours, one clearly busiest.
        plan += [(1, h, MEMBER)] * (40 if h == 12 else 5)
    _seed(conn, mid, plan)

    data = mcs.compute_mod_coverage(conn, GUILD, mod_ids=[MOD_A], gap_days=10, now=now)

    busy = [r["hour"] for r in data["hours"] if r["busy"]]
    assert busy == [12]
    assert data["busy_hours"] == 1


# ── Per-moderator breakdown ───────────────────────────────────────────
#
# Presence, not a leaderboard: each row is one moderator's own numbers, never
# compared against another's. The tests below check the arithmetic is scoped
# per person and per busy hour, and that nothing in the service itself orders
# the list by how active a moderator was — that would let the shape of the
# data recreate the ranking the panel copy promises not to show.


def test_mods_report_each_moderators_own_presence(conn):
    mid = _local_midnight()
    now = mid + 12 * 3600
    # Hour 9 is the server's one busy hour: five days of member traffic, and
    # MOD_A covers every one of them.
    plan = [(d, 9, MEMBER) for d in range(1, 6)]
    plan += [(d, 9, MOD_A) for d in range(1, 6)]
    # MOD_B shows up, but only once in the busy hour and once elsewhere —
    # present, but nowhere near covering it.
    plan += [(1, 9, MOD_B), (3, 20, MOD_B)]
    _seed(conn, mid, plan)

    data = mcs.compute_mod_coverage(
        conn, GUILD, mod_ids=[MOD_B, MOD_A], gap_days=10, now=now
    )
    mods = {m["user_id"]: m for m in data["mods"]}
    assert set(mods) == {str(MOD_A), str(MOD_B)}

    a = mods[str(MOD_A)]
    assert a["days_active"] == 5
    assert a["busy_hours_covered"] == data["busy_hours"] == 1
    assert a["peak_coverage_pct"] == 100.0

    b = mods[str(MOD_B)]
    assert b["days_active"] == 2  # one distinct day per message, not per row
    assert b["busy_hours_covered"] == 0  # 1 of 5 days is under the 50% bar
    assert b["peak_coverage_pct"] == 20.0


def test_mod_days_active_counts_days_not_messages(conn):
    """Forty messages in one day is one day of presence, not forty."""
    mid = _local_midnight()
    now = mid + 12 * 3600
    plan = [(2, 9, MEMBER)] + [(2, 9, MOD_A)] * 40
    _seed(conn, mid, plan)

    data = mcs.compute_mod_coverage(conn, GUILD, mod_ids=[MOD_A], gap_days=10, now=now)

    assert data["mods"][0]["days_active"] == 1


def test_mods_empty_when_no_mod_ids(conn):
    mid = _local_midnight()
    now = mid + 12 * 3600
    _seed(conn, mid, [(1, 9, MEMBER)])

    data = mcs.compute_mod_coverage(conn, GUILD, mod_ids=[], gap_days=10, now=now)

    assert data["mods"] == []


def test_mods_list_order_is_not_activity_ranked(conn):
    """The service orders by id, never by whoever covered more.

    Ordering by name for display is the dashboard route's job (see
    ``health.py``); a service that reordered toward the more active
    moderator would make that promise false one layer down.
    """
    mid = _local_midnight()
    now = mid + 12 * 3600
    # MOD_B (the higher id) covers every day; MOD_A (the lower id) barely
    # shows up. If the list were activity-ranked, MOD_B would sort first.
    plan = [(d, 9, MEMBER) for d in range(1, 6)]
    plan += [(d, 9, MOD_B) for d in range(1, 6)]
    plan += [(1, 9, MOD_A)]
    _seed(conn, mid, plan)

    data = mcs.compute_mod_coverage(
        conn, GUILD, mod_ids=[MOD_B, MOD_A], gap_days=10, now=now
    )

    assert [m["user_id"] for m in data["mods"]] == [str(MOD_A), str(MOD_B)]


def test_mod_rows_scores_only_busy_hours():
    """A quiet hour a moderator missed must not drag down their busy-hour score.

    Hour 1 here is a full gap (0% covered) but not busy; if it leaked into the
    average, a moderator who owns the one busy hour outright would read as
    less than fully covering it.
    """
    rows = [
        {
            "hour": 0, "label": "", "server_messages": 10, "days_observed": 4,
            "days_with_mod": 4, "coverage_pct": 100.0, "busy": True, "gap": False,
        },
        {
            "hour": 1, "label": "", "server_messages": 10, "days_observed": 4,
            "days_with_mod": 0, "coverage_pct": 0.0, "busy": False, "gap": True,
        },
    ]
    got = mcs._mod_rows(rows, [MOD_A], {MOD_A: {0: 4, 1: 0}}, {MOD_A: 4})

    assert got == [
        {
            "user_id": str(MOD_A),
            "days_active": 4,
            "busy_hours_covered": 1,
            "peak_coverage_pct": 100.0,
        }
    ]


# ── The new include_user_ids filter on the shared overlay query ──────


def test_include_user_ids_narrows_to_the_group(conn):
    h = _lived_hour()
    _seed(conn, _local_midnight(), [(0, h, MEMBER), (0, h, MOD_A), (0, h, MOD_B)])

    everyone = query_activity_overlay(conn, GUILD, "day", same_weekday=True)
    mods = query_activity_overlay(
        conn, GUILD, "day", same_weekday=True, include_user_ids={MOD_A, MOD_B}
    )

    assert everyone.current[h] == 3
    assert mods.current[h] == 2


def test_empty_include_set_applies_no_filter(conn):
    """Documented behaviour: falsy means unfiltered, which is why the service
    refuses to call with an empty group rather than expecting zeros."""
    h = _lived_hour()
    _seed(conn, _local_midnight(), [(0, h, MEMBER), (0, h, MOD_A)])

    got = query_activity_overlay(
        conn, GUILD, "day", same_weekday=True, include_user_ids=set()
    )

    assert got.current[h] == 2
