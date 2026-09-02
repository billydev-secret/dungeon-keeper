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


# ---------------------------------------------------------------------------
# bulk_upsert_labels — the "rip through it real fast" path
# ---------------------------------------------------------------------------

OTHER_GUILD = 2


def test_bulk_label_happy_path(db_path):
    """All ids exist and belong to the caller's guild: every one is labelled
    and reported in ``labeled``, none in ``skipped``."""
    with open_db(db_path) as conn:
        ids = [_event(conn) for _ in range(3)]
        result = service.bulk_upsert_labels(
            conn, GUILD, ids, is_violation=False, labeled_by=42
        )
        rows = [_labelled(conn, eid) for eid in ids]

    assert result == {"labeled": ids, "skipped": []}
    assert all(row["is_violation"] == 0 for row in rows)


def test_bulk_label_mixed_batch_reports_missing_ids(db_path):
    """A batch mixing real ids with one that was never inserted labels the
    real ones and reports the phantom id back under ``skipped`` rather than
    failing the whole call."""
    with open_db(db_path) as conn:
        real_ids = [_event(conn) for _ in range(2)]
        phantom_id = max(real_ids) + 1000
        result = service.bulk_upsert_labels(
            conn,
            GUILD,
            [*real_ids, phantom_id],
            is_violation=True,
            corrected_rule="7",
        )
        rows = [_labelled(conn, eid) for eid in real_ids]

    assert result == {"labeled": real_ids, "skipped": [phantom_id]}
    assert all(row["is_violation"] == 1 and row["corrected_rule"] == "7" for row in rows)


def test_bulk_relabel_overwrites_an_existing_label(db_path):
    """An event labelled singly, then swept up in a later bulk pass, ends up
    holding the bulk call's values — same upsert semantics as a lone
    ``upsert_label`` call, just reached through the batch path."""
    with open_db(db_path) as conn:
        event_id = _event(conn)
        service.upsert_label(conn, event_id, is_violation=True, corrected_rule="7")
        result = service.bulk_upsert_labels(
            conn, GUILD, [event_id], is_violation=False
        )
        row = _labelled(conn, event_id)

    assert result == {"labeled": [event_id], "skipped": []}
    assert row["is_violation"] == 0
    assert row["corrected_rule"] is None


def test_bulk_label_skips_a_foreign_guild_event(db_path):
    """The core safety gate: an id that exists but belongs to a different
    guild is refused, not labelled, and reported back — a moderator of one
    guild cannot use the bulk path to write another guild's event."""
    with open_db(db_path) as conn:
        home_id = _event(conn)
        foreign_id = service.insert_event(
            conn,
            guild_id=OTHER_GUILD,
            message_id=2000,
            author_id=99,
            channel_id=CHANNEL,
            guard_verdict="violation",
            guard_rule="3",
        )
        result = service.bulk_upsert_labels(
            conn, GUILD, [home_id, foreign_id], is_violation=False
        )
        home_row = _labelled(conn, home_id)
        foreign_row = _labelled(conn, foreign_id)

    assert result == {"labeled": [home_id], "skipped": [foreign_id]}
    assert home_row["is_violation"] == 0
    # Untouched: no label row was ever written for the other guild's event.
    assert foreign_row["is_violation"] is None


def test_bulk_label_rejects_a_batch_over_the_cap(db_path):
    """The batch is bounded — the panel is expected to send exactly the ids
    it is showing, never an unbounded 'everything matching the filter'."""
    with open_db(db_path) as conn:
        oversized = list(range(1, service.MAX_BULK_LABEL + 2))
        with pytest.raises(ValueError):
            service.bulk_upsert_labels(conn, GUILD, oversized, is_violation=False)


def test_bulk_label_empty_list_is_a_no_op(db_path):
    with open_db(db_path) as conn:
        result = service.bulk_upsert_labels(conn, GUILD, [], is_violation=False)

    assert result == {"labeled": [], "skipped": []}


# ---------------------------------------------------------------------------
# get_event guild scoping — the single-event path's own safety gate
# ---------------------------------------------------------------------------


def test_get_event_returns_an_event_of_the_callers_guild(db_path):
    """The ordinary case: a moderator reads an event in the guild they are
    actually viewing."""
    with open_db(db_path) as conn:
        event_id = _event(conn)
        row = service.get_event(conn, event_id, GUILD)

    assert row is not None
    assert int(row["id"]) == event_id


def test_get_event_refuses_a_foreign_guild_event(db_path):
    """The safety gate. ``get_event`` used to select on id alone, so a
    moderator of one guild could read another guild's event detail — message,
    author and channel ids, the guard verdict, the priority score — simply by
    knowing the id, and could write a label onto it too, because the label
    route looks the event up through this same function and 404s only when it
    comes back None. Scoping the lookup closes the read and the write at once.
    """
    with open_db(db_path) as conn:
        foreign_id = service.insert_event(
            conn,
            guild_id=OTHER_GUILD,
            message_id=2000,
            author_id=99,
            channel_id=CHANNEL,
            guard_verdict="violation",
            guard_rule="3",
        )
        row = service.get_event(conn, foreign_id, GUILD)

    assert row is None
