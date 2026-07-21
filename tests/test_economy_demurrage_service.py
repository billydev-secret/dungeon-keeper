"""Tests for services/economy_demurrage_service.py — the weekly hoard tax.

The money-critical paths: only the excess above the protected floor is ever
taxed (so a member can't be pushed below it and 100% is a hard cap, not a
wipe), floor-division grace on small excesses, and the exactly-once sweep
claim so a replayed week roll collects nothing twice.
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_demurrage_service import (
    TAX_KIND,
    demurrage_enabled,
    get_sweep,
    run_sweep,
)
from bot_modules.services.economy_service import (
    EconSettings,
    apply_credit,
    get_balance,
)
from migrations import apply_migrations_sync

GUILD = 700
USER = 2001
USER_2 = 2002
WEEK = "2026-W29"
NOW = 1_800_000_000.0

SETTINGS = EconSettings(
    enabled=True, demurrage_rate_pct=10, demurrage_threshold=500
)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    apply_migrations_sync(path)
    return path


def _fund(conn, amount, user_id=USER):
    apply_credit(conn, GUILD, user_id, amount, "grant")


def _ledger_rows(conn, user_id):
    return conn.execute(
        "SELECT * FROM econ_ledger WHERE guild_id = ? AND user_id = ? "
        "AND kind = ?",
        (GUILD, user_id, TAX_KIND),
    ).fetchall()


def test_enabled_gate():
    assert demurrage_enabled(SETTINGS)
    assert not demurrage_enabled(EconSettings(enabled=True))  # rate 0 default


def test_taxes_only_the_excess_above_the_floor(db):
    with open_db(db) as conn:
        _fund(conn, 1500)          # 1000 over the 500 floor → 10% = 100
        _fund(conn, 400, USER_2)   # under the floor → untouched
        out = run_sweep(conn, SETTINGS, GUILD, WEEK, now=NOW)
        assert out is not None
        assert out.taxed_members == 1 and out.total == 100
        assert get_balance(conn, GUILD, USER) == 1400
        assert get_balance(conn, GUILD, USER_2) == 400
        rows = _ledger_rows(conn, USER)
        assert len(rows) == 1 and int(rows[0]["amount"]) == -100


def test_balance_exactly_at_floor_is_untouched(db):
    with open_db(db) as conn:
        _fund(conn, 500)
        out = run_sweep(conn, SETTINGS, GUILD, WEEK, now=NOW)
        assert out is not None and out.taxed_members == 0
        assert get_balance(conn, GUILD, USER) == 500
        assert _ledger_rows(conn, USER) == []


def test_small_excess_rounds_down_to_grace(db):
    with open_db(db) as conn:
        _fund(conn, 509)  # excess 9 → 10% = 0.9 → floor 0 → no tax, no row
        out = run_sweep(conn, SETTINGS, GUILD, WEEK, now=NOW)
        assert out is not None and out.taxed_members == 0 and out.total == 0
        assert get_balance(conn, GUILD, USER) == 509
        assert _ledger_rows(conn, USER) == []


def test_hundred_percent_is_a_hard_cap_at_the_floor(db):
    settings = EconSettings(
        enabled=True, demurrage_rate_pct=100, demurrage_threshold=500
    )
    with open_db(db) as conn:
        _fund(conn, 2000)
        out = run_sweep(conn, settings, GUILD, WEEK, now=NOW)
        assert out is not None and out.total == 1500
        assert get_balance(conn, GUILD, USER) == 500  # never below the floor


def test_sweep_is_exactly_once(db):
    with open_db(db) as conn:
        _fund(conn, 1500)
        first = run_sweep(conn, SETTINGS, GUILD, WEEK, now=NOW)
        assert first is not None and first.total == 100
        # A crash-and-replay of the week roll re-runs the sweep: the claim
        # PK refuses it and no second debit lands.
        assert run_sweep(conn, SETTINGS, GUILD, WEEK, now=NOW) is None
        assert get_balance(conn, GUILD, USER) == 1400
        assert len(_ledger_rows(conn, USER)) == 1
        # A different week is a fresh claim.
        assert run_sweep(conn, SETTINGS, GUILD, "2026-W30", now=NOW) is not None
        assert get_balance(conn, GUILD, USER) == 1310  # 10% of 900


def test_sweep_row_records_totals_and_meta(db):
    with open_db(db) as conn:
        _fund(conn, 1500)
        _fund(conn, 700, USER_2)
        run_sweep(conn, SETTINGS, GUILD, WEEK, now=NOW)
        sweep = get_sweep(conn, GUILD, WEEK)
        assert sweep is not None
        assert int(sweep["taxed_members"]) == 2
        assert int(sweep["total"]) == 100 + 20
        row = _ledger_rows(conn, USER)[0]
        import json

        meta = json.loads(row["meta"])
        assert meta["iso_week"] == WEEK and meta["balance"] == 1500
