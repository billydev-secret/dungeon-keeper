"""Unit tests for bot_modules.services.health_metrics.

Pure metric math is the easy half — gini, lorenz, percent helpers. The
harder half exercises ``compute_*`` against a migrated DB seeded with
synthetic messages / xp / sentiment / interactions / audit log entries.
Each test targets a specific branch (empty DB, happy path, signal-driver
edge cases) called out in the task brief.
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services import health_metrics as hm
from migrations import apply_migrations_sync


GUILD = 10


# ── Shared fixture ───────────────────────────────────────────────────


@pytest.fixture
def db_conn(tmp_path):
    path = tmp_path / "hm.db"
    apply_migrations_sync(path)
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
    conn.execute(
        "INSERT OR REPLACE INTO message_sentiment "
        "(message_id, guild_id, channel_id, sentiment, emotion, computed_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (mid, GUILD, cid, sentiment, emotion, ts_now),
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


# ── compute_user_churn_score (single-user signal coverage) ───────────


def test_compute_user_churn_score_quiet_user_is_declining(db_conn):
    now = 1_700_000_000.0
    # No activity at all → frequency (30%) + channels (25%) + reciprocity (20%)
    # all max out = 75. Sentiment and gap signals stay at 0 (no messages to
    # measure), so the tier lands in the 50-79 ``declining`` band.
    out = hm.compute_user_churn_score(db_conn, GUILD, user_id=42, now=now)
    assert out["score"] == 75
    assert out["tier"] == "declining"
    assert out["last_seen"] == 0.0
    assert out["signals"]["frequency"] == 100
    assert out["signals"]["channels"] == 100
    assert out["signals"]["reciprocity"] == 100


def test_compute_user_churn_score_active_user_is_clear(db_conn):
    now = 1_700_000_000.0
    # Steady activity in last 7d AND prior 7d, multiple channels
    for d in range(6):
        ts = int(now - d * 86400 - 60)
        _seed_message(db_conn, mid=100 + d, cid=100 + (d % 2), aid=1, ts=ts)
        _seed_interaction(db_conn, frm=2, to=1, ts=ts)
    # Inbound interactions in both windows
    for d in range(8, 14):
        ts = int(now - d * 86400)
        _seed_message(db_conn, mid=200 + d, cid=100, aid=1, ts=ts)
        _seed_interaction(db_conn, frm=2, to=1, ts=ts)
    db_conn.commit()
    out = hm.compute_user_churn_score(db_conn, GUILD, user_id=1, now=now)
    assert out["score"] < 50
    assert out["tier"] in ("clear", "watch")


def test_compute_user_churn_score_declining_user(db_conn):
    now = 1_700_000_000.0
    # Lots of activity in prev 7d window, almost none in last 7d → freq_decline
    for d in range(8, 14):
        ts = int(now - d * 86400)
        _seed_message(db_conn, mid=100 + d, cid=100, aid=1, ts=ts)
    # One tiny message last 7d
    _seed_message(db_conn, mid=200, cid=100, aid=1, ts=int(now - 3 * 86400))
    db_conn.commit()
    out = hm.compute_user_churn_score(db_conn, GUILD, user_id=1, now=now)
    # Score should be elevated by frequency decline + channel narrow
    assert out["score"] >= 30
    assert out["signals"]["frequency"] > 0


# ── compute_churn_risk (guild-wide aggregate) ────────────────────────


def test_compute_churn_risk_empty_db(db_conn):
    out = hm.compute_churn_risk(db_conn, GUILD, now=1_700_000_000.0)
    assert out["at_risk_count"] == 0
    assert out["badge"] == "clear"
    assert out["risk_distribution"] == [0] * 10


def test_compute_churn_risk_classifies_at_risk_users(db_conn):
    now = 1_700_000_000.0
    # User 1: declining
    _seed_known_user(db_conn, 1)
    _seed_known_user(db_conn, 2)
    # Heavy prior 7d activity, very light recent → big freq_decline
    for d in range(8, 14):
        _seed_message(db_conn, mid=100 + d, cid=100, aid=1, ts=int(now - d * 86400))
    _seed_message(db_conn, mid=300, cid=100, aid=1, ts=int(now - 3 * 86400))
    # User 2: bot — should be excluded
    _seed_known_user(db_conn, 2, is_bot=1)
    _seed_message(db_conn, mid=500, cid=100, aid=2, ts=int(now - 60))
    db_conn.commit()
    out = hm.compute_churn_risk(db_conn, GUILD, now=now)
    # User 1 should appear in at_risk list, user 2 (bot) should not
    user_ids = [r["user_id"] for r in out["at_risk"]]
    assert "1" in user_ids
    assert "2" not in user_ids


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
    """A mod using their own Voice Master channel isn't moderation work."""
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
