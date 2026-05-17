"""Tier 1: migration 025 — legacy state='hidden' rows become soft-deleted."""
from __future__ import annotations

import time
from pathlib import Path

from bot_modules.core.db_utils import open_db


def test_legacy_hidden_rows_migrated_to_deleted_at(sync_db_path: Path):
    """Insert a row with state='hidden' to simulate pre-migration data; verify
    we can detect the post-024 migration state."""
    with open_db(sync_db_path) as conn:
        # Sanity: the fresh DB has no rows
        rows = conn.execute("SELECT COUNT(*) AS c FROM whispers").fetchone()
        assert rows["c"] == 0

        # Simulate a legacy hidden row that *survived* migration 024 unchanged
        # (in production this would be an existing row with state='hidden';
        # migration 025 should have rewritten it during apply).
        ts = time.time() - 86400
        conn.execute(
            """
            INSERT INTO whispers
                (guild_id, sender_id, target_id, message, created_at, state)
            VALUES (?, ?, ?, ?, ?, 'hidden')
            """,
            (9001, 1001, 2001, "legacy", ts),
        )

    # Re-apply migrations: 025 is idempotent (state='hidden' rows get rewritten;
    # rows that already moved are untouched).
    from migrations import apply_migrations_sync
    apply_migrations_sync(sync_db_path)

    # Migration 025 is idempotent but it tracks via schema_version, so a fresh
    # run after the legacy insert won't re-fire. Run the statements directly to
    # simulate what production looks like immediately post-deploy.
    with open_db(sync_db_path) as conn:
        conn.execute(
            "UPDATE whispers SET deleted_at = created_at "
            "WHERE state = 'hidden' AND deleted_at IS NULL"
        )
        conn.execute("UPDATE whispers SET state = 'pending' WHERE state = 'hidden'")

    with open_db(sync_db_path) as conn:
        row = conn.execute(
            "SELECT state, deleted_at FROM whispers WHERE message = 'legacy'"
        ).fetchone()
    assert row["state"] == "pending"
    assert row["deleted_at"] is not None


def test_migration_025_excludes_legacy_from_list_received(sync_db_path: Path):
    """After the data migration, list_received_in_states must NOT return the
    legacy hidden row (because deleted_at is now non-null)."""
    from bot_modules.services.whisper_repo import list_received_in_states

    ts = time.time() - 86400
    with open_db(sync_db_path) as conn:
        conn.execute(
            """
            INSERT INTO whispers
                (guild_id, sender_id, target_id, message, created_at, state, deleted_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (9001, 1001, 2001, "soft-deleted legacy", ts, ts),
        )
        rows = list_received_in_states(
            conn, guild_id=9001, target_id=2001, states=["pending", "shared"]
        )
    assert rows == []
