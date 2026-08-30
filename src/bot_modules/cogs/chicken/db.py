"""Async SQLite helpers for Chicken. All SQL lives here."""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from bot_modules.games.utils import game_store
from .game import ChickenGame, game_from_row

if TYPE_CHECKING:
    from bot_modules.services.games_db import GamesDb


async def create_lobby(
    db: GamesDb,
    guild_id: int,
    channel_id: int,
    host_id: int,
    stakes_text: str | None,
    nick_stake: bool = False,
) -> int:
    now = time.time()
    roster = json.dumps([host_id])
    return await db.lastrowid(
        """
        INSERT INTO chicken_games
            (guild_id, channel_id, host_id, stakes_text, nick_stake, state, roster,
             alive, created_at, last_action_at)
        VALUES (?, ?, ?, ?, ?, 'LOBBY', ?, '[]', ?, ?)
        """,
        (guild_id, channel_id, host_id, stakes_text, int(nick_stake), roster, now, now),
    )


async def get_game(db: GamesDb, game_id: int) -> ChickenGame | None:
    row = await db.fetchone("SELECT * FROM chicken_games WHERE id = ?", (game_id,))
    return game_from_row(row) if row else None


async def set_game_state(db: GamesDb, game_id: int, state: str, **extra_fields) -> None:
    await game_store.set_game_state(db, "chicken_games", game_id, state, **extra_fields)


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
        # No `lobby_timeout`: the stale-lobby sweep in fetch_sweepable_games
        # uses its own fixed window, nothing ever read the stored value, and
        # migration 194 dropped the column.
    }
    if row:
        defaults.update(dict(row))
    return defaults


async def upsert_config(db: GamesDb, guild_id: int, **fields) -> None:
    await game_store.upsert_config(db, "chicken_config", guild_id, **fields)


