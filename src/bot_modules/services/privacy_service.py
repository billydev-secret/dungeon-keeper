"""Privacy data deletion — DB purge extracted from privacy_cog for testability."""

from __future__ import annotations

import logging
import sqlite3
from itertools import islice

log = logging.getLogger("dungeonkeeper.privacy")

# SQLite's default variable cap is 32,766; stay far below it so the purge can
# never fail on a heavy poster (the accounts most likely to file an erasure
# request are exactly the ones with the most rows — 2026-08 review, A1).
_ID_CHUNK = 500


def _chunks(ids: list[int], size: int = _ID_CHUNK):
    it = iter(ids)
    while chunk := list(islice(it, size)):
        yield chunk


def _delete(
    conn: sqlite3.Connection, sql: str, params: tuple, *, table: str
) -> None:
    """One tolerated delete: schema drift (a table missing on an older guild
    deployment) logs and moves on rather than aborting the erasure midway."""
    try:
        conn.execute(sql, params)
    except sqlite3.Error as exc:
        log.warning("Purge: failed on %s (%s)", table, exc)


def purge_user_data(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    *,
    keep_messages: bool = False,
) -> int:
    """Delete all DB records for *user_id* in *guild_id*. Returns the count of
    message rows that exist for the user (and were removed unless
    *keep_messages* is set).

    NOTE: this is the genuine hard-erasure path and is deliberately **not wired
    to any command** — the ``/delete_me`` and ``/delete_user`` commands only
    clear Discord messages and retain all server-side data for moderation. This
    function is retained for manual/legal (e.g. GDPR) erasure run out-of-band;
    the operator procedure lives in ``docs/gdpr_erasure_runbook.md``.

    *keep_messages*: when True, the messages table and its child tables
    (attachments, mentions, embeds, reactions, sentiment, processed_messages)
    are left untouched. Used by ``/delete_me``: the server retains its own copy
    of the messages for moderation even once the Discord copies are gone. That
    retention is disclosed in the confirmation prompt, before the member
    confirms. Other PII (XP, activity, profile, wellness) is still cleared.

    Only a full erasure reaches this function — a partial ``mode`` scrub skips
    the purge entirely rather than passing flags here.

    Every per-table delete tolerates schema drift (logged warning, sweep
    continues). The caller owns the transaction: run this on one connection
    and commit at the end, so a hard failure rolls the whole erasure back
    instead of leaving partial state.
    """
    msg_ids = [
        r[0]
        for r in conn.execute(
            "SELECT message_id FROM messages WHERE guild_id = ? AND author_id = ?",
            (guild_id, user_id),
        ).fetchall()
    ]

    if msg_ids and not keep_messages:
        # Chunked: one IN (…) per _ID_CHUNK ids, so a heavy poster can never
        # blow SQLite's bound-variable cap mid-erasure.
        for chunk in _chunks(msg_ids):
            ph = ",".join("?" * len(chunk))
            for table in (
                "message_attachments",
                "message_mentions",
                "message_embeds",
                "message_reactions",
                "message_sentiment",
            ):
                _delete(
                    conn,
                    f"DELETE FROM {table} WHERE message_id IN ({ph})",
                    tuple(chunk),
                    table=table,
                )

        _delete(
            conn,
            "DELETE FROM processed_messages WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
            table="processed_messages",
        )
        _delete(
            conn,
            "DELETE FROM messages WHERE guild_id = ? AND author_id = ?",
            (guild_id, user_id),
            table="messages",
        )

    for table in (
        "member_xp",
        "voice_sessions",
        "member_activity",
        "quality_score_leaves",
        "member_gender",
        "member_events",
        # Slash-command and dashboard-panel usage telemetry. Retained
        # indefinitely for reporting (no routine pruning), so this hard-erasure
        # path is the only thing that clears it.
        "usage_events",
        "known_users",
        "xp_events",
        # Added by the 2026-08 review (previously missed — register rows):
        "xp_reaction_awards",
        "member_birthdays",
        "voice_master_profiles",
        "bios",
        "bio_answers",
        "bio_field_values",
    ):
        _delete(
            conn,
            f"DELETE FROM {table} WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
            table=table,
        )

    # Anonymous-features audit trail. Keyed on actor_id/target_id rather than
    # user_id, so it needs its own statements. Routinely pruned by the
    # retention sweep (default 90 days), but a hard-erasure request must not
    # have to wait for that — these rows are precisely the deanonymising ones.
    for col in ("actor_id", "target_id"):
        _delete(
            conn,
            f"DELETE FROM anon_audit_log WHERE guild_id = ? AND {col} = ?",
            (guild_id, user_id),
            table=f"anon_audit_log.{col}",
        )
    _delete(
        conn,
        "DELETE FROM role_events WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
        table="role_events",
    )

    # Pair tables: clear whichever side the erased user is on.
    for table, col_a, col_b in (
        ("user_interactions", "from_user_id", "to_user_id"),
        ("user_interactions_log", "from_user_id", "to_user_id"),
        ("watched_users", "watched_user_id", "watcher_user_id"),
        ("voice_master_trusted", "owner_id", "target_id"),
        ("invite_edges", "inviter_id", "invitee_id"),
    ):
        for col in (col_a, col_b):
            _delete(
                conn,
                f"DELETE FROM {table} WHERE guild_id = ? AND {col} = ?",
                (guild_id, user_id),
                table=f"{table}.{col}",
            )

    for table in (
        "wellness_users",
        "wellness_caps",
        "wellness_cap_counters",
        "wellness_cap_overages",
        "wellness_blackouts",
        "wellness_blackout_overages",
        "wellness_blackout_active",
        "wellness_slow_mode",
        "wellness_streaks",
        "wellness_streak_history",
        "wellness_away_rate_limit",
        "wellness_weekly_reports",
    ):
        _delete(
            conn,
            f"DELETE FROM {table} WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
            table=table,
        )

    for col in ("user_a", "user_b"):
        _delete(
            conn,
            f"DELETE FROM wellness_partners WHERE guild_id = ? AND {col} = ?",
            (guild_id, user_id),
            table=f"wellness_partners.{col}",
        )

    # Economy + casino per-member state (the ledger is deliberately kept —
    # see economy_service._PURGE_USER_ID_TABLES for the list and the rule).
    from bot_modules.services.economy_service import econ_purge_user

    econ_purge_user(conn, guild_id, user_id)

    return len(msg_ids)
