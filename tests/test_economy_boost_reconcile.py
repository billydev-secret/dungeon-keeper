"""Tests for services/economy_boost_reconcile.py — the startup boost backfill.

The live ``on_member_update`` listener only credits a boost when it *sees* the
premium_since transition, so members who were already boosting when the quest
shipped never get paid. ``reconcile_guild_boosters`` replays the boost trigger
for current boosters, keyed on their premium_since timestamp — the same
occurrence key the listener uses — so it credits the missed ones exactly once
and never double-pays anyone the listener already handled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_boost_reconcile import reconcile_guild_boosters
from bot_modules.services.economy_quests_service import (
    create_quest,
    fire_trigger_inline,
    set_quest_active,
)
from bot_modules.services.economy_service import get_balance, save_econ_settings
from migrations import apply_migrations_sync

GUILD = 500
BOOST_REWARD = 20
# ceil(20 * 1.5) — every current booster carries the 1.5x booster flag.
BOOSTED_PAYOUT = 30


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    apply_migrations_sync(path)
    return path


@dataclass
class _Member:
    id: int
    premium_since: datetime | None


@dataclass
class _Guild:
    id: int
    premium_subscribers: list[_Member] = field(default_factory=list)


def _member(uid: int, boosted_ts: int | None) -> _Member:
    since = (
        datetime.fromtimestamp(boosted_ts, tz=timezone.utc)
        if boosted_ts is not None
        else None
    )
    return _Member(id=uid, premium_since=since)


def _guild(boosters, gid: int = GUILD) -> _Guild:
    return _Guild(id=gid, premium_subscribers=list(boosters))


def _make_boost_quest(conn) -> int:
    qid = create_quest(
        conn,
        GUILD,
        title="Boosted",
        description="desc",
        qtype="event",
        reward=BOOST_REWARD,
        signoff=0,
        criteria="boost the server",
        starts_at=None,
        ends_at=None,
        rotate_tag="",
        community_target=None,
        created_by=9001,
        trigger_kind="boost",
    )
    set_quest_active(conn, GUILD, qid, True)
    return qid


def test_credits_existing_boosters_once_each(db):
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
        _make_boost_quest(conn)
        guild = _guild([_member(101, 1_784_000_000), _member(102, 1_784_100_000)])

        filed = reconcile_guild_boosters(conn, guild)

        assert filed == 2
        assert get_balance(conn, GUILD, 101) == BOOSTED_PAYOUT
        assert get_balance(conn, GUILD, 102) == BOOSTED_PAYOUT


def test_idempotent_across_runs(db):
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
        _make_boost_quest(conn)
        guild = _guild([_member(101, 1_784_000_000)])

        assert reconcile_guild_boosters(conn, guild) == 1
        # Second pass collides on the boost:<ts> claim key — nothing new.
        assert reconcile_guild_boosters(conn, guild) == 0
        assert get_balance(conn, GUILD, 101) == BOOSTED_PAYOUT


def test_does_not_double_pay_a_member_the_listener_already_credited(db):
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
        _make_boost_quest(conn)
        ts = 1_784_000_000
        # Simulate the live listener already paying member 101 at this boost.
        fired = fire_trigger_inline(
            conn, GUILD, "boost", 101, occurrence=str(ts), booster=True
        )
        assert len(fired) == 1

        guild = _guild([_member(101, ts), _member(102, 1_784_100_000)])
        # Only the un-credited booster (102) is filed.
        assert reconcile_guild_boosters(conn, guild) == 1
        assert get_balance(conn, GUILD, 101) == BOOSTED_PAYOUT
        assert get_balance(conn, GUILD, 102) == BOOSTED_PAYOUT


def test_ignores_members_without_premium_since(db):
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
        _make_boost_quest(conn)
        # A defensive None (real premium_subscribers never includes these).
        guild = _guild([_member(101, None)])

        assert reconcile_guild_boosters(conn, guild) == 0
        assert get_balance(conn, GUILD, 101) == 0


def test_no_boosters_files_nothing(db):
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
        _make_boost_quest(conn)

        assert reconcile_guild_boosters(conn, _guild([])) == 0


def test_economy_disabled_files_nothing(db):
    with open_db(db) as conn:
        # Economy left disabled — fire_trigger_inline no-ops.
        _make_boost_quest(conn)
        guild = _guild([_member(101, 1_784_000_000)])

        assert reconcile_guild_boosters(conn, guild) == 0
        assert get_balance(conn, GUILD, 101) == 0
