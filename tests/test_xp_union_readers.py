"""Readers that union xp_daily with raw xp_events (retention Stage 2a).

Two properties matter, and every test here is one of them:

1. **No-op today.** Nothing has been pruned, so a unioning reader must return
   exactly what the raw-only reader returned. If this breaks, shipping Stage 2
   changes live leaderboards for no reason.
2. **Survives the prune.** Delete the raw rows the rollup covers — what Stage 3
   will do — and the answers must not move. If this breaks, Stage 3 silently
   redefines "all-time".

The second is the one that would otherwise fail in production six months from
now, with no error and no way to notice.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.core.xp_system import (
    get_user_xp_by_source,
    get_user_xp_standing,
    get_xp_distribution_stats,
    get_xp_leaderboard,
    has_any_xp_events,
)
from bot_modules.services import xp_rollup_service as rollup
from bot_modules.services.inactive_report_service import channel_activity_map

GUILD = 7001
USER_A = 11
USER_B = 22
USER_C = 33
CHAN = 555

# "Now" for every scenario, so day arithmetic is stable.
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc).timestamp()


def _days_ago(n: int, hour: int = 12) -> float:
    base = datetime.fromtimestamp(NOW, timezone.utc).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    return (base - timedelta(days=n)).timestamp()


def _event(conn, *, days_ago, user=USER_A, source="text", amount=1.0, channel=CHAN):
    conn.execute(
        "INSERT INTO xp_events (guild_id, user_id, source, amount, created_at, channel_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (GUILD, user, source, amount, _days_ago(days_ago), channel),
    )


def _seed(conn):
    """Activity straddling the 90-day retention boundary."""
    # Old — will be pruned by Stage 3, must survive via the rollup.
    _event(conn, days_ago=170, user=USER_A, amount=100.0)
    _event(conn, days_ago=150, user=USER_B, amount=60.0)
    _event(conn, days_ago=150, user=USER_B, amount=5.0, source="voice")
    _event(conn, days_ago=120, user=USER_A, amount=10.0, channel=None)
    _event(conn, days_ago=95, user=USER_C, amount=7.0)
    # Recent — stays raw.
    _event(conn, days_ago=30, user=USER_A, amount=3.0)
    _event(conn, days_ago=2, user=USER_B, amount=1.0)


def _roll(conn):
    rollup.rollup_pending_days(conn, now=NOW)
    rollup.recompute_watermark(conn)


def _prune(conn):
    """What Stage 3 will do: drop raw rows the rollup already covers."""
    boundary = rollup.read_boundary(conn, now=NOW)
    assert boundary is not None, "rollup should be readable in these scenarios"
    conn.execute("DELETE FROM xp_events WHERE created_at < ?", (boundary[1],))


def _snapshot(conn):
    """Every unioning reader's output, as one comparable structure."""
    return {
        "leaderboard_text": [
            (e.user_id, e.xp) for e in get_xp_leaderboard(conn, GUILD, "text", limit=10)
        ],
        "leaderboard_voice": [
            (e.user_id, e.xp) for e in get_xp_leaderboard(conn, GUILD, "voice", limit=10)
        ],
        "distribution": get_xp_distribution_stats(conn, GUILD, "text"),
        "standing_a": get_user_xp_standing(conn, GUILD, "text", USER_A),
        "standing_c": get_user_xp_standing(conn, GUILD, "text", USER_C),
        "by_source_a": get_user_xp_by_source(conn, GUILD, USER_A),
        "by_source_b": get_user_xp_by_source(conn, GUILD, USER_B),
        "has_events": has_any_xp_events(conn, GUILD),
        "last_active": {
            uid: a.created_at
            for uid, a in channel_activity_map(
                conn, GUILD, [USER_A, USER_B, USER_C], CHAN
            ).items()
        },
    }


def test_union_is_a_no_op_before_anything_is_pruned(sync_db_path):
    """Rolling up must not change a single answer while raw still has it all."""
    with open_db(sync_db_path) as conn:
        _seed(conn)
        before = _snapshot(conn)
        _roll(conn)
        after = _snapshot(conn)

    assert after == before


def test_answers_survive_the_prune(sync_db_path):
    with open_db(sync_db_path) as conn:
        _seed(conn)
        _roll(conn)
        before = _snapshot(conn)
        _prune(conn)
        after = _snapshot(conn)

    assert after == before


def test_all_time_leaderboard_keeps_pruned_xp(sync_db_path):
    """The concrete failure the plan exists to prevent."""
    with open_db(sync_db_path) as conn:
        _seed(conn)
        _roll(conn)
        _prune(conn)
        entries = {e.user_id: e.xp for e in get_xp_leaderboard(conn, GUILD, "text", limit=10)}

    # USER_A: 100 (170d) + 10 (120d) + 3 (30d) — only the 3 is still raw.
    assert entries[USER_A] == 113.0
    # USER_B: 60 (150d, pruned) + 1 (2d, raw) — both arms in one total.
    assert entries[USER_B] == 61.0
    # USER_C: 7 (95d) — entirely from the rollup.
    assert entries[USER_C] == 7.0


def test_inactive_report_still_sees_a_long_absent_member(sync_db_path):
    """USER_C's only activity is 95 days old — past the boundary. Losing it
    would report the one member the report exists to surface as 'never here'."""
    with open_db(sync_db_path) as conn:
        _seed(conn)
        _roll(conn)
        _prune(conn)
        act = channel_activity_map(conn, GUILD, [USER_C], CHAN)

    assert USER_C in act
    assert act[USER_C].created_at == pytest.approx(_days_ago(95))


def test_has_any_xp_events_true_when_only_the_rollup_remains(sync_db_path):
    """A guild whose every event predates the boundary still has a history."""
    with open_db(sync_db_path) as conn:
        _event(conn, days_ago=200, user=USER_A, amount=5.0)
        _roll(conn)
        _prune(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM xp_events WHERE guild_id = ?", (GUILD,)
        ).fetchone()[0] == 0

        assert has_any_xp_events(conn, GUILD) is True


def test_windowed_reads_never_touch_the_rollup(sync_db_path):
    """A 30-day window is entirely inside the boundary, so it must be served
    from raw and stay exact — no day-granularity skew for recent windows."""
    with open_db(sync_db_path) as conn:
        _seed(conn)
        _roll(conn)
        # A cutoff mid-day, deliberately not on a day boundary.
        cutoff = _days_ago(31, hour=7)
        entries = {
            e.user_id: e.xp
            for e in get_xp_leaderboard(conn, GUILD, "text", since_ts=cutoff, limit=10)
        }

    assert entries == {USER_A: 3.0, USER_B: 1.0}


def test_reader_falls_back_to_raw_while_the_backfill_is_incomplete(sync_db_path):
    """Before the rollup has covered everything, the boundary is None and the
    readers stay on raw — which is still complete, since nothing is pruned."""
    with open_db(sync_db_path) as conn:
        _seed(conn)
        assert rollup.read_boundary(conn, now=NOW) is None
        entries = {e.user_id: e.xp for e in get_xp_leaderboard(conn, GUILD, "text", limit=10)}

    assert entries[USER_A] == 113.0


def test_a_hole_in_the_rollup_holds_the_watermark_back(sync_db_path):
    """A missing day must not be read past. The watermark is a contiguous
    prefix, not the newest day present."""
    with open_db(sync_db_path) as conn:
        _seed(conn)
        _roll(conn)
        # Blow a hole in the middle of the rolled range.
        gap_day = rollup.utc_day(_days_ago(150))
        conn.execute("DELETE FROM xp_daily WHERE day = ?", (gap_day,))
        watermark = rollup.recompute_watermark(conn)

    assert watermark is not None
    assert watermark < gap_day


def test_by_source_totals_are_rounded_so_the_two_paths_agree(sync_db_path):
    """Summing per-day buckets associates the additions differently than
    summing every event, so the paths can differ in a float's last bits —
    seen on prod as 13589.63 vs 13589.630000000001. Both round to 2dp, the
    precision XP is displayed at, which makes them exactly equal."""
    with open_db(sync_db_path) as conn:
        for i in range(40):
            _event(conn, days_ago=150 + (i % 20), user=USER_A, amount=0.07)
        for i in range(15):
            _event(conn, days_ago=10 + (i % 5), user=USER_A, amount=0.03)
        before = get_user_xp_by_source(conn, GUILD, USER_A)
        _roll(conn)
        after = get_user_xp_by_source(conn, GUILD, USER_A)
        _prune(conn)
        pruned = get_user_xp_by_source(conn, GUILD, USER_A)

    assert before == after == pruned
    assert after["text"] == round(40 * 0.07 + 15 * 0.03, 2)


def test_null_channel_events_are_not_lost_by_the_union(sync_db_path):
    """USER_A's 120-day-old event has no channel; it must still count toward
    the all-time total even though it cannot appear in a channel report."""
    with open_db(sync_db_path) as conn:
        _seed(conn)
        _roll(conn)
        _prune(conn)
        by_source = get_user_xp_by_source(conn, GUILD, USER_A)

    assert by_source["text"] == 113.0
