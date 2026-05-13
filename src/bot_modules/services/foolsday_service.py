"""April Fools shuffle — DB helpers and core algorithm extracted from foolsday_cog."""

from __future__ import annotations

import random
import sqlite3
import time

DAY_SECONDS = 86_400


# ---------------------------------------------------------------------------
# Pure algorithm
# ---------------------------------------------------------------------------


def derangement(items: list[str], own: list[str]) -> list[str]:
    """Return a permutation of *items* where result[i] != own[i] for all i.

    *items* is the pool of names to distribute (one per slot).
    *own* is the name each slot must NOT receive (the member's original).
    Both lists must have the same length. Falls back to a best-effort shuffle
    if a perfect derangement is impossible (e.g. one name makes up more than
    half the pool).
    """
    n = len(items)
    if n < 2:
        return list(items)

    for _ in range(50):
        shuffled = list(items)
        random.shuffle(shuffled)
        if all(shuffled[i] != own[i] for i in range(n)):
            return shuffled

    shuffled = list(items)
    random.shuffle(shuffled)
    violations = [i for i in range(n) if shuffled[i] == own[i]]
    for i in violations:
        swapped = False
        candidates = list(range(n))
        random.shuffle(candidates)
        for j in candidates:
            if j == i:
                continue
            if shuffled[j] != own[i] and shuffled[i] != own[j]:
                shuffled[i], shuffled[j] = shuffled[j], shuffled[i]
                swapped = True
                break
        if not swapped:
            for j in candidates:
                if j != i and shuffled[j] != own[i]:
                    shuffled[i], shuffled[j] = shuffled[j], shuffled[i]
                    break
    return shuffled


# ---------------------------------------------------------------------------
# DB schema
# ---------------------------------------------------------------------------


def init_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS foolsday_names (
            guild_id   INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            original   TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS foolsday_exclusions (
            guild_id   INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )


# ---------------------------------------------------------------------------
# Name snapshot CRUD
# ---------------------------------------------------------------------------


def save_names(conn: sqlite3.Connection, guild_id: int, names: dict[int, str]) -> None:
    """Store original display names, overwriting any previous snapshot."""
    conn.execute("DELETE FROM foolsday_names WHERE guild_id = ?", (guild_id,))
    conn.executemany(
        "INSERT INTO foolsday_names (guild_id, user_id, original) VALUES (?, ?, ?)",
        [(guild_id, uid, name) for uid, name in names.items()],
    )


def load_names(conn: sqlite3.Connection, guild_id: int) -> dict[int, str]:
    rows = conn.execute(
        "SELECT user_id, original FROM foolsday_names WHERE guild_id = ?",
        (guild_id,),
    ).fetchall()
    return {int(r[0]): r[1] for r in rows}


def clear_names(conn: sqlite3.Connection, guild_id: int) -> None:
    conn.execute("DELETE FROM foolsday_names WHERE guild_id = ?", (guild_id,))


# ---------------------------------------------------------------------------
# Exclusion list
# ---------------------------------------------------------------------------


def add_exclusion(conn: sqlite3.Connection, guild_id: int, user_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO foolsday_exclusions (guild_id, user_id) VALUES (?, ?)",
        (guild_id, user_id),
    )


def remove_exclusion(conn: sqlite3.Connection, guild_id: int, user_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM foolsday_exclusions WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    return (cur.rowcount or 0) > 0


def excluded_user_ids(conn: sqlite3.Connection, guild_id: int) -> set[int]:
    rows = conn.execute(
        "SELECT user_id FROM foolsday_exclusions WHERE guild_id = ?",
        (guild_id,),
    ).fetchall()
    return {int(r[0]) for r in rows}


# ---------------------------------------------------------------------------
# Activity query
# ---------------------------------------------------------------------------


def active_user_ids(
    conn: sqlite3.Connection,
    guild_id: int,
    min_days: int = 3,
    window_days: int = 5,
) -> set[int]:
    """Return user IDs that posted on at least *min_days* of the last *window_days*."""
    cutoff = int(time.time()) - window_days * DAY_SECONDS
    rows = conn.execute(
        """
        SELECT user_id
        FROM (
            SELECT user_id,
                   COUNT(DISTINCT CAST(created_at / ? AS INTEGER)) AS active_days
            FROM processed_messages
            WHERE guild_id = ? AND created_at >= ?
            GROUP BY user_id
        )
        WHERE active_days >= ?
        """,
        (DAY_SECONDS, guild_id, cutoff, min_days),
    ).fetchall()
    return {int(r[0]) for r in rows}
