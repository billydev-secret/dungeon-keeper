"""Privacy data deletion — DB purge extracted from privacy_cog for testability."""

from __future__ import annotations

import logging
import json
import sqlite3
from itertools import islice

from bot_modules.services.economy_service import (
    apply_debit as econ_apply_debit,
    get_balance as econ_get_balance,
)
from bot_modules.services.economy_wager_service import (
    refund_game as wager_refund_game,
)

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
        # The daily aggregate of xp_events (migration 186). Purged with its
        # source rather than preserved: it is the same per-member XP, only
        # summed, so leaving it would keep an erased member's activity
        # readable through the readers that will union it
        # (docs/plans/xp-events-retention-and-rollup.md).
        "xp_daily",
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
        # Pen Pals opt-out (migration 174). A bare preference — the state
        # behind a Leave Pool press — held in its own table only because the
        # pool row it belongs to keeps being deleted. Nothing but the requeue
        # paths and _eligible_pool reads it, and an erasure means the member
        # wants their record gone, not curated. Note this leaves an erased
        # member's preserved session able to re-pool them at expiry (the pool
        # and session rows survive the purge by an older decision, register
        # row above) — accepted: an erasure is an out-of-band operator act,
        # and one Leave press puts the flag back.
        "pen_pals_optouts",
        # ── Added by the 2026-09-02 GDPR review ──────────────────────────
        # Found by `scripts/privacy_coverage.py`, which reads a live database
        # and reports every table whose column *values* are real member ids.
        # Each of these named a member and had neither a purge statement nor a
        # register row explaining a preserve, which is the combination the
        # register exists to make impossible. Register rows land with them.
        #
        # Who starred a message and who was starred. The starboard post itself
        # is an ordinary Discord message the purge cannot reach, the same
        # documented limit as a published Flash Theme announcement.
        "starboard_reactors",
        # Members held back from, or exempted from, an inactivity sweep. Bare
        # preferences with no Art 17(3) ground; note the pen_pals_optouts
        # consequence applies here too — erasing the exemption exposes the
        # member to the next sweep, and re-adding it is one mod action.
        "inactivity_prune_exceptions",
        "inactive_sweep_exemptions",
        # Which roles the member picked from a role menu, and the grant log.
        "role_menu_grants",
        # The level-5 congratulation card. Part of the XP family, which is
        # purged in full.
        "xp_level_5_cards",
        # Queued "this member is ready for promotion" posts.
        "pending_promotion_posts",
        # That the member's birthday was announced on a given date — the
        # dedup marker behind `member_birthdays`, which is already purged.
        "birthday_announcements",
        # Which member bumped the server on a listing site.
        "bump_tracker_log",
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

    # Ping Response (migration 198): the erased member's own role pings.
    # Purged, not preserved — this is an analytics table about how a ping
    # landed, and "we wanted the numbers" is not an Art 17(3) ground. Their
    # pings drop out of the report; everyone else's turnout is unaffected,
    # since turnout is counted from messages and reactions at read time and
    # never denormalized onto the ping row.
    _delete(
        conn,
        "DELETE FROM ping_events WHERE guild_id = ? AND author_id = ?",
        (guild_id, user_id),
        table="ping_events",
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

    # Meadow Mahjong (migration 175; register rows in docs/data_register.md).
    # A LIVE seat can't just be deleted — the table dies with it: close the
    # table (the service's next timer fire sees status != 'live' and stands
    # down), free every seat, and refund all escrow still held for it, so the
    # other players get their coins back rather than a wedged hand. Escrow
    # rides econ_game_wagers at game_id = table_id·100000 + hand_no, so the
    # whole range is swept rather than trusting the state JSON to parse.
    try:
        live_tables = [
            int(r[0])
            for r in conn.execute(
                "SELECT DISTINCT t.id FROM mahjong_tables t "
                "JOIN mahjong_seats s ON s.table_id = t.id "
                "WHERE t.status = 'live' AND s.guild_id = ? AND s.user_id = ? "
                "AND s.live = 1",
                (guild_id, user_id),
            ).fetchall()
        ]
        for table_id in live_tables:
            for r in conn.execute(
                "SELECT DISTINCT game_id FROM econ_game_wagers "
                "WHERE game_type = 'mahjong' AND state = 'held' "
                "AND game_id >= ? AND game_id < ?",
                (table_id * 100_000, (table_id + 1) * 100_000),
            ).fetchall():
                wager_refund_game(conn, "mahjong", int(r[0]))
            conn.execute(
                "UPDATE mahjong_tables SET status = 'closed', "
                "closed_reason = 'purged', deadline_at = NULL WHERE id = ?",
                (table_id,),
            )
            conn.execute(
                "UPDATE mahjong_seats SET live = 0 WHERE table_id = ?",
                (table_id,),
            )
            # a fill table's house bots just got their escrow refunded —
            # burn the synthetic wallets back so a purge can't strand
            # house coins (bots plan B6; negative user_id = house bot)
            for r in conn.execute(
                "SELECT DISTINCT user_id FROM mahjong_seats "
                "WHERE table_id = ? AND user_id < 0", (table_id,),
            ).fetchall():
                bot_id = int(r[0])
                balance = econ_get_balance(conn, guild_id, bot_id)
                if balance > 0:
                    econ_apply_debit(
                        conn, guild_id, bot_id, balance,
                        "mahjong_house_settle", meta={"table_id": table_id},
                    )
    except sqlite3.Error as exc:
        log.warning("Purge: failed dissolving mahjong tables (%s)", exc)

    # The member's own rows go; results they WON are anonymised instead of
    # deleted (winner_id → NULL) because the row also carries the other
    # seats' hand history via mahjong_result_seats.
    for table in (
        "mahjong_seats",
        "mahjong_result_seats",
        "mahjong_stats",
        "mahjong_prefs",
    ):
        _delete(
            conn,
            f"DELETE FROM {table} WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
            table=table,
        )
    _scrub(
        conn,
        "UPDATE mahjong_results SET winner_id = NULL "
        "WHERE guild_id = ? AND winner_id = ?",
        (guild_id, user_id),
        table="mahjong_results.winner_id",
    )

    # ── The five decisions the 2026-09-02 GDPR review left open, settled by
    # Billy on 2026-09-02. Each register row carries the reasoning.

    # Quote cards. A row says "X quoted Y's message" and names two members, so
    # it goes whole from either side — with one of them removed it records
    # nothing coherent. Decided as purge rather than preserve: it is named an
    # audit log, but it logs a decorative feature, not a moderation decision,
    # so the sanctions ground does not reach it.
    _delete(
        conn,
        "DELETE FROM quote_audit_log WHERE guild_id = ? AND "
        "(quoter_id = ? OR quoted_user_id = ?)",
        (guild_id, user_id, user_id),
        table="quote_audit_log",
    )

    # Inactivity records and promotion review cards each name a member in two
    # different roles, and the two roles get different treatment — the same
    # split `todos` makes.
    #
    # As the SUBJECT (`user_id`), the row is a record about them and goes.
    # As the ACTOR (the `moderator_id` who set them inactive, the
    # `resolved_by` who closed the card), the row belongs to somebody else:
    # deleting it would take an unrelated member's inactivity record or review
    # card away because a mod asked to be erased. So the actor id is blanked
    # and the row stands — `moderator_id` to 0 (its own NOT NULL default) and
    # `resolved_by` to NULL, in both cases the "unknown member" every surface
    # already renders.
    for table in ("inactive_members", "promotion_review_cards"):
        _delete(
            conn,
            f"DELETE FROM {table} WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
            table=table,
        )
    _scrub(
        conn,
        "UPDATE inactive_members SET moderator_id = 0 "
        "WHERE guild_id = ? AND moderator_id = ?",
        (guild_id, user_id),
        table="inactive_members.moderator_id",
    )
    _scrub(
        conn,
        "UPDATE promotion_review_cards SET resolved_by = NULL "
        "WHERE guild_id = ? AND resolved_by = ?",
        (guild_id, user_id),
        table="promotion_review_cards.resolved_by",
    )

    # QA tracker. The verdict is the member's own — their pass/fail call, the
    # note they wrote, what they were paid — and goes. The test itself is the
    # project's work product like a `todos` row, so the signature on it is
    # blanked rather than the test deleted: removing it would take a recorded
    # sign-off off the board because the tester left. `voided_by` names a
    # different mod on someone else's verdict and is blanked for the same
    # reason. The coins already paid live on in the preserved `econ_ledger`.
    _delete(
        conn,
        "DELETE FROM qa_verdicts WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
        table="qa_verdicts",
    )
    _scrub(
        conn,
        "UPDATE qa_verdicts SET voided_by = NULL "
        "WHERE guild_id = ? AND voided_by = ?",
        (guild_id, user_id),
        table="qa_verdicts.voided_by",
    )
    _scrub(
        conn,
        "UPDATE qa_tests SET verified_by = NULL "
        "WHERE guild_id = ? AND verified_by = ?",
        (guild_id, user_id),
        table="qa_tests.verified_by",
    )

    # Voice Master per-room block lists — ASYMMETRIC, deliberately.
    #
    # The member's OWN list goes: it is their preference about their own room,
    # with no ground to outlive them. Rows where they are the `target_id` are
    # KEPT: that entry is somebody else's protection, and erasing it at the
    # request of the person it excludes is exactly what Art 17(3)'s "rights of
    # others" carve-out exists to prevent — the `no_contact_pairs` ground.
    #
    # This is neither the symmetric delete `voice_master_trusted` does nor a
    # blanket preserve, so it is a deliberate call rather than an oversight:
    # a trust list confers something and can be dropped freely, a block list
    # withholds something and cannot.
    _delete(
        conn,
        "DELETE FROM voice_master_blocked WHERE guild_id = ? AND owner_id = ?",
        (guild_id, user_id),
        table="voice_master_blocked.owner_id",
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
        # 2026-09-02 GDPR review. A duel row names exactly two members and
        # means nothing with one of them removed, so it goes whole from either
        # side — the same call the Risky Rolls rounds made.
        ("duel_cooldowns", "player_a", "player_b"),
        ("duel_nicks", "loser_id", "winner_id"),
        # Who tipped whom. The coins live on in the preserved `econ_ledger`.
        ("reaction_tip_awards", "user_id", "author_id"),
    ):
        for col in (col_a, col_b):
            _delete(
                conn,
                f"DELETE FROM {table} WHERE guild_id = ? AND {col} = ?",
                (guild_id, user_id),
                table=f"{table}.{col}",
            )

    # Confessions held by mod-approve mode (migration 200). Deleted outright,
    # and it is the one confessions table that is: `confession_threads` relies
    # on its seven-day TTL because it holds routing metadata, but a pending row
    # holds the member's confession *text*, and it is queued to be published.
    # Erasing somebody and then posting what they wrote — because a moderator
    # got to the queue before the sweep did — is not a defensible outcome, so
    # the row goes now. Nothing is announced to the mods. Note this path cannot
    # repaint the board — erasure is run out-of-band, with no bot in hand, and
    # the board's own loop only repaints guilds where a recurring chore spawned
    # — so a clipped copy of the confession stays rendered in the sticky embed
    # until something else moves it. The stored row is gone either way, which is
    # what the erasure owes; the stale pixels are a display lag, and pressing
    # the board's Confessions button will already find nothing there.
    _delete(
        conn,
        "DELETE FROM confession_pending WHERE guild_id = ? AND author_id = ?",
        (guild_id, user_id),
        table="confession_pending",
    )

    # ── 2026-09-02 GDPR review: tables whose member column is not `user_id`,
    # or which are not guild-scoped and must reach their guild through a
    # parent. Same schema-drift tolerance as every step above.
    for table, column in (
        # Who was starred. The starboard message in Discord is an ordinary
        # message the purge cannot reach.
        ("starboard_posts", "author_id"),
        ("duel_group_cooldowns", "player_id"),
        # A live temporary voice room owned by the member. Live state, not
        # history — the room itself is torn down by the voice cog.
        ("voice_master_channels", "owner_id"),
        # LegitLibs: a template and a revision are the member's own writing.
        ("legitlibs_templates", "author_id"),
        # Auto-react bookkeeping naming whose message was reacted to.
        ("auto_react_placements", "author_id"),
    ):
        _delete(
            conn,
            f"DELETE FROM {table} WHERE guild_id = ? AND {column} = ?",
            (guild_id, user_id),
            table=f"{table}.{column}",
        )

    # `foolsday_exclusions` is created by application code at first use
    # (`foolsday_service`, a CREATE TABLE IF NOT EXISTS) rather than by a
    # migration, so it does not exist in a freshly-migrated schema and the
    # migration-based register gate cannot see it. It holds members who opted
    # out of the April Fools nickname prank — a bare preference with no
    # Art 17(3) ground. Erasing it re-exposes the member to next year's prank,
    # the accepted `pen_pals_optouts` consequence; one opt-out press restores
    # it. The `_delete` drift tolerance covers the table's absence.
    _delete(
        conn,
        "DELETE FROM foolsday_exclusions WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
        table="foolsday_exclusions",
    )

    # Not guild-scoped: each reaches its guild through a parent row, the same
    # shape `survivor_*` uses. Scoping matters — a bare `user_id` match would
    # delete the member's rows in every guild the bot serves, not just this one.
    for table, column, parent, parent_key, child_key in (
        ("role_menu_bindings", "user_id", "role_menus", "id", "menu_id"),
        ("policy_votes", "user_id", "policy_tickets", "id", "policy_id"),
        ("legitlibs_revisions", "editor_id", "legitlibs_templates",
         "template_id", "template_id"),
    ):
        _delete(
            conn,
            f"DELETE FROM {table} WHERE {column} = ? AND {child_key} IN "
            f"(SELECT {parent_key} FROM {parent} WHERE guild_id = ?)",
            (user_id, guild_id),
            table=f"{table}.{column}",
        )

    # `legitlibs_reports` keys on `game_id`, which belongs to an in-memory
    # game rather than a table, so there is no parent to scope through. The
    # report names its reporter and nothing else about the guild; it is
    # deleted across guilds rather than left standing, since a report is a
    # small row whose only personal datum is the id being erased.
    _delete(
        conn,
        "DELETE FROM legitlibs_reports WHERE reporter_id = ?",
        (user_id,),
        table="legitlibs_reports.reporter_id",
    )

    # A group Hot Potato row names its host, holder, winner and loser in
    # columns and its whole roster in JSON lists (`roster`, `alive`,
    # `elimination_order`, `pass_log`) — a list-valued blind spot. The row goes
    # whole from any named seat: a party game with a hole in it is incoherent
    # state the cog would try to re-attach a view to on the next boot, and the
    # alternative is retaining the id inside the JSON.
    _delete(
        conn,
        "DELETE FROM hp_group_games WHERE guild_id = ? AND ("
        "host_id = ? OR holder_id = ? OR winner_id = ? OR loser_id = ? "
        "OR (',' || COALESCE(roster, '') || ',') LIKE ? "
        "OR (',' || COALESCE(alive, '') || ',') LIKE ?)",
        (guild_id, user_id, user_id, user_id, user_id,
         *([f"%,{user_id},%"] * 2)),
        table="hp_group_games",
    )

    # Wellness counter children, deleted BEFORE their parents — they key on
    # `cap_id`/`blackout_id` and have no `guild_id` or `user_id` of their own,
    # so once the parent row is gone there is nothing left to find them by.
    #
    # All three were listed in the plain `guild_id`+`user_id` sweep below from
    # the day they shipped, where every erasure failed on them with "no such
    # column: guild_id", logged a warning and moved on — the register recorded
    # the wellness family as fully purged throughout (2026-09-02 GDPR review).
    # Production holds no rows in any of the three, so nothing was retained in
    # error; the defect was that the erasure could not have cleared them.
    for table, child_key, parent, parent_key in (
        ("wellness_cap_counters", "cap_id", "wellness_caps", "id"),
        ("wellness_cap_overages", "cap_id", "wellness_caps", "id"),
        ("wellness_blackout_overages", "blackout_id", "wellness_blackouts", "id"),
    ):
        _delete(
            conn,
            f"DELETE FROM {table} WHERE {child_key} IN "
            f"(SELECT {parent_key} FROM {parent} "
            f"WHERE guild_id = ? AND user_id = ?)",
            (guild_id, user_id),
            table=table,
        )

    for table in (
        "wellness_users",
        "wellness_caps",
        "wellness_blackouts",
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
#
# The convention is only as good as the sweep that checks it, which is what
# ``scripts/privacy_coverage.py`` does: it reads a live database and reports
# every column whose values are real member ids but whose name is not in here.
# The 2026-09-02 GDPR review ran it against production and added seven such
# names — ``hidden_by``, ``labeled_by``, ``player_a``/``player_b``,
# ``posted_by``, ``updated_by`` and ``verified_by`` — each of which named a
# member in every table carrying it while being invisible to an access request.
# A second pass added four more the same way: ``active_player``,
# ``answer_id`` (the member a Guess round is *about*), ``closed_by`` and
# ``resolved_by``. The last three matter even where the table was already
# exported through some other column: a member who only ever *closed* a
# ticket or *resolved* an intake card matched nothing, so their rows were
# silently absent from an answer that looked complete.
SUBJECT_ID_COLUMNS = frozenset(
    {
        "active_player", "actor_id", "added_by", "answer_id", "approved_by",
        "asker_id", "author_id",
        "beneficiary_id", "blocked_user_id", "challenger_id", "claimed_by",
        "claimer_id", "closed_by", "completed_by", "created_by", "creator_id",
        "done_by",
        "editor_id",
        # grant_role_permissions (and its dead prod-only predecessor
        # give_role_permissions) name a member as "entity_id" alongside an
        # entity_type of 'user' or 'role' — a role id can never collide with a
        # member id, so matching on it is safe and it is the only way those
        # keeper allow-lists reach an access export at all.
        "entity_id",
        "extra_questioner_id", "from_user_id", "guessed_id", "guessed_user_id",
        "guesser_id", "hidden_by", "high_bidder_id", "highest_user",
        "holder_id", "host_id",
        "invitee_id", "inviter_id", "labeled_by", "last_winner_id", "loser_id",
        "lowest_user",
        "member_id", "mod_id", "moderator_id", "opener_id", "original_author_id",
        "owner_id", "partner_id", "player_a", "player_b", "player_id",
        "posted_by", "poster_id", "protected_user_id",
        "quoted_id", "quoted_user_id", "quoter_id", "reactor_id", "recipient_id",
        "replier_id", "reporter_id", "requester_id", "resolved_by",
        "resolver_id",
        "reviewed_by",
        "second_highest_user", "second_lowest_user", "sender_id", "set_by",
        "solver_id", "sponsor_user_id", "subject_id", "submitter_id",
        "target_author_id", "target_id", "to_user_id", "updated_by",
        "updated_by_user_id",
        "user1_id", "user2_id", "user_a", "user_a_id", "user_b", "user_b_id",
        "user_high", "user_id", "user_low", "verified_by", "voided_by",
        "voter_id",
        "watched_user_id", "watcher_user_id", "winner_id",
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
    # A group Hot Potato row keeps its whole roster in JSON/CSV lists. The
    # purge matches `roster` and `alive` by exact membership and takes the row
    # whole; the export cannot match inside a list, so the gap is disclosed
    # here (2026-09-02 GDPR review).
    ("hp_group_games", "alive"),
    ("hp_group_games", "elimination_order"),
    ("hp_group_games", "pass_log"),
    ("hp_group_games", "roster"),
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
        "mahjong_tables",
        "tickets", "user_interactions", "user_interactions_log", "warnings",
        "watched_users", "whisper_guesses", "whisper_replies",
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
