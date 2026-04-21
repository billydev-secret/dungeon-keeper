"""Compute functions for the 12 community health dashboard tiles.

Every function is synchronous, touches only the SQLite database (no discord.py
objects), and returns a plain ``dict`` suitable for JSON serialisation.  Callers
run them via ``asyncio.to_thread`` / ``run_query``.
"""

from __future__ import annotations

import datetime
import sqlite3
import statistics
import time
from collections import defaultdict
from collections.abc import Sequence

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DAY = 86400
_WEEK = 7 * _DAY
_MONTH = 30 * _DAY


def _ts(days_ago: int = 0, *, now: float | None = None) -> int:
    return int((now or time.time()) - days_ago * _DAY)


def _gini(values: Sequence[float]) -> float:
    """Compute the Gini coefficient for a list of non-negative values."""
    if not values or max(values) == 0:
        return 0.0
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    total = sum(sorted_vals)
    cum = 0.0
    weighted_sum = 0.0
    for i, v in enumerate(sorted_vals):
        cum += v
        weighted_sum += (2 * (i + 1) - n - 1) * v
    return weighted_sum / (n * total) if total else 0.0


def _lorenz_points(values: list[float], num_points: int = 20) -> list[dict]:
    """Return Lorenz curve points as [{x: pct_population, y: pct_value}]."""
    if not values:
        return [{"x": 0.0, "y": 0.0}, {"x": 100.0, "y": 100.0}]
    sorted_vals = sorted(values)
    total = sum(sorted_vals)
    if total == 0:
        return [{"x": 0.0, "y": 0.0}, {"x": 100.0, "y": 100.0}]
    n = len(sorted_vals)
    points: list[dict[str, float]] = [{"x": 0.0, "y": 0.0}]
    step = max(1, n // num_points)
    cum = 0.0
    for i in range(0, n, step):
        cum += (
            sum(sorted_vals[max(0, i - step + 1) : i + 1]) if i > 0 else sorted_vals[0]
        )
        points.append(
            {
                "x": round((i + 1) / n * 100, 1),
                "y": round(cum / total * 100, 1),
            }
        )
    # Ensure the final point
    if points[-1]["x"] != 100.0:
        points.append({"x": 100.0, "y": 100.0})
    return points


def _badge(value: float, thresholds: list[tuple[float, str]]) -> str:
    """Return a badge label based on value crossing thresholds (ascending)."""
    for threshold, label in thresholds:
        if value <= threshold:
            return label
    return thresholds[-1][1] if thresholds else "unknown"


def _pct(num: float, den: float) -> float:
    return round(num / den * 100, 1) if den else 0.0


# ---------------------------------------------------------------------------
# 1. DAU / MAU stickiness
# ---------------------------------------------------------------------------


def compute_dau_mau(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    now: float | None = None,
    member_count: int = 0,
    voice_active_count: int = 0,
) -> dict:
    now = now or time.time()

    # DAU / WAU / MAU counts
    dau = conn.execute(
        "SELECT COUNT(DISTINCT author_id) FROM messages WHERE guild_id=? AND ts>=?",
        (guild_id, _ts(1, now=now)),
    ).fetchone()[0]
    wau = conn.execute(
        "SELECT COUNT(DISTINCT author_id) FROM messages WHERE guild_id=? AND ts>=?",
        (guild_id, _ts(7, now=now)),
    ).fetchone()[0]
    mau = conn.execute(
        "SELECT COUNT(DISTINCT author_id) FROM messages WHERE guild_id=? AND ts>=?",
        (guild_id, _ts(30, now=now)),
    ).fetchone()[0]

    dau_mau = _pct(dau, mau)
    wau_mau = _pct(wau, mau)

    # 30-day sparkline (daily DAU)
    sparkline = []
    for d in range(29, -1, -1):
        day_start = _ts(d + 1, now=now)
        day_end = _ts(d, now=now)
        cnt = conn.execute(
            "SELECT COUNT(DISTINCT author_id) FROM messages "
            "WHERE guild_id=? AND ts>=? AND ts<?",
            (guild_id, day_start, day_end),
        ).fetchone()[0]
        sparkline.append(cnt)

    # Voice active: unique users with voice XP events in last 7 days
    voice_7d = conn.execute(
        "SELECT COUNT(DISTINCT user_id) FROM xp_events "
        "WHERE guild_id = ? AND source = 'voice' AND created_at >= ?",
        (guild_id, now - 7 * _DAY),
    ).fetchone()[0]

    # Engagement depth funnel
    funnel = {
        "total_members": member_count,
        "mau": mau,
        "wau": wau,
        "dau": dau,
        "voice_active": voice_7d or voice_active_count,
    }

    # Active user composition: returning vs reactivated vs new (first 7 days)
    seven_days_ago = _ts(7, now=now)
    thirty_days_ago = _ts(30, now=now)

    # Users active today
    todays_authors = conn.execute(
        "SELECT DISTINCT author_id FROM messages WHERE guild_id=? AND ts>=?",
        (guild_id, _ts(1, now=now)),
    ).fetchall()
    today_ids = {r[0] for r in todays_authors}

    # First-ever message timestamp per user (within 90 days to limit scan)
    first_msg = {}
    rows = conn.execute(
        "SELECT author_id, MIN(ts) AS first_ts FROM messages "
        "WHERE guild_id=? AND ts>=? GROUP BY author_id",
        (guild_id, _ts(90, now=now)),
    ).fetchall()
    for r in rows:
        first_msg[r["author_id"]] = r["first_ts"]

    new_count = 0
    reactivated_count = 0
    returning_count = 0
    for uid in today_ids:
        ft = first_msg.get(uid)
        if ft and ft >= seven_days_ago:
            new_count += 1
        else:
            # Was active in previous 8-30 day window?
            prev = conn.execute(
                "SELECT 1 FROM messages WHERE guild_id=? AND author_id=? "
                "AND ts>=? AND ts<? LIMIT 1",
                (guild_id, uid, _ts(30, now=now), _ts(7, now=now)),
            ).fetchone()
            if prev:
                returning_count += 1
            else:
                reactivated_count += 1

    composition = {
        "returning": returning_count,
        "reactivated": reactivated_count,
        "new": new_count,
    }

    # Lurker activation rate: users whose first message is within last 30 days
    # out of total members
    first_timers_30d = conn.execute(
        """SELECT COUNT(*) FROM (
            SELECT author_id, MIN(ts) AS first_ts FROM messages
            WHERE guild_id=? GROUP BY author_id
            HAVING first_ts >= ?
        )""",
        (guild_id, thirty_days_ago),
    ).fetchone()[0]
    lurker_activation = _pct(first_timers_30d, member_count) if member_count else 0

    # Day-of-week breakdown (avg DAU per weekday, 0=Mon)
    dow_rows = conn.execute(
        """SELECT CAST(((ts % 604800) + 345600) / 86400 AS INTEGER) % 7 AS dow,
                  COUNT(DISTINCT author_id) AS cnt
           FROM messages WHERE guild_id=? AND ts>=?
           GROUP BY dow ORDER BY dow""",
        (guild_id, thirty_days_ago),
    ).fetchall()
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    dow_map = {r["dow"]: r["cnt"] for r in dow_rows}
    day_of_week = [
        {"day": dow_names[i], "avg_dau": round(dow_map.get(i, 0) / 4.3, 1)}
        for i in range(7)
    ]

    badge = _badge(
        dau_mau,
        [(10, "critical"), (20, "needs_work"), (30, "healthy"), (100, "excellent")],
    )

    return {
        "dau": dau,
        "wau": wau,
        "mau": mau,
        "dau_mau": dau_mau,
        "wau_mau": wau_mau,
        "badge": badge,
        "sparkline": sparkline,
        "funnel": funnel,
        "composition": composition,
        "lurker_activation": lurker_activation,
        "day_of_week": day_of_week,
    }


# ---------------------------------------------------------------------------
# 2. Activity heatmap
# ---------------------------------------------------------------------------


def compute_heatmap(
    conn: sqlite3.Connection, guild_id: int, *, now: float | None = None
) -> dict:
    now = now or time.time()
    thirty_days_ago = _ts(30, now=now)

    # 7x24 grid: day_of_week (0=Mon) x hour_of_day -> avg msgs/hr
    rows = conn.execute(
        """SELECT
             CAST(((ts % 604800) + 345600) / 86400 AS INTEGER) % 7 AS dow,
             (ts % 86400) / 3600 AS hod,
             COUNT(*) AS cnt
           FROM messages WHERE guild_id=? AND ts>=?
           GROUP BY dow, hod""",
        (guild_id, thirty_days_ago),
    ).fetchall()

    weeks = 4.3  # ~30 days
    grid = [[0.0] * 24 for _ in range(7)]
    for r in rows:
        grid[r["dow"]][r["hod"]] = round(r["cnt"] / weeks, 1)

    # Find peak and quiet slots
    peak_val: float = 0
    peak_dow, peak_hod = 0, 0
    quiet_val, quiet_dow, quiet_hod = float("inf"), 0, 0
    dead_hours = 0
    for d in range(7):
        for h in range(24):
            v = grid[d][h]
            if v > peak_val:
                peak_val, peak_dow, peak_hod = v, d, h
            if v < quiet_val:
                quiet_val, quiet_dow, quiet_hod = v, d, h
            if v < 1:
                dead_hours += 1

    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    def _slot_label(dow, hod):
        h = hod % 12 or 12
        ap = "am" if hod < 12 else "pm"
        return f"{dow_names[dow]} {h}{ap}"

    # Per-channel mini heatmaps (top 10 channels by volume)
    ch_rows = conn.execute(
        """SELECT channel_id,
             CAST(((ts % 604800) + 345600) / 86400 AS INTEGER) % 7 AS dow,
             (ts % 86400) / 3600 AS hod,
             COUNT(*) AS cnt
           FROM messages WHERE guild_id=? AND ts>=?
           GROUP BY channel_id, dow, hod
           ORDER BY cnt DESC""",
        (guild_id, thirty_days_ago),
    ).fetchall()

    ch_totals: dict[int, int] = defaultdict(int)
    ch_grids: dict[int, list[list[float]]] = {}
    for r in ch_rows:
        cid = r["channel_id"]
        ch_totals[cid] += r["cnt"]
        if cid not in ch_grids:
            ch_grids[cid] = [[0.0] * 24 for _ in range(7)]
        ch_grids[cid][r["dow"]][r["hod"]] = round(r["cnt"] / weeks, 1)

    top_channels = sorted(ch_totals, key=lambda c: ch_totals[c], reverse=True)[:10]
    per_channel = [
        {"channel_id": str(cid), "grid": ch_grids.get(cid, [[0] * 24] * 7)}
        for cid in top_channels
    ]

    return {
        "grid": grid,
        "peak_slot": _slot_label(peak_dow, peak_hod),
        "peak_value": peak_val,
        "quiet_slot": _slot_label(quiet_dow, quiet_hod),
        "quiet_value": quiet_val,
        "dead_hours": dead_hours,
        "per_channel": per_channel,
    }


# ---------------------------------------------------------------------------
# 3. Channel health
# ---------------------------------------------------------------------------


def compute_channel_health(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    now: float | None = None,
    nsfw_channel_ids: list[int] | None = None,
) -> dict:
    now = now or time.time()
    thirty_days_ago = _ts(30, now=now)
    nsfw_ids = set(nsfw_channel_ids or [])

    # Per-channel stats
    ch_rows = conn.execute(
        """SELECT channel_id,
                  COUNT(*) AS msg_count,
                  COUNT(DISTINCT author_id) AS unique_users
           FROM messages WHERE guild_id=? AND ts>=?
           GROUP BY channel_id""",
        (guild_id, thirty_days_ago),
    ).fetchall()

    # Thread depth: avg replies per thread starter
    depth_rows = conn.execute(
        """SELECT m.channel_id,
                  AVG(reply_cnt) AS avg_depth
           FROM (
               SELECT channel_id, reply_to_id, COUNT(*) AS reply_cnt
               FROM messages
               WHERE guild_id=? AND ts>=? AND reply_to_id IS NOT NULL
               GROUP BY channel_id, reply_to_id
           ) m
           GROUP BY m.channel_id""",
        (guild_id, thirty_days_ago),
    ).fetchall()
    depth_map = {r["channel_id"]: round(r["avg_depth"], 2) for r in depth_rows}

    # Per-channel Gini (message distribution among authors)
    author_counts_rows = conn.execute(
        """SELECT channel_id, author_id, COUNT(*) AS cnt
           FROM messages WHERE guild_id=? AND ts>=?
           GROUP BY channel_id, author_id""",
        (guild_id, thirty_days_ago),
    ).fetchall()
    ch_author_counts: dict[int, list[int]] = defaultdict(list)
    for r in author_counts_rows:
        ch_author_counts[r["channel_id"]].append(r["cnt"])

    # Last message per channel (for dormant detection)
    last_msg_rows = conn.execute(
        "SELECT channel_id, MAX(ts) AS last_ts FROM messages WHERE guild_id=? GROUP BY channel_id",
        (guild_id,),
    ).fetchall()
    last_msg_map = {r["channel_id"]: r["last_ts"] for r in last_msg_rows}

    channels = []
    active_count = 0
    flagged_count = 0
    dormant_count = 0
    archive_count = 0

    for r in ch_rows:
        cid = r["channel_id"]
        msg_count = r["msg_count"]
        unique = r["unique_users"]
        depth = depth_map.get(cid, 0)
        gini = _gini(ch_author_counts.get(cid, []))
        msgs_per_day = round(msg_count / 30, 1)

        # Composite score: volume (30%) + unique users (25%) + depth (25%) + equity (20%)
        vol_score = min(100, msgs_per_day * 5)  # 20 msgs/day = 100
        user_score = min(100, unique * 10)  # 10 unique = 100
        depth_score = min(100, depth * 25)  # depth 4 = 100
        equity_score = max(0, (1 - gini) * 100)
        composite = round(
            vol_score * 0.3
            + user_score * 0.25
            + depth_score * 0.25
            + equity_score * 0.2,
            1,
        )

        last_ts = last_msg_map.get(cid, 0)
        age = now - last_ts if last_ts else float("inf")

        if age > 30 * _DAY:
            status = "archive"
            archive_count += 1
        elif age > 14 * _DAY:
            status = "dormant"
            dormant_count += 1
        elif composite < 50:
            status = "flagged"
            flagged_count += 1
            active_count += 1
        else:
            status = "healthy"
            active_count += 1

        channels.append(
            {
                "channel_id": str(cid),
                "score": composite,
                "msgs_per_day": msgs_per_day,
                "unique_weekly_users": unique,
                "avg_thread_depth": depth,
                "gini": round(gini, 3),
                "status": status,
                "is_nsfw": cid in nsfw_ids,
            }
        )

    channels.sort(key=lambda c: c["score"], reverse=True)

    return {
        "active_count": active_count,
        "flagged_count": flagged_count,
        "dormant_count": dormant_count,
        "archive_count": archive_count,
        "channels": channels[:50],
        "top5": channels[:5],
    }


# ---------------------------------------------------------------------------
# 4. Participation Gini coefficient
# ---------------------------------------------------------------------------


def compute_gini(
    conn: sqlite3.Connection, guild_id: int, *, now: float | None = None
) -> dict:
    now = now or time.time()
    thirty_days_ago = _ts(30, now=now)

    # Message counts per user
    rows = conn.execute(
        "SELECT author_id, COUNT(*) AS cnt FROM messages "
        "WHERE guild_id=? AND ts>=? GROUP BY author_id ORDER BY cnt",
        (guild_id, thirty_days_ago),
    ).fetchall()
    msg_counts = [r["cnt"] for r in rows]

    gini_val = round(_gini(msg_counts), 3)
    lorenz = _lorenz_points(msg_counts)

    # Top 5% / 10% share
    total_msgs = sum(msg_counts)
    n = len(msg_counts)
    top5_idx = max(0, n - max(1, int(n * 0.05)))
    top10_idx = max(0, n - max(1, int(n * 0.10)))
    bottom40_idx = max(1, int(n * 0.40))
    top5_share = _pct(sum(msg_counts[top5_idx:]), total_msgs)
    top10_share = _pct(sum(msg_counts[top10_idx:]), total_msgs)
    bottom40_share = sum(msg_counts[:bottom40_idx])
    top10_abs = sum(msg_counts[top10_idx:])
    palma = round(top10_abs / bottom40_share, 2) if bottom40_share else 0

    # Participation tiers
    user_counts_weekly = {}
    for r in rows:
        user_counts_weekly[r["author_id"]] = r["cnt"] / 4.3  # approximate weekly

    lurkers = power = active = moderate = light = 0
    for wk in user_counts_weekly.values():
        if wk == 0:
            lurkers += 1
        elif wk <= 5:
            light += 1
        elif wk <= 20:
            moderate += 1
        elif wk <= 50:
            active += 1
        else:
            power += 1

    tiers = {
        "lurker": lurkers,
        "light": light,
        "moderate": moderate,
        "active": active,
        "power": power,
    }

    # 30-day sparkline (weekly Gini)
    sparkline = []
    for w in range(3, -1, -1):
        w_start = _ts((w + 1) * 7, now=now)
        w_end = _ts(w * 7, now=now)
        w_rows = conn.execute(
            "SELECT COUNT(*) AS cnt FROM messages "
            "WHERE guild_id=? AND ts>=? AND ts<? GROUP BY author_id ORDER BY cnt",
            (guild_id, w_start, w_end),
        ).fetchall()
        w_vals = [r["cnt"] for r in w_rows]
        sparkline.append(round(_gini(w_vals), 3))

    # Gini history (12 weekly snapshots, oldest → newest)
    gini_history = []
    for w in range(11, -1, -1):
        w_start = _ts((w + 1) * 7, now=now)
        w_end = _ts(w * 7, now=now)
        h_rows = conn.execute(
            "SELECT COUNT(*) AS cnt FROM messages "
            "WHERE guild_id=? AND ts>=? AND ts<? GROUP BY author_id ORDER BY cnt",
            (guild_id, w_start, w_end),
        ).fetchall()
        h_vals = [r["cnt"] for r in h_rows]
        dt = datetime.datetime.utcfromtimestamp(w_start)
        label = dt.strftime("%b ") + str(dt.day)
        gini_history.append({"label": label, "gini": round(_gini(h_vals), 3)})

    # Per-channel Gini (top 10 channels)
    ch_rows = conn.execute(
        """SELECT channel_id, author_id, COUNT(*) AS cnt
           FROM messages WHERE guild_id=? AND ts>=?
           GROUP BY channel_id, author_id""",
        (guild_id, thirty_days_ago),
    ).fetchall()
    ch_counts: dict[int, list[int]] = defaultdict(list)
    for r in ch_rows:
        ch_counts[r["channel_id"]].append(r["cnt"])

    ch_totals = {cid: sum(vals) for cid, vals in ch_counts.items()}
    top_chs = sorted(ch_totals, key=lambda c: ch_totals[c], reverse=True)[:10]
    per_channel = [
        {
            "channel_id": str(cid),
            "gini": round(_gini(ch_counts[cid]), 3),
            "msgs": ch_totals[cid],
        }
        for cid in top_chs
    ]

    # Weighted Gini (messages + reactions*0.25 + voice*0.5)
    # Reactions received per user
    react_rows = conn.execute(
        """SELECT author_id, COUNT(*) AS cnt FROM reaction_log
           WHERE guild_id=? AND ts>=? GROUP BY author_id""",
        (guild_id, thirty_days_ago),
    ).fetchall()
    react_map = {r["author_id"]: r["cnt"] for r in react_rows}

    # Voice activity per user (each voice XP event ≈ 1 minute interval)
    voice_rows = conn.execute(
        """SELECT user_id, COUNT(*) AS intervals
           FROM xp_events
           WHERE guild_id=? AND source='voice' AND created_at>=?
           GROUP BY user_id""",
        (guild_id, thirty_days_ago),
    ).fetchall()
    voice_map: dict[int, float] = {
        r["user_id"]: float(r["intervals"]) for r in voice_rows
    }

    all_users = set(r["author_id"] for r in rows)
    all_users.update(react_map.keys())
    all_users.update(voice_map.keys())
    weighted_vals = []
    for uid in all_users:
        mc = sum(1 for r in rows if r["author_id"] == uid)
        rc = react_map.get(uid, 0) * 0.25
        vc = voice_map.get(uid, 0) * 0.5  # 0.5 per minute
        weighted_vals.append(mc + rc + vc)
    weighted_gini = round(_gini(sorted(weighted_vals)), 3) if weighted_vals else 0

    # XP distribution Gini
    xp_rows = conn.execute(
        "SELECT user_id, SUM(amount) AS total_xp FROM xp_events "
        "WHERE guild_id=? AND created_at>=? GROUP BY user_id ORDER BY total_xp",
        (guild_id, thirty_days_ago),
    ).fetchall()
    xp_vals = [r["total_xp"] for r in xp_rows]
    xp_gini = round(_gini(xp_vals), 3)

    badge = _badge(
        gini_val,
        [(0.50, "excellent"), (0.70, "healthy"), (0.85, "warning"), (1.0, "critical")],
    )

    return {
        "gini": gini_val,
        "badge": badge,
        "lorenz": lorenz,
        "top5_share": top5_share,
        "top10_share": top10_share,
        "palma": palma,
        "tiers": tiers,
        "sparkline": sparkline,
        "per_channel": per_channel,
        "weighted_gini": weighted_gini,
        "xp_gini": xp_gini,
        "gini_history": gini_history,
    }


# ---------------------------------------------------------------------------
# 5. Social graph health
# ---------------------------------------------------------------------------


def compute_social_graph(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    now: float | None = None,
    nsfw_channel_ids: list[int] | None = None,
) -> dict:
    from services.graph_metrics import compute_graph_metrics

    now = now or time.time()
    thirty_days_ago = _ts(30, now=now)
    nsfw_ids = set(nsfw_channel_ids or [])

    # Build adjacency list from interaction log (30-day window)
    rows = conn.execute(
        """SELECT from_user_id, to_user_id, COUNT(*) AS weight
           FROM user_interactions_log
           WHERE guild_id=? AND ts>=?
           GROUP BY from_user_id, to_user_id""",
        (guild_id, int(thirty_days_ago)),
    ).fetchall()

    metrics = compute_graph_metrics(
        ((r["from_user_id"], r["to_user_id"], r["weight"]) for r in rows),
        top_n=100,
    )

    # SFW/NSFW bridge: users active in both
    if nsfw_ids:
        nsfw_ph = ",".join("?" * len(nsfw_ids))
        sfw_users = set(
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT author_id FROM messages WHERE guild_id=? AND ts>=? "
                f"AND channel_id NOT IN ({nsfw_ph})",
                [guild_id, thirty_days_ago] + list(nsfw_ids),
            ).fetchall()
        )
        nsfw_users = set(
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT author_id FROM messages WHERE guild_id=? AND ts>=? "
                f"AND channel_id IN ({nsfw_ph})",
                [guild_id, thirty_days_ago] + list(nsfw_ids),
            ).fetchall()
        )
        bridge_both = len(sfw_users & nsfw_users)
        sfw_nsfw_bridge = _pct(bridge_both, len(sfw_users | nsfw_users))
    else:
        sfw_nsfw_bridge = 0

    return {**metrics, "sfw_nsfw_bridge_pct": sfw_nsfw_bridge}


# ---------------------------------------------------------------------------
# 6. Sentiment & tone
# ---------------------------------------------------------------------------


def compute_sentiment(
    conn: sqlite3.Connection, guild_id: int, *, now: float | None = None
) -> dict:
    now = now or time.time()
    thirty_days_ago = _ts(30, now=now)

    # Overall stats — use message timestamp (m.ts) so backfilled scores
    # are attributed to the day the message was actually sent, not the day
    # the VADER score was computed.
    row = conn.execute(
        "SELECT AVG(ms.sentiment) AS avg_s, COUNT(*) AS cnt "
        "FROM message_sentiment ms "
        "JOIN messages m ON ms.message_id = m.message_id "
        "WHERE ms.guild_id=? AND m.ts>=?",
        (guild_id, thirty_days_ago),
    ).fetchone()
    avg_sentiment = round(row["avg_s"], 3) if row["avg_s"] is not None else 0
    scored_count = row["cnt"]

    # Emotion category breakdown
    emotion_rows = conn.execute(
        "SELECT ms.emotion, COUNT(*) AS cnt "
        "FROM message_sentiment ms "
        "JOIN messages m ON ms.message_id = m.message_id "
        "WHERE ms.guild_id=? AND m.ts>=? AND ms.emotion IS NOT NULL "
        "GROUP BY ms.emotion",
        (guild_id, thirty_days_ago),
    ).fetchall()
    emotions = {r["emotion"]: r["cnt"] for r in emotion_rows}
    emotion_total = sum(emotions.values()) or 1
    emotion_pcts = {k: round(v / emotion_total * 100, 1) for k, v in emotions.items()}

    # Positive / negative ratio
    pos_count = conn.execute(
        "SELECT COUNT(*) FROM message_sentiment ms "
        "JOIN messages m ON ms.message_id = m.message_id "
        "WHERE ms.guild_id=? AND m.ts>=? AND ms.sentiment>0.05",
        (guild_id, thirty_days_ago),
    ).fetchone()[0]
    neg_count = conn.execute(
        "SELECT COUNT(*) FROM message_sentiment ms "
        "JOIN messages m ON ms.message_id = m.message_id "
        "WHERE ms.guild_id=? AND m.ts>=? AND ms.sentiment<-0.05",
        (guild_id, thirty_days_ago),
    ).fetchone()[0]
    pos_neg_ratio = round(pos_count / neg_count, 1) if neg_count else 0

    # 30-day daily sentiment sparkline
    sparkline = []
    for d in range(29, -1, -1):
        day_start = _ts(d + 1, now=now)
        day_end = _ts(d, now=now)
        r = conn.execute(
            "SELECT AVG(ms.sentiment) AS avg_s "
            "FROM message_sentiment ms "
            "JOIN messages m ON ms.message_id = m.message_id "
            "WHERE ms.guild_id=? AND m.ts>=? AND m.ts<?",
            (guild_id, day_start, day_end),
        ).fetchone()
        sparkline.append(round(r["avg_s"], 3) if r["avg_s"] is not None else 0)

    # Negative spikes: 5-minute windows where avg sentiment < -0.3
    spike_rows = conn.execute(
        """SELECT CAST(m.ts / 300 AS INTEGER) * 300 AS window_start,
                  AVG(ms.sentiment) AS avg_s,
                  COUNT(*) AS cnt
           FROM message_sentiment ms
           JOIN messages m ON ms.message_id = m.message_id
           WHERE ms.guild_id=? AND m.ts>=?
           GROUP BY CAST(m.ts / 300 AS INTEGER)
           HAVING avg_s < -0.3 AND cnt >= 3
           ORDER BY window_start DESC
           LIMIT 20""",
        (guild_id, _ts(7, now=now)),
    ).fetchall()
    spikes = [
        {
            "timestamp": r["window_start"],
            "avg_sentiment": round(r["avg_s"], 3),
            "msg_count": r["cnt"],
        }
        for r in spike_rows
    ]

    # Per-channel sentiment
    ch_rows = conn.execute(
        """SELECT ms.channel_id, AVG(ms.sentiment) AS avg_s, COUNT(*) AS cnt
           FROM message_sentiment ms
           JOIN messages m ON ms.message_id = m.message_id
           WHERE ms.guild_id=? AND m.ts>=?
           GROUP BY ms.channel_id
           ORDER BY avg_s DESC""",
        (guild_id, thirty_days_ago),
    ).fetchall()
    per_channel = [
        {
            "channel_id": str(r["channel_id"]),
            "avg_sentiment": round(r["avg_s"], 3),
            "count": r["cnt"],
        }
        for r in ch_rows[:20]
    ]

    badge = _badge(
        avg_sentiment,
        [(-0.1, "critical"), (0.0, "needs_work"), (0.2, "healthy"), (1.0, "excellent")],
    )

    return {
        "avg_sentiment": avg_sentiment,
        "badge": badge,
        "scored_count": scored_count,
        "emotions": emotion_pcts,
        "pos_neg_ratio": pos_neg_ratio,
        "sparkline": sparkline,
        "spikes_7d": len(spikes),
        "spike_log": spikes,
        "per_channel": per_channel,
    }


# ---------------------------------------------------------------------------
# 7. Newcomer activation funnel
# ---------------------------------------------------------------------------


def compute_newcomer_funnel(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    now: float | None = None,
    recent_join_ids: dict[int, float] | None = None,
) -> dict:
    """*recent_join_ids* maps user_id -> join_timestamp for members who joined
    in the last 90 days (sourced from guild cache by the caller)."""
    now = now or time.time()
    if not recent_join_ids:
        recent_join_ids = {}

    # Also try invite_edges for join times
    invite_rows = conn.execute(
        "SELECT invitee_id, joined_at FROM invite_edges WHERE guild_id=? AND joined_at>=?",
        (guild_id, _ts(90, now=now)),
    ).fetchall()
    for r in invite_rows:
        if r["invitee_id"] not in recent_join_ids:
            recent_join_ids[r["invitee_id"]] = r["joined_at"]

    if not recent_join_ids:
        return {
            "activation_rate": 0,
            "badge": "no_data",
            "funnel": {
                "joined": 0,
                "first_message": 0,
                "first_reply": 0,
                "three_channels": 0,
                "d7_return": 0,
            },
            "time_to_first_msg": {"median_hours": 0, "distribution": []},
            "first_response_latency": {"median_minutes": 0},
            "cohorts": [],
        }

    joined = len(recent_join_ids)
    first_message = 0
    first_reply = 0
    three_channels = 0
    d7_return = 0
    ttfm_hours = []
    response_latencies = []

    for uid, join_ts in recent_join_ids.items():
        # First message
        first_msg = conn.execute(
            "SELECT MIN(ts) AS first_ts FROM messages WHERE guild_id=? AND author_id=? AND ts>=?",
            (guild_id, uid, int(join_ts)),
        ).fetchone()
        if not first_msg or first_msg["first_ts"] is None:
            continue
        first_message += 1
        ttfm_hours.append((first_msg["first_ts"] - join_ts) / 3600)

        # First reply received
        first_reply_row = conn.execute(
            """SELECT MIN(m.ts) AS reply_ts FROM messages m
               WHERE m.guild_id=? AND m.reply_to_id IN (
                   SELECT message_id FROM messages WHERE guild_id=? AND author_id=? AND ts>=?
               ) AND m.author_id != ? AND m.ts>=?""",
            (guild_id, guild_id, uid, int(join_ts), uid, int(join_ts)),
        ).fetchone()
        if first_reply_row and first_reply_row["reply_ts"] is not None:
            first_reply += 1
            latency = (first_reply_row["reply_ts"] - first_msg["first_ts"]) / 60
            response_latencies.append(latency)

        # 3+ channels visited
        ch_count = conn.execute(
            "SELECT COUNT(DISTINCT channel_id) FROM messages WHERE guild_id=? AND author_id=? AND ts>=?",
            (guild_id, uid, int(join_ts)),
        ).fetchone()[0]
        if ch_count >= 3:
            three_channels += 1

        # D7 return: active 7+ days after joining
        d7_ts = join_ts + 7 * _DAY
        if d7_ts < now:
            d7_active = conn.execute(
                "SELECT 1 FROM messages WHERE guild_id=? AND author_id=? AND ts>=? AND ts<? LIMIT 1",
                (guild_id, uid, int(d7_ts), int(d7_ts + 7 * _DAY)),
            ).fetchone()
            if d7_active:
                d7_return += 1

    # Only count users eligible for D7 (joined 14+ days ago)
    d7_eligible = sum(1 for ts in recent_join_ids.values() if now - ts >= 14 * _DAY)
    activation_rate = _pct(d7_return, d7_eligible) if d7_eligible else 0

    funnel = {
        "joined": joined,
        "first_message": first_message,
        "first_reply": first_reply,
        "three_channels": three_channels,
        "d7_return": d7_return,
    }

    median_ttfm = round(statistics.median(ttfm_hours), 1) if ttfm_hours else 0
    median_latency = (
        round(statistics.median(response_latencies), 1) if response_latencies else 0
    )

    # Time-to-first-message distribution
    ttfm_dist = {"under_1h": 0, "1_4h": 0, "4_24h": 0, "24_48h": 0, "over_48h": 0}
    for h in ttfm_hours:
        if h < 1:
            ttfm_dist["under_1h"] += 1
        elif h < 4:
            ttfm_dist["1_4h"] += 1
        elif h < 24:
            ttfm_dist["4_24h"] += 1
        elif h < 48:
            ttfm_dist["24_48h"] += 1
        else:
            ttfm_dist["over_48h"] += 1

    badge = _badge(
        activation_rate,
        [(20, "critical"), (30, "needs_work"), (40, "healthy"), (100, "excellent")],
    )

    return {
        "activation_rate": activation_rate,
        "badge": badge,
        "funnel": funnel,
        "time_to_first_msg": {"median_hours": median_ttfm, "distribution": ttfm_dist},
        "first_response_latency": {"median_minutes": median_latency},
        "cohorts": [],  # populated in deep-dive endpoint
    }


# ---------------------------------------------------------------------------
# 8. Cohort retention curves
# ---------------------------------------------------------------------------


def compute_cohort_retention(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    now: float | None = None,
    join_times: dict[int, float] | None = None,
) -> dict:
    now = now or time.time()
    if not join_times:
        join_times = {}
    # Supplement from invite_edges
    rows = conn.execute(
        "SELECT invitee_id, joined_at FROM invite_edges WHERE guild_id=?",
        (guild_id,),
    ).fetchall()
    for r in rows:
        if r["invitee_id"] not in join_times:
            join_times[r["invitee_id"]] = r["joined_at"]

    # Fall back to first-message-ever as proxy for join date
    first_msg_rows = conn.execute(
        "SELECT author_id, MIN(ts) AS first_ts FROM messages WHERE guild_id=? GROUP BY author_id",
        (guild_id,),
    ).fetchall()
    for r in first_msg_rows:
        if r["author_id"] not in join_times:
            join_times[r["author_id"]] = r["first_ts"]

    if not join_times:
        return {
            "d7": 0,
            "d30": 0,
            "d90": 0,
            "badge": "no_data",
            "cohorts": [],
            "heatmap": [],
        }

    # Group into weekly cohorts, keyed by week_start timestamp for stable ordering
    cohorts: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for uid, jt in join_times.items():
        week_start = int(jt) - (int(jt) % _WEEK)
        cohorts[week_start].append((uid, jt))

    # Compute retention for each cohort at D1, D7, D14, D30, D60, D90
    checkpoints = [1, 7, 14, 30, 60, 90]
    cohort_results = []
    for week_start in sorted(cohorts.keys())[-12:]:  # last 12 weeks
        members = cohorts[week_start]
        size = len(members)
        label = time.strftime("%Y-W%W", time.gmtime(week_start))
        rates: dict[str, float | None] = {}
        for d in checkpoints:
            retained = 0
            eligible = 0
            for uid, jt in members:
                target_start = jt + d * _DAY
                target_end = target_start + 7 * _DAY
                if target_start > now:
                    continue
                eligible += 1
                active = conn.execute(
                    "SELECT 1 FROM messages WHERE guild_id=? AND author_id=? "
                    "AND ts>=? AND ts<? LIMIT 1",
                    (guild_id, uid, int(target_start), int(target_end)),
                ).fetchone()
                if active:
                    retained += 1
            rates[f"d{d}"] = _pct(retained, eligible) if eligible else None
        cohort_results.append({"label": label, "size": size, **rates})

    # Headline metrics: most recent cohort that has actually reached each checkpoint
    def _latest_reached(key: str) -> tuple[float | None, dict]:
        for c in reversed(cohort_results):
            val = c.get(key)
            if val is not None:
                return float(val), c  # type: ignore[arg-type]
        return None, {}

    d7, d7_cohort = _latest_reached("d7")
    d30, _ = _latest_reached("d30")
    d90, _ = _latest_reached("d90")

    badge = _badge(
        float(d7 if d7 is not None else 0),
        [(40, "critical"), (50, "needs_work"), (60, "healthy"), (100, "excellent")],
    ) if d7 is not None else "no_data"

    latest = cohort_results[-1] if cohort_results else {}

    return {
        "d7": d7,
        "d30": d30,
        "d90": d90,
        "d7_cohort_label": d7_cohort.get("label"),
        "badge": badge,
        "latest_cohort_size": latest.get("size", 0),
        "cohorts": cohort_results,
    }


# ---------------------------------------------------------------------------
# 9. Churn risk early warning
# ---------------------------------------------------------------------------


def compute_churn_risk(
    conn: sqlite3.Connection, guild_id: int, *, now: float | None = None
) -> dict:
    now = now or time.time()

    # Get all human current-members active in last 60 days
    sixty_days_ago = _ts(60, now=now)
    users = conn.execute(
        """
        SELECT DISTINCT m.author_id
        FROM messages m
        JOIN known_users ku ON ku.guild_id = m.guild_id AND ku.user_id = m.author_id
        WHERE m.guild_id=? AND m.ts>=?
          AND ku.is_bot = 0
          AND ku.current_member = 1
        """,
        (guild_id, sixty_days_ago),
    ).fetchall()
    user_ids = [r[0] for r in users]

    at_risk = []
    risk_distribution = [0] * 10  # 0-9, 10-19, ..., 90-100

    for uid in user_ids:
        # Signal 1: Frequency decline (30%) — compare last 7d vs previous 7d
        recent_msgs = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE guild_id=? AND author_id=? AND ts>=?",
            (guild_id, uid, _ts(7, now=now)),
        ).fetchone()[0]
        prev_msgs = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE guild_id=? AND author_id=? AND ts>=? AND ts<?",
            (guild_id, uid, _ts(14, now=now), _ts(7, now=now)),
        ).fetchone()[0]
        if prev_msgs > 0:
            freq_decline = max(0, 1 - recent_msgs / prev_msgs)
        elif recent_msgs == 0:
            freq_decline = 1.0
        else:
            freq_decline = 0.0

        # Signal 2: Channel narrowing (25%) — distinct channels last 7d vs previous 14d
        recent_chs = conn.execute(
            "SELECT COUNT(DISTINCT channel_id) FROM messages WHERE guild_id=? AND author_id=? AND ts>=?",
            (guild_id, uid, _ts(7, now=now)),
        ).fetchone()[0]
        prev_chs = conn.execute(
            "SELECT COUNT(DISTINCT channel_id) FROM messages WHERE guild_id=? AND author_id=? AND ts>=? AND ts<?",
            (guild_id, uid, _ts(21, now=now), _ts(7, now=now)),
        ).fetchone()[0]
        if prev_chs > 0:
            ch_narrow = max(0, 1 - recent_chs / prev_chs)
        elif recent_chs == 0:
            ch_narrow = 1.0
        else:
            ch_narrow = 0.0

        # Signal 3: Lost reciprocity (20%) — decline in inbound interactions
        recent_inbound = conn.execute(
            "SELECT COUNT(*) FROM user_interactions_log "
            "WHERE guild_id=? AND to_user_id=? AND ts>=?",
            (guild_id, uid, _ts(7, now=now)),
        ).fetchone()[0]
        prev_inbound = conn.execute(
            "SELECT COUNT(*) FROM user_interactions_log "
            "WHERE guild_id=? AND to_user_id=? AND ts>=? AND ts<?",
            (guild_id, uid, _ts(14, now=now), _ts(7, now=now)),
        ).fetchone()[0]
        if prev_inbound > 0:
            recip_loss = max(0, 1 - recent_inbound / prev_inbound)
        elif recent_inbound == 0:
            recip_loss = 1.0
        else:
            recip_loss = 0.0

        # Signal 4: Sentiment trend (15%)
        sent_row = conn.execute(
            "SELECT AVG(ms.sentiment) FROM message_sentiment ms "
            "JOIN messages m ON ms.message_id = m.message_id "
            "WHERE m.guild_id=? AND m.author_id=? AND m.ts>=?",
            (guild_id, uid, _ts(7, now=now)),
        ).fetchone()
        recent_sent = sent_row[0] if sent_row and sent_row[0] is not None else 0
        prev_sent_row = conn.execute(
            "SELECT AVG(ms.sentiment) FROM message_sentiment ms "
            "JOIN messages m ON ms.message_id = m.message_id "
            "WHERE m.guild_id=? AND m.author_id=? AND m.ts>=? AND m.ts<?",
            (guild_id, uid, _ts(14, now=now), _ts(7, now=now)),
        ).fetchone()
        prev_sent = (
            prev_sent_row[0] if prev_sent_row and prev_sent_row[0] is not None else 0
        )
        sent_decline = max(0, prev_sent - recent_sent) / 2  # normalize to 0-1 range

        # Signal 5: Visit gaps (10%) — longest gap in last 30d
        msg_ts_rows = conn.execute(
            "SELECT ts FROM messages WHERE guild_id=? AND author_id=? AND ts>=? ORDER BY ts",
            (guild_id, uid, _ts(30, now=now)),
        ).fetchall()
        max_gap = 0
        if len(msg_ts_rows) >= 2:
            for i in range(1, len(msg_ts_rows)):
                gap = msg_ts_rows[i]["ts"] - msg_ts_rows[i - 1]["ts"]
                max_gap = max(max_gap, gap)
        # Also gap from last message to now
        if msg_ts_rows:
            max_gap = max(max_gap, now - msg_ts_rows[-1]["ts"])
        gap_score = min(1.0, max_gap / (14 * _DAY))  # 14 day gap = 1.0

        # Composite score (0-100)
        score = round(
            (
                freq_decline * 0.30
                + ch_narrow * 0.25
                + recip_loss * 0.20
                + sent_decline * 0.15
                + gap_score * 0.10
            )
            * 100
        )
        score = min(100, max(0, score))

        bucket = min(9, score // 10)
        risk_distribution[bucket] += 1

        if score >= 30:
            tier = (
                "critical" if score >= 80 else "declining" if score >= 50 else "watch"
            )
            at_risk.append(
                {
                    "user_id": str(uid),
                    "score": score,
                    "tier": tier,
                    "signals": {
                        "frequency": round(freq_decline * 100),
                        "channels": round(ch_narrow * 100),
                        "reciprocity": round(recip_loss * 100),
                        "sentiment": round(sent_decline * 100),
                        "gap": round(gap_score * 100),
                    },
                    "last_seen": msg_ts_rows[-1]["ts"] if msg_ts_rows else 0,
                }
            )

    at_risk.sort(key=lambda x: x["score"], reverse=True)

    critical = sum(1 for r in at_risk if r["tier"] == "critical")
    declining = sum(1 for r in at_risk if r["tier"] == "declining")
    watch = sum(1 for r in at_risk if r["tier"] == "watch")

    badge = "clear" if not at_risk else "warning" if critical == 0 else "critical"

    return {
        "at_risk_count": len(at_risk),
        "badge": badge,
        "critical": critical,
        "declining": declining,
        "watch": watch,
        "at_risk": at_risk[:50],
        "risk_distribution": risk_distribution,
    }


# ---------------------------------------------------------------------------
# 10. Moderator workload
# ---------------------------------------------------------------------------


def compute_mod_workload(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    now: float | None = None,
    mod_ids: list[int] | None = None,
) -> dict:
    now = now or time.time()
    seven_days_ago = _ts(7, now=now)
    thirty_days_ago = _ts(30, now=now)

    # Build mod filter clause
    mod_set = set(mod_ids) if mod_ids else None
    if mod_set:
        placeholders = ",".join("?" for _ in mod_set)
        mod_filter = f" AND actor_id IN ({placeholders})"
        mod_filter_author = f" AND author_id IN ({placeholders})"
        mod_params = tuple(mod_set)
    else:
        mod_filter = ""
        mod_filter_author = ""
        mod_params = ()

    # Collect mod-related channel IDs: mod chat + ticket/jail/policy channels
    mod_channel_ids: set[int] = set()
    # mod_channel_id from config
    _mc_row = conn.execute(
        "SELECT value FROM config WHERE key='mod_channel_id'"
    ).fetchone()
    if _mc_row and _mc_row["value"] and _mc_row["value"] != "0":
        try:
            mod_channel_ids.add(int(_mc_row["value"]))
        except ValueError:
            pass
    # Active ticket channels
    for r in conn.execute(
        "SELECT channel_id FROM tickets WHERE guild_id=? AND channel_id>0",
        (guild_id,),
    ).fetchall():
        mod_channel_ids.add(r["channel_id"])
    # Active jail channels
    for r in conn.execute(
        "SELECT channel_id FROM jails WHERE guild_id=? AND channel_id>0",
        (guild_id,),
    ).fetchall():
        mod_channel_ids.add(r["channel_id"])
    # Policy ticket channels
    for r in conn.execute(
        "SELECT channel_id FROM policy_tickets WHERE guild_id=? AND channel_id>0",
        (guild_id,),
    ).fetchall():
        mod_channel_ids.add(r["channel_id"])

    # Total actions (7d) — mod-only when mod_ids provided
    total_actions = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE guild_id=? AND created_at>=?"
        + mod_filter,
        (guild_id, seven_days_ago) + mod_params,
    ).fetchone()[0]

    # Per-mod action counts (7d)
    mod_rows = conn.execute(
        "SELECT actor_id, COUNT(*) AS cnt FROM audit_log "
        "WHERE guild_id=? AND created_at>=?"
        + mod_filter
        + " GROUP BY actor_id ORDER BY cnt DESC",
        (guild_id, seven_days_ago) + mod_params,
    ).fetchall()
    action_by_mod: dict[int, int] = {r["actor_id"]: r["cnt"] for r in mod_rows}

    # Per-mod message counts in mod channels (7d)
    msg_by_mod: dict[int, int] = {}
    if mod_channel_ids:
        ch_placeholders = ",".join("?" for _ in mod_channel_ids)
        msg_rows = conn.execute(
            "SELECT author_id, COUNT(*) AS cnt FROM messages "
            "WHERE guild_id=? AND ts>=? AND channel_id IN ("
            + ch_placeholders
            + ")"
            + mod_filter_author
            + " GROUP BY author_id",
            (guild_id, seven_days_ago) + tuple(mod_channel_ids) + mod_params,
        ).fetchall()
        msg_by_mod = {r["author_id"]: r["cnt"] for r in msg_rows}

    # Merge: total activity = audit actions + mod-channel messages
    all_mod_ids = set(action_by_mod.keys()) | set(msg_by_mod.keys())
    mod_actions = []
    for mid in all_mod_ids:
        acts = action_by_mod.get(mid, 0)
        msgs = msg_by_mod.get(mid, 0)
        mod_actions.append(
            {
                "user_id": str(mid),
                "count": acts + msgs,
                "actions": acts,
                "messages": msgs,
            }
        )
    mod_actions.sort(key=lambda m: m["count"], reverse=True)  # type: ignore[arg-type,return-value]

    total_messages = sum(msg_by_mod.values())
    total_activity = total_actions + total_messages

    # Workload Gini
    action_counts: list[float] = [m["count"] for m in mod_actions]  # type: ignore[misc]
    workload_gini = round(_gini(action_counts), 3) if action_counts else 0

    # Response times: time from ticket open to first mod action on it
    ticket_rows = conn.execute(
        """SELECT t.id, t.created_at AS open_ts,
                  MIN(a.created_at) AS first_action_ts
           FROM tickets t
           LEFT JOIN audit_log a ON a.guild_id = t.guild_id
               AND a.action LIKE 'ticket_%' AND a.created_at > t.created_at
           WHERE t.guild_id=? AND t.created_at>=?
           GROUP BY t.id""",
        (guild_id, thirty_days_ago),
    ).fetchall()
    response_times = []
    for r in ticket_rows:
        if r["first_action_ts"] and r["open_ts"]:
            rt = (r["first_action_ts"] - r["open_ts"]) / 60  # minutes
            response_times.append(rt)

    median_rt = round(statistics.median(response_times), 1) if response_times else 0
    p95_rt = (
        round(sorted(response_times)[int(len(response_times) * 0.95)], 1)
        if len(response_times) >= 5
        else median_rt
    )

    # Action type breakdown (mod-only when filtered)
    type_rows = conn.execute(
        "SELECT action, COUNT(*) AS cnt FROM audit_log "
        "WHERE guild_id=? AND created_at>=?"
        + mod_filter
        + " GROUP BY action ORDER BY cnt DESC",
        (guild_id, seven_days_ago) + mod_params,
    ).fetchall()
    action_types = [{"action": r["action"], "count": r["cnt"]} for r in type_rows]

    # Escalation rate: warnings that lead to jails within 14 days
    warns_total = conn.execute(
        "SELECT COUNT(*) FROM warnings WHERE guild_id=? AND created_at>=?",
        (guild_id, thirty_days_ago),
    ).fetchone()[0]
    escalated = conn.execute(
        """SELECT COUNT(*) FROM warnings w
           WHERE w.guild_id=? AND w.created_at>=?
           AND EXISTS (
               SELECT 1 FROM jails j WHERE j.guild_id=w.guild_id
               AND j.user_id=w.user_id AND j.created_at > w.created_at
               AND j.created_at <= w.created_at + ?
           )""",
        (guild_id, thirty_days_ago, 14 * _DAY),
    ).fetchone()[0]
    escalation_rate = _pct(escalated, warns_total) if warns_total else 0

    # Recidivism rate: warned users who get another warning within 14 days
    recid = conn.execute(
        """SELECT COUNT(DISTINCT w1.user_id) FROM warnings w1
           WHERE w1.guild_id=? AND w1.created_at>=?
           AND EXISTS (
               SELECT 1 FROM warnings w2
               WHERE w2.guild_id=w1.guild_id AND w2.user_id=w1.user_id
               AND w2.created_at > w1.created_at
               AND w2.created_at <= w1.created_at + ?
           )""",
        (guild_id, thirty_days_ago, 14 * _DAY),
    ).fetchone()[0]
    warned_users = conn.execute(
        "SELECT COUNT(DISTINCT user_id) FROM warnings WHERE guild_id=? AND created_at>=?",
        (guild_id, thirty_days_ago),
    ).fetchone()[0]
    recidivism_rate = _pct(recid, warned_users) if warned_users else 0

    badge = _badge(
        median_rt,
        [(3, "excellent"), (5, "healthy"), (10, "needs_work"), (9999, "critical")],
    )

    return {
        "median_response_time": median_rt,
        "p95_response_time": p95_rt,
        "badge": badge,
        "total_actions_7d": total_activity,
        "total_audit_actions_7d": total_actions,
        "total_messages_7d": total_messages,
        "workload_gini": workload_gini,
        "mod_actions": mod_actions,
        "action_types": action_types,
        "escalation_rate": escalation_rate,
        "recidivism_rate": recidivism_rate,
    }


# ---------------------------------------------------------------------------
# 11. Incident detection (reads from incident_events + baselines)
# ---------------------------------------------------------------------------


def compute_incidents(
    conn: sqlite3.Connection, guild_id: int, *, now: float | None = None
) -> dict:
    now = now or time.time()
    seven_days_ago = _ts(7, now=now)

    # Active (unresolved) incidents
    active = conn.execute(
        "SELECT COUNT(*) FROM incident_events WHERE guild_id=? AND resolved_at IS NULL",
        (guild_id,),
    ).fetchone()[0]

    # 7-day incident log
    log_rows = conn.execute(
        """SELECT id, event_type, severity, channel_id, details_json,
                  detected_at, resolved_at, resolved_by
           FROM incident_events WHERE guild_id=? AND detected_at>=?
           ORDER BY detected_at DESC LIMIT 50""",
        (guild_id, seven_days_ago),
    ).fetchall()
    incident_log = [
        {
            "id": r["id"],
            "type": r["event_type"],
            "severity": r["severity"],
            "channel_id": str(r["channel_id"]) if r["channel_id"] else None,
            "detected_at": r["detected_at"],
            "resolved_at": r["resolved_at"],
            "duration_min": round((r["resolved_at"] - r["detected_at"]) / 60, 1)
            if r["resolved_at"]
            else None,
        }
        for r in log_rows
    ]

    # Alert category counts (7d)
    cat_rows = conn.execute(
        "SELECT event_type, COUNT(*) AS cnt FROM incident_events "
        "WHERE guild_id=? AND detected_at>=? GROUP BY event_type",
        (guild_id, seven_days_ago),
    ).fetchall()
    categories = {r["event_type"]: r["cnt"] for r in cat_rows}

    # 7-day timeline (daily incident counts)
    timeline = []
    for d in range(6, -1, -1):
        day_start = _ts(d + 1, now=now)
        day_end = _ts(d, now=now)
        cnt = conn.execute(
            "SELECT COUNT(*) FROM incident_events WHERE guild_id=? AND detected_at>=? AND detected_at<?",
            (guild_id, day_start, day_end),
        ).fetchone()[0]
        timeline.append(cnt)

    badge = "clear" if active == 0 else "active"

    return {
        "active_count": active,
        "badge": badge,
        "incident_log": incident_log,
        "categories": categories,
        "timeline": timeline,
    }


# ---------------------------------------------------------------------------
# 12. Composite health score
# ---------------------------------------------------------------------------


def compute_composite_health(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    dau_mau_data: dict | None = None,
    gini_data: dict | None = None,
    social_data: dict | None = None,
    sentiment_data: dict | None = None,
    retention_data: dict | None = None,
    heatmap_data: dict | None = None,
) -> dict:
    """Compute the composite health score from the other tiles' data.
    Callers should pass pre-computed tile data where available."""

    def _dim_score(value, healthy_range, weight):
        """Normalize a metric value to 0-100 based on healthy range."""
        lo, hi = healthy_range
        if hi == lo:
            return 50
        normalized = (value - lo) / (hi - lo)
        return max(0, min(100, round(normalized * 100)))

    # Activity (20%) — based on DAU/MAU ratio
    dau_mau_ratio = (dau_mau_data or {}).get("dau_mau", 0)
    activity_score = min(100, round(dau_mau_ratio * 2.5))  # 40% = 100

    # Engagement (20%) — based on heatmap activity consistency
    dead_hours = (heatmap_data or {}).get("dead_hours", 168)
    engagement_score = max(0, min(100, round((1 - dead_hours / 168) * 100)))

    # Distribution (15%) — based on inverse Gini
    gini_val = (gini_data or {}).get("gini", 0.85)
    distribution_score = max(
        0, min(100, round((1 - gini_val) * 150))
    )  # 0.33 gini = 100

    # Network (15%) — based on clustering coefficient
    clustering = (social_data or {}).get("clustering_coefficient", 0)
    network_score = min(100, round(clustering * 200))  # 0.5 = 100

    # Retention (15%) — based on D7 retention
    d7 = (retention_data or {}).get("d7") or 0
    retention_score = min(100, round(d7 * 1.25))  # 80% = 100

    # Sentiment (15%) — based on avg sentiment
    avg_sent = (sentiment_data or {}).get("avg_sentiment", 0)
    sentiment_score = max(
        0, min(100, round((avg_sent + 0.5) * 100))
    )  # -0.5 to +0.5 -> 0-100

    # Weighted composite
    composite = round(
        activity_score * 0.20
        + engagement_score * 0.20
        + distribution_score * 0.15
        + network_score * 0.15
        + retention_score * 0.15
        + sentiment_score * 0.15
    )

    dimensions = [
        {"name": "Activity", "score": activity_score, "weight": 20},
        {"name": "Engagement", "score": engagement_score, "weight": 20},
        {"name": "Distribution", "score": distribution_score, "weight": 15},
        {"name": "Network", "score": network_score, "weight": 15},
        {"name": "Retention", "score": retention_score, "weight": 15},
        {"name": "Sentiment", "score": sentiment_score, "weight": 15},
    ]

    # Recommendations: weakest dimensions first
    sorted_dims = sorted(dimensions, key=lambda d: d["score"])
    recommendations = []
    interventions = {
        "Activity": "Schedule events during peak hours identified in the heatmap to boost daily return rate.",
        "Engagement": "Reduce dead hours by scheduling discussion prompts during quiet periods.",
        "Distribution": "Create structured activities (game nights, Q&A) that encourage broader participation.",
        "Network": "Pair new members with mentors to accelerate relationship formation.",
        "Retention": "Implement personal DM outreach for members who haven't returned after 7 days.",
        "Sentiment": "Review the negative spike log and address recurring sources of friction.",
    }
    for dim in sorted_dims[:3]:
        if dim["score"] < 80:
            recommendations.append(
                {
                    "dimension": dim["name"],
                    "score": dim["score"],
                    "action": interventions.get(dim["name"], ""),
                    "estimated_impact": round(
                        (80 - dim["score"]) * dim["weight"] / 100, 1
                    ),
                }
            )

    if composite >= 80:
        badge = "excellent"
    elif composite >= 60:
        badge = "healthy"
    elif composite >= 40:
        badge = "needs_work"
    else:
        badge = "critical"

    return {
        "score": composite,
        "badge": badge,
        "dimensions": dimensions,
        "recommendations": recommendations,
    }
