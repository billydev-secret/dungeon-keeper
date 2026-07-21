"""Unit tests for the 24 h ticket auto-delete query.

``get_tickets_to_autodelete`` is the only automated proof that the hourly
sweep (``ticket_autodelete_loop`` in ``bot_modules.commands.jail_commands``)
selects the right rows: closed tickets whose close time is at or before the
cutoff, excluding reopened, still-open, and already-deleted tickets.
"""

from __future__ import annotations

from bot_modules.core.db_utils import open_db
from bot_modules.services.moderation import (
    close_ticket,
    create_ticket,
    get_tickets_to_autodelete,
    reopen_ticket,
)


def _new_ticket(conn, *, user_id: int, desc: str) -> int:
    return create_ticket(
        conn, guild_id=123, user_id=user_id, channel_id=0, description=desc
    )


def test_only_old_closed_tickets_are_returned(sync_db_path):
    with open_db(sync_db_path) as conn:
        old = _new_ticket(conn, user_id=1, desc="old")
        recent = _new_ticket(conn, user_id=2, desc="recent")
        still_open = _new_ticket(conn, user_id=3, desc="open")  # noqa: F841
        close_ticket(conn, old, closed_by=99, reason="done")
        close_ticket(conn, recent, closed_by=99, reason="done")
        # Pin closed_at deterministically (close_ticket stamps time.time()).
        conn.execute("UPDATE tickets SET closed_at = 1000 WHERE id = ?", (old,))
        conn.execute("UPDATE tickets SET closed_at = 5000 WHERE id = ?", (recent,))

    with open_db(sync_db_path) as conn:
        due = get_tickets_to_autodelete(conn, closed_before=2000)

    # recent (5000 > 2000) and the never-closed ticket (closed_at IS NULL) are
    # both excluded.
    assert [t["id"] for t in due] == [old]


def test_cutoff_boundary_is_inclusive(sync_db_path):
    with open_db(sync_db_path) as conn:
        tid = _new_ticket(conn, user_id=1, desc="boundary")
        close_ticket(conn, tid, closed_by=99, reason="done")
        conn.execute("UPDATE tickets SET closed_at = 2000 WHERE id = ?", (tid,))

    with open_db(sync_db_path) as conn:
        due = get_tickets_to_autodelete(conn, closed_before=2000)

    # closed_at == cutoff qualifies (query uses closed_at <= ?).
    assert [t["id"] for t in due] == [tid]


def test_reopened_ticket_is_excluded(sync_db_path):
    with open_db(sync_db_path) as conn:
        tid = _new_ticket(conn, user_id=1, desc="reopened")
        close_ticket(conn, tid, closed_by=99, reason="done")
        conn.execute("UPDATE tickets SET closed_at = 1000 WHERE id = ?", (tid,))
        # Reopen flips status back to 'open' and clears closed_at, so the
        # 24 h countdown restarts and this ticket drops out of the sweep.
        reopen_ticket(conn, tid)

    with open_db(sync_db_path) as conn:
        due = get_tickets_to_autodelete(conn, closed_before=999_999)

    assert due == []


def test_deleted_ticket_is_excluded(sync_db_path):
    with open_db(sync_db_path) as conn:
        tid = _new_ticket(conn, user_id=1, desc="deleted")
        close_ticket(conn, tid, closed_by=99, reason="done")
        conn.execute(
            "UPDATE tickets SET closed_at = 1000, status = 'deleted' WHERE id = ?",
            (tid,),
        )

    with open_db(sync_db_path) as conn:
        due = get_tickets_to_autodelete(conn, closed_before=999_999)

    assert due == []


def test_results_are_ordered_by_close_time(sync_db_path):
    with open_db(sync_db_path) as conn:
        first = _new_ticket(conn, user_id=1, desc="a")
        second = _new_ticket(conn, user_id=2, desc="b")
        third = _new_ticket(conn, user_id=3, desc="c")
        for t in (first, second, third):
            close_ticket(conn, t, closed_by=99, reason="done")
        # Close times out of insertion order to prove ORDER BY closed_at.
        conn.execute("UPDATE tickets SET closed_at = 3000 WHERE id = ?", (first,))
        conn.execute("UPDATE tickets SET closed_at = 1000 WHERE id = ?", (second,))
        conn.execute("UPDATE tickets SET closed_at = 2000 WHERE id = ?", (third,))

    with open_db(sync_db_path) as conn:
        due = get_tickets_to_autodelete(conn, closed_before=5000)

    assert [t["id"] for t in due] == [second, third, first]
