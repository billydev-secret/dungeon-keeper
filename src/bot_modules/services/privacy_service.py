"""Privacy data deletion — DB purge extracted from privacy_cog for testability."""

from __future__ import annotations

import logging
import json
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


def _scrub(
    conn: sqlite3.Connection, sql: str, params: tuple, *, table: str
) -> None:
    """One tolerated anonymising UPDATE.

    Same schema-drift tolerance as ``_delete``; separate name because erasing a
    member *from* a shared row is a different act from removing the row, and a
    reader scanning this file for what the purge deletes should not have to
    read the SQL to find out that these two are not deletions.
    """
    _delete(conn, sql, params, table=table)


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
        # Guess consent evidence. Kept through an optout (the record that
        # consent was held is the point), but a full erasure clears it.
        "guess_consents",
        # Pen Pals pool movement (migration 160). Operational history — who
        # joined the matching pool and why they left it. Unlike
        # pen_pals_sessions (the no-repeat memory) and pen_pals_blocks (a
        # protective record), nothing reads this back, so there is no ground
        # to hold it against an erasure request.
        "pen_pals_pool_events",
        # Pen Pals opt-out (migration 173). A bare preference — "don't match
        # me" — that nothing but the requeue paths reads, and an erasure also
        # clears pen_pals_pool, so there is no state left for it to protect.
        "pen_pals_optouts",
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

    # Risky Rolls (migration 019): every row naming the member goes.
    #
    # These are ephemeral party-game rows — a round deletes itself on close and
    # a posted question is swept at 7 days — and nothing downstream reads them
    # back, so there is no Art 17(3) ground to hold one against an erasure.
    # Rows are deleted WHOLE rather than scrubbed: a round whose winner no
    # longer exists, or a question whose asker was erased, is incoherent state
    # that the cog would then try to re-attach a view to on next boot. That
    # does take an in-flight round away from the other players in it; erasure
    # is rare, out-of-band, and the alternative is retaining the id.
    #
    # `risky_round_rolls` has no `guild_id` and reaches its round by FK, so the
    # game ids are resolved first: this connection is the caller's and does not
    # promise `PRAGMA foreign_keys = ON` (the feature's own store sets it), so
    # the cascade cannot be relied on to take the rolls with the round.
    try:
        risky_game_ids = [
            r[0]
            for r in conn.execute(
                "SELECT game_id FROM risky_active_rounds WHERE guild_id = ? AND ("
                "opener_id = ? OR highest_user = ? OR lowest_user = ? OR "
                "second_lowest_user = ? OR second_highest_user = ? OR game_id IN "
                "(SELECT game_id FROM risky_round_rolls WHERE user_id = ?))",
                (guild_id, user_id, user_id, user_id, user_id, user_id, user_id),
            ).fetchall()
        ]
    except sqlite3.Error as exc:
        log.warning("Purge: failed on risky_active_rounds lookup (%s)", exc)
        risky_game_ids = []

    if risky_game_ids:
        ph = ",".join("?" for _ in risky_game_ids)
        _delete(
            conn,
            f"DELETE FROM risky_round_rolls WHERE game_id IN ({ph})",
            tuple(risky_game_ids),
            table="risky_round_rolls",
        )
        _delete(
            conn,
            f"DELETE FROM risky_active_rounds WHERE game_id IN ({ph})",
            tuple(risky_game_ids),
            table="risky_active_rounds",
        )

    # The CSV columns need exact membership, not a bare LIKE: '%123%' also
    # matches 1234. Wrapping both sides in commas makes ',123,' the needle.
    _delete(
        conn,
        "DELETE FROM risky_pending_questions WHERE guild_id = ? AND ("
        "winner_id = ? OR extra_questioner_id = ? "
        "OR (',' || COALESCE(participant_user_ids, '') || ',') LIKE ? "
        "OR (',' || COALESCE(lowest_tie_user_ids, '') || ',') LIKE ? "
        "OR (',' || COALESCE(questioners_asked, '') || ',') LIKE ?)",
        (guild_id, user_id, user_id, *([f"%,{user_id},%"] * 3)),
        table="risky_pending_questions",
    )

    # question_text is the member's own words, so a posted question they asked
    # goes with them; one they were merely a recipient of names them in
    # allowed_replier_ids and goes too.
    _delete(
        conn,
        "DELETE FROM risky_posted_questions WHERE guild_id = ? AND ("
        "asker_id = ? "
        "OR (',' || COALESCE(allowed_replier_ids, '') || ',') LIKE ?)",
        (guild_id, user_id, f"%,{user_id},%"),
        table="risky_posted_questions",
    )

    # Survivor (migration 167): purged across every season, live or archived
    # — a game record has no Art 17(3) ground to outlive the member (register
    # rows: docs/data_register.md). guild_id is denormalized onto both tables
    # (so the export's standard guild scoping covers them too); coin
    # movements live in econ_ledger, which is preserved separately.
    for table in ("survivor_picks", "survivor_players"):
        _delete(
            conn,
            f"DELETE FROM {table} WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
            table=table,
        )

    # Shared todo list — ANONYMISED, NOT DELETED, and deliberately so.
    #
    # A todos row is two different things at once: the task text, which is the
    # mod team's shared work product and belongs to the server, and the two
    # Discord ids naming who added it and who ticked it, which are the member's
    # personal data. Deleting the row to erase the ids would take real
    # outstanding work off other people's list — a task someone else is
    # part-way through vanishing because an unrelated member left. Clearing the
    # ids erases everything that identifies a person while leaving the work
    # standing, which is the minimisation the erasure right actually asks for.
    #
    # `added_by` becomes 0 and `completed_by` NULL — the same "unknown" the
    # board and the dashboard already render for an unresolvable member, so no
    # surface needs to learn a new state. `missed_at` rows name nobody by
    # definition and are untouched.
    for col, blank in (("added_by", 0), ("completed_by", None)):
        _scrub(
            conn,
            f"UPDATE todos SET {col} = ? WHERE guild_id = ? AND {col} = ?",
            (blank, guild_id, user_id),
            table=f"todos.{col}",
        )

    # …and the recurring definitions behind them, or the erasure undoes itself.
    # `_spawn_one` stamps the definition's `created_by` onto every row it
    # materialises, so a member who set up "Post QOTD" and then asked to be
    # erased would have their id written straight back into `todos.added_by` at
    # the next fire, and every day after. Blanking the definition is what makes
    # the scrub above durable rather than a one-off.
    _scrub(
        conn,
        "UPDATE todo_recurring SET created_by = 0 WHERE guild_id = ? AND created_by = ?",
        (guild_id, user_id),
        table="todo_recurring.created_by",
    )

    # Mention Awards: a `from_user` condition chip names the member inside the
    # rule's conditions JSON — the list-column blind spot, so SUBJECT_ID_COLUMNS
    # can't see it. Strip the member's chips; a rule left with no chips is
    # deleted outright (an empty chip list is the matcher's fail-closed
    # "matches nothing" state, and a husk that existed only because of the
    # erased member serves nobody). Other chips, and other members' chips,
    # survive. Tolerates schema drift like every other step.
    try:
        needle = str(user_id)
        rows = conn.execute(
            "SELECT id, conditions FROM mention_award_rules "
            "WHERE guild_id = ? AND conditions LIKE ?",
            (guild_id, f'%"{needle}"%'),
        ).fetchall()
        for rule_id, raw in rows:
            try:
                chips = json.loads(raw or "[]")
            except json.JSONDecodeError:
                continue
            kept = [
                c for c in chips
                if not (
                    isinstance(c, dict)
                    and c.get("kind") == "from_user"
                    and str(c.get("value", "")) == needle
                )
            ]
            if len(kept) == len(chips):
                continue
            if kept:
                conn.execute(
                    "UPDATE mention_award_rules SET conditions = ? WHERE id = ?",
                    (json.dumps(kept), rule_id),
                )
            else:
                conn.execute(
                    "DELETE FROM mention_award_rules WHERE id = ?", (rule_id,)
                )
    except sqlite3.Error as exc:
        log.warning("Purge: failed on mention_award_rules (%s)", exc)

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

    # Music playlist: `added_by` rows in tracks + unmatched, and reviewer
    # references nulled — the member column is `added_by`, so the generic
    # user_id sweep above can't reach it (register: docs/data_register.md).
    from bot_modules.music_playlist.music_playlist_store import purge_member_rows

    try:
        purge_member_rows(conn, guild_id, user_id)
    except sqlite3.Error as exc:
        log.warning("Purge: failed on music_playlist tables (%s)", exc)

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
        "claimer_id", "completed_by", "created_by", "creator_id", "done_by",
        "editor_id",
        "extra_questioner_id", "from_user_id", "guessed_id", "guessed_user_id",
        "guesser_id", "high_bidder_id", "highest_user", "holder_id", "host_id",
        "invitee_id", "inviter_id", "last_winner_id", "loser_id", "lowest_user",
        "member_id", "mod_id", "moderator_id", "opener_id", "original_author_id",
        "owner_id", "partner_id", "player_id", "poster_id", "protected_user_id",
        "quoted_id", "quoted_user_id", "quoter_id", "reactor_id", "recipient_id",
        "replier_id", "reporter_id", "requester_id", "resolver_id",
        "reviewed_by",
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
# ``risky_pending_questions.participant_user_ids`` / ``lowest_tie_user_ids`` /
# ``questioners_asked``, ``risky_posted_questions.allowed_replier_ids``,
# ``confession_config.blocked_user_ids``, ``revive_events.follow_authors``). A subject inside one of those lists is not
# found by an equality match and will not appear in the export. The volumes are
# small and the content is incidental, but it is a gap, not an absence — the
# runbook tells the operator to grep them by hand.
LIST_VALUED_MEMBER_COLUMNS = (
    ("confession_config", "blocked_user_ids"),
    # from_user chip values live in the conditions JSON (purge strips them;
    # export can only disclose the gap).
    ("mention_award_rules", "conditions"),
    ("econ_demurrage_sweeps", "taxed_members"),
    ("revive_events", "follow_authors"),
    ("risky_pending_questions", "lowest_tie_user_ids"),
    ("risky_pending_questions", "participant_user_ids"),
    ("risky_pending_questions", "questioners_asked"),
    ("risky_posted_questions", "allowed_replier_ids"),
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
