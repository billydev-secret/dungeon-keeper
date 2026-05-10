"""Race-guard tests for mark_round_solved.

The first correct guess wins. A second 'first solve' attempt arriving in flight
must be a no-op (rowcount == 0) so the cog doesn't double-edit the message.
"""
from __future__ import annotations

from pathlib import Path

from db_utils import open_db
from services.veil_repo import insert_round, mark_round_solved


def test_mark_solved_returns_one_on_first_call(sync_db_path: Path) -> None:
    with open_db(sync_db_path) as conn:
        rid = insert_round(conn, guild_id=1, submitter_id=10, answer_id=10)

    with open_db(sync_db_path) as conn:
        rowcount = mark_round_solved(
            conn, rid, solver_id=20, guesses_to_solve=1, unique_guessers_to_solve=1
        )

    assert rowcount == 1


def test_mark_solved_returns_zero_on_already_solved_round(sync_db_path: Path) -> None:
    with open_db(sync_db_path) as conn:
        rid = insert_round(conn, guild_id=1, submitter_id=10, answer_id=10)

    with open_db(sync_db_path) as conn:
        first = mark_round_solved(
            conn, rid, solver_id=20, guesses_to_solve=1, unique_guessers_to_solve=1
        )
    assert first == 1

    with open_db(sync_db_path) as conn:
        second = mark_round_solved(
            conn, rid, solver_id=99, guesses_to_solve=2, unique_guessers_to_solve=2
        )

    # Second call must NOT overwrite the original solver/counts.
    assert second == 0
    with open_db(sync_db_path) as conn:
        row = conn.execute(
            "SELECT solver_id, guesses_to_solve FROM veil_rounds WHERE id = ?", (rid,)
        ).fetchone()
    assert row["solver_id"] == 20
    assert row["guesses_to_solve"] == 1
