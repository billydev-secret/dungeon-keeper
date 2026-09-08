"""Erase a guild's data once the bot is no longer in it.

Until this module existed, being removed from a server erased almost nothing.
The only leave-time cleanup anywhere in the bot was ``whisper_cog`` deleting
five ``config`` keys; every other row — messages, economy ledgers, moderation
records, game history — stayed in the database indefinitely, keyed to a guild
the bot could no longer see. That is a retention decision nobody took, and
:doc:`../../docs/data_register` had no row for it.

Three shapes of table have to be found, because only the first can be found
automatically:

* **Guild-scoped** (258 of 303 tables) carry a ``guild_id`` column and are
  discovered from the live schema at run time, so a table added next year is
  covered without anyone remembering to list it here.
* **Child tables** (:data:`CHILD_TABLES`, 33) key off a parent row's id and
  carry no ``guild_id`` of their own. They are reachable only through a
  subquery on the parent, built recursively because two of them are a second
  level down (``whisper_reply_reports`` → ``whisper_replies`` → ``whispers``,
  ``doc_placement_messages`` → ``doc_placements`` → ``docs``).
* **Channel-keyed** (:data:`CHANNEL_TABLES`, 2) key off a channel id, and
  nothing in the database maps a channel back to a guild. They can only be
  cleared from the departing guild's cached channel list, which exists for
  exactly as long as the ``on_guild_remove`` handler holds the ``Guild``
  object — so the caller passes those ids in.

Everything else (:data:`GLOBAL_TABLES`, 10) is genuinely bot-wide — the shared
question bank, the migration ledger, the NFL fixture list — and is left alone.
``tests/test_guild_purge_coverage.py`` fails when a table in a fully migrated
schema falls into none of the four buckets, so the classification cannot rot
as the schema grows: a new table either carries ``guild_id`` or someone has to
say, in this file, which of the other three it is.

**Guild 0 is refused outright.** It is not a guild: it is the legacy/global
config fallback slot, the shared LegitLibs template pool and the bot-global AI
prompt store all at once. A purge that accepted it would wipe the bot's own
settings for every server.

**Errors are not swallowed.** ``privacy_service`` tolerates a failed delete and
carries on, because a partial erasure of one member beats none. Here the
opposite holds: a tolerated failure means a guild's data is silently retained,
which is the exact defect this module exists to fix. A failure propagates, the
caller's transaction rolls back whole, and the departed marker stays in place
so the next daily sweep tries again.
"""

from __future__ import annotations

import logging
import sqlite3
import time

log = logging.getLogger(__name__)

#: The instance-wide dial, stored at ``guild_id = 0`` because the row it would
#: otherwise live in — the departing guild's own config — is part of what gets
#: deleted. Absent means **off**: nothing is ever purged until an operator sets
#: it. ``0`` purges the moment the bot is removed; ``N > 0`` waits N days, and
#: a re-invite inside that window cancels the purge.
PURGE_DELAY_CONFIG_KEY = "guild_removal_purge_days"

#: Not a guild. See the module docstring.
GLOBAL_GUILD_ID = 0

#: ``child table -> (foreign key column, parent table, parent key column)``.
#: Hand-written because only 21 of 303 tables declare a foreign key, so the
#: relationships are not in the schema to be read.
CHILD_TABLES: dict[str, tuple[str, str, str]] = {
    "announcement_buttons": ("announcement_id", "announcements", "id"),
    "bio_fields": ("template_id", "bio_templates", "id"),
    "doc_placements": ("doc_id", "docs", "id"),
    "doc_placement_messages": ("placement_id", "doc_placements", "id"),
    "econ_community_contrib": ("quest_id", "econ_quests", "id"),
    "econ_community_payouts": ("quest_id", "econ_quests", "id"),
    "econ_community_progress": ("quest_id", "econ_quests", "id"),
    "econ_community_tier_payouts": ("quest_id", "econ_quests", "id"),
    "econ_qotd_rewards": ("qotd_id", "econ_qotd", "id"),
    "econ_quest_progress": ("quest_id", "econ_quests", "id"),
    "econ_quest_progress_marks": ("quest_id", "econ_quests", "id"),
    "guess_guesses": ("round_id", "guess_rounds", "id"),
    "intake_card_steps": ("card_id", "intake_cards", "id"),
    "legitlibs_revisions": ("template_id", "legitlibs_templates", "template_id"),
    "message_attachments": ("message_id", "messages", "message_id"),
    "message_embeds": ("message_id", "messages", "message_id"),
    "message_mentions": ("message_id", "messages", "message_id"),
    "message_reactions": ("message_id", "messages", "message_id"),
    "nsfw_detections": ("message_id", "messages", "message_id"),
    "pen_pals_questions": ("session_id", "pen_pals_sessions", "session_id"),
    "policy_votes": ("policy_id", "policies", "id"),
    "risky_round_rolls": ("game_id", "risky_active_rounds", "game_id"),
    "role_menu_bindings": ("menu_id", "role_menus", "id"),
    "role_menu_options": ("menu_id", "role_menus", "id"),
    "rules_labels": ("event_id", "rules_events", "id"),
    "ticket_participants": ("ticket_id", "tickets", "id"),
    "wellness_blackout_overages": ("blackout_id", "wellness_blackouts", "id"),
    "wellness_cap_counters": ("cap_id", "wellness_caps", "id"),
    "wellness_cap_overages": ("cap_id", "wellness_caps", "id"),
    "whisper_guesses": ("whisper_id", "whispers", "id"),
    "whisper_replies": ("whisper_id", "whispers", "id"),
    "whisper_reports": ("whisper_id", "whispers", "id"),
    "whisper_reply_reports": ("reply_id", "whisper_replies", "id"),
}

#: ``table -> channel id column``. Cleared only from the departing guild's
#: channel list, which the caller reads off the ``Guild`` object before it goes.
CHANNEL_TABLES: dict[str, str] = {
    "games_session_tracker": "channel_id",
    "legitlibs_channel_config": "channel_id",
}

#: Bot-wide by design; a guild leaving must not touch them. ``legitlibs_reports``
#: is here reluctantly — it keys on a ``game_id`` belonging to an in-memory game
#: that no table records, so its rows cannot be attributed to a guild at all
#: (``privacy_service`` hits the same wall and erases them by ``reporter_id``).
GLOBAL_TABLES: frozenset[str] = frozenset({
    "games_question_bank",
    "games_timer_defaults",
    "id_remap",
    "legitlibs_blank_axes",
    "legitlibs_blank_prompts",
    "legitlibs_reports",
    "nfl_games",
    "schema_version",
    "sqlite_sequence",
    "xp_rollup_state",
})


# ---------------------------------------------------------------- the dial


def purge_delay_days(conn: sqlite3.Connection) -> int | None:
    """Days to wait before purging a departed guild, or ``None`` if disabled.

    Read without ``get_config_value``'s legacy fallback because there is
    nothing to fall back to: the row *is* the ``guild_id = 0`` row. A value
    that is not a non-negative integer reads as disabled rather than raising —
    the fail-safe direction for a switch whose other setting deletes data.
    """
    row = conn.execute(
        "SELECT value FROM config WHERE guild_id = ? AND key = ?",
        (GLOBAL_GUILD_ID, PURGE_DELAY_CONFIG_KEY),
    ).fetchone()
    if not row:
        return None
    try:
        days = int(str(row[0]).strip())
    except (TypeError, ValueError):
        log.warning("Guild purge: %s is not a number, treating as off", PURGE_DELAY_CONFIG_KEY)
        return None
    return days if days >= 0 else None


# ------------------------------------------------------- table classification


def guild_scoped_tables(conn: sqlite3.Connection) -> list[str]:
    """Every table in the live schema carrying a ``guild_id`` column."""
    names = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return [
        name
        for name in names
        if any(c[1] == "guild_id" for c in conn.execute(f"PRAGMA table_info({name})"))
    ]


def classify_tables(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Sort every table in the live schema into the four buckets.

    The coverage test's whole job: anything landing in ``unclassified`` is a
    table this module would leave behind on a guild removal.
    """
    guild = set(guild_scoped_tables(conn))
    out: dict[str, list[str]] = {"guild": [], "child": [], "channel": [], "global": [], "unclassified": []}
    for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ):
        name = row[0]
        if name in guild:
            out["guild"].append(name)
        elif name in CHILD_TABLES:
            out["child"].append(name)
        elif name in CHANNEL_TABLES:
            out["channel"].append(name)
        elif name in GLOBAL_TABLES:
            out["global"].append(name)
        else:
            out["unclassified"].append(name)
    return out


def _child_where(table: str, guild_id: int) -> tuple[str, list[int]]:
    """A WHERE clause selecting ``table``'s rows for one guild, and its params.

    Recursive, so a grandchild resolves through its parent's own subquery
    rather than needing a second hand-written rule.
    """
    fk, parent, pk = CHILD_TABLES[table]
    if parent in CHILD_TABLES:
        cond, params = _child_where(parent, guild_id)
    else:
        cond, params = "guild_id = ?", [guild_id]
    return f"{fk} IN (SELECT {pk} FROM {parent} WHERE {cond})", params


def _child_depth(table: str) -> int:
    """How many parents up ``table`` sits from a guild-scoped row."""
    depth, seen = 1, {table}
    parent = CHILD_TABLES[table][1]
    while parent in CHILD_TABLES:
        if parent in seen:  # pragma: no cover - a cycle would be a typo above
            raise ValueError(f"CHILD_TABLES cycle at {parent}")
        seen.add(parent)
        depth += 1
        parent = CHILD_TABLES[parent][1]
    return depth


def _child_tables_deepest_first() -> list[str]:
    """Deepest first, so a parent is never deleted out from under its children.

    Sorted at run time rather than trusting the order entries were written in:
    a declaration order that silently has to be correct is a trap for whoever
    adds the next row.
    """
    return sorted(CHILD_TABLES, key=lambda t: (-_child_depth(t), t))


# ------------------------------------------------------------- the purge


def purge_guild_data(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    channel_ids: tuple[int, ...] = (),
) -> dict[str, int]:
    """Delete every row belonging to ``guild_id``. Returns per-table counts.

    The caller owns the transaction: run this on one connection and commit at
    the end, so a failure rolls the whole purge back rather than leaving a
    guild half-erased. Only non-zero counts are returned — a 303-entry dict of
    mostly zeroes is not a log line anyone reads.

    ``channel_ids`` are the departing guild's channel ids, read off the
    ``Guild`` object while it still exists; without them the two channel-keyed
    tables cannot be reached (see the module docstring).
    """
    if int(guild_id) == GLOBAL_GUILD_ID:
        raise ValueError(
            "refusing to purge guild_id 0: it is the bot-global config slot, "
            "not a guild"
        )

    counts: dict[str, int] = {}

    def _run(table: str, sql: str, params: list[int]) -> None:
        deleted = conn.execute(sql, params).rowcount
        if deleted > 0:
            counts[table] = counts.get(table, 0) + deleted

    for table in _child_tables_deepest_first():
        cond, params = _child_where(table, guild_id)
        _run(table, f"DELETE FROM {table} WHERE {cond}", params)

    for table, column in CHANNEL_TABLES.items():
        if not channel_ids:
            continue
        marks = ",".join("?" * len(channel_ids))
        _run(table, f"DELETE FROM {table} WHERE {column} IN ({marks})", list(channel_ids))

    for table in guild_scoped_tables(conn):
        _run(table, f"DELETE FROM {table} WHERE guild_id = ?", [guild_id])

    return counts


def sweep_orphans(conn: sqlite3.Connection) -> dict[str, int]:
    """Delete child rows whose parent no longer exists. Returns non-zero counts.

    Residue from before this module existed — prod carries 135
    ``nsfw_detections`` rows pointing at messages that are gone. A child row
    with no parent is unreachable by every query in the bot and attributable to
    no guild, so it is dead data by definition.

    Rows whose foreign key is NULL are **left alone**: they are unreachable for
    a different reason (never linked, rather than orphaned), and sweeping them
    here would quietly widen this from cleanup into deletion of rows nobody has
    looked at.
    """
    counts: dict[str, int] = {}
    for table in _child_tables_deepest_first():
        fk, parent, pk = CHILD_TABLES[table]
        deleted = conn.execute(
            f"DELETE FROM {table} WHERE {fk} IS NOT NULL AND NOT EXISTS "
            f"(SELECT 1 FROM {parent} p WHERE p.{pk} = {table}.{fk})"
        ).rowcount
        if deleted > 0:
            counts[table] = deleted
    return counts


# --------------------------------------------------------- departed markers


def mark_departed(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    delay_days: int,
    channel_ids: tuple[int, ...] = (),
    now: float | None = None,
    source: str = "event",
) -> float:
    """Record that the bot is out of ``guild_id``, and when to purge it.

    The deadline is **baked into the row here**, never recomputed later from
    config: the guild's own config rows are among the things the purge deletes,
    and a deadline re-read at sweep time would move every time the dial did.

    ``channel_ids`` is stored for the same reason — see :data:`CHANNEL_TABLES`.
    It is available only while the ``Guild`` object survives, which is the
    duration of the removal handler and no longer.

    Re-marking an already-marked guild keeps the original deadline. A guild
    that departs, is re-invited and departs again gets a fresh one, because
    :func:`clear_departed` removed the row in between.
    """
    now = time.time() if now is None else now
    purge_after = now + delay_days * 86400
    conn.execute(
        "INSERT INTO departed_guilds (guild_id, departed_at, purge_after, channel_ids, source) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(guild_id) DO NOTHING",
        (guild_id, now, purge_after, _pack_channels(channel_ids), source),
    )
    row = conn.execute(
        "SELECT purge_after FROM departed_guilds WHERE guild_id = ?", (guild_id,)
    ).fetchone()
    return float(row[0])


def _pack_channels(channel_ids: tuple[int, ...]) -> str:
    return ",".join(str(int(c)) for c in channel_ids)


def departed_channel_ids(conn: sqlite3.Connection, guild_id: int) -> tuple[int, ...]:
    """The channel snapshot taken when ``guild_id`` departed, if any."""
    row = conn.execute(
        "SELECT channel_ids FROM departed_guilds WHERE guild_id = ?", (guild_id,)
    ).fetchone()
    if not row or not str(row[0]).strip():
        return ()
    return tuple(int(part) for part in str(row[0]).split(",") if part.strip())


def clear_departed(conn: sqlite3.Connection, guild_id: int) -> bool:
    """Cancel a pending purge — the bot is back in the guild. True if one was."""
    return conn.execute(
        "DELETE FROM departed_guilds WHERE guild_id = ?", (guild_id,)
    ).rowcount > 0


def due_guilds(conn: sqlite3.Connection, *, now: float | None = None) -> list[int]:
    """Marked guilds whose grace period has run out."""
    now = time.time() if now is None else now
    return [
        int(r[0])
        for r in conn.execute(
            "SELECT guild_id FROM departed_guilds WHERE purge_after <= ? ORDER BY guild_id",
            (now,),
        )
    ]


def pending_guilds(conn: sqlite3.Connection) -> list[tuple[int, float]]:
    """Every marked guild and its deadline, for the sweep's re-invite check."""
    return [
        (int(r[0]), float(r[1]))
        for r in conn.execute(
            "SELECT guild_id, purge_after FROM departed_guilds ORDER BY guild_id"
        )
    ]


def run_departed_purge(
    conn: sqlite3.Connection, guild_id: int
) -> dict[str, int]:
    """Purge one marked guild and drop its marker. Returns non-zero counts.

    The marker is deleted inside the caller's transaction, alongside the rows —
    so if anything fails, both roll back together and tomorrow's sweep finds
    the guild still queued. A marker removed while the data survived would make
    the guild invisible to every future pass.
    """
    channel_ids = departed_channel_ids(conn, guild_id)
    counts = purge_guild_data(conn, guild_id, channel_ids=channel_ids)
    conn.execute("DELETE FROM departed_guilds WHERE guild_id = ?", (guild_id,))
    return counts


def known_guild_ids(conn: sqlite3.Connection) -> set[int]:
    """Guilds the database has settings for, excluding the global slot.

    ``config`` is the reconciliation probe because it is the one table every
    configured guild has a row in, and a guild with no config has nothing worth
    queueing. Guild 0 is excluded here rather than relied on being filtered
    downstream — it is the one id a purge must never see.
    """
    return {
        int(r[0])
        for r in conn.execute("SELECT DISTINCT guild_id FROM config")
        if int(r[0]) != GLOBAL_GUILD_ID
    }


def reconcile_presence(
    conn: sqlite3.Connection,
    present_ids: set[int],
    *,
    now: float | None = None,
) -> tuple[list[int], list[int]]:
    """Re-align the queue with the guilds the bot is actually in.

    Returns ``(marked, rejoined)`` — guilds newly queued because the database
    knows them but the bot is not in them, and guilds un-queued because it
    plainly is.

    Rejoins are un-queued **even with the dial off**. A stale marker for a
    guild the bot is sitting in is wrong either way, and leaving it there would
    arm a purge for the moment someone sets the dial.

    Raises ``ValueError`` on an empty ``present_ids``. An empty guild list means
    the bot has not finished receiving guilds, not that it was removed from all
    of them, and acting on that reading would queue every server at once.
    """
    if not present_ids:
        raise ValueError("refusing to reconcile against an empty guild list")

    rejoined = [gid for gid, _ in pending_guilds(conn) if gid in present_ids]
    for gid in rejoined:
        clear_departed(conn, gid)

    delay = purge_delay_days(conn)
    if delay is None:
        return [], rejoined

    marked = [gid for gid in sorted(known_guild_ids(conn)) if gid not in present_ids]
    for gid in marked:
        mark_departed(conn, gid, delay_days=delay, now=now, source="reconcile")
    return marked, rejoined
