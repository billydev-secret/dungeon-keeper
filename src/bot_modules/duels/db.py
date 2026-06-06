"""Shared duel database helpers — duel_nicks, duel_cooldowns, duel_config."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.services.games_db import GamesDb

_CONFIG_DEFAULTS: dict = {
    "cooldown_hours": 48,
    "sentence_hours": 24,
    "allow_early_revert": 0,
    "channel_allowlist": "[]",
    "nick_denylist": "[]",
    "max_nick_length": 32,
    "max_stakes_length": 200,
}


# ── Config ────────────────────────────────────────────────────────────────────

async def get_config(db: GamesDb, guild_id: int, game_type: str) -> dict:
    row = await db.fetchone(
        "SELECT * FROM duel_config WHERE guild_id = ? AND game_type = ?",
        (guild_id, game_type),
    )
    if row:
        return dict(row)
    return {"guild_id": guild_id, "game_type": game_type, **_CONFIG_DEFAULTS}


async def upsert_config(db: GamesDb, guild_id: int, game_type: str, **fields) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO duel_config (guild_id, game_type) VALUES (?, ?)",
        (guild_id, game_type),
    )
    for key, value in fields.items():
        await db.execute(
            f"UPDATE duel_config SET {key} = ? WHERE guild_id = ? AND game_type = ?",
            (value, guild_id, game_type),
        )


# ── Nicks ─────────────────────────────────────────────────────────────────────

async def apply_nick(
    db: GamesDb,
    game_id: int,
    game_type: str,
    guild_id: int,
    loser_id: int,
    winner_id: int,
    original_nick: str | None,
    imposed_nick: str,
    sentence_hours: int,
) -> int:
    now = time.time()
    expires_at = now + sentence_hours * 3600
    return await db.lastrowid(
        """
        INSERT INTO duel_nicks
            (game_id, game_type, guild_id, loser_id, winner_id, original_nick,
             imposed_nick, applied_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (game_id, game_type, guild_id, loser_id, winner_id, original_nick, imposed_nick, now, expires_at),
    )


async def fetch_expired_nicks(db: GamesDb, now: float) -> list[dict]:
    rows = await db.fetchall(
        "SELECT * FROM duel_nicks WHERE reverted_at IS NULL AND expires_at <= ?",
        (now,),
    )
    return [dict(r) for r in rows]


async def get_active_nick_for_user(db: GamesDb, guild_id: int, user_id: int) -> dict | None:
    """Return any active nick sentence for this user, regardless of game type."""
    row = await db.fetchone(
        """
        SELECT * FROM duel_nicks
        WHERE guild_id = ? AND loser_id = ? AND reverted_at IS NULL
        ORDER BY applied_at DESC LIMIT 1
        """,
        (guild_id, user_id),
    )
    return dict(row) if row else None


async def mark_nick_reverted(db: GamesDb, nick_id: int, reason: str) -> None:
    await db.execute(
        "UPDATE duel_nicks SET reverted_at = ?, revert_reason = ? WHERE id = ?",
        (time.time(), reason, nick_id),
    )


# ── Cooldowns ─────────────────────────────────────────────────────────────────

async def check_cooldown(
    db: GamesDb,
    guild_id: int,
    game_type: str,
    user_a: int,
    user_b: int,
    cooldown_hours: int,
) -> float | None:
    lo, hi = min(user_a, user_b), max(user_a, user_b)
    row = await db.fetchone(
        """
        SELECT last_game_at FROM duel_cooldowns
        WHERE guild_id = ? AND game_type = ? AND player_a = ? AND player_b = ?
        """,
        (guild_id, game_type, lo, hi),
    )
    if not row:
        return None
    elapsed = time.time() - row["last_game_at"]
    remaining = cooldown_hours * 3600 - elapsed
    return remaining if remaining > 0 else None


async def set_cooldown(
    db: GamesDb, guild_id: int, game_type: str, user_a: int, user_b: int
) -> None:
    lo, hi = min(user_a, user_b), max(user_a, user_b)
    await db.execute(
        """
        INSERT INTO duel_cooldowns (guild_id, game_type, player_a, player_b, last_game_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, game_type, player_a, player_b)
        DO UPDATE SET last_game_at = excluded.last_game_at
        """,
        (guild_id, game_type, lo, hi, time.time()),
    )


# ── Group cooldowns (per-player, for N-player BaseGame games) ──────────────────

async def check_group_cooldown(
    db: GamesDb,
    guild_id: int,
    game_type: str,
    player_id: int,
    cooldown_hours: int,
) -> float | None:
    """Return seconds remaining on this player's group cooldown, or None if clear."""
    if cooldown_hours <= 0:
        return None
    row = await db.fetchone(
        """
        SELECT last_game_at FROM duel_group_cooldowns
        WHERE guild_id = ? AND game_type = ? AND player_id = ?
        """,
        (guild_id, game_type, player_id),
    )
    if not row:
        return None
    elapsed = time.time() - row["last_game_at"]
    remaining = cooldown_hours * 3600 - elapsed
    return remaining if remaining > 0 else None


async def set_group_cooldown(
    db: GamesDb, guild_id: int, game_type: str, player_id: int
) -> None:
    await db.execute(
        """
        INSERT INTO duel_group_cooldowns (guild_id, game_type, player_id, last_game_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(guild_id, game_type, player_id)
        DO UPDATE SET last_game_at = excluded.last_game_at
        """,
        (guild_id, game_type, player_id, time.time()),
    )
