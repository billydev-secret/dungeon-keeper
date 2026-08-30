"""Async SQLite helpers for Quickdraw. All SQL lives here."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from bot_modules.duels.db import CHALLENGE_RESPONSE_SECONDS
from bot_modules.games.utils import game_store
from .game import QuickdrawGame, game_from_row

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
        INSERT INTO quickdraw_games
            (guild_id, channel_id, challenger_id, target_id, stakes_text,
             nick_stake, state, qd_state, created_at, last_action_at)
        VALUES (?, ?, ?, ?, ?, ?, 'PENDING', 'WAITING', ?, ?)
        """,
        (guild_id, channel_id, challenger_id, target_id, stakes_text, int(nick_stake),
         now, now),
    )


async def get_game(db: GamesDb, game_id: int) -> QuickdrawGame | None:
    row = await db.fetchone("SELECT * FROM quickdraw_games WHERE id = ?", (game_id,))
    return game_from_row(row) if row else None


async def get_active_game_for_pair(
    db: GamesDb, guild_id: int, user_a: int, user_b: int
) -> QuickdrawGame | None:
    row = await game_store.fetch_live_game_for_pair(
        db, "quickdraw_games", guild_id, user_a, user_b, _NON_TERMINAL
    )
    return game_from_row(row) if row else None


async def get_pending_game_for_challenger(
    db: GamesDb, guild_id: int, channel_id: int, challenger_id: int
) -> QuickdrawGame | None:
    row = await game_store.fetch_pending_game_for_challenger(
        db, "quickdraw_games", guild_id, channel_id, challenger_id
    )
    return game_from_row(row) if row else None


async def set_game_state(db: GamesDb, game_id: int, state: str, **extra_fields) -> None:
    await game_store.set_game_state(db, "quickdraw_games", game_id, state, **extra_fields)


async def fetch_active_games(db: GamesDb) -> list[QuickdrawGame]:
    rows = await db.fetchall("SELECT * FROM quickdraw_games WHERE state = 'ACTIVE'")
    return [game_from_row(r) for r in rows]


async def fetch_resolved_games(db: GamesDb) -> list[QuickdrawGame]:
    rows = await db.fetchall(
        "SELECT * FROM quickdraw_games WHERE state IN ('RESOLVED', 'NICKED')"
    )
    return [game_from_row(r) for r in rows]


async def fetch_sweepable_games(db: GamesDb, now: float) -> list[QuickdrawGame]:
    rows = await db.fetchall(
        """
        SELECT * FROM quickdraw_games
        WHERE
          (state = 'PENDING'  AND created_at      <= ?)
       OR (state = 'ACTIVE'   AND last_action_at   <= ?)
       OR (state = 'RESOLVED' AND resolved_at      <= ?)
        """,
        (now - CHALLENGE_RESPONSE_SECONDS, now - 600, now - 300),
    )
    return [game_from_row(r) for r in rows]


async def get_config(db: GamesDb, guild_id: int) -> dict:
    row = await db.fetchone(
        "SELECT * FROM quickdraw_config WHERE guild_id = ?", (guild_id,)
    )
    defaults: dict = {
        "guild_id": guild_id,
        "min_delay": 3.0,
        "max_delay": 8.0,
        "draw_window": 5.0,
        # No `void_on_double_noshow`: a draw nobody answers is always
        # voided, nothing ever read the flag, and migration 194 dropped the
        # column it sat in.
    }
    if row:
        defaults.update(dict(row))
    return defaults


async def upsert_config(db: GamesDb, guild_id: int, **fields) -> None:
    await game_store.upsert_config(db, "quickdraw_config", guild_id, **fields)


