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
from bot_modules.economy.rentals import GRACE_SECONDS, WEEK_SECONDS
from bot_modules.services import economy_loop
from bot_modules.services.economy_loop import (
    run_claim_expiry,
    run_guild_day_roll,
    run_guild_rentals,
    run_tick,
)
from bot_modules.services.economy_quests_service import (
    claim_quest,
    create_quest,
    set_community_progress,
    set_quest_active,
)
from bot_modules.services.economy_rentals_service import rent_perk
from bot_modules.services.economy_service import (
    apply_credit,
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
    # Set bonuses zeroed — single-quest boards would otherwise pay the
    # clear-the-board bonus on every claim and skew exact balances.
    values: dict[str, object] = {
        "enabled": True,
        "xp_per_coin": 10.0,
        "quest_set_bonus_daily": 0,
        "quest_set_bonus_weekly": 0,
    }
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


def _rollup_count(db_path, guild_id=GUILD) -> int:
    with open_db(db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM econ_metrics_weekly WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()["n"]


def _rollup(db_path, iso_week, guild_id=GUILD):
    with open_db(db_path) as conn:
        return conn.execute(
            "SELECT * FROM econ_metrics_weekly WHERE guild_id = ? AND iso_week = ?",
            (guild_id, iso_week),
        ).fetchone()


def test_week_roll_computes_metrics_rollup_once(db):
    _enable(db)
    # A credit inside W28 so the rollup has real ledger data to summarise.
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO econ_ledger "
            "(guild_id, user_id, amount, kind, actor_id, meta, created_at) "
            "VALUES (?, ?, ?, ?, NULL, NULL, ?)",
            (GUILD, USER, 40, "login", _ts(D1)),  # D1 is in W28
        )
    bot = _Bot([_Guild(GUILD)])

    _roll(bot, db, _ts(DSUN))  # first sight → mark W28, no rollup
    assert _rollup_count(db) == 0

    _roll(bot, db, _ts(DMON))  # week change W28 → W29: roll up the CLOSED week
    assert _rollup_count(db) == 1
    row = _rollup(db, WEEK_28)
    assert row is not None
    assert row["minted"] == 40  # the seeded login credit

    # A second tick in the new week must not create another rollup (PK-idempotent).
    _rewind_marks(db, DSUN, WEEK_28)
    _roll(bot, db, _ts(DMON))
    assert _rollup_count(db) == 1


def test_same_week_day_roll_computes_no_metrics(db):
    _enable(db)
    bot = _Bot([_Guild(GUILD)])
    _roll(bot, db, _ts(D1))  # W28 first sight
    _roll(bot, db, _ts(D2))  # still W28 → day roll only, no week roll
    assert _rollup_count(db) == 0


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


# ── rental billing pass (run_guild_rentals) ────────────────────────────

# Anchor rental time on a plain float so week arithmetic is exact and tz-free.
RT0 = 2_000_000.0
PRICE_COLOR = 50  # EconSettings.price_role_color default
PRICE_ICON = 75  # EconSettings.price_role_icon default


class _PerkRecorder:
    """Async stand-ins for the perk_actions projector, patched onto the loop.

    ``gate`` is either a bool (all perks) or a {perk: bool} map; flipping it
    between ticks simulates a guild gaining/losing a role feature. Records every
    apply/revoke/gate call so tests can assert the exact post-commit wiring.
    """

    def __init__(self, gate: bool | dict[str, bool] = True) -> None:
        self.gate = gate
        self.gate_calls: list[tuple[int, str]] = []
        self.apply_calls: list[tuple[int, int]] = []
        self.revoke_calls: list[tuple[int, int]] = []

    async def feature_gate_ok(self, bot, guild_id, perk) -> bool:
        self.gate_calls.append((guild_id, perk))
        if isinstance(self.gate, dict):
            return self.gate.get(perk, True)
        return self.gate

    async def apply_role_perks(self, bot, db_path, guild_id, user_id) -> bool:
        self.apply_calls.append((guild_id, user_id))
        return True

    async def revoke_role_perks(self, bot, db_path, guild_id, user_id) -> None:
        self.revoke_calls.append((guild_id, user_id))


def _patch_perks(monkeypatch, gate: bool | dict[str, bool] = True) -> _PerkRecorder:
    rec = _PerkRecorder(gate)
    monkeypatch.setattr(economy_loop, "feature_gate_ok", rec.feature_gate_ok)
    monkeypatch.setattr(economy_loop, "apply_role_perks", rec.apply_role_perks)
    monkeypatch.setattr(economy_loop, "revoke_role_perks", rec.revoke_role_perks)
    return rec


def _fund(db_path, user_id, amount, guild_id=GUILD) -> None:
    with open_db(db_path) as conn:
        apply_credit(conn, guild_id, user_id, amount, "grant")


def _rent(db_path, user_id, perk, *, beneficiary_id=None, now=RT0, guild_id=GUILD) -> int:
    with open_db(db_path) as conn:
        settings = load_econ_settings(conn, guild_id)
        row = rent_perk(
            conn, settings, guild_id, user_id, perk,
            beneficiary_id=beneficiary_id, now=now,
        )
        return int(row["id"])


def _rental(db_path, rental_id):
    with open_db(db_path) as conn:
        return dict(
            conn.execute(
                "SELECT * FROM econ_rentals WHERE id = ?", (rental_id,)
            ).fetchone()
        )


def _set_rental(db_path, rental_id, **cols) -> None:
    assigns = ", ".join(f"{k} = ?" for k in cols)
    with open_db(db_path) as conn:
        conn.execute(
            f"UPDATE econ_rentals SET {assigns} WHERE id = ?",
            (*cols.values(), rental_id),
        )


async def _rental_tick(bot, db_path, now_ts, guild_id=GUILD) -> None:
    await run_guild_rentals(bot, db_path, guild_id, now_ts)


def _rental_bot():
    return _Bot([_Guild(GUILD)])


# ── charge / grace / retry ─────────────────────────────────────────────


async def test_due_charge_advances_silently_without_drift(db, monkeypatch):
    _enable(db)
    rec = _patch_perks(monkeypatch)
    notify = _NotifyRecorder()
    monkeypatch.setattr(economy_loop, "notify_member", notify)
    _fund(db, USER, 2 * PRICE_COLOR)  # first week upfront + one renewal
    rid = _rent(db, USER, "role_color")  # charges 50 upfront → balance 50
    assert _balance(db) == PRICE_COLOR
    due = _rental(db, rid)["next_bill_at"]  # == RT0 + WEEK

    # Tick a bit *after* the anniversary: the charge must advance from the
    # scheduled time, not from ``now`` (no drift).
    await _rental_tick(_rental_bot(), db, due + 100.0)

    row = _rental(db, rid)
    assert row["state"] == "active"
    assert row["next_bill_at"] == due + WEEK_SECONDS  # scheduled+week, not now+week
    assert _balance(db) == 0  # second 50 charged
    assert notify.calls == []  # silent renewal
    assert rec.apply_calls == [] and rec.revoke_calls == []


async def test_insufficient_enters_grace_dm_once_then_retry_silent(db, monkeypatch):
    _enable(db)
    rec = _patch_perks(monkeypatch)
    notify = _NotifyRecorder()
    monkeypatch.setattr(economy_loop, "notify_member", notify)
    _fund(db, USER, PRICE_COLOR)  # exactly one week — renewal will fail
    rid = _rent(db, USER, "role_color")
    due = _rental(db, rid)["next_bill_at"]

    await _rental_tick(_rental_bot(), db, due)  # debit fails → enter grace
    row = _rental(db, rid)
    assert row["state"] == "grace"
    assert row["grace_since"] == due
    assert len(notify.calls) == 1
    assert "grace" in (notify.calls[0][2] or "")
    assert rec.revoke_calls == []

    # Second tick still inside the 36h window → retry, silent (no repeat DM).
    await _rental_tick(_rental_bot(), db, due + 3600.0)
    assert _rental(db, rid)["state"] == "grace"
    assert len(notify.calls) == 1  # still just the one
    assert rec.revoke_calls == []


async def test_funded_retry_recovers_silently(db, monkeypatch):
    _enable(db)
    rec = _patch_perks(monkeypatch)
    notify = _NotifyRecorder()
    monkeypatch.setattr(economy_loop, "notify_member", notify)
    _fund(db, USER, PRICE_COLOR)
    rid = _rent(db, USER, "role_color")
    due = _rental(db, rid)["next_bill_at"]
    await _rental_tick(_rental_bot(), db, due)  # → grace
    assert _rental(db, rid)["state"] == "grace"

    # Top the wallet up, then retry inside the window → silent recovery.
    _fund(db, USER, PRICE_COLOR)
    notify.calls.clear()
    await _rental_tick(_rental_bot(), db, due + 7200.0)

    row = _rental(db, rid)
    assert row["state"] == "active"
    assert row["grace_since"] is None
    assert row["next_bill_at"] == due + WEEK_SECONDS  # advanced from scheduled
    assert _balance(db) == 0
    # Grace never revoked the perk, so recovery re-projects nothing and is silent.
    assert notify.calls == []
    assert rec.apply_calls == [] and rec.revoke_calls == []


# ── revoke / cancel ────────────────────────────────────────────────────


async def test_grace_elapsed_revokes_and_dms_owner(db, monkeypatch):
    _enable(db)
    rec = _patch_perks(monkeypatch)
    notify = _NotifyRecorder()
    monkeypatch.setattr(economy_loop, "notify_member", notify)
    _fund(db, USER, PRICE_COLOR)
    rid = _rent(db, USER, "role_color")
    due = _rental(db, rid)["next_bill_at"]
    await _rental_tick(_rental_bot(), db, due)  # → grace
    notify.calls.clear()

    # Past the 36h grace window → revoke.
    await _rental_tick(_rental_bot(), db, due + GRACE_SECONDS + 1.0)

    assert _rental(db, rid)["state"] == "lapsed"
    assert rec.revoke_calls == [(GUILD, USER)]  # beneficiary == owner here
    assert len(notify.calls) == 1
    assert (GUILD, USER) == (notify.calls[0][0], notify.calls[0][1])
    assert "lapsed" in (notify.calls[0][2] or "")


async def test_grace_elapsed_gift_dms_owner_and_beneficiary(db, monkeypatch):
    _enable(db)
    rec = _patch_perks(monkeypatch)
    notify = _NotifyRecorder()
    monkeypatch.setattr(economy_loop, "notify_member", notify)
    _fund(db, USER, PRICE_COLOR)  # payer funds one week only
    rid = _rent(db, USER, "gift_color", beneficiary_id=OTHER)
    due = _rental(db, rid)["next_bill_at"]
    await _rental_tick(_rental_bot(), db, due)  # → grace
    notify.calls.clear()

    await _rental_tick(_rental_bot(), db, due + GRACE_SECONDS + 1.0)  # → revoke

    assert _rental(db, rid)["state"] == "lapsed"
    assert rec.revoke_calls == [(GUILD, OTHER)]  # beneficiary, not payer
    dmed = {(gid, uid) for gid, uid, _ in notify.calls}
    assert dmed == {(GUILD, USER), (GUILD, OTHER)}  # owner + courtesy to friend


async def test_cancel_at_period_end_revokes_with_no_dm(db, monkeypatch):
    _enable(db)
    rec = _patch_perks(monkeypatch)
    notify = _NotifyRecorder()
    monkeypatch.setattr(economy_loop, "notify_member", notify)
    _fund(db, USER, 2 * PRICE_COLOR)
    rid = _rent(db, USER, "role_color")
    due = _rental(db, rid)["next_bill_at"]
    _set_rental(db, rid, cancel_at_period_end=1)

    await _rental_tick(_rental_bot(), db, due)  # anniversary of a cancelled rental

    assert _rental(db, rid)["state"] == "cancelled"
    assert _balance(db) == PRICE_COLOR  # NOT charged the second week
    assert rec.revoke_calls == [(GUILD, USER)]  # beneficiary revoked …
    assert notify.calls == []  # … but member-initiated: silent


# ── feature-gate suspension sweep ──────────────────────────────────────


async def test_feature_loss_suspends_once_and_freezes_billing(db, monkeypatch):
    _enable(db)
    rec = _patch_perks(monkeypatch, gate=False)  # server lacks the icon feature
    notify = _NotifyRecorder()
    monkeypatch.setattr(economy_loop, "notify_member", notify)
    _fund(db, USER, PRICE_ICON)  # one week only → a charge would fail if attempted
    rid = _rent(db, USER, "role_icon")
    due = _rental(db, rid)["next_bill_at"]

    await _rental_tick(_rental_bot(), db, due)  # due, but feature gone → suspend
    row = _rental(db, rid)
    assert row["suspended"] == 1
    assert row["state"] == "active"  # suspended, NOT billed → never entered grace
    assert row["grace_since"] is None
    assert _balance(db) == 0  # nothing charged while suspended
    assert len(notify.calls) == 1
    assert "paused" in (notify.calls[0][2] or "")

    # Feature still gone next tick: no transition → no second DM, still frozen.
    await _rental_tick(_rental_bot(), db, due + 3600.0)
    assert _rental(db, rid)["suspended"] == 1
    assert len(notify.calls) == 1  # not re-notified
    assert rec.revoke_calls == []  # suspended rental is skipped by billing


async def test_feature_return_unsuspends_and_reprojects(db, monkeypatch):
    _enable(db)
    gate = {"role_icon": False}
    rec = _patch_perks(monkeypatch, gate=gate)
    notify = _NotifyRecorder()
    monkeypatch.setattr(economy_loop, "notify_member", notify)
    _fund(db, USER, PRICE_ICON)
    rid = _rent(db, USER, "role_icon")  # anniversary a week out

    # Feature lost well before the anniversary → suspend, no billing yet.
    await _rental_tick(_rental_bot(), db, RT0 + 1000.0)
    assert _rental(db, rid)["suspended"] == 1
    notify.calls.clear()

    # Feature returns, still before the (frozen-span-pushed) anniversary.
    gate["role_icon"] = True
    await _rental_tick(_rental_bot(), db, RT0 + 2000.0)

    row = _rental(db, rid)
    assert row["suspended"] == 0
    assert rec.apply_calls == [(GUILD, USER)]  # re-projected on resume
    assert rec.revoke_calls == []  # resume never revokes
    assert len(notify.calls) == 1
    assert "resumed" in (notify.calls[0][2] or "")


# ── guard rails: disabled guild + idempotence ──────────────────────────


async def test_disabled_guild_rental_pass_untouched(db, monkeypatch):
    _enable(db)  # enable to create the rental …
    rec = _patch_perks(monkeypatch)
    notify = _NotifyRecorder()
    monkeypatch.setattr(economy_loop, "notify_member", notify)
    _fund(db, USER, 2 * PRICE_COLOR)
    rid = _rent(db, USER, "role_color")
    due = _rental(db, rid)["next_bill_at"]
    _enable(db, enabled=False)  # … then turn economy off

    await _rental_tick(_rental_bot(), db, due + WEEK_SECONDS)  # long overdue

    row = _rental(db, rid)
    assert row["state"] == "active"
    assert row["next_bill_at"] == due  # not advanced
    assert _balance(db) == PRICE_COLOR  # not charged
    assert notify.calls == []
    assert rec.revoke_calls == [] and rec.gate_calls == []


async def test_double_tick_idempotent_over_rental_pass(db, monkeypatch):
    _enable(db)
    _patch_perks(monkeypatch)
    notify = _NotifyRecorder()
    monkeypatch.setattr(economy_loop, "notify_member", notify)
    _fund(db, USER, 2 * PRICE_COLOR)
    rid = _rent(db, USER, "role_color")
    due = _rental(db, rid)["next_bill_at"]

    await _rental_tick(_rental_bot(), db, due)  # charge
    after_first = _rental(db, rid)
    assert after_first["next_bill_at"] == due + WEEK_SECONDS
    assert _balance(db) == 0

    # Immediate second tick at the same clock: nothing due → no change.
    await _rental_tick(_rental_bot(), db, due)
    after_second = _rental(db, rid)
    assert after_second["next_bill_at"] == after_first["next_bill_at"]
    assert _balance(db) == 0
    assert _ledger_kind_count(db, "rental") == 2  # upfront + one renewal, no more
    assert notify.calls == []


async def test_run_tick_drives_the_rental_pass(db, monkeypatch):
    # Integration: exercise the real entry point (run_tick's per-guild loop),
    # not run_guild_rentals directly, so the wiring itself is covered.
    _enable(db)
    _patch_perks(monkeypatch)
    notify = _NotifyRecorder()
    monkeypatch.setattr(economy_loop, "notify_member", notify)
    _fund(db, USER, 2 * PRICE_COLOR)
    rid = _rent(db, USER, "role_color")
    due = _rental(db, rid)["next_bill_at"]

    await _tick(_rental_bot(), db, due)  # the actual hourly tick (run_tick)

    row = _rental(db, rid)
    assert row["state"] == "active"
    assert row["next_bill_at"] == due + WEEK_SECONDS  # renewal charged + advanced
    assert _balance(db) == 0


# ── community weeklies: gap-week alternation + beats (stage 3) ────────


def _mk_community_kind(db_path, *, title, kind, reward=30, guild_id=GUILD) -> int:
    with open_db(db_path) as conn:
        return create_quest(
            conn, guild_id,
            title=title, description="d", qtype="community", reward=reward,
            signoff=0, criteria="", starts_at=None, ends_at=None,
            rotate_tag="", community_target=None, created_by=None,
            trigger_kind=kind,
        )


def _fire(db_path, kind, user_id, occ, day, guild_id=GUILD) -> None:
    from bot_modules.services.economy_quests_service import fire_trigger_quests

    with open_db(db_path) as conn:
        settings = load_econ_settings(conn, guild_id)
        fire_trigger_quests(
            conn, settings, guild_id, kind, user_id,
            local_day=day, occurrence=occ, booster=False,
        )


def _roll_beats(bot, db_path, now_ts, guild_id=GUILD):
    with open_db(db_path) as conn:
        return list(run_guild_day_roll(bot, conn, guild_id, now_ts).beats)


def _community_state(db_path, qid):
    with open_db(db_path) as conn:
        q = conn.execute(
            "SELECT active, community_target, last_run_week FROM econ_quests "
            "WHERE id = ?", (qid,),
        ).fetchone()
        p = conn.execute(
            "SELECT current, settled_at FROM econ_community_progress "
            "WHERE quest_id = ?", (qid,),
        ).fetchone()
        return q, p


def test_community_weekly_gap_week_lifecycle(db):
    _enable(db)
    bot = _Bot([_Guild(GUILD, {USER: _Member()})])
    qa = _mk_community_kind(db, title="Msgs", kind="message_sent")
    qb = _mk_community_kind(db, title="Replies", kind="reply_sent")
    _add_activity(db, USER)

    _roll(bot, db, _ts(D1))  # first sight: marks only

    # W28 → W29 roll: first-ever community week activates the library's next
    # quest (id order when never run) with an auto-sized (floor) target.
    beats = _roll_beats(bot, db, _ts("2026-07-13"))
    assert any("kicked off" in b.text for b in beats)
    q, p = _community_state(db, qa)
    assert int(q["active"]) == 1 and int(q["community_target"]) == 10
    assert q["last_run_week"] == "2026-W29"

    # Members act during the run week → counter + contrib move.
    for i in range(7):
        _fire(db, "message_sent", USER, f"m{i}", "2026-07-15")

    # W29 → W30 roll: the run settles (7/10 = 70% → tiers 1+2), resolution
    # beat, quest deactivates, and nothing new activates (the win breathes).
    beats = _roll_beats(bot, db, _ts("2026-07-20"))
    assert any("resolved" in b.text for b in beats)
    assert not any("kicked off" in b.text for b in beats)
    q, p = _community_state(db, qa)
    assert int(q["active"]) == 0 and p["settled_at"] is not None
    # 2 tiers × 30 flat + 15 top-contributor bonus.
    assert _balance(db, USER) == 75

    # W30 → W31 roll: gap week over → the OTHER quest activates.
    beats = _roll_beats(bot, db, _ts("2026-07-27"))
    assert any("kicked off" in b.text for b in beats)
    q, _ = _community_state(db, qb)
    assert int(q["active"]) == 1 and q["last_run_week"] == "2026-W31"


def test_community_hourly_beats_fire_once(db):
    from bot_modules.services.economy_loop import community_hourly_beats
    from bot_modules.services.economy_quests_service import (
        activate_community_weekly,
    )

    _enable(db)
    qid = _mk_community_kind(db, title="Msgs", kind="message_sent")
    with open_db(db) as conn:
        activate_community_weekly(conn, GUILD, qid, target=10, week="2026-W29")
    for i in range(5):  # 50% → tier 1
        _fire(db, "message_sent", USER, f"x{i}", "2026-07-15")

    now = _ts("2026-07-15")  # Wed of W29 — far from week end
    with open_db(db) as conn:
        beats = community_hourly_beats(conn, GUILD, now)
    assert len(beats) == 1 and "Tier 1 crossed" in beats[0].text
    with open_db(db) as conn:
        assert community_hourly_beats(conn, GUILD, now) == []

    # Sunday inside the final day → the 24h nudge, exactly once.
    sunday = _ts("2026-07-19", hour=12)
    with open_db(db) as conn:
        beats = community_hourly_beats(conn, GUILD, sunday)
    assert len(beats) == 1 and "Final 24h" in beats[0].text
    with open_db(db) as conn:
        assert community_hourly_beats(conn, GUILD, sunday) == []
