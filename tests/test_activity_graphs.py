"""Tests for bot_modules.services.activity_graphs.

The module is large (~1170 statements) and at ~21% coverage before this
file.  Strategy mirrors test_interaction_graph.py: cover pure helpers,
DB query helpers, and smoke-test PNG renderers.  Renderers are smoke-
tested for matplotlib PNG magic; pixel content is not validated.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.activity_graphs import (
    CadenceBucket,
    DropoffProfile,
    _append_exclusions,
    _BUCKET_BUILDERS,
    _day_buckets,
    _DOW_LABELS,
    _hour_buckets,
    _HOD_LABELS,
    _month_buckets,
    _strftime_expr,
    _week_buckets,
    _WINDOW_LABELS,
    query_burst_ranking,
    query_dropoff_profiles,
    query_greeter_response_times,
    query_message_activity,
    query_message_cadence,
    query_message_histogram,
    query_message_rate_10min,
    query_message_rate_drops,
    query_nsfw_gender_activity,
    query_role_growth,
    query_session_burst,
    query_xp_activity,
    query_xp_activity_with_breakdown,
    query_xp_histogram,
    query_xp_histogram_with_breakdown,
    render_activity_chart,
    render_burst_ranking_chart,
    render_greeter_response_chart,
    render_join_histogram,
    render_level_histogram,
    render_message_cadence_chart,
    render_message_rate_chart,
    render_nsfw_gender_chart,
    render_nsfw_gender_line_chart,
    render_role_growth_chart,
    render_session_burst_chart,
)
from migrations import apply_migrations_sync


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# ── Shared fixtures ──────────────────────────────────────────────────


@pytest.fixture
def db_conn(tmp_path):
    """A migrated SQLite connection ready for activity-graph tests."""
    path = tmp_path / "ag.db"
    apply_migrations_sync(path)
    with open_db(path) as conn:
        yield conn


# ── Bucket builders ──────────────────────────────────────────────────


def test_hour_buckets_returns_24_entries():
    now = datetime(2026, 5, 31, 12, 30, tzinfo=timezone.utc)
    buckets, start_ts = _hour_buckets(now)
    assert len(buckets) == 24
    assert isinstance(start_ts, float)
    # The last bucket label should be the current hour
    assert buckets[-1][1].startswith("Sun")  # 2026-05-31 was Sunday


def test_hour_buckets_keys_are_utc_strftime():
    """Keys should match SQLite strftime('%Y-%m-%d %H', ...) format in UTC."""
    now = datetime(2026, 5, 31, 12, 30, tzinfo=timezone.utc)
    buckets, _ = _hour_buckets(now, utc_offset_hours=0)
    # First key: 23 hours before noon on the 31st = 13:00 on the 30th
    assert buckets[0][0] == "2026-05-30 13"
    assert buckets[-1][0] == "2026-05-31 12"


def test_hour_buckets_respects_utc_offset():
    """Local labels should shift with utc_offset_hours, keys stay UTC."""
    now = datetime(2026, 5, 31, 0, 0, tzinfo=timezone.utc)
    buckets, _ = _hour_buckets(now, utc_offset_hours=5)
    # Last label is in the user's local time (+5 from UTC midnight = 05:00)
    assert buckets[-1][1].endswith("05:00")
    # Last key is the UTC hour, so 00
    assert buckets[-1][0].endswith(" 00")


def test_day_buckets_returns_30_entries():
    now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
    buckets, start_ts = _day_buckets(now)
    assert len(buckets) == 30
    # 30 days back from now
    span = now.timestamp() - start_ts
    assert 29.5 * 86400 < span < 30.5 * 86400


def test_week_buckets_returns_12_entries():
    now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
    buckets, _ = _week_buckets(now)
    assert len(buckets) == 12
    # Each key is an integer epoch (string repr)
    for key, _label in buckets:
        assert key.isdigit()


def test_month_buckets_returns_12_entries():
    now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
    buckets, _ = _month_buckets(now)
    assert len(buckets) == 12
    for key, _label in buckets:
        assert key.isdigit()


def test_bucket_builders_dict_covers_four_resolutions():
    assert set(_BUCKET_BUILDERS) == {"hour", "day", "week", "month"}


def test_window_labels_cover_all_resolutions():
    for r in ("hour", "day", "week", "month", "hour_of_day", "day_of_week"):
        assert r in _WINDOW_LABELS


# ── _strftime_expr ───────────────────────────────────────────────────


def test_strftime_expr_hour_uses_calendar_buckets():
    expr = _strftime_expr("hour")
    assert "strftime" in expr
    assert "%Y-%m-%d %H" in expr


def test_strftime_expr_hour_applies_offset_secs():
    expr = _strftime_expr("hour", utc_offset_secs=3600)
    assert "+ 3600" in expr


def test_strftime_expr_day_uses_rolling_window():
    expr = _strftime_expr("day", since_ts=1000.0)
    assert "86400" in expr
    assert "1000.0" in expr


def test_strftime_expr_week_uses_604800_seconds():
    expr = _strftime_expr("week", since_ts=2000.0)
    assert "604800" in expr


def test_strftime_expr_month_uses_2592000_seconds():
    expr = _strftime_expr("month", since_ts=3000.0)
    assert "2592000" in expr


# ── _append_exclusions ───────────────────────────────────────────────


def test_append_exclusions_no_exclusions_unchanged():
    params: list[object] = [1]
    where = _append_exclusions("guild_id = ?", params, None, None)
    assert where == "guild_id = ?"
    assert params == [1]


def test_append_exclusions_user_ids_adds_clause_and_params():
    params: list[object] = [1]
    where = _append_exclusions("guild_id = ?", params, {7, 8}, None)
    assert "user_id NOT IN" in where
    # The set order is non-deterministic but both ids must be appended
    assert set(params) == {1, 7, 8}


def test_append_exclusions_channel_ids_adds_clause_and_params():
    params: list[object] = [1]
    where = _append_exclusions("guild_id = ?", params, None, {42})
    assert "channel_id IS NULL OR channel_id NOT IN" in where
    assert 42 in params


def test_append_exclusions_both_sets_apply():
    params: list[object] = []
    where = _append_exclusions("1=1", params, {1}, {2})
    assert "user_id NOT IN" in where
    assert "channel_id NOT IN" in where
    assert set(params) == {1, 2}


# ── DB seeding helpers ───────────────────────────────────────────────


def _seed_messages(conn, guild_id=10, rows=None):
    """rows: iterable of (message_id, channel_id, author_id, ts, reply_to, content)."""
    for mid, cid, aid, ts, rep, content in rows or []:
        conn.execute(
            "INSERT OR REPLACE INTO messages "
            "(message_id, guild_id, channel_id, author_id, content, reply_to_id, ts)"
            " VALUES (?,?,?,?,?,?,?)",
            (mid, guild_id, cid, aid, content, rep, ts),
        )


def _seed_processed(conn, guild_id=10, rows=None):
    """rows: iterable of (message_id, channel_id, user_id, created_at)."""
    for mid, cid, uid, ts in rows or []:
        conn.execute(
            "INSERT OR REPLACE INTO processed_messages "
            "(guild_id, message_id, channel_id, user_id, created_at, processed_at)"
            " VALUES (?,?,?,?,?,?)",
            (guild_id, mid, cid, uid, ts, ts),
        )


def _seed_xp(conn, guild_id=10, rows=None):
    """rows: iterable of (user_id, source, amount, created_at)."""
    for uid, src, amt, ts in rows or []:
        conn.execute(
            "INSERT INTO xp_events (guild_id, user_id, source, amount, created_at)"
            " VALUES (?,?,?,?,?)",
            (guild_id, uid, src, amt, ts),
        )


# ── query_message_activity ───────────────────────────────────────────


def test_query_message_activity_empty_returns_zero_padded_lists(db_conn):
    labels, msg_counts, member_counts = query_message_activity(
        db_conn, guild_id=10, resolution="day"
    )
    assert len(labels) == 30
    assert msg_counts == [0] * 30
    assert member_counts == [0] * 30


def test_query_message_activity_counts_messages_in_window(db_conn):
    """Recent processed messages should be counted in the last day bucket."""
    now_ts = datetime.now(timezone.utc).timestamp() - 60  # 1 min ago
    _seed_processed(
        db_conn,
        rows=[
            (1, 100, 7, now_ts),
            (2, 100, 8, now_ts - 30),
            (3, 100, 7, now_ts - 45),  # same author → 1 unique member for that row
        ],
    )
    db_conn.commit()
    labels, msgs, members = query_message_activity(
        db_conn, guild_id=10, resolution="day"
    )
    assert len(labels) == 30
    assert sum(msgs) == 3
    assert max(members) >= 2  # at least the most-recent bucket sees both users


def test_query_message_activity_filters_by_user(db_conn):
    now_ts = datetime.now(timezone.utc).timestamp() - 60
    _seed_processed(db_conn, rows=[(1, 1, 7, now_ts), (2, 1, 8, now_ts)])
    db_conn.commit()
    _, msgs, _ = query_message_activity(
        db_conn, guild_id=10, resolution="day", user_id=7
    )
    assert sum(msgs) == 1


def test_query_message_activity_filters_by_channel(db_conn):
    now_ts = datetime.now(timezone.utc).timestamp() - 60
    _seed_processed(
        db_conn,
        rows=[(1, 100, 7, now_ts), (2, 200, 7, now_ts)],
    )
    db_conn.commit()
    _, msgs, _ = query_message_activity(
        db_conn, guild_id=10, resolution="day", channel_id=100
    )
    assert sum(msgs) == 1


def test_query_message_activity_honors_exclusions(db_conn):
    now_ts = datetime.now(timezone.utc).timestamp() - 60
    _seed_processed(
        db_conn,
        rows=[(1, 100, 7, now_ts), (2, 200, 8, now_ts)],
    )
    db_conn.commit()
    _, msgs_excl_user, _ = query_message_activity(
        db_conn, guild_id=10, resolution="day", exclude_user_ids={7}
    )
    _, msgs_excl_ch, _ = query_message_activity(
        db_conn, guild_id=10, resolution="day", exclude_channel_ids={200}
    )
    assert sum(msgs_excl_user) == 1
    assert sum(msgs_excl_ch) == 1


def test_query_message_activity_hour_resolution_returns_24(db_conn):
    labels, msgs, members = query_message_activity(
        db_conn, guild_id=10, resolution="hour"
    )
    assert len(labels) == 24
    assert msgs == [0] * 24


# ── query_message_histogram ──────────────────────────────────────────


def test_query_message_histogram_hour_of_day_returns_24(db_conn):
    labels, counts = query_message_histogram(
        db_conn, guild_id=10, resolution="hour_of_day"
    )
    assert labels == _HOD_LABELS
    assert counts == [0] * 24


def test_query_message_histogram_day_of_week_returns_7(db_conn):
    labels, counts = query_message_histogram(
        db_conn, guild_id=10, resolution="day_of_week"
    )
    assert labels == _DOW_LABELS
    assert counts == [0] * 7


def test_query_message_histogram_counts_messages(db_conn):
    # Pick a ts at a known UTC hour
    ts = int(datetime(2026, 5, 31, 14, 0, tzinfo=timezone.utc).timestamp())
    _seed_processed(db_conn, rows=[(1, 100, 7, ts), (2, 100, 7, ts + 60)])
    db_conn.commit()
    _, counts = query_message_histogram(
        db_conn, guild_id=10, resolution="hour_of_day"
    )
    assert counts[14] == 2


def test_query_message_histogram_filter_by_channel_and_user(db_conn):
    ts = int(datetime(2026, 5, 31, 14, 0, tzinfo=timezone.utc).timestamp())
    _seed_processed(
        db_conn,
        rows=[(1, 100, 7, ts), (2, 200, 7, ts), (3, 100, 8, ts)],
    )
    db_conn.commit()
    _, counts = query_message_histogram(
        db_conn, guild_id=10, resolution="hour_of_day", channel_id=100, user_id=7
    )
    assert counts[14] == 1


# ── query_xp_activity / xp_histogram ─────────────────────────────────


def test_query_xp_activity_empty(db_conn):
    labels, xps, members = query_xp_activity(
        db_conn, guild_id=10, resolution="day"
    )
    assert len(labels) == 30
    assert xps == [0.0] * 30
    assert members == [0] * 30


def test_query_xp_activity_sums_amounts(db_conn):
    now_ts = datetime.now(timezone.utc).timestamp() - 60
    _seed_xp(
        db_conn,
        rows=[(7, "text", 5.0, now_ts), (8, "voice", 2.5, now_ts)],
    )
    db_conn.commit()
    _, xps, members = query_xp_activity(db_conn, guild_id=10, resolution="day")
    assert sum(xps) == pytest.approx(7.5, abs=0.05)
    assert max(members) == 2


def test_query_xp_histogram_sums_by_hour(db_conn):
    ts = int(datetime(2026, 5, 31, 9, 0, tzinfo=timezone.utc).timestamp())
    _seed_xp(db_conn, rows=[(7, "text", 4.0, ts), (8, "text", 3.0, ts + 60)])
    db_conn.commit()
    _, counts = query_xp_histogram(
        db_conn, guild_id=10, resolution="hour_of_day"
    )
    assert counts[9] == pytest.approx(7.0, abs=0.05)


def test_query_xp_activity_with_breakdown_separates_sources(db_conn):
    now_ts = datetime.now(timezone.utc).timestamp() - 60
    _seed_xp(
        db_conn,
        rows=[(7, "text", 5.0, now_ts), (7, "voice", 3.0, now_ts)],
    )
    db_conn.commit()
    _, totals, _, by_src = query_xp_activity_with_breakdown(
        db_conn, guild_id=10, resolution="day"
    )
    assert sum(totals) == pytest.approx(8.0, abs=0.05)
    assert "text" in by_src and "voice" in by_src
    assert sum(by_src["text"]) == pytest.approx(5.0, abs=0.05)
    assert sum(by_src["voice"]) == pytest.approx(3.0, abs=0.05)


def test_query_xp_histogram_with_breakdown_separates_sources(db_conn):
    ts = int(datetime(2026, 5, 31, 10, 0, tzinfo=timezone.utc).timestamp())
    _seed_xp(
        db_conn,
        rows=[(7, "text", 2.0, ts), (7, "reply", 1.0, ts + 60)],
    )
    db_conn.commit()
    _, totals, by_src = query_xp_histogram_with_breakdown(
        db_conn, guild_id=10, resolution="hour_of_day"
    )
    assert totals[10] == pytest.approx(3.0, abs=0.05)
    assert by_src["text"][10] == pytest.approx(2.0, abs=0.05)
    assert by_src["reply"][10] == pytest.approx(1.0, abs=0.05)


# ── query_message_rate_drops ─────────────────────────────────────────


def test_query_message_rate_drops_empty(db_conn):
    drops = query_message_rate_drops(db_conn, guild_id=10, period_seconds=86400)
    assert drops == []


def test_query_message_rate_drops_identifies_users(db_conn):
    """User 7 sends 10 in the previous half-window and 1 in the recent half."""
    now_ts = int(datetime.now(timezone.utc).timestamp())
    period = 3600  # 1 hour halves → 2 hour full window
    prev_ts = now_ts - period - 60  # squarely in previous half
    recent_ts = now_ts - 60
    rows = []
    mid = 1
    for _ in range(10):
        rows.append((mid, 100, 7, prev_ts))
        mid += 1
        prev_ts += 1
    rows.append((mid, 100, 7, recent_ts))
    _seed_processed(db_conn, rows=rows)
    db_conn.commit()

    drops = query_message_rate_drops(
        db_conn, guild_id=10, period_seconds=period, min_previous=5
    )
    assert len(drops) == 1
    uid, prev_count, recent_count = drops[0]
    assert uid == 7
    assert prev_count == 10
    assert recent_count == 1


def test_query_message_rate_drops_respects_channel_filter(db_conn):
    now_ts = int(datetime.now(timezone.utc).timestamp())
    period = 3600
    prev_ts = now_ts - period - 60
    recent_ts = now_ts - 60
    rows = [(i, 200, 7, prev_ts + i) for i in range(1, 7)]  # 6 prev in channel 200
    rows.append((100, 200, 7, recent_ts))  # 1 recent
    rows.extend((200 + i, 100, 8, prev_ts + i) for i in range(6))  # noise channel
    _seed_processed(db_conn, rows=rows)
    db_conn.commit()

    drops = query_message_rate_drops(
        db_conn, guild_id=10, period_seconds=period, channel_id=200, min_previous=5
    )
    assert len(drops) == 1
    assert drops[0][0] == 7


# ── query_dropoff_profiles ───────────────────────────────────────────


def test_query_dropoff_profiles_empty(db_conn):
    profiles = query_dropoff_profiles(db_conn, guild_id=10, period_seconds=86400)
    assert profiles == []


def test_query_dropoff_profiles_target_user_returns_one(db_conn):
    """Asking about a specific user returns a single profile even with no drop."""
    now_ts = int(datetime.now(timezone.utc).timestamp())
    period = 3600
    prev_ts = now_ts - period - 60
    _seed_processed(db_conn, rows=[(1, 100, 42, prev_ts)])
    db_conn.commit()
    profiles = query_dropoff_profiles(
        db_conn, guild_id=10, period_seconds=period, target_user_id=42
    )
    assert len(profiles) == 1
    assert isinstance(profiles[0], DropoffProfile)
    assert profiles[0].user_id == 42


def test_query_dropoff_profiles_returns_rich_metadata(db_conn):
    """A user with messages in previous + recent windows gets a full profile."""
    now_ts = int(datetime.now(timezone.utc).timestamp())
    period = 86400  # 1 day halves
    prev_ts = now_ts - period - 100
    recent_ts = now_ts - 100

    # Seed both processed_messages (for rate detection) and messages (for enrichment)
    proc_rows = [(i, 100, 42, prev_ts + i) for i in range(10)]
    proc_rows.extend((200 + i, 100, 42, recent_ts + i) for i in range(3))
    _seed_processed(db_conn, rows=proc_rows)

    msg_rows = [
        (i, 100, 42, prev_ts + i, None, "hello") for i in range(10)
    ]
    msg_rows.extend(
        (200 + i, 100, 42, recent_ts + i, None, "hi") for i in range(3)
    )
    _seed_messages(db_conn, rows=msg_rows)
    db_conn.commit()

    profiles = query_dropoff_profiles(
        db_conn, guild_id=10, period_seconds=period, target_user_id=42
    )
    assert len(profiles) == 1
    p = profiles[0]
    assert p.user_id == 42
    assert p.msgs_prev == 10
    assert p.msgs_recent == 3
    assert p.days_in_window >= 1
    # Avg msg length: "hello"=5, "hi"=2
    assert p.avg_len_prev == pytest.approx(5.0, abs=0.5)
    assert p.avg_len_recent == pytest.approx(2.0, abs=0.5)


# ── query_role_growth ────────────────────────────────────────────────


def test_query_role_growth_empty(db_conn):
    labels, role_counts = query_role_growth(db_conn, guild_id=10, resolution="day")
    assert len(labels) == 30
    assert role_counts == {}


def test_query_role_growth_tracks_net_grants(db_conn):
    """Two grants - one removal = net +1 inside the window."""
    now_ts = datetime.now(timezone.utc).timestamp() - 60
    for action, ts in [("grant", now_ts), ("grant", now_ts - 30), ("remove", now_ts - 15)]:
        db_conn.execute(
            "INSERT INTO role_events (guild_id, user_id, role_name, action, granted_at)"
            " VALUES (?,?,?,?,?)",
            (10, 1, "Booster", action, ts),
        )
    db_conn.commit()
    _, role_counts = query_role_growth(db_conn, guild_id=10, resolution="day")
    assert "Booster" in role_counts
    assert role_counts["Booster"][-1] == 1


def test_query_role_growth_uses_baseline_before_window(db_conn):
    """Pre-window grants form a positive baseline."""
    # 60 days ago — before the 30-day window
    old_ts = datetime.now(timezone.utc).timestamp() - 60 * 86400
    for _ in range(3):
        db_conn.execute(
            "INSERT INTO role_events (guild_id, user_id, role_name, action, granted_at)"
            " VALUES (?,?,?,?,?)",
            (10, 1, "OG", "grant", old_ts),
        )
    db_conn.commit()
    _, role_counts = query_role_growth(db_conn, guild_id=10, resolution="day")
    assert role_counts["OG"][0] == 3  # baseline carried forward
    assert role_counts["OG"][-1] == 3


# ── query_session_burst ──────────────────────────────────────────────


def test_query_session_burst_returns_zero_if_too_few_messages(db_conn):
    now_ts = datetime.now(timezone.utc).timestamp()
    _seed_processed(db_conn, rows=[(1, 100, 7, now_ts)])  # only 1 msg
    db_conn.commit()
    pre, post, rate = query_session_burst(db_conn, guild_id=10, user_id=7)
    assert pre == []
    assert post == []
    assert rate == 0.0


def test_query_session_burst_segments_sessions(db_conn):
    """Two user messages with a >20min gap → two session starts."""
    base_ts = datetime.now(timezone.utc).timestamp() - 7200
    rows = [
        (1, 100, 7, base_ts),
        (2, 100, 7, base_ts + 60),       # part of session 1
        (3, 100, 7, base_ts + 30 * 60),  # >20min gap → session 2
    ]
    _seed_processed(db_conn, rows=rows)
    db_conn.commit()
    pre, post, rate = query_session_burst(db_conn, guild_id=10, user_id=7)
    # 2 sessions detected
    assert len(pre) == 2
    assert len(post) == 2
    # Bin counts per session match the configured window sizes
    assert len(pre[0]) == 10  # 20 min / 2 min
    assert len(post[0]) == 30  # 60 min / 2 min
    assert rate >= 0.0


# ── query_burst_ranking ──────────────────────────────────────────────


def test_query_burst_ranking_empty(db_conn):
    assert query_burst_ranking(db_conn, guild_id=10) == []


def test_query_burst_ranking_requires_min_sessions(db_conn):
    base_ts = datetime.now(timezone.utc).timestamp() - 3600
    _seed_processed(db_conn, rows=[(1, 100, 7, base_ts), (2, 100, 7, base_ts + 60)])
    db_conn.commit()
    # Only 1 session detected → below default min_sessions=3
    assert query_burst_ranking(db_conn, guild_id=10) == []


def test_query_burst_ranking_with_days_cutoff_returns_recent_only(db_conn):
    """Old data outside the days window is ignored."""
    very_old = datetime.now(timezone.utc).timestamp() - 365 * 86400
    rows = [(i, 100, 7, very_old + i) for i in range(5)]
    _seed_processed(db_conn, rows=rows)
    db_conn.commit()
    # cutoff of 1 day excludes everything
    results = query_burst_ranking(db_conn, guild_id=10, days=1)
    assert results == []


# ── query_message_cadence ────────────────────────────────────────────


def test_query_message_cadence_empty(db_conn):
    out = query_message_cadence(db_conn, guild_id=10, resolution="day")
    # No messages → empty list
    assert out == []


def test_query_message_cadence_hour_of_day_returns_24_buckets_on_empty(db_conn):
    # When there are no messages it returns [] but with one message → fixed bin set
    ts = int(datetime(2026, 5, 31, 11, 0, tzinfo=timezone.utc).timestamp())
    _seed_messages(
        db_conn,
        rows=[
            (1, 100, 7, ts, None, "hi"),
            (2, 100, 7, ts + 600, None, "ho"),
        ],
    )
    db_conn.commit()
    buckets = query_message_cadence(
        db_conn, guild_id=10, resolution="hour_of_day"
    )
    assert len(buckets) == 24
    assert all(isinstance(b, CadenceBucket) for b in buckets)
    # The bucket at hour 11 must have a non-zero gap (600s / 60 = 10 minutes)
    assert buckets[11].median_gap > 0


def test_query_message_cadence_day_of_week_returns_7_buckets(db_conn):
    ts = int(datetime(2026, 5, 31, 11, 0, tzinfo=timezone.utc).timestamp())
    _seed_messages(
        db_conn,
        rows=[(1, 100, 7, ts, None, "a"), (2, 100, 7, ts + 60, None, "b")],
    )
    db_conn.commit()
    buckets = query_message_cadence(
        db_conn, guild_id=10, resolution="day_of_week"
    )
    assert len(buckets) == 7


def test_query_message_cadence_day_resolution_returns_30_buckets(db_conn):
    now_ts = int(datetime.now(timezone.utc).timestamp() - 60)
    _seed_messages(
        db_conn,
        rows=[
            (1, 100, 7, now_ts, None, "a"),
            (2, 100, 7, now_ts + 60, None, "b"),
        ],
    )
    db_conn.commit()
    buckets = query_message_cadence(
        db_conn, guild_id=10, resolution="day"
    )
    assert len(buckets) == 30


# ── query_nsfw_gender_activity ───────────────────────────────────────


def test_query_nsfw_gender_activity_empty_channels(db_conn):
    labels, counts = query_nsfw_gender_activity(
        db_conn, guild_id=10, resolution="day", channel_ids=[]
    )
    assert labels == []
    assert counts == {}


def test_query_nsfw_gender_activity_buckets_by_gender(db_conn):
    now_ts = int(datetime.now(timezone.utc).timestamp() - 60)
    _seed_messages(
        db_conn,
        rows=[
            (1, 999, 7, now_ts, None, "hi"),
            (2, 999, 8, now_ts, None, "hi"),
        ],
    )
    db_conn.execute(
        "INSERT INTO member_gender (guild_id, user_id, gender, set_by, set_at)"
        " VALUES (?,?,?,?,?)",
        (10, 7, "male", 0, now_ts),
    )
    db_conn.commit()
    labels, by_gender = query_nsfw_gender_activity(
        db_conn, guild_id=10, resolution="day", channel_ids=[999]
    )
    assert len(labels) == 30
    assert "male" in by_gender
    assert "unknown" in by_gender  # user 8 has no gender entry
    assert sum(by_gender["male"]) == 1
    assert sum(by_gender["unknown"]) == 1


def test_query_nsfw_gender_activity_media_only_filters_by_media_kind(db_conn):
    now_ts = int(datetime.now(timezone.utc).timestamp() - 60)
    _seed_messages(
        db_conn,
        rows=[
            (1, 999, 7, now_ts, None, "hi"),
            (2, 999, 7, now_ts + 1, None, "pic"),
            (3, 999, 7, now_ts + 2, None, "gif"),
        ],
    )
    # media_kind is the lightweight metadata that drives the media split — it is
    # recorded even when raw attachment URLs are not retained (storage "none").
    db_conn.execute("UPDATE messages SET media_kind = 'media' WHERE message_id = 2")
    db_conn.execute("UPDATE messages SET media_kind = 'gif' WHERE message_id = 3")
    db_conn.commit()
    _, by_gender = query_nsfw_gender_activity(
        db_conn,
        guild_id=10,
        resolution="day",
        channel_ids=[999],
        media_only=True,
    )
    # Only message 2 counts: 'media' is included, 'gif' and text are excluded.
    total = sum(sum(v) for v in by_gender.values())
    assert total == 1


# ── query_greeter_response_times ─────────────────────────────────────


def test_query_greeter_response_times_no_inputs_returns_empty(db_conn):
    assert query_greeter_response_times(
        db_conn,
        guild_id=10,
        greeter_channel_id=100,
        greeter_user_ids=set(),
        join_times={},
    ) == []


def test_query_greeter_response_times_no_greeter_messages_returns_empty(db_conn):
    assert query_greeter_response_times(
        db_conn,
        guild_id=10,
        greeter_channel_id=100,
        greeter_user_ids={42},
        join_times={1: 1000.0},
    ) == []


def test_query_greeter_response_times_computes_deltas(db_conn):
    """Greeter posts at t=1000, member joined at t=900 → response time = 100s."""
    _seed_messages(
        db_conn, rows=[(1, 100, 42, 1000, None, "welcome")]
    )
    db_conn.commit()
    rts = query_greeter_response_times(
        db_conn,
        guild_id=10,
        greeter_channel_id=100,
        greeter_user_ids={42},
        join_times={500: 900.0},
    )
    assert rts == [100]


# ── query_message_rate_10min ─────────────────────────────────────────


def test_query_message_rate_10min_returns_144_buckets(db_conn):
    counts = query_message_rate_10min(db_conn, guild_id=10, days=7)
    assert len(counts) == 144
    assert counts == [0] * 144


def test_query_message_rate_10min_counts_messages(db_conn):
    ts = int(datetime(2026, 5, 31, 9, 5, tzinfo=timezone.utc).timestamp())
    _seed_processed(db_conn, rows=[(1, 100, 7, ts), (2, 100, 7, ts + 60)])
    db_conn.commit()
    counts = query_message_rate_10min(db_conn, guild_id=10, days=365)
    # 09:05 → bucket = 9*6 + 0 = 54
    assert counts[54] == 2


def test_query_message_rate_10min_channel_filter(db_conn):
    ts = int(datetime(2026, 5, 31, 9, 5, tzinfo=timezone.utc).timestamp())
    _seed_processed(
        db_conn,
        rows=[(1, 100, 7, ts), (2, 200, 7, ts)],
    )
    db_conn.commit()
    counts = query_message_rate_10min(
        db_conn, guild_id=10, days=365, channel_id=100
    )
    assert counts[54] == 1


# ── Render functions (smoke tests for PNG output) ────────────────────


def test_render_activity_chart_returns_png():
    labels = [f"D{i}" for i in range(5)]
    out = render_activity_chart(
        labels, [1, 2, 3, 4, 5], [1, 1, 1, 1, 1], "Title", "day"
    )
    assert isinstance(out, bytes)
    assert out[:8] == PNG_MAGIC


def test_render_activity_chart_with_breakdown_uses_stacked_bars():
    labels = ["A", "B"]
    by_source = {"text": [1.0, 2.0], "voice": [0.5, 0.5]}
    out = render_activity_chart(
        labels, [1.5, 2.5], [0, 0], "title", "day", by_source=by_source
    )
    assert out[:8] == PNG_MAGIC


def test_render_activity_chart_hides_member_overlay_when_zero():
    out = render_activity_chart(
        ["a", "b"], [1, 2], [0, 0], "title", "day", show_members=False
    )
    assert out[:8] == PNG_MAGIC


def test_render_activity_chart_many_labels_thins_ticks():
    """Trigger the >20-label thinning branch."""
    labels = [f"B{i}" for i in range(50)]
    counts = [1] * 50
    members = [1] * 50
    out = render_activity_chart(labels, counts, members, "t", "day")
    assert out[:8] == PNG_MAGIC


def test_render_level_histogram_returns_png():
    durations: list[float] = [float(86400 * i) for i in range(1, 10)]
    out = render_level_histogram(
        durations,
        target_level=5,
        xp_required=1000,
        mean_s=float(5 * 86400),
        stddev_s=float(86400),
        modal_days=3,
    )
    assert out[:8] == PNG_MAGIC


def test_render_role_growth_chart_returns_png():
    labels = ["a", "b", "c"]
    role_counts = {"Mod": [1, 2, 3], "Booster": [0, 1, 1]}
    out = render_role_growth_chart(labels, role_counts, "Roles")
    assert out[:8] == PNG_MAGIC


def test_render_role_growth_chart_many_labels_thins_ticks():
    labels = [f"d{i}" for i in range(40)]
    role_counts = {"R": [i for i in range(40)]}
    out = render_role_growth_chart(labels, role_counts, "Roles")
    assert out[:8] == PNG_MAGIC


def test_render_role_growth_chart_empty_dict_still_returns_png():
    out = render_role_growth_chart(["a", "b"], {}, "Roles")
    assert out[:8] == PNG_MAGIC


def test_render_session_burst_chart_returns_png():
    # 10 pre-bins, 30 post-bins per spec
    pre = [[1.0] * 10 for _ in range(2)]
    post = [[2.0] * 30 for _ in range(2)]
    out = render_session_burst_chart(pre, post, overall_rate=1.5, user_display_name="Alice")
    assert out[:8] == PNG_MAGIC


def test_render_session_burst_chart_handles_many_sessions():
    """>20 sessions should suppress individual lines but still render."""
    pre = [[1.0] * 10 for _ in range(25)]
    post = [[2.0] * 30 for _ in range(25)]
    out = render_session_burst_chart(pre, post, overall_rate=0.0, user_display_name="X")
    assert out[:8] == PNG_MAGIC


def test_render_burst_ranking_chart_returns_png():
    entries = [
        ("Alice", 1.0, 3.0, 5),
        ("Bob", 0.5, 1.0, 4),
        ("Carol", 0.4, 0.6, 4),
    ]
    out = render_burst_ranking_chart(entries, limit=2, guild_name="Guild")
    assert out[:8] == PNG_MAGIC


def test_render_burst_ranking_chart_raises_on_empty():
    with pytest.raises(ValueError):
        render_burst_ranking_chart([], limit=5, guild_name="X")


def test_render_burst_ranking_chart_handles_single_entry():
    out = render_burst_ranking_chart(
        [("solo", 0.0, 1.0, 5)], limit=10, guild_name="G"
    )
    assert out[:8] == PNG_MAGIC


def test_render_message_cadence_chart_returns_png():
    buckets = [
        CadenceBucket(label=f"B{i}", min_gap=1, p20_gap=2, median_gap=3, p80_gap=4, max_gap=5)
        for i in range(5)
    ]
    out = render_message_cadence_chart(buckets, "Cadence")
    assert out[:8] == PNG_MAGIC


def test_render_message_cadence_chart_zero_buckets_skipped():
    """Buckets with max_gap=0 are skipped but the render still emits PNG."""
    buckets = [
        CadenceBucket(label="empty", min_gap=0, p20_gap=0, median_gap=0, p80_gap=0, max_gap=0),
        CadenceBucket(label="full", min_gap=1, p20_gap=2, median_gap=3, p80_gap=4, max_gap=5),
    ]
    out = render_message_cadence_chart(buckets, "Cadence")
    assert out[:8] == PNG_MAGIC


def test_render_message_cadence_chart_many_labels_thins_ticks():
    buckets = [
        CadenceBucket(label=f"L{i}", min_gap=1, p20_gap=2, median_gap=3 + (i % 2), p80_gap=4, max_gap=5)
        for i in range(40)
    ]
    out = render_message_cadence_chart(buckets, "Cadence")
    assert out[:8] == PNG_MAGIC


def test_render_join_histogram_returns_png():
    out = render_join_histogram(["a", "b", "c"], [1, 2, 3], "Joins")
    assert out[:8] == PNG_MAGIC


def test_render_join_histogram_many_labels_thins_ticks():
    out = render_join_histogram([f"d{i}" for i in range(40)], list(range(40)), "Joins")
    assert out[:8] == PNG_MAGIC


def test_render_nsfw_gender_chart_returns_png():
    labels = ["a", "b"]
    counts = {"male": [1, 2], "female": [3, 4]}
    out = render_nsfw_gender_chart(labels, counts, "NSFW")
    assert out[:8] == PNG_MAGIC


def test_render_nsfw_gender_chart_empty_counts_still_returns_png():
    out = render_nsfw_gender_chart(["a", "b"], {}, "NSFW")
    assert out[:8] == PNG_MAGIC


def test_render_nsfw_gender_chart_many_labels_thins_ticks():
    labels = [f"d{i}" for i in range(40)]
    counts = {"male": [1] * 40}
    out = render_nsfw_gender_chart(labels, counts, "NSFW")
    assert out[:8] == PNG_MAGIC


def test_render_nsfw_gender_line_chart_returns_png():
    out = render_nsfw_gender_line_chart(
        ["a", "b"], {"male": [1, 2], "female": [1, 1]}, "ratio"
    )
    assert out[:8] == PNG_MAGIC


def test_render_nsfw_gender_line_chart_empty_falls_back_to_bar():
    """No genders → falls through to render_nsfw_gender_chart."""
    out = render_nsfw_gender_line_chart(["a", "b"], {}, "ratio")
    assert out[:8] == PNG_MAGIC


def test_render_nsfw_gender_line_chart_many_labels_thins_ticks():
    labels = [f"d{i}" for i in range(40)]
    counts = {"male": [1] * 40, "female": [2] * 40}
    out = render_nsfw_gender_line_chart(labels, counts, "ratio")
    assert out[:8] == PNG_MAGIC


def test_render_greeter_response_chart_returns_png():
    # Mixed bucket of response times in seconds
    out = render_greeter_response_chart(
        [10, 200, 2000, 50000], "Greeter"
    )
    assert out[:8] == PNG_MAGIC


def test_render_greeter_response_chart_empty_returns_png():
    out = render_greeter_response_chart([], "Greeter")
    assert out[:8] == PNG_MAGIC


def test_render_message_rate_chart_returns_png():
    counts = [i % 5 for i in range(144)]
    out = render_message_rate_chart(counts, days_in_window=7, title="Rate")
    assert out[:8] == PNG_MAGIC


# ── DropoffProfile dataclass smoke ───────────────────────────────────


def test_dropoff_profile_dataclass_has_expected_fields():
    """Stable serialisation surface — guard against accidental field renames."""
    fields = {f for f in DropoffProfile.__dataclass_fields__}
    expected_subset = {
        "user_id",
        "msgs_prev",
        "msgs_recent",
        "voice_xp_prev",
        "voice_xp_recent",
        "days_in_window",
        "channels_left",
        "channels_joined",
        "channels_stayed",
        "deep_convos_prev",
        "deep_convos_recent",
        "server_msgs_prev",
        "server_msgs_recent",
    }
    assert expected_subset.issubset(fields)


# ── Edge cases for histogram-with-breakdown ──────────────────────────


def test_query_xp_histogram_with_breakdown_returns_empty_on_no_data(db_conn):
    labels, totals, by_src = query_xp_histogram_with_breakdown(
        db_conn, guild_id=10, resolution="day_of_week"
    )
    assert labels == _DOW_LABELS
    assert totals == [0.0] * 7
    assert by_src == {}


def test_query_xp_activity_with_breakdown_returns_empty_on_no_data(db_conn):
    labels, totals, members, by_src = query_xp_activity_with_breakdown(
        db_conn, guild_id=10, resolution="day"
    )
    assert len(labels) == 30
    assert totals == [0.0] * 30
    assert members == [0] * 30
    assert by_src == {}
