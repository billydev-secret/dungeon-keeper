"""Whisper cog — DB query layer (sync sqlite3)."""
from __future__ import annotations

import sqlite3
import time

from bot_modules.core.db_utils import get_config_value, set_config_value
from bot_modules.services.whisper_models import (
    Whisper,
    WhisperConfig,
    WhisperGuess,
    WhisperReply,
    WhisperState,
)

_CONFIG_DEFAULTS: dict[str, str] = {
    "whisper_role_id": "0",
    "whisper_channel_id": "0",
    "whisper_log_channel_id": "0",
    "whisper_launcher_message_id": "0",
}


def get_whisper_config(conn: sqlite3.Connection, guild_id: int) -> WhisperConfig:
    def _get(key: str) -> str:
        return get_config_value(conn, key, _CONFIG_DEFAULTS[key], guild_id)

    return WhisperConfig(
        guild_id=guild_id,
        role_id=int(_get("whisper_role_id") or 0),
        channel_id=int(_get("whisper_channel_id") or 0),
        log_channel_id=int(_get("whisper_log_channel_id") or 0),
        launcher_message_id=int(_get("whisper_launcher_message_id") or 0),
    )


def set_whisper_config_value(
    conn: sqlite3.Connection, guild_id: int, key: str, value: str
) -> None:
    """key is the full config key, e.g. 'whisper_channel_id'."""
    set_config_value(conn, key, value, guild_id)


def set_whisper_launcher_message_id(
    conn: sqlite3.Connection, guild_id: int, message_id: int
) -> None:
    set_config_value(conn, "whisper_launcher_message_id", str(message_id), guild_id)


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
        deleted_at=row["deleted_at"],
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


def try_consume_guess(conn: sqlite3.Connection, whisper_id: int) -> bool:
    """Atomically decrement guesses_left; returns True if succeeded (>0 attempts remained and not yet solved)."""
    cur = conn.execute(
        "UPDATE whispers SET guesses_left = guesses_left - 1 WHERE id = ? AND guesses_left > 0 AND solved = 0",
        (whisper_id,),
    )
    return cur.rowcount == 1


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
    """Active (non-deleted) whispers received by target_id with this state."""
    rows = conn.execute(
        """
        SELECT * FROM whispers
        WHERE guild_id = ? AND target_id = ? AND state = ? AND deleted_at IS NULL
        ORDER BY created_at DESC
        """,
        (guild_id, target_id, state),
    ).fetchall()
    return [_row_to_whisper(r) for r in rows]


def list_received_in_states(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    target_id: int,
    states: list[WhisperState],
) -> list[Whisper]:
    """Active (non-deleted) whispers received by target_id whose state is in states."""
    if not states:
        return []
    placeholders = ",".join("?" * len(states))
    rows = conn.execute(
        f"""
        SELECT * FROM whispers
        WHERE guild_id = ? AND target_id = ? AND state IN ({placeholders})
              AND deleted_at IS NULL
        ORDER BY created_at DESC
        """,
        (guild_id, target_id, *states),
    ).fetchall()
    return [_row_to_whisper(r) for r in rows]


def list_sent(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    sender_id: int,
) -> list[Whisper]:
    """All whispers sent by sender_id (including target-deleted; sender sees their own copy)."""
    rows = conn.execute(
        """
        SELECT * FROM whispers
        WHERE guild_id = ? AND sender_id = ?
        ORDER BY created_at DESC
        """,
        (guild_id, sender_id),
    ).fetchall()
    return [_row_to_whisper(r) for r in rows]


def soft_delete_whisper(
    conn: sqlite3.Connection, whisper_id: int, *, now: float | None = None
) -> None:
    """Mark whisper as soft-deleted (target removed from inbox). No-op if already deleted."""
    ts = now if now is not None else time.time()
    conn.execute(
        "UPDATE whispers SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
        (ts, whisper_id),
    )


def count_replies(conn: sqlite3.Connection, whisper_id: int) -> int:
    """Count replies for a whisper (drives the one-reply cap)."""
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM whisper_replies WHERE whisper_id = ?",
        (whisper_id,),
    ).fetchone()
    return int(row["c"])


def _row_to_reply(row: sqlite3.Row) -> WhisperReply:
    return WhisperReply(
        id=row["id"],
        whisper_id=row["whisper_id"],
        from_user_id=row["from_user_id"],
        to_user_id=row["to_user_id"],
        content=row["content"],
        created_at=row["created_at"],
    )


def insert_reply(
    conn: sqlite3.Connection,
    *,
    whisper_id: int,
    from_user_id: int,
    to_user_id: int,
    content: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO whisper_replies
            (whisper_id, from_user_id, to_user_id, content, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (whisper_id, from_user_id, to_user_id, content, time.time()),
    )
    return cur.lastrowid  # type: ignore[return-value]


def get_reply(conn: sqlite3.Connection, reply_id: int) -> WhisperReply | None:
    row = conn.execute(
        "SELECT * FROM whisper_replies WHERE id = ?", (reply_id,)
    ).fetchone()
    return _row_to_reply(row) if row else None


def delete_reply(conn: sqlite3.Connection, reply_id: int) -> None:
    conn.execute("DELETE FROM whisper_replies WHERE id = ?", (reply_id,))


def insert_report(
    conn: sqlite3.Connection,
    *,
    whisper_id: int,
    reporter_id: int,
    reason: str,
) -> bool:
    """Insert a report; returns True if inserted, False if duplicate (same reporter)."""
    try:
        conn.execute(
            "INSERT INTO whisper_reports (whisper_id, reporter_id, reason, created_at) VALUES (?, ?, ?, ?)",
            (whisper_id, reporter_id, reason, time.time()),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def insert_reply_report(
    conn: sqlite3.Connection,
    *,
    reply_id: int,
    reporter_id: int,
    reason: str,
) -> bool:
    """Insert a reply report; returns True if inserted, False if duplicate."""
    try:
        conn.execute(
            "INSERT INTO whisper_reply_reports (reply_id, reporter_id, reason, created_at) VALUES (?, ?, ?, ?)",
            (reply_id, reporter_id, reason, time.time()),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def list_replies_for_whisper(
    conn: sqlite3.Connection, *, whisper_id: int
) -> list[WhisperReply]:
    rows = conn.execute(
        "SELECT * FROM whisper_replies WHERE whisper_id = ? ORDER BY created_at",
        (whisper_id,),
    ).fetchall()
    return [_row_to_reply(r) for r in rows]
