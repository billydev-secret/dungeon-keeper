"""Whisper cog — DB query layer (sync sqlite3)."""
from __future__ import annotations

import sqlite3
import time

from db_utils import get_config_value, set_config_value
from services.whisper_models import Whisper, WhisperConfig, WhisperGuess, WhisperState

_CONFIG_DEFAULTS: dict[str, str] = {
    "whisper_role_id": "0",
    "whisper_channel_id": "0",
    "whisper_log_channel_id": "0",
}


def get_whisper_config(conn: sqlite3.Connection, guild_id: int) -> WhisperConfig:
    def _get(key: str) -> str:
        return get_config_value(conn, key, _CONFIG_DEFAULTS[key], guild_id)

    return WhisperConfig(
        guild_id=guild_id,
        role_id=int(_get("whisper_role_id") or 0),
        channel_id=int(_get("whisper_channel_id") or 0),
        log_channel_id=int(_get("whisper_log_channel_id") or 0),
    )


def set_whisper_config_value(
    conn: sqlite3.Connection, guild_id: int, key: str, value: str
) -> None:
    """key is the full config key, e.g. 'whisper_channel_id'."""
    set_config_value(conn, key, value, guild_id)


def _row_to_whisper(row: sqlite3.Row) -> Whisper:
    return Whisper(
        id=row["id"],
        guild_id=row["guild_id"],
        sender_id=row["sender_id"],
        target_id=row["target_id"],
        message=row["message"],
        created_at=row["created_at"],
        state=row["state"],
        solved=bool(row["solved"]),
        exposed=bool(row["exposed"]),
        guesses_left=row["guesses_left"],
        channel_msg_id=row["channel_msg_id"],
        dm_msg_id=row["dm_msg_id"],
    )


def insert_whisper(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    sender_id: int,
    target_id: int,
    message: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO whispers
            (guild_id, sender_id, target_id, message, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (guild_id, sender_id, target_id, message, time.time()),
    )
    return cur.lastrowid  # type: ignore[return-value]


def get_whisper(conn: sqlite3.Connection, whisper_id: int) -> Whisper | None:
    row = conn.execute(
        "SELECT * FROM whispers WHERE id = ?", (whisper_id,)
    ).fetchone()
    return _row_to_whisper(row) if row else None


def delete_whisper(conn: sqlite3.Connection, whisper_id: int) -> None:
    conn.execute("DELETE FROM whispers WHERE id = ?", (whisper_id,))


def set_whisper_message_ids(
    conn: sqlite3.Connection,
    whisper_id: int,
    *,
    channel_msg_id: int,
    dm_msg_id: int,
) -> None:
    conn.execute(
        "UPDATE whispers SET channel_msg_id = ?, dm_msg_id = ? WHERE id = ?",
        (channel_msg_id, dm_msg_id, whisper_id),
    )


def update_whisper_state(
    conn: sqlite3.Connection, whisper_id: int, new_state: WhisperState
) -> None:
    conn.execute(
        "UPDATE whispers SET state = ? WHERE id = ?", (new_state, whisper_id)
    )


def _row_to_guess(row: sqlite3.Row) -> WhisperGuess:
    return WhisperGuess(
        id=row["id"],
        whisper_id=row["whisper_id"],
        guessed_id=row["guessed_id"],
        correct=bool(row["correct"]),
        created_at=row["created_at"],
    )


def insert_guess(
    conn: sqlite3.Connection,
    *,
    whisper_id: int,
    guessed_id: int,
    correct: bool,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO whisper_guesses (whisper_id, guessed_id, correct, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (whisper_id, guessed_id, int(correct), time.time()),
    )
    return cur.lastrowid  # type: ignore[return-value]


def list_guesses(conn: sqlite3.Connection, *, whisper_id: int) -> list[WhisperGuess]:
    rows = conn.execute(
        "SELECT * FROM whisper_guesses WHERE whisper_id = ? ORDER BY created_at",
        (whisper_id,),
    ).fetchall()
    return [_row_to_guess(r) for r in rows]


def decrement_guesses_left(conn: sqlite3.Connection, whisper_id: int) -> None:
    conn.execute(
        "UPDATE whispers SET guesses_left = guesses_left - 1 WHERE id = ?",
        (whisper_id,),
    )


def mark_solved(conn: sqlite3.Connection, whisper_id: int) -> None:
    conn.execute("UPDATE whispers SET solved = 1 WHERE id = ?", (whisper_id,))


def mark_exposed(conn: sqlite3.Connection, whisper_id: int) -> None:
    conn.execute("UPDATE whispers SET exposed = 1 WHERE id = ?", (whisper_id,))


def list_received(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    target_id: int,
    state: WhisperState,
) -> list[Whisper]:
    rows = conn.execute(
        """
        SELECT * FROM whispers
        WHERE guild_id = ? AND target_id = ? AND state = ?
        ORDER BY created_at DESC
        """,
        (guild_id, target_id, state),
    ).fetchall()
    return [_row_to_whisper(r) for r in rows]
