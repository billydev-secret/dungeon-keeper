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
