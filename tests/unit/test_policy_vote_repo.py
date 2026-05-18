"""Tier 1: policy ticket repo helpers — status-guarded resolve + expiry query."""
from __future__ import annotations

import time
from pathlib import Path

from bot_modules.core.db_utils import open_db
from bot_modules.services.moderation import (
    create_policy_ticket,
    find_expired_policy_votes,
    get_policy_ticket,
    resolve_policy_vote,
    start_policy_vote,
)

GUILD = 9001
CREATOR = 1001


def _seed_voting_policy(db_path: Path, *, started_offset: float = -1.0) -> int:
    """Create a policy ticket already in 'voting' state, with vote_started_at offset from now."""
    with open_db(db_path) as conn:
        pid = create_policy_ticket(
            conn,
            guild_id=GUILD,
            creator_id=CREATOR,
            channel_id=555,
            title="test policy",
            description="test desc",
        )
        start_policy_vote(conn, pid, vote_text="test vote text")
        # Override vote_started_at so we can simulate elapsed time without sleeping.
        conn.execute(
            "UPDATE policy_tickets SET vote_started_at = ? WHERE id = ?",
            (time.time() + started_offset, pid),
        )
    return pid


def test_resolve_policy_vote_returns_true_when_voting(sync_db_path: Path):
    pid = _seed_voting_policy(sync_db_path)
    with open_db(sync_db_path) as conn:
        assert resolve_policy_vote(conn, pid, status="passed") is True
        row = get_policy_ticket(conn, pid)
    assert row is not None
    assert row["status"] == "passed"
    assert row["vote_ended_at"] is not None


def test_resolve_policy_vote_second_call_returns_false(sync_db_path: Path):
    """Race protection: the loser of a concurrent finalize gets False."""
    pid = _seed_voting_policy(sync_db_path)
    with open_db(sync_db_path) as conn:
        assert resolve_policy_vote(conn, pid, status="passed") is True
        # Second call sees status != 'voting' and bails.
        assert resolve_policy_vote(conn, pid, status="failed") is False
        row = get_policy_ticket(conn, pid)
    assert row is not None
    assert row["status"] == "passed"  # unchanged by the losing caller


def test_resolve_policy_vote_skips_open_policy(sync_db_path: Path):
    """Open (pre-vote) policies are not yet 'voting' and must not be resolved."""
    with open_db(sync_db_path) as conn:
        pid = create_policy_ticket(
            conn,
            guild_id=GUILD,
            creator_id=CREATOR,
            channel_id=555,
            title="t",
            description="d",
        )
        assert resolve_policy_vote(conn, pid, status="failed") is False
        row = get_policy_ticket(conn, pid)
    assert row is not None
    assert row["status"] == "open"


def test_find_expired_policy_votes_empty_when_none_voting(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        create_policy_ticket(
            conn,
            guild_id=GUILD,
            creator_id=CREATOR,
            channel_id=555,
            title="t",
            description="d",
        )
        expired = find_expired_policy_votes(conn, GUILD, timeout_seconds=3600)
    assert expired == []


def test_find_expired_policy_votes_returns_overdue(sync_db_path: Path):
    pid = _seed_voting_policy(sync_db_path, started_offset=-7200.0)  # 2h ago
    with open_db(sync_db_path) as conn:
        expired = find_expired_policy_votes(conn, GUILD, timeout_seconds=3600)
    assert [p["id"] for p in expired] == [pid]


def test_find_expired_policy_votes_skips_recent_votes(sync_db_path: Path):
    _seed_voting_policy(sync_db_path, started_offset=-60.0)  # 1m ago
    with open_db(sync_db_path) as conn:
        expired = find_expired_policy_votes(conn, GUILD, timeout_seconds=3600)
    assert expired == []


def test_find_expired_policy_votes_zero_timeout_disabled(sync_db_path: Path):
    _seed_voting_policy(sync_db_path, started_offset=-86400.0)  # 1d ago
    with open_db(sync_db_path) as conn:
        expired = find_expired_policy_votes(conn, GUILD, timeout_seconds=0)
    assert expired == []


def test_find_expired_policy_votes_scopes_to_guild(sync_db_path: Path):
    _seed_voting_policy(sync_db_path, started_offset=-7200.0)
    with open_db(sync_db_path) as conn:
        expired_other = find_expired_policy_votes(conn, GUILD + 1, timeout_seconds=3600)
    assert expired_other == []
