"""CRUD + placement bookkeeping for docs.

All functions take an open ``sqlite3.Connection`` (via ``ctx.open_db()``) and are
synchronous — callers wrap them in ``asyncio.to_thread`` / ``run_query``. The
schema lives in ``migrations/059_docs.sql``.
"""

from __future__ import annotations

import re
import sqlite3

DOC_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,48}$")
TITLE_MAX_LEN = 200
BODY_MAX_LEN = 40_000


def slugify_key(raw: str) -> str:
    """Normalise a user-supplied key into a valid slug (may return '')."""
    slug = re.sub(r"[^a-z0-9]+", "-", (raw or "").strip().lower()).strip("-")
    return slug[:49]


def _doc_row(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "guild_id": r["guild_id"],
        "doc_key": r["doc_key"],
        "title": r["title"],
        "body_md": r["body_md"],
        "accent": r["accent"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "updated_by": r["updated_by"],
    }


# ── docs ────────────────────────────────────────────────────────────

def list_docs(conn: sqlite3.Connection, guild_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM docs WHERE guild_id = ? ORDER BY doc_key",
        (guild_id,),
    ).fetchall()
    return [_doc_row(r) for r in rows]


def get_doc(conn: sqlite3.Connection, guild_id: int, doc_key: str) -> dict | None:
    r = conn.execute(
        "SELECT * FROM docs WHERE guild_id = ? AND doc_key = ?",
        (guild_id, doc_key),
    ).fetchone()
    return _doc_row(r) if r else None


def get_doc_by_id(conn: sqlite3.Connection, doc_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM docs WHERE id = ?", (doc_id,)).fetchone()
    return _doc_row(r) if r else None


def create_doc(
    conn: sqlite3.Connection,
    guild_id: int,
    doc_key: str,
    title: str,
    body_md: str,
    accent: str,
    user_id: int,
    now: float,
) -> int:
    cur = conn.execute(
        "INSERT INTO docs (guild_id, doc_key, title, body_md, accent,"
        " created_at, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (guild_id, doc_key, title, body_md, accent, now, now, user_id),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def update_doc(
    conn: sqlite3.Connection,
    doc_id: int,
    *,
    title: str,
    body_md: str,
    accent: str,
    user_id: int,
    now: float,
) -> None:
    conn.execute(
        "UPDATE docs SET title = ?, body_md = ?, accent = ?,"
        " updated_at = ?, updated_by = ? WHERE id = ?",
        (title, body_md, accent, now, user_id, doc_id),
    )
    conn.commit()


def delete_doc(conn: sqlite3.Connection, doc_id: int) -> None:
    """Delete a doc and all of its placement/message bookkeeping."""
    placement_ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM doc_placements WHERE doc_id = ?", (doc_id,)
        ).fetchall()
    ]
    for pid in placement_ids:
        conn.execute(
            "DELETE FROM doc_placement_messages WHERE placement_id = ?", (pid,)
        )
    conn.execute("DELETE FROM doc_placements WHERE doc_id = ?", (doc_id,))
    conn.execute("DELETE FROM docs WHERE id = ?", (doc_id,))
    conn.commit()


# ── placements ──────────────────────────────────────────────────────

def list_placements(conn: sqlite3.Connection, doc_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT p.id, p.channel_id, p.pinned, p.created_at, p.updated_at,"
        " (SELECT COUNT(*) FROM doc_placement_messages m WHERE m.placement_id = p.id)"
        "   AS message_count"
        " FROM doc_placements p WHERE p.doc_id = ? ORDER BY p.created_at",
        (doc_id,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "channel_id": r["channel_id"],
            "pinned": bool(r["pinned"]),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "message_count": r["message_count"],
        }
        for r in rows
    ]


def get_placement(
    conn: sqlite3.Connection, doc_id: int, channel_id: int
) -> dict | None:
    r = conn.execute(
        "SELECT * FROM doc_placements WHERE doc_id = ? AND channel_id = ?",
        (doc_id, channel_id),
    ).fetchone()
    if not r:
        return None
    return {
        "id": r["id"],
        "doc_id": r["doc_id"],
        "channel_id": r["channel_id"],
        "pinned": bool(r["pinned"]),
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def upsert_placement(
    conn: sqlite3.Connection, doc_id: int, channel_id: int, now: float
) -> int:
    existing = get_placement(conn, doc_id, channel_id)
    if existing:
        conn.execute(
            "UPDATE doc_placements SET updated_at = ? WHERE id = ?",
            (now, existing["id"]),
        )
        conn.commit()
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO doc_placements (doc_id, channel_id, created_at, updated_at)"
        " VALUES (?, ?, ?, ?)",
        (doc_id, channel_id, now, now),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def set_placement_pinned(
    conn: sqlite3.Connection, placement_id: int, pinned: bool, now: float
) -> None:
    conn.execute(
        "UPDATE doc_placements SET pinned = ?, updated_at = ? WHERE id = ?",
        (1 if pinned else 0, now, placement_id),
    )
    conn.commit()


def delete_placement(conn: sqlite3.Connection, placement_id: int) -> None:
    conn.execute(
        "DELETE FROM doc_placement_messages WHERE placement_id = ?", (placement_id,)
    )
    conn.execute("DELETE FROM doc_placements WHERE id = ?", (placement_id,))
    conn.commit()


def get_placement_message_ids(
    conn: sqlite3.Connection, placement_id: int
) -> list[int]:
    rows = conn.execute(
        "SELECT message_id FROM doc_placement_messages"
        " WHERE placement_id = ? ORDER BY position",
        (placement_id,),
    ).fetchall()
    return [int(r["message_id"]) for r in rows]


def set_placement_message_ids(
    conn: sqlite3.Connection,
    placement_id: int,
    message_ids: list[int],
    now: float,
) -> None:
    """Replace the ordered message-id list for a placement."""
    conn.execute(
        "DELETE FROM doc_placement_messages WHERE placement_id = ?", (placement_id,)
    )
    conn.executemany(
        "INSERT INTO doc_placement_messages (placement_id, message_id, position)"
        " VALUES (?, ?, ?)",
        [(placement_id, mid, pos) for pos, mid in enumerate(message_ids)],
    )
    conn.execute(
        "UPDATE doc_placements SET updated_at = ? WHERE id = ?", (now, placement_id)
    )
    conn.commit()
