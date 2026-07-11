"""Tests for services/economy_service.py."""

from __future__ import annotations

from unittest.mock import MagicMock

import discord
import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from migrations import apply_migrations_sync
from bot_modules.services.economy_service import (
    DEFAULT_ECON_SETTINGS,
    ECON_PREFIX,
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
    open_qotd_for,
    process_conversion,
    process_login,
    save_econ_settings,
    set_notify_muted,
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


def test_process_conversion_basic_credit(db):
    with open_db(db) as conn:
        credited = process_conversion(
            conn, S, GUILD, USER, local_day=DAY, xp=31.0, booster=False
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
            conn, S, GUILD, USER, local_day=DAY, xp=31.0, booster=False
        ) == 2
        assert process_conversion(
            conn, S, GUILD, USER, local_day=DAY, xp=31.0, booster=False
        ) == 0
        assert get_balance(conn, GUILD, USER) == 2
        assert len(get_ledger(conn, GUILD, USER)) == 1


def test_process_conversion_remainder_carries_across_days(db):
    with open_db(db) as conn:
        process_conversion(conn, S, GUILD, USER, local_day=PREV, xp=10.0, booster=False)
        credited = process_conversion(
            conn, S, GUILD, USER, local_day=DAY, xp=6.0, booster=False
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
            conn, S, GUILD, USER, local_day=DAY, xp=7.0, booster=False
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
            conn, S, GUILD, USER, local_day=DAY, xp=45.0, booster=True
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


def test_create_and_open_qotd(db):
    with open_db(db) as conn:
        qid = create_qotd(conn, GUILD, CHANNEL, 555, "Best snack?", USER, DAY)
        assert qid > 0
        row = open_qotd_for(conn, GUILD, CHANNEL, DAY)
        assert row is not None
        assert row["id"] == qid
        assert row["question"] == "Best snack?"
        assert row["posted_by"] == USER
        # Wrong day / wrong channel -> no match.
        assert open_qotd_for(conn, GUILD, CHANNEL, PREV) is None
        assert open_qotd_for(conn, GUILD, CHANNEL + 1, DAY) is None


def test_open_qotd_latest_wins(db):
    with open_db(db) as conn:
        create_qotd(conn, GUILD, CHANNEL, 555, "First?", USER, DAY)
        second = create_qotd(conn, GUILD, CHANNEL, 556, "Second?", USER, DAY)
        row = open_qotd_for(conn, GUILD, CHANNEL, DAY)
        assert row is not None
        assert row["id"] == second


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


def _fake_member(*, premium=None):
    member = MagicMock(spec=discord.Member)
    member.premium_since = premium
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
