"""Veil cog — DB query layer (sync sqlite3)."""
from __future__ import annotations

import sqlite3
import time

from db_utils import get_config_value, set_config_value
from services.veil_models import VeilConfig, VeilGuess, VeilOptin, VeilRound

_CONFIG_DEFAULTS: dict[str, str] = {
    "veil_role_id": "0",
    "veil_channel_id": "0",
    "veil_guess_cooldown_seconds": "30",
    "veil_crop_difficulty": "medium",
    "veil_min_image_dimension_px": "400",
    "veil_max_image_size_mb": "10",
}


def get_veil_config(conn: sqlite3.Connection, guild_id: int) -> VeilConfig:
    def _get(key: str) -> str:
        return get_config_value(conn, key, _CONFIG_DEFAULTS[key], guild_id)

    return VeilConfig(
        guild_id=guild_id,
        veil_role_id=int(_get("veil_role_id") or 0),
        veil_channel_id=int(_get("veil_channel_id") or 0),
        guess_cooldown_seconds=int(_get("veil_guess_cooldown_seconds") or 30),
        crop_difficulty=_get("veil_crop_difficulty"),
        min_image_dimension_px=int(_get("veil_min_image_dimension_px") or 400),
        max_image_size_mb=int(_get("veil_max_image_size_mb") or 10),
    )


def set_veil_config_value(
    conn: sqlite3.Connection, guild_id: int, key: str, value: str
) -> None:
    """key is the full config key, e.g. 'veil_crop_difficulty'."""
    set_config_value(conn, key, value, guild_id)


def _row_to_round(row: sqlite3.Row) -> VeilRound:
    return VeilRound(
        id=row["id"],
        guild_id=row["guild_id"],
        submitter_id=row["submitter_id"],
        answer_id=row["answer_id"],
        channel_id=row["channel_id"],
        message_id=row["message_id"],
        crop_path=row["crop_path"],
        crop_url=row["crop_url"],
        original_path=row["original_path"],
        difficulty=row["difficulty"],
        candidate_count=row["candidate_count"],
        reroll_count=row["reroll_count"],
        allow_reuse=bool(row["allow_reuse"]),
        is_reuse=bool(row["is_reuse"]),
        original_round_id=row["original_round_id"],
        reuse_blocked=bool(row["reuse_blocked"]),
        created_at=row["created_at"],
        solved_at=row["solved_at"],
        solver_id=row["solver_id"],
        guesses_to_solve=row["guesses_to_solve"],
        unique_guessers_to_solve=row["unique_guessers_to_solve"],
        answer_optout=bool(row["answer_optout"]),
        deleted_at=row["deleted_at"],
    )


def insert_round(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    submitter_id: int,
    answer_id: int,
    channel_id: int = 0,
    message_id: int = 0,
    crop_path: str = "",
    crop_url: str = "",
    difficulty: str = "medium",
    candidate_count: int = 0,
    allow_reuse: bool = False,
    is_reuse: bool = False,
    original_round_id: int | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO veil_rounds
            (guild_id, submitter_id, answer_id, channel_id, message_id,
             crop_path, crop_url, difficulty, candidate_count,
             allow_reuse, is_reuse, original_round_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (guild_id, submitter_id, answer_id, channel_id, message_id,
         crop_path, crop_url, difficulty, candidate_count,
         int(allow_reuse), int(is_reuse), original_round_id, time.time()),
    )
    return cur.lastrowid  # type: ignore[return-value]


def get_round(conn: sqlite3.Connection, round_id: int) -> VeilRound | None:
    row = conn.execute("SELECT * FROM veil_rounds WHERE id = ?", (round_id,)).fetchone()
    return _row_to_round(row) if row else None


def update_round_message(
    conn: sqlite3.Connection, round_id: int, *, message_id: int, crop_url: str, crop_path: str
) -> None:
    conn.execute(
        "UPDATE veil_rounds SET message_id = ?, crop_url = ?, crop_path = ? WHERE id = ?",
        (message_id, crop_url, crop_path, round_id),
    )


def set_round_original_path(
    conn: sqlite3.Connection, round_id: int, original_path: str
) -> None:
    conn.execute(
        "UPDATE veil_rounds SET original_path = ? WHERE id = ?",
        (original_path, round_id),
    )


def mark_round_solved(
    conn: sqlite3.Connection, round_id: int,
    *, solver_id: int, guesses_to_solve: int, unique_guessers_to_solve: int
) -> None:
    conn.execute(
        """
        UPDATE veil_rounds
        SET solved_at = ?, solver_id = ?,
            guesses_to_solve = ?, unique_guessers_to_solve = ?
        WHERE id = ?
        """,
        (time.time(), solver_id, guesses_to_solve, unique_guessers_to_solve, round_id),
    )


def soft_delete_round(conn: sqlite3.Connection, round_id: int) -> None:
    conn.execute(
        "UPDATE veil_rounds SET deleted_at = ? WHERE id = ?", (time.time(), round_id)
    )


def set_round_answer_optout(conn: sqlite3.Connection, round_id: int) -> None:
    conn.execute("UPDATE veil_rounds SET answer_optout = 1 WHERE id = ?", (round_id,))


def set_round_reroll_count(conn: sqlite3.Connection, round_id: int, reroll_count: int) -> None:
    conn.execute(
        "UPDATE veil_rounds SET reroll_count = ? WHERE id = ?", (reroll_count, round_id)
    )


def get_active_rounds_for_guild(
    conn: sqlite3.Connection, guild_id: int, *, limit: int = 50
) -> list[VeilRound]:
    rows = conn.execute(
        """
        SELECT * FROM veil_rounds
        WHERE guild_id = ? AND deleted_at IS NULL
        ORDER BY created_at DESC LIMIT ?
        """,
        (guild_id, limit),
    ).fetchall()
    return [_row_to_round(r) for r in rows]


def get_reusable_rounds(
    conn: sqlite3.Connection, guild_id: int, *, min_age_seconds: float
) -> list[VeilRound]:
    cutoff = time.time() - min_age_seconds
    rows = conn.execute(
        """
        SELECT * FROM veil_rounds
        WHERE guild_id = ? AND allow_reuse = 1 AND reuse_blocked = 0
          AND solved_at IS NOT NULL AND created_at <= ?
          AND deleted_at IS NULL
        ORDER BY created_at ASC
        LIMIT 200
        """,
        (guild_id, cutoff),
    ).fetchall()
    return [_row_to_round(r) for r in rows]


def _row_to_guess(row: sqlite3.Row) -> VeilGuess:
    return VeilGuess(
        id=row["id"],
        round_id=row["round_id"],
        guesser_id=row["guesser_id"],
        guessed_user_id=row["guessed_user_id"],
        correct=bool(row["correct"]),
        created_at=row["created_at"],
    )


def insert_guess(
    conn: sqlite3.Connection,
    *, round_id: int, guesser_id: int, guessed_user_id: int, correct: bool
) -> int:
    cur = conn.execute(
        """
        INSERT INTO veil_guesses (round_id, guesser_id, guessed_user_id, correct, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (round_id, guesser_id, guessed_user_id, int(correct), time.time()),
    )
    return cur.lastrowid  # type: ignore[return-value]


def get_guesses_for_round(conn: sqlite3.Connection, round_id: int) -> list[VeilGuess]:
    rows = conn.execute(
        "SELECT * FROM veil_guesses WHERE round_id = ? ORDER BY created_at ASC", (round_id,)
    ).fetchall()
    return [_row_to_guess(r) for r in rows]


def count_guesses_for_round(conn: sqlite3.Connection, round_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM veil_guesses WHERE round_id = ?", (round_id,)
    ).fetchone()[0]


def count_unique_guessers_for_round(conn: sqlite3.Connection, round_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(DISTINCT guesser_id) FROM veil_guesses WHERE round_id = ?", (round_id,)
    ).fetchone()[0]


def get_last_guess_by_user_for_round(
    conn: sqlite3.Connection, round_id: int, guesser_id: int
) -> VeilGuess | None:
    row = conn.execute(
        """
        SELECT * FROM veil_guesses
        WHERE round_id = ? AND guesser_id = ?
        ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (round_id, guesser_id),
    ).fetchone()
    return _row_to_guess(row) if row else None


def _row_to_optin(row: sqlite3.Row) -> VeilOptin:
    return VeilOptin(user_id=row["user_id"], guild_id=row["guild_id"], opted_in_at=row["opted_in_at"])


def upsert_optin(conn: sqlite3.Connection, user_id: int, guild_id: int) -> None:
    conn.execute(
        """
        INSERT INTO veil_optins (user_id, guild_id, opted_in_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id, guild_id) DO UPDATE SET opted_in_at = excluded.opted_in_at
        """,
        (user_id, guild_id, time.time()),
    )


def delete_optin(conn: sqlite3.Connection, user_id: int, guild_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM veil_optins WHERE user_id = ? AND guild_id = ?", (user_id, guild_id)
    )
    return (cur.rowcount or 0) > 0


def get_optin(conn: sqlite3.Connection, user_id: int, guild_id: int) -> VeilOptin | None:
    row = conn.execute(
        "SELECT * FROM veil_optins WHERE user_id = ? AND guild_id = ?", (user_id, guild_id)
    ).fetchone()
    return _row_to_optin(row) if row else None


def is_opted_in(conn: sqlite3.Connection, user_id: int, guild_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM veil_optins WHERE user_id = ? AND guild_id = ?", (user_id, guild_id)
    ).fetchone() is not None


def get_all_optins_for_guild(conn: sqlite3.Connection, guild_id: int) -> list[VeilOptin]:
    rows = conn.execute(
        "SELECT * FROM veil_optins WHERE guild_id = ? ORDER BY opted_in_at ASC", (guild_id,)
    ).fetchall()
    return [_row_to_optin(r) for r in rows]


def get_all_active_round_ids(conn: sqlite3.Connection) -> list[tuple[int, bool]]:
    rows = conn.execute(
        "SELECT id, solved_at IS NOT NULL AS solved FROM veil_rounds WHERE deleted_at IS NULL"
    ).fetchall()
    return [(row["id"], bool(row["solved"])) for row in rows]
