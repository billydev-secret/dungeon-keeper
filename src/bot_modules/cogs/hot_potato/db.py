"""Async SQLite helpers for Hot Potato. All SQL lives here."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from bot_modules.duels.db import (
    CHALLENGE_RESPONSE_SECONDS,
    NAMING_WINDOW_SECONDS,
    active_idle_seconds,
)
from bot_modules.games.utils import game_store
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
    nick_stake: bool = False,
) -> int:
    now = time.time()
    return await db.lastrowid(
        """
        INSERT INTO hot_potato_games
            (guild_id, channel_id, challenger_id, target_id, stakes_text,
             nick_stake, state, created_at, last_action_at)
        VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
        """,
        (guild_id, channel_id, challenger_id, target_id, stakes_text, int(nick_stake),
         now, now),
    )


async def get_game(db: GamesDb, game_id: int) -> HotPotatoGame | None:
    row = await db.fetchone("SELECT * FROM hot_potato_games WHERE id = ?", (game_id,))
    return game_from_row(row) if row else None


async def get_active_game_for_pair(
    db: GamesDb, guild_id: int, user_a: int, user_b: int
) -> HotPotatoGame | None:
    row = await game_store.fetch_live_game_for_pair(
        db, "hot_potato_games", guild_id, user_a, user_b, _NON_TERMINAL
    )
    return game_from_row(row) if row else None


async def get_pending_game_for_challenger(
    db: GamesDb, guild_id: int, channel_id: int, challenger_id: int
) -> HotPotatoGame | None:
    row = await game_store.fetch_pending_game_for_challenger(
        db, "hot_potato_games", guild_id, channel_id, challenger_id
    )
    return game_from_row(row) if row else None


async def set_game_state(db: GamesDb, game_id: int, state: str, **extra_fields) -> None:
    await game_store.set_game_state(db, "hot_potato_games", game_id, state, **extra_fields)


async def fetch_active_games(db: GamesDb) -> list[HotPotatoGame]:
    rows = await db.fetchall("SELECT * FROM hot_potato_games WHERE state = 'ACTIVE'")
    return [game_from_row(r) for r in rows]


async def fetch_resolved_games(db: GamesDb) -> list[HotPotatoGame]:
    rows = await db.fetchall(
        "SELECT * FROM hot_potato_games "
        "WHERE state IN ('RESOLVED', 'RESOLVED_NO_NICK', 'NICKED', 'NO_NICK_SET')"
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
        (
            now - CHALLENGE_RESPONSE_SECONDS,
            now - active_idle_seconds("hot_potato"),
            now - NAMING_WINDOW_SECONDS,
        ),
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
        # The group cog's anti-ping-pong wait, adopted here in migration 208:
        # without it the duel was a click race decided by a random tick.
        "min_hold": 2.0,
    }
    if row:
        defaults.update(dict(row))
    return defaults


async def upsert_config(db: GamesDb, guild_id: int, **fields) -> None:
    await game_store.upsert_config(db, "hot_potato_config", guild_id, **fields)


async def get_style_total(db: GamesDb, guild_id: int, user_id: int) -> int:
    """A player's running style total on this server (0 if they have none)."""
    row = await db.fetchone(
        "SELECT total_points FROM hot_potato_style WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    return int(row["total_points"]) if row else 0


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
