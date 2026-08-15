"""Selection rule for the one-off Pen Pals pool seed.

The expiry requeue can only refill a pool that still has sessions running
through it; The Golden Meadow's had already drained to one member, so it needed
a seed. These cover who that seed picks — the part that decides whether a real
member is dropped into a chat they didn't ask for.
"""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path

from bot_modules.cogs import pen_pals_cog as pp
from bot_modules.core.db_utils import open_db

_SPEC = importlib.util.spec_from_file_location(
    "backfill_pen_pals_pool",
    Path(__file__).resolve().parent.parent / "scripts" / "backfill_pen_pals_pool.py",
)
assert _SPEC and _SPEC.loader
backfill = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backfill)

GUILD = 9001


def _closed_session(conn, sid, u1, u2, *, channel_id, closed_at, guild_id=GUILD):
    pp._create_session(conn, sid, guild_id, channel_id, u1, u2, closed_at - 86400)
    conn.execute(
        "UPDATE pen_pals_sessions SET state='closed', closed_at=?, close_reason='expired' "
        "WHERE session_id=?",
        (closed_at, sid),
    )


def _spoke(conn, uid, channel_id, guild_id=GUILD):
    conn.execute(
        "INSERT INTO messages (guild_id, channel_id, author_id, ts) VALUES (?, ?, ?, ?)",
        (guild_id, channel_id, uid, int(time.time())),
    )


def test_seeds_only_members_who_used_their_last_chat(sync_db_path):
    with open_db(sync_db_path) as conn:
        _closed_session(conn, "s1", 1, 2, channel_id=100, closed_at=1000.0)
        _spoke(conn, 1, 100)  # 2 never posted

        assert [u for u, _ in backfill.candidates(conn, GUILD)] == [1]


def test_judges_the_most_recent_chat_not_an_older_one(sync_db_path):
    """Someone who talked once and then ghosted is judged on the ghosting."""
    with open_db(sync_db_path) as conn:
        _closed_session(conn, "old", 1, 2, channel_id=100, closed_at=1000.0)
        _spoke(conn, 1, 100)
        _closed_session(conn, "new", 1, 3, channel_id=200, closed_at=2000.0)
        _spoke(conn, 3, 200)  # 1 said nothing this time

        assert [u for u, _ in backfill.candidates(conn, GUILD)] == [3]


def test_skips_members_already_pooled_or_mid_chat(sync_db_path):
    """Which is also what makes a second run a no-op."""
    with open_db(sync_db_path) as conn:
        _closed_session(conn, "s1", 1, 2, channel_id=100, closed_at=1000.0)
        _spoke(conn, 1, 100)
        _spoke(conn, 2, 100)
        pp._add_to_pool(conn, GUILD, 1)
        pp._create_session(conn, "live", GUILD, 300, 2, 4, time.time())

        assert backfill.candidates(conn, GUILD) == []


def test_orders_oldest_waiter_first(sync_db_path):
    """joined_at is backdated to the last close so the pool's FIFO order
    reflects who has actually been waiting longest, not script run order."""
    with open_db(sync_db_path) as conn:
        _closed_session(conn, "a", 1, 2, channel_id=100, closed_at=5000.0)
        _closed_session(conn, "b", 3, 4, channel_id=200, closed_at=1000.0)
        for uid, ch in ((1, 100), (2, 100), (3, 200), (4, 200)):
            _spoke(conn, uid, ch)

        picks = backfill.candidates(conn, GUILD)

        assert [u for u, _ in picks] == [3, 4, 1, 2]
        assert picks[0][1] == 1000.0


def test_is_scoped_to_one_guild(sync_db_path):
    with open_db(sync_db_path) as conn:
        _closed_session(conn, "s1", 1, 2, channel_id=100, closed_at=1000.0, guild_id=GUILD + 1)
        _spoke(conn, 1, 100, guild_id=GUILD + 1)

        assert backfill.candidates(conn, GUILD) == []


def test_apply_writes_pool_rows_and_audit_events(sync_db_path):
    with open_db(sync_db_path) as conn:
        _closed_session(conn, "s1", 1, 2, channel_id=100, closed_at=1000.0)
        _spoke(conn, 1, 100)
        _spoke(conn, 2, 100)

        backfill.apply(conn, GUILD, backfill.candidates(conn, GUILD))

        assert [r["user_id"] for r in pp._get_pool(conn, GUILD)] == [1, 2]
        assert [
            (r["user_id"], r["action"], r["reason"])
            for r in pp._recent_pool_events(conn, GUILD)
        ] == [(2, "join", "backfill"), (1, "join", "backfill")]


def test_apply_is_idempotent(sync_db_path):
    """Re-running must not double-pool anyone or spray duplicate audit rows."""
    with open_db(sync_db_path) as conn:
        _closed_session(conn, "s1", 1, 2, channel_id=100, closed_at=1000.0)
        _spoke(conn, 1, 100)
        _spoke(conn, 2, 100)

        backfill.apply(conn, GUILD, backfill.candidates(conn, GUILD))
        backfill.apply(conn, GUILD, backfill.candidates(conn, GUILD))  # second run

        assert len(pp._get_pool(conn, GUILD)) == 2
        assert len(pp._recent_pool_events(conn, GUILD)) == 2


def test_seeded_members_still_wait_out_the_cooldown(sync_db_path):
    """The seed does not bypass the re-match cooldown — a member whose chat
    ended inside the window sits in the pool ineligible, exactly as they would
    after a normal expiry."""
    now = time.time()
    with open_db(sync_db_path) as conn:
        _closed_session(conn, "s1", 1, 2, channel_id=100, closed_at=now - 3600)
        _spoke(conn, 1, 100)
        _spoke(conn, 2, 100)
        backfill.apply(conn, GUILD, backfill.candidates(conn, GUILD))

        assert pp._eligible_pool(conn, GUILD, now, 172800) == []
        assert sorted(pp._eligible_pool(conn, GUILD, now + 172800, 172800)) == [1, 2]


def test_seed_excludes_members_who_are_no_longer_in_the_guild(sync_db_path):
    """History alone can't tell you who left.

    A departed member seeded into the pool is a row nothing can ever clear —
    `_do_pair` refuses on the missing member, but `_do_round` has already spent
    their would-be partner, so a real member goes unmatched every round.
    """
    with open_db(sync_db_path) as conn:
        _closed_session(conn, "s1", 1, 2, channel_id=100, closed_at=1000.0)
        _spoke(conn, 1, 100)
        _spoke(conn, 2, 100)

        assert [u for u, _ in backfill.candidates(conn, GUILD, present={1})] == [1]


def test_seed_with_no_live_filter_still_pools_everyone(sync_db_path):
    """present=None is the offline path, and `--apply` refuses to use it."""
    with open_db(sync_db_path) as conn:
        _closed_session(conn, "s1", 1, 2, channel_id=100, closed_at=1000.0)
        _spoke(conn, 1, 100)
        _spoke(conn, 2, 100)

        assert len(backfill.candidates(conn, GUILD, present=None)) == 2


def test_seed_excludes_everyone_when_nobody_holds_the_opt_in_role(sync_db_path):
    """`present` arrives already role-filtered, so an empty set seeds nobody
    rather than falling back to seeding all of them."""
    with open_db(sync_db_path) as conn:
        _closed_session(conn, "s1", 1, 2, channel_id=100, closed_at=1000.0)
        _spoke(conn, 1, 100)
        _spoke(conn, 2, 100)

        assert backfill.candidates(conn, GUILD, present=set()) == []
