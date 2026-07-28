"""Tests for services/pools_service.py and the Pools round lifecycle.

Two things here are worth more than the rest:

* **Session-day attribution.** ``test_straddling_hand_...`` is written to
  fail against naive row-timestamp bucketing. Without it, a member holding
  "under" could deal a 1,000-Petal hand at 23:59 and stand at 00:00:30 to
  move the day's metric by the full stake at an expected cost of ~7.
* **Pools' own money is invisible to the metric it settles against**, or a
  bigger pool drags the number it is betting on.
"""

from __future__ import annotations

import json

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services import casino_service as svc
from bot_modules.services import pools_logic as L
from bot_modules.services import pools_service as ps
from bot_modules.services.economy_service import (
    apply_credit,
    get_balance,
    save_econ_settings,
)
from tests.db_template import migrated_db

GUILD = 820
CHAN = 9200
A, B, C = 4001, 4002, 4003
TZ = -7.0

# 2026-07-20 12:00 guild-local (tz -7) → the middle of a known day.
DAY = "2026-07-20"
NEXT_DAY = "2026-07-21"
NOON = 1_784_600_000.0


def _local_midnight(day: str) -> float:
    from bot_modules.economy.logic import local_day_bounds

    return local_day_bounds(day, TZ)[0]


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    migrated_db(path)
    with open_db(path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
        svc.save_casino_settings(
            conn, GUILD,
            # max_bet mirrors the shipping guild (1000), not the dataclass
            # default (100) — the pool-split cases need room to be lopsided.
            {"channel_id": CHAN, "pools_enabled": True, "max_bet": 1000},
        )
    return path


def _fund(conn, user_id, amount):
    apply_credit(conn, GUILD, user_id, amount, "grant")


def _ledger(conn, amount, kind, ts, meta=None):
    conn.execute(
        "INSERT INTO econ_ledger (guild_id, user_id, amount, kind, meta, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (GUILD, A, amount, kind, json.dumps(meta) if meta else None, ts),
    )


def _series(conn):
    return {m.day: m for m in ps.daily_series(conn, GUILD, tz_offset_hours=TZ)}


# ── the metric ─────────────────────────────────────────────────────────


def test_net_is_the_circulation_delta_and_the_candle_body(db):
    """The settlement value and the chart's candle body are the same sum,
    so the market and the chart cannot disagree."""
    with open_db(db) as conn:
        _ledger(conn, 500, "quest", NOON)
        _ledger(conn, -120, "shop_purchase", NOON + 60)
        s = _series(conn)[DAY]
    assert s.net == 380
    assert s.close - s.open == s.net
    assert s.mint == 500
    assert s.burn == 120


def test_series_reconciles_to_the_wallet_total(db):
    """Circulation is exactly the running ledger sum — the property the
    whole candlestick chart rests on."""
    with open_db(db) as conn:
        _fund(conn, A, 300)
        _fund(conn, B, 150)
        series = ps.daily_series(conn, GUILD, tz_offset_hours=TZ)
        wallets = conn.execute(
            "SELECT COALESCE(SUM(balance), 0) FROM econ_wallets WHERE guild_id = ?",
            (GUILD,),
        ).fetchone()[0]
    assert series[-1].close == wallets


def test_days_are_continuous(db):
    with open_db(db) as conn:
        _ledger(conn, 400, "quest", NOON)
        _ledger(conn, 250, "quest", NOON + 86_400)
        series = ps.daily_series(conn, GUILD, tz_offset_hours=TZ)
    assert [m.day for m in series] == [DAY, NEXT_DAY]
    assert series[1].open == series[0].close


# ── session-day attribution (the straddle) ─────────────────────────────


def test_straddling_hand_counts_against_the_day_it_started(db):
    """A hand dealt before midnight and settled after it contributes its
    whole stake AND payout to the earlier day.

    Fails against naive row-timestamp bucketing: that would book the stake
    as day D burn and the payout as day D+1 income, moving each day's
    metric by the full 1,000 for an expected cost of nothing.
    """
    before = _local_midnight(NEXT_DAY) - 60      # 23:59 on DAY
    after = _local_midnight(NEXT_DAY) + 30       # 00:00:30 on NEXT_DAY
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO casino_blackjack_hands "
            "(id, guild_id, channel_id, message_id, user_id, stake, "
            " state_json, created_at, last_action_at) "
            "VALUES (77, ?, ?, 0, ?, 1000, '{}', ?, ?)",
            (GUILD, CHAN, A, before, before),
        )
        # Stake at deal (no hand_id — take_stake books game only), payout
        # at settle, which DOES carry hand_id.
        _ledger(conn, -1000, "casino_stake", before, {"game": "blackjack"})
        _ledger(
            conn, 1000, "casino_payout", after,
            {"game": "blackjack", "hand_id": 77},
        )
        s = _series(conn)
    assert s[DAY].net == 0, "the straddle must not move the day it started"
    assert NEXT_DAY not in s, "nothing should be attributed to the next day"


def test_straddling_windowed_round_counts_against_its_open_day(db):
    """Same lever through a windowed round joined seconds before the roll —
    both halves follow the round, which is why take_stake now records
    round_id."""
    before = _local_midnight(NEXT_DAY) - 10
    after = _local_midnight(NEXT_DAY) + 35
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO casino_keno_rounds "
            "(id, guild_id, channel_id, status, opened_at, closes_at) "
            "VALUES (55, ?, ?, 'settled', ?, ?)",
            (GUILD, CHAN, before, before + 45),
        )
        _ledger(
            conn, -800, "casino_stake", before,
            {"game": "keno", "round_id": 55},
        )
        _ledger(
            conn, 800, "casino_payout", after,
            {"game": "keno", "round_id": 55},
        )
        s = _series(conn)
    assert s[DAY].net == 0
    assert NEXT_DAY not in s


def test_unlinked_rows_fall_back_to_their_own_day(db):
    """Historical rows predate the linkage and must still be counted, on
    their own timestamp, rather than silently dropped."""
    after = _local_midnight(NEXT_DAY) + 30
    with open_db(db) as conn:
        _ledger(conn, -500, "casino_stake", after, {"game": "keno"})
        s = _series(conn)
    assert s[NEXT_DAY].net == -500


# ── pools' own rows are excluded ───────────────────────────────────────


def test_pools_stakes_and_payouts_are_invisible_to_the_metric(db):
    """Otherwise a bigger pool mechanically drags the number it bets on:
    stake under, inflate the pool, and the burned takeout moves the metric
    your way."""
    with open_db(db) as conn:
        _ledger(conn, 1000, "quest", NOON)
        baseline = _series(conn)[DAY].net
        _ledger(conn, -400, "casino_stake", NOON + 10, {"game": "pools"})
        _ledger(conn, 380, "casino_payout", NOON + 20, {"game": "pools"})
        after = _series(conn)[DAY]
    assert after.net == baseline == 1000
    assert after.hold == 0


def test_other_games_still_count_toward_the_metric(db):
    """The exclusion is surgical — the casino hold is the dominant burn
    term and dropping it would gut the metric."""
    with open_db(db) as conn:
        _ledger(conn, -400, "casino_stake", NOON, {"game": "keno"})
        _ledger(conn, 100, "casino_payout", NOON + 5, {"game": "keno"})
        s = _series(conn)[DAY]
    assert s.hold == 300
    assert s.net == -300


# ── the line ───────────────────────────────────────────────────────────


def test_line_excludes_the_day_being_measured(db):
    """Opening on a line that included its own partial day would let the
    first hours of trading set the target being traded against."""
    with open_db(db) as conn:
        for i in range(7):
            _ledger(conn, 100, "quest", NOON + i * 86_400)
        # A huge partial-day row on the day we are about to open.
        _ledger(conn, 99_999, "quest", NOON + 7 * 86_400)
        day8 = ps.daily_series(conn, GUILD, tz_offset_hours=TZ)[7].day
        line = ps.line_for(conn, GUILD, day8, tz_offset_hours=TZ)
    assert line == 100.5


def test_no_line_before_a_week_of_history(db):
    with open_db(db) as conn:
        for i in range(3):
            _ledger(conn, 100, "quest", NOON + i * 86_400)
        assert ps.line_for(conn, GUILD, NEXT_DAY, tz_offset_hours=TZ) is None


# ── round lifecycle ────────────────────────────────────────────────────


def _open_round(conn, line=100.5, closes_at=NOON + 3600):
    return svc.open_pools_round(
        conn, GUILD, CHAN, DAY, line, closes_at, now=NOON
    )


def test_one_round_per_guild_day(db):
    with open_db(db) as conn:
        assert _open_round(conn) is not None
        assert _open_round(conn) is None


def test_settle_pays_pro_rata_and_burns_the_takeout(db):
    with open_db(db) as conn:
        rid = _open_round(conn)
        for u in (A, B, C):
            _fund(conn, u, 1000)
        assert svc.place_pools_bet(conn, rid, A, L.OVER, 100, now=NOON) is None
        assert svc.place_pools_bet(conn, rid, B, L.OVER, 50, now=NOON) is None
        assert svc.place_pools_bet(conn, rid, C, L.UNDER, 200, now=NOON) is None

        res = svc.settle_pools_round(conn, rid, 5000, now=NOON + 7200)
        assert res.voided is False
        paid = {int(b["user_id"]): int(b["payout"]) for b in res.bets or []}
        # 350 pool, 95% kept, over-side holds 150 split 2:1.
        assert paid == {A: 221, B: 110, C: 0}
        assert res.takeout == 19
        assert sum(paid.values()) + res.takeout == 350
        assert get_balance(conn, GUILD, A) == 1000 - 100 + 221
        assert get_balance(conn, GUILD, C) == 1000 - 200


def test_takeout_is_burned_not_fed_to_the_jackpot(db):
    """The pot re-mints what it holds, so routing the takeout there would
    make Pools inflationary on a delay instead of deflationary."""
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"jackpot_enabled": True})
        before = svc.get_jackpot(conn, GUILD)
        rid = _open_round(conn)
        for u in (A, B):
            _fund(conn, u, 1000)
        svc.place_pools_bet(conn, rid, A, L.OVER, 100, now=NOON)
        svc.place_pools_bet(conn, rid, B, L.UNDER, 100, now=NOON)
        svc.settle_pools_round(conn, rid, 5000, now=NOON + 7200)
        assert svc.get_jackpot(conn, GUILD) == before


def test_settlement_is_exactly_once_under_replay(db):
    with open_db(db) as conn:
        rid = _open_round(conn)
        for u in (A, B):
            _fund(conn, u, 1000)
        svc.place_pools_bet(conn, rid, A, L.OVER, 100, now=NOON)
        svc.place_pools_bet(conn, rid, B, L.UNDER, 100, now=NOON)
        first = svc.settle_pools_round(conn, rid, 5000, now=NOON + 7200)
        after_first = get_balance(conn, GUILD, A)
        second = svc.settle_pools_round(conn, rid, 5000, now=NOON + 7300)
    assert first.bets is not None
    assert second.bets is None
    assert after_first == 1000 - 100 + 190


def test_one_sided_round_voids_and_refunds_in_full(db):
    with open_db(db) as conn:
        rid = _open_round(conn)
        _fund(conn, A, 1000)
        svc.place_pools_bet(conn, rid, A, L.OVER, 250, now=NOON)
        res = svc.settle_pools_round(conn, rid, 5000, now=NOON + 7200)
        assert res.voided is True
        assert res.takeout == 0
        assert res.refunds == {A: 250}
        assert get_balance(conn, GUILD, A) == 1000
        assert str(svc.get_pools_round(conn, rid)["status"]) == "void"


def test_settling_against_the_stored_line_not_a_recomputed_one(db):
    """A round settled hours late must use the line members bet into."""
    with open_db(db) as conn:
        rid = svc.open_pools_round(
            conn, GUILD, CHAN, DAY, 4_000.5, NOON + 3600, now=NOON
        )
        for u in (A, B):
            _fund(conn, u, 1000)
        svc.place_pools_bet(conn, rid, A, L.OVER, 100, now=NOON)
        svc.place_pools_bet(conn, rid, B, L.UNDER, 100, now=NOON)
        # 3,000 is under a 4,000.5 line, so B wins.
        res = svc.settle_pools_round(conn, rid, 3000, now=NOON + 200_000)
        paid = {int(b["user_id"]): int(b["payout"]) for b in res.bets or []}
    assert paid[B] > 0
    assert paid[A] == 0


def test_betting_closes_at_the_window_end(db):
    with open_db(db) as conn:
        rid = _open_round(conn, closes_at=NOON + 100)
        _fund(conn, A, 1000)
        err = svc.place_pools_bet(conn, rid, A, L.OVER, 50, now=NOON + 500)
    assert err is not None
    assert "closed" in err.lower()


def test_unknown_side_is_a_programming_error(db):
    with open_db(db) as conn:
        rid = _open_round(conn)
        _fund(conn, A, 100)
        with pytest.raises(ValueError):
            svc.place_pools_bet(conn, rid, A, "sideways", 10, now=NOON)


# ── the leaver carve-out ───────────────────────────────────────────────


def test_leaver_refund_pulls_a_stake_while_betting_is_open(db):
    with open_db(db) as conn:
        rid = _open_round(conn, closes_at=NOON + 3600)
        _fund(conn, A, 1000)
        svc.place_pools_bet(conn, rid, A, L.OVER, 100, now=NOON)
        out = svc.refund_member_live_stakes(conn, GUILD, A, now=NOON + 60)
        assert out.get("pools") == 100
        assert get_balance(conn, GUILD, A) == 1000


def test_leaver_refund_stops_at_the_betting_close(db):
    """A Pools round stays 'open' for hours after betting shuts. Pulling a
    stake out of a closed pool would silently change every remaining
    bettor's pro-rata payout — unreachable with 45s windows, routine here.
    """
    with open_db(db) as conn:
        rid = _open_round(conn, closes_at=NOON + 100)
        for u in (A, B):
            _fund(conn, u, 1000)
        svc.place_pools_bet(conn, rid, A, L.OVER, 100, now=NOON)
        svc.place_pools_bet(conn, rid, B, L.UNDER, 100, now=NOON)

        out = svc.refund_member_live_stakes(conn, GUILD, A, now=NOON + 500)
        assert "pools" not in out
        assert get_balance(conn, GUILD, A) == 900

        # ...and the stake still settles normally.
        res = svc.settle_pools_round(conn, rid, 5000, now=NOON + 7200)
        paid = {int(b["user_id"]): int(b["payout"]) for b in res.bets or []}
        assert paid[A] == 190


def test_leaver_refund_still_works_for_short_windowed_games(db):
    """The carve-out is opt-in per descriptor; the 45s games keep the old
    behaviour of refunding any open round."""
    with open_db(db) as conn:
        rid = svc.open_keno_round(conn, GUILD, CHAN, 45, now=NOON)
        _fund(conn, A, 1000)
        svc.place_keno_ticket(conn, rid, A, 4, 40, now=NOON)
        out = svc.refund_member_live_stakes(conn, GUILD, A, now=NOON + 9999)
    assert out.get("keno") == 40
