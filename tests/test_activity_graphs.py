"""Tests for bot_modules.services.activity_graphs.

The module is large (~1170 statements) and at ~21% coverage before this
file.  Strategy mirrors test_interaction_graph.py: cover pure helpers,
DB query helpers, and smoke-test PNG renderers.  Renderers are smoke-
tested for matplotlib PNG magic; pixel content is not validated.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.activity_graphs import (
    DropoffProfile,
    _append_exclusions,
    _percentile,
    _BUCKET_BUILDERS,
    _day_buckets,
    _DOW_LABELS,
    _hour_buckets,
    _HOD_LABELS,
    _month_buckets,
    _strftime_expr,
    _week_buckets,
    _WINDOW_LABELS,
    OVERLAY_SMOOTH_WINDOW,
    OverlayChart,
    overlay_labels,
    overlay_period_cap,
    overlay_period_start,
    overlay_stride_days,
    overlay_weekday_name,
    query_activity_overlay,
    query_dropoff_profiles,
    query_greeter_response_times,
    query_message_activity,
    query_message_histogram,
    query_message_rate_drops,
    query_nsfw_gender_activity,
    query_nsfw_tag_activity,
    query_xp_activity,
    query_xp_activity_with_breakdown,
    query_xp_histogram,
    query_xp_histogram_with_breakdown,
    render_activity_chart,
    render_greeter_response_chart,
    render_join_histogram,
    render_level_histogram,
    render_nsfw_gender_chart,
    render_nsfw_gender_line_chart,
    render_overlay_panel,
    smooth_series,
)
from tests.db_template import migrated_db


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# ── Shared fixtures ──────────────────────────────────────────────────


@pytest.fixture
def db_conn(tmp_path):
    """A migrated SQLite connection ready for activity-graph tests."""
    path = tmp_path / "ag.db"
    migrated_db(path)
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


def _recent_utc_hour(hour: int) -> int:
    """Yesterday at ``hour`` UTC — inside the histogram's 90-day window.

    The window is measured from ``time.time()``, so a hard-coded date is a
    time bomb: it passes when written and goes red weeks later with nobody
    having touched the code (this pair detonated 2026-08-29, exactly 90 days
    after the 2026-05-31 they pinned). Only the *hour* matters to an
    hour-of-day bucket, so anchor the day to now and keep the hour fixed.
    """
    return int(
        (datetime.now(timezone.utc) - timedelta(days=1))
        .replace(hour=hour, minute=0, second=0, microsecond=0)
        .timestamp()
    )


def test_query_xp_histogram_sums_by_hour(db_conn):
    ts = _recent_utc_hour(9)
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
    ts = _recent_utc_hour(10)
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


# ── query_session_burst ──────────────────────────────────────────────


# ── query_burst_ranking ──────────────────────────────────────────────


# ── query_message_cadence ────────────────────────────────────────────


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


# ── query_nsfw_tag_activity ──────────────────────────────────────────


def _seed_classification(
    conn,
    *,
    message_id,
    label,
    guild_id=10,
    channel_id=999,
    created_at=None,
    verdict=1,
    marqo_score=0.9,
):
    """One nsfw_classifications row.

    `label=None` = classified but never tagged; `marqo_score=None` = a row
    written before the Marqo swap (migration 147).
    """
    if created_at is None:
        created_at = int(datetime.now(timezone.utc).timestamp() - 60)
    conn.execute(
        """
        INSERT INTO nsfw_classifications
            (message_id, attachment_id, guild_id, channel_id, verdict,
             marqo_score, top_label, top_score, model, threshold, label_set,
             inference_ms, bytes, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (message_id, 1, guild_id, channel_id, verdict, marqo_score, label,
         0.8 if label else None, "320n", 0.5, "", 12, 1024, created_at),
    )


def test_query_nsfw_tag_activity_buckets_by_label(db_conn):
    _seed_classification(db_conn, message_id=1, label="FEMALE_BREAST_EXPOSED")
    _seed_classification(db_conn, message_id=2, label="FEMALE_BREAST_EXPOSED")
    _seed_classification(db_conn, message_id=3, label="BUTTOCKS_EXPOSED")
    db_conn.commit()

    labels, by_tag = query_nsfw_tag_activity(db_conn, guild_id=10, resolution="day")

    assert len(labels) == 30
    assert sum(by_tag["FEMALE_BREAST_EXPOSED"]) == 2
    assert sum(by_tag["BUTTOCKS_EXPOSED"]) == 1
    # Every series spans the full bucket sequence, or the stack misaligns.
    assert all(len(counts) == 30 for counts in by_tag.values())


def test_query_nsfw_tag_activity_excludes_untagged_rows(db_conn):
    """Marqo writes a verdict for every image; NudeNet a label only where it ran.

    An untagged row is not "tagged, but nothing qualified" — it is an image
    outside this report's scope, and counting it would put the majority of the
    table into a band the chart has nothing true to say about.
    """
    _seed_classification(db_conn, message_id=1, label="SEX_ACT")
    _seed_classification(db_conn, message_id=2, label=None)
    _seed_classification(db_conn, message_id=3, label="")
    db_conn.commit()

    _, by_tag = query_nsfw_tag_activity(db_conn, guild_id=10, resolution="day")

    assert set(by_tag) == {"SEX_ACT"}
    assert sum(sum(c) for c in by_tag.values()) == 1


def test_query_nsfw_tag_activity_excludes_pre_swap_rows(db_conn):
    """Rows written before the Marqo swap carry a NULL marqo_score.

    /api/moderation/nsfw-tags drops them, and this report is documented as
    showing the same labels — so a total that silently disagreed with the panel
    next door would be worse than the handful of rows it costs. Only a 12-month
    window reaches back far enough to contain any.
    """
    _seed_classification(db_conn, message_id=1, label="BUTTOCKS_EXPOSED")
    _seed_classification(db_conn, message_id=2, label="BUTTOCKS_EXPOSED", marqo_score=None)
    db_conn.commit()

    _, by_tag = query_nsfw_tag_activity(db_conn, guild_id=10, resolution="day")

    assert sum(by_tag["BUTTOCKS_EXPOSED"]) == 1


def test_query_nsfw_tag_activity_orders_by_taxonomy_not_volume(db_conn):
    """Series order is fixed, so a series keeps its colour across windows.

    Ordering by observed volume would repaint every band whenever a different
    label happened to lead — the exact instability the fixed list prevents.
    """
    for i in range(5):
        _seed_classification(db_conn, message_id=100 + i, label="BUTTOCKS_EXPOSED")
    _seed_classification(db_conn, message_id=1, label="FEMALE_BREAST_EXPOSED")
    db_conn.commit()

    _, by_tag = query_nsfw_tag_activity(db_conn, guild_id=10, resolution="day")

    # BUTTOCKS_EXPOSED leads on volume 5:1 and still sorts after the chest label.
    assert list(by_tag) == ["FEMALE_BREAST_EXPOSED", "BUTTOCKS_EXPOSED"]


def test_query_nsfw_tag_activity_reports_labels_outside_the_known_order(db_conn):
    """The vocabulary is the detector's. An unfamiliar label is appended, never dropped."""
    _seed_classification(db_conn, message_id=1, label="FEMALE_BREAST_EXPOSED")
    _seed_classification(db_conn, message_id=2, label="ZZ_NEW_LABEL")
    db_conn.commit()

    _, by_tag = query_nsfw_tag_activity(db_conn, guild_id=10, resolution="day")

    assert list(by_tag) == ["FEMALE_BREAST_EXPOSED", "ZZ_NEW_LABEL"]


def test_query_nsfw_tag_activity_channel_filter_narrows(db_conn):
    _seed_classification(db_conn, message_id=1, label="SEX_ACT", channel_id=999)
    _seed_classification(db_conn, message_id=2, label="SEX_ACT", channel_id=888)
    db_conn.commit()

    _, all_ch = query_nsfw_tag_activity(db_conn, guild_id=10, resolution="day")
    _, one_ch = query_nsfw_tag_activity(
        db_conn, guild_id=10, resolution="day", channel_ids=[999]
    )

    assert sum(all_ch["SEX_ACT"]) == 2
    assert sum(one_ch["SEX_ACT"]) == 1


def test_query_nsfw_tag_activity_is_scoped_to_one_guild(db_conn):
    _seed_classification(db_conn, message_id=1, label="SEX_ACT", guild_id=10)
    _seed_classification(db_conn, message_id=2, label="SEX_ACT", guild_id=11)
    db_conn.commit()

    _, by_tag = query_nsfw_tag_activity(db_conn, guild_id=10, resolution="day")

    assert sum(by_tag["SEX_ACT"]) == 1


def test_query_nsfw_tag_activity_empty_table_returns_bucket_labels(db_conn):
    """No rows is not no chart: the axis still spans the window, with no series."""
    labels, by_tag = query_nsfw_tag_activity(db_conn, guild_id=10, resolution="week")

    assert len(labels) == 12
    assert by_tag == {}


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


# ── Period overlay (this day/week vs a band of the last N) ────────────

_WEEK = 7 * 86400


def test_overlay_period_start_anchors_to_local_midnight():
    """The anchor is guild-local, not UTC — the off-by-one-timezone bug."""
    # Thu 2026-08-27 03:00 UTC is Wed 2026-08-26 20:00 at UTC-7, so "today"
    # began Wed 00:00 local (= Wed 07:00 UTC) and the week began the Sunday
    # before that (Sun 2026-08-23 00:00 local = Sun 07:00 UTC).
    now = datetime(2026, 8, 27, 3, 0, tzinfo=timezone.utc)
    assert overlay_period_start(now, -7, "day") == datetime(
        2026, 8, 26, 7, 0, tzinfo=timezone.utc
    ).timestamp()
    assert overlay_period_start(now, -7, "week") == datetime(
        2026, 8, 23, 7, 0, tzinfo=timezone.utc
    ).timestamp()


def test_overlay_period_start_utc_is_plain_midnight():
    now = datetime(2026, 8, 27, 3, 0, tzinfo=timezone.utc)
    assert overlay_period_start(now, 0, "day") == datetime(
        2026, 8, 27, tzinfo=timezone.utc
    ).timestamp()
    assert overlay_period_start(now, 0, "week") == datetime(
        2026, 8, 23, tzinfo=timezone.utc
    ).timestamp()


def test_overlay_labels_shapes():
    assert overlay_labels("day") == _HOD_LABELS
    week = overlay_labels("week")
    assert len(week) == 168
    assert week[0] == "Sun 12am"
    assert week[167] == "Sat 11pm"


@pytest.mark.parametrize(
    "q,expected",
    [(0.25, 1.75), (0.5, 2.5), (0.75, 3.25)],
)
def test_percentile_interpolates(q, expected):
    assert _percentile([4.0, 1.0, 3.0, 2.0], q) == expected


def test_percentile_edges():
    assert _percentile([], 0.5) == 0.0
    assert _percentile([7.0], 0.5) == 7.0


@pytest.mark.parametrize(
    "period,mode,same_weekday,expected",
    [
        ("week", "messages", False, 26),
        # 90 days of raw retention is 12 whole weeks, which is why the panel
        # offers 26 weeks in messages mode only.
        ("week", "xp", False, 12),
        ("day", "messages", False, 90),
        ("day", "xp", False, 90),
        # Same-weekday days step a week apart, so they are capped like weeks
        # rather than like days: 26 back in messages, and in XP only as far as
        # raw retention reaches — 12 x 7 = 84 days fits inside 90, 13 does not.
        ("day", "messages", True, 26),
        ("day", "xp", True, 12),
        # A week is already every seventh day; the flag changes nothing there.
        ("week", "xp", True, 12),
    ],
)
def test_overlay_period_cap(period, mode, same_weekday, expected):
    assert overlay_period_cap(period, mode, same_weekday) == expected


@pytest.mark.parametrize(
    "period,same_weekday,expected",
    [("day", False, 1), ("day", True, 7), ("week", False, 7), ("week", True, 7)],
)
def test_overlay_stride_days(period, same_weekday, expected):
    assert overlay_stride_days(period, same_weekday) == expected


def test_overlay_weekday_name_reads_the_guild_clock():
    """18:00 Saturday in a UTC-7 guild is already Sunday in UTC."""
    now = datetime(2026, 8, 30, 1, 0, tzinfo=timezone.utc)  # Sunday 01:00 UTC
    assert overlay_weekday_name(now, 0.0) == "Sunday"
    assert overlay_weekday_name(now, -7.0) == "Saturday"


def _seed_weeks(conn, tz, counts_by_week, hour_of_week=0):
    """Seed `counts_by_week[k]` messages at `hour_of_week` of the week k back."""
    start = overlay_period_start(datetime.now(timezone.utc), tz, "week")
    mid = 1
    rows = []
    for weeks_back, count in counts_by_week.items():
        ts = start - weeks_back * _WEEK + hour_of_week * 3600 + 1800
        for _ in range(count):
            rows.append((mid, 100, 7, ts))
            mid += 1
    _seed_processed(conn, rows=rows)
    return start


def test_overlay_buckets_by_local_hour_of_week(db_conn):
    """23:30 local on a Saturday is hour-of-week 167, at a negative offset."""
    _seed_weeks(db_conn, -7.0, {1: 1, 2: 1, 3: 1}, hour_of_week=167)
    res = query_activity_overlay(
        db_conn, 10, "week", mode="messages", compare_periods=4, utc_offset_hours=-7.0
    )
    assert res.periods_sampled == 3
    assert res.band_mid[167] == 1.0
    assert res.band_mid[0] == 0.0
    assert res.labels[167] == "Sat 11pm"


def test_overlay_band_is_percentiles_over_sampled_weeks(db_conn):
    _seed_weeks(db_conn, 0.0, {1: 4, 2: 3, 3: 2, 4: 1})
    res = query_activity_overlay(
        db_conn, 10, "week", mode="messages", compare_periods=4, utc_offset_hours=0.0
    )
    assert res.periods_sampled == 4
    # Interpolated p25/p50/p75 of 1,2,3,4 — see test_percentile_interpolates
    # for the exact values; the query rounds to 1dp like every other total here.
    assert res.band_low[0] == 1.8
    assert res.band_mid[0] == 2.5
    assert res.band_high[0] == 3.2


def test_overlay_skips_weeks_with_no_data_at_all(db_conn):
    """Weeks before the archive starts must not be counted as zeros.

    Asking for 8 weeks when only 4 exist has to sample the 4 — counting the
    other 4 as rows of zeros would drag the median to 1.5 for a reason that is
    an artefact of when logging started, not a fact about the server.
    """
    _seed_weeks(db_conn, 0.0, {1: 4, 2: 3, 3: 2, 4: 1})
    res = query_activity_overlay(
        db_conn, 10, "week", mode="messages", compare_periods=8, utc_offset_hours=0.0
    )
    assert res.periods_sampled == 4
    assert res.band_mid[0] == 2.5


def test_overlay_suppresses_band_below_minimum_sample(db_conn):
    _seed_weeks(db_conn, 0.0, {1: 4, 2: 3})
    res = query_activity_overlay(
        db_conn, 10, "week", mode="messages", compare_periods=4, utc_offset_hours=0.0
    )
    assert res.periods_sampled == 2
    assert res.has_band is False
    assert res.band_mid == []


def test_overlay_current_period_stops_at_the_hour_we_are_in(db_conn):
    """Unlived hours are None, never 0 — a zero would draw a cliff."""
    now = datetime.now(timezone.utc)
    start = overlay_period_start(now, 0.0, "day")
    expected_lived = int((now.timestamp() - start) // 3600) + 1

    res = query_activity_overlay(
        db_conn, 10, "day", mode="messages", compare_periods=7, utc_offset_hours=0.0
    )
    assert len(res.current) == 24
    lived = sum(1 for c in res.current if c is not None)
    # +-1 rather than equality only because the hour can roll between the two
    # clock reads above; the shape assertion below is the real check.
    assert abs(lived - expected_lived) <= 1
    assert all(c is None for c in res.current[lived:])


def test_overlay_xp_never_unions_the_daily_rollup(db_conn, monkeypatch):
    """xp_daily has no hour-of-day, so the overlay must clamp instead.

    A rolled-up row is stamped at its UTC day's midnight; unioning it would
    invent a spike in one hour bucket and a trough across the other 23.
    """
    from bot_modules.services import xp_rollup_service

    now = datetime.now(timezone.utc)
    start = overlay_period_start(now, 0.0, "week")
    boundary_ts = start - 2 * _WEEK
    monkeypatch.setattr(
        xp_rollup_service,
        "read_boundary",
        lambda conn, **kw: (xp_rollup_service.utc_day(boundary_ts), boundary_ts),
    )

    # A fat rollup row well below the boundary, inside the *requested* window.
    db_conn.execute(
        "INSERT INTO xp_daily (guild_id, user_id, source, channel_id, day, xp,"
        " events, first_at, last_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (10, 7, "text", 100, xp_rollup_service.utc_day(start - 5 * _WEEK),
         9999.0, 1, start - 5 * _WEEK, start - 5 * _WEEK),
    )
    _seed_xp(db_conn, rows=[(7, "text", 5.0, start - _WEEK + 1800)])

    res = query_activity_overlay(
        db_conn, 10, "week", mode="xp", compare_periods=12, utc_offset_hours=0.0
    )

    assert res.clamped is True
    # Only the raw event inside the clamped window survives; the 9999 does not
    # appear anywhere in the band.
    assert res.periods_requested == 12
    assert max(res.band_mid, default=0.0) < 9999.0
    assert 9999.0 not in res.band_mid


def test_overlay_unclamped_when_no_rollup_boundary(db_conn):
    res = query_activity_overlay(
        db_conn, 10, "week", mode="xp", compare_periods=12, utc_offset_hours=0.0
    )
    assert res.clamped is False


# ── same-weekday day overlay ──────────────────────────────────────────


def _seed_days(conn, tz, counts_by_day, hour_of_day=0):
    """Seed `counts_by_day[k]` messages at `hour_of_day` of the day k back."""
    start = overlay_period_start(datetime.now(timezone.utc), tz, "day")
    mid = 1
    rows = []
    for days_back, count in counts_by_day.items():
        ts = start - days_back * 86400 + hour_of_day * 3600 + 1800
        for _ in range(count):
            rows.append((mid, 100, 7, ts))
            mid += 1
    _seed_processed(conn, rows=rows)
    return start


def test_overlay_same_weekday_samples_every_seventh_day(db_conn):
    """The band is built from 7/14/21 days back, not from 1..21.

    The six days between each sample carry ten times the traffic; if any of
    them reached the band the median would be nowhere near 2.
    """
    counts = {d: (2 if d % 7 == 0 else 20) for d in range(1, 22)}
    _seed_days(db_conn, 0.0, counts)

    res = query_activity_overlay(
        db_conn, 10, "day", mode="messages", compare_periods=3,
        same_weekday=True, utc_offset_hours=0.0,
    )

    assert res.same_weekday is True
    assert res.periods_sampled == 3
    assert res.band_low[0] == 2.0
    assert res.band_mid[0] == 2.0
    assert res.band_high[0] == 2.0


def test_overlay_every_day_basis_still_sees_the_days_between(db_conn):
    """The contrast case: the same fixture read day-by-day is mostly 20s."""
    counts = {d: (2 if d % 7 == 0 else 20) for d in range(1, 22)}
    _seed_days(db_conn, 0.0, counts)

    res = query_activity_overlay(
        db_conn, 10, "day", mode="messages", compare_periods=21,
        same_weekday=False, utc_offset_hours=0.0,
    )

    assert res.same_weekday is False
    assert res.periods_sampled == 21
    assert res.band_mid[0] == 20.0


def test_overlay_same_weekday_keeps_the_local_hour(db_conn):
    """Hour-of-day still divides by the day, not by the seven-day stride."""
    _seed_days(db_conn, -7.0, {7: 1, 14: 1, 21: 1}, hour_of_day=23)

    res = query_activity_overlay(
        db_conn, 10, "day", mode="messages", compare_periods=3,
        same_weekday=True, utc_offset_hours=-7.0,
    )

    assert res.periods_sampled == 3
    assert res.band_mid[23] == 1.0
    assert res.band_mid[0] == 0.0


def test_overlay_same_weekday_is_ignored_for_the_week_period(db_conn):
    """A week is already every seventh day — the flag must not widen it."""
    _seed_weeks(db_conn, 0.0, {1: 4, 2: 3, 3: 2, 4: 1})

    res = query_activity_overlay(
        db_conn, 10, "week", mode="messages", compare_periods=4,
        same_weekday=True, utc_offset_hours=0.0,
    )

    assert res.same_weekday is False
    assert res.periods_sampled == 4
    assert res.band_mid[0] == 2.5


def test_overlay_same_weekday_xp_clamps_in_whole_strides(db_conn, monkeypatch):
    """The retention clamp counts same-weekdays, not days.

    A boundary 70 days back leaves exactly 10 same-weekdays reachable; asking
    for 26 must shorten to those rather than to 70.
    """
    from bot_modules.services import xp_rollup_service

    start = overlay_period_start(datetime.now(timezone.utc), 0.0, "day")
    boundary_ts = start - 70 * 86400
    monkeypatch.setattr(
        xp_rollup_service,
        "read_boundary",
        lambda conn, **kw: (xp_rollup_service.utc_day(boundary_ts), boundary_ts),
    )
    # One event in every reachable same-weekday, plus one just past the
    # boundary that the clamp must exclude.
    _seed_xp(db_conn, rows=[
        (7, "text", 3.0, start - d * 7 * 86400 + 1800) for d in range(1, 11)
    ] + [(7, "text", 999.0, start - 77 * 86400 + 1800)])

    res = query_activity_overlay(
        db_conn, 10, "day", mode="xp", compare_periods=26,
        same_weekday=True, utc_offset_hours=0.0,
    )

    assert res.clamped is True
    assert res.periods_requested == 26
    assert res.periods_sampled == 10
    assert res.band_mid[0] == 3.0


# ── Current-line smoothing ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "values,window,expected",
    [
        # A window of 1 (or 0) is smoothing turned off — the series comes back
        # as it went in, so the day overlay's raw shape is untouched.
        ([1.0, 5.0, 1.0], 1, [1.0, 5.0, 1.0]),
        ([1.0, 5.0, 1.0], 0, [1.0, 5.0, 1.0]),
        # Centred mean of three, truncating at both ends rather than padding:
        # hour 0 averages itself with hour 1 only.
        ([0.0, 3.0, 0.0, 3.0, 0.0], 3, [1.5, 1.0, 2.0, 1.0, 1.5]),
        # An unlived hour stays unlived, and the last lived hour averages only
        # with what came before it — never across the gap.
        ([2.0, 4.0, 6.0, None, None], 3, [3.0, 4.0, 5.0, None, None]),
        # A period nobody has lived an hour of yet is all Nones, not zeros.
        ([None, None], 3, [None, None]),
        ([], 3, []),
    ],
)
def test_smooth_series(values, window, expected):
    assert smooth_series(values, window) == expected


def test_smooth_series_never_overshoots_the_raw_range():
    """A mean cannot leave the range it averages over.

    Worth pinning because the chart's other softener, Chart.js `tension`, *can*
    bow a curve below zero between two low points — which is the reason the
    line is smoothed here in numbers rather than harder in the renderer.
    """
    raw = [1.0, 9.0, 2.0, 8.0, 3.0, 7.0, 4.0]
    smoothed = smooth_series(raw, 3)
    assert min(smoothed) >= min(raw)
    assert max(smoothed) <= max(raw)


def test_overlay_week_smooths_the_current_line_only(db_conn):
    """The week's line is averaged; the band it is read against is not.

    Smoothing both would blur the envelope; smoothing neither leaves 168 hourly
    points reading as hash. See docs/plans/weekly-activity-comparison.md.
    """
    # Week 0 is the one in progress; 1-4 are the history the band comes from.
    _seed_weeks(db_conn, 0.0, {0: 6, 1: 4, 2: 3, 3: 2, 4: 1})

    res = query_activity_overlay(
        db_conn, 10, "week", mode="messages", compare_periods=4, utc_offset_hours=0.0
    )

    assert res.smooth_window == OVERLAY_SMOOTH_WINDOW["week"] == 3
    assert len(res.current_smooth) == len(res.current) == 168
    # The raw series keeps the spike whole - it is what the table and the
    # period total are read from - while the drawn line is the same series
    # under the shared helper.
    assert res.current[0] == 6.0
    assert res.current_smooth == smooth_series(res.current, 3)
    # And the band is the untouched percentile of the four sampled weeks.
    assert res.band_mid[0] == 2.5
    assert res.band_low[0] == 1.8
    assert res.band_high[0] == 3.2


def test_overlay_week_smoothing_stops_at_the_live_edge(db_conn):
    """Unlived hours stay None in the smoothed line too — never bridged."""
    _seed_weeks(db_conn, 0.0, {1: 4, 2: 3, 3: 2})
    res = query_activity_overlay(
        db_conn, 10, "week", mode="messages", compare_periods=4, utc_offset_hours=0.0
    )
    lived = sum(1 for c in res.current if c is not None)
    assert [i for i, v in enumerate(res.current_smooth) if v is None] == list(
        range(lived, 168)
    )


def test_overlay_day_is_drawn_raw(db_conn):
    """24 points an hour apart are the reading; averaging them would blur it."""
    _seed_days(db_conn, 0.0, {1: 4, 2: 3, 3: 2})
    res = query_activity_overlay(
        db_conn, 10, "day", mode="messages", compare_periods=7, utc_offset_hours=0.0
    )
    assert res.smooth_window == 1
    assert res.current_smooth == []


# ── Overlay renderer (the moderator stats panel's image) ──────────────


def _overlay_chart(*, band=True, current=None) -> OverlayChart:
    hours = list(range(24))
    return OverlayChart(
        title="Today vs the last 8 days",
        labels=list(_HOD_LABELS),
        # Unlived hours are None, exactly as the query returns them.
        current=current if current is not None else [float(h) for h in hours[:10]]
        + [None] * 14,
        band_low=[float(h) for h in hours] if band else [],
        band_mid=[float(h) + 1 for h in hours] if band else [],
        band_high=[float(h) + 2 for h in hours] if band else [],
        empty_note="Not enough history to compare against yet.",
    )


def test_render_overlay_panel_stacks_charts_into_one_png():
    """Two overlays, one image — Discord gives an embed a single image slot."""
    png = render_overlay_panel([_overlay_chart(), _overlay_chart()])
    assert png.startswith(PNG_MAGIC)


def test_render_overlay_panel_draws_without_a_band():
    """A young server has no band; the chart still has to render rather than
    raise on the empty series, and says why the band is missing."""
    png = render_overlay_panel([_overlay_chart(band=False)])
    assert png.startswith(PNG_MAGIC)


def test_render_overlay_panel_survives_a_day_with_no_data_at_all():
    """Every hour unlived — a panel posted seconds after local midnight."""
    png = render_overlay_panel([_overlay_chart(current=[None] * 24)])
    assert png.startswith(PNG_MAGIC)


def test_render_overlay_panel_needs_a_chart():
    with pytest.raises(ValueError):
        render_overlay_panel([])
