"""Tests for services/qa_service.py."""

from __future__ import annotations

import json
import time

import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.economy.logic import local_day_for
from bot_modules.services.economy_service import (
    apply_credit,
    apply_debit,
    get_balance,
    get_ledger,
)
from bot_modules.services.qa_service import (
    DEFAULT_QA_SETTINGS,
    QA_PREFIX,
    QASettings,
    archive_test,
    compute_status,
    create_test,
    get_test,
    list_tests,
    list_verdicts,
    load_qa_settings,
    record_verdict,
    save_qa_settings,
    set_test_message,
    void_verdict,
)
from migrations import apply_migrations_sync

GUILD = 123
USER = 1001
OTHER = 1002
ADMIN = 9001

S = DEFAULT_QA_SETTINGS
DAY = local_day_for(time.time(), 0.0)  # today in UTC — matches ledger created_at


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    apply_migrations_sync(db_path)
    return db_path


def _mk_test(conn, n: int = 1) -> int:
    return create_test(
        conn,
        GUILD,
        f"Entry {n}",
        f"Entry {n} title",
        "- [ ] check the thing",
        commit_sha=f"abc{n:04d}",
        commit_subject=f"Feature {n}",
    )


def _record(conn, settings, test_id, user_id, verdict, note=None):
    return record_verdict(
        conn, settings, test_id, GUILD, user_id, verdict, note, local_day=DAY
    )


# ── settings ──────────────────────────────────────────────────────────


def test_defaults_when_unconfigured(db):
    with open_db(db) as conn:
        settings = load_qa_settings(conn, GUILD)
    assert settings == DEFAULT_QA_SETTINGS
    assert settings.enabled is False
    assert settings.role_id == 0
    assert settings.channel_id == 0
    assert settings.reward == 15
    assert settings.daily_cap == 4


def test_save_load_roundtrip(db):
    values = {
        "enabled": True,
        "role_id": 555,
        "channel_id": 777,
        "reward": 25,
        "daily_cap": 2,
    }
    with open_db(db) as conn:
        save_qa_settings(conn, GUILD, values)
    with open_db(db) as conn:
        settings = load_qa_settings(conn, GUILD)
    assert settings.enabled is True
    assert isinstance(settings.enabled, bool)
    assert settings.role_id == 555
    assert settings.channel_id == 777
    assert settings.reward == 25
    assert settings.daily_cap == 2


def test_partial_save_keeps_defaults(db):
    with open_db(db) as conn:
        save_qa_settings(conn, GUILD, {"reward": 30})
        settings = load_qa_settings(conn, GUILD)
    assert settings.reward == 30
    assert settings.daily_cap == DEFAULT_QA_SETTINGS.daily_cap
    assert settings.enabled is False


def test_no_legacy_guild0_fallback(db):
    with open_db(db) as conn:
        set_config_value(conn, f"{QA_PREFIX}enabled", "1", 0)
        set_config_value(conn, f"{QA_PREFIX}reward", "99", 0)
    with open_db(db) as conn:
        settings = load_qa_settings(conn, GUILD)
    assert settings.enabled is False
    assert settings.reward == 15


def test_save_rejects_unknown_key(db):
    with open_db(db) as conn:
        with pytest.raises(KeyError):
            save_qa_settings(conn, GUILD, {"not_a_field": 1})


# ── status math ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        ([], "pending"),
        (["pass"], "passed"),
        (["pass", "pass"], "passed"),
        (["blocked"], "blocked"),
        (["blocked", "pass"], "blocked"),
        (["fail"], "failed"),
        (["fail", "pass"], "failed"),
        (["fail", "blocked"], "failed"),
        (["fail", "blocked", "pass"], "failed"),
        (["pass", "blocked", "fail"], "failed"),  # order-independent
    ],
)
def test_compute_status_precedence(verdicts, expected):
    assert compute_status(verdicts) == expected


# ── tests CRUD ────────────────────────────────────────────────────────


def test_create_and_get_test(db):
    with open_db(db) as conn:
        tid = create_test(
            conn,
            GUILD,
            "Economy register channel",
            "Economy: register channel",
            "- [ ] feed posts",
            commit_sha="0f3a584",
            commit_subject="Economy: configurable register channel",
        )
        assert tid > 0
        row = get_test(conn, tid)
        assert row is not None
        assert row["entry_key"] == "Economy register channel"
        assert row["status"] == "pending"
        assert row["verified_by"] is None
        assert row["commit_sha"] == "0f3a584"
        assert get_test(conn, tid + 999) is None


def test_create_test_idempotent_on_entry_and_sha(db):
    with open_db(db) as conn:
        first = _mk_test(conn)
        again = _mk_test(conn)
        assert again == first
        assert len(list_tests(conn, GUILD)) == 1
        # Same entry re-queued under a new commit is a new test.
        other = create_test(
            conn, GUILD, "Entry 1", "Entry 1 title", "- [ ] recheck",
            commit_sha="def9999",
        )
        assert other != first


def test_set_test_message(db):
    with open_db(db) as conn:
        tid = _mk_test(conn)
        set_test_message(conn, tid, 42, 4242)
        row = get_test(conn, tid)
        assert row is not None
        assert row["channel_id"] == 42
        assert row["message_id"] == 4242


def test_list_tests_by_guild_and_status(db):
    with open_db(db) as conn:
        t1 = _mk_test(conn, 1)
        t2 = _mk_test(conn, 2)
        create_test(conn, GUILD + 1, "Other guild", "t", "b", commit_sha="fff")
        _record(conn, S, t2, USER, "pass")
        rows = list_tests(conn, GUILD)
        assert [r["id"] for r in rows] == [t2, t1]  # newest first
        passed = list_tests(conn, GUILD, status="passed")
        assert [r["id"] for r in passed] == [t2]
        assert list_tests(conn, GUILD, status="failed") == []


def test_archive_test_and_terminal(db):
    with open_db(db) as conn:
        tid = _mk_test(conn)
        assert archive_test(conn, tid) is True
        row = get_test(conn, tid)
        assert row is not None
        assert row["status"] == "archived"
        assert archive_test(conn, tid + 999) is False
        # Archived is terminal: verdicts are rejected.
        with pytest.raises(ValueError, match="archived"):
            _record(conn, S, tid, USER, "pass")


# ── record_verdict: pay on fresh insert ───────────────────────────────


def test_fresh_verdict_pays_and_stamps(db):
    with open_db(db) as conn:
        tid = _mk_test(conn)
        out = _record(conn, S, tid, USER, "pass")
        assert out.fresh is True
        assert out.paid == S.reward
        assert out.status == "passed"
        assert get_balance(conn, GUILD, USER) == S.reward
        rows = get_ledger(conn, GUILD, USER)
        assert len(rows) == 1
        assert rows[0]["kind"] == "qa_reward"
        assert json.loads(rows[0]["meta"]) == {"test_id": tid, "verdict": "pass"}
        verdicts = list_verdicts(conn, tid)
        assert len(verdicts) == 1
        assert verdicts[0]["paid_amount"] == S.reward
        assert verdicts[0]["verdict"] == "pass"
        test = get_test(conn, tid)
        assert test is not None
        assert test["status"] == "passed"
        assert test["verified_by"] == USER
        assert test["verified_at"] is not None


def test_verdict_update_pays_nothing_more(db):
    with open_db(db) as conn:
        tid = _mk_test(conn)
        _record(conn, S, tid, USER, "pass")
        out = _record(conn, S, tid, USER, "fail", note="broke on retry")
        assert out.fresh is False
        assert out.paid == 0
        # Re-click updated the row in place, no second pay.
        assert get_balance(conn, GUILD, USER) == S.reward
        assert len(get_ledger(conn, GUILD, USER)) == 1
        verdicts = list_verdicts(conn, tid)
        assert len(verdicts) == 1
        assert verdicts[0]["verdict"] == "fail"
        assert verdicts[0]["note"] == "broke on retry"
        assert verdicts[0]["paid_amount"] == S.reward  # original pay kept on record


def test_fail_after_pass_flips_status_to_failed(db):
    with open_db(db) as conn:
        tid = _mk_test(conn)
        assert _record(conn, S, tid, USER, "pass").status == "passed"
        out = _record(conn, S, tid, OTHER, "fail", note="nope")
        assert out.status == "failed"
        test = get_test(conn, tid)
        assert test is not None
        assert test["status"] == "failed"
        # A failed card is no longer "verified".
        assert test["verified_by"] is None


def test_verified_by_stays_first_passer(db):
    with open_db(db) as conn:
        tid = _mk_test(conn)
        _record(conn, S, tid, USER, "pass")
        _record(conn, S, tid, OTHER, "pass")
        test = get_test(conn, tid)
        assert test is not None
        assert test["status"] == "passed"
        assert test["verified_by"] == USER


def test_daily_cap_blocks_pay_but_records_verdict(db):
    settings = QASettings(reward=15, daily_cap=2)
    with open_db(db) as conn:
        t1, t2, t3 = _mk_test(conn, 1), _mk_test(conn, 2), _mk_test(conn, 3)
        assert _record(conn, settings, t1, USER, "pass").paid == 15
        assert _record(conn, settings, t2, USER, "pass").paid == 15
        out = _record(conn, settings, t3, USER, "blocked", note="env down")
        assert out.fresh is True
        assert out.paid == 0
        assert out.status == "blocked"
        assert get_balance(conn, GUILD, USER) == 30
        assert len(get_ledger(conn, GUILD, USER)) == 2
        # Verdict is still on the books, just unpaid.
        verdicts = list_verdicts(conn, t3)
        assert len(verdicts) == 1
        assert verdicts[0]["paid_amount"] == 0
        # The cap is per tester — another member still gets paid.
        assert _record(conn, settings, t3, OTHER, "pass").paid == 15


def test_zero_reward_records_without_ledger(db):
    settings = QASettings(reward=0)
    with open_db(db) as conn:
        tid = _mk_test(conn)
        out = _record(conn, settings, tid, USER, "pass")
        assert out.fresh is True
        assert out.paid == 0
        assert get_ledger(conn, GUILD, USER) == []


def test_record_verdict_rejects_bad_input(db):
    with open_db(db) as conn:
        tid = _mk_test(conn)
        with pytest.raises(ValueError, match="unknown verdict"):
            _record(conn, S, tid, USER, "maybe")
        with pytest.raises(ValueError, match="unknown test"):
            _record(conn, S, tid + 999, USER, "pass")
        # A test belongs to its guild; a cross-guild click is unknown too.
        with pytest.raises(ValueError, match="unknown test"):
            record_verdict(conn, S, tid, GUILD + 1, USER, "pass", local_day=DAY)


# ── void_verdict: clawback + recompute ────────────────────────────────


def test_void_claws_back_and_recomputes(db):
    with open_db(db) as conn:
        tid = _mk_test(conn)
        out = _record(conn, S, tid, USER, "pass")
        void = void_verdict(conn, out.verdict_id, ADMIN)
        assert void is not None
        assert void.clawed == S.reward
        assert void.shortfall == 0
        assert void.status == "pending"
        assert get_balance(conn, GUILD, USER) == 0
        debit = get_ledger(conn, GUILD, USER, limit=1)[0]
        assert debit["kind"] == "qa_void"
        assert debit["amount"] == -S.reward
        assert debit["actor_id"] == ADMIN
        assert json.loads(debit["meta"]) == {"verdict_id": out.verdict_id, "test_id": tid}
        row = list_verdicts(conn, tid)[0]
        assert row["voided_by"] == ADMIN
        assert row["voided_at"] is not None
        test = get_test(conn, tid)
        assert test is not None
        assert test["status"] == "pending"
        assert test["verified_by"] is None


def test_void_spent_down_wallet_claws_balance_records_shortfall(db):
    with open_db(db) as conn:
        tid = _mk_test(conn)
        out = _record(conn, S, tid, USER, "pass")  # +15
        assert apply_debit(conn, GUILD, USER, 9, "spend") is True  # balance 6
        void = void_verdict(conn, out.verdict_id, ADMIN)
        assert void is not None
        assert void.clawed == 6
        assert void.shortfall == 9
        assert get_balance(conn, GUILD, USER) == 0
        debit = get_ledger(conn, GUILD, USER, limit=1)[0]
        assert debit["kind"] == "qa_void"
        assert debit["amount"] == -6
        assert json.loads(debit["meta"]) == {
            "verdict_id": out.verdict_id,
            "test_id": tid,
            "shortfall": 9,
        }


def test_void_empty_wallet_no_debit_row(db):
    with open_db(db) as conn:
        tid = _mk_test(conn)
        out = _record(conn, S, tid, USER, "pass")
        assert apply_debit(conn, GUILD, USER, S.reward, "spend") is True  # balance 0
        void = void_verdict(conn, out.verdict_id, ADMIN)
        assert void is not None
        assert void.clawed == 0
        assert void.shortfall == S.reward
        # apply_debit can't mint a negative row; nothing beyond the spend.
        kinds = [r["kind"] for r in get_ledger(conn, GUILD, USER)]
        assert "qa_void" not in kinds


def test_void_unpaid_verdict_no_ledger_touch(db):
    settings = QASettings(reward=15, daily_cap=0)  # cap 0 = never pay
    with open_db(db) as conn:
        tid = _mk_test(conn)
        out = _record(conn, settings, tid, USER, "pass")
        assert out.paid == 0
        void = void_verdict(conn, out.verdict_id, ADMIN)
        assert void is not None
        assert void.clawed == 0
        assert void.shortfall == 0
        assert get_ledger(conn, GUILD, USER) == []


def test_void_already_voided_is_noop(db):
    with open_db(db) as conn:
        tid = _mk_test(conn)
        out = _record(conn, S, tid, USER, "pass")
        apply_credit(conn, GUILD, USER, 100, "grant")  # spare balance
        assert void_verdict(conn, out.verdict_id, ADMIN) is not None
        again = void_verdict(conn, out.verdict_id, ADMIN)
        assert not again
        # No second clawback.
        assert get_balance(conn, GUILD, USER) == 100
        assert void_verdict(conn, out.verdict_id + 999, ADMIN) is None


def test_void_first_pass_restamps_verified_from_second(db):
    with open_db(db) as conn:
        tid = _mk_test(conn)
        first = _record(conn, S, tid, USER, "pass")
        _record(conn, S, tid, OTHER, "pass")
        void = void_verdict(conn, first.verdict_id, ADMIN)
        assert void is not None
        assert void.status == "passed"  # OTHER's pass still verifies
        test = get_test(conn, tid)
        assert test is not None
        assert test["verified_by"] == OTHER


def test_void_fail_restores_passed(db):
    with open_db(db) as conn:
        tid = _mk_test(conn)
        _record(conn, S, tid, USER, "pass")
        bad = _record(conn, S, tid, OTHER, "fail", note="flaky env")
        assert bad.status == "failed"
        void = void_verdict(conn, bad.verdict_id, ADMIN)
        assert void is not None
        assert void.status == "passed"
        test = get_test(conn, tid)
        assert test is not None
        assert test["verified_by"] == USER
