"""Tests for services/economy_loop.run_guild_day_roll (the hourly tick body)."""

from __future__ import annotations

from datetime import datetime

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_loop import run_guild_day_roll
from bot_modules.services.economy_service import get_balance, save_econ_settings
from migrations import apply_migrations_sync

GUILD = 123
USER = 1001
OTHER = 1002

D1 = "2026-07-10"
D2 = "2026-07-11"
D3 = "2026-07-12"


def _ts(day: str, hour: int = 12) -> float:
    return datetime.fromisoformat(f"{day}T{hour:02d}:00:00+00:00").timestamp()


# ── fake discord objects ──────────────────────────────────────────────


class _Member:
    def __init__(self, booster: bool = False) -> None:
        self.premium_since = object() if booster else None


class _Guild:
    def __init__(self, gid: int, members: dict[int, _Member] | None = None) -> None:
        self.id = gid
        self._members = members or {}

    def get_member(self, uid: int) -> _Member | None:
        return self._members.get(uid)


class _Bot:
    def __init__(self, guilds: list[_Guild]) -> None:
        self.guilds = list(guilds)
        self._by_id = {g.id: g for g in guilds}

    def get_guild(self, gid: int) -> _Guild | None:
        return self._by_id.get(gid)


# ── helpers ───────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    apply_migrations_sync(db_path)
    return db_path


def _enable(db_path, guild_id=GUILD, **overrides) -> None:
    values: dict[str, object] = {"enabled": True, "xp_per_coin": 10.0}
    values.update(overrides)
    with open_db(db_path) as conn:
        save_econ_settings(conn, guild_id, values)


def _add_xp(db_path, user_id, amount, ts, guild_id=GUILD) -> None:
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO xp_events (guild_id, user_id, source, amount, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (guild_id, user_id, "message", amount, ts),
        )


def _roll(bot, db_path, now_ts, guild_id=GUILD) -> None:
    with open_db(db_path) as conn:
        run_guild_day_roll(bot, conn, guild_id, now_ts)


def _mark(db_path, guild_id=GUILD) -> str | None:
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT last_local_day FROM econ_day_marks WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    return row["last_local_day"] if row else None


def _balance(db_path, user_id=USER, guild_id=GUILD) -> int:
    with open_db(db_path) as conn:
        return get_balance(conn, guild_id, user_id)


def _conversion_count(db_path, guild_id=GUILD) -> int:
    with open_db(db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM econ_conversions WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()["n"]


# ── tests ─────────────────────────────────────────────────────────────


def test_first_run_sets_mark_only(db):
    _enable(db)
    _add_xp(db, USER, 100.0, _ts(D1))  # present but must NOT be converted
    bot = _Bot([_Guild(GUILD)])

    _roll(bot, db, _ts(D1))

    assert _mark(db) == D1
    assert _balance(db) == 0
    assert _conversion_count(db) == 0


def test_day_roll_converts_all_users_and_advances_mark(db):
    _enable(db)
    bot = _Bot([_Guild(GUILD)])
    _roll(bot, db, _ts(D1))  # first run → mark D1

    _add_xp(db, USER, 100.0, _ts(D1))
    _add_xp(db, OTHER, 50.0, _ts(D1))
    _roll(bot, db, _ts(D2))  # roll → convert D1

    assert _mark(db) == D2
    assert _balance(db, USER) == 10  # 100 / 10
    assert _balance(db, OTHER) == 5  # 50 / 10
    assert _conversion_count(db) == 2


def test_double_tick_same_day_converts_nothing_new(db):
    _enable(db)
    bot = _Bot([_Guild(GUILD)])
    _roll(bot, db, _ts(D1))
    _add_xp(db, USER, 100.0, _ts(D1))
    _roll(bot, db, _ts(D2))
    assert _balance(db) == 10

    # Same local day again — the mark guard short-circuits.
    _roll(bot, db, _ts(D2, hour=18))

    assert _balance(db) == 10
    assert _conversion_count(db) == 1


def test_remainder_carries_across_two_consecutive_rolls(db):
    _enable(db)  # xp_per_coin = 10
    bot = _Bot([_Guild(GUILD)])
    _roll(bot, db, _ts(D1))  # first run → mark D1

    _add_xp(db, USER, 15.0, _ts(D1))
    _roll(bot, db, _ts(D2))  # 15 → 1 coin, remainder 5
    assert _balance(db) == 1

    _add_xp(db, USER, 15.0, _ts(D2))
    _roll(bot, db, _ts(D3))  # 15 + carry 5 = 20 → 2 coins

    assert _balance(db) == 3  # 1 + 2
    assert _mark(db) == D3


def test_disabled_guild_skipped(db):
    # economy not enabled — no mark, no conversion even with XP present.
    _add_xp(db, USER, 100.0, _ts(D1))
    bot = _Bot([_Guild(GUILD)])

    _roll(bot, db, _ts(D1))
    _roll(bot, db, _ts(D2))

    assert _mark(db) is None
    assert _balance(db) == 0
    assert _conversion_count(db) == 0


def test_booster_member_gets_ceil(db):
    _enable(db)  # booster_multiplier default 1.5
    bot = _Bot([_Guild(GUILD, {USER: _Member(booster=True)})])
    _roll(bot, db, _ts(D1))

    _add_xp(db, USER, 100.0, _ts(D1))
    _roll(bot, db, _ts(D2))

    assert _balance(db) == 15  # ceil(10 * 1.5)


def test_guild_missing_from_cache_handled(db):
    # Guild not in bot.guilds cache → member_is_booster returns False, but the
    # conversion still proceeds (non-booster credit).
    _enable(db)
    bot = _Bot([])  # GUILD is absent from the cache
    _roll(bot, db, _ts(D1))

    _add_xp(db, USER, 100.0, _ts(D1))
    _roll(bot, db, _ts(D2))

    assert _mark(db) == D2
    assert _balance(db) == 10  # non-booster amount


def test_crash_between_conversions_and_mark_update_replays_safely(db):
    _enable(db)
    bot = _Bot([_Guild(GUILD)])
    _roll(bot, db, _ts(D1))
    _add_xp(db, USER, 100.0, _ts(D1))
    _roll(bot, db, _ts(D2))
    assert _balance(db) == 10

    # Simulate a crash after conversions but before the mark advanced: rewind
    # the mark to D1 and replay the same tick. process_conversion idempotency
    # must prevent a second credit.
    with open_db(db) as conn:
        conn.execute(
            "UPDATE econ_day_marks SET last_local_day = ? WHERE guild_id = ?",
            (D1, GUILD),
        )

    _roll(bot, db, _ts(D2))

    assert _balance(db) == 10  # unchanged
    assert _conversion_count(db) == 1
    assert _mark(db) == D2
