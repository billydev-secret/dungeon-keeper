"""Deleting raw xp_events once the rollup covers them (retention Stage 3).

This is the only code in the feature that destroys anything, and there is no
undo — the raw events are the only copy of when a member earned what. So every
test here is a guard test: the interesting cases are the ones where the prune
must **refuse**, not the one where it works.

The ordering property is the one that would be silent in production: a prune
that runs before the rollup has covered a day deletes XP that no reader can
ever find again, and nothing errors.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.core.xp_system import get_xp_leaderboard
from bot_modules.services import xp_rollup_service as rollup

GUILD = 7201
OTHER_GUILD = 7202
USER_A = 11
USER_B = 22
CHAN = 555

NOW = datetime.now(timezone.utc).timestamp()


def _days_ago(n: int, hour: int = 12) -> float:
    base = datetime.fromtimestamp(NOW, timezone.utc).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    return (base - timedelta(days=n)).timestamp()


def _event(conn, *, days_ago, user=USER_A, guild=GUILD, source="text", amount=1.0):
    conn.execute(
        "INSERT INTO xp_events (guild_id, user_id, source, amount, created_at, channel_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (guild, user, source, amount, _days_ago(days_ago), CHAN),
    )


def _enable(conn, guild=GUILD, value="1"):
    conn.execute(
        "INSERT OR REPLACE INTO config (key, value, guild_id) VALUES (?, ?, ?)",
        (rollup.RETENTION_CONFIG_KEY, value, guild),
    )


def _roll(conn):
    rollup.rollup_pending_days(conn, now=NOW)
    rollup.recompute_watermark(conn)


def _raw_count(conn, guild=GUILD):
    return conn.execute(
        "SELECT COUNT(*) FROM xp_events WHERE guild_id = ?", (guild,)
    ).fetchone()[0]


def _seed(conn):
    """Five old events that may be pruned, two recent ones that may not."""
    _event(conn, days_ago=300, amount=100.0)
    _event(conn, days_ago=250, user=USER_B, amount=60.0)
    _event(conn, days_ago=200, amount=10.0)
    _event(conn, days_ago=150, user=USER_B, amount=5.0, source="voice")
    _event(conn, days_ago=95, amount=7.0)
    _event(conn, days_ago=30, amount=3.0)
    _event(conn, days_ago=2, user=USER_B, amount=1.0)


# ── the guards, which are the point ─────────────────────────────────────


def test_a_guild_that_did_not_opt_in_is_never_pruned(sync_db_path):
    """Off by default. This is what makes shipping Stage 3 safe."""
    with open_db(sync_db_path) as conn:
        _seed(conn)
        _roll(conn)
        with pytest.raises(rollup.PruneRefused, match="not enabled"):
            rollup.prune_raw_events(conn, GUILD, now=NOW)
        assert _raw_count(conn) == 7


@pytest.mark.parametrize("value", ["0", "", "false", "no", "maybe"])
def test_only_an_explicit_one_counts_as_opted_in(sync_db_path, value):
    """A stored value the reader doesn't recognise must read as off, not on."""
    with open_db(sync_db_path) as conn:
        _seed(conn)
        _roll(conn)
        _enable(conn, value=value)
        assert rollup.retention_enabled(conn, GUILD) is False
        with pytest.raises(rollup.PruneRefused):
            rollup.prune_raw_events(conn, GUILD, now=NOW)


def test_nothing_is_pruned_before_the_rollup_has_run(sync_db_path):
    """The failure this whole plan exists to prevent, and it is silent."""
    with open_db(sync_db_path) as conn:
        _seed(conn)
        _enable(conn)
        with pytest.raises(rollup.PruneRefused):
            rollup.prune_raw_events(conn, GUILD, now=NOW)
        assert _raw_count(conn) == 7


def test_a_hole_in_the_rollup_stops_the_prune(sync_db_path):
    """A day rolled and then lost must block deletion, not be stepped over."""
    with open_db(sync_db_path) as conn:
        _seed(conn)
        _roll(conn)
        _enable(conn)
        hole = rollup.utc_day(_days_ago(200))
        conn.execute("DELETE FROM xp_daily WHERE day = ?", (hole,))
        rollup.recompute_watermark(conn)

        with pytest.raises(rollup.PruneRefused):
            rollup.prune_raw_events(conn, GUILD, now=NOW)
        assert _raw_count(conn) == 7


def test_an_incomplete_backfill_stops_the_prune(sync_db_path):
    """Mid-backfill the oldest days have no rollup yet — refuse, don't guess."""
    with open_db(sync_db_path) as conn:
        _seed(conn)
        _enable(conn)
        # Roll only the newest pending day: the rest are still unrolled.
        rollup.rollup_pending_days(conn, now=NOW, limit=1)
        rollup.recompute_watermark(conn)

        with pytest.raises(rollup.PruneRefused):
            rollup.prune_raw_events(conn, GUILD, now=NOW)
        assert _raw_count(conn) == 7


def test_another_guilds_opt_in_does_not_prune_this_one(sync_db_path):
    with open_db(sync_db_path) as conn:
        _seed(conn)
        _event(conn, days_ago=300, guild=OTHER_GUILD, amount=9.0)
        _roll(conn)
        _enable(conn, guild=OTHER_GUILD)

        rollup.prune_raw_events(conn, OTHER_GUILD, now=NOW)
        assert _raw_count(conn, OTHER_GUILD) == 0
        assert _raw_count(conn, GUILD) == 7


# ── what it does when it is allowed to ──────────────────────────────────


def test_it_deletes_only_below_the_boundary(sync_db_path):
    with open_db(sync_db_path) as conn:
        _seed(conn)
        _roll(conn)
        _enable(conn)
        deleted = rollup.prune_raw_events(conn, GUILD, now=NOW)

        assert deleted == 5
        remaining = [
            r[0] for r in conn.execute(
                "SELECT created_at FROM xp_events WHERE guild_id = ?", (GUILD,)
            )
        ]
        boundary = rollup.read_boundary(conn, now=NOW)
        assert boundary is not None
        assert all(ts >= boundary[1] for ts in remaining)
        assert len(remaining) == 2


def test_the_leaderboard_does_not_move(sync_db_path):
    """The property Stage 2 bought: pruning changes storage, not answers."""
    with open_db(sync_db_path) as conn:
        _seed(conn)
        _roll(conn)
        before = [(e.user_id, e.xp) for e in get_xp_leaderboard(conn, GUILD, "text")]
        _enable(conn)
        rollup.prune_raw_events(conn, GUILD, now=NOW)
        after = [(e.user_id, e.xp) for e in get_xp_leaderboard(conn, GUILD, "text")]

    assert after == before
    assert dict(before)[USER_A] == 120.0  # 100 + 10 + 7 + 3, all still counted


def test_the_chunk_limit_is_honoured_and_the_rest_follows(sync_db_path):
    """A first pass over half a million rows must not be one write."""
    with open_db(sync_db_path) as conn:
        _seed(conn)
        _roll(conn)
        _enable(conn)

        assert rollup.prune_raw_events(conn, GUILD, now=NOW, limit=2) == 2
        assert _raw_count(conn) == 5
        assert rollup.prune_raw_events(conn, GUILD, now=NOW, limit=2) == 2
        assert rollup.prune_raw_events(conn, GUILD, now=NOW, limit=2) == 1
        assert rollup.prune_raw_events(conn, GUILD, now=NOW, limit=2) == 0
        assert _raw_count(conn) == 2


def test_it_deletes_oldest_first(sync_db_path):
    """So a half-finished prune leaves a contiguous tail, not swiss cheese."""
    with open_db(sync_db_path) as conn:
        _seed(conn)
        _roll(conn)
        _enable(conn)
        rollup.prune_raw_events(conn, GUILD, now=NOW, limit=2)

        oldest = conn.execute(
            "SELECT MIN(created_at) FROM xp_events WHERE guild_id = ?", (GUILD,)
        ).fetchone()[0]
        assert oldest == _days_ago(200)


def test_a_second_pass_over_a_pruned_guild_is_a_no_op(sync_db_path):
    with open_db(sync_db_path) as conn:
        _seed(conn)
        _roll(conn)
        _enable(conn)
        rollup.prune_raw_events(conn, GUILD, now=NOW)
        assert rollup.prune_raw_events(conn, GUILD, now=NOW) == 0


def test_a_guild_with_only_recent_activity_prunes_nothing(sync_db_path):
    with open_db(sync_db_path) as conn:
        _event(conn, days_ago=10, amount=1.0)
        _event(conn, days_ago=3, amount=1.0)
        _roll(conn)
        _enable(conn)
        assert rollup.prune_raw_events(conn, GUILD, now=NOW) == 0
        assert _raw_count(conn) == 2


# ── the dry run the dashboard shows ─────────────────────────────────────


def test_the_preview_count_matches_what_a_prune_deletes(sync_db_path):
    with open_db(sync_db_path) as conn:
        _seed(conn)
        _roll(conn)
        preview = rollup.prunable_row_count(conn, GUILD, now=NOW)
        _enable(conn)
        assert preview == rollup.prune_raw_events(conn, GUILD, now=NOW)


def test_the_preview_is_zero_while_a_prune_would_refuse(sync_db_path):
    """The panel must never advertise rows the guards would not release."""
    with open_db(sync_db_path) as conn:
        _seed(conn)
        assert rollup.prunable_row_count(conn, GUILD, now=NOW) == 0
