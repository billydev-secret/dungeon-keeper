"""Storage for Mention Award rules (``mention_award_rules``).

Plain CRUD over guild configuration — the dashboard writes, the listener
reads. Rule *matching* is pure and lives in ``logic.py``; nothing here decides
who gets paid.

Conditions ("chips") are stored as a JSON array on the row — see migration
157 for the shape. Ids inside the JSON are strings, because the panel reads
this JSON and a bare snowflake past 2^53 loses precision in JavaScript.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Mapping, Sequence

from bot_modules.mention_awards.logic import CONDITION_KINDS, Condition, Rule

# A pattern or phrase long enough to be a paragraph is a mis-paste.
MAX_TEXT_LEN = 200
# Chips per rule. Well past any real use; a bound, not a policy.
MAX_CONDITIONS = 10
# Ceiling on a single award. Not a policy on what things are worth — a guard
# against a typo'd extra zero opening a faucet nobody notices.
MAX_AMOUNT = 100_000


def conditions_to_json(conditions: Sequence[Condition]) -> str:
    return json.dumps(
        [
            {"kind": c.kind, "value": c.value, "regex": bool(c.regex)}
            for c in conditions
        ]
    )


def conditions_from_json(raw: str | None) -> tuple[Condition, ...]:
    """Parse a row's conditions JSON; anything malformed yields no chips.

    No chips means the rule matches nothing (logic.py fails closed), so a
    corrupted row parks its rule rather than opening a faucet.
    """
    try:
        items = json.loads(raw or "[]")
        if not isinstance(items, list):
            return ()
        return tuple(
            Condition(
                kind=str(item.get("kind", "")),
                value=str(item.get("value", "")),
                regex=bool(item.get("regex", False)),
            )
            for item in items
            if isinstance(item, dict)
        )
    except (json.JSONDecodeError, AttributeError, TypeError):
        return ()


def validate(amount: int, conditions: Sequence[Condition]) -> str | None:
    """The reason this rule is invalid, or ``None`` if it's fine.

    Also enforced by ``create_rule``/``update_rule`` (raising ``ValueError``),
    so a writer that skips this gate cannot store an invalid rule.
    """
    if amount < 0:
        return "Amount can't be negative."
    if amount > MAX_AMOUNT:
        return f"Amount is limited to {MAX_AMOUNT:,}."
    if not conditions:
        return "Add at least one condition — a rule with none would never fire."
    if len(conditions) > MAX_CONDITIONS:
        return f"A rule is limited to {MAX_CONDITIONS} conditions."
    for cond in conditions:
        if cond.kind not in CONDITION_KINDS:
            return f"Unknown condition kind {cond.kind!r}."
        if cond.kind == "contains_text":
            if not cond.value.strip():
                return "A text condition can't be empty."
            if len(cond.value) > MAX_TEXT_LEN:
                return f"Text conditions are limited to {MAX_TEXT_LEN} characters."
            if cond.regex:
                try:
                    re.compile(cond.value)
                except re.error as e:
                    return f"Invalid regex: {e}."
        else:
            label = CONDITION_KINDS[cond.kind]
            try:
                ok = int(cond.value) > 0
            except (TypeError, ValueError):
                ok = False
            if not ok:
                return f"Pick a {label} for the {label} condition."
    return None


def _require_valid(amount: int, conditions: Sequence[Condition]) -> None:
    problem = validate(amount, conditions)
    if problem:
        raise ValueError(problem)


def list_rules(conn: sqlite3.Connection, guild_id: int) -> list[Mapping[str, Any]]:
    """Every rule for a guild, oldest first — the order the matcher tries."""
    return conn.execute(
        "SELECT id, guild_id, channel_id, amount, conditions, "
        "created_by, created_at FROM mention_award_rules "
        "WHERE guild_id = ? ORDER BY id",
        (guild_id,),
    ).fetchall()


def rules_for_channel(
    conn: sqlite3.Connection, guild_id: int, channel_id: int
) -> list[Rule]:
    """The listener's hot path — rules watching one channel, in match order."""
    rows = conn.execute(
        "SELECT id, channel_id, amount, conditions "
        "FROM mention_award_rules WHERE guild_id = ? AND channel_id = ? "
        "ORDER BY id",
        (guild_id, channel_id),
    ).fetchall()
    return [
        Rule(
            id=int(r["id"]),
            channel_id=int(r["channel_id"]),
            amount=int(r["amount"]),
            conditions=conditions_from_json(r["conditions"]),
        )
        for r in rows
    ]


def create_rule(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    channel_id: int,
    amount: int,
    conditions: Sequence[Condition],
    created_by: int | None = None,
) -> int:
    """Insert a rule, returning its id.

    Raises ``ValueError`` on an invalid rule — the bounds live with the data
    they protect, so no writer can store a rule ``validate`` would reject.
    """
    _require_valid(amount, conditions)
    cur = conn.execute(
        "INSERT INTO mention_award_rules "
        "(guild_id, channel_id, amount, conditions, created_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (guild_id, channel_id, amount, conditions_to_json(conditions), created_by),
    )
    return int(cur.lastrowid or 0)


def update_rule(
    conn: sqlite3.Connection,
    guild_id: int,
    rule_id: int,
    *,
    channel_id: int,
    amount: int,
    conditions: Sequence[Condition],
) -> bool:
    """Overwrite the rule. False when it isn't this guild's.

    Scoped on ``guild_id`` as well as ``id`` so one guild's dashboard can
    never edit another's rule by id. Raises ``ValueError`` on an invalid rule.
    """
    _require_valid(amount, conditions)
    cur = conn.execute(
        "UPDATE mention_award_rules SET channel_id = ?, amount = ?, "
        "conditions = ? WHERE id = ? AND guild_id = ?",
        (channel_id, amount, conditions_to_json(conditions), rule_id, guild_id),
    )
    return cur.rowcount > 0


def delete_rule(conn: sqlite3.Connection, guild_id: int, rule_id: int) -> bool:
    """Remove a rule. False when it isn't this guild's."""
    cur = conn.execute(
        "DELETE FROM mention_award_rules WHERE id = ? AND guild_id = ?",
        (rule_id, guild_id),
    )
    return cur.rowcount > 0
