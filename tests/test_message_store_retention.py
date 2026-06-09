"""Tests for content-storage gating in message_store.

Covers the per-guild ``message_storage_level`` privacy feature: at level
``none`` (the default) raw message content/attachments/embeds are dropped at
ingest while derivations (sentiment, @-mention edges, the row skeleton used for
Discord deep-links) are kept; switching a guild to ``none`` purges any content
already stored.
"""

from __future__ import annotations

import sqlite3

import pytest

from bot_modules.core.db_utils import set_config_value
from bot_modules.services.message_store import (
    guild_retains_content,
    init_message_tables,
    purge_guild_message_content,
    store_message,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_message_tables(conn)
    conn.execute(
        """
        CREATE TABLE config (
            guild_id INTEGER NOT NULL,
            key      TEXT NOT NULL,
            value    TEXT NOT NULL,
            PRIMARY KEY (guild_id, key)
        )
        """
    )
    return conn


def _store(conn: sqlite3.Connection, *, retain_content: bool, guild_id: int = 1,
           message_id: int = 100) -> None:
    store_message(
        conn,
        message_id=message_id,
        guild_id=guild_id,
        channel_id=10,
        author_id=50,
        content="secret text",
        reply_to_id=None,
        ts=1_000_000,
        attachment_urls=["https://cdn.example/img.png"],
        mention_ids=[77],
        sentiment=0.5,
        emotion="joy",
        embeds=[{"title": "embed title", "description": "embed body"}],
        retain_content=retain_content,
    )


def _row(conn: sqlite3.Connection, message_id: int = 100) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM messages WHERE message_id = ?", (message_id,)
    ).fetchone()


# ── ingest gating ─────────────────────────────────────────────────────


def test_retain_content_true_stores_everything():
    conn = _conn()
    _store(conn, retain_content=True)
    row = _row(conn)
    assert row["content"] == "secret text"
    assert row["sentiment"] == 0.5
    assert row["emotion"] == "joy"
    assert conn.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM message_embeds").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM message_mentions").fetchone()[0] == 1


def test_retain_content_false_drops_content_but_keeps_derivations():
    conn = _conn()
    _store(conn, retain_content=False)
    row = _row(conn)
    # Row skeleton survives → message_id/channel/guild still reconstruct a link.
    assert row is not None
    assert row["channel_id"] == 10
    assert row["author_id"] == 50
    # Content + media are gone.
    assert row["content"] is None
    assert conn.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM message_embeds").fetchone()[0] == 0
    # Derivations are kept.
    assert row["sentiment"] == 0.5
    assert row["emotion"] == "joy"
    assert conn.execute("SELECT COUNT(*) FROM message_mentions").fetchone()[0] == 1


def test_retain_content_false_does_not_resurrect_content_from_embeds():
    """Embed-only messages must not have flattened embed text revived as content."""
    conn = _conn()
    store_message(
        conn,
        message_id=200,
        guild_id=1,
        channel_id=10,
        author_id=50,
        content=None,
        reply_to_id=None,
        ts=1_000_000,
        attachment_urls=[],
        mention_ids=[],
        embeds=[{"title": "leak", "description": "should not persist"}],
        retain_content=False,
    )
    assert _row(conn, 200)["content"] is None


# ── purge on switch ───────────────────────────────────────────────────


def test_purge_nulls_content_and_keeps_derivations():
    conn = _conn()
    _store(conn, retain_content=True)
    purged = purge_guild_message_content(conn, guild_id=1)
    assert purged == 1
    row = _row(conn)
    assert row["content"] is None
    # Derivations survive the purge.
    assert row["sentiment"] == 0.5
    assert row["emotion"] == "joy"
    assert conn.execute("SELECT COUNT(*) FROM message_mentions").fetchone()[0] == 1
    # Media is removed.
    assert conn.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM message_embeds").fetchone()[0] == 0


def test_purge_is_guild_scoped():
    conn = _conn()
    _store(conn, retain_content=True, guild_id=1, message_id=100)
    _store(conn, retain_content=True, guild_id=2, message_id=101)
    purge_guild_message_content(conn, guild_id=1)
    assert _row(conn, 100)["content"] is None
    assert _row(conn, 101)["content"] == "secret text"  # other guild untouched


def test_purge_on_empty_guild_returns_zero():
    conn = _conn()
    assert purge_guild_message_content(conn, guild_id=999) == 0


# ── level lookup ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "stored,expected",
    [(None, False), ("none", False), ("all", True), ("bogus", False)],
)
def test_guild_retains_content_reads_level(stored, expected):
    conn = _conn()
    if stored is not None:
        set_config_value(conn, "message_storage_level", stored, guild_id=1)
    assert guild_retains_content(conn, 1, allow_legacy_fallback=False) is expected
