"""Tests for migration 007 (beta source-tag columns)."""

from __future__ import annotations



async def _column_names(db, table: str) -> list[str]:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    return [row[1] for row in rows]


async def test_migration_007_adds_source_to_messages(temp_db):
    cols = await _column_names(temp_db, "messages")
    assert "source" in cols, f"expected 'source' column on messages, got {cols}"


async def test_migration_007_adds_source_to_member_xp(temp_db):
    cols = await _column_names(temp_db, "member_xp")
    assert "source" in cols, f"expected 'source' column on member_xp, got {cols}"


async def test_migration_007_adds_source_to_jails(temp_db):
    cols = await _column_names(temp_db, "jails")
    assert "source" in cols, f"expected 'source' column on jails, got {cols}"


async def test_migration_007_adds_source_to_tickets(temp_db):
    cols = await _column_names(temp_db, "tickets")
    assert "source" in cols, f"expected 'source' column on tickets, got {cols}"


async def test_migration_007_creates_index(temp_db):
    cursor = await temp_db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_messages_source'"
    )
    row = await cursor.fetchone()
    assert row is not None, "expected idx_messages_source index to exist"


async def test_migration_007_is_idempotent(temp_db):
    """Re-applying migrations on an already-migrated DB should be a no-op."""
    from migrations import apply_migrations
    await apply_migrations(temp_db)  # second apply
    # Still works:
    cols = await _column_names(temp_db, "messages")
    assert "source" in cols
