"""Bank Manager Statistics — the on-demand aggregation layer (spec §9).

Assembles the Statistics page payload from the live economy tables for a single
guild: currency supply and its inequality, the balance distribution, trailing
7-day flow (mint / burn / transfers / grants), a per-member earning table, guild
engagement, the biggest transfer pairs, and perk affordability. Pure math lives
in ``economy.stats``; the weekly-rollup precedent is ``economy_metrics_service``.

This is an admin/manager endpoint, not a hot path — clarity over
micro-optimization — but member rows are built from a handful of ``GROUP BY``
aggregates, never a per-member query loop.

**Scope: members active in the last 30 days.** Every figure here is restricted
to members whose ``member_activity.last_message_at`` falls inside the trailing
30-day window (the same population :func:`active_member_ids` reports as
``active_members``). A member who has gone quiet or left the guild keeps a wallet
and a ledger history, but counting them would let a departed whale inflate
supply and Gini, pad the member table with dead accounts, and distort the flow /
income / spender boards — so they are filtered out everywhere, not just from the
engagement counts. The active set is materialized once into a temp table
(:func:`_load_active_members`) and every per-user query joins to it.

Windows are trailing epoch spans from ``now`` (``now - 7·86400`` / ``now -
30·86400``); no timezone math is needed for a trailing window. Money definitions
match the weekly rollup: mint / income exclude ``transfer_in``, burn excludes
``transfer_out`` (a transfer moves currency, it does not mint or burn).
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from typing import TYPE_CHECKING

from bot_modules.economy import stats
from bot_modules.economy.metrics import FAUCET_GROUP_NAMES, FAUCET_GROUPS
from bot_modules.services.economy_quests_service import active_member_ids

if TYPE_CHECKING:
    from bot_modules.services.economy_service import EconSettings

_DAY = 86400.0

# How many trailing 7-day buckets the income-source stacked bar spans. Eight
# weeks is enough to see a trend without the chart turning into a hairline forest.
_INCOME_WEEKS = 8

# Restricts a per-user query to the 30-day-active population. Appended inside a
# WHERE that already keys on ``user_id`` (see :func:`_load_active_members` for
# the temp table this joins to). No bind params, so it composes with the
# positional placeholders the queries already carry.
_ACTIVE = " AND user_id IN (SELECT user_id FROM _stats_active_members)"


def _load_active_members(
    conn: sqlite3.Connection, guild_id: int
) -> frozenset[int]:
    """Materialize the 30-day-active member set into a temp table + return it.

    Every figure on the page is scoped to this population (module docstring).
    Using a temp table lets each query ``JOIN``/``IN`` against it rather than
    binding an unbounded ``IN (?, ?, …)`` list, and keeps the active set as the
    single source of truth the ``active_members`` count already reports. The
    returned set is for the handful of Python-side filters (transfer pairs)
    where the recipient id lives in JSON rather than a column.
    """
    ids = active_member_ids(conn, guild_id, days=30)
    conn.execute("DROP TABLE IF EXISTS _stats_active_members")
    conn.execute(
        "CREATE TEMP TABLE _stats_active_members (user_id INTEGER PRIMARY KEY)"
    )
    conn.executemany(
        "INSERT OR IGNORE INTO _stats_active_members (user_id) VALUES (?)",
        [(uid,) for uid in ids],
    )
    return frozenset(ids)


def compute_stats(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    *,
    now: float,
    member_limit: int = 100,
) -> dict:
    """Assemble the full Statistics payload for ``guild_id`` as of ``now``.

    See the module docstring for window and money conventions. ``member_limit``
    caps the member table (top holders by balance).
    """
    cut7 = now - 7 * _DAY
    cut30 = now - 30 * _DAY

    # Scope every figure below to the 30-day-active population (module docstring).
    active = _load_active_members(conn, guild_id)

    supply = _supply(conn, guild_id)
    positive_balances = _positive_balances(conn, guild_id)

    return {
        "supply": supply,
        "distribution": stats.balance_histogram(positive_balances),
        "flow_7d": _flow(conn, guild_id, cut7),
        "income_sources": _income_sources(conn, guild_id, now),
        "members": _members(conn, guild_id, cut7, cut30, member_limit),
        "engagement": _engagement(conn, settings, guild_id, cut7, cut30, supply),
        "transfers_top": _transfers_top(conn, guild_id, cut30, active),
        "burn_top": _burn_top(conn, guild_id),
        "affordability": _affordability(conn, settings, guild_id, cut7),
    }


# ── income sources (stacked bar over trailing weeks) ───────────────────


def _income_sources(conn: sqlite3.Connection, guild_id: int, now: float) -> dict:
    """Minted coins per faucet group over the last :data:`_INCOME_WEEKS`
    trailing 7-day buckets — the composition of income over time.

    Buckets are trailing epoch spans (no timezone math, per the module note):
    bucket ``i`` (oldest first) covers ``[now - (n-i)·7d, now - (n-i-1)·7d)``.
    Money follows the mint convention: positive credits excluding ``transfer_in``.
    Kinds outside :data:`FAUCET_GROUPS` cannot mint, so they never appear.
    """
    span = 7 * _DAY
    cut = now - _INCOME_WEEKS * span
    # totals[bucket_index_from_recent][group] = coins. bucket 0 = most recent.
    totals: list[dict[str, int]] = [
        dict.fromkeys(FAUCET_GROUP_NAMES, 0) for _ in range(_INCOME_WEEKS)
    ]
    for r in conn.execute(
        "SELECT kind, CAST((? - created_at) / ? AS INTEGER) AS bucket, "
        "SUM(amount) AS s FROM econ_ledger "
        "WHERE guild_id = ? AND amount > 0 AND kind != 'transfer_in' "
        "AND created_at >= ?" + _ACTIVE + " GROUP BY bucket, kind",
        (now, span, guild_id, cut),
    ):
        b = int(r["bucket"])
        group = FAUCET_GROUPS.get(str(r["kind"]))
        # A credit exactly at ``cut`` lands in bucket _INCOME_WEEKS (out of range);
        # clamp it into the oldest bucket rather than dropping it.
        if group is None:
            continue
        b = min(b, _INCOME_WEEKS - 1)
        totals[b][group] += int(r["s"])

    # Emit oldest-first so the chart reads left→right in time.
    buckets = []
    for i in range(_INCOME_WEEKS - 1, -1, -1):
        by_group = totals[i]
        buckets.append({
            "start": now - (i + 1) * span,
            "totals": by_group,
            "total": sum(by_group.values()),
        })
    return {"groups": list(FAUCET_GROUP_NAMES), "buckets": buckets}


def _burn_top(
    conn: sqlite3.Connection, guild_id: int, limit: int = 15
) -> list[stats.BurnRow]:
    """Lifetime biggest spenders. Deliberately all-time, not a trailing window.

    The point of showing this is to make spending itself a status worth
    chasing, and a 7-day window would erase that standing every week. The window
    on *time* is still lifetime; the page-wide 30-day-active scope only drops
    members who have since gone quiet or left (module docstring).
    """
    by_user: dict[int, dict[str, int]] = {}
    placeholders = ", ".join("?" for _ in stats.BURN_EXCLUDED_KINDS)
    for r in conn.execute(
        "SELECT user_id, kind, SUM(-amount) AS s FROM econ_ledger "
        f"WHERE guild_id = ? AND amount < 0 AND kind NOT IN ({placeholders})"
        + _ACTIVE + " GROUP BY user_id, kind",
        (guild_id, *stats.BURN_EXCLUDED_KINDS),
    ):
        by_user.setdefault(int(r["user_id"]), {})[str(r["kind"])] = int(r["s"])
    return stats.burn_leaderboard(by_user, limit)


# ── supply / distribution ──────────────────────────────────────────────


def _positive_balances(conn: sqlite3.Connection, guild_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT balance FROM econ_wallets "
        "WHERE guild_id = ? AND balance > 0" + _ACTIVE,
        (guild_id,),
    ).fetchall()
    return [int(r["balance"]) for r in rows]


def _supply(conn: sqlite3.Connection, guild_id: int) -> dict:
    total = int(
        conn.execute(
            "SELECT COALESCE(SUM(balance), 0) FROM econ_wallets "
            "WHERE guild_id = ?" + _ACTIVE,
            (guild_id,),
        ).fetchone()[0]
    )
    balances = _positive_balances(conn, guild_id)
    holders = len(balances)
    median_balance = int(statistics.median(balances)) if balances else 0
    return {
        "total": total,
        "holders": holders,
        "median_balance": median_balance,
        "top10_share": stats.top_share(balances, 0.1),
        "gini": stats.gini(balances),
    }


# ── flow ───────────────────────────────────────────────────────────────


def _flow(conn: sqlite3.Connection, guild_id: int, cut7: float) -> dict:
    minted = int(
        conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM econ_ledger "
            "WHERE guild_id = ? AND created_at >= ? "
            "AND amount > 0 AND kind != 'transfer_in'" + _ACTIVE,
            (guild_id, cut7),
        ).fetchone()[0]
    )
    burned = int(
        conn.execute(
            "SELECT COALESCE(SUM(-amount), 0) FROM econ_ledger "
            "WHERE guild_id = ? AND created_at >= ? "
            "AND amount < 0 AND kind != 'transfer_out'" + _ACTIVE,
            (guild_id, cut7),
        ).fetchone()[0]
    )
    transfer_volume = int(
        conn.execute(
            "SELECT COALESCE(SUM(-amount), 0) FROM econ_ledger "
            "WHERE guild_id = ? AND created_at >= ? AND kind = 'transfer_out'"
            + _ACTIVE,
            (guild_id, cut7),
        ).fetchone()[0]
    )
    grants = int(
        conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM econ_ledger "
            "WHERE guild_id = ? AND created_at >= ? AND kind = 'grant' "
            "AND amount > 0" + _ACTIVE,
            (guild_id, cut7),
        ).fetchone()[0]
    )
    return {
        "minted": minted,
        "burned": burned,
        "burn_rate": (burned / minted) if minted > 0 else 0,
        "transfer_volume": transfer_volume,
        "grants": grants,
    }


# ── members ────────────────────────────────────────────────────────────


def _members(
    conn: sqlite3.Connection,
    guild_id: int,
    cut7: float,
    cut30: float,
    member_limit: int,
) -> list[dict]:
    limit = min(max(member_limit, 1), 500)
    # Only the 30-day-active top holders make the table; the windowed aggregates
    # below are read solely for these ids, so filtering here scopes them too.
    top = conn.execute(
        "SELECT user_id, balance FROM econ_wallets "
        "WHERE guild_id = ? AND balance > 0" + _ACTIVE
        + " ORDER BY balance DESC, user_id ASC LIMIT ?",
        (guild_id, limit),
    ).fetchall()
    if not top:
        return []

    # Aggregate the windowed ledger once per metric, then join to the top
    # holders in Python — no per-member query loop.
    income_7d: dict[int, int] = {}
    for r in conn.execute(
        "SELECT user_id, SUM(amount) AS s FROM econ_ledger "
        "WHERE guild_id = ? AND created_at >= ? AND amount > 0 "
        "AND kind != 'transfer_in' GROUP BY user_id",
        (guild_id, cut7),
    ):
        income_7d[int(r["user_id"])] = int(r["s"])

    # 30d income per (user, faucet group) → income_30d + top_faucet.
    income_30d: dict[int, int] = {}
    group_totals: dict[int, dict[str, int]] = {}
    for r in conn.execute(
        "SELECT user_id, kind, SUM(amount) AS s FROM econ_ledger "
        "WHERE guild_id = ? AND created_at >= ? AND amount > 0 "
        "AND kind != 'transfer_in' GROUP BY user_id, kind",
        (guild_id, cut30),
    ):
        uid = int(r["user_id"])
        amt = int(r["s"])
        income_30d[uid] = income_30d.get(uid, 0) + amt
        group = FAUCET_GROUPS.get(str(r["kind"]))
        if group is not None:
            group_totals.setdefault(uid, {})[group] = (
                group_totals.get(uid, {}).get(group, 0) + amt
            )

    # Every sink, not just rentals — this column read 'rental' only, which
    # silently under-reported the moment one-shot consumables shipped.
    spent_7d: dict[int, int] = {}
    burn_placeholders = ", ".join("?" for _ in stats.BURN_EXCLUDED_KINDS)
    for r in conn.execute(
        "SELECT user_id, SUM(-amount) AS s FROM econ_ledger "
        "WHERE guild_id = ? AND created_at >= ? AND amount < 0 "
        f"AND kind NOT IN ({burn_placeholders}) GROUP BY user_id",
        (guild_id, cut7, *stats.BURN_EXCLUDED_KINDS),
    ):
        spent_7d[int(r["user_id"])] = int(r["s"])

    last_earned: dict[int, float] = {}
    for r in conn.execute(
        "SELECT user_id, MAX(created_at) AS t FROM econ_ledger "
        "WHERE guild_id = ? AND amount > 0 AND kind != 'transfer_in' "
        "GROUP BY user_id",
        (guild_id,),
    ):
        last_earned[int(r["user_id"])] = float(r["t"])

    rentals_live: dict[int, int] = {}
    for r in conn.execute(
        "SELECT user_id, COUNT(*) AS c FROM econ_rentals "
        "WHERE guild_id = ? AND state IN ('active', 'grace') GROUP BY user_id",
        (guild_id,),
    ):
        rentals_live[int(r["user_id"])] = int(r["c"])

    streaks: dict[int, int] = {}
    for r in conn.execute(
        "SELECT user_id, current_streak FROM econ_streaks WHERE guild_id = ?",
        (guild_id,),
    ):
        streaks[int(r["user_id"])] = int(r["current_streak"])

    out = []
    for row in top:
        uid = int(row["user_id"])
        groups = group_totals.get(uid, {})
        top_faucet = max(groups, key=lambda g: groups[g]) if groups else None
        inc7 = income_7d.get(uid, 0)
        out.append(
            {
                "user_id": str(uid),
                "balance": int(row["balance"]),
                "income_7d": inc7,
                "income_30d": income_30d.get(uid, 0),
                "coins_per_day_7d": round(inc7 / 7, 1),
                "spent_7d": spent_7d.get(uid, 0),
                "top_faucet": top_faucet,
                "rentals_live": rentals_live.get(uid, 0),
                "streak": streaks.get(uid, 0),
                "last_earned_at": last_earned.get(uid),
            }
        )
    return out


# ── engagement ─────────────────────────────────────────────────────────


def _engagement(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    cut7: float,
    cut30: float,
    supply: dict,
) -> dict:
    del settings  # reserved; engagement math is settings-independent
    active_members = len(active_member_ids(conn, guild_id, days=30))
    earners_7d = int(
        conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM econ_ledger "
            "WHERE guild_id = ? AND created_at >= ? AND amount > 0 "
            "AND kind != 'transfer_in'" + _ACTIVE,
            (guild_id, cut7),
        ).fetchone()[0]
    )
    spenders_7d = int(
        conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM econ_ledger "
            "WHERE guild_id = ? AND created_at >= ? AND kind = 'rental' "
            "AND amount < 0" + _ACTIVE,
            (guild_id, cut7),
        ).fetchone()[0]
    )
    quest_claims_7d = int(
        conn.execute(
            "SELECT COUNT(*) FROM econ_quest_claims "
            "WHERE guild_id = ? AND created_at >= ?" + _ACTIVE,
            (guild_id, cut7),
        ).fetchone()[0]
    )
    resolved = conn.execute(
        "SELECT "
        "SUM(CASE WHEN state = 'paid' THEN 1 ELSE 0 END) AS paid, "
        "SUM(CASE WHEN state = 'denied' THEN 1 ELSE 0 END) AS denied "
        "FROM econ_quest_claims "
        "WHERE guild_id = ? AND resolved_at IS NOT NULL AND resolved_at >= ? "
        "AND state IN ('paid', 'denied')" + _ACTIVE,
        (guild_id, cut30),
    ).fetchone()
    paid = int(resolved["paid"] or 0)
    denied = int(resolved["denied"] or 0)
    approval = (paid / (paid + denied)) if (paid + denied) > 0 else None

    return {
        "active_members": active_members,
        "earners_7d": earners_7d,
        "earner_ratio": (earners_7d / active_members) if active_members > 0 else 0,
        "spenders_7d": spenders_7d,
        "quest_claims_7d": quest_claims_7d,
        "quest_approval_rate_30d": approval,
        "hoard_weeks": _hoard_weeks(conn, guild_id, supply["median_balance"]),
    }


def _hoard_weeks(
    conn: sqlite3.Connection, guild_id: int, median_balance: int
) -> float | None:
    """Median balance ÷ median weekly income from the latest rollup.

    None when there is no rollup row yet or its median income is 0 (no basis).
    """
    row = conn.execute(
        "SELECT median_income FROM econ_metrics_weekly "
        "WHERE guild_id = ? ORDER BY iso_week DESC LIMIT 1",
        (guild_id,),
    ).fetchone()
    if row is None or row["median_income"] is None:
        return None
    weekly = float(row["median_income"])
    if weekly <= 0:
        return None
    return round(median_balance / weekly, 1)


# ── transfers ──────────────────────────────────────────────────────────


def _transfers_top(
    conn: sqlite3.Connection,
    guild_id: int,
    cut30: float,
    active: frozenset[int],
) -> list[dict]:
    """Top 5 (from, to) pairs by 30d transfer_out magnitude. Recipient comes
    from the ``transfer_out`` meta ``{"to": ...}``; rows with missing/malformed
    meta are skipped rather than crashing the aggregation.

    Both parties must be 30-day-active (page-wide scope): the sender is filtered
    in SQL, and the recipient — whose id lives in the JSON meta, not a column —
    against ``active`` here."""
    pairs: dict[tuple[int, int], int] = {}
    for r in conn.execute(
        "SELECT user_id, amount, meta FROM econ_ledger "
        "WHERE guild_id = ? AND created_at >= ? AND kind = 'transfer_out'"
        + _ACTIVE,
        (guild_id, cut30),
    ):
        raw = r["meta"]
        if not raw:
            continue
        try:
            to_id = int(json.loads(raw)["to"])
        except (ValueError, TypeError, KeyError):
            continue
        if to_id not in active:
            continue
        key = (int(r["user_id"]), to_id)
        pairs[key] = pairs.get(key, 0) + int(-r["amount"])
    ranked = sorted(pairs.items(), key=lambda kv: kv[1], reverse=True)[:5]
    return [
        {"from_id": str(f), "to_id": str(t), "total": total}
        for (f, t), total in ranked
    ]


# ── affordability ──────────────────────────────────────────────────────


def _affordability(
    conn: sqlite3.Connection, settings: EconSettings, guild_id: int, cut7: float
) -> dict[str, float]:
    """Days-of-median-income per perk price. The median daily income is taken
    over 7d earners (their 7d income ÷ 7); 0 earners → ``{}`` (via the helper)."""
    rows = conn.execute(
        "SELECT user_id, SUM(amount) AS s FROM econ_ledger "
        "WHERE guild_id = ? AND created_at >= ? AND amount > 0 "
        "AND kind != 'transfer_in'" + _ACTIVE
        + " GROUP BY user_id HAVING s > 0",
        (guild_id, cut7),
    ).fetchall()
    dailies = [float(r["s"]) / 7 for r in rows]
    median_daily = statistics.median(dailies) if dailies else 0.0
    return stats.affordability(median_daily, settings)


# ── live "happening now" tracker (quest-variety stage 4) ──────────────


def compute_live(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    now: float,
) -> dict:
    """The Statistics page's "Happening now" payload — quest pulse, not money.

    Anonymous by design (2026-07-18 decision): aggregates and counts only,
    never member names — the community hero card, per-quest completion
    counts for the current period of each cadence, event-quest totals, and
    the day/week countdowns. Cheap single-pass queries; refreshed on a
    30-60s interval by the panel.
    """
    from datetime import date, timedelta

    from bot_modules.core.db_utils import get_tz_offset_hours
    from bot_modules.economy import quests as quest_rules
    from bot_modules.economy.logic import local_day_bounds, local_day_for

    offset = get_tz_offset_hours(conn, guild_id)
    today = local_day_for(now, offset)
    day_obj = date.fromisoformat(today)
    periods = {
        "daily": quest_rules.quest_period("daily", today),
        "weekly": quest_rules.quest_period("weekly", today),
        "monthly": quest_rules.quest_period("monthly", today),
    }

    # Countdowns: next guild-local midnight, next ISO-week start.
    _start, day_end = local_day_bounds(today, offset)
    next_monday = day_obj + timedelta(days=7 - day_obj.weekday())
    week_end, _ = local_day_bounds(next_monday.isoformat(), offset)

    # Community hero: the running auto weekly (or none = gap week).
    community = []
    for q in conn.execute(
        """
        SELECT q.id, q.title, q.trigger_kind, q.reward, q.community_target,
               p.current, p.completed_at
        FROM econ_quests q
        LEFT JOIN econ_community_progress p ON p.quest_id = q.id
        WHERE q.guild_id = ? AND q.qtype = 'community' AND q.active = 1
          AND q.trigger_kind != ''
        """,
        (guild_id,),
    ):
        target = int(q["community_target"] or 0)
        current = int(q["current"] or 0)
        contributors = conn.execute(
            "SELECT COUNT(*) AS n FROM econ_community_contrib "
            "WHERE quest_id = ? AND count > 0",
            (int(q["id"]),),
        ).fetchone()["n"]
        # Pace on daily buckets: expected = target × elapsed fraction of the
        # ISO week; "push" under 90% of that. Sub-day noise is deliberate —
        # a small server has dead hours.
        week_start_day = day_obj - timedelta(days=day_obj.weekday())
        elapsed_days = max(1, (day_obj - week_start_day).days + 1)
        expected = target * elapsed_days / 7 if target else 0
        community.append({
            "title": q["title"],
            "kind": q["trigger_kind"],
            "kind_label": quest_rules.TRIGGER_KINDS.get(
                str(q["trigger_kind"]), str(q["trigger_kind"])
            ),
            "current": current,
            "target": target,
            "pct": round(100 * current / target) if target else 0,
            "tiers_crossed": quest_rules.community_tiers_crossed(current, target),
            "contributors": int(contributors),
            "reward_per_tier": int(q["reward"]),
            "on_track": bool(expected == 0 or current >= 0.9 * expected),
            "completed": q["completed_at"] is not None,
        })

    # Per-cadence pulse: paid completions + counted quests in flight for the
    # CURRENT period of each active board quest.
    cadences: dict[str, list[dict]] = {"daily": [], "weekly": [], "monthly": []}
    events: list[dict] = []
    for q in conn.execute(
        "SELECT id, title, qtype, trigger_kind, target_count, target_min, "
        "target_max FROM econ_quests "
        "WHERE guild_id = ? AND active = 1 AND qtype != 'community' "
        "ORDER BY qtype, id",
        (guild_id,),
    ):
        qid = int(q["id"])
        if q["qtype"] == "event":
            paid = conn.execute(
                "SELECT COUNT(*) AS n FROM econ_quest_claims "
                "WHERE quest_id = ? AND state = 'paid'",
                (qid,),
            ).fetchone()["n"]
            paid_week = conn.execute(
                "SELECT COUNT(*) AS n FROM econ_quest_claims "
                "WHERE quest_id = ? AND state = 'paid' AND created_at >= ?",
                (qid, now - 7 * 86400.0),
            ).fetchone()["n"]
            events.append({
                "title": q["title"], "kind": q["trigger_kind"],
                "paid_total": int(paid), "paid_7d": int(paid_week),
            })
            continue
        period = periods[str(q["qtype"])]
        paid = conn.execute(
            "SELECT COUNT(*) AS n FROM econ_quest_claims "
            "WHERE quest_id = ? AND state = 'paid' AND period = ?",
            (qid, period),
        ).fetchone()["n"]
        in_flight = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(current), 0) AS s "
            "FROM econ_quest_progress WHERE quest_id = ? AND period = ?",
            (qid, period),
        ).fetchone()
        cadences[str(q["qtype"])].append({
            "title": q["title"],
            "kind": q["trigger_kind"] or None,
            "counted": int(q["target_count"]) > 1 or int(q["target_max"]) > 0,
            "completed": int(paid),
            "in_progress": int(in_flight["n"]),
        })

    # Anonymous ticker aggregates: completions today / this ISO week.
    day_start, _ = local_day_bounds(today, offset)
    week_start_day = day_obj - timedelta(days=day_obj.weekday())
    ws_ts, _ = local_day_bounds(week_start_day.isoformat(), offset)
    paid_today = conn.execute(
        "SELECT COUNT(*) AS n FROM econ_quest_claims c "
        "JOIN econ_quests q ON q.id = c.quest_id "
        "WHERE q.guild_id = ? AND c.state = 'paid' AND c.created_at >= ?",
        (guild_id, day_start),
    ).fetchone()["n"]
    paid_week = conn.execute(
        "SELECT COUNT(*) AS n FROM econ_quest_claims c "
        "JOIN econ_quests q ON q.id = c.quest_id "
        "WHERE q.guild_id = ? AND c.state = 'paid' AND c.created_at >= ?",
        (guild_id, ws_ts),
    ).fetchone()["n"]

    from bot_modules.services.economy_quests_service import spotlight_kind

    spot = spotlight_kind(conn, guild_id, quest_rules.iso_week_for(today))
    return {
        "community": community,
        "cadences": cadences,
        "events": events,
        "completions_today": int(paid_today),
        "completions_week": int(paid_week),
        "seconds_to_day_roll": max(0, round(day_end - now)),
        "seconds_to_week_roll": max(0, round(week_end - now)),
        "spotlight_kind": spot,
        "spotlight_label": (
            quest_rules.TRIGGER_KINDS.get(spot, spot) if spot else None
        ),
    }
