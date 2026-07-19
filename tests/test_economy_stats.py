"""Tests for the Bank Manager Statistics page — pure math (economy.stats) and
the DB-touching ``compute_stats`` assembly (economy_stats_service).

Pure: gini against known values (all-equal / single / empty / skewed), the
top-share fraction, histogram bucket edges, and affordability (including the
zero-income short-circuit). Service: a seeded guild rolled up end-to-end
asserting every top-level key — trailing-window edges (an 8-day-old row is in
30d but not 7d), transfers excluded from mint/burn, top-faucet grouping,
approval-rate null/value cases, and hoard_weeks with and without a rollup row.
"""

from __future__ import annotations

import json

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.economy import stats
from bot_modules.services.economy_service import EconSettings
from bot_modules.services.economy_stats_service import compute_stats
from migrations import apply_migrations_sync

GUILD = 900
NOW = 1_800_000_000.0
DAY = 86400.0
SETTINGS = EconSettings(
    enabled=True,
    price_role_color=50,
    price_role_name=35,
    price_role_icon=75,
    price_role_gradient=120,
    price_gift_color=50,
    price_text_room=200,
    price_voice_room=200,
)


# ── pure: gini ─────────────────────────────────────────────────────────


def test_gini_known_skewed():
    # Anchored discriminators: descending-sort / 0-index bugs miss these.
    assert stats.gini([1, 2, 3, 4, 5]) == pytest.approx(4 / 15)
    assert stats.gini([0, 100]) == pytest.approx(0.5)


def test_gini_all_equal_single_empty_zero():
    assert stats.gini([5, 5, 5]) == 0.0
    assert stats.gini([7]) == 0.0
    assert stats.gini([]) == 0.0
    assert stats.gini([0, 0, 0]) == 0.0  # total zero → no inequality


def test_gini_max_inequality_approaches_one():
    # One holder owns everything; approaches (n-1)/n.
    g = stats.gini([0, 0, 0, 0, 1000])
    assert g == pytest.approx(0.8)


# ── pure: top_share ────────────────────────────────────────────────────


def test_top_share_default_decile():
    # n=10, fraction 0.1 → top 1 of 55 total.
    assert stats.top_share(list(range(1, 11))) == pytest.approx(10 / 55)


def test_top_share_rounds_count_up_and_clamps():
    # n=5, ceil(0.5)=1 → largest / total.
    assert stats.top_share([1, 2, 3, 4, 5]) == pytest.approx(5 / 15)
    # fraction >= 1 takes everyone → share 1.0.
    assert stats.top_share([1, 2, 3], fraction=1.0) == pytest.approx(1.0)


def test_top_share_empty_and_zero_total():
    assert stats.top_share([]) == 0.0
    assert stats.top_share([0, 0]) == 0.0


# ── pure: histogram ────────────────────────────────────────────────────


def test_histogram_bucket_edges():
    vals = [0, 1, 9, 10, 49, 50, 99, 100, 249, 250, 499, 500, 999, 1000, 5000]
    hist = stats.balance_histogram(vals)
    counts = {(h["lo"], h["hi"]): h["count"] for h in hist}
    assert counts[(0, 0)] == 1  # exactly 0
    assert counts[(1, 9)] == 2  # 1, 9
    assert counts[(10, 49)] == 2  # 10, 49
    assert counts[(50, 99)] == 2
    assert counts[(100, 249)] == 2
    assert counts[(250, 499)] == 2
    assert counts[(500, 999)] == 2
    assert counts[(1000, None)] == 2  # 1000, 5000
    assert sum(h["count"] for h in hist) == len(vals)


def test_histogram_shape_and_open_top():
    hist = stats.balance_histogram([])
    assert len(hist) == len(stats.DEFAULT_BUCKETS)
    assert hist[-1]["hi"] is None
    assert all(h["count"] == 0 for h in hist)


# ── pure: affordability ────────────────────────────────────────────────


def test_affordability_days_one_dp():
    # median daily income 10 → role_color 50 costs 5.0 days, gradient 120 → 12.0.
    aff = stats.affordability(10.0, SETTINGS)
    assert aff["price_role_color"] == 5.0
    assert aff["price_role_gradient"] == 12.0
    assert aff["price_text_room"] == 20.0
    assert set(aff) == set(stats.PRICE_FIELDS)


def test_affordability_rounds_and_short_circuits():
    aff = stats.affordability(7.0, SETTINGS)
    assert aff["price_role_name"] == 5.0  # 35/7
    assert aff["price_role_color"] == 7.1  # 50/7 = 7.14 → 7.1
    assert stats.affordability(0.0, SETTINGS) == {}
    assert stats.affordability(-3.0, SETTINGS) == {}


# ── service seeding helpers ────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "stats.db"
    apply_migrations_sync(path)
    return path


def _wallet(db, user, balance):
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO econ_wallets "
            "(guild_id, user_id, balance, created_at, updated_at) "
            "VALUES (?, ?, ?, 0, 0)",
            (GUILD, user, balance),
        )


def _ledger(db, user, amount, kind, ts, *, meta=None, actor=None):
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO econ_ledger "
            "(guild_id, user_id, amount, kind, actor_id, meta, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (GUILD, user, amount, kind, actor, json.dumps(meta) if meta else None, ts),
        )


def _activity(db, user, when):
    with open_db(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO member_activity "
            "(guild_id, user_id, last_channel_id, last_message_id, last_message_at) "
            "VALUES (?, ?, 0, 0, ?)",
            (GUILD, user, when),
        )


def _rental(db, user, perk, state, *, beneficiary=None):
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO econ_rentals "
            "(guild_id, user_id, perk, state, price, started_at, next_bill_at, "
            " beneficiary_id, created_at) "
            "VALUES (?, ?, ?, ?, 50, 0, 0, ?, 0)",
            (GUILD, user, perk, state, beneficiary or user),
        )


def _streak(db, user, current, last_day):
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO econ_streaks "
            "(guild_id, user_id, current_streak, longest_streak, last_login_day) "
            "VALUES (?, ?, ?, ?, ?)",
            (GUILD, user, current, current, last_day),
        )


def _claim(db, quest_id, user, state, *, created_at, resolved_at=None):
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO econ_quest_claims "
            "(quest_id, guild_id, user_id, period, state, created_at, resolved_at) "
            "VALUES (?, ?, ?, 'p', ?, ?, ?)",
            (quest_id, GUILD, user, state, created_at, resolved_at),
        )


def _rollup(db, iso_week, median_income):
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO econ_metrics_weekly "
            "(guild_id, iso_week, median_income, computed_at) VALUES (?, ?, ?, 0)",
            (GUILD, iso_week, median_income),
        )


def _seed(db):
    """A three-member guild with income across windows, a transfer, a rental
    debit, live rentals, streaks, and quest claims."""
    # Wallets: one whale, one mid, one small, one zero-balance (not a holder).
    _wallet(db, 1, 1000)
    _wallet(db, 2, 100)
    _wallet(db, 3, 10)
    _wallet(db, 4, 0)

    d1 = NOW - 1 * DAY  # inside 7d
    d3 = NOW - 3 * DAY  # inside 7d
    d8 = NOW - 8 * DAY  # outside 7d, inside 30d

    # Member 1: logins (in + out of 7d) + a quest → faucet split.
    _ledger(db, 1, 50, "login", d1)
    _ledger(db, 1, 30, "login", d8)  # 30d only
    _ledger(db, 1, 20, "quest", d3)
    # Member 2: activity (conversion) dominates 30d.
    _ledger(db, 2, 40, "conversion", d1)
    _ledger(db, 2, 5, "quest", d3)
    # Member 3: a single grant.
    _ledger(db, 3, 15, "grant", d1, actor=99)

    # A transfer (excluded from mint/burn): member 1 → member 2, 25.
    _ledger(db, 1, -25, "transfer_out", d1, meta={"to": 2})
    _ledger(db, 2, 25, "transfer_in", d1, meta={"from": 1})

    # A rental debit (spend) by member 2, inside 7d.
    _ledger(db, 2, -18, "rental", d3)

    # Live rentals (member 1 self-color, member 3 gift to member 4).
    _rental(db, 1, "role_color", "active")
    _rental(db, 3, "gift_color", "grace", beneficiary=4)

    # Streaks.
    _streak(db, 1, 9, "2026-01-01")
    _streak(db, 2, 2, "2026-01-01")

    # Activity: members 1, 2, 3 active in 30d (member 4 not).
    _activity(db, 1, NOW - 2 * DAY)
    _activity(db, 2, NOW - 2 * DAY)
    _activity(db, 3, NOW - 10 * DAY)

    # Quest claims resolved in 30d: 2 paid, 1 denied → approval 2/3.
    _claim(db, 1, 1, "paid", created_at=d3, resolved_at=d1)
    _claim(db, 1, 2, "paid", created_at=d3, resolved_at=d1)
    _claim(db, 1, 3, "denied", created_at=d3, resolved_at=d1)
    # An instant-paid claim (resolved_at NULL) is NOT counted in approval rate.
    _claim(db, 2, 1, "paid", created_at=d1, resolved_at=None)


# ── service: supply / distribution ─────────────────────────────────────


def test_compute_stats_supply():
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp()) / "s.db"
    apply_migrations_sync(tmp)
    _seed(tmp)
    with open_db(tmp) as conn:
        out = compute_stats(conn, SETTINGS, GUILD, now=NOW)
    sup = out["supply"]
    assert sup["total"] == 1110  # 1000 + 100 + 10 + 0
    assert sup["holders"] == 3  # positive balances only
    assert sup["median_balance"] == 100
    assert sup["top10_share"] == pytest.approx(1000 / 1110)
    assert sup["gini"] > 0.5


def test_compute_stats_all_top_level_keys(db):
    _seed(db)
    with open_db(db) as conn:
        out = compute_stats(conn, SETTINGS, GUILD, now=NOW)
    for key in (
        "supply",
        "distribution",
        "flow_7d",
        "members",
        "engagement",
        "transfers_top",
        "affordability",
    ):
        assert key in out


def test_distribution_over_positive_balances(db):
    _seed(db)
    with open_db(db) as conn:
        out = compute_stats(conn, SETTINGS, GUILD, now=NOW)
    total = sum(b["count"] for b in out["distribution"])
    assert total == 3  # only the three positive holders


# ── service: flow window + transfer exclusion ──────────────────────────


def test_flow_7d_excludes_transfers(db):
    _seed(db)
    with open_db(db) as conn:
        out = compute_stats(conn, SETTINGS, GUILD, now=NOW)
    flow = out["flow_7d"]
    # 7d minted: login 50 + quest 20 + conversion 40 + quest 5 + grant 15 = 130.
    # The 30d-only login (30) and the transfer_in (25) are excluded.
    assert flow["minted"] == 130
    # 7d burned excludes transfer_out; only the rental debit (18).
    assert flow["burned"] == 18
    assert flow["burn_rate"] == pytest.approx(18 / 130)
    assert flow["transfer_volume"] == 25
    assert flow["grants"] == 15


def test_flow_burn_rate_zero_when_no_mint(db):
    with open_db(db) as conn:
        out = compute_stats(conn, SETTINGS, GUILD, now=NOW)
    assert out["flow_7d"]["burn_rate"] == 0


# ── service: members ───────────────────────────────────────────────────


def test_members_windows_and_faucet(db):
    _seed(db)
    with open_db(db) as conn:
        out = compute_stats(conn, SETTINGS, GUILD, now=NOW)
    members = {int(m["user_id"]): m for m in out["members"]}
    m1 = members[1]
    # income_7d = login 50 + quest 20 = 70; income_30d adds the 8-day login → 100.
    assert m1["income_7d"] == 70
    assert m1["income_30d"] == 100
    assert m1["coins_per_day_7d"] == pytest.approx(10.0)  # 70 / 7
    assert m1["rentals_live"] == 1
    assert m1["streak"] == 9
    # top faucet over 30d: logins (50+30=80) beats quests (20) → "logins".
    assert m1["top_faucet"] == "logins"
    m2 = members[2]
    assert m2["spent_7d"] == 18  # rental debit magnitude
    assert m2["top_faucet"] == "activity"  # conversion 40 > quest 5
    m3 = members[3]
    assert m3["top_faucet"] == "grants"


def test_members_sorted_by_balance_and_limited(db):
    _seed(db)
    with open_db(db) as conn:
        out = compute_stats(conn, SETTINGS, GUILD, now=NOW, member_limit=2)
    assert [int(m["user_id"]) for m in out["members"]] == [1, 2]


def test_member_top_faucet_none_without_income(db):
    _wallet(db, 50, 500)  # a holder with no ledger income at all
    with open_db(db) as conn:
        out = compute_stats(conn, SETTINGS, GUILD, now=NOW)
    m = next(m for m in out["members"] if int(m["user_id"]) == 50)
    assert m["top_faucet"] is None
    assert m["income_30d"] == 0
    assert m["last_earned_at"] is None


# ── service: engagement ────────────────────────────────────────────────


def test_engagement_counts_and_ratios(db):
    _seed(db)
    with open_db(db) as conn:
        out = compute_stats(conn, SETTINGS, GUILD, now=NOW)
    eng = out["engagement"]
    # active_member_ids is wall-clock based (existing helper); all three seeded
    # activity rows sit in the future relative to now, so all count.
    assert eng["active_members"] == 3  # members 1, 2, 3 have activity rows
    assert eng["earners_7d"] == 3  # members 1, 2, 3 earned in 7d
    assert eng["earner_ratio"] == pytest.approx(1.0)
    assert eng["spenders_7d"] == 1  # member 2 rental debit
    assert eng["quest_claims_7d"] >= 1
    assert eng["quest_approval_rate_30d"] == pytest.approx(2 / 3)


def test_engagement_approval_null_without_resolved(db):
    _wallet(db, 1, 100)
    with open_db(db) as conn:
        out = compute_stats(conn, SETTINGS, GUILD, now=NOW)
    assert out["engagement"]["quest_approval_rate_30d"] is None


def test_engagement_hoard_weeks_with_and_without_rollup(db):
    _seed(db)
    with open_db(db) as conn:
        out = compute_stats(conn, SETTINGS, GUILD, now=NOW)
    assert out["engagement"]["hoard_weeks"] is None  # no rollup yet
    _rollup(db, "2026-W20", 50.0)
    with open_db(db) as conn:
        out = compute_stats(conn, SETTINGS, GUILD, now=NOW)
    # median_balance 100 / median weekly income 50 = 2.0 weeks.
    assert out["engagement"]["hoard_weeks"] == pytest.approx(2.0)


def test_engagement_hoard_weeks_null_on_zero_median_income(db):
    _seed(db)
    _rollup(db, "2026-W20", 0.0)
    with open_db(db) as conn:
        out = compute_stats(conn, SETTINGS, GUILD, now=NOW)
    assert out["engagement"]["hoard_weeks"] is None


# ── service: transfers_top ─────────────────────────────────────────────


def test_transfers_top_pairs(db):
    _seed(db)
    with open_db(db) as conn:
        out = compute_stats(conn, SETTINGS, GUILD, now=NOW)
    top = out["transfers_top"]
    assert len(top) == 1
    assert int(top[0]["from_id"]) == 1
    assert int(top[0]["to_id"]) == 2
    assert top[0]["total"] == 25


def test_transfers_top_skips_malformed_meta(db):
    d1 = NOW - DAY
    _ledger(db, 1, -10, "transfer_out", d1, meta=None)  # no meta → skipped
    with open_db(db) as conn:
        out = compute_stats(conn, SETTINGS, GUILD, now=NOW)
    assert out["transfers_top"] == []


# ── service: affordability ─────────────────────────────────────────────


def test_affordability_from_median_daily(db):
    _seed(db)
    with open_db(db) as conn:
        out = compute_stats(conn, SETTINGS, GUILD, now=NOW)
    # earners_7d incomes: m1 70, m2 45, m3 15 → daily 10, 6.43, 2.14 →
    # median daily = 45/7 ≈ 6.43; role_color 50 / 6.43 ≈ 7.8 days.
    aff = out["affordability"]
    assert aff  # non-empty
    assert aff["price_role_color"] == pytest.approx(50 / (45 / 7), abs=0.05)


def test_affordability_empty_without_earners(db):
    _wallet(db, 1, 100)  # holder, no income
    with open_db(db) as conn:
        out = compute_stats(conn, SETTINGS, GUILD, now=NOW)
    assert out["affordability"] == {}


# ── live "happening now" payload ──────────────────────────────────────


def test_compute_live_shapes_and_counts(tmp_path):
    from bot_modules.services.economy_quests_service import (
        activate_community_weekly,
        create_quest,
        fire_trigger_quests,
        set_quest_active,
    )
    from bot_modules.services.economy_service import save_econ_settings
    from bot_modules.services.economy_stats_service import compute_live

    db_path = tmp_path / "live.db"
    apply_migrations_sync(db_path)
    with open_db(db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
        settings = SETTINGS
        # A daily kind quest, an event quest, and a running community weekly.
        daily = create_quest(
            conn, GUILD, title="Chatter", description="", qtype="daily",
            reward=10, signoff=0, criteria="", starts_at=None, ends_at=None,
            rotate_tag="", community_target=None, created_by=None,
            trigger_kind="message_sent",
        )
        set_quest_active(conn, GUILD, daily, True)
        event = create_quest(
            conn, GUILD, title="Booster", description="", qtype="event",
            reward=5, signoff=0, criteria="", starts_at=None, ends_at=None,
            rotate_tag="", community_target=None, created_by=None,
            trigger_kind="boost",
        )
        set_quest_active(conn, GUILD, event, True)
        comm = create_quest(
            conn, GUILD, title="Together", description="", qtype="community",
            reward=30, signoff=0, criteria="", starts_at=None, ends_at=None,
            rotate_tag="", community_target=None, created_by=None,
            trigger_kind="message_sent",
        )
        activate_community_weekly(conn, GUILD, comm, target=100, week="2026-W29")

        # One member completes the daily (also bumps community to 1) and one
        # event occurrence pays.
        fire_trigger_quests(
            conn, settings, GUILD, "message_sent", 1,
            local_day="2026-07-14", occurrence="m1", booster=False,
        )
        fire_trigger_quests(
            conn, settings, GUILD, "boost", 1,
            local_day="2026-07-14", occurrence="b1", booster=False,
        )
        # "Now" must sit inside the same guild-local day the fires used, or
        # the current-period counts correctly read 0.
        from datetime import datetime, timezone

        live_now = datetime(
            2026, 7, 14, 18, 0, tzinfo=timezone.utc
        ).timestamp()
        live = compute_live(conn, GUILD, now=live_now)

    assert live["community"][0]["title"] == "Together"
    assert live["community"][0]["current"] == 1
    assert live["community"][0]["contributors"] == 1
    assert live["community"][0]["tiers_crossed"] == 0
    daily_rows = live["cadences"]["daily"]
    assert daily_rows and daily_rows[0]["completed"] == 1
    assert live["events"][0]["paid_total"] == 1
    assert live["completions_week"] >= 1
    assert live["seconds_to_day_roll"] > 0
    assert live["seconds_to_week_roll"] > 0
