"""Shared SQL for the per-game mini-game stores.

Six games — chicken, hot potato, hot potato group, musical chairs, pressure
cooker and quickdraw — each keep their own ``db.py`` with their own tables and
their own row-to-dataclass mapper. That part is genuinely per-game. What was
not per-game was the SQL underneath: six copies of the same UPDATE builder,
five of the same upsert, three each of the same two lookups, differing only in
the table name they interpolated.

Not to be confused with ``bot_modules.duels.db``, which is a different
storage model rather than a different copy of this one: it keeps *one*
``duel_config`` table discriminated by ``game_type``, and pressure cooker
alone uses it for config. The other five carry their own ``<game>_config``
table. Two of the six (musical chairs, hot potato group) aren't duels at all,
which is why these helpers live under ``games/utils`` and take a table name
rather than assuming the duel schema.

These take the table as an argument and hand back raw rows; mapping a row to
``ChickenGame`` or ``QuickdrawGame`` stays with the game that owns it.

Note on interpolation: table and column names cannot be bound as SQL
parameters, so they are formatted into the statement. Every caller passes a
literal, but a shared builder is exactly where that stops being obvious —
hence ``sql_identifier``, which refuses anything that isn't a plain one.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from bot_modules.core.db_utils import sql_identifier as _ident

if TYPE_CHECKING:
    from bot_modules.services.games_db import GamesDb


async def upsert_config(
    db: GamesDb, table: str, guild_id: int, **fields: Any
) -> None:
    """Insert or update a guild's row in a per-game ``*_config`` table.

    A no-op with no fields — callers pass whatever the dashboard changed, and
    "nothing changed" is a normal answer, not an error.
    """
    if not fields:
        return
    table = _ident(table)
    cols = ", ".join(_ident(k) for k in fields)
    placeholders = ", ".join("?" * len(fields))
    updates = ", ".join(f"{_ident(k)} = excluded.{_ident(k)}" for k in fields)
    await db.execute(
        f"""
        INSERT INTO {table} (guild_id, {cols})
        VALUES (?, {placeholders})
        ON CONFLICT (guild_id) DO UPDATE SET {updates}
        """,
        (guild_id, *fields.values()),
    )


async def set_game_state(
    db: GamesDb, table: str, game_id: int, state: str, **extra_fields: Any
) -> None:
    """Move a game to ``state``, writing any other columns in the same UPDATE.

    One statement rather than two so a game can't be observed half-moved —
    e.g. ``RESOLVED`` with last round's winner still on the row.
    """
    table = _ident(table)
    fields = {"state": state, **extra_fields}
    set_clause = ", ".join(f"{_ident(k)} = ?" for k in fields)
    await db.execute(
        f"UPDATE {table} SET {set_clause} WHERE id = ?",
        (*fields.values(), game_id),
    )


async def fetch_live_game_for_pair(
    db: GamesDb,
    table: str,
    guild_id: int,
    user_a: int,
    user_b: int,
    states: Sequence[str],
) -> Any:
    """The unfinished game between two members, in either direction.

    ``states`` is per-game rather than a constant here: pressure cooker has an
    ``ACCEPTED`` step between pending and active that the others don't, and
    reading that list wrong would let a second duel start on top of a live one.
    """
    table = _ident(table)
    placeholders = ",".join("?" * len(states))
    return await db.fetchone(
        f"""
        SELECT * FROM {table}
        WHERE guild_id = ?
          AND state IN ({placeholders})
          AND (
               (challenger_id = ? AND target_id = ?)
            OR (challenger_id = ? AND target_id = ?)
          )
        """,
        (guild_id, *states, user_a, user_b, user_b, user_a),
    )


async def fetch_pending_game_for_challenger(
    db: GamesDb, table: str, guild_id: int, channel_id: int, challenger_id: int
) -> Any:
    """The challenger's most recent un-accepted challenge in this channel."""
    table = _ident(table)
    return await db.fetchone(
        f"""
        SELECT * FROM {table}
        WHERE guild_id = ? AND channel_id = ? AND challenger_id = ? AND state = 'PENDING'
        ORDER BY created_at DESC LIMIT 1
        """,
        (guild_id, channel_id, challenger_id),
    )
