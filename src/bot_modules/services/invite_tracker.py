"""Invite tracking — caches guild invites and records who invited whom on member join.

The cache maps invite code → :class:`InviteSnapshot` per guild. On each join
the current invite list is diffed against the cache (:func:`diff_invite_snapshot`,
pure and unit-tested) to find the invite that was consumed. Two attribution
paths:

  - a code whose use count increased (the common case; only one use is
    consumed per join, so a burst of joins drains a +N delta one at a time),
  - a code that *vanished* while sitting one use short of ``max_uses`` — a
    consumed single-use invite, which Discord deletes before we ever see its
    count go up. This was the collector's biggest silent miss.

Misses and permission failures are logged loudly — invite_edges data quality
was ~15% of joins before 2026-07 because failures here were invisible.
"""

from __future__ import annotations

import logging
import sqlite3
import time as _time
from typing import NamedTuple

import discord

log = logging.getLogger("dungeonkeeper.invite_tracker")


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# In-memory invite cache + diff
# ---------------------------------------------------------------------------


class InviteSnapshot(NamedTuple):
    uses: int
    max_uses: int  # 0 = unlimited
    inviter_id: int | None


# {guild_id: {invite_code: InviteSnapshot}}
_invite_cache: dict[int, dict[str, InviteSnapshot]] = {}


def snapshot_invites(invites: list) -> dict[str, InviteSnapshot]:
    """Map a ``guild.invites()`` result into cacheable snapshots."""
    return {
        inv.code: InviteSnapshot(
            uses=inv.uses or 0,
            max_uses=inv.max_uses or 0,
            inviter_id=inv.inviter.id if inv.inviter else None,
        )
        for inv in invites
    }


def diff_invite_snapshot(
    old: dict[str, InviteSnapshot],
    new: dict[str, InviteSnapshot],
) -> tuple[int | None, str | None, dict[str, InviteSnapshot]]:
    """Attribute one join by diffing invite snapshots.

    Returns ``(inviter_id, invite_code, updated_cache)``. The updated cache
    consumes exactly ONE use of the matched invite: when a burst of joins
    bumps a code by +N before we diff, the remaining N-1 uses stay
    unconsumed in the cache so the next join events can each claim one
    (matching the new counts wholesale would silently swallow them).
    """
    # Path 1: a known or new code whose use count went up.
    for code, snap in new.items():
        old_uses = old[code].uses if code in old else 0
        if snap.uses > old_uses and snap.inviter_id is not None:
            updated = dict(new)
            updated[code] = snap._replace(uses=old_uses + 1)
            return snap.inviter_id, code, updated

    # Path 2: a cached code that vanished one use short of its cap —
    # a consumed single/limited-use invite (deleted by Discord on use).
    for code, snap in old.items():
        if (
            code not in new
            and snap.max_uses > 0
            and snap.uses == snap.max_uses - 1
            and snap.inviter_id is not None
        ):
            return snap.inviter_id, code, dict(new)

    return None, None, dict(new)


async def refresh_invite_cache(guild: discord.Guild) -> None:
    """Snapshot current invite state for a guild."""
    try:
        invites = await guild.invites()
        _invite_cache[guild.id] = snapshot_invites(invites)
        log.info(
            "Invite cache refreshed for %s: %d invites tracked.",
            guild.name,
            len(invites),
        )
    except discord.Forbidden:
        log.warning(
            "Missing Manage Server permission to cache invites for %s — "
            "invite attribution is OFF for this guild.",
            guild.name,
        )
    except discord.HTTPException as exc:
        log.warning("Failed to fetch invites for %s: %s", guild.name, exc)


def cache_invite_create(guild_id: int, invite: discord.Invite) -> None:
    """Track a newly created invite immediately (no full refetch needed)."""
    _invite_cache.setdefault(guild_id, {})[invite.code] = InviteSnapshot(
        uses=invite.uses or 0,
        max_uses=invite.max_uses or 0,
        inviter_id=invite.inviter.id if invite.inviter else None,
    )


def cache_invite_delete(guild_id: int, code: str) -> None:
    """Forget a deleted invite — unless it looks freshly consumed.

    A limited-use invite is deleted by Discord the moment its last use is
    spent, and the delete event can beat the member-join event. Keeping a
    one-use-short snapshot lets the join's diff still attribute it
    (vanished-code path); anything else deleted is a mod action, forget it.
    """
    cached = _invite_cache.get(guild_id, {}).get(code)
    if cached and cached.max_uses > 0 and cached.uses == cached.max_uses - 1:
        return
    _invite_cache.get(guild_id, {}).pop(code, None)


async def detect_inviter(guild: discord.Guild) -> tuple[int | None, str | None]:
    """Identify the inviter for a join that just happened.

    Returns ``(inviter_id, invite_code)``, or ``(None, None)`` when the join
    can't be attributed — in which case the reason is logged, never swallowed.
    """
    old_cache = _invite_cache.get(guild.id)

    try:
        current_invites = await guild.invites()
    except discord.Forbidden:
        log.warning(
            "Can't attribute join in %s: missing Manage Server permission.",
            guild.name,
        )
        return None, None
    except discord.HTTPException as exc:
        log.warning("Can't attribute join in %s: invite fetch failed (%s).", guild.name, exc)
        return None, None

    new_cache = snapshot_invites(current_invites)

    if old_cache is None:
        # First sighting (join raced on_ready's refresh): nothing to diff
        # against, so seed the cache and record the miss.
        _invite_cache[guild.id] = new_cache
        log.warning(
            "Join in %s before the invite cache was seeded — attribution missed.",
            guild.name,
        )
        return None, None

    inviter_id, invite_code, updated = diff_invite_snapshot(old_cache, new_cache)
    _invite_cache[guild.id] = updated

    if inviter_id is None:
        log.warning(
            "Join in %s matched no invite (vanity URL, bot add, or a diff race) — "
            "%d invites checked.",
            guild.name,
            len(new_cache),
        )
    return inviter_id, invite_code
