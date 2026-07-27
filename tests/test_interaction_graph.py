"""Tests for bot_modules.services.interaction_graph.

Covers the DB layer: table init idempotency, guild-scoped clearing, and
interaction recording (weights, self-loop skip, message-id dedup).
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.interaction_graph import (
    clear_interaction_data,
    init_interaction_tables,
    record_interactions,
)
from tests.db_template import migrated_db


# ── Label sanitization ───────────────────────────────────────────────


# ── DB init and clear ────────────────────────────────────────────────


@pytest.fixture
def db_conn(tmp_path):
    """A migrated DB connection ready for interaction-graph tests."""
    path = tmp_path / "ig.db"
    migrated_db(path)
    with open_db(path) as conn:
        init_interaction_tables(conn)
        yield conn


def test_init_interaction_tables_is_idempotent(tmp_path):
    """init can be called many times without error."""
    path = tmp_path / "ig.db"
    migrated_db(path)
    with open_db(path) as conn:
        init_interaction_tables(conn)
        init_interaction_tables(conn)
        init_interaction_tables(conn)
        # Smoke: tables present
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "user_interactions" in names
        assert "user_interactions_log" in names


def test_clear_interaction_data_removes_only_target_guild(db_conn):
    record_interactions(db_conn, guild_id=10, from_user_id=1, to_user_ids=[2])
    record_interactions(db_conn, guild_id=20, from_user_id=1, to_user_ids=[2])

    clear_interaction_data(db_conn, guild_id=10)

    rest = db_conn.execute(
        "SELECT COUNT(*) FROM user_interactions"
    ).fetchone()[0]
    assert rest == 1  # only guild 20 left


# ── record_interactions ──────────────────────────────────────────────


def test_record_interactions_inserts_aggregate_and_log(db_conn):
    record_interactions(db_conn, guild_id=10, from_user_id=1, to_user_ids=[2, 3])

    rows = db_conn.execute(
        "SELECT from_user_id, to_user_id, weight FROM user_interactions"
        " WHERE guild_id = 10 ORDER BY to_user_id"
    ).fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [(1, 2, 1), (1, 3, 1)]

    log_rows = db_conn.execute(
        "SELECT COUNT(*) FROM user_interactions_log WHERE guild_id = 10"
    ).fetchone()[0]
    assert log_rows == 2


def test_record_interactions_increments_existing_weight(db_conn):
    record_interactions(db_conn, guild_id=10, from_user_id=1, to_user_ids=[2])
    record_interactions(db_conn, guild_id=10, from_user_id=1, to_user_ids=[2], amount=3)

    weight = db_conn.execute(
        "SELECT weight FROM user_interactions"
        " WHERE guild_id = 10 AND from_user_id = 1 AND to_user_id = 2"
    ).fetchone()[0]
    assert weight == 4  # 1 + 3


def test_record_interactions_skips_self_interaction(db_conn):
    """A reply or mention to oneself must not be counted."""
    record_interactions(db_conn, guild_id=10, from_user_id=1, to_user_ids=[1, 2])

    rows = db_conn.execute(
        "SELECT from_user_id, to_user_id FROM user_interactions"
        " WHERE guild_id = 10"
    ).fetchall()
    assert (1, 2) in [(r[0], r[1]) for r in rows]
    assert (1, 1) not in [(r[0], r[1]) for r in rows]


def test_record_interactions_dedupes_via_message_id(db_conn):
    """Same message_id seen twice (live + backfill) must increment only once."""
    record_interactions(
        db_conn, guild_id=10, from_user_id=1, to_user_ids=[2], message_id=500
    )
    record_interactions(
        db_conn, guild_id=10, from_user_id=1, to_user_ids=[2], message_id=500
    )

    weight = db_conn.execute(
        "SELECT weight FROM user_interactions"
        " WHERE guild_id = 10 AND from_user_id = 1 AND to_user_id = 2"
    ).fetchone()[0]
    assert weight == 1  # second insert was a duplicate


def test_record_interactions_without_message_id_does_not_dedupe(db_conn):
    """Without message_id, the unique index doesn't apply — counts increment."""
    record_interactions(db_conn, guild_id=10, from_user_id=1, to_user_ids=[2])
    record_interactions(db_conn, guild_id=10, from_user_id=1, to_user_ids=[2])

    weight = db_conn.execute(
        "SELECT weight FROM user_interactions"
        " WHERE guild_id = 10 AND from_user_id = 1 AND to_user_id = 2"
    ).fetchone()[0]
    assert weight == 2


# ── query_connection_web ─────────────────────────────────────────────


def _mark_bot(conn, guild_id, user_id):
    conn.execute(
        "INSERT INTO known_users (guild_id, user_id, is_bot) VALUES (?, ?, 1)"
        " ON CONFLICT(guild_id, user_id) DO UPDATE SET is_bot = 1",
        (guild_id, user_id),
    )


# ── _find_components ────────────────────────────────────────────────


# ── _detect_communities ─────────────────────────────────────────────


# ── _radial_layout ──────────────────────────────────────────────────


# ── Geometry: segment crossing ──────────────────────────────────────


# ── Render functions (smoke tests) ──────────────────────────────────


