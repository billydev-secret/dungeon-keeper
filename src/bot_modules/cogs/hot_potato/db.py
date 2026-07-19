"""Async SQLite helpers for Hot Potato. All SQL lives here."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .game import HotPotatoGame, game_from_row

if TYPE_CHECKING:
    from bot_modules.services.games_db import GamesDb

_NON_TERMINAL = ("PENDING", "ACTIVE", "RESOLVED")


async def create_game(
    db: GamesDb,
    guild_id: int,
    channel_id: int,
    challenger_id: int,
    target_id: int,
    stakes_text: str | None,
) -> int:
    now = time.time()
    return await db.lastrowid(
        """
        INSERT INTO hot_potato_games
            (guild_id, channel_id, challenger_id, target_id, stakes_text,
             state, created_at, last_action_at)
        VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?)
        """,
        (guild_id, channel_id, challenger_id, target_id, stakes_text, now, now),
    )


async def get_game(db: GamesDb, game_id: int) -> HotPotatoGame | None:
    row = await db.fetchone("SELECT * FROM hot_potato_games WHERE id = ?", (game_id,))
    return game_from_row(row) if row else None


async def get_active_game_for_pair(
    db: GamesDb, guild_id: int, user_a: int, user_b: int
) -> HotPotatoGame | None:
    placeholders = ",".join("?" * len(_NON_TERMINAL))
    row = await db.fetchone(
        f"""
        SELECT * FROM hot_potato_games
        WHERE guild_id = ?
          AND state IN ({placeholders})
          AND (
               (challenger_id = ? AND target_id = ?)
            OR (challenger_id = ? AND target_id = ?)
          )
        """,
        (guild_id, *_NON_TERMINAL, user_a, user_b, user_b, user_a),
    )
    return game_from_row(row) if row else None


async def get_pending_game_for_challenger(
    db: GamesDb, guild_id: int, channel_id: int, challenger_id: int
) -> HotPotatoGame | None:
    row = await db.fetchone(
        """
        SELECT * FROM hot_potato_games
        WHERE guild_id = ? AND channel_id = ? AND challenger_id = ? AND state = 'PENDING'
        ORDER BY created_at DESC LIMIT 1
        """,
        (guild_id, channel_id, challenger_id),
    )
    return game_from_row(row) if row else None


async def set_game_state(db: GamesDb, game_id: int, state: str, **extra_fields) -> None:
    fields = {"state": state, **extra_fields}
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    await db.execute(
        f"UPDATE hot_potato_games SET {set_clause} WHERE id = ?",
        (*fields.values(), game_id),
    )


async def fetch_active_games(db: GamesDb) -> list[HotPotatoGame]:
    rows = await db.fetchall("SELECT * FROM hot_potato_games WHERE state = 'ACTIVE'")
    return [game_from_row(r) for r in rows]


async def fetch_resolved_games(db: GamesDb) -> list[HotPotatoGame]:
    rows = await db.fetchall(
        "SELECT * FROM hot_potato_games WHERE state IN ('RESOLVED', 'NICKED')"
    )
    return [game_from_row(r) for r in rows]


async def fetch_sweepable_games(db: GamesDb, now: float) -> list[HotPotatoGame]:
    rows = await db.fetchall(
        """
        SELECT * FROM hot_potato_games
        WHERE
          (state = 'PENDING'  AND created_at    <= ?)
       OR (state = 'ACTIVE'   AND last_action_at <= ?)
       OR (state = 'RESOLVED' AND resolved_at   <= ?)
        """,
        (now - 60, now - 600, now - 300),
    )
    return [game_from_row(r) for r in rows]


async def get_config(db: GamesDb, guild_id: int) -> dict:
    row = await db.fetchone(
        "SELECT * FROM hot_potato_config WHERE guild_id = ?", (guild_id,)
    )
    defaults: dict = {
        "guild_id": guild_id,
        "min_timer": 10.0,
        "max_timer": 45.0,
    }
    if row:
        defaults.update(dict(row))
    return defaults


async def upsert_config(db: GamesDb, guild_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" * len(fields))
    updates = ", ".join(f"{k} = excluded.{k}" for k in fields)
    await db.execute(
        f"""
        INSERT INTO hot_potato_config (guild_id, {cols})
        VALUES (?, {placeholders})
        ON CONFLICT (guild_id) DO UPDATE SET {updates}
        """,
        (guild_id, *fields.values()),
    )


async def add_style_points(db: GamesDb, guild_id: int, user_id: int, points: int) -> None:
    await db.execute(
        """
        INSERT INTO hot_potato_style (guild_id, user_id, total_points)
        VALUES (?, ?, ?)
        ON CONFLICT (guild_id, user_id) DO UPDATE SET
            total_points = total_points + excluded.total_points
        """,
        (guild_id, user_id, points),
    )
