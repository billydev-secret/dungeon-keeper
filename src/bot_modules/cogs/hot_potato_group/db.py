"""Async SQLite helpers for Hot Potato (group). All SQL lives here."""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from .game import HotPotatoGroupGame, game_from_row

if TYPE_CHECKING:
    from bot_modules.services.games_db import GamesDb


async def create_lobby(
    db: GamesDb,
    guild_id: int,
    channel_id: int,
    host_id: int,
    stakes_text: str | None,
) -> int:
    now = time.time()
    roster = json.dumps([host_id])
    return await db.lastrowid(
        """
        INSERT INTO hp_group_games
            (guild_id, channel_id, host_id, stakes_text, state, roster, alive,
             created_at, last_action_at)
        VALUES (?, ?, ?, ?, 'LOBBY', ?, '[]', ?, ?)
        """,
        (guild_id, channel_id, host_id, stakes_text, roster, now, now),
    )


async def get_game(db: GamesDb, game_id: int) -> HotPotatoGroupGame | None:
    row = await db.fetchone("SELECT * FROM hp_group_games WHERE id = ?", (game_id,))
    return game_from_row(row) if row else None


async def set_game_state(db: GamesDb, game_id: int, state: str, **extra_fields) -> None:
    fields = {"state": state, **extra_fields}
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    await db.execute(
        f"UPDATE hp_group_games SET {set_clause} WHERE id = ?",
        (*fields.values(), game_id),
    )


async def fetch_active_games(db: GamesDb) -> list[HotPotatoGroupGame]:
    rows = await db.fetchall("SELECT * FROM hp_group_games WHERE state = 'ACTIVE'")
    return [game_from_row(r) for r in rows]


async def fetch_lobby_games(db: GamesDb) -> list[HotPotatoGroupGame]:
    rows = await db.fetchall("SELECT * FROM hp_group_games WHERE state = 'LOBBY'")
    return [game_from_row(r) for r in rows]


async def fetch_resolved_games(db: GamesDb) -> list[HotPotatoGroupGame]:
    rows = await db.fetchall(
        "SELECT * FROM hp_group_games WHERE state IN ('RESOLVED', 'NICKED')"
    )
    return [game_from_row(r) for r in rows]


async def fetch_sweepable_games(db: GamesDb, now: float) -> list[HotPotatoGroupGame]:
    rows = await db.fetchall(
        """
        SELECT * FROM hp_group_games
        WHERE
          (state = 'LOBBY'    AND last_action_at <= ?)
       OR (state = 'ACTIVE'   AND last_action_at <= ?)
       OR (state = 'RESOLVED' AND resolved_at   <= ?)
        """,
        (now - 90, now - 600, now - 300),
    )
    return [game_from_row(r) for r in rows]


async def get_config(db: GamesDb, guild_id: int) -> dict:
    row = await db.fetchone(
        "SELECT * FROM hp_group_config WHERE guild_id = ?", (guild_id,)
    )
    defaults: dict = {
        "guild_id": guild_id,
        "min_fuse": 20.0,
        "max_fuse": 60.0,
        "min_hold": 2.0,
        "shake_threshold": 0.70,
        "pass_mode": "clockwise",
        "min_players": 2,
        "max_players": 10,
        "lobby_timeout": 60.0,
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
        INSERT INTO hp_group_config (guild_id, {cols})
        VALUES (?, {placeholders})
        ON CONFLICT (guild_id) DO UPDATE SET {updates}
        """,
        (guild_id, *fields.values()),
    )


