"""Guess cog — DB query layer (sync sqlite3)."""
from __future__ import annotations

import json
import sqlite3
import time

from bot_modules.core.db_utils import get_config_value, set_config_value
from bot_modules.services.guess_models import (
    GuessAuditEvent,
    GuessConfig,
    GuessGuess,
    GuessRound,
)

_CONFIG_DEFAULTS: dict[str, str] = {
    "guess_role_id": "0",
    "guess_channel_id": "0",
    "guess_guess_cooldown_seconds": "60",
    "guess_crop_difficulty": "medium",
    "guess_min_image_dimension_px": "400",
    "guess_max_image_size_mb": "10",
    "guess_prompt_message_id": "0",
    "guess_prompt_channel_id": "0",
    "guess_submit_max_per_window": "5",
    "guess_submit_window_seconds": "3600",
    "guess_max_guesses_per_round": "5",
    "guess_inactivity_ping_hours": "0",
}


def get_guess_config(conn: sqlite3.Connection, guild_id: int) -> GuessConfig:
    def _get(key: str) -> str:
        return get_config_value(conn, key, _CONFIG_DEFAULTS[key], guild_id)

    return GuessConfig(
        guild_id=guild_id,
        guess_role_id=int(_get("guess_role_id") or 0),
        guess_channel_id=int(_get("guess_channel_id") or 0),
        guess_cooldown_seconds=int(_get("guess_guess_cooldown_seconds") or 60),
        crop_difficulty=_get("guess_crop_difficulty"),
        min_image_dimension_px=int(_get("guess_min_image_dimension_px") or 400),
        max_image_size_mb=int(_get("guess_max_image_size_mb") or 10),
        prompt_message_id=int(_get("guess_prompt_message_id") or 0),
        prompt_channel_id=int(_get("guess_prompt_channel_id") or 0),
        submit_max_per_window=int(_get("guess_submit_max_per_window") or 5),
        submit_window_seconds=int(_get("guess_submit_window_seconds") or 3600),
        max_guesses_per_round=int(_get("guess_max_guesses_per_round") or 5),
        inactivity_ping_hours=int(_get("guess_inactivity_ping_hours") or 0),
    )


def set_guess_config_value(
    conn: sqlite3.Connection, guild_id: int, key: str, value: str
) -> None:
    """key is the full config key, e.g. 'guess_crop_difficulty'."""
    set_config_value(conn, key, value, guild_id)


def _row_to_round(row: sqlite3.Row) -> GuessRound:
    return GuessRound(
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
        created_at=row["created_at"],
        solved_at=row["solved_at"],
        solver_id=row["solver_id"],
        guesses_to_solve=row["guesses_to_solve"],
        unique_guessers_to_solve=row["unique_guessers_to_solve"],
        answer_optout=bool(row["answer_optout"]),
        deleted_at=row["deleted_at"],
        crop_box_x1=row["crop_box_x1"],
        crop_box_y1=row["crop_box_y1"],
        crop_box_x2=row["crop_box_x2"],
        crop_box_y2=row["crop_box_y2"],
        round_type=row["round_type"],
        confession_text=row["confession_text"],
        confession_prompt_text=row["confession_prompt_text"],
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
    round_type: str = "photo",
    confession_text: str = "",
    confession_prompt_text: str = "",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO guess_rounds
            (guild_id, submitter_id, answer_id, channel_id, message_id,
             crop_path, crop_url, difficulty, candidate_count, created_at,
             round_type, confession_text, confession_prompt_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (guild_id, submitter_id, answer_id, channel_id, message_id,
         crop_path, crop_url, difficulty, candidate_count, time.time(),
         round_type, confession_text, confession_prompt_text),
    )
    return cur.lastrowid  # type: ignore[return-value]


def get_round(conn: sqlite3.Connection, round_id: int) -> GuessRound | None:
    row = conn.execute("SELECT * FROM guess_rounds WHERE id = ?", (round_id,)).fetchone()
    return _row_to_round(row) if row else None


def update_round_message(
    conn: sqlite3.Connection, round_id: int, *, message_id: int, crop_url: str, crop_path: str
) -> None:
    conn.execute(
        "UPDATE guess_rounds SET message_id = ?, crop_url = ?, crop_path = ? WHERE id = ?",
        (message_id, crop_url, crop_path, round_id),
    )


def set_round_crop_box(
    conn: sqlite3.Connection, round_id: int, x1: float, y1: float, x2: float, y2: float
) -> None:
    conn.execute(
        "UPDATE guess_rounds SET crop_box_x1=?, crop_box_y1=?, crop_box_x2=?, crop_box_y2=? WHERE id=?",
        (x1, y1, x2, y2, round_id),
    )


def set_round_original_path(
    conn: sqlite3.Connection, round_id: int, original_path: str
) -> None:
    conn.execute(
        "UPDATE guess_rounds SET original_path = ? WHERE id = ?",
        (original_path, round_id),
    )


def list_stale_open_originals(
    conn: sqlite3.Connection, *, older_than: float
) -> list[tuple[int, str]]:
    """Open (unsolved, undeleted) rounds created before *older_than* whose
    original submission is still cached on disk.

    Solved rounds delete their original at solve time; this is the age-out
    for rounds nobody ever solves, so a member's uncropped submission
    doesn't sit in ``guess_cache/`` forever (2026-08 review, guess G2)."""
    rows = conn.execute(
        """
        SELECT id, original_path FROM guess_rounds
         WHERE solved_at IS NULL AND deleted_at IS NULL
           AND original_path IS NOT NULL AND original_path != ''
           AND created_at < ?
        """,
        (older_than,),
    ).fetchall()
    return [(row["id"], row["original_path"]) for row in rows]


def clear_round_original_path(conn: sqlite3.Connection, round_id: int) -> None:
    conn.execute(
        "UPDATE guess_rounds SET original_path = '' WHERE id = ?", (round_id,)
    )


def mark_round_solved(
    conn: sqlite3.Connection, round_id: int,
    *, solver_id: int, guesses_to_solve: int, unique_guessers_to_solve: int
) -> int:
    """Mark the round solved iff it is currently unsolved. Returns rowcount.

    The ``solved_at IS NULL`` predicate is the race guard for two near-
    simultaneous correct guesses — only the winner sees rowcount == 1.
    """
    cur = conn.execute(
        """
        UPDATE guess_rounds
        SET solved_at = ?, solver_id = ?,
            guesses_to_solve = ?, unique_guessers_to_solve = ?
        WHERE id = ? AND solved_at IS NULL
        """,
        (time.time(), solver_id, guesses_to_solve, unique_guessers_to_solve, round_id),
    )
    return cur.rowcount or 0


def soft_delete_round(conn: sqlite3.Connection, round_id: int) -> None:
    conn.execute(
        "UPDATE guess_rounds SET deleted_at = ? WHERE id = ?", (time.time(), round_id)
    )


def set_round_answer_optout(conn: sqlite3.Connection, round_id: int) -> None:
    conn.execute("UPDATE guess_rounds SET answer_optout = 1 WHERE id = ?", (round_id,))


def set_round_reroll_count(conn: sqlite3.Connection, round_id: int, reroll_count: int) -> None:
    conn.execute(
        "UPDATE guess_rounds SET reroll_count = ? WHERE id = ?", (reroll_count, round_id)
    )


def get_active_rounds_for_guild(
    conn: sqlite3.Connection, guild_id: int, *, limit: int = 50
) -> list[GuessRound]:
    rows = conn.execute(
        """
        SELECT * FROM guess_rounds
        WHERE guild_id = ? AND deleted_at IS NULL
        ORDER BY created_at DESC LIMIT ?
        """,
        (guild_id, limit),
    ).fetchall()
    return [_row_to_round(r) for r in rows]


def _row_to_guess(row: sqlite3.Row) -> GuessGuess:
    return GuessGuess(
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
        INSERT INTO guess_guesses (round_id, guesser_id, guessed_user_id, correct, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (round_id, guesser_id, guessed_user_id, int(correct), time.time()),
    )
    return cur.lastrowid  # type: ignore[return-value]


def get_guesses_for_round(conn: sqlite3.Connection, round_id: int) -> list[GuessGuess]:
    rows = conn.execute(
        "SELECT * FROM guess_guesses WHERE round_id = ? ORDER BY created_at ASC", (round_id,)
    ).fetchall()
    return [_row_to_guess(r) for r in rows]


def count_guesses_for_round(conn: sqlite3.Connection, round_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM guess_guesses WHERE round_id = ?", (round_id,)
    ).fetchone()[0]


def count_unique_guessers_for_round(conn: sqlite3.Connection, round_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(DISTINCT guesser_id) FROM guess_guesses WHERE round_id = ?", (round_id,)
    ).fetchone()[0]


def get_last_guess_by_user_for_round(
    conn: sqlite3.Connection, round_id: int, guesser_id: int
) -> GuessGuess | None:
    row = conn.execute(
        """
        SELECT * FROM guess_guesses
        WHERE round_id = ? AND guesser_id = ?
        ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (round_id, guesser_id),
    ).fetchone()
    return _row_to_guess(row) if row else None


def count_user_guesses_for_round(
    conn: sqlite3.Connection, round_id: int, guesser_id: int
) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM guess_guesses WHERE round_id = ? AND guesser_id = ?",
        (round_id, guesser_id),
    ).fetchone()[0]


def insert_audit_event(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    actor_id: int,
    action: str,
    round_id: int | None = None,
    details: dict | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO guess_audit_log (guild_id, ts, actor_id, action, round_id, details)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            guild_id,
            time.time(),
            actor_id,
            action,
            round_id,
            json.dumps(details or {}),
        ),
    )
    return cur.lastrowid  # type: ignore[return-value]


def list_audit_events(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    limit: int = 100,
    action: str | None = None,
) -> list[GuessAuditEvent]:
    if action is None:
        rows = conn.execute(
            """
            SELECT * FROM guess_audit_log WHERE guild_id = ?
            ORDER BY ts DESC, id DESC LIMIT ?
            """,
            (guild_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM guess_audit_log WHERE guild_id = ? AND action = ?
            ORDER BY ts DESC, id DESC LIMIT ?
            """,
            (guild_id, action, limit),
        ).fetchall()
    return [
        GuessAuditEvent(
            id=row["id"],
            guild_id=row["guild_id"],
            ts=row["ts"],
            actor_id=row["actor_id"],
            action=row["action"],
            round_id=row["round_id"],
            details=row["details"],
        )
        for row in rows
    ]


def get_all_active_round_ids(conn: sqlite3.Connection) -> list[tuple[int, bool]]:
    rows = conn.execute(
        "SELECT id, solved_at IS NOT NULL AS solved FROM guess_rounds WHERE deleted_at IS NULL"
    ).fetchall()
    return [(row["id"], bool(row["solved"])) for row in rows]


def get_unsolved_round_ids(
    conn: sqlite3.Connection, *, limit: int = 1000
) -> list[int]:
    """Round IDs that still need a persistent GameView at startup.

    Solved rounds are skipped — their 'Guess late' button is fun-loop polish,
    not load-bearing. Newest-first so the cap takes the most recently active.
    """
    rows = conn.execute(
        """
        SELECT id FROM guess_rounds
        WHERE deleted_at IS NULL AND solved_at IS NULL
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [row["id"] for row in rows]


def get_top_posters(
    conn: sqlite3.Connection, guild_id: int, *, limit: int = 5
) -> list[tuple[int, int, int]]:
    """Return (submitter_id, rounds_posted, rounds_solved) ordered by rounds_posted desc."""
    rows = conn.execute(
        """
        SELECT submitter_id,
               COUNT(*) AS rounds_posted,
               SUM(CASE WHEN solved_at IS NOT NULL THEN 1 ELSE 0 END) AS rounds_solved
        FROM guess_rounds
        WHERE guild_id = ? AND deleted_at IS NULL
        GROUP BY submitter_id
        ORDER BY rounds_posted DESC, rounds_solved DESC
        LIMIT ?
        """,
        (guild_id, limit),
    ).fetchall()
    return [(row["submitter_id"], row["rounds_posted"], row["rounds_solved"]) for row in rows]


def get_top_guessers(
    conn: sqlite3.Connection, guild_id: int, *, limit: int = 5
) -> list[tuple[int, int]]:
    """Return (solver_id, rounds_solved) ordered by rounds_solved desc."""
    rows = conn.execute(
        """
        SELECT solver_id,
               COUNT(*) AS rounds_solved
        FROM guess_rounds
        WHERE guild_id = ? AND deleted_at IS NULL AND solver_id IS NOT NULL
        GROUP BY solver_id
        ORDER BY rounds_solved DESC
        LIMIT ?
        """,
        (guild_id, limit),
    ).fetchall()
    return [(row["solver_id"], row["rounds_solved"]) for row in rows]


def flag_user_open_rounds_optout(
    conn: sqlite3.Connection, *, guild_id: int, user_id: int
) -> int:
    """Mark every open (unsolved, undeleted) round in this guild where *user_id*
    is the answer as ``answer_optout=True``. Returns the number of rounds flagged.

    Called when a user loses the Guess role — their consent has been revoked,
    so existing rounds where they're the answer should become unsolvable.
    Already-flagged rounds are unaffected.
    """
    cur = conn.execute(
        """
        UPDATE guess_rounds
        SET answer_optout = 1
        WHERE guild_id = ? AND answer_id = ?
          AND solved_at IS NULL AND deleted_at IS NULL AND answer_optout = 0
        """,
        (guild_id, user_id),
    )
    return cur.rowcount or 0


# Bump when the Guess consent disclosure changes materially (what is stored,
# for how long, or who can see it). Rows carrying an older version recorded
# agreement to wording the member never saw — which is the whole point of
# storing it, and is not reconstructable after the fact.
GUESS_DISCLOSURE_VERSION = 1


def record_guess_consent(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    user_id: int,
    consented_at: float,
    disclosure_version: int = GUESS_DISCLOSURE_VERSION,
) -> None:
    """Record that *user_id* agreed to the Guess disclosure (GDPR Art 7(1)).

    Art 7(1) puts the burden on the controller to *demonstrate* consent. The
    role alone shows only that someone holds it — not what they were shown or
    when — so the evidence has to be written at the moment of the click.
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO guess_consents
            (guild_id, user_id, consented_at, disclosure_version, withdrawn_at)
        VALUES (?, ?, ?, ?, NULL)
        """,
        (guild_id, user_id, consented_at, disclosure_version),
    )


def withdraw_guess_consent(
    conn: sqlite3.Connection, *, guild_id: int, user_id: int, withdrawn_at: float
) -> int:
    """Stamp the member's outstanding consent rows as withdrawn.

    The rows are stamped, not deleted: Art 7(3) makes withdrawal as easy as
    giving consent, but the record that consent *was* held — and for how long —
    is what makes past processing defensible. A full erasure removes them.
    """
    cur = conn.execute(
        """
        UPDATE guess_consents SET withdrawn_at = ?
        WHERE guild_id = ? AND user_id = ? AND withdrawn_at IS NULL
        """,
        (withdrawn_at, guild_id, user_id),
    )
    return cur.rowcount or 0


def get_guess_consent(
    conn: sqlite3.Connection, *, guild_id: int, user_id: int
) -> sqlite3.Row | None:
    """Most recent consent row for the member, withdrawn or not."""
    return conn.execute(
        """
        SELECT * FROM guess_consents
        WHERE guild_id = ? AND user_id = ?
        ORDER BY consented_at DESC LIMIT 1
        """,
        (guild_id, user_id),
    ).fetchone()
