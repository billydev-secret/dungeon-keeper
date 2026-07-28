"""Label capture for Rules Watch events, including the corrected rule number.

``corrected_rule`` records "this *is* a violation, but of rule N, not the rule
the guard matched" — the highest-value training signal the system collects,
because it corrects the classifier rather than just confirming it.

It briefly had no writer: the only surface that ever sent one was
``/rules-watch label``, deleted 2026-07-28 on the grounds that the dashboard
covered it, when in fact ``rules-watch.js`` posted only ``is_violation``. The
panel sends it again; these lock the persistence contract that panel depends on,
since nothing reads the column back into the UI to make a break visible.
"""
from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.rules_watch import service
from tests.db_template import migrated_db

GUILD = 1
CHANNEL = 10


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "labels.db"
    migrated_db(path)
    return path


def _event(conn, *, guard_rule: str = "3") -> int:
    return service.insert_event(
        conn,
        guild_id=GUILD,
        message_id=1000,
        author_id=77,
        channel_id=CHANNEL,
        guard_verdict="violation",
        guard_rule=guard_rule,
        priority_score=8.0,
        priority_tier="immediate",
    )



def _labelled(conn, event_id: int):
    """Read an event with its label joined on.

    ``service.get_event`` selects from rules_events alone, so the label columns
    aren't on it; the joined shape is what ``get_all_events`` (and therefore the
    panel) sees.
    """
    return conn.execute(
        """
        SELECT e.*, l.is_violation, l.corrected_rule
        FROM rules_events e
        LEFT JOIN rules_labels l ON l.event_id = e.id
        WHERE e.id = ?
        """,
        (event_id,),
    ).fetchone()


def test_corrected_rule_is_persisted_and_read_back(db_path):
    with open_db(db_path) as conn:
        event_id = _event(conn, guard_rule="3")
        service.upsert_label(
            conn, event_id, is_violation=True, corrected_rule="7", labeled_by=42
        )
        row = _labelled(conn, event_id)

    assert row is not None
    assert row["is_violation"] == 1
    assert row["corrected_rule"] == "7"
    # The guard's original call is retained alongside the correction — the pair
    # is what makes the row useful for tuning.
    assert row["guard_rule"] == "3"


def test_label_without_a_correction_leaves_the_column_null(db_path):
    """The panel omits the field entirely when the mod leaves it blank, rather
    than writing an empty string that would look like a correction to ""."""
    with open_db(db_path) as conn:
        event_id = _event(conn)
        service.upsert_label(conn, event_id, is_violation=True, labeled_by=42)
        row = _labelled(conn, event_id)

    assert row is not None
    assert row["corrected_rule"] is None


def test_relabelling_overwrites_a_previous_correction(db_path):
    """upsert_label is ON CONFLICT DO UPDATE, so a second pass has to replace
    the earlier correction rather than keep the stale one."""
    with open_db(db_path) as conn:
        event_id = _event(conn)
        service.upsert_label(conn, event_id, is_violation=True, corrected_rule="7")
        service.upsert_label(conn, event_id, is_violation=True, corrected_rule="9")
        row = _labelled(conn, event_id)

    assert row is not None
    assert row["corrected_rule"] == "9"


def test_relabelling_as_a_false_positive_clears_the_correction(db_path):
    """A dismissal has no correct rule. The panel drops anything typed when
    False positive is clicked, and the row must follow — a stale rule number
    beside is_violation=0 would poison the training set."""
    with open_db(db_path) as conn:
        event_id = _event(conn)
        service.upsert_label(conn, event_id, is_violation=True, corrected_rule="7")
        service.upsert_label(conn, event_id, is_violation=False)
        row = _labelled(conn, event_id)

    assert row is not None
    assert row["is_violation"] == 0
    assert row["corrected_rule"] is None
