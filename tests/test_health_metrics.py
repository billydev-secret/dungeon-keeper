"""Unit tests for bot_modules.services.health_metrics.

Pure metric math is the easy half — gini, lorenz, percent helpers. The
harder half exercises ``compute_*`` against a migrated DB seeded with
synthetic messages / xp / sentiment / interactions / audit log entries.
Each test targets a specific branch (empty DB, happy path, signal-driver
edge cases) called out in the task brief.
"""

from __future__ import annotations

import datetime
import warnings

import pytest

from bot_modules.core.bot_exclusion import bot_filter_clause
from bot_modules.core.db_utils import open_db
from bot_modules.services import health_metrics as hm
from bot_modules.services.channel_rollup import build_resolver
from tests.db_template import migrated_db


GUILD = 10


# ── Shared fixture ───────────────────────────────────────────────────


@pytest.fixture
def db_conn(tmp_path):
    path = tmp_path / "hm.db"
    migrated_db(path)
    with open_db(path) as conn:
        yield conn


# ── Seed helpers (keep TS as INT — the SQL does ts % 86400) ──────────


def _seed_message(
    conn,
    *,
    mid: int,
    cid: int,
    aid: int,
    ts: int,
    reply_to: int | None = None,
    content: str = "x",
    guild_id: int = GUILD,
):
    conn.execute(
        "INSERT OR REPLACE INTO messages "
        "(message_id, guild_id, channel_id, author_id, content, reply_to_id, ts)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (mid, guild_id, cid, aid, content, reply_to, int(ts)),
    )


def _seed_xp(conn, *, uid: int, src: str, amount: float, ts: float):
    conn.execute(
        "INSERT INTO xp_events (guild_id, user_id, source, amount, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (GUILD, uid, src, amount, ts),
    )


def _seed_known_user(conn, uid: int, *, is_bot: int = 0, current_member: int = 1):
    conn.execute(
        "INSERT OR REPLACE INTO known_users "
        "(guild_id, user_id, username, display_name, updated_at, is_bot, current_member)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (GUILD, uid, f"u{uid}", f"u{uid}", 0.0, is_bot, current_member),
    )


def _seed_interaction(conn, *, frm: int, to: int, ts: int):
    conn.execute(
        "INSERT INTO user_interactions_log "
        "(guild_id, from_user_id, to_user_id, ts, message_id) VALUES (?,?,?,?,?)",
        (GUILD, frm, to, ts, None),
    )


def _seed_reaction(
    conn, *, reactor: int, author: int, channel: int, mid: int, ts: int
):
    conn.execute(
        "INSERT OR REPLACE INTO reaction_log "
        "(guild_id, reactor_id, author_id, channel_id, message_id, ts)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (GUILD, reactor, author, channel, mid, ts),
    )


def _seed_sentiment(
    conn, *, mid: int, cid: int, sentiment: float, emotion: str | None, ts_now: float
):
    """Score a message the way production does — in *both* places.

    ``events_cog`` (ingest) and ``sentiment_service`` (backfill) write the
    ``messages.sentiment``/``.emotion`` columns and the ``message_sentiment``
    side table together, so a fixture that seeds only one of them is not a
    state the bot can actually produce. ``compute_sentiment`` reads
    ``messages``; the side-table row is kept so the equivalence test below can
    run the old join shape against the same data.
    """
    conn.execute(
        "INSERT OR REPLACE INTO message_sentiment "
        "(message_id, guild_id, channel_id, sentiment, emotion, computed_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (mid, GUILD, cid, sentiment, emotion, ts_now),
    )
    conn.execute(
        "UPDATE messages SET sentiment = ?, emotion = ? WHERE message_id = ?",
        (sentiment, emotion, mid),
    )


def _seed_audit(conn, *, action: str, actor: int, ts: float):
    conn.execute(
        "INSERT INTO audit_log (guild_id, action, actor_id, target_id, extra, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (GUILD, action, actor, None, "{}", ts),
    )


def _seed_warning(conn, *, uid: int, ts: float):
    conn.execute(
        "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (GUILD, uid, 999, "", ts),
    )


def _seed_jail(conn, *, uid: int, ts: float):
    conn.execute(
        "INSERT INTO jails (guild_id, user_id, moderator_id, created_at)"
        " VALUES (?, ?, ?, ?)",
        (GUILD, uid, 999, ts),
    )


# ── Pure math helpers ────────────────────────────────────────────────


def test_gini_empty_is_zero():
    assert hm._gini([]) == 0.0


def test_gini_all_zero_is_zero():
    assert hm._gini([0, 0, 0]) == 0.0


def test_gini_uniform_distribution_is_zero():
    # Equal share → perfect equality → 0
    assert hm._gini([1, 1, 1, 1]) == 0.0


def test_gini_max_inequality_approaches_one():
    # One person has it all → close to (n-1)/n
    g = hm._gini([0, 0, 0, 0, 100])
    assert g >= 0.7  # well above moderate


def test_gini_two_values_known():
    # For [0, 10]: gini = 0.5
    assert hm._gini([0, 10]) == 0.5


def test_lorenz_points_empty_returns_diagonal():
    pts = hm._lorenz_points([])
    assert pts == [{"x": 0.0, "y": 0.0}, {"x": 100.0, "y": 100.0}]


def test_lorenz_points_all_zero_returns_diagonal():
    pts = hm._lorenz_points([0, 0, 0])
    assert pts == [{"x": 0.0, "y": 0.0}, {"x": 100.0, "y": 100.0}]


def test_lorenz_points_endpoints_anchored():
    pts = hm._lorenz_points([1, 2, 3, 4, 5])
    assert pts[0] == {"x": 0.0, "y": 0.0}
    assert pts[-1]["x"] == 100.0
    # Y values should be monotone non-decreasing
    ys = [p["y"] for p in pts]
    assert ys == sorted(ys)


def test_badge_picks_lowest_passing_threshold():
    thresholds = [(0.1, "low"), (0.5, "mid"), (1.0, "high")]
    assert hm._badge(0.05, thresholds) == "low"
    assert hm._badge(0.3, thresholds) == "mid"
    assert hm._badge(0.9, thresholds) == "high"


def test_badge_value_above_all_returns_last():
    thresholds = [(0.1, "low"), (0.5, "mid")]
    assert hm._badge(99, thresholds) == "mid"


def test_badge_empty_thresholds_returns_unknown():
    assert hm._badge(1.0, []) == "unknown"


def test_pct_zero_denominator_is_zero():
    assert hm._pct(5, 0) == 0.0


def test_pct_rounds_to_one_decimal():
    assert hm._pct(1, 3) == 33.3


def test_ts_helper_returns_int_offsets_correctly():
    base = 1_700_000_000.0
    assert hm._ts(0, now=base) == int(base)
    assert hm._ts(1, now=base) == int(base - 86400)
    assert hm._ts(7, now=base) == int(base - 7 * 86400)


# ── compute_dau_mau ──────────────────────────────────────────────────


def test_compute_dau_mau_empty_db_returns_zeros(db_conn):
    out = hm.compute_dau_mau(db_conn, GUILD, now=1_700_000_000.0)
    assert out["dau"] == out["wau"] == out["mau"] == 0
    assert out["dau_mau"] == 0
    assert out["badge"] == "critical"
    assert len(out["sparkline"]) == 30
    assert out["composition"] == {"returning": 0, "reactivated": 0, "new": 0}


def test_compute_dau_mau_counts_authors_in_windows(db_conn):
    now = 1_700_000_000.0
    # 3 distinct authors today, 1 author 5 days ago, 1 author 20 days ago
    _seed_message(db_conn, mid=1, cid=100, aid=1, ts=int(now - 60))
    _seed_message(db_conn, mid=2, cid=100, aid=2, ts=int(now - 120))
    _seed_message(db_conn, mid=3, cid=100, aid=3, ts=int(now - 180))
    _seed_message(db_conn, mid=4, cid=100, aid=4, ts=int(now - 5 * 86400))
    _seed_message(db_conn, mid=5, cid=100, aid=5, ts=int(now - 20 * 86400))
    db_conn.commit()
    out = hm.compute_dau_mau(db_conn, GUILD, now=now, member_count=10)
    assert out["dau"] == 3
    assert out["wau"] == 4
    assert out["mau"] == 5
    assert out["dau_mau"] == 60.0


def test_compute_dau_mau_classifies_today_users(db_conn):
    now = 1_700_000_000.0
    # User 1: brand new (first msg < 7d ago)
    _seed_message(db_conn, mid=1, cid=100, aid=1, ts=int(now - 60))
    # User 2: returning (first msg was 60d ago — too old to be in 90d scan,
    # but they had a message in the previous 8-30 day window)
    _seed_message(db_conn, mid=2, cid=100, aid=2, ts=int(now - 60))
    _seed_message(db_conn, mid=3, cid=100, aid=2, ts=int(now - 20 * 86400))
    # User 3: reactivated (first msg older than 7d, no message in 8-30d)
    _seed_message(db_conn, mid=4, cid=100, aid=3, ts=int(now - 60))
    _seed_message(db_conn, mid=5, cid=100, aid=3, ts=int(now - 80 * 86400))
    db_conn.commit()
    out = hm.compute_dau_mau(db_conn, GUILD, now=now, member_count=10)
    composition = out["composition"]
    assert composition["new"] == 1  # user 1
    assert composition["returning"] == 1  # user 2
    assert composition["reactivated"] == 1  # user 3


# ── compute_heatmap ──────────────────────────────────────────────────


def test_compute_heatmap_empty_db_returns_zero_grid(db_conn):
    out = hm.compute_heatmap(db_conn, GUILD, now=1_700_000_000.0)
    assert len(out["grid"]) == 7
    assert all(len(row) == 24 for row in out["grid"])
    # Every cell == 0 → dead_hours covers all 7*24
    assert out["dead_hours"] == 168


def test_compute_heatmap_records_slot_values(db_conn):
    now = 1_700_000_000.0  # 2023-11-14 22:13:20 UTC (a Tuesday)
    # Seed two messages at the same hour
    _seed_message(db_conn, mid=1, cid=100, aid=1, ts=int(now - 60))
    _seed_message(db_conn, mid=2, cid=100, aid=2, ts=int(now - 60))
    db_conn.commit()
    out = hm.compute_heatmap(db_conn, GUILD, now=now)
    flat = [v for row in out["grid"] for v in row]
    assert max(flat) > 0
    assert len(out["per_channel"]) >= 1


def test_compute_heatmap_shifts_by_utc_offset(db_conn):
    now = 1_700_000_000.0  # 2023-11-14 22:13:20 UTC -> UTC hour-of-day bucket 22
    _seed_message(db_conn, mid=1, cid=100, aid=1, ts=int(now - 60))
    db_conn.commit()

    out_utc = hm.compute_heatmap(db_conn, GUILD, now=now)
    assert out_utc["grid"][1][22] > 0  # Tue (dow=1), 22:00 UTC

    # Eastern (-5h): the same message should land 5 hours earlier, at 17:00.
    out_local = hm.compute_heatmap(db_conn, GUILD, now=now, utc_offset_hours=-5.0)
    assert out_local["grid"][1][17] > 0
    assert out_local["grid"][1][22] == 0


# ── compute_channel_health ───────────────────────────────────────────


def test_compute_channel_health_empty(db_conn):
    out = hm.compute_channel_health(db_conn, GUILD, now=1_700_000_000.0)
    assert out["active_count"] == 0
    assert out["channels"] == []


def test_compute_channel_health_marks_dormant_and_active(db_conn):
    now = 1_700_000_000.0
    # Channel 100: very recent activity
    _seed_message(db_conn, mid=1, cid=100, aid=1, ts=int(now - 60))
    _seed_message(db_conn, mid=2, cid=100, aid=2, ts=int(now - 120))
    _seed_message(db_conn, mid=3, cid=100, aid=3, ts=int(now - 180))
    # Channel 200: dormant (last msg 20 days ago — still in 30d window)
    _seed_message(db_conn, mid=10, cid=200, aid=1, ts=int(now - 20 * 86400))
    db_conn.commit()
    out = hm.compute_channel_health(db_conn, GUILD, now=now, nsfw_channel_ids=[200])
    statuses = {int(c["channel_id"]): c["status"] for c in out["channels"]}
    assert statuses[100] in {"flagged", "healthy"}
    # 20d ago > 14 days → dormant
    assert statuses[200] == "dormant"
    # The nsfw flag should be propagated
    nsfw = {int(c["channel_id"]): c["is_nsfw"] for c in out["channels"]}
    assert nsfw[200] is True
    assert nsfw[100] is False


# ── Thread attribution (see services/channel_rollup) ─────────────────
#
# A message posted in a thread carries the thread's own id, so without the
# resolver every thread scored as a channel of its own and its parent read as
# quieter than it was.


def _seed_thread(conn, *, thread_id: int, parent_id: int):
    conn.execute(
        "INSERT OR REPLACE INTO known_channels "
        "(guild_id, channel_id, channel_name, updated_at, parent_id, is_thread)"
        " VALUES (?, ?, '', 0, ?, 1)",
        (GUILD, thread_id, parent_id),
    )


def test_channel_health_counts_thread_messages_toward_the_parent(db_conn):
    now = 1_700_000_000.0
    _seed_message(db_conn, mid=1, cid=100, aid=1, ts=int(now - 60))
    _seed_message(db_conn, mid=2, cid=101, aid=2, ts=int(now - 120))
    _seed_message(db_conn, mid=3, cid=101, aid=3, ts=int(now - 180))
    _seed_thread(db_conn, thread_id=101, parent_id=100)
    db_conn.commit()

    out = hm.compute_channel_health(
        db_conn,
        GUILD,
        now=now,
        resolver=build_resolver(db_conn, GUILD, live_channel_ids=[100]),
    )

    rows = {int(c["channel_id"]): c for c in out["channels"]}
    assert set(rows) == {100}, "the thread must not appear as a channel"
    assert rows[100]["msgs_per_day"] == round(3 / 30, 1)


def test_channel_health_does_not_double_count_an_author_across_a_thread(db_conn):
    # Author 1 posts in both the channel and its thread: one unique user, not
    # two. Summing the per-id distinct counts would say two.
    now = 1_700_000_000.0
    _seed_message(db_conn, mid=1, cid=100, aid=1, ts=int(now - 60))
    _seed_message(db_conn, mid=2, cid=101, aid=1, ts=int(now - 120))
    _seed_thread(db_conn, thread_id=101, parent_id=100)
    db_conn.commit()

    out = hm.compute_channel_health(
        db_conn,
        GUILD,
        now=now,
        resolver=build_resolver(db_conn, GUILD, live_channel_ids=[100]),
    )

    assert {int(c["channel_id"]) for c in out["channels"]} == {100}
    assert out["channels"][0]["msgs_per_day"] == round(2 / 30, 1)
    assert out["channels"][0]["unique_weekly_users"] == 1


def test_a_channel_alive_only_in_its_threads_is_not_dormant(db_conn):
    # All the recent talk happened in the thread. Taking the parent's own last
    # message would age it out at 14 days and call a live channel dormant.
    now = 1_700_000_000.0
    _seed_message(db_conn, mid=1, cid=100, aid=1, ts=int(now - 25 * 86400))
    _seed_message(db_conn, mid=2, cid=101, aid=2, ts=int(now - 60))
    _seed_thread(db_conn, thread_id=101, parent_id=100)
    db_conn.commit()

    out = hm.compute_channel_health(
        db_conn,
        GUILD,
        now=now,
        resolver=build_resolver(db_conn, GUILD, live_channel_ids=[100]),
    )

    assert out["channels"][0]["status"] in {"healthy", "flagged"}


def test_channel_health_drops_a_channel_the_guild_no_longer_has(db_conn):
    now = 1_700_000_000.0
    _seed_message(db_conn, mid=1, cid=100, aid=1, ts=int(now - 60))
    _seed_message(db_conn, mid=2, cid=999, aid=1, ts=int(now - 60))
    db_conn.commit()

    out = hm.compute_channel_health(
        db_conn,
        GUILD,
        now=now,
        resolver=build_resolver(db_conn, GUILD, live_channel_ids=[100]),
    )

    assert {int(c["channel_id"]) for c in out["channels"]} == {100}


def test_heatmap_folds_thread_hours_into_the_parents_grid(db_conn):
    now = 1_700_000_000.0
    ts = int(now - 3600)
    _seed_message(db_conn, mid=1, cid=100, aid=1, ts=ts)
    _seed_message(db_conn, mid=2, cid=101, aid=2, ts=ts)
    _seed_thread(db_conn, thread_id=101, parent_id=100)
    db_conn.commit()

    out = hm.compute_heatmap(
        db_conn,
        GUILD,
        now=now,
        resolver=build_resolver(db_conn, GUILD, live_channel_ids=[100]),
    )

    per_channel = {int(c["channel_id"]): c for c in out["per_channel"]}
    assert set(per_channel) == {100}
    # Both messages land in the same slot, so the parent's grid carries both —
    # and carries exactly what the server-wide grid does, since between them
    # those two messages are the whole guild's traffic.
    parent_peak = max(max(row) for row in per_channel[100]["grid"])
    assert parent_peak == max(max(row) for row in out["grid"])
    assert parent_peak > 0


# ── compute_gini ─────────────────────────────────────────────────────


def test_compute_gini_empty_db_zero(db_conn):
    out = hm.compute_gini(db_conn, GUILD, now=1_700_000_000.0)
    assert out["gini"] == 0
    assert out["top5_share"] == 0
    assert out["top10_share"] == 0
    assert out["palma"] == 0


def test_compute_gini_populated_distribution(db_conn):
    now = 1_700_000_000.0
    # 1 power user with 300 messages (>50/week ≈ 70/wk), 4 light users with 1 each.
    # ``compute_gini`` divides 30-day count by 4.3 to estimate weekly volume —
    # the >50/wk "power" tier needs >215 messages over 30d.
    for i in range(300):
        _seed_message(db_conn, mid=100 + i, cid=1, aid=1, ts=int(now - 60 - i))
    for j, uid in enumerate((2, 3, 4, 5)):
        _seed_message(db_conn, mid=500 + j, cid=1, aid=uid, ts=int(now - 60))
    db_conn.commit()
    out = hm.compute_gini(db_conn, GUILD, now=now)
    assert out["gini"] > 0
    # The lone power user should account for the bulk of messages
    assert out["top5_share"] > 50
    # Tiers should sum to total users (5)
    assert sum(out["tiers"].values()) == 5
    assert out["tiers"]["power"] >= 1


def test_compute_gini_counts_silent_members_as_lurkers(db_conn):
    """A lurker posts nothing, so they are absent from the message rows.

    The tier loop reads ``SELECT … GROUP BY author_id``, where every row has
    ``cnt >= 1`` — the old ``wk == 0`` branch could never be taken and the
    Lurker slice was permanently zero. Counting lurkers means reading
    membership and subtracting the people who posted.
    """
    now = 1_700_000_000.0
    _seed_known_user(db_conn, 1)  # posts
    _seed_known_user(db_conn, 2)  # silent → lurker
    _seed_known_user(db_conn, 3)  # silent → lurker
    _seed_known_user(db_conn, 4, current_member=0)  # left the guild → not a lurker
    _seed_known_user(db_conn, 99, is_bot=1)  # bot → excluded by default
    _seed_message(db_conn, mid=1, cid=1, aid=1, ts=int(now) - 60)
    db_conn.commit()

    out = hm.compute_gini(db_conn, GUILD, now=now)
    assert out["tiers"]["lurker"] == 2
    assert out["tiers"]["light"] == 1
    assert out["posters"] == 1
    # The bot toggle reaches the lurker count too: opted in, the silent bot is
    # a silent member like any other.
    opted_in = hm.compute_gini(db_conn, GUILD, now=now, include_bots=True)
    assert opted_in["tiers"]["lurker"] == 3


def test_compute_gini_reports_poster_count_for_the_empty_state(db_conn):
    """``posters`` is the honest "nothing to measure" signal.

    ``tiers`` is a five-key dict that is never empty, so the panel cannot ask
    it whether anyone posted; it asks this instead.
    """
    _seed_known_user(db_conn, 2)
    db_conn.commit()
    out = hm.compute_gini(db_conn, GUILD, now=1_700_000_000.0)
    assert out["posters"] == 0
    assert out["total_messages"] == 0
    # A silent server still has members — an empty distribution is not an
    # empty guild.
    assert out["tiers"]["lurker"] == 1


# ── compute_sentiment ────────────────────────────────────────────────


def test_compute_sentiment_empty(db_conn):
    out = hm.compute_sentiment(db_conn, GUILD, now=1_700_000_000.0)
    assert out["avg_sentiment"] == 0
    assert out["scored_count"] == 0
    assert out["emotions"] == {}
    assert len(out["sparkline"]) == 30


def test_compute_sentiment_average_and_emotions(db_conn):
    now = 1_700_000_000.0
    for mid in (1, 2, 3):
        _seed_message(db_conn, mid=mid, cid=100, aid=mid, ts=int(now - 60))
    _seed_sentiment(db_conn, mid=1, cid=100, sentiment=0.8, emotion="joy", ts_now=now)
    _seed_sentiment(db_conn, mid=2, cid=100, sentiment=0.6, emotion="joy", ts_now=now)
    _seed_sentiment(db_conn, mid=3, cid=100, sentiment=-0.4, emotion="anger", ts_now=now)
    db_conn.commit()
    out = hm.compute_sentiment(db_conn, GUILD, now=now)
    # avg = (0.8+0.6-0.4)/3 = 0.333
    assert abs(out["avg_sentiment"] - 0.333) < 0.01
    assert out["scored_count"] == 3
    # Two of three are positive
    assert out["pos_neg_ratio"] == 2.0
    assert "joy" in out["emotions"] and "anger" in out["emotions"]


# ── compute_sentiment reads `messages`, not the side table ───────────
#
# The dashboard's tiles/feed/outlier queries moved to ``messages`` in
# b9b65c83 after equivalence was verified read-only against prod (502,051 rows
# on each side, zero orphans either direction, zero sentiment or emotion
# mismatches, ``idx_messages_sentiment (guild_id, sentiment)`` present).
# ``compute_sentiment`` was the last reader keeping ``message_sentiment`` on
# the hot path. The property that matters is that the numbers did not move, so
# the old query shape is kept verbatim below and asserted against.


def _legacy_compute_sentiment(conn, guild_id, *, now, include_bots=False) -> dict:
    """The pre-switch query shape: join ``message_sentiment`` to ``messages``.

    Verbatim (modulo the ``conn``/params plumbing) so the assertion is
    "identical figures", not "plausible figures".
    """
    thirty_days_ago = hm._ts(30, now=now)
    bot_clause, bot_params = bot_filter_clause(
        guild_id, column="m.author_id", include_bots=include_bots
    )

    row = conn.execute(
        f"SELECT AVG(ms.sentiment) AS avg_s, COUNT(*) AS cnt "
        f"FROM message_sentiment ms "
        f"JOIN messages m ON ms.message_id = m.message_id "
        f"WHERE ms.guild_id=? AND m.ts>=?{bot_clause}",
        (guild_id, thirty_days_ago, *bot_params),
    ).fetchone()
    avg_sentiment = round(row["avg_s"], 3) if row["avg_s"] is not None else 0

    emotion_rows = conn.execute(
        f"SELECT ms.emotion, COUNT(*) AS cnt "
        f"FROM message_sentiment ms "
        f"JOIN messages m ON ms.message_id = m.message_id "
        f"WHERE ms.guild_id=? AND m.ts>=? AND ms.emotion IS NOT NULL{bot_clause} "
        f"GROUP BY ms.emotion",
        (guild_id, thirty_days_ago, *bot_params),
    ).fetchall()
    emotions = {r["emotion"]: r["cnt"] for r in emotion_rows}
    emotion_total = sum(emotions.values()) or 1

    pos_count = conn.execute(
        f"SELECT COUNT(*) FROM message_sentiment ms "
        f"JOIN messages m ON ms.message_id = m.message_id "
        f"WHERE ms.guild_id=? AND m.ts>=? AND ms.sentiment>0.05{bot_clause}",
        (guild_id, thirty_days_ago, *bot_params),
    ).fetchone()[0]
    neg_count = conn.execute(
        f"SELECT COUNT(*) FROM message_sentiment ms "
        f"JOIN messages m ON ms.message_id = m.message_id "
        f"WHERE ms.guild_id=? AND m.ts>=? AND ms.sentiment<-0.05{bot_clause}",
        (guild_id, thirty_days_ago, *bot_params),
    ).fetchone()[0]

    sparkline = []
    for d in range(29, -1, -1):
        r = conn.execute(
            f"SELECT AVG(ms.sentiment) AS avg_s "
            f"FROM message_sentiment ms "
            f"JOIN messages m ON ms.message_id = m.message_id "
            f"WHERE ms.guild_id=? AND m.ts>=? AND m.ts<?{bot_clause}",
            (guild_id, hm._ts(d + 1, now=now), hm._ts(d, now=now), *bot_params),
        ).fetchone()
        sparkline.append(round(r["avg_s"], 3) if r["avg_s"] is not None else 0)

    spike_rows = conn.execute(
        f"""SELECT CAST(m.ts / 300 AS INTEGER) * 300 AS window_start,
                  AVG(ms.sentiment) AS avg_s,
                  COUNT(*) AS cnt
           FROM message_sentiment ms
           JOIN messages m ON ms.message_id = m.message_id
           WHERE ms.guild_id=? AND m.ts>=?{bot_clause}
           GROUP BY CAST(m.ts / 300 AS INTEGER)
           HAVING avg_s < -0.3 AND cnt >= 3
           ORDER BY window_start DESC
           LIMIT 20""",
        (guild_id, hm._ts(7, now=now), *bot_params),
    ).fetchall()

    ch_rows = conn.execute(
        f"""SELECT ms.channel_id, AVG(ms.sentiment) AS avg_s, COUNT(*) AS cnt
           FROM message_sentiment ms
           JOIN messages m ON ms.message_id = m.message_id
           WHERE ms.guild_id=? AND m.ts>=?{bot_clause}
           GROUP BY ms.channel_id
           ORDER BY avg_s DESC""",
        (guild_id, thirty_days_ago, *bot_params),
    ).fetchall()

    return {
        "avg_sentiment": avg_sentiment,
        "badge": hm._badge(
            avg_sentiment,
            [(-0.1, "critical"), (0.0, "needs_work"), (0.2, "healthy"), (1.0, "excellent")],
        ),
        "scored_count": row["cnt"],
        "emotions": {
            k: round(v / emotion_total * 100, 1) for k, v in emotions.items()
        },
        "pos_neg_ratio": round(pos_count / neg_count, 1) if neg_count else 0,
        "sparkline": sparkline,
        "spikes_7d": len(spike_rows),
        "spike_log": [
            {
                "timestamp": r["window_start"],
                "avg_sentiment": round(r["avg_s"], 3),
                "msg_count": r["cnt"],
            }
            for r in spike_rows
        ],
        "per_channel": [
            {
                "channel_id": str(r["channel_id"]),
                "avg_sentiment": round(r["avg_s"], 3),
                "count": r["cnt"],
            }
            for r in ch_rows[:20]
        ],
    }


def _seed_mixed_sentiment_corpus(conn, now: float) -> None:
    """Scored + unscored traffic across two channels, plus a negative spike."""
    recent = int(now) - 3600
    for uid in (1, 2, 99):
        _seed_known_user(conn, uid, is_bot=1 if uid == 99 else 0)

    # Channel 100: a positive human, a negative human, a negative bot.
    _seed_message(conn, mid=1, cid=100, aid=1, ts=recent)
    _seed_sentiment(conn, mid=1, cid=100, sentiment=0.8, emotion="joy", ts_now=now)
    _seed_message(conn, mid=2, cid=100, aid=2, ts=recent)
    _seed_sentiment(conn, mid=2, cid=100, sentiment=-0.6, emotion="anger", ts_now=now)
    _seed_message(conn, mid=3, cid=100, aid=99, ts=recent)
    _seed_sentiment(conn, mid=3, cid=100, sentiment=-0.9, emotion="anger", ts_now=now)

    # Channel 200: a neutral score and one with no emotion label.
    _seed_message(conn, mid=4, cid=200, aid=1, ts=recent)
    _seed_sentiment(conn, mid=4, cid=200, sentiment=0.02, emotion=None, ts_now=now)
    _seed_message(conn, mid=5, cid=200, aid=2, ts=recent - 86400 * 3)
    _seed_sentiment(conn, mid=5, cid=200, sentiment=0.4, emotion="joy", ts_now=now)

    # A 5-minute window of three strongly negative messages — a spike.
    for i, mid in enumerate((10, 11, 12)):
        _seed_message(conn, mid=mid, cid=100, aid=1, ts=recent - 7200 + i)
        _seed_sentiment(
            conn, mid=mid, cid=100, sentiment=-0.7, emotion="anger", ts_now=now
        )

    # Unscored traffic: real messages that VADER never got to. These have no
    # ``message_sentiment`` row, so the join excluded them implicitly; reading
    # ``messages`` has to exclude them explicitly.
    for mid in (20, 21, 22, 23):
        _seed_message(conn, mid=mid, cid=100, aid=1, ts=recent)
    conn.commit()


@pytest.mark.parametrize("include_bots", [False, True])
def test_compute_sentiment_matches_the_message_sentiment_join(db_conn, include_bots):
    """Read-path swap, not a behaviour change: every figure must be identical."""
    now = 1_700_000_000.0
    _seed_mixed_sentiment_corpus(db_conn, now)

    new = hm.compute_sentiment(db_conn, GUILD, now=now, include_bots=include_bots)
    old = _legacy_compute_sentiment(
        db_conn, GUILD, now=now, include_bots=include_bots
    )
    assert new == old


def test_compute_sentiment_ignores_unscored_messages(db_conn):
    """Without an explicit IS NOT NULL the four unscored rows join the counts.

    ``AVG`` skips NULLs on its own, so the mean survives — but ``COUNT(*)``
    does not, and ``scored_count`` / ``per_channel.count`` / the spike
    ``HAVING cnt >= 3`` all ride on it.
    """
    now = 1_700_000_000.0
    recent = int(now) - 3600
    _seed_known_user(db_conn, 1)
    _seed_message(db_conn, mid=1, cid=100, aid=1, ts=recent)
    _seed_sentiment(db_conn, mid=1, cid=100, sentiment=0.8, emotion="joy", ts_now=now)
    for mid in (2, 3, 4, 5):
        _seed_message(db_conn, mid=mid, cid=100, aid=1, ts=recent)
    db_conn.commit()

    out = hm.compute_sentiment(db_conn, GUILD, now=now)
    assert out["scored_count"] == 1, "unscored messages were counted as scored"
    assert out["avg_sentiment"] == 0.8
    assert out["per_channel"] == [
        {"channel_id": "100", "avg_sentiment": 0.8, "count": 1}
    ]


def test_compute_sentiment_reads_messages_without_the_side_table(db_conn):
    """Rows only in ``messages``: under the old join this came back empty."""
    now = 1_700_000_000.0
    recent = int(now) - 3600
    _seed_known_user(db_conn, 1)
    for mid, score in ((1, 0.9), (2, 0.5)):
        _seed_message(db_conn, mid=mid, cid=100, aid=1, ts=recent)
        db_conn.execute(
            "UPDATE messages SET sentiment = ?, emotion = 'joy' WHERE message_id = ?",
            (score, mid),
        )
    db_conn.commit()

    assert db_conn.execute("SELECT COUNT(*) FROM message_sentiment").fetchone()[0] == 0
    out = hm.compute_sentiment(db_conn, GUILD, now=now)
    assert out["scored_count"] == 2
    assert out["avg_sentiment"] == 0.7
    assert out["emotions"] == {"joy": 100.0}


def test_compute_sentiment_does_not_query_the_side_table(db_conn):
    """No statement it issues may name ``message_sentiment``."""
    now = 1_700_000_000.0
    _seed_mixed_sentiment_corpus(db_conn, now)

    sink: list[str] = []
    db_conn.set_trace_callback(sink.append)
    try:
        hm.compute_sentiment(db_conn, GUILD, now=now)
    finally:
        db_conn.set_trace_callback(None)
    assert sink, "no SQL was traced"
    assert not [s for s in sink if "message_sentiment" in s]


# ── datetime: UTC semantics at the sites that dropped utcfromtimestamp ─


def test_gini_history_labels_are_utc_and_unchanged(db_conn):
    """``utcfromtimestamp`` -> aware ``fromtimestamp(ts, UTC)``.

    The value is only ever strftime'd into a "Mon D" label, never compared
    against another datetime, so the aware form must render byte-identically.
    The label is UTC by design (it is not shifted by the guild's tz offset) —
    that was true before the swap and stays true after it.
    """
    now = 1_700_000_000.0  # 2023-11-14 22:13:20 UTC
    out = hm.compute_gini(db_conn, GUILD, now=now)

    labels = [h["label"] for h in out["gini_history"]]
    assert len(labels) == 12
    expected = [
        datetime.datetime.fromtimestamp(
            hm._ts((w + 1) * 7, now=now), datetime.UTC
        ).strftime("%b ")
        + str(
            datetime.datetime.fromtimestamp(
                hm._ts((w + 1) * 7, now=now), datetime.UTC
            ).day
        )
        for w in range(11, -1, -1)
    ]
    assert labels == expected
    # Pinned literally so a silent tz shift can't move both sides together.
    assert labels[0] == "Aug 22"
    assert labels[-1] == "Nov 7"


def test_compute_gini_emits_no_datetime_deprecation_warning(db_conn):
    """``datetime.utcfromtimestamp`` is deprecated and slated for removal."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        hm.compute_gini(db_conn, GUILD, now=1_700_000_000.0)
    offenders = [
        w for w in caught if issubclass(w.category, DeprecationWarning)
        and "utc" in str(w.message).lower()
    ]
    assert not offenders, [str(w.message) for w in offenders]


# ── compute_newcomer_funnel ──────────────────────────────────────────


def test_compute_newcomer_funnel_no_joins(db_conn):
    out = hm.compute_newcomer_funnel(db_conn, GUILD, now=1_700_000_000.0)
    assert out["activation_rate"] == 0
    assert out["badge"] == "no_data"
    assert out["funnel"]["joined"] == 0


def test_compute_newcomer_funnel_with_joiners(db_conn):
    now = 1_700_000_000.0
    join_ts = now - 20 * 86400  # joined 20 days ago → eligible for D7

    # User 1: joined and posted, hit 3 channels, returned after 7d
    _seed_message(db_conn, mid=1, cid=100, aid=1, ts=int(join_ts + 3600))
    _seed_message(db_conn, mid=2, cid=200, aid=1, ts=int(join_ts + 7200))
    _seed_message(db_conn, mid=3, cid=300, aid=1, ts=int(join_ts + 10800))
    _seed_message(db_conn, mid=4, cid=100, aid=1, ts=int(join_ts + 10 * 86400))  # D7+
    # Plus a reply from someone else to user 1's first message
    _seed_message(
        db_conn,
        mid=99,
        cid=100,
        aid=999,
        ts=int(join_ts + 7200),
        reply_to=1,
    )
    db_conn.commit()
    out = hm.compute_newcomer_funnel(
        db_conn, GUILD, now=now, recent_join_ids={1: join_ts}
    )
    funnel = out["funnel"]
    assert funnel["joined"] == 1
    assert funnel["first_message"] == 1
    assert funnel["first_reply"] == 1
    assert funnel["three_channels"] == 1
    assert funnel["d7_return"] == 1
    assert out["activation_rate"] == 100.0
    assert out["time_to_first_msg"]["median_hours"] >= 0


# ── compute_cohort_retention ─────────────────────────────────────────


def test_compute_cohort_retention_empty(db_conn):
    out = hm.compute_cohort_retention(db_conn, GUILD, now=1_700_000_000.0)
    assert out["badge"] == "no_data"
    assert out["cohorts"] == []


def test_compute_cohort_retention_basic(db_conn):
    now = 1_700_000_000.0
    join_ts = now - 100 * 86400
    # Same week cohort: 2 users
    join_times = {1: join_ts, 2: join_ts + 86400}
    # User 1 returns at D7+
    _seed_message(db_conn, mid=1, cid=100, aid=1, ts=int(join_ts + 1))
    _seed_message(db_conn, mid=2, cid=100, aid=1, ts=int(join_ts + 8 * 86400))
    # User 2 never returns
    _seed_message(db_conn, mid=3, cid=100, aid=2, ts=int(join_ts + 86400))
    db_conn.commit()
    out = hm.compute_cohort_retention(
        db_conn, GUILD, now=now, join_times=dict(join_times)
    )
    assert len(out["cohorts"]) >= 1
    assert out["latest_cohort_size"] >= 1






# ── compute_mod_workload ─────────────────────────────────────────────


def test_compute_mod_workload_empty(db_conn):
    out = hm.compute_mod_workload(db_conn, GUILD, now=1_700_000_000.0)
    assert out["total_actions_7d"] == 0
    assert out["mod_actions"] == []


def test_compute_mod_workload_counts_audit_and_messages(db_conn):
    now = 1_700_000_000.0
    # Audit actions
    _seed_audit(db_conn, action="kick", actor=10, ts=now - 60)
    _seed_audit(db_conn, action="ban", actor=10, ts=now - 120)
    _seed_audit(db_conn, action="warn", actor=20, ts=now - 180)
    # Warning → jail escalation (within 14d window)
    _seed_warning(db_conn, uid=999, ts=now - 10 * 86400)
    _seed_jail(db_conn, uid=999, ts=now - 5 * 86400)
    db_conn.commit()
    out = hm.compute_mod_workload(db_conn, GUILD, now=now, mod_ids=[10, 20])
    actors = {int(m["user_id"]) for m in out["mod_actions"]}
    assert {10, 20}.issubset(actors)
    # Escalation rate: 1 warned, 1 escalated → 100%
    assert out["escalation_rate"] == 100.0


def test_compute_mod_workload_excludes_voice_master_self_service(db_conn):
    """A mod using their own Voice Control channel isn't moderation work."""
    now = 1_700_000_000.0
    _seed_audit(db_conn, action="kick", actor=10, ts=now - 60)
    _seed_audit(db_conn, action="vm_channel_create", actor=10, ts=now - 90)
    _seed_audit(db_conn, action="vm_claim", actor=10, ts=now - 120)
    db_conn.commit()
    out = hm.compute_mod_workload(db_conn, GUILD, now=now, mod_ids=[10])
    assert out["total_actions_7d"] == 1
    assert out["mod_actions"][0]["actions"] == 1
    assert not any(t["action"].startswith("vm_") for t in out["action_types"])


# ── compute_composite_health ─────────────────────────────────────────


def test_compute_composite_health_perfect_inputs():
    out = hm.compute_composite_health(
        None,  # type: ignore[arg-type]
        GUILD,
        dau_mau_data={"dau_mau": 40},
        gini_data={"gini": 0.3},
        social_data={"clustering_coefficient": 0.5},
        sentiment_data={"avg_sentiment": 0.5},
        retention_data={"d7": 80},
        heatmap_data={"dead_hours": 0},
    )
    assert out["score"] >= 80
    assert out["badge"] == "excellent"


def test_compute_composite_health_all_defaults_low():
    out = hm.compute_composite_health(None, GUILD)  # type: ignore[arg-type]
    # With nothing provided, distribution & engagement & retention & sentiment all stuck at floor
    assert out["score"] >= 0
    assert "dimensions" in out and len(out["dimensions"]) == 6


def test_compute_composite_health_recommendations_show_weakest():
    out = hm.compute_composite_health(
        None,  # type: ignore[arg-type]
        GUILD,
        dau_mau_data={"dau_mau": 0},
        gini_data={"gini": 0.99},
        social_data={"clustering_coefficient": 0.0},
        sentiment_data={"avg_sentiment": -0.5},
        retention_data={"d7": 0},
        heatmap_data={"dead_hours": 168},
    )
    # Three weakest recommended actions surfaced
    assert len(out["recommendations"]) == 3
    # Each carries an estimated_impact ≥ 0
    for rec in out["recommendations"]:
        assert rec["estimated_impact"] >= 0


# ── compute_mod_engagement ───────────────────────────────────────────


def test_compute_mod_engagement_no_mods_returns_empty(db_conn):
    out = hm.compute_mod_engagement(db_conn, GUILD, mod_ids=None)
    assert out["mods"] == []
    assert out["total_public_messages"] == 0


def test_compute_mod_engagement_aggregates_per_mod(db_conn):
    now = 1_700_000_000.0
    # Mod 10 sends 2 messages in public channel 500
    _seed_message(db_conn, mid=1, cid=500, aid=10, ts=int(now - 60))
    _seed_message(db_conn, mid=2, cid=500, aid=10, ts=int(now - 120))
    # User 99 replies (counts as reply received)
    _seed_message(db_conn, mid=3, cid=500, aid=99, ts=int(now - 30), reply_to=1)
    _seed_interaction(db_conn, frm=99, to=10, ts=int(now - 30))
    _seed_reaction(db_conn, reactor=99, author=10, channel=500, mid=2, ts=int(now - 100))
    db_conn.commit()
    out = hm.compute_mod_engagement(
        db_conn, GUILD, mod_ids=[10], now=now, days=7
    )
    assert len(out["mods"]) == 1
    mod_row = out["mods"][0]
    assert mod_row["public_messages"] == 2
    assert mod_row["reactions_received"] == 1
    assert mod_row["replies_received"] == 1
    # engagement_rate = (1 react + 1 reply) / 2 msgs = 1.0
    assert mod_row["engagement_rate"] == 1.0


def test_compute_mod_engagement_with_newcomer_touchpoints(db_conn):
    now = 1_700_000_000.0
    newcomer_ts = now - 5 * 86400  # within 30d window
    _seed_message(db_conn, mid=1, cid=500, aid=10, ts=int(now - 60))
    _seed_interaction(db_conn, frm=10, to=42, ts=int(now - 60))
    db_conn.commit()
    out = hm.compute_mod_engagement(
        db_conn,
        GUILD,
        mod_ids=[10],
        now=now,
        recent_joins={42: newcomer_ts},
    )
    assert out["mods"][0]["newcomer_touchpoints"] == 1
    assert out["total_newcomer_touchpoints"] == 1


# ── compute_social_graph — light smoke test ─────────────────────────


def test_compute_social_graph_empty_returns_keys(db_conn):
    out = hm.compute_social_graph(db_conn, GUILD, now=1_700_000_000.0)
    assert "sfw_nsfw_bridge_pct" in out
    # graph_metrics output keys should be present
    assert isinstance(out, dict)


def test_compute_social_graph_excludes_bot_interactions(db_conn):
    """A bot on either endpoint must not inflate the social graph's node count."""
    now = 1_700_000_000.0
    recent = int(now) - 86400  # within the 30-day window
    # Two humans interacting, plus everyone talking at a bot.
    _seed_known_user(db_conn, 1)
    _seed_known_user(db_conn, 2)
    _seed_known_user(db_conn, 99, is_bot=1)
    _seed_interaction(db_conn, frm=1, to=2, ts=recent)
    _seed_interaction(db_conn, frm=1, to=99, ts=recent)
    _seed_interaction(db_conn, frm=99, to=2, ts=recent)

    out = hm.compute_social_graph(db_conn, GUILD, now=now)
    # Only humans 1 and 2 form the graph; the bot's edges are dropped.
    assert out["node_count"] == 2
    assert out["edge_count"] == 1


# ── Bot exclusion (default) across the metric tiles ──────────────────
#
# Bots are ~21% of stored message volume in prod, so every message-volume
# tile counted them until now. Each test seeds one human and one bot in the
# same window and asserts the bot is invisible by default but returns under
# ``include_bots=True``.


@pytest.fixture
def bot_and_human(db_conn):
    """One human (id 1) and one bot (id 99), each with messages in-window."""
    now = 1_700_000_000.0
    recent = int(now) - 3600
    _seed_known_user(db_conn, 1)
    _seed_known_user(db_conn, 99, is_bot=1)
    _seed_message(db_conn, mid=1, cid=100, aid=1, ts=recent)
    # The bot out-posts the human 3:1, the shape of the #register problem.
    for i, mid in enumerate((2, 3, 4)):
        _seed_message(db_conn, mid=mid, cid=100, aid=99, ts=recent - i)
    return db_conn, now


def test_dau_mau_excludes_bots_by_default(bot_and_human):
    conn, now = bot_and_human
    assert hm.compute_dau_mau(conn, GUILD, now=now)["dau"] == 1


def test_dau_mau_counts_bots_when_opted_in(bot_and_human):
    conn, now = bot_and_human
    out = hm.compute_dau_mau(conn, GUILD, now=now, include_bots=True)
    assert out["dau"] == 2


def test_channel_health_excludes_bots_by_default(bot_and_human):
    """The #register case: a channel that is 75% bot must report only humans."""
    conn, now = bot_and_human
    out = hm.compute_channel_health(conn, GUILD, now=now)
    ch = {c["channel_id"]: c for c in out["channels"]}
    assert ch["100"]["unique_weekly_users"] == 1


def test_channel_health_counts_bots_when_opted_in(bot_and_human):
    conn, now = bot_and_human
    out = hm.compute_channel_health(conn, GUILD, now=now, include_bots=True)
    ch = {c["channel_id"]: c for c in out["channels"]}
    assert ch["100"]["unique_weekly_users"] == 2
    # The bot's 3 messages triple the channel's throughput.
    quiet = hm.compute_channel_health(conn, GUILD, now=now)
    quiet_ch = {c["channel_id"]: c for c in quiet["channels"]}
    assert ch["100"]["msgs_per_day"] > quiet_ch["100"]["msgs_per_day"]


def test_gini_ignores_bot_volume(bot_and_human):
    """A lone high-volume bot must not read as participation inequality.

    With the bot counted there are two very unequal authors (3 vs 1); with it
    excluded there is a single human, which is perfect equality (gini 0).
    """
    conn, now = bot_and_human
    assert hm.compute_gini(conn, GUILD, now=now)["gini"] == 0.0
    assert hm.compute_gini(conn, GUILD, now=now, include_bots=True)["gini"] > 0.0


def test_heatmap_excludes_bots_by_default(bot_and_human):
    conn, now = bot_and_human
    quiet = hm.compute_heatmap(conn, GUILD, now=now)
    busy = hm.compute_heatmap(conn, GUILD, now=now, include_bots=True)
    assert sum(map(sum, busy["grid"])) > sum(map(sum, quiet["grid"]))


def test_cohort_retention_drops_bot_cohorts(bot_and_human):
    """A bot's first message must not seed a retention cohort."""
    conn, now = bot_and_human
    out = hm.compute_cohort_retention(conn, GUILD, now=now)
    with_bots = hm.compute_cohort_retention(conn, GUILD, now=now, include_bots=True)
    assert out["latest_cohort_size"] == 1
    assert with_bots["latest_cohort_size"] == 2


def test_sentiment_excludes_bot_authored_scores(db_conn):
    """Bot output is VADER-scored like any text; it must not move the average."""
    now = 1_700_000_000.0
    recent = int(now) - 3600
    _seed_known_user(db_conn, 1)
    _seed_known_user(db_conn, 99, is_bot=1)
    _seed_message(db_conn, mid=1, cid=100, aid=1, ts=recent)
    _seed_message(db_conn, mid=2, cid=100, aid=99, ts=recent)
    for mid, score in ((1, 1.0), (2, -1.0)):
        _seed_sentiment(
            db_conn, mid=mid, cid=100, sentiment=score, emotion="joy", ts_now=recent
        )

    # Human-only: the single +1.0 message. With the bot: +1.0 and -1.0 average 0.
    assert hm.compute_sentiment(db_conn, GUILD, now=now)["avg_sentiment"] == 1.0
    assert (
        hm.compute_sentiment(db_conn, GUILD, now=now, include_bots=True)[
            "avg_sentiment"
        ]
        == 0.0
    )


def test_newcomer_funnel_ignores_bot_replies(db_conn):
    """A welcome bot auto-replying is not the community responding."""
    now = 1_700_000_000.0
    joined = int(now) - 7200
    _seed_known_user(db_conn, 1)
    _seed_known_user(db_conn, 99, is_bot=1)
    _seed_message(db_conn, mid=1, cid=100, aid=1, ts=joined + 60)
    # Only a bot replies to the newcomer's first message.
    _seed_message(db_conn, mid=2, cid=100, aid=99, ts=joined + 120, reply_to=1)

    out = hm.compute_newcomer_funnel(
        db_conn, GUILD, now=now, recent_join_ids={1: float(joined)}
    )
    assert out["funnel"]["first_message"] == 1
    assert out["funnel"]["first_reply"] == 0

    with_bots = hm.compute_newcomer_funnel(
        db_conn,
        GUILD,
        now=now,
        recent_join_ids={1: float(joined)},
        include_bots=True,
    )
    assert with_bots["funnel"]["first_reply"] == 1


def test_newcomer_funnel_does_not_treat_a_bot_as_a_newcomer(db_conn):
    """A bot that joined in the window is not a newcomer to activate."""
    now = 1_700_000_000.0
    joined = int(now) - 7200
    _seed_known_user(db_conn, 99, is_bot=1)
    _seed_message(db_conn, mid=1, cid=100, aid=99, ts=joined + 60)

    out = hm.compute_newcomer_funnel(
        db_conn, GUILD, now=now, recent_join_ids={99: float(joined)}
    )
    assert out["badge"] == "no_data"
    assert out["funnel"]["joined"] == 0
