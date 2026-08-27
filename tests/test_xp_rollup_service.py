"""xp_events → xp_daily rollup (Stage 1 of the retention plan).

The rollup's whole job is to be a faithful summary of raw events, so most of
these tests are equality assertions against the raw table rather than against
hand-written expectations.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services import xp_rollup_service as rollup

GUILD = 4001
USER_A = 111
USER_B = 222
CHAN = 900


def _ts(day: str, hour: int = 12) -> float:
    """Unix timestamp at a given hour on a UTC day."""
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (d + timedelta(hours=hour)).timestamp()


def _event(conn, *, day, hour=12, user=USER_A, source="text", amount=1.0, channel=CHAN):
    conn.execute(
        "INSERT INTO xp_events (guild_id, user_id, source, amount, created_at, channel_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (GUILD, user, source, amount, _ts(day, hour), channel),
    )


def _buckets(conn, day=None):
    sql = "SELECT * FROM xp_daily"
    params: list[object] = []
    if day:
        sql += " WHERE day = ?"
        params.append(day)
    return conn.execute(sql, params).fetchall()


def test_rollup_day_sums_one_bucket_per_user_source_channel(sync_db_path):
    with open_db(sync_db_path) as conn:
        _event(conn, day="2026-03-01", hour=1, amount=2.0)
        _event(conn, day="2026-03-01", hour=5, amount=3.0)
        _event(conn, day="2026-03-01", hour=7, amount=4.0, source="voice")
        _event(conn, day="2026-03-01", hour=9, amount=5.0, user=USER_B)
        rollup.rollup_day(conn, "2026-03-01")

        rows = {(r["user_id"], r["source"]): r for r in _buckets(conn)}

    assert set(rows) == {(USER_A, "text"), (USER_A, "voice"), (USER_B, "text")}
    text = rows[(USER_A, "text")]
    assert text["xp"] == 5.0
    assert text["events"] == 2
    assert text["first_at"] == _ts("2026-03-01", 1)
    assert text["last_at"] == _ts("2026-03-01", 5)


def test_rollup_day_ignores_other_days(sync_db_path):
    with open_db(sync_db_path) as conn:
        _event(conn, day="2026-03-01", amount=2.0)
        _event(conn, day="2026-03-02", amount=9.0)
        rollup.rollup_day(conn, "2026-03-01")
        rows = _buckets(conn)

    assert len(rows) == 1
    assert rows[0]["day"] == "2026-03-01"
    assert rows[0]["xp"] == 2.0


def test_rollup_is_idempotent(sync_db_path):
    """A re-run must replace, not accumulate — this is the whole design."""
    with open_db(sync_db_path) as conn:
        _event(conn, day="2026-03-01", amount=2.0)
        rollup.rollup_day(conn, "2026-03-01")
        first = [dict(r) for r in _buckets(conn)]
        rollup.rollup_day(conn, "2026-03-01")
        second = [dict(r) for r in _buckets(conn)]

    assert first == second


def test_null_channel_rows_fold_into_one_bucket(sync_db_path):
    """27% of prod rows have no channel; the PK cannot dedupe them (SQLite
    treats NULLs as distinct), so the delete-then-insert rebuild must."""
    with open_db(sync_db_path) as conn:
        _event(conn, day="2026-03-01", hour=1, amount=2.0, channel=None)
        _event(conn, day="2026-03-01", hour=2, amount=3.0, channel=None)
        rollup.rollup_day(conn, "2026-03-01")
        rollup.rollup_day(conn, "2026-03-01")  # twice: the dedupe hazard
        rows = _buckets(conn)

    assert len(rows) == 1
    assert rows[0]["channel_id"] is None
    assert rows[0]["xp"] == 5.0
    assert rows[0]["events"] == 2


def test_rollup_shrinks_when_events_were_erased(sync_db_path):
    """A purge run deletes raw events; re-rolling that day must not leave the
    erased member's total behind in the aggregate."""
    with open_db(sync_db_path) as conn:
        _event(conn, day="2026-03-01", amount=2.0, user=USER_A)
        _event(conn, day="2026-03-01", amount=5.0, user=USER_B)
        rollup.rollup_day(conn, "2026-03-01")
        assert len(_buckets(conn)) == 2

        conn.execute("DELETE FROM xp_events WHERE user_id = ?", (USER_B,))
        rollup.rollup_day(conn, "2026-03-01")
        rows = _buckets(conn)

    assert [r["user_id"] for r in rows] == [USER_A]


def test_rollup_totals_match_raw_events(sync_db_path):
    """The summary is only useful if it is exact. Sum both sides."""
    with open_db(sync_db_path) as conn:
        for i, day in enumerate(["2026-03-01", "2026-03-02", "2026-03-03"]):
            for hour in (0, 6, 23):
                _event(conn, day=day, hour=hour, amount=1.5 + i, user=USER_A)
                _event(conn, day=day, hour=hour, amount=0.5, user=USER_B, source="voice")
                _event(conn, day=day, hour=hour, amount=2.0, channel=None)
            rollup.rollup_day(conn, day)

        raw = conn.execute(
            "SELECT COUNT(*), ROUND(SUM(amount), 4) FROM xp_events"
        ).fetchone()
        agg = conn.execute(
            "SELECT SUM(events), ROUND(SUM(xp), 4) FROM xp_daily"
        ).fetchone()

    assert (agg[0], agg[1]) == (raw[0], raw[1])


def test_days_with_events_excludes_the_current_day(sync_db_path):
    with open_db(sync_db_path) as conn:
        _event(conn, day="2026-03-01")
        _event(conn, day="2026-03-02")
        days = rollup.days_with_events(conn, before="2026-03-02")

    assert days == ["2026-03-01"]


def test_rollup_pending_days_skips_today(sync_db_path):
    """Today's bucket is still growing; rolling it up would freeze a partial
    total that a Stage-2 reader would then trust."""
    now = _ts("2026-03-03", 10)
    with open_db(sync_db_path) as conn:
        _event(conn, day="2026-03-01")
        _event(conn, day="2026-03-02")
        _event(conn, day="2026-03-03")
        days, buckets = rollup.rollup_pending_days(conn, now=now)
        rolled = rollup.rolled_up_days(conn)

    assert days == 2
    assert buckets == 2
    assert rolled == {"2026-03-01", "2026-03-02"}


def test_rollup_pending_days_is_a_no_op_when_current(sync_db_path):
    now = _ts("2026-03-03", 10)
    with open_db(sync_db_path) as conn:
        _event(conn, day="2026-03-01")
        rollup.rollup_pending_days(conn, now=now)
        days, buckets = rollup.rollup_pending_days(conn, now=now)

    assert (days, buckets) == (0, 0)


def test_rollup_pending_days_honours_the_chunk_limit(sync_db_path):
    now = _ts("2026-03-10", 10)
    with open_db(sync_db_path) as conn:
        for d in range(1, 6):
            _event(conn, day=f"2026-03-0{d}")
        first, _ = rollup.rollup_pending_days(conn, now=now, limit=2)
        second, _ = rollup.rollup_pending_days(conn, now=now, limit=2)
        third, _ = rollup.rollup_pending_days(conn, now=now, limit=2)
        remaining, _ = rollup.rollup_pending_days(conn, now=now, limit=2)

    assert [first, second, third, remaining] == [2, 2, 1, 0]


def test_refresh_recent_days_picks_up_late_events(sync_db_path):
    """Voice XP is credited when the session ends, so a day can gain events
    after it was first rolled up."""
    now = _ts("2026-03-03", 10)
    with open_db(sync_db_path) as conn:
        _event(conn, day="2026-03-02", hour=1, amount=1.0)
        rollup.rollup_pending_days(conn, now=now)
        assert _buckets(conn, "2026-03-02")[0]["xp"] == 1.0

        _event(conn, day="2026-03-02", hour=23, amount=4.0, source="voice")
        rollup.refresh_recent_days(conn, days=2, now=now)
        total = conn.execute(
            "SELECT SUM(xp) FROM xp_daily WHERE day = '2026-03-02'"
        ).fetchone()[0]

    assert total == 5.0


@pytest.mark.parametrize("day,hour", [("2026-03-01", 0), ("2026-03-01", 23)])
def test_day_boundaries_are_utc_inclusive_exclusive(sync_db_path, day, hour):
    """Midnight belongs to its own day; 23:59 does not leak into the next."""
    with open_db(sync_db_path) as conn:
        _event(conn, day=day, hour=hour, amount=3.0)
        rollup.rollup_day(conn, day)
        rows = _buckets(conn)

    assert len(rows) == 1
    assert rows[0]["day"] == day
    assert rows[0]["xp"] == 3.0


def test_rollup_stats_reports_missing_days(sync_db_path):
    now = _ts("2026-03-05", 10)
    with open_db(sync_db_path) as conn:
        _event(conn, day="2026-03-01")
        _event(conn, day="2026-03-02")
        rollup.rollup_day(conn, "2026-03-01")
        stats = rollup.rollup_stats(conn)

    assert stats["buckets"] == 1
    assert stats["raw_events"] == 2
    assert stats["events_covered"] == 1
    assert stats["days_missing"] == ["2026-03-02"]
    assert now  # the fixture timestamp is only here to date the scenario


def test_rollup_stats_does_not_call_today_a_missing_day(sync_db_path):
    """Today is never rolled on purpose, so it must not read as a gap.

    Otherwise every backfill and every daily pass reports one missing day
    forever, and the list stops meaning anything.
    """
    today = rollup.current_utc_day()
    with open_db(sync_db_path) as conn:
        _event(conn, day=today)
        stats = rollup.rollup_stats(conn)

    assert stats["days_missing"] == []
    assert stats["raw_events"] == 1


def test_xp_daily_is_purged_with_the_member(sync_db_path):
    """A new per-user table joins purge_user_data — the exact decision the
    2026-08 GDPR register exists to force."""
    from bot_modules.services.privacy_service import purge_user_data

    with open_db(sync_db_path) as conn:
        _event(conn, day="2026-03-01", user=USER_A)
        _event(conn, day="2026-03-01", user=USER_B)
        rollup.rollup_day(conn, "2026-03-01")

        purge_user_data(conn, guild_id=GUILD, user_id=USER_A)

        left = [r["user_id"] for r in _buckets(conn)]

    assert left == [USER_B]
