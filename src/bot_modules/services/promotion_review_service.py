"""Promotion-review cards for members who lost access and came back.

The bot already posts a *promotion review* card to the Level 5 Log Channel
(``xp_level_5_log_channel_id``) when a member reaches Level 5 (see
:func:`bot_modules.services.xp_service.maybe_log_level_5`). This module extends
that channel with two more triggers, and adds a persistent **Grant access**
button to the cards:

* **pruned-return** — an automated sweep pulled a role (recorded in
  ``role_prune_events``; see :mod:`role_grant_audit_service`) and the member is
  active again, detected the moment they post a message anywhere. The Grant
  button re-adds a configured role and closes their open prune events.
* **sleeper** — a member held in the inactive/"sleeper" channel
  (``inactive_members``; the channel is ``inactive_channel_id``) posts there
  again. The Grant button runs the full inactive-reactivate flow.

Both new triggers ship **dark** until the Level 5 Log Channel is configured;
the pruned-return trigger additionally needs ``promotion_review_grant_role_id``.

This module owns the durable card ledger (``promotion_review_cards``, migration
112) and the pure gating logic. The Discord embed + persistent buttons live in
:mod:`bot_modules.services.promotion_review_views`; the message-hot-path hook
uses :func:`is_watched` for an O(1) filter before ever touching the DB.
"""

from __future__ import annotations

import sqlite3
import threading

from bot_modules.core.db_utils import get_config_value, open_db
from bot_modules.inactive.store import active_inactive_user_ids

CHANNEL_KEY = "xp_level_5_log_channel_id"  # the promotion-reviews channel
GRANT_ROLE_KEY = "promotion_review_grant_role_id"
SLEEPER_CHANNEL_KEY = "inactive_channel_id"

KIND_PRUNED_RETURN = "pruned_return"
KIND_SLEEPER = "sleeper"

RESOLUTION_GRANTED = "granted"
RESOLUTION_REACTIVATED = "reactivated"
RESOLUTION_DISMISSED = "dismissed"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _int_config(conn: sqlite3.Connection, key: str, guild_id: int) -> int:
    try:
        return int(get_config_value(conn, key, "0", guild_id))
    except (TypeError, ValueError):
        return 0


def review_channel_id(conn: sqlite3.Connection, guild_id: int) -> int:
    return _int_config(conn, CHANNEL_KEY, guild_id)


def grant_role_id(conn: sqlite3.Connection, guild_id: int) -> int:
    return _int_config(conn, GRANT_ROLE_KEY, guild_id)


def sleeper_channel_id(conn: sqlite3.Connection, guild_id: int) -> int:
    return _int_config(conn, SLEEPER_CHANNEL_KEY, guild_id)


def is_enabled(conn: sqlite3.Connection, guild_id: int) -> bool:
    """True once there's a promotion-reviews channel to post cards into.

    A card can post as long as the review channel is set; the pruned-return
    *button* additionally needs a grant role, gated per-kind in
    :func:`evaluate_trigger`.
    """
    return review_channel_id(conn, guild_id) > 0


# ---------------------------------------------------------------------------
# Card ledger
# ---------------------------------------------------------------------------


def get_open_card(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> sqlite3.Row | None:
    """The member's un-resolved review card, if one is already posted."""
    return conn.execute(
        "SELECT * FROM promotion_review_cards "
        "WHERE guild_id = ? AND user_id = ? AND resolved_at IS NULL",
        (guild_id, user_id),
    ).fetchone()


def get_card(conn: sqlite3.Connection, card_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM promotion_review_cards WHERE id = ?", (card_id,)
    ).fetchone()


def reserve_card(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    kind: str,
    created_at: float,
) -> int | None:
    """Claim the single open-card slot for a member; returns the new card id.

    Inserts a placeholder (channel/message filled in once the card is actually
    posted). The partial unique index makes a racing second insert fail — we
    return ``None`` so that caller treats it as "already carded" and skips,
    which is how two near-simultaneous messages can't both post a card.
    """
    try:
        cursor = conn.execute(
            "INSERT INTO promotion_review_cards "
            "(guild_id, user_id, kind, channel_id, message_id, created_at) "
            "VALUES (?, ?, ?, 0, 0, ?)",
            (guild_id, user_id, kind, created_at),
        )
    except sqlite3.IntegrityError:
        return None
    return int(cursor.lastrowid or 0)


def set_card_message(
    conn: sqlite3.Connection, card_id: int, channel_id: int, message_id: int
) -> None:
    """Attach the posted message's location to a reserved card row."""
    conn.execute(
        "UPDATE promotion_review_cards SET channel_id = ?, message_id = ? WHERE id = ?",
        (channel_id, message_id, card_id),
    )


def delete_card(conn: sqlite3.Connection, card_id: int) -> None:
    """Roll back a reserved card whose post never made it to Discord."""
    conn.execute("DELETE FROM promotion_review_cards WHERE id = ?", (card_id,))


def resolve_card(
    conn: sqlite3.Connection,
    card_id: int,
    resolved_by: int,
    resolved_at: float,
    resolution: str,
) -> int:
    """Close an open card; returns rows updated (0 if already resolved)."""
    cursor = conn.execute(
        "UPDATE promotion_review_cards "
        "SET resolved_at = ?, resolved_by = ?, resolution = ? "
        "WHERE id = ? AND resolved_at IS NULL",
        (resolved_at, resolved_by, resolution, card_id),
    )
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Trigger populations
# ---------------------------------------------------------------------------


def open_prune_user_ids(conn: sqlite3.Connection, guild_id: int) -> set[int]:
    """Members with at least one open (unrestored) auto-sweep prune event.

    Role-agnostic on purpose: any sweep that stripped a role qualifies the
    member — the grant button re-adds the *configured* role.
    """
    rows = conn.execute(
        "SELECT DISTINCT user_id FROM role_prune_events "
        "WHERE guild_id = ? AND restored_at IS NULL",
        (guild_id,),
    ).fetchall()
    return {int(r["user_id"]) for r in rows}


def carded_user_ids(conn: sqlite3.Connection, guild_id: int) -> set[int]:
    return {
        int(r["user_id"])
        for r in conn.execute(
            "SELECT user_id FROM promotion_review_cards "
            "WHERE guild_id = ? AND resolved_at IS NULL",
            (guild_id,),
        ).fetchall()
    }


def watch_candidates(conn: sqlite3.Connection, guild_id: int) -> set[int]:
    """Everyone the hot path should watch: owed a card by either new trigger.

    = (open prune events ∪ currently inactive-held) − already carded. Sleepers
    are included by user id; the channel condition is checked at post time.
    """
    pruned = open_prune_user_ids(conn, guild_id)
    sleepers = active_inactive_user_ids(conn, guild_id)
    return (pruned | sleepers) - carded_user_ids(conn, guild_id)


def evaluate_trigger(
    conn: sqlite3.Connection, guild_id: int, user_id: int, posted_channel_id: int
) -> str | None:
    """Which card kind (if any) a message from ``user_id`` should post.

    Returns ``KIND_PRUNED_RETURN``, ``KIND_SLEEPER``, or ``None``. The
    authoritative DB-truth gate the hot path confirms with before posting.
    """
    if not is_enabled(conn, guild_id):
        return None
    if get_open_card(conn, guild_id, user_id) is not None:
        return None
    # Pruned-return: an open prune event, a configured grant role, any channel.
    if grant_role_id(conn, guild_id) > 0 and user_id in open_prune_user_ids(
        conn, guild_id
    ):
        return KIND_PRUNED_RETURN
    # Sleeper: held inactive AND posting in the sleeper channel.
    sleeper_chan = sleeper_channel_id(conn, guild_id)
    if (
        sleeper_chan > 0
        and posted_channel_id == sleeper_chan
        and user_id in active_inactive_user_ids(conn, guild_id)
    ):
        return KIND_SLEEPER
    return None


def still_candidate(conn: sqlite3.Connection, guild_id: int, user_id: int) -> bool:
    """True if the member still belongs on the watch set at all (any channel).

    Used to decide whether a no-op message (e.g. a sleeper posting outside the
    sleeper channel) should drop them from the watch set or keep them for later.
    """
    return (
        user_id in open_prune_user_ids(conn, guild_id)
        or user_id in active_inactive_user_ids(conn, guild_id)
    )


# ---------------------------------------------------------------------------
# Card-content helpers
# ---------------------------------------------------------------------------


def pruned_roles_for(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> list[tuple[int, float | None]]:
    """``(role_id, pruned_at)`` for each open prune event, most recent first."""
    rows = conn.execute(
        "SELECT role_id, MAX(pruned_at) AS pruned_at FROM role_prune_events "
        "WHERE guild_id = ? AND user_id = ? AND restored_at IS NULL "
        "GROUP BY role_id ORDER BY pruned_at DESC",
        (guild_id, user_id),
    ).fetchall()
    return [
        (
            int(r["role_id"]),
            float(r["pruned_at"]) if r["pruned_at"] is not None else None,
        )
        for r in rows
    ]


def mark_prunes_restored(
    conn: sqlite3.Connection, guild_id: int, user_id: int, restored_at: float
) -> int:
    """Close every open prune event for the member (they got access back)."""
    cursor = conn.execute(
        "UPDATE role_prune_events SET restored_at = ? "
        "WHERE guild_id = ? AND user_id = ? AND restored_at IS NULL",
        (restored_at, guild_id, user_id),
    )
    return cursor.rowcount


def member_level(conn: sqlite3.Connection, guild_id: int, user_id: int) -> int:
    """The member's current level for the card, 0 if they have no XP row."""
    row = conn.execute(
        "SELECT level FROM member_xp WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    return int(row["level"]) if row else 0


# ---------------------------------------------------------------------------
# In-memory watch registry — the message hot-path accelerator
# ---------------------------------------------------------------------------
#
# on_message fires constantly; a DB round-trip per message is unacceptable. We
# keep a per-guild set of user ids that *might* be owed a card so the hot path
# is a single set-membership test. The set is seeded at startup (warm) and fed
# by the prune sweep (note_pruned) and the inactive-hold path (note_inactive) —
# the only writers of the two populations — so it never goes stale on the way
# in. A stale-positive is harmless: evaluate_trigger re-checks the DB and
# discard() drops members who no longer qualify.

_watch: dict[int, set[int]] = {}
_lock = threading.Lock()


def warm(db_path, guild_ids) -> None:
    """Seed the watch registry at startup for every enabled guild."""
    with open_db(db_path) as conn:
        seeded: dict[int, set[int]] = {}
        for gid in guild_ids:
            if is_enabled(conn, gid):
                seeded[gid] = watch_candidates(conn, gid)
    with _lock:
        _watch.clear()
        _watch.update(seeded)


def note_pruned(db_path, guild_id: int, user_ids) -> None:
    """Add freshly-pruned members to the watch set (called from the sweep)."""
    _note_many(db_path, guild_id, user_ids)


def note_inactive(db_path, guild_id: int, user_id: int) -> None:
    """Add a freshly inactive-held member to the watch set."""
    _note_many(db_path, guild_id, [user_id])


def _note_many(db_path, guild_id: int, user_ids) -> None:
    ids = {int(u) for u in user_ids}
    if not ids:
        return
    with open_db(db_path) as conn:
        if not is_enabled(conn, guild_id):
            return
    with _lock:
        _watch.setdefault(guild_id, set()).update(ids)


def is_watched(guild_id: int, user_id: int) -> bool:
    """O(1) hot-path test: might this member be owed a review card?"""
    bucket = _watch.get(guild_id)
    return bucket is not None and user_id in bucket


def add_watched(guild_id: int, user_id: int) -> None:
    with _lock:
        _watch.setdefault(guild_id, set()).add(user_id)


def discard(guild_id: int, user_id: int) -> None:
    """Drop a member from the watch set once carded or no longer a candidate."""
    with _lock:
        bucket = _watch.get(guild_id)
        if bucket is not None:
            bucket.discard(user_id)


def _reset_watch_for_tests() -> None:
    with _lock:
        _watch.clear()
