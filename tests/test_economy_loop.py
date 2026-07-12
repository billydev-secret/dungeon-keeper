"""Tests for services/economy_loop — the hourly tick body.

Covers the Stage-0/1 XP→currency day roll and the Stage-2 quest surface it
also drives: daily/weekly rotate-tag rotation, community settlement on the ISO
week change, and stale sign-off claim expiry (with after-commit DMs).
"""

from __future__ import annotations

import time
from datetime import datetime

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services import economy_loop
from bot_modules.services.economy_loop import (
    run_claim_expiry,
    run_guild_day_roll,
    run_tick,
)
from bot_modules.services.economy_quests_service import (
    claim_quest,
    create_quest,
    set_community_progress,
    set_quest_active,
)
from bot_modules.services.economy_service import (
    get_balance,
    load_econ_settings,
    save_econ_settings,
)
from migrations import apply_migrations_sync

GUILD = 123
USER = 1001
OTHER = 1002

D1 = "2026-07-10"  # Fri, ISO 2026-W28
D2 = "2026-07-11"  # Sat, ISO 2026-W28  (same week as D1 → day roll only)
D3 = "2026-07-12"  # Sun, ISO 2026-W28
DSUN = "2026-07-12"  # Sun, ISO 2026-W28
DMON = "2026-07-13"  # Mon, ISO 2026-W29  (DSUN→DMON crosses the ISO week)
WEEK_28 = "2026-W28"


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


async def _tick(bot, db_path, now_ts) -> None:
    await run_tick(bot, db_path, now_ts)


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


# ── Stage-2 quest surface ─────────────────────────────────────────────


def _create_quest(
    db_path,
    *,
    qtype,
    reward=0,
    signoff=0,
    rotate_tag="",
    community_target=None,
    active=False,
    guild_id=GUILD,
) -> int:
    with open_db(db_path) as conn:
        qid = create_quest(
            conn,
            guild_id,
            title=f"{qtype}-{rotate_tag or 'q'}",
            description="",
            qtype=qtype,
            reward=reward,
            signoff=signoff,
            criteria="",
            starts_at=None,
            ends_at=None,
            rotate_tag=rotate_tag,
            community_target=community_target,
            created_by=None,
        )
        if active:
            set_quest_active(conn, guild_id, qid, True)
    return qid


def _is_active(db_path, qid) -> bool:
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT active FROM econ_quests WHERE id = ?", (qid,)
        ).fetchone()
    return bool(row["active"])


def _set_progress(db_path, qid, current, target) -> None:
    with open_db(db_path) as conn:
        set_community_progress(conn, qid, current, target=target)


def _add_activity(db_path, user_id, guild_id=GUILD, when=None) -> None:
    when = time.time() if when is None else when
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO member_activity "
            "(guild_id, user_id, last_channel_id, last_message_id, last_message_at) "
            "VALUES (?, ?, 0, 0, ?)",
            (guild_id, user_id, when),
        )


def _payout_count(db_path, qid) -> int:
    with open_db(db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM econ_community_payouts WHERE quest_id = ?",
            (qid,),
        ).fetchone()["n"]


def _settled_at(db_path, qid):
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT settled_at FROM econ_community_progress WHERE quest_id = ?",
            (qid,),
        ).fetchone()
    return row["settled_at"] if row else None


def _ledger_kind_count(db_path, kind, guild_id=GUILD) -> int:
    with open_db(db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM econ_ledger WHERE guild_id = ? AND kind = ?",
            (guild_id, kind),
        ).fetchone()["n"]


def _rewind_marks(db_path, local_day, iso_week, guild_id=GUILD) -> None:
    with open_db(db_path) as conn:
        conn.execute(
            "UPDATE econ_day_marks SET last_local_day = ?, last_iso_week = ? "
            "WHERE guild_id = ?",
            (local_day, iso_week, guild_id),
        )


def _make_pending_claim(db_path, quest_id, user_id, period, guild_id=GUILD) -> None:
    with open_db(db_path) as conn:
        settings = load_econ_settings(conn, guild_id)
        claim_quest(
            conn,
            settings,
            guild_id,
            quest_id,
            user_id,
            period=period,
            booster=False,
        )


# ── daily rotation (day roll only) ─────────────────────────────────────


def test_daily_rotation_fires_on_day_roll_weekly_pool_untouched(db):
    _enable(db)
    d_a = _create_quest(db, qtype="daily", rotate_tag="dy", active=True)
    d_b = _create_quest(db, qtype="daily", rotate_tag="dy")
    w_1 = _create_quest(db, qtype="weekly", rotate_tag="wk", active=True)
    w_2 = _create_quest(db, qtype="weekly", rotate_tag="wk")
    bot = _Bot([_Guild(GUILD)])

    _roll(bot, db, _ts(D1))  # first sight → mark only
    _roll(bot, db, _ts(D2))  # same ISO week: day roll, no week roll

    assert not _is_active(db, d_a)  # daily pool advanced …
    assert _is_active(db, d_b)
    assert _is_active(db, w_1)  # … weekly pool did NOT
    assert not _is_active(db, w_2)


def test_disabled_guild_leaves_quests_untouched(db):
    # No _enable → run_guild_day_roll returns before any rotation or mark.
    d_a = _create_quest(db, qtype="daily", rotate_tag="dy", active=True)
    d_b = _create_quest(db, qtype="daily", rotate_tag="dy")
    bot = _Bot([_Guild(GUILD)])

    _roll(bot, db, _ts(D1))
    _roll(bot, db, _ts(D2))

    assert _mark(db) is None
    assert _is_active(db, d_a)
    assert not _is_active(db, d_b)


# ── weekly rotation + community settlement (ISO week change) ───────────


def test_week_roll_rotates_weekly_and_settles_community(db):
    _enable(db)
    w_1 = _create_quest(db, qtype="weekly", rotate_tag="wk", active=True)
    w_2 = _create_quest(db, qtype="weekly", rotate_tag="wk")
    qc = _create_quest(db, qtype="community", reward=30, community_target=5, active=True)
    _set_progress(db, qc, 5, 5)  # completed
    _add_activity(db, USER)
    _add_activity(db, OTHER)
    bot = _Bot([_Guild(GUILD)])

    _roll(bot, db, _ts(DSUN))  # first sight → mark (W28)
    _roll(bot, db, _ts(DMON))  # week change W28 → W29

    assert not _is_active(db, w_1)  # weekly pool advanced
    assert _is_active(db, w_2)
    assert _payout_count(db, qc) == 2
    assert _settled_at(db, qc) is not None
    assert _ledger_kind_count(db, "quest_community") == 2
    assert _balance(db, USER) == 30
    assert _balance(db, OTHER) == 30


def test_same_week_day_roll_does_not_settle_community(db):
    _enable(db)
    qc = _create_quest(db, qtype="community", reward=30, community_target=5, active=True)
    _set_progress(db, qc, 5, 5)
    _add_activity(db, USER)
    bot = _Bot([_Guild(GUILD)])

    _roll(bot, db, _ts(D1))  # W28
    _roll(bot, db, _ts(D2))  # still W28 → day roll only

    assert _payout_count(db, qc) == 0
    assert _settled_at(db, qc) is None
    assert _balance(db, USER) == 0


def test_signoff_community_not_auto_settled(db):
    _enable(db)
    qc = _create_quest(
        db, qtype="community", reward=30, signoff=1, community_target=5, active=True
    )
    _set_progress(db, qc, 5, 5)  # completed but needs manual sign-off
    _add_activity(db, USER)
    bot = _Bot([_Guild(GUILD)])

    _roll(bot, db, _ts(DSUN))
    _roll(bot, db, _ts(DMON))  # week change

    assert _payout_count(db, qc) == 0
    assert _settled_at(db, qc) is None
    assert _balance(db, USER) == 0


def test_community_settlement_booster_ceil(db):
    _enable(db)  # booster_multiplier default 1.5
    qc = _create_quest(db, qtype="community", reward=30, community_target=1, active=True)
    _set_progress(db, qc, 1, 1)
    _add_activity(db, USER)
    bot = _Bot([_Guild(GUILD, {USER: _Member(booster=True)})])

    _roll(bot, db, _ts(DSUN))
    _roll(bot, db, _ts(DMON))

    assert _balance(db, USER) == 45  # ceil(30 * 1.5)


def test_community_settlement_exactly_once_on_crash_replay(db):
    _enable(db)
    qc = _create_quest(db, qtype="community", reward=30, community_target=1, active=True)
    _set_progress(db, qc, 1, 1)
    _add_activity(db, USER)
    _add_activity(db, OTHER)
    bot = _Bot([_Guild(GUILD)])

    _roll(bot, db, _ts(DSUN))
    _roll(bot, db, _ts(DMON))  # settles both members
    assert _payout_count(db, qc) == 2
    assert _ledger_kind_count(db, "quest_community") == 2

    # Simulate a crash after settlement but before the mark advanced: rewind
    # BOTH marks so the replay re-enters the roll path (not the day==today
    # early return). The quest is already settled (settled_at set), so
    # list_settleable_community_quests filters it out — this exercises the
    # settled_at guard + mark idempotency, NOT the reserve-row path (that is
    # covered by test_settlement_reserve_rows_prevent_double_credit).
    _rewind_marks(db, DSUN, WEEK_28)
    _roll(bot, db, _ts(DMON))

    assert _payout_count(db, qc) == 2  # no double reservation
    assert _ledger_kind_count(db, "quest_community") == 2  # no double credit
    assert _balance(db, USER) == 30
    assert _balance(db, OTHER) == 30


def test_settlement_reserve_rows_prevent_double_credit(db):
    # Drive settle_community_quest a second time with a payout row already
    # present: the reserve-row INSERT OR IGNORE must skip the re-credit.
    _enable(db)
    qc = _create_quest(db, qtype="community", reward=30, community_target=1, active=True)
    _set_progress(db, qc, 1, 1)
    _add_activity(db, USER)
    bot = _Bot([_Guild(GUILD)])
    _roll(bot, db, _ts(DSUN))
    _roll(bot, db, _ts(DMON))  # settles USER once; reserve row written
    assert _balance(db, USER) == 30

    # Clear settled_at so the quest is settleable again, but KEEP the payout
    # reservation, then replay: the reserve row is the only thing stopping a
    # second credit.
    with open_db(db) as conn:
        conn.execute(
            "UPDATE econ_community_progress SET settled_at = NULL WHERE quest_id = ?",
            (qc,),
        )
    _rewind_marks(db, DSUN, WEEK_28)
    _roll(bot, db, _ts(DMON))

    assert _balance(db, USER) == 30  # not 60
    assert _payout_count(db, qc) == 1
    assert _ledger_kind_count(db, "quest_community") == 1


def test_null_last_iso_week_backfills_without_retroactive_settle(db):
    # A pre-064 mark row has NULL last_iso_week; a week roll off it must backfill
    # the week rather than fire a spurious community settlement.
    _enable(db)
    qc = _create_quest(db, qtype="community", reward=30, community_target=1, active=True)
    _set_progress(db, qc, 1, 1)
    _add_activity(db, USER)
    bot = _Bot([_Guild(GUILD)])

    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO econ_day_marks (guild_id, last_local_day, last_iso_week) "
            "VALUES (?, ?, NULL)",
            (GUILD, DSUN),
        )

    _roll(bot, db, _ts(DMON))  # week 'changes' but last_week was NULL

    assert _payout_count(db, qc) == 0
    assert _settled_at(db, qc) is None
    assert _balance(db, USER) == 0
    with open_db(db) as conn:
        week = conn.execute(
            "SELECT last_iso_week FROM econ_day_marks WHERE guild_id = ?", (GUILD,)
        ).fetchone()["last_iso_week"]
    assert week == "2026-W29"  # backfilled to the current week


# ── stale claim expiry + DMs (run_tick) ───────────────────────────────


class _NotifyRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int, str | None]] = []

    async def __call__(
        self, bot, db_path, guild_id, user_id, *, embed=None, content=None
    ) -> bool:
        self.calls.append((guild_id, user_id, content))
        return True


async def test_run_tick_expires_stale_claims_and_dms_once(db, monkeypatch):
    _enable(db)
    quest_id = _create_quest(db, qtype="daily", reward=15, signoff=1, active=True)
    _make_pending_claim(db, quest_id, USER, D1)
    _make_pending_claim(db, quest_id, OTHER, D1)

    recorder = _NotifyRecorder()
    monkeypatch.setattr(economy_loop, "notify_member", recorder)
    bot = _Bot([])  # isolate expiry from the per-guild roll

    # Claims were created at wall-clock now; expire 8 days in the future so the
    # 7-day cutoff catches them (expire_stale_claims reads now_ts, not the clock).
    future = time.time() + 8 * 86400
    await _tick(bot, db, future)

    dmed = {(gid, uid) for gid, uid, _ in recorder.calls}
    assert dmed == {(GUILD, USER), (GUILD, OTHER)}
    assert all("expired" in (content or "") for _, _, content in recorder.calls)

    # Second tick: rows are already 'expired', so no one is DM'd again.
    recorder.calls.clear()
    await _tick(bot, db, future + 3600)
    assert recorder.calls == []


def test_run_claim_expiry_returns_notices_with_quest_title(db):
    _enable(db)
    quest_id = _create_quest(db, qtype="daily", reward=15, signoff=1, active=True)
    _make_pending_claim(db, quest_id, USER, D1)

    future = time.time() + 8 * 86400
    with open_db(db) as conn:
        notices = run_claim_expiry(conn, future)

    assert len(notices) == 1
    assert notices[0].user_id == USER
    assert notices[0].guild_id == GUILD
    assert notices[0].quest_id == quest_id
    assert notices[0].quest_title  # non-empty, drawn from the quest row
