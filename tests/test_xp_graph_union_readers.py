"""Bucketed XP readers over the rollup (retention Stage 2b).

Stage 2a converted the readers that ask for a *sum* or a *max*. These are the
ones that ask for a shape: XP per rolling time bucket, XP per hour of day, and
the running total behind "how long did it take to reach level 5". They are the
readers the plan warned would be hardest, and they are the ones where the
fidelity cost of a *daily* rollup actually lands.

The properties under test:

1. **Exact where it can be.** When a rolled-up day's XP sits at that day's UTC
   midnight anyway, the union must equal raw bucket-for-bucket, before and
   after the prune.
2. **Conserved where it can't.** For events at arbitrary times of day, a
   rolled-up day is attributed whole to whichever bucket its midnight falls in
   — so an individual bucket may shift by one day's worth at an edge, but the
   *total* across the graph must not move. Losing XP entirely is the bug; the
   bounded shuffle is the accepted cost.
3. **Windowed, not silently truncated.** The hour-of-day / day-of-week XP
   histograms cannot be served from a daily rollup at all, so they are limited
   to the retention horizon and the report says so.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot_modules.core.db_utils import open_db
from bot_modules.core.xp_system import (
    get_oldest_xp_event_timestamp,
    get_time_to_level_details,
)
from bot_modules.services import xp_rollup_service as rollup
from bot_modules.services.activity_graphs import (
    XP_HISTOGRAM_WINDOW_DAYS,
    query_xp_activity,
    query_xp_activity_with_breakdown,
    query_xp_histogram,
    query_xp_histogram_with_breakdown,
)
from bot_modules.services.reports_data import get_activity_data

GUILD = 7101
USER_A = 11
USER_B = 22
CHAN = 555
OTHER_CHAN = 556

NOW = datetime.now(timezone.utc).timestamp()


def _days_ago(n: int, hour: int = 0) -> float:
    base = datetime.fromtimestamp(NOW, timezone.utc).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    return (base - timedelta(days=n)).timestamp()


def _event(conn, *, days_ago, hour=0, user=USER_A, source="text", amount=1.0, channel=CHAN):
    conn.execute(
        "INSERT INTO xp_events (guild_id, user_id, source, amount, created_at, channel_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (GUILD, user, source, amount, _days_ago(days_ago, hour), channel),
    )


def _roll(conn):
    rollup.rollup_pending_days(conn, now=NOW)
    rollup.recompute_watermark(conn)


def _prune(conn):
    boundary = rollup.read_boundary(conn)
    assert boundary is not None, "rollup should be readable in these scenarios"
    conn.execute("DELETE FROM xp_events WHERE created_at < ?", (boundary[1],))


def _seed_midnight(conn):
    """Activity straddling the boundary, every event at a UTC midnight.

    Midnight is what makes exact comparison possible: the rollup stamps a day's
    total at that day's 00:00, so for these events the synthetic timestamp and
    the real one coincide and every bucket must match to the XP.
    """
    _event(conn, days_ago=300, amount=100.0)
    _event(conn, days_ago=200, user=USER_B, amount=60.0)
    _event(conn, days_ago=200, user=USER_B, amount=5.0, source="voice")
    _event(conn, days_ago=150, amount=10.0, channel=None)
    _event(conn, days_ago=95, user=USER_B, amount=7.0, channel=OTHER_CHAN)
    _event(conn, days_ago=40, amount=3.0)
    _event(conn, days_ago=2, user=USER_B, amount=1.0)


# ── 1. exact where it can be ────────────────────────────────────────────


def test_month_graph_is_unchanged_by_rolling_up(sync_db_path):
    """Building the rollup must not move a single bucket while raw is intact."""
    with open_db(sync_db_path) as conn:
        _seed_midnight(conn)
        before = query_xp_activity(conn, GUILD, "month")
        _roll(conn)
        after = query_xp_activity(conn, GUILD, "month")

    assert after == before


def test_month_graph_survives_the_prune(sync_db_path):
    """The 360-day graph is why xp_events could not simply be swept."""
    with open_db(sync_db_path) as conn:
        _seed_midnight(conn)
        _roll(conn)
        before = query_xp_activity(conn, GUILD, "month")
        _prune(conn)
        after = query_xp_activity(conn, GUILD, "month")

    assert after == before
    # And it is not vacuously equal — the old XP is really in there.
    assert sum(before[1]) == 186.0


def test_breakdown_and_member_counts_survive_the_prune(sync_db_path):
    with open_db(sync_db_path) as conn:
        _seed_midnight(conn)
        _roll(conn)
        before = query_xp_activity_with_breakdown(conn, GUILD, "month")
        _prune(conn)
        after = query_xp_activity_with_breakdown(conn, GUILD, "month")

    assert after == before
    labels, totals, members, by_source = after
    assert set(by_source) == {"text", "voice"}
    assert sum(by_source["voice"]) == 5.0
    # A member active only in a pruned month still counts toward that bucket.
    assert max(members) >= 1


def test_channel_filter_survives_the_prune(sync_db_path):
    """channel_id is in the rollup key precisely so this keeps working."""
    with open_db(sync_db_path) as conn:
        _seed_midnight(conn)
        _roll(conn)
        before = query_xp_activity(conn, GUILD, "month", channel_id=OTHER_CHAN)
        _prune(conn)
        after = query_xp_activity(conn, GUILD, "month", channel_id=OTHER_CHAN)

    assert after == before
    assert sum(after[1]) == 7.0


def test_exclusions_survive_the_prune(sync_db_path):
    """The user/channel exclusion lists filter the rollup arm too."""
    with open_db(sync_db_path) as conn:
        _seed_midnight(conn)
        _roll(conn)
        before = query_xp_activity(conn, GUILD, "month", exclude_user_ids={USER_B})
        _prune(conn)
        after = query_xp_activity(conn, GUILD, "month", exclude_user_ids={USER_B})

    assert after == before
    assert sum(after[1]) == 113.0  # USER_A only: 100 + 10 + 3


def test_null_channel_rows_are_not_lost_by_a_channel_exclusion(sync_db_path):
    """27% of prod rows have no channel; excluding a channel must keep them."""
    with open_db(sync_db_path) as conn:
        _seed_midnight(conn)
        _roll(conn)
        _prune(conn)
        _, totals, _ = query_xp_activity(
            conn, GUILD, "month", exclude_channel_ids={CHAN}
        )

    # The NULL-channel event (10.0) and the OTHER_CHAN event (7.0) remain.
    assert sum(totals) == 17.0


def test_short_windows_never_touch_the_rollup(sync_db_path):
    """A 30-day view is served entirely from raw, so it is exact, not rounded."""
    with open_db(sync_db_path) as conn:
        _event(conn, days_ago=10, hour=13, amount=1.3)
        _event(conn, days_ago=3, hour=7, amount=2.5)
        before = query_xp_activity(conn, GUILD, "day")
        _roll(conn)
        after = query_xp_activity(conn, GUILD, "day")

    assert after == before
    assert sum(after[1]) == 3.8  # rounded to 1dp per bucket, as ever


# ── 2. conserved where it can't ─────────────────────────────────────────


def test_total_xp_is_conserved_for_events_at_arbitrary_times(sync_db_path):
    """The accepted skew is a shuffle between adjacent buckets, never a loss.

    These events sit at 13:00 and 21:00, so a rolled-up day's midnight can land
    on the far side of a rolling bucket edge from the events themselves. An
    individual bucket may therefore move by up to one day's XP — but nothing
    may vanish, and nothing may be counted twice.
    """
    with open_db(sync_db_path) as conn:
        for n in range(95, 360, 7):
            _event(conn, days_ago=n, hour=13, amount=1.0)
            _event(conn, days_ago=n, hour=21, user=USER_B, amount=0.5)
        _roll(conn)
        before = sum(query_xp_activity(conn, GUILD, "month")[1])
        _prune(conn)
        after = sum(query_xp_activity(conn, GUILD, "month")[1])

    assert after == before


# ── 3. the histograms, which cannot union at all ────────────────────────


def test_xp_histogram_is_limited_to_the_retention_window(sync_db_path):
    """A daily rollup has no hour, so the hour-of-day graph stops at the horizon."""
    with open_db(sync_db_path) as conn:
        _event(conn, days_ago=XP_HISTOGRAM_WINDOW_DAYS + 10, hour=3, amount=50.0)
        _event(conn, days_ago=5, hour=3, amount=2.0)
        _, totals = query_xp_histogram(conn, GUILD, "hour_of_day")

    assert totals[3] == 2.0
    assert sum(totals) == 2.0


def test_xp_histogram_breakdown_is_limited_to_the_retention_window(sync_db_path):
    with open_db(sync_db_path) as conn:
        _event(conn, days_ago=XP_HISTOGRAM_WINDOW_DAYS + 10, hour=3, amount=50.0)
        _event(conn, days_ago=5, hour=3, amount=2.0, source="voice")
        _, totals, by_source = query_xp_histogram_with_breakdown(
            conn, GUILD, "hour_of_day"
        )

    assert set(by_source) == {"voice"}
    assert sum(totals) == 2.0


def test_the_report_says_the_xp_histogram_is_windowed(sync_db_path):
    """A narrowed window that the chart title doesn't mention is the real bug."""
    with open_db(sync_db_path) as conn:
        _event(conn, days_ago=5, hour=3, amount=2.0)
        xp = get_activity_data(conn, GUILD, "hour_of_day", 0.0, mode="xp")
        messages = get_activity_data(
            conn, GUILD, "hour_of_day", 0.0, mode="messages"
        )

    assert xp["window_label"] == f"By Hour of Day (last {XP_HISTOGRAM_WINDOW_DAYS} days)"
    # The message histogram reads its own archive and is untouched.
    assert messages["window_label"] == "By Hour of Day"


# ── 4. time to level, the one reader that needs ordering ────────────────


def test_time_to_level_survives_the_prune(sync_db_path):
    """The All Time card must not forget members who levelled long ago."""
    with open_db(sync_db_path) as conn:
        # 300 XP earned across three old days — level 5 needs 250 by default.
        _event(conn, days_ago=300, amount=100.0)
        _event(conn, days_ago=250, amount=100.0)
        _event(conn, days_ago=200, amount=100.0)
        _roll(conn)
        before = get_time_to_level_details(conn, GUILD, 5)
        _prune(conn)
        after = get_time_to_level_details(conn, GUILD, 5)

    assert len(before) == 1
    assert [(r["user_id"], r["seconds"]) for r in after] == [
        (r["user_id"], r["seconds"]) for r in before
    ]


def test_time_to_level_keeps_the_real_first_event_time(sync_db_path):
    """first_at comes from xp_daily.first_at, not the bucket's midnight.

    Taking it from the synthetic timestamp would inflate every pruned member's
    "time to level" by up to a day at the near end and shrink it at the far.
    """
    first = _days_ago(300, hour=18)
    with open_db(sync_db_path) as conn:
        _event(conn, days_ago=300, hour=18, amount=100.0)
        _event(conn, days_ago=250, hour=6, amount=200.0)
        _roll(conn)
        _prune(conn)
        rows = get_time_to_level_details(conn, GUILD, 5)

    assert len(rows) == 1
    assert rows[0]["first_at"] == first


def test_a_recent_crossing_stays_exact_to_the_second(sync_db_path):
    """Inside the boundary nothing is rolled up, so nothing is rounded."""
    with open_db(sync_db_path) as conn:
        _event(conn, days_ago=40, hour=9, amount=200.0)
        _event(conn, days_ago=10, hour=17, amount=100.0)
        _roll(conn)
        rows = get_time_to_level_details(conn, GUILD, 5)

    assert len(rows) == 1
    assert rows[0]["first_at"] == _days_ago(40, hour=9)
    assert rows[0]["reached_at"] == _days_ago(10, hour=17)


def test_oldest_event_timestamp_survives_the_prune(sync_db_path):
    """MIN(created_at) over the whole history, still true after pruning."""
    oldest = _days_ago(300, hour=18)
    with open_db(sync_db_path) as conn:
        _event(conn, days_ago=300, hour=18, amount=1.0)
        _event(conn, days_ago=2, hour=4, amount=1.0)
        _roll(conn)
        assert get_oldest_xp_event_timestamp(conn, GUILD) == oldest
        _prune(conn)
        assert get_oldest_xp_event_timestamp(conn, GUILD) == oldest
