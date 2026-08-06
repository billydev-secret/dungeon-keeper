"""Tests for scripts/economy_tuning_report.py.

Focused on the casino netting: before this, a stake counted as a burn and a
payout counted as income, so a guild that churned 40k through the slots and
got 33k back looked like it both minted and destroyed a fortune. The report
is the instrument used to tune the faucet dials, so a lying instrument is
worse than none.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

from bot_modules.core.db_utils import open_db
from scripts import economy_tuning_report as rpt
from tests.db_template import migrated_db

GUILD = 4242
TODAY = dt.date(2026, 7, 26)


def _ts(day: dt.date, hour: int = 18) -> float:
    """A timestamp landing on ``day`` in the report's guild-local tz (UTC-7)."""
    return dt.datetime(
        day.year, day.month, day.day, hour, tzinfo=dt.timezone.utc
    ).timestamp()


def _ledger(conn, kind: str, amount: int, day: dt.date, user: int = 1) -> None:
    conn.execute(
        "INSERT INTO econ_ledger (guild_id, user_id, amount, kind, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (GUILD, user, amount, kind, _ts(day)),
    )


def _wallet(conn, user: int, balance: int) -> None:
    conn.execute(
        "INSERT INTO econ_wallets (guild_id, user_id, balance, created_at, updated_at) "
        "VALUES (?, ?, ?, 0, 0)",
        (GUILD, user, balance),
    )


def _collect(tmp_path, rows, *, pot: int = 0, days: int = 3) -> dict:
    path = tmp_path / "report.db"
    migrated_db(path)
    with open_db(path) as conn:
        _wallet(conn, 1, 900)
        _wallet(conn, 2, 100)
        for kind, amount in rows:
            _ledger(conn, kind, amount, TODAY)
        if pot:
            conn.execute(
                "INSERT INTO casino_jackpot (guild_id, pot, updated_at) VALUES (?, ?, 0)",
                (GUILD, pot),
            )
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return rpt.collect(conn, GUILD, TODAY, days)
    finally:
        conn.close()


def test_casino_churn_nets_to_the_hold(tmp_path):
    """1,000 staked, 800 returned = a 200 sink, not a 1,000 sink + 800 mint."""
    stats = _collect(tmp_path, [
        ("casino_stake", -1_000),
        ("casino_payout", 800),
        ("quest", 500),
    ])
    # The payout is the member's own stake coming back — never a faucet.
    assert "casino_payout" not in stats["faucet_mix"]
    assert stats["minted_week"] == 500
    # The stake is not destruction; only the hold is.
    assert "casino_stake" not in stats["sink_mix"]
    assert stats["sink_mix"]["casino_hold"] == 200
    assert stats["burned_week"] == 200
    assert stats["casino_handle"] == 1_000
    assert stats["casino_returned"] == 800
    assert stats["casino_hold"] == 200


def test_a_winning_week_for_players_is_a_signed_faucet(tmp_path):
    """When the players come out ahead the burn total must say so."""
    stats = _collect(tmp_path, [
        ("casino_stake", -500),
        ("casino_payout", 900),
        ("rental", -100),
    ])
    assert stats["casino_hold"] == -400
    assert stats["sink_mix"]["casino_hold"] == -400
    assert stats["burned_week"] == -300  # 100 of rentals, less the 400 paid out


def test_jackpot_pot_is_reported_beside_the_hold(tmp_path):
    """The pot is hold owed to a future winner, not currency destroyed."""
    stats = _collect(
        tmp_path, [("casino_stake", -1_000), ("casino_payout", 800)], pot=250
    )
    assert stats["jackpot_pot"] == 250
    assert stats["casino_hold"] == 200  # unchanged: the memo never nets in


def test_no_casino_traffic_leaves_the_sink_mix_clean(tmp_path):
    stats = _collect(tmp_path, [("quest", 300), ("rental", -50)])
    assert "casino_hold" not in stats["sink_mix"]
    assert stats["burned_week"] == 50
    assert stats["casino_handle"] == 0


def test_window_days_is_recorded_so_baselines_cannot_be_mixed(tmp_path):
    """A 3-day run diffed against a 7-day baseline is nonsense; flag it."""
    stats = _collect(tmp_path, [("quest", 300)], days=3)
    assert stats["window_days"] == 3
    assert stats["week"] == "2026-07-24..2026-07-26"
    full = _collect(tmp_path, [("quest", 300)], days=0)
    assert full["window_days"] == 7  # falsy days = the last full ISO week


# ── escrow netting (auctions, bounties) ───────────────────────────────


def test_auction_escrow_round_trip_is_not_a_sink(tmp_path):
    """900 bid, 900 refunded when outbid = no sink and no faucet."""
    stats = _collect(tmp_path, [
        ("auction_bid", -900),
        ("auction_refund", 900),
        ("rental", -50),
    ])
    assert stats["escrow_flows"]["auction_bid"] == {"staked": 900, "returned": 900}
    assert "auction_bid" not in stats["sink_mix"]
    assert "auction_refund" not in stats["faucet_mix"]
    assert stats["burned_week"] == 50   # the rental, and nothing else


def test_bounty_refund_is_a_return_leg_like_the_payout(tmp_path):
    """cancel/expire credits bounty_refund, award credits bounty_payout —
    both are returns, and missing either makes a cancel read as a mint."""
    stats = _collect(tmp_path, [
        ("bounty_stake", -600),
        ("bounty_refund", 600),
    ])
    assert stats["escrow_flows"]["bounty_stake"] == {"staked": 600, "returned": 600}
    assert "bounty_refund" not in stats["faucet_mix"]
    assert stats["minted_week"] == 0
    assert stats["burned_week"] == 0


def test_escrow_straddling_the_window_cannot_drive_burn_negative(tmp_path):
    """A bid staked before the window and refunded inside it.

    Netting the two legs per-window would book a NEGATIVE burn here and flip
    the burn ratio below zero — auctions run for days, so a --days 5 run over
    an auction boundary hits this. The memo reports the flows instead.
    """
    stats = _collect(tmp_path, [
        ("auction_refund", 6_830),   # no matching stake in this window
        ("rental", -50),
    ])
    assert stats["escrow_flows"]["auction_bid"] == {"staked": 0, "returned": 6_830}
    assert stats["burned_week"] == 50
    assert stats["burn_ratio_pct"] >= 0


# ── per-guild timezone ────────────────────────────────────────────────


def test_window_uses_the_guilds_own_timezone(tmp_path):
    """A guild nine hours east buckets its days by its own offset.

    The offset was a module constant until 2026-08-06, so pointing --guild at
    the second guild (UTC+2) silently mis-bucketed every window by nine hours.
    """
    path = tmp_path / "tz.db"
    migrated_db(path)
    with open_db(path) as conn:
        _wallet(conn, 1, 100)
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (?, 'tz_offset_hours', '2.0')",
            (GUILD,),
        )
        # 2026-07-27 00:30 UTC: still 07-26 at UTC-7, already 07-27 at UTC+2.
        conn.execute(
            "INSERT INTO econ_ledger (guild_id, user_id, amount, kind, created_at) "
            "VALUES (?, 1, 300, 'quest', ?)",
            (GUILD, dt.datetime(2026, 7, 27, 0, 30, tzinfo=dt.timezone.utc).timestamp()),
        )
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        assert rpt._tz_offset(conn, GUILD) == 2.0
        # Window ends 07-26 guild-local, so the row belongs to the NEXT day
        # and must not be counted here.
        stats = rpt.collect(conn, GUILD, TODAY, 3)
    finally:
        conn.close()
    assert stats["tz_offset_hours"] == 2.0
    assert stats["minted_week"] == 0


def test_tz_offset_falls_back_to_the_global_row(tmp_path):
    path = tmp_path / "tzfall.db"
    migrated_db(path)
    with open_db(path) as conn:
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (0, 'tz_offset_hours', '-5.5')"
        )
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        assert rpt._tz_offset(conn, GUILD) == -5.5          # no guild row
        assert rpt._tz_offset(conn, 0) == -5.5
    finally:
        conn.close()


def test_guilds_with_wallets_ranks_by_float(tmp_path):
    """--all-guilds must lead with the biggest float, which is how a second
    guild outgrew the main one without appearing in any review."""
    path = tmp_path / "many.db"
    migrated_db(path)
    with open_db(path) as conn:
        for guild, bal in ((11, 500), (22, 9_000), (33, 1_200)):
            conn.execute(
                "INSERT INTO econ_wallets (guild_id, user_id, balance, created_at, updated_at) "
                "VALUES (?, 1, ?, 0, 0)",
                (guild, bal),
            )
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        assert rpt.guilds_with_wallets(conn) == [22, 33, 11]
    finally:
        conn.close()


def test_tz_fallback_matches_the_runtime_default(tmp_path):
    """With no guild row and no guild_id=0 row, the report must bucket days
    the way the bot does — get_tz_offset_hours defaults to 0.0.

    This was -7.0, which meant --all-guilds bucketed an *unconfigured* guild
    seven hours away from the bot: the exact class of mis-bucketing the
    per-guild lookup was added to remove.
    """
    from bot_modules.core.db_utils import get_tz_offset_hours

    path = tmp_path / "tznone.db"
    migrated_db(path)
    with open_db(path) as conn:
        runtime = get_tz_offset_hours(conn, GUILD)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        assert rpt._tz_offset(conn, GUILD) == runtime == 0.0
    finally:
        conn.close()
