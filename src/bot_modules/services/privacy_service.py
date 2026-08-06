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

# Children of ``messages``, keyed on message_id rather than on the author. They
# carry no subject column of their own, so the export's column-discovery pass
# cannot find them — both the purge and the export reach them by joining
# through the author's message ids. Shared so the two can never drift.
_MESSAGE_CHILD_TABLES = (
    "message_attachments",
    "message_mentions",
    "message_embeds",
    "message_reactions",
    "message_sentiment",
)


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
    the operator procedure lives in ``docs/gdpr_runbook.md``.

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
            for table in _MESSAGE_CHILD_TABLES:
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


# ── Subject access / portability (GDPR Art 15 + Art 20) ──────────────────────

# Column names that identify a member. The export finds a subject's rows by
# intersecting each table's columns with this set rather than from a curated
# table list: the curated list is what goes stale, and a stale access export is
# an incomplete answer to a statutory request. A new feature's table is covered
# the day it lands, provided it names its member column conventionally.
SUBJECT_ID_COLUMNS = frozenset(
    {
        "actor_id", "added_by", "approved_by", "asker_id", "author_id",
        "beneficiary_id", "blocked_user_id", "challenger_id", "claimed_by",
        "claimer_id", "created_by", "creator_id", "done_by", "editor_id",
        "extra_questioner_id", "from_user_id", "guessed_id", "guessed_user_id",
        "guesser_id", "high_bidder_id", "highest_user", "holder_id", "host_id",
        "invitee_id", "inviter_id", "last_winner_id", "loser_id", "lowest_user",
        "member_id", "mod_id", "moderator_id", "opener_id", "original_author_id",
        "owner_id", "partner_id", "player_id", "poster_id", "protected_user_id",
        "quoted_id", "quoted_user_id", "quoter_id", "reactor_id", "recipient_id",
        "replier_id", "reporter_id", "requester_id", "resolver_id",
        "second_highest_user", "second_lowest_user", "sender_id", "set_by",
        "solver_id", "sponsor_user_id", "subject_id", "submitter_id",
        "target_author_id", "target_id", "to_user_id", "updated_by_user_id",
        "user1_id", "user2_id", "user_a", "user_a_id", "user_b", "user_b_id",
        "user_high", "user_id", "user_low", "voter_id", "watched_user_id",
        "watcher_user_id", "winner_id",
    }
)

# Known blind spot: a handful of columns store a *list* of member ids as JSON or
# CSV (``econ_demurrage_sweeps.taxed_members``,
# ``risky_pending_questions.participant_user_ids`` / ``lowest_tie_user_ids``,
# ``risky_active_rounds.reroll_user_ids``, ``confession_config.blocked_user_ids``,
# ``revive_events.follow_authors``). A subject inside one of those lists is not
# found by an equality match and will not appear in the export. The volumes are
# small and the content is incidental, but it is a gap, not an absence — the
# runbook tells the operator to grep them by hand.
LIST_VALUED_MEMBER_COLUMNS = (
    ("confession_config", "blocked_user_ids"),
    ("econ_demurrage_sweeps", "taxed_members"),
    ("revive_events", "follow_authors"),
    ("risky_active_rounds", "reroll_user_ids"),
    ("risky_pending_questions", "lowest_tie_user_ids"),
    ("risky_pending_questions", "participant_user_ids"),
)

# Tables whose rows name a *second* member, where that person's identity is the
# payload rather than incidental. The subject is entitled to their own data, but
# Art 15(4) says an access request must not adversely affect others' rights — so
# these are surfaced for operator review before disclosure rather than silently
# included or silently dropped. Redacting automatically would corrupt the
# record; deciding for the operator would hide the tension.
THIRD_PARTY_TABLES = frozenset(
    {
        "anon_audit_log", "audit_log", "confession_threads", "dm_audit_log",
        "dm_consent_pairs", "dm_requests", "econ_msg_replies", "guess_audit_log",
        "guess_guesses", "guess_rounds", "inactive_members", "invite_edges",
        "jails", "no_contact_events", "no_contact_pairs", "pen_pals_blocks",
        "pen_pals_sessions", "quote_audit_log", "reaction_log",
        "reaction_tip_awards", "rules_events", "rules_ledger", "starboard_reactors",
        "tickets", "user_interactions", "user_interactions_log", "warnings",
        "watched_users", "wellness_partners", "whisper_guesses", "whisper_replies",
        "whisper_reports", "whispers",
    }
)


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def export_user_data(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> dict:
    """Collect every row naming *user_id* into a JSON-ready dict.

    This is the read half of the erasure path — the answer to a subject access
    request (Art 15) and a portability request (Art 20). The operator procedure
    is in ``docs/gdpr_runbook.md``; ``scripts/export_user_data.py`` is
    the CLI.

    **The export is deliberately a superset of the purge.** ``purge_user_data``
    skips categories the server keeps under Art 17(3) — the ``econ_ledger``
    double-entry record, sanction history, consent audit, no-contact orders.
    Retaining data does not exempt it from disclosure, so those rows are
    exported even though they are never deleted. Anything scoped narrower than
    "every table naming this member" would answer the wrong question.

    Read-only: opens no transaction and writes nothing.

    Returns ``{"subject", "tables", "counts", "review_required", "notes"}``.
    ``tables`` maps table name → ``{"matched_columns", "guild_scoped", "rows"}``.
    ``review_required`` lists the tables that name a second member.
    """
    tables: dict[str, dict] = {}
    notes: list[str] = []

    names = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]

    for table in names:
        if table in _MESSAGE_CHILD_TABLES:
            continue  # reached below, through the author's message ids
        try:
            cols = _table_columns(conn, table)
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            log.warning("Export: cannot introspect %s (%s)", table, exc)
            continue

        matched = sorted(set(cols) & SUBJECT_ID_COLUMNS)
        if not matched:
            continue

        guild_scoped = "guild_id" in cols
        where = " OR ".join(f'"{c}" = ?' for c in matched)
        params: list[int] = [user_id] * len(matched)
        sql = f'SELECT * FROM "{table}" WHERE ({where})'
        if guild_scoped:
            sql += " AND guild_id = ?"
            params.append(guild_id)

        try:
            cur = conn.execute(sql, tuple(params))
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        except sqlite3.Error as exc:
            # Same schema-drift tolerance as the purge: one unreadable table
            # must not cost the subject the rest of their answer.
            log.warning("Export: failed on %s (%s)", table, exc)
            notes.append(f"{table}: unreadable ({exc})")
            continue

        if rows and not guild_scoped:
            # Only worth saying when it actually affects this subject's answer.
            notes.append(
                f"{table}: has no guild_id — its rows span every guild, not just {guild_id}"
            )
        if rows:
            tables[table] = {
                "matched_columns": matched,
                "guild_scoped": guild_scoped,
                "rows": rows,
            }

    _export_message_children(conn, guild_id, user_id, tables)

    present = {
        f"{t}.{c}"
        for t, c in LIST_VALUED_MEMBER_COLUMNS
        if t in {n for n in names}
    }
    if present:
        notes.append(
            "list-valued member columns are not searchable by equality and are "
            "NOT covered by this export — grep them by hand: "
            + ", ".join(sorted(present))
        )

    return {
        "subject": {"guild_id": guild_id, "user_id": user_id},
        "tables": tables,
        "counts": {t: len(v["rows"]) for t, v in sorted(tables.items())},
        "review_required": sorted(set(tables) & THIRD_PARTY_TABLES),
        "notes": notes,
    }


def _export_message_children(
    conn: sqlite3.Connection, guild_id: int, user_id: int, tables: dict
) -> None:
    """Add the message children, joined through the author's message ids.

    Chunked exactly as the purge is: a heavy poster's id list would otherwise
    blow SQLite's bound-variable cap, and the accounts most likely to file an
    access request are the ones with the most rows.
    """
    msg_ids = [
        r[0]
        for r in conn.execute(
            "SELECT message_id FROM messages WHERE guild_id = ? AND author_id = ?",
            (guild_id, user_id),
        ).fetchall()
    ]
    if not msg_ids:
        return

    for table in _MESSAGE_CHILD_TABLES:
        try:
            cols = _table_columns(conn, table)
        except sqlite3.Error:  # pragma: no cover - defensive
            continue
        if not cols:
            continue
        rows: list[dict] = []
        try:
            for chunk in _chunks(msg_ids):
                ph = ",".join("?" * len(chunk))
                cur = conn.execute(
                    f'SELECT * FROM "{table}" WHERE message_id IN ({ph})',
                    tuple(chunk),
                )
                rows.extend(dict(zip(cols, r)) for r in cur.fetchall())
        except sqlite3.Error as exc:
            log.warning("Export: failed on %s (%s)", table, exc)
            continue
        if rows:
            tables[table] = {
                "matched_columns": ["message_id (via messages.author_id)"],
                "guild_scoped": True,
                "rows": rows,
            }
