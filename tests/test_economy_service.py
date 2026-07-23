"""Tests for services/economy_service.py."""

from __future__ import annotations

import math
from dataclasses import replace
from unittest.mock import MagicMock

import discord
import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from migrations import apply_migrations_sync
from bot_modules.services.economy_service import (
    DEFAULT_ECON_SETTINGS,
    ECON_PREFIX,
    award_host_bounty,
    top_up_voice_login,
    EconSettings,
    apply_credit,
    apply_debit,
    award_game_reward,
    create_qotd,
    get_balance,
    get_ledger,
    get_notify_muted,
    load_econ_settings,
    member_is_booster,
    notify_member,
    qotd_for_message,
    get_streak_shield_price,
    get_streak_shields,
    process_conversion,
    process_login,
    purchase_streak_shield,
    refund_streak_shield,
    save_econ_settings,
    set_notify_muted,
    transfer_currency,
    try_award_qotd,
)

GUILD = 123
USER = 1001
OTHER = 1002


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    apply_migrations_sync(db_path)
    return db_path


# ── settings ──────────────────────────────────────────────────────────


def test_defaults_when_unconfigured(db):
    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD)
    assert settings == DEFAULT_ECON_SETTINGS
    assert settings.enabled is False
    assert settings.currency_name == "Coin"


def test_save_load_roundtrip(db):
    values = {
        "enabled": True,
        "transfers_enabled": False,
        "bank_channel_id": 555,
        "manager_role_id": 777,
        "currency_name": "Gem",
        "currency_plural": "Gems",
        "currency_emoji": "💎",
        "booster_multiplier": 2.0,
        "xp_per_coin": 20.0,
        "reward_qotd": 42,
    }
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, values)
    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD)
    assert settings.enabled is True
    assert settings.transfers_enabled is False
    assert settings.bank_channel_id == 555
    assert settings.manager_role_id == 777
    assert settings.currency_name == "Gem"
    assert settings.currency_plural == "Gems"
    assert settings.currency_emoji == "💎"
    assert settings.booster_multiplier == 2.0
    assert settings.xp_per_coin == 20.0
    assert settings.reward_qotd == 42
    # Untouched fields keep defaults.
    assert settings.wallet_name == DEFAULT_ECON_SETTINGS.wallet_name


def test_enabled_stays_bool_not_int(db):
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD)
    assert settings.enabled is True
    assert isinstance(settings.enabled, bool)


def test_no_legacy_guild0_fallback(db):
    # Write legacy guild_id=0 rows directly; a real guild must ignore them.
    with open_db(db) as conn:
        set_config_value(conn, f"{ECON_PREFIX}enabled", "1", 0)
        set_config_value(conn, f"{ECON_PREFIX}currency_name", "Legacy", 0)
    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD)
    assert settings.enabled is False
    assert settings.currency_name == "Coin"


def test_save_rejects_unknown_key(db):
    with open_db(db) as conn:
        with pytest.raises(KeyError):
            save_econ_settings(conn, GUILD, {"not_a_field": 1})


# ── wallet + ledger ───────────────────────────────────────────────────


def test_get_balance_zero_when_no_wallet(db):
    with open_db(db) as conn:
        assert get_balance(conn, GUILD, USER) == 0


def test_credit_creates_wallet_and_ledger(db):
    with open_db(db) as conn:
        credited = apply_credit(conn, GUILD, USER, 10, "grant", actor_id=9)
        assert credited == 10
        assert get_balance(conn, GUILD, USER) == 10
        rows = get_ledger(conn, GUILD, USER)
    assert len(rows) == 1
    assert rows[0]["amount"] == 10
    assert rows[0]["kind"] == "grant"
    assert rows[0]["actor_id"] == 9


def test_credit_accumulates(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 10, "grant")
        apply_credit(conn, GUILD, USER, 5, "grant")
        assert get_balance(conn, GUILD, USER) == 15


def test_credit_meta_serialized_as_json(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 3, "grant", meta={"reason": "hi"})
        rows = get_ledger(conn, GUILD, USER)
    assert rows[0]["meta"] == '{"reason": "hi"}'


def test_credit_no_meta_is_null(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 3, "grant")
        rows = get_ledger(conn, GUILD, USER)
    assert rows[0]["meta"] is None


def test_credit_booster_ceil_rounding(db):
    with open_db(db) as conn:
        credited = apply_credit(
            conn, GUILD, USER, 5, "grant", booster=True, multiplier=1.5
        )
    # ceil(5 * 1.5) == ceil(7.5) == 8
    assert credited == 8


def test_credit_no_booster_ignores_multiplier(db):
    with open_db(db) as conn:
        credited = apply_credit(
            conn, GUILD, USER, 5, "grant", booster=False, multiplier=1.5
        )
    assert credited == 5


def test_credit_rejects_amount_below_one(db):
    with open_db(db) as conn:
        with pytest.raises(ValueError):
            apply_credit(conn, GUILD, USER, 0, "grant")
        with pytest.raises(ValueError):
            apply_credit(conn, GUILD, USER, -3, "grant")
        # No writes happened.
        assert get_balance(conn, GUILD, USER) == 0
        assert get_ledger(conn, GUILD, USER) == []


def test_debit_success(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 20, "grant")
        ok = apply_debit(conn, GUILD, USER, 8, "spend")
        assert ok is True
        assert get_balance(conn, GUILD, USER) == 12
        rows = get_ledger(conn, GUILD, USER)
    assert rows[0]["amount"] == -8
    assert rows[0]["kind"] == "spend"


def test_debit_insufficient_leaves_no_writes(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 5, "grant")
        ok = apply_debit(conn, GUILD, USER, 10, "spend")
        assert ok is False
        assert get_balance(conn, GUILD, USER) == 5
        rows = get_ledger(conn, GUILD, USER)
    # Only the initial credit row exists — no debit ledger row.
    assert len(rows) == 1
    assert rows[0]["amount"] == 5


def test_debit_no_wallet_returns_false(db):
    with open_db(db) as conn:
        ok = apply_debit(conn, GUILD, USER, 1, "spend")
        assert ok is False
        assert get_ledger(conn, GUILD, USER) == []


def test_debit_exact_balance_to_zero(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 7, "grant")
        ok = apply_debit(conn, GUILD, USER, 7, "spend")
        assert ok is True
        assert get_balance(conn, GUILD, USER) == 0


def test_debit_rejects_amount_below_one(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 5, "grant")
        with pytest.raises(ValueError):
            apply_debit(conn, GUILD, USER, 0, "spend")
        assert get_balance(conn, GUILD, USER) == 5


def test_wallets_isolated_per_user(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 10, "grant")
        assert get_balance(conn, GUILD, OTHER) == 0


def test_ledger_newest_first_and_limit(db):
    with open_db(db) as conn:
        for i in range(1, 6):
            apply_credit(conn, GUILD, USER, i, "grant")
        rows = get_ledger(conn, GUILD, USER, limit=3)
    assert len(rows) == 3
    # Newest (last inserted, amount 5) first.
    assert [r["amount"] for r in rows] == [5, 4, 3]


def test_ledger_scoped_to_user(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 10, "grant")
        apply_credit(conn, GUILD, OTHER, 20, "grant")
        rows = get_ledger(conn, GUILD, USER)
    assert len(rows) == 1
    assert rows[0]["amount"] == 10


# ── notify prefs ──────────────────────────────────────────────────────


def test_notify_muted_defaults_false(db):
    with open_db(db) as conn:
        assert get_notify_muted(conn, GUILD, USER) is False


def test_notify_muted_roundtrip(db):
    with open_db(db) as conn:
        set_notify_muted(conn, GUILD, USER, True)
        assert get_notify_muted(conn, GUILD, USER) is True
        set_notify_muted(conn, GUILD, USER, False)
        assert get_notify_muted(conn, GUILD, USER) is False


# ── daily login ───────────────────────────────────────────────────────

S = DEFAULT_ECON_SETTINGS
DAY = "2026-07-10"
PREV = "2026-07-09"


def _seed_streak(
    conn,
    *,
    streak: int,
    last_login: str,
    last_grace: str | None = None,
    longest: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO econ_streaks
            (guild_id, user_id, current_streak, longest_streak,
             last_login_day, last_grace_day)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (GUILD, USER, streak, longest if longest is not None else streak,
         last_login, last_grace),
    )


def _streak_row(conn):
    return conn.execute(
        "SELECT * FROM econ_streaks WHERE guild_id = ? AND user_id = ?",
        (GUILD, USER),
    ).fetchone()


def test_process_login_first_ever(db):
    with open_db(db) as conn:
        out = process_login(
            conn, S, GUILD, USER, local_day=DAY, source="text", booster=False
        )
        assert out is not None
        assert out.paid == S.login_text_base
        assert out.streak == 1
        assert out.milestone == 0
        assert out.grace_consumed is False
        assert out.reset is False
        assert get_balance(conn, GUILD, USER) == S.login_text_base
        rows = get_ledger(conn, GUILD, USER)
        assert [r["kind"] for r in rows] == ["login"]
        row = _streak_row(conn)
        assert row["current_streak"] == 1
        assert row["last_login_day"] == DAY
        login = conn.execute(
            "SELECT * FROM econ_logins WHERE guild_id=? AND user_id=?",
            (GUILD, USER),
        ).fetchone()
        assert login["source"] == "text"
        assert login["paid"] == S.login_text_base


# ── voice login top-up ────────────────────────────────────────────────────
# The daily login pays whichever source fires first and text nearly always
# wins (live 2026-07-23: 688 text vs 30 voice), so the advertised voice rate
# was paid on 4% of days. Voice presence now tops up the base difference.


def test_voice_top_up_pays_the_base_difference_after_a_text_login(db):
    with open_db(db) as conn:
        process_login(
            conn, S, GUILD, USER, local_day=DAY, source="text", booster=False
        )
        paid = top_up_voice_login(
            conn, S, GUILD, USER, local_day=DAY, booster=False
        )
        assert paid == S.login_voice_base - S.login_text_base
        assert get_balance(conn, GUILD, USER) == S.login_voice_base
        login = conn.execute(
            "SELECT * FROM econ_logins WHERE guild_id=? AND user_id=?",
            (GUILD, USER),
        ).fetchone()
        assert login["source"] == "voice"  # flipped, so it can't pay twice
        assert login["paid"] == S.login_voice_base


def test_voice_top_up_is_exactly_once(db):
    with open_db(db) as conn:
        process_login(
            conn, S, GUILD, USER, local_day=DAY, source="text", booster=False
        )
        assert top_up_voice_login(
            conn, S, GUILD, USER, local_day=DAY, booster=False
        ) > 0
        # Every later voice tick that day is a no-op.
        for _ in range(3):
            assert top_up_voice_login(
                conn, S, GUILD, USER, local_day=DAY, booster=False
            ) == 0
        assert get_balance(conn, GUILD, USER) == S.login_voice_base


def test_voice_top_up_does_nothing_when_voice_already_won_the_day(db):
    with open_db(db) as conn:
        process_login(
            conn, S, GUILD, USER, local_day=DAY, source="voice", booster=False
        )
        assert top_up_voice_login(
            conn, S, GUILD, USER, local_day=DAY, booster=False
        ) == 0
        assert get_balance(conn, GUILD, USER) == S.login_voice_base


def test_voice_top_up_does_nothing_without_a_login_that_day(db):
    with open_db(db) as conn:
        assert top_up_voice_login(
            conn, S, GUILD, USER, local_day=DAY, booster=False
        ) == 0
        assert get_balance(conn, GUILD, USER) == 0


def test_voice_top_up_never_pays_when_voice_is_worth_no_more(db):
    # Guard the subtraction: a guild that sets voice at or below text must not
    # produce a zero or negative credit (apply_credit raises below 1).
    flat = replace(S, login_voice_base=S.login_text_base)
    cheap = replace(S, login_voice_base=S.login_text_base - 1)
    for settings in (flat, cheap):
        with open_db(db) as conn:
            conn.execute("DELETE FROM econ_logins")
            conn.execute("DELETE FROM econ_wallets")
            process_login(
                conn, settings, GUILD, USER,
                local_day=DAY, source="text", booster=False,
            )
            before = get_balance(conn, GUILD, USER)
            assert top_up_voice_login(
                conn, settings, GUILD, USER, local_day=DAY, booster=False
            ) == 0
            assert get_balance(conn, GUILD, USER) == before


def test_voice_top_up_applies_the_guild_booster_multiplier(db):
    boosted = replace(S, booster_multiplier=3.0)
    with open_db(db) as conn:
        process_login(
            conn, boosted, GUILD, USER, local_day=DAY, source="text", booster=True
        )
        paid = top_up_voice_login(
            conn, boosted, GUILD, USER, local_day=DAY, booster=True
        )
        delta = boosted.login_voice_base - boosted.login_text_base
        assert paid == math.ceil(delta * 3.0)


# ── host bounty ───────────────────────────────────────────────────────────


def test_host_bounty_pays_per_joiner_and_ledgers_the_count(db):
    s = replace(S, host_bounty_per_joiner=4, host_bounty_cap=5)
    with open_db(db) as conn:
        paid = award_host_bounty(
            conn, s, GUILD, USER, joiners=3, booster=False
        )
        assert paid == 12
        assert get_balance(conn, GUILD, USER) == 12
        row = get_ledger(conn, GUILD, USER)[-1]
        assert row["kind"] == "game_host"


def test_host_bounty_is_dark_by_default(db):
    # Default settings ship the rate at 0 — the whole feature is off.
    with open_db(db) as conn:
        assert award_host_bounty(
            conn, S, GUILD, USER, joiners=4, booster=False
        ) == 0
        assert get_balance(conn, GUILD, USER) == 0


def test_host_bounty_pays_nothing_without_joiners(db):
    s = replace(S, host_bounty_per_joiner=4, host_bounty_cap=5)
    with open_db(db) as conn:
        assert award_host_bounty(
            conn, s, GUILD, USER, joiners=0, booster=False
        ) == 0


def test_host_bounty_applies_the_booster_multiplier(db):
    s = replace(S, host_bounty_per_joiner=4, host_bounty_cap=5, booster_multiplier=2.0)
    with open_db(db) as conn:
        paid = award_host_bounty(
            conn, s, GUILD, USER, joiners=3, booster=True
        )
        assert paid == math.ceil(12 * 2.0)


def test_host_bounty_ignores_an_unusable_host_id(db):
    s = replace(S, host_bounty_per_joiner=4, host_bounty_cap=5)
    with open_db(db) as conn:
        assert award_host_bounty(
            conn, s, GUILD, 0, joiners=3, booster=False
        ) == 0


def test_process_login_same_day_returns_none(db):
    with open_db(db) as conn:
        assert process_login(
            conn, S, GUILD, USER, local_day=DAY, source="text", booster=False
        ) is not None
        assert process_login(
            conn, S, GUILD, USER, local_day=DAY, source="voice", booster=False
        ) is None
        # No double pay.
        assert get_balance(conn, GUILD, USER) == S.login_text_base
        assert len(get_ledger(conn, GUILD, USER)) == 1


def test_process_login_voice_uses_voice_base(db):
    with open_db(db) as conn:
        out = process_login(
            conn, S, GUILD, USER, local_day=DAY, source="voice", booster=False
        )
        assert out is not None
        assert out.paid == S.login_voice_base


def test_process_login_consecutive_day_adds_streak_bonus(db):
    with open_db(db) as conn:
        _seed_streak(conn, streak=3, last_login=PREV)
        out = process_login(
            conn, S, GUILD, USER, local_day=DAY, source="text", booster=False
        )
        assert out is not None
        assert out.streak == 4
        assert out.paid == S.login_text_base + 3  # +1/day bonus, streak 4
        assert _streak_row(conn)["longest_streak"] == 4


def test_process_login_bonus_capped_but_streak_counter_grows(db):
    with open_db(db) as conn:
        _seed_streak(conn, streak=50, last_login=PREV)
        out = process_login(
            conn, S, GUILD, USER, local_day=DAY, source="text", booster=False
        )
        assert out is not None
        assert out.streak == 51  # cap applies to the bonus, not the counter
        assert out.paid == S.login_text_base + S.streak_bonus_cap


def test_process_login_milestone_separate_ledger_row(db):
    with open_db(db) as conn:
        _seed_streak(conn, streak=6, last_login=PREV)
        out = process_login(
            conn, S, GUILD, USER, local_day=DAY, source="text", booster=False
        )
        assert out is not None
        assert out.streak == 7
        assert out.milestone == S.milestone_day7
        rows = get_ledger(conn, GUILD, USER)
        assert sorted(r["kind"] for r in rows) == ["login", "milestone"]
        assert get_balance(conn, GUILD, USER) == out.paid + out.milestone
        login = conn.execute(
            "SELECT paid FROM econ_logins WHERE guild_id=? AND user_id=?",
            (GUILD, USER),
        ).fetchone()
        assert login["paid"] == out.paid + out.milestone


def test_process_login_grace_bridges_and_persists(db):
    with open_db(db) as conn:
        _seed_streak(conn, streak=4, last_login="2026-07-08")
        out = process_login(
            conn, S, GUILD, USER, local_day=DAY, source="text", booster=False
        )
        assert out is not None
        assert out.streak == 5
        assert out.grace_consumed is True
        assert out.reset is False
        assert _streak_row(conn)["last_grace_day"] == PREV


def test_process_login_grace_exhausted_resets(db):
    with open_db(db) as conn:
        _seed_streak(conn, streak=4, last_login="2026-07-08", last_grace="2026-07-05")
        out = process_login(
            conn, S, GUILD, USER, local_day=DAY, source="text", booster=False
        )
        assert out is not None
        assert out.streak == 1
        assert out.reset is True
        # Old grace anchor preserved (no new grace consumed).
        assert _streak_row(conn)["last_grace_day"] == "2026-07-05"


def test_process_login_two_day_gap_resets_but_keeps_longest(db):
    with open_db(db) as conn:
        _seed_streak(conn, streak=9, last_login="2026-07-07")
        out = process_login(
            conn, S, GUILD, USER, local_day=DAY, source="text", booster=False
        )
        assert out is not None
        assert out.streak == 1
        assert out.reset is True
        assert out.paid == S.login_text_base
        assert _streak_row(conn)["longest_streak"] == 9


def test_process_login_booster_ceil(db):
    with open_db(db) as conn:
        out = process_login(
            conn, S, GUILD, USER, local_day=DAY, source="text", booster=True
        )
        assert out is not None
        # ceil(5 * 1.5) == 8
        assert out.paid == 8
        assert get_balance(conn, GUILD, USER) == 8


# ── XP conversion ─────────────────────────────────────────────────────

# The conversion faucet ships off (xp_per_coin default 0); these tests cover
# the retained mechanism with it explicitly re-enabled at the old rate of 15.
S_CONV = EconSettings(xp_per_coin=15.0)


def test_process_conversion_basic_credit(db):
    with open_db(db) as conn:
        credited = process_conversion(
            conn, S_CONV, GUILD, USER, local_day=DAY, xp=31.0, booster=False
        )
        assert credited == 2  # 31 / 15
        rows = get_ledger(conn, GUILD, USER)
        assert rows[0]["kind"] == "conversion"
        row = conn.execute(
            "SELECT * FROM econ_conversions WHERE guild_id=? AND user_id=?",
            (GUILD, USER),
        ).fetchone()
        assert row["coins"] == 2
        assert row["remainder"] == pytest.approx(1.0)


def test_process_conversion_idempotent_per_day(db):
    with open_db(db) as conn:
        assert process_conversion(
            conn, S_CONV, GUILD, USER, local_day=DAY, xp=31.0, booster=False
        ) == 2
        assert process_conversion(
            conn, S_CONV, GUILD, USER, local_day=DAY, xp=31.0, booster=False
        ) == 0
        assert get_balance(conn, GUILD, USER) == 2
        assert len(get_ledger(conn, GUILD, USER)) == 1


def test_process_conversion_remainder_carries_across_days(db):
    with open_db(db) as conn:
        process_conversion(conn, S_CONV, GUILD, USER, local_day=PREV, xp=10.0, booster=False)
        credited = process_conversion(
            conn, S_CONV, GUILD, USER, local_day=DAY, xp=6.0, booster=False
        )
        # 10 carried + 6 = 16 -> 1 coin, 1 XP remainder.
        assert credited == 1
        row = conn.execute(
            "SELECT remainder FROM econ_conversions "
            "WHERE guild_id=? AND user_id=? AND local_day=?",
            (GUILD, USER, DAY),
        ).fetchone()
        assert row["remainder"] == pytest.approx(1.0)


def test_process_conversion_zero_coins_writes_row_no_ledger(db):
    with open_db(db) as conn:
        assert process_conversion(
            conn, S_CONV, GUILD, USER, local_day=DAY, xp=7.0, booster=False
        ) == 0
        assert get_ledger(conn, GUILD, USER) == []
        row = conn.execute(
            "SELECT remainder FROM econ_conversions WHERE guild_id=? AND user_id=?",
            (GUILD, USER),
        ).fetchone()
        assert row["remainder"] == pytest.approx(7.0)


def test_process_conversion_booster_ceil(db):
    with open_db(db) as conn:
        credited = process_conversion(
            conn, S_CONV, GUILD, USER, local_day=DAY, xp=45.0, booster=True
        )
        # 3 coins -> ceil(3 * 1.5) == 5
        assert credited == 5


def test_process_conversion_zero_rate_carries_everything(db):
    settings = EconSettings(xp_per_coin=0.0)
    with open_db(db) as conn:
        assert process_conversion(
            conn, settings, GUILD, USER, local_day=DAY, xp=40.0, booster=False
        ) == 0
        row = conn.execute(
            "SELECT coins, remainder FROM econ_conversions "
            "WHERE guild_id=? AND user_id=?",
            (GUILD, USER),
        ).fetchone()
        assert row["coins"] == 0
        assert row["remainder"] == pytest.approx(40.0)


# ── QOTD ──────────────────────────────────────────────────────────────

CHANNEL = 42


def test_create_and_find_qotd_by_message(db):
    with open_db(db) as conn:
        qid = create_qotd(conn, GUILD, CHANNEL, 555, "Best snack?", USER, DAY)
        assert qid > 0
        row = qotd_for_message(conn, GUILD, 555)
        assert row is not None
        assert row["id"] == qid
        assert row["question"] == "Best snack?"
        assert row["posted_by"] == USER
        assert row["local_day"] == DAY
        # Another message / another guild -> no match.
        assert qotd_for_message(conn, GUILD, 556) is None
        assert qotd_for_message(conn, GUILD + 1, 555) is None


def test_qotd_for_message_keeps_old_days_findable(db):
    """Staleness is the caller's call — the row itself never expires."""
    with open_db(db) as conn:
        create_qotd(conn, GUILD, CHANNEL, 555, "Yesterday?", USER, PREV)
        row = qotd_for_message(conn, GUILD, 555)
        assert row is not None
        assert row["local_day"] == PREV


def test_try_award_qotd_once_per_member(db):
    with open_db(db) as conn:
        qid = create_qotd(conn, GUILD, CHANNEL, 555, "Q?", USER, DAY)
        assert try_award_qotd(conn, S, qid, GUILD, OTHER, booster=False) is True
        assert try_award_qotd(conn, S, qid, GUILD, OTHER, booster=False) is False
        assert get_balance(conn, GUILD, OTHER) == S.reward_qotd
        rows = get_ledger(conn, GUILD, OTHER)
        assert len(rows) == 1
        assert rows[0]["kind"] == "qotd"


def test_try_award_qotd_booster_ceil(db):
    with open_db(db) as conn:
        qid = create_qotd(conn, GUILD, CHANNEL, 555, "Q?", USER, DAY)
        assert try_award_qotd(conn, S, qid, GUILD, OTHER, booster=True) is True
        # ceil(10 * 1.5) == 15
        assert get_balance(conn, GUILD, OTHER) == 15


def test_try_award_qotd_zero_reward_still_marks(db):
    settings = EconSettings(reward_qotd=0)
    with open_db(db) as conn:
        qid = create_qotd(conn, GUILD, CHANNEL, 555, "Q?", USER, DAY)
        assert try_award_qotd(conn, settings, qid, GUILD, OTHER, booster=False) is True
        assert get_balance(conn, GUILD, OTHER) == 0
        assert try_award_qotd(conn, settings, qid, GUILD, OTHER, booster=False) is False


# ── game rewards ──────────────────────────────────────────────────────


def test_award_game_reward_amounts_and_kinds(db):
    with open_db(db) as conn:
        assert award_game_reward(
            conn, S, GUILD, USER, kind="game_participation", booster=False
        ) == S.reward_game_participation
        assert award_game_reward(
            conn, S, GUILD, USER, kind="game_win", booster=False
        ) == S.reward_game_win
        rows = get_ledger(conn, GUILD, USER)
        assert sorted(r["kind"] for r in rows) == ["game_participation", "game_win"]


def test_award_game_reward_booster_ceil(db):
    with open_db(db) as conn:
        credited = award_game_reward(
            conn, S, GUILD, USER, kind="game_participation", booster=True
        )
        # ceil(5 * 1.5) == 8
        assert credited == 8


def test_award_game_reward_unknown_kind_raises(db):
    with open_db(db) as conn:
        with pytest.raises(ValueError):
            award_game_reward(conn, S, GUILD, USER, kind="game_loss", booster=False)


def test_award_game_reward_zero_amount_no_writes(db):
    settings = EconSettings(reward_game_participation=0)
    with open_db(db) as conn:
        assert award_game_reward(
            conn, settings, GUILD, USER, kind="game_participation", booster=False
        ) == 0
        assert get_ledger(conn, GUILD, USER) == []


# ── booster check + notifications ─────────────────────────────────────


def _fake_bot(*, guild=None):
    bot = MagicMock()
    bot.get_guild.return_value = guild
    return bot


def _fake_guild(*, member=None, channel=None):
    guild = MagicMock()
    guild.get_member.return_value = member
    guild.get_channel.return_value = channel
    return guild


def _fake_member(*, premium=None, role_ids=()):
    member = MagicMock(spec=discord.Member)
    member.premium_since = premium
    member.roles = [MagicMock(id=rid) for rid in role_ids]
    return member


def _forbidden() -> discord.Forbidden:
    return discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "no DMs")


def test_member_is_booster_true():
    guild = _fake_guild(member=_fake_member(premium=object()))
    assert member_is_booster(_fake_bot(guild=guild), GUILD, USER) is True


def test_member_is_booster_false_cases():
    assert member_is_booster(_fake_bot(guild=None), GUILD, USER) is False
    assert member_is_booster(_fake_bot(guild=_fake_guild()), GUILD, USER) is False
    guild = _fake_guild(member=_fake_member(premium=None))
    assert member_is_booster(_fake_bot(guild=guild), GUILD, USER) is False


async def test_notify_member_muted_drops_silently(db):
    with open_db(db) as conn:
        set_notify_muted(conn, GUILD, USER, True)
    member = _fake_member()
    bot = _fake_bot(guild=_fake_guild(member=member))
    assert await notify_member(bot, db, GUILD, USER, content="hi") is True
    member.send.assert_not_called()


async def test_notify_member_dm_success(db):
    member = _fake_member()
    bot = _fake_bot(guild=_fake_guild(member=member))
    assert await notify_member(bot, db, GUILD, USER, content="hi") is True
    member.send.assert_awaited_once_with(content="hi")


async def test_notify_member_dm_forbidden_falls_back_to_bank_channel(db):
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"bank_channel_id": 777})
    member = _fake_member()
    member.send.side_effect = _forbidden()
    channel = MagicMock(spec=discord.TextChannel)
    bot = _fake_bot(guild=_fake_guild(member=member, channel=channel))
    assert await notify_member(bot, db, GUILD, USER, content="hi") is True
    channel.send.assert_awaited_once()
    kwargs = channel.send.await_args.kwargs
    assert f"<@{USER}>" in kwargs["content"]
    assert "hi" in kwargs["content"]
    # The bank channel is public and some callers pass raw member-authored
    # bodies; the fallback must restrict pings to the target member so an
    # embedded @everyone / role / other-user mention can't fire.
    am = kwargs["allowed_mentions"]
    assert am.everyone is False
    assert am.roles is False
    assert [u.id for u in am.users] == [USER]


async def test_notify_member_no_fallback_configured_returns_false(db):
    member = _fake_member()
    member.send.side_effect = _forbidden()
    bot = _fake_bot(guild=_fake_guild(member=member))
    assert await notify_member(bot, db, GUILD, USER, content="hi") is False


async def test_notify_member_both_fail_returns_false(db):
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"bank_channel_id": 777})
    member = _fake_member()
    member.send.side_effect = _forbidden()
    channel = MagicMock(spec=discord.TextChannel)
    channel.send.side_effect = _forbidden()
    bot = _fake_bot(guild=_fake_guild(member=member, channel=channel))
    assert await notify_member(bot, db, GUILD, USER, content="hi") is False


async def test_notify_member_member_gone_uses_bank_channel(db):
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"bank_channel_id": 777})
    channel = MagicMock(spec=discord.TextChannel)
    bot = _fake_bot(guild=_fake_guild(member=None, channel=channel))
    assert await notify_member(bot, db, GUILD, USER, content="hi") is True
    channel.send.assert_awaited_once()


async def test_notify_member_require_role_delivers_to_role_holder(db):
    """With a game role set, an opted-in member still gets the DM."""
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"game_role_id": 777})
    member = _fake_member(role_ids=(777,))
    bot = _fake_bot(guild=_fake_guild(member=member))
    delivered = await notify_member(
        bot, db, GUILD, USER, content="hi", require_game_role=True
    )
    assert delivered is True
    member.send.assert_awaited_once_with(content="hi")


async def test_notify_member_require_role_drops_non_holder_silently(db):
    """A member without the opt-in role is dropped silently (no DM, no bank
    fallback) — counts as handled, mirroring a mute."""
    with open_db(db) as conn:
        save_econ_settings(
            conn, GUILD, {"game_role_id": 777, "bank_channel_id": 555}
        )
    member = _fake_member(role_ids=(111,))
    channel = MagicMock(spec=discord.TextChannel)
    bot = _fake_bot(guild=_fake_guild(member=member, channel=channel))
    delivered = await notify_member(
        bot, db, GUILD, USER, content="hi", require_game_role=True
    )
    assert delivered is True
    member.send.assert_not_called()
    channel.send.assert_not_called()


async def test_notify_member_require_role_drops_all_when_no_role_configured(db):
    """With no game role configured, nobody has opted in — dropped silently."""
    member = _fake_member(role_ids=())
    channel = MagicMock(spec=discord.TextChannel)
    bot = _fake_bot(guild=_fake_guild(member=member, channel=channel))
    delivered = await notify_member(
        bot, db, GUILD, USER, content="hi", require_game_role=True
    )
    assert delivered is True
    member.send.assert_not_called()
    channel.send.assert_not_called()


# ── transfers ─────────────────────────────────────────────────────────


def test_transfer_roundtrip_moves_balance_and_ledgers_both_sides(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 100, "grant")
    with open_db(db) as conn:
        transfer_currency(conn, GUILD, USER, OTHER, 30)
    with open_db(db) as conn:
        assert get_balance(conn, GUILD, USER) == 70
        assert get_balance(conn, GUILD, OTHER) == 30
        out = get_ledger(conn, GUILD, USER, limit=1)[0]
        assert out["kind"] == "transfer_out"
        assert out["amount"] == -30
        assert out["actor_id"] == USER
        import json
        assert json.loads(out["meta"]) == {"to": OTHER}
        inc = get_ledger(conn, GUILD, OTHER, limit=1)[0]
        assert inc["kind"] == "transfer_in"
        assert inc["amount"] == 30
        assert inc["actor_id"] == USER
        assert json.loads(inc["meta"]) == {"from": USER}


def test_transfer_memo_lands_on_both_ledger_sides(db):
    import json

    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 100, "grant")
    with open_db(db) as conn:
        transfer_currency(conn, GUILD, USER, OTHER, 30, memo="rent money")
    with open_db(db) as conn:
        out = get_ledger(conn, GUILD, USER, limit=1)[0]
        assert json.loads(out["meta"]) == {"to": OTHER, "memo": "rent money"}
        inc = get_ledger(conn, GUILD, OTHER, limit=1)[0]
        assert json.loads(inc["meta"]) == {"from": USER, "memo": "rent money"}


def test_transfer_without_memo_omits_the_key(db):
    """No memo must leave meta exactly as it was before memos existed."""
    import json

    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 100, "grant")
    with open_db(db) as conn:
        transfer_currency(conn, GUILD, USER, OTHER, 30, memo=None)
    with open_db(db) as conn:
        assert json.loads(get_ledger(conn, GUILD, USER, limit=1)[0]["meta"]) == {
            "to": OTHER
        }


def test_transfer_insufficient_is_zero_write(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 10, "grant")
    with open_db(db) as conn:
        with pytest.raises(ValueError, match="insufficient"):
            transfer_currency(conn, GUILD, USER, OTHER, 11)
    with open_db(db) as conn:
        assert get_balance(conn, GUILD, USER) == 10
        assert get_balance(conn, GUILD, OTHER) == 0
        # No ledger rows written on either side.
        assert get_ledger(conn, GUILD, USER) == [] or all(
            r["kind"] != "transfer_out" for r in get_ledger(conn, GUILD, USER)
        )
        assert get_ledger(conn, GUILD, OTHER) == []


def test_transfer_to_self_rejected_before_any_write(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 50, "grant")
    with open_db(db) as conn:
        with pytest.raises(ValueError, match="yourself"):
            transfer_currency(conn, GUILD, USER, USER, 10)
    with open_db(db) as conn:
        assert get_balance(conn, GUILD, USER) == 50


def test_transfer_below_min_rejected(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 50, "grant")
    with open_db(db) as conn:
        with pytest.raises(ValueError, match=">= 1"):
            transfer_currency(conn, GUILD, USER, OTHER, 0)
        with pytest.raises(ValueError, match=">= 1"):
            transfer_currency(conn, GUILD, USER, OTHER, -5)


def test_transfer_does_not_mint_no_booster_multiplier(db):
    # Even with a hefty booster multiplier configured, a transfer credits the
    # recipient exactly what the sender paid — transfers never mint currency.
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"booster_multiplier": 10.0})
        apply_credit(conn, GUILD, USER, 100, "grant")
    with open_db(db) as conn:
        transfer_currency(conn, GUILD, USER, OTHER, 40)
    with open_db(db) as conn:
        assert get_balance(conn, GUILD, USER) == 60
        assert get_balance(conn, GUILD, OTHER) == 40


def test_transfer_min_amount_one(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 1, "grant")
    with open_db(db) as conn:
        transfer_currency(conn, GUILD, USER, OTHER, 1)
    with open_db(db) as conn:
        assert get_balance(conn, GUILD, USER) == 0
        assert get_balance(conn, GUILD, OTHER) == 1


# ── streak shield: purchase + consumption (sinks round 3, stage 2) ────


def _shield_price():
    return S.price_streak_shield


def test_purchase_shield_debits_and_holds(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 100, "grant")
        charged = purchase_streak_shield(conn, S, GUILD, USER)
        assert charged == _shield_price()
        assert get_balance(conn, GUILD, USER) == 100 - _shield_price()
        assert get_streak_shields(conn, GUILD, USER) == 1
        assert [r["kind"] for r in get_ledger(conn, GUILD, USER)][0] == "streak_shield"


def test_purchase_shield_refused_while_holding(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 100, "grant")
        purchase_streak_shield(conn, S, GUILD, USER)
        with pytest.raises(ValueError, match="already holding"):
            purchase_streak_shield(conn, S, GUILD, USER)
        # Charged exactly once.
        assert get_balance(conn, GUILD, USER) == 100 - _shield_price()


def test_purchase_shield_insufficient_leaves_no_claim(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, _shield_price() - 1, "grant")
    # The raise must escape the transaction block (as it does in the cog) so
    # the claim-before-debit upsert rolls back with everything else.
    with pytest.raises(ValueError, match="insufficient"):
        with open_db(db) as conn:
            purchase_streak_shield(conn, S, GUILD, USER)
    with open_db(db) as conn:
        assert get_streak_shields(conn, GUILD, USER) == 0
        assert get_balance(conn, GUILD, USER) == _shield_price() - 1


def test_refund_shield_credits_price_paid_not_current_price(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 100, "grant")
        purchase_streak_shield(conn, S, GUILD, USER)  # price 30, snapshotted
        # The guild re-prices the shield AFTER purchase — the refund must
        # still honor what was actually paid, not the new current price.
        save_econ_settings(conn, GUILD, {"price_streak_shield": 500})
        repriced = load_econ_settings(conn, GUILD)
        assert repriced.price_streak_shield == 500
        refunded = refund_streak_shield(conn, GUILD, USER, repriced)
        assert refunded == _shield_price()  # the ORIGINAL 30, not 500
        assert get_balance(conn, GUILD, USER) == 100
        assert get_streak_shields(conn, GUILD, USER) == 0
        assert [r["kind"] for r in get_ledger(conn, GUILD, USER)][0] == (
            "streak_shield_refund"
        )


def test_refund_shield_legacy_zero_price_falls_back_to_current(db):
    # A shield held before migration 114 added shield_price snapshots as the
    # column default 0 — indistinguishable from "unheld" otherwise. The
    # refund must fall back to the guild's current price rather than hiding
    # the option or crediting nothing.
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 100, "grant")
        conn.execute(
            "INSERT INTO econ_streaks (guild_id, user_id, shields, shield_price) "
            "VALUES (?, ?, 1, 0)",
            (GUILD, USER),
        )
        assert get_streak_shield_price(conn, GUILD, USER, S) == _shield_price()
        refunded = refund_streak_shield(conn, GUILD, USER, S)
        assert refunded == _shield_price()
        assert get_balance(conn, GUILD, USER) == 100 + _shield_price()


def test_refund_shield_none_held_rejected(db):
    with open_db(db) as conn:
        with pytest.raises(ValueError, match="no shield held"):
            refund_streak_shield(conn, GUILD, USER, S)


def test_refund_shield_exactly_once(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 100, "grant")
        purchase_streak_shield(conn, S, GUILD, USER)
        refund_streak_shield(conn, GUILD, USER, S)
        with pytest.raises(ValueError, match="no shield held"):
            refund_streak_shield(conn, GUILD, USER, S)
        # A second attempt must not credit a second refund.
        assert get_balance(conn, GUILD, USER) == 100


def test_purchase_shield_allowed_at_streak_zero(db):
    # No econ_streaks row at all yet — the INSERT arm of the upsert claims it.
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 100, "grant")
        purchase_streak_shield(conn, S, GUILD, USER)
        row = _streak_row(conn)
        assert row["shields"] == 1
        assert row["current_streak"] == 0


def test_login_consumes_shield_when_grace_burned(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 100, "grant")
        # Day 1 login, then a graced miss, then logins to build the streak.
        process_login(conn, S, GUILD, USER, local_day="2026-07-01", source="text", booster=False)
        process_login(conn, S, GUILD, USER, local_day="2026-07-03", source="text", booster=False)  # grace covers 07-02
        purchase_streak_shield(conn, S, GUILD, USER)
        # Miss 07-04: grace is inside the rolling window -> shield burns.
        out = process_login(conn, S, GUILD, USER, local_day="2026-07-05", source="text", booster=False)
        assert out is not None
        assert out.shield_consumed is True
        assert out.grace_consumed is False
        assert out.reset is False
        assert out.streak == 3
        assert get_streak_shields(conn, GUILD, USER) == 0


def test_login_gap_three_consumes_grace_and_shield(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 100, "grant")
        process_login(conn, S, GUILD, USER, local_day="2026-07-01", source="text", booster=False)
        process_login(conn, S, GUILD, USER, local_day="2026-07-02", source="text", booster=False)
        purchase_streak_shield(conn, S, GUILD, USER)
        # Miss 07-03 AND 07-04 -> grace + shield together keep it alive.
        out = process_login(conn, S, GUILD, USER, local_day="2026-07-05", source="text", booster=False)
        assert out is not None
        assert out.grace_consumed is True
        assert out.shield_consumed is True
        assert out.streak == 3
        row = _streak_row(conn)
        assert row["shields"] == 0
        assert row["last_grace_day"] == "2026-07-03"


def test_login_hopeless_gap_keeps_shield(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 100, "grant")
        process_login(conn, S, GUILD, USER, local_day="2026-07-01", source="text", booster=False)
        purchase_streak_shield(conn, S, GUILD, USER)
        # Four missed days -> reset; the shield must survive for next time.
        out = process_login(conn, S, GUILD, USER, local_day="2026-07-06", source="text", booster=False)
        assert out is not None
        assert out.reset is True
        assert out.shield_consumed is False
        assert get_streak_shields(conn, GUILD, USER) == 1


def test_purchase_shield_fires_shop_purchase_trigger(db):
    from bot_modules.services.economy_service import save_econ_settings

    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
        apply_credit(conn, GUILD, USER, 100, "grant")
        purchase_streak_shield(conn, S, GUILD, USER)
        row = conn.execute(
            "SELECT 1 FROM econ_kind_activity WHERE guild_id = ? "
            "AND user_id = ? AND kind = 'shop_purchase'",
            (GUILD, USER),
        ).fetchone()
        assert row is not None
