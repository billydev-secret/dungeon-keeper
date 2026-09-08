"""Every table in the schema is classified, or a guild removal leaves it behind.

This is the gate that keeps ``guild_purge_service`` honest as the schema grows.
The service finds guild-scoped tables dynamically, so most new tables are
covered the day they are added — but a table without a ``guild_id`` column is
invisible to that sweep, and the only thing standing between "someone forgot"
and a departed server's rows living in the database forever is this file
failing.

It runs against a **fully migrated** schema rather than a hand-built one, so a
migration landing tomorrow is checked tomorrow, not whenever a test is next
edited.
"""

from __future__ import annotations

import sqlite3

import pytest

from bot_modules.services import guild_purge_service as purge
from tests.db_template import migrated_db


@pytest.fixture
def conn(tmp_path):
    db = migrated_db(tmp_path / "schema.db")
    c = sqlite3.connect(db)
    try:
        yield c
    finally:
        c.close()


def test_every_table_is_classified(conn):
    """No table falls through the four buckets.

    The failure message names the offenders and says what to do, because the
    person who trips this will be someone who added a table for an unrelated
    feature and has never read this module.
    """
    buckets = purge.classify_tables(conn)
    assert buckets["unclassified"] == [], (
        "These tables would survive a guild removal untouched: "
        f"{buckets['unclassified']}. Give each one a `guild_id` column, or add "
        "it to CHILD_TABLES / CHANNEL_TABLES / GLOBAL_TABLES in "
        "bot_modules/services/guild_purge_service.py with a note saying which "
        "it is and why."
    )


def test_classification_covers_the_whole_schema(conn):
    """The buckets partition the schema — nothing counted twice or dropped."""
    buckets = purge.classify_tables(conn)
    named = [t for group in buckets.values() for t in group]
    assert len(named) == len(set(named))
    actual = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert set(named) == actual


def test_declared_tables_all_exist(conn):
    """A hand-written entry pointing at a dropped table is a silent no-op.

    ``purge_guild_data`` would raise on it, which is loud — but only for
    whoever removes the next guild. Catching it here instead means a migration
    that drops a table cannot leave a landmine in the erasure path.
    """
    tables = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    missing = {
        t for t in purge.CHILD_TABLES if t not in tables
    } | {
        parent for _, (_, parent, _) in purge.CHILD_TABLES.items() if parent not in tables
    } | {
        t for t in purge.CHANNEL_TABLES if t not in tables
    } | {
        t for t in purge.GLOBAL_TABLES if t not in tables and not t.startswith("sqlite_")
    }
    assert not missing, f"declared but not in the schema: {sorted(missing)}"


def test_declared_columns_all_exist(conn):
    """Each hand-written foreign key and parent key names a real column."""
    def cols(table: str) -> set[str]:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}

    problems = []
    for table, (fk, parent, pk) in purge.CHILD_TABLES.items():
        if fk not in cols(table):
            problems.append(f"{table}.{fk}")
        if pk not in cols(parent):
            problems.append(f"{parent}.{pk}")
    for table, column in purge.CHANNEL_TABLES.items():
        if column not in cols(table):
            problems.append(f"{table}.{column}")
    assert not problems, f"declared columns missing: {sorted(problems)}"


def test_child_tables_are_not_guild_scoped(conn):
    """A child table that grows a ``guild_id`` should stop being hand-mapped.

    Not a correctness bug — it would be deleted twice, harmlessly — but the
    hand-written rule becomes dead weight that outlives the reason for it.
    """
    scoped = set(purge.guild_scoped_tables(conn))
    overlap = scoped & (set(purge.CHILD_TABLES) | set(purge.CHANNEL_TABLES))
    assert not overlap, (
        f"{sorted(overlap)} now carry guild_id and are found automatically; "
        "drop them from the hand-written maps."
    )


def test_global_tables_hold_no_guild_column(conn):
    """Nothing marked bot-wide is secretly per-guild.

    This is the dangerous direction: a table listed as global that actually
    carries a guild's rows would be *skipped* by every purge, which is the
    exact failure this feature exists to prevent — and it would be silent.
    """
    for table in purge.GLOBAL_TABLES:
        if table.startswith("sqlite_"):
            continue
        columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert "guild_id" not in columns, f"{table} is per-guild, not global"
