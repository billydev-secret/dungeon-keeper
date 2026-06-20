"""Async SQLite helpers for Pressure Cooker. All SQL lives here."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from bot_modules.duels import db as duels_db

from .game import PressureGame, game_from_row, pumps_to_json

if TYPE_CHECKING:
    from bot_modules.services.games_db import GamesDb

_GAME_TYPE = "pressure"

_NON_TERMINAL = (
    "PENDING", "ACCEPTED", "ACTIVE", "RESOLVED",
)

# ── Config (shim → duels/db) ──────────────────────────────────────────────────

async def get_config(db: GamesDb, guild_id: int) -> dict:
    return await duels_db.get_config(db, guild_id, _GAME_TYPE)


async def upsert_config(db: GamesDb, guild_id: int, **fields) -> None:
    await duels_db.upsert_config(db, guild_id, _GAME_TYPE, **fields)


# ── Games ─────────────────────────────────────────────────────────────────────

async def create_game(
    db: GamesDb,
    guild_id: int,
    channel_id: int,
    challenger_id: int,
    target_id: int,
    stakes_text: str | None,
) -> int:
    return await db.lastrowid(
        """
        INSERT INTO pressure_games
            (guild_id, channel_id, challenger_id, target_id, stakes_text, state, created_at)
        VALUES (?, ?, ?, ?, ?, 'PENDING', ?)
        """,
        (guild_id, channel_id, challenger_id, target_id, stakes_text, time.time()),
    )


async def get_game(db: GamesDb, game_id: int) -> PressureGame | None:
    row = await db.fetchone("SELECT * FROM pressure_games WHERE id = ?", (game_id,))
    return game_from_row(row) if row else None


async def get_active_game_for_pair(
    db: GamesDb, guild_id: int, user_a: int, user_b: int
) -> PressureGame | None:
    placeholders = ",".join("?" * len(_NON_TERMINAL))
    row = await db.fetchone(
        f"""
        SELECT * FROM pressure_games
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
) -> PressureGame | None:
    row = await db.fetchone(
        """
        SELECT * FROM pressure_games
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
        f"UPDATE pressure_games SET {set_clause} WHERE id = ?",
        (*fields.values(), game_id),
    )


async def save_pump(db: GamesDb, game: PressureGame) -> None:
    await db.execute(
        """
        UPDATE pressure_games
        SET gauge = ?, active_player = ?, last_pump_at = ?,
            pumps_json = ?, state = ?, winner_id = ?, loser_id = ?, resolved_at = ?
        WHERE id = ?
        """,
        (
            game.gauge,
            game.active_player,
            game.last_pump_at,
            pumps_to_json(game.pumps),
            game.state,
            game.winner_id,
            game.loser_id,
            game.resolved_at,
            game.id,
        ),
    )


async def fetch_active_games(db: GamesDb) -> list[PressureGame]:
    rows = await db.fetchall("SELECT * FROM pressure_games WHERE state = 'ACTIVE'")
    return [game_from_row(r) for r in rows]


async def fetch_resolved_games(db: GamesDb) -> list[PressureGame]:
    rows = await db.fetchall(
        "SELECT * FROM pressure_games WHERE state IN ('RESOLVED', 'NICKED')"
    )
    return [game_from_row(r) for r in rows]


async def fetch_sweepable_games(db: GamesDb, now: float) -> list[PressureGame]:
    rows = await db.fetchall(
        """
        SELECT * FROM pressure_games
        WHERE
          (state = 'PENDING'  AND created_at  <= ?)
       OR (state = 'ACTIVE'   AND last_pump_at <= ?)
       OR (state = 'RESOLVED' AND resolved_at  <= ?)
        """,
        (now - 60, now - 300, now - 300),
    )
    return [game_from_row(r) for r in rows]


async def get_pending_game_for_target(
    db: GamesDb, guild_id: int, user_id: int
) -> PressureGame | None:
    row = await db.fetchone(
        """
        SELECT * FROM pressure_games
        WHERE guild_id = ? AND target_id = ? AND state = 'PENDING'
        ORDER BY created_at DESC LIMIT 1
        """,
        (guild_id, user_id),
    )
    return game_from_row(row) if row else None


# ── Nicks (shim → duels/db) ───────────────────────────────────────────────────

async def apply_nick(
    db: GamesDb,
    game_id: int,
    guild_id: int,
    loser_id: int,
    winner_id: int,
    original_nick: str | None,
    imposed_nick: str,
    sentence_hours: int,
) -> int:
    return await duels_db.apply_nick(
        db,
        game_id=game_id,
        game_type=_GAME_TYPE,
        guild_id=guild_id,
        loser_id=loser_id,
        winner_id=winner_id,
        original_nick=original_nick,
        imposed_nick=imposed_nick,
        sentence_hours=sentence_hours,
    )


async def fetch_expired_nicks(db: GamesDb, now: float) -> list[dict]:
    return await duels_db.fetch_expired_nicks(db, now, _GAME_TYPE)


async def get_active_nick_for_user(
    db: GamesDb, guild_id: int, user_id: int
) -> dict | None:
    return await duels_db.get_active_nick_for_user(db, guild_id, user_id)


async def mark_nick_reverted(db: GamesDb, nick_id: int, reason: str) -> None:
    await duels_db.mark_nick_reverted(db, nick_id, reason)


# ── Cooldowns (shim → duels/db) ───────────────────────────────────────────────

async def check_cooldown(
    db: GamesDb, guild_id: int, user_a: int, user_b: int, cooldown_hours: int
) -> float | None:
    return await duels_db.check_cooldown(
        db, guild_id, _GAME_TYPE, user_a, user_b, cooldown_hours
    )


async def set_cooldown(db: GamesDb, guild_id: int, user_a: int, user_b: int) -> None:
    await duels_db.set_cooldown(db, guild_id, _GAME_TYPE, user_a, user_b)


# ── Stats ─────────────────────────────────────────────────────────────────────

async def get_stats(db: GamesDb, guild_id: int, user_id: int) -> dict:
    row = await db.fetchone(
        """
        SELECT
          SUM(CASE WHEN winner_id = ?1 THEN 1 ELSE 0 END)      AS wins,
          SUM(CASE WHEN loser_id  = ?1 THEN 1 ELSE 0 END)      AS losses,
          COUNT(*)                                               AS total_games,
          MAX(CASE WHEN winner_id = ?1 THEN gauge ELSE NULL END) AS highest_gauge_win
        FROM pressure_games
        WHERE guild_id = ?2
          AND (challenger_id = ?1 OR target_id = ?1)
          AND state IN ('RESOLVED', 'NICKED', 'NO_NICK_SET', 'RESOLVED_NO_NICK', 'EXPIRED')
        """,
        (user_id, guild_id),
    )
    if not row:
        return {"wins": 0, "losses": 0, "total_games": 0, "highest_gauge_win": None}
    return {
        "wins": row["wins"] or 0,
        "losses": row["losses"] or 0,
        "total_games": row["total_games"] or 0,
        "highest_gauge_win": row["highest_gauge_win"],
    }
