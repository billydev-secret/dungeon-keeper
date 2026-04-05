"""Invite tracking — caches guild invites and records who invited whom on member join.

Provides:
  - In-memory invite cache refreshed on startup and invite events
  - on_member_join diff to identify the inviter
  - DB storage of invite relationships
  - Query function returning edges compatible with render_connection_web()
"""
from __future__ import annotations

import logging
import sqlite3
import time as _time
import discord

log = logging.getLogger("dungeonkeeper.invite_tracker")


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def init_invite_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS invite_edges (
            guild_id    INTEGER NOT NULL,
            inviter_id  INTEGER NOT NULL,
            invitee_id  INTEGER NOT NULL,
            joined_at   REAL NOT NULL,
            invite_code TEXT,
            PRIMARY KEY (guild_id, invitee_id)
        )
        """
    )


def record_invite(
    conn: sqlite3.Connection,
    guild_id: int,
    inviter_id: int,
    invitee_id: int,
    invite_code: str | None = None,
    joined_at: float | None = None,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO invite_edges (guild_id, inviter_id, invitee_id, joined_at, invite_code)
        VALUES (?, ?, ?, ?, ?)
        """,
        (guild_id, inviter_id, invitee_id, joined_at or _time.time(), invite_code),
    )


def query_invite_web(
    conn: sqlite3.Connection,
    guild_id: int,
) -> list[tuple[int, int, int]]:
    """Return edges as (inviter_id, invitee_id, count).

    Each inviter→invitee pair gets weight 1.  To make the graph useful with
    render_connection_web, we group by inviter and count how many people they
    invited, returning one edge per inviter-invitee pair.
    """
    rows = conn.execute(
        """
        SELECT inviter_id, invitee_id, 1 AS w
        FROM invite_edges
        WHERE guild_id = ?
        """,
        (guild_id,),
    ).fetchall()
    return [(int(r[0]), int(r[1]), int(r[2])) for r in rows]


# ---------------------------------------------------------------------------
# In-memory invite cache
# ---------------------------------------------------------------------------

# {guild_id: {invite_code: uses}}
_invite_cache: dict[int, dict[str, int]] = {}


async def refresh_invite_cache(guild: discord.Guild) -> None:
    """Snapshot current invite use counts for a guild."""
    try:
        invites = await guild.invites()
        _invite_cache[guild.id] = {inv.code: inv.uses or 0 for inv in invites}
        log.info("Invite cache refreshed for %s: %d invites tracked.", guild.name, len(invites))
    except discord.Forbidden:
        log.warning("Missing Manage Server permission to cache invites for %s.", guild.name)
    except discord.HTTPException as exc:
        log.warning("Failed to fetch invites for %s: %s", guild.name, exc)


async def detect_inviter(guild: discord.Guild) -> tuple[int | None, str | None]:
    """Compare current invite counts to cache and return (inviter_id, invite_code).

    Returns (None, None) if the invite can't be determined.
    Also refreshes the cache with the new counts.
    """
    old_cache = _invite_cache.get(guild.id, {})

    try:
        current_invites = await guild.invites()
    except (discord.Forbidden, discord.HTTPException):
        return None, None

    new_cache = {inv.code: inv.uses or 0 for inv in current_invites}

    inviter_id: int | None = None
    invite_code: str | None = None

    for inv in current_invites:
        old_uses = old_cache.get(inv.code, 0)
        new_uses = inv.uses or 0
        if new_uses > old_uses and inv.inviter is not None:
            inviter_id = inv.inviter.id
            invite_code = inv.code
            break

    _invite_cache[guild.id] = new_cache
    return inviter_id, invite_code
