"""Storage for Mention Award rules (``mention_award_rules``).

Plain CRUD over guild configuration — the dashboard writes, the listener
reads. Rule *matching* is pure and lives in ``logic.py``; nothing here decides
who gets paid.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Mapping, Sequence

from bot_modules.mention_awards.logic import Rule

# A phrase long enough to be a paragraph is a mis-paste, not a trigger.
MAX_PHRASE_LEN = 200
# Ceiling on a single award. Not a policy on what things are worth — a guard
# against a typo'd extra zero opening a faucet nobody notices.
MAX_AMOUNT = 100_000


def rows_to_rules(rows: Sequence[Mapping[str, Any]]) -> list[Rule]:
    """Adapt DB rows to the pure matcher's ``Rule``."""
    return [
        Rule(
            id=int(r["id"]),
            channel_id=int(r["channel_id"]),
            phrase=str(r["phrase"]),
            amount=int(r["amount"]),
            announcer_role_id=int(r["announcer_role_id"] or 0),
        )
        for r in rows
    ]


def validate(phrase: str, amount: int) -> str | None:
    """The reason this rule is invalid, or ``None`` if it's fine."""
    if not phrase.strip():
        return "Trigger phrase can't be empty."
    if len(phrase) > MAX_PHRASE_LEN:
        return f"Trigger phrase is limited to {MAX_PHRASE_LEN} characters."
    if amount < 0:
        return "Amount can't be negative."
    if amount > MAX_AMOUNT:
        return f"Amount is limited to {MAX_AMOUNT:,}."
    return None


def list_rules(conn: sqlite3.Connection, guild_id: int) -> list[Mapping[str, Any]]:
    """Every rule for a guild, oldest first — the order the matcher tries."""
    return conn.execute(
        "SELECT id, guild_id, channel_id, phrase, amount, announcer_role_id, "
        "created_by, created_at FROM mention_award_rules "
        "WHERE guild_id = ? ORDER BY id",
        (guild_id,),
    ).fetchall()


def rules_for_channel(
    conn: sqlite3.Connection, guild_id: int, channel_id: int
) -> list[Rule]:
    """The listener's hot path — rules watching one channel, in match order."""
    rows = conn.execute(
        "SELECT id, channel_id, phrase, amount, announcer_role_id "
        "FROM mention_award_rules WHERE guild_id = ? AND channel_id = ? "
        "ORDER BY id",
        (guild_id, channel_id),
    ).fetchall()
    return rows_to_rules(rows)


def create_rule(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    channel_id: int,
    phrase: str,
    amount: int,
    announcer_role_id: int = 0,
    created_by: int | None = None,
) -> int:
    """Insert a rule, returning its id. Caller validates first."""
    cur = conn.execute(
        "INSERT INTO mention_award_rules "
        "(guild_id, channel_id, phrase, amount, announcer_role_id, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (guild_id, channel_id, phrase.strip(), amount, announcer_role_id, created_by),
    )
    return int(cur.lastrowid or 0)


def update_rule(
    conn: sqlite3.Connection,
    guild_id: int,
    rule_id: int,
    *,
    channel_id: int,
    phrase: str,
    amount: int,
    announcer_role_id: int = 0,
) -> bool:
    """Overwrite all four levers. False when the rule isn't this guild's.

    Scoped on ``guild_id`` as well as ``id`` so one guild's dashboard can
    never edit another's rule by id.
    """
    cur = conn.execute(
        "UPDATE mention_award_rules SET channel_id = ?, phrase = ?, amount = ?, "
        "announcer_role_id = ? WHERE id = ? AND guild_id = ?",
        (channel_id, phrase.strip(), amount, announcer_role_id, rule_id, guild_id),
    )
    return cur.rowcount > 0


def delete_rule(conn: sqlite3.Connection, guild_id: int, rule_id: int) -> bool:
    """Remove a rule. False when it isn't this guild's."""
    cur = conn.execute(
        "DELETE FROM mention_award_rules WHERE id = ? AND guild_id = ?",
        (rule_id, guild_id),
    )
    return cur.rowcount > 0
