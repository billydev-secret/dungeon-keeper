"""Async SQLite helpers for Chicken. All SQL lives here."""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from .game import ChickenGame, game_from_row

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
        INSERT INTO chicken_games
            (guild_id, channel_id, host_id, stakes_text, state, roster, alive,
             created_at, last_action_at)
        VALUES (?, ?, ?, ?, 'LOBBY', ?, '[]', ?, ?)
        """,
        (guild_id, channel_id, host_id, stakes_text, roster, now, now),
    )


async def get_game(db: GamesDb, game_id: int) -> ChickenGame | None:
    row = await db.fetchone("SELECT * FROM chicken_games WHERE id = ?", (game_id,))
    return game_from_row(row) if row else None


async def set_game_state(db: GamesDb, game_id: int, state: str, **extra_fields) -> None:
    fields = {"state": state, **extra_fields}
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    await db.execute(
        f"UPDATE chicken_games SET {set_clause} WHERE id = ?",
        (*fields.values(), game_id),
    )


async def fetch_active_games(db: GamesDb) -> list[ChickenGame]:
    rows = await db.fetchall("SELECT * FROM chicken_games WHERE state = 'ACTIVE'")
    return [game_from_row(r) for r in rows]


async def fetch_lobby_games(db: GamesDb) -> list[ChickenGame]:
    rows = await db.fetchall("SELECT * FROM chicken_games WHERE state = 'LOBBY'")
    return [game_from_row(r) for r in rows]


async def fetch_resolved_games(db: GamesDb) -> list[ChickenGame]:
    rows = await db.fetchall("SELECT * FROM chicken_games WHERE state IN ('RESOLVED', 'NICKED')")
    return [game_from_row(r) for r in rows]


async def fetch_sweepable_games(db: GamesDb, now: float) -> list[ChickenGame]:
    rows = await db.fetchall(
        """
        SELECT * FROM chicken_games
        WHERE
          (state = 'LOBBY'    AND last_action_at <= ?)
       OR (state = 'ACTIVE'   AND last_action_at <= ?)
       OR (state = 'RESOLVED' AND resolved_at   <= ?)
        """,
        (now - 90, now - 600, now - 300),
    )
    return [game_from_row(r) for r in rows]


async def get_config(db: GamesDb, guild_id: int) -> dict:
    row = await db.fetchone("SELECT * FROM chicken_config WHERE guild_id = ?", (guild_id,))
    defaults: dict = {
        "guild_id": guild_id,
        "climb_duration": 25.0,
        "min_players": 2,
        "max_players": 8,
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
        INSERT INTO chicken_config (guild_id, {cols})
        VALUES (?, {placeholders})
        ON CONFLICT (guild_id) DO UPDATE SET {updates}
        """,
        (guild_id, *fields.values()),
    )


async def get_stats(db: GamesDb, guild_id: int, user_id: int) -> dict:
    rows = await db.fetchall(
        """
        SELECT roster, winner_id, loser_id FROM chicken_games
        WHERE guild_id = ?
          AND state IN ('RESOLVED', 'NICKED', 'NO_NICK_SET', 'RESOLVED_NO_NICK')
        """,
        (guild_id,),
    )
    wins = losses = total = 0
    for r in rows:
        roster = json.loads(r["roster"] or "[]")
        if user_id not in roster:
            continue
        total += 1
        if r["winner_id"] == user_id:
            wins += 1
        if r["loser_id"] == user_id:
            losses += 1
    return {"wins": wins, "losses": losses, "total_games": total}
