"""The erasure that runs when the bot is removed from a server.

Every test here runs against the **real migrated schema**, not a hand-built
stand-in: the whole point of the service is that it reaches tables nobody
remembered, so a test schema containing only the tables the test knows about
would prove nothing.

The dial is enabled by default in :func:`_conn` for the same reason
``test_retention_service`` does it — a purge test that silently forgot to turn
the feature on would pass without deleting anything.
"""

from __future__ import annotations

import sqlite3

import pytest

from bot_modules.services import guild_purge_service as purge
from tests.db_template import migrated_db

NOW = 2_000_000_000.0
DAY = 86400.0
GUILD = 111
OTHER_GUILD = 222


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(migrated_db(tmp_path / "purge.db"))
    c.row_factory = sqlite3.Row
    try:
        yield c
    finally:
        c.close()


def _set_delay(conn: sqlite3.Connection, value: str | None) -> None:
    conn.execute(
        "DELETE FROM config WHERE guild_id = ? AND key = ?",
        (purge.GLOBAL_GUILD_ID, purge.PURGE_DELAY_CONFIG_KEY),
    )
    if value is not None:
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            (purge.GLOBAL_GUILD_ID, purge.PURGE_DELAY_CONFIG_KEY, value),
        )


def _seed_config(conn: sqlite3.Connection, guild_id: int) -> None:
    conn.execute(
        "INSERT INTO config (guild_id, key, value) VALUES (?, 'seeded', '1')",
        (guild_id,),
    )


def _present_apart_from(conn: sqlite3.Connection, *absent: int) -> set[int]:
    """Every guild the database knows of, minus the ones this test says are gone.

    The migrated template ships config rows for a real guild id, so a test that
    handed reconciliation a bare literal would find that guild queued for
    erasure too — and be asserting against the template, not the behaviour.
    """
    return purge.known_guild_ids(conn) - set(absent)


# ----------------------------------------------------------------- the dial


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        pytest.param(None, None, id="unset-is-off"),
        pytest.param("", None, id="blank-is-off"),
        pytest.param("   ", None, id="whitespace-is-off"),
        pytest.param("soon", None, id="garbage-is-off"),
        pytest.param("-1", None, id="negative-is-off"),
        pytest.param("0", 0, id="zero-means-immediately"),
        pytest.param("30", 30, id="thirty-days"),
        pytest.param(" 7 ", 7, id="padded-value-still-parses"),
    ],
)
def test_purge_delay_days(conn, stored, expected):
    """Anything that is not a non-negative integer reads as off.

    The fail-safe direction matters here in a way it usually doesn't: the
    other reading of a malformed value is "delete a server's data now".
    """
    _set_delay(conn, stored)
    assert purge.purge_delay_days(conn) == expected


def test_dial_ships_off(conn):
    """A freshly migrated database purges nothing. This is the dark default."""
    assert purge.purge_delay_days(conn) is None


# ----------------------------------------------------------------- the purge


def test_guild_zero_is_refused(conn):
    """Guild 0 is the bot-global config slot, not a server.

    Purging it would delete the instance's own settings — including the dial
    that armed the purge — plus the shared LegitLibs template pool and the AI
    prompt store.
    """
    with pytest.raises(ValueError, match="guild_id 0"):
        purge.purge_guild_data(conn, 0)


def test_guild_scoped_rows_go_and_neighbours_stay(conn):
    """The departing guild's rows are deleted; another guild's are not."""
    _seed_config(conn, GUILD)
    _seed_config(conn, OTHER_GUILD)
    for guild_id in (GUILD, OTHER_GUILD):
        conn.execute(
            "INSERT INTO econ_wallets "
            "(guild_id, user_id, balance, created_at, updated_at) "
            "VALUES (?, 1, 500, ?, ?)",
            (guild_id, NOW, NOW),
        )

    counts = purge.purge_guild_data(conn, GUILD)

    assert counts["config"] == 1
    assert counts["econ_wallets"] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM econ_wallets WHERE guild_id = ?", (GUILD,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM econ_wallets WHERE guild_id = ?", (OTHER_GUILD,)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM config WHERE guild_id = ?", (OTHER_GUILD,)
    ).fetchone()[0] == 1


def test_no_contact_list_goes_with_the_guild(conn):
    """Named explicitly because it is the one table it would be tempting to keep.

    A no-contact pair is a safety record, and preserving safety records is the
    normal instinct — but it is a record about two members of *this* server,
    stored per guild, and once the bot is gone there is no surface left that
    could consult it. See docs/data_register.md.
    """
    conn.execute(
        "INSERT INTO no_contact_pairs "
        "(guild_id, user_low, user_high, created_by, created_at) "
        "VALUES (?, 1, 2, 1, ?)",
        (GUILD, NOW),
    )
    counts = purge.purge_guild_data(conn, GUILD)
    assert counts["no_contact_pairs"] == 1


def test_children_and_grandchildren_are_reached(conn):
    """A row three tables from ``guild_id`` still goes.

    ``whisper_reply_reports`` -> ``whisper_replies`` -> ``whispers``, where
    only ``whispers`` carries a guild. Nothing in the schema declares that
    chain — it is hand-written in CHILD_TABLES and resolved recursively, so
    this is the test that the recursion actually works.
    """
    conn.execute(
        "INSERT INTO whispers "
        "(id, guild_id, sender_id, target_id, message, created_at) "
        "VALUES (1, ?, 9, 8, 'x', ?)",
        (GUILD, NOW),
    )
    conn.execute(
        "INSERT INTO whisper_replies "
        "(id, whisper_id, from_user_id, to_user_id, content, created_at) "
        "VALUES (1, 1, 8, 9, 'y', ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO whisper_reply_reports (reply_id, reporter_id, reason, created_at) "
        "VALUES (1, 7, 'spam', ?)",
        (NOW,),
    )

    counts = purge.purge_guild_data(conn, GUILD)

    assert counts["whispers"] == 1
    assert counts["whisper_replies"] == 1
    assert counts["whisper_reply_reports"] == 1


def test_children_of_another_guild_survive(conn):
    """The recursive subquery scopes by guild, not by "all children"."""
    conn.execute(
        "INSERT INTO whispers "
        "(id, guild_id, sender_id, target_id, message, created_at) "
        "VALUES (1, ?, 9, 8, 'x', ?)",
        (GUILD, NOW),
    )
    conn.execute(
        "INSERT INTO whispers "
        "(id, guild_id, sender_id, target_id, message, created_at) "
        "VALUES (2, ?, 9, 8, 'x', ?)",
        (OTHER_GUILD, NOW),
    )
    conn.execute(
        "INSERT INTO whisper_replies "
        "(id, whisper_id, from_user_id, to_user_id, content, created_at) "
        "VALUES (1, 1, 8, 9, 'a', ?), (2, 2, 8, 9, 'b', ?)",
        (NOW, NOW),
    )

    purge.purge_guild_data(conn, GUILD)

    rows = [r[0] for r in conn.execute("SELECT whisper_id FROM whisper_replies")]
    assert rows == [2]


def test_channel_rows_need_the_snapshot(conn):
    """Channel-keyed tables are only reachable through the departing channel list.

    Without it they are skipped rather than guessed at — a channel id is
    globally unique, but deleting by "every channel id we have ever seen" would
    be a different and much wider operation.
    """
    conn.execute(
        "INSERT INTO games_session_tracker (session_id, channel_id) VALUES ('s1', 5001)"
    )

    kept = purge.purge_guild_data(conn, GUILD)
    assert "games_session_tracker" not in kept
    assert conn.execute("SELECT COUNT(*) FROM games_session_tracker").fetchone()[0] == 1

    counts = purge.purge_guild_data(conn, GUILD, channel_ids=(5001, 5002))
    assert counts["games_session_tracker"] == 1


def test_global_tables_are_untouched(conn):
    """A guild leaving must not empty the shared question bank.

    The bank ships pre-seeded by the migrations, so this counts before and
    after rather than asserting a number.
    """
    conn.execute(
        "INSERT INTO games_question_bank (game_type, question_text) "
        "VALUES ('tod', 'q')"
    )
    before = conn.execute("SELECT COUNT(*) FROM games_question_bank").fetchone()[0]

    counts = purge.purge_guild_data(conn, GUILD)

    assert "games_question_bank" not in counts
    after = conn.execute("SELECT COUNT(*) FROM games_question_bank").fetchone()[0]
    assert after == before


# ---------------------------------------------------------- departed markers


def test_mark_stores_deadline_and_channels(conn):
    deadline = purge.mark_departed(
        conn, GUILD, delay_days=7, channel_ids=(10, 20), now=NOW
    )
    assert deadline == NOW + 7 * DAY
    assert purge.departed_channel_ids(conn, GUILD) == (10, 20)
    assert purge.pending_guilds(conn) == [(GUILD, NOW + 7 * DAY)]


def test_remark_keeps_the_original_deadline(conn):
    """A second sweep must not push the deadline out forever.

    The reconciliation pass re-marks absent guilds every day; if each pass
    reset the clock, a guild queued with a 30-day grace period would never
    reach it.
    """
    first = purge.mark_departed(conn, GUILD, delay_days=7, now=NOW)
    second = purge.mark_departed(conn, GUILD, delay_days=7, now=NOW + 5 * DAY)
    assert second == first


def test_mark_with_no_channels_round_trips_empty(conn):
    purge.mark_departed(conn, GUILD, delay_days=0, now=NOW)
    assert purge.departed_channel_ids(conn, GUILD) == ()


def test_clear_cancels_a_pending_purge(conn):
    """The re-invite path: back inside the window costs nothing."""
    purge.mark_departed(conn, GUILD, delay_days=30, now=NOW)
    assert purge.clear_departed(conn, GUILD) is True
    assert purge.pending_guilds(conn) == []
    assert purge.clear_departed(conn, GUILD) is False


def test_a_fresh_departure_after_a_rejoin_gets_a_new_deadline(conn):
    purge.mark_departed(conn, GUILD, delay_days=7, now=NOW)
    purge.clear_departed(conn, GUILD)
    again = purge.mark_departed(conn, GUILD, delay_days=7, now=NOW + 100 * DAY)
    assert again == NOW + 107 * DAY


@pytest.mark.parametrize(
    ("delay_days", "at", "due"),
    [
        pytest.param(0, NOW, True, id="zero-delay-is-due-at-once"),
        pytest.param(7, NOW + DAY, False, id="inside-the-window"),
        pytest.param(7, NOW + 7 * DAY, True, id="exactly-at-the-deadline"),
        pytest.param(7, NOW + 8 * DAY, True, id="past-the-deadline"),
    ],
)
def test_due_guilds(conn, delay_days, at, due):
    purge.mark_departed(conn, GUILD, delay_days=delay_days, now=NOW)
    assert purge.due_guilds(conn, now=at) == ([GUILD] if due else [])


def test_run_departed_purge_uses_the_snapshot_and_drops_the_marker(conn):
    """The end-to-end path the sweep takes.

    The channel rows prove the stored snapshot is what gets used — by purge
    time the ``Guild`` object it came from has been gone for up to a month.
    """
    _seed_config(conn, GUILD)
    conn.execute(
        "INSERT INTO games_session_tracker (session_id, channel_id) VALUES ('s2', 7001)"
    )
    purge.mark_departed(conn, GUILD, delay_days=0, channel_ids=(7001,), now=NOW)

    counts = purge.run_departed_purge(conn, GUILD)

    assert counts["config"] == 1
    assert counts["games_session_tracker"] == 1
    assert purge.pending_guilds(conn) == []


# --------------------------------------------------------------- odds & ends


def test_known_guild_ids_excludes_the_global_slot(conn):
    """Reconciliation must never hand guild 0 to the purge."""
    _seed_config(conn, GUILD)
    _seed_config(conn, OTHER_GUILD)
    conn.execute(
        "INSERT INTO config (guild_id, key, value) VALUES (0, 'global', '1')"
    )
    known = purge.known_guild_ids(conn)
    assert purge.GLOBAL_GUILD_ID not in known
    assert {GUILD, OTHER_GUILD} <= known


def test_sweep_orphans_removes_parentless_rows(conn):
    """Residue from before this module existed — prod carries 135 such rows."""
    conn.execute(
        "INSERT INTO messages (message_id, guild_id, channel_id, author_id, ts) "
        "VALUES (1, ?, 2, 3, ?)",
        (GUILD, NOW),
    )
    conn.execute(
        "INSERT INTO message_mentions (message_id, user_id) VALUES (1, 4), (999, 5)"
    )

    counts = purge.sweep_orphans(conn)

    assert counts["message_mentions"] == 1
    rows = [r[0] for r in conn.execute("SELECT message_id FROM message_mentions")]
    assert rows == [1]


def test_sweep_orphans_leaves_null_keys_alone(conn, monkeypatch):
    """A NULL foreign key is unlinked, not orphaned — a different thing.

    Sweeping it would widen this from cleaning up dangling references into
    deleting rows that were never meant to point anywhere.

    No child table in today's schema actually permits a NULL key — every entry
    in ``CHILD_TABLES`` points at a NOT NULL column or a rowid alias — so the
    guard is exercised through a synthetic pair rather than a real one. It is
    forward-looking, and this is what keeps it from being deleted as dead code
    the next time someone reads the query.
    """
    conn.executescript(
        "CREATE TABLE zz_parent (id INTEGER PRIMARY KEY);"
        "CREATE TABLE zz_child (ref INTEGER, note TEXT);"
        "INSERT INTO zz_parent (id) VALUES (1);"
        "INSERT INTO zz_child (ref, note) VALUES (1, 'linked'),"
        " (NULL, 'never linked'), (99, 'dangling');"
    )
    monkeypatch.setitem(purge.CHILD_TABLES, "zz_child", ("ref", "zz_parent", "id"))

    counts = purge.sweep_orphans(conn)

    assert counts["zz_child"] == 1
    kept = sorted(r[0] for r in conn.execute("SELECT note FROM zz_child"))
    assert kept == ["linked", "never linked"]


def test_deepest_children_are_deleted_first(conn):
    """Ordering is derived at run time, not from how the map was typed.

    A parent deleted before its child leaves the child unreachable forever, so
    the ordering is load-bearing rather than cosmetic.
    """
    order = purge._child_tables_deepest_first()
    assert order.index("whisper_reply_reports") < order.index("whisper_replies")
    assert order.index("doc_placement_messages") < order.index("doc_placements")


# ------------------------------------------------------------- reconcile


def test_reconcile_refuses_an_empty_guild_list(conn):
    """An empty guild list is a bot that has not finished connecting.

    Reading it as "removed from everywhere" would queue every server the
    database knows about, which is the one mistake this feature cannot make
    quietly.
    """
    _seed_config(conn, GUILD)
    _set_delay(conn, "7")

    with pytest.raises(ValueError):
        purge.reconcile_presence(conn, set())

    assert purge.pending_guilds(conn) == []


def test_reconcile_marks_guilds_the_bot_is_no_longer_in(conn):
    """A guild lost while the bot was offline fired no removal event."""
    _seed_config(conn, GUILD)
    _seed_config(conn, OTHER_GUILD)
    _set_delay(conn, "7")

    present = _present_apart_from(conn, GUILD)
    marked, rejoined = purge.reconcile_presence(conn, present, now=NOW)

    assert marked == [GUILD]
    assert rejoined == []
    assert [gid for gid, _ in purge.pending_guilds(conn)] == [GUILD]
    assert purge.pending_guilds(conn)[0][1] == pytest.approx(NOW + 7 * DAY)


def test_reconcile_marks_nothing_while_the_dial_is_off(conn):
    """Ships dark: with no grace period set, nothing is ever queued."""
    _seed_config(conn, GUILD)

    present = _present_apart_from(conn, GUILD)
    marked, rejoined = purge.reconcile_presence(conn, present, now=NOW)

    assert marked == []
    assert purge.pending_guilds(conn) == []


def test_reconcile_unqueues_a_rejoined_guild_even_with_the_dial_off(conn):
    """A marker for a guild the bot is sitting in is wrong either way.

    Left in place it would arm a purge for the instant someone sets the dial,
    so the rejoin half runs unconditionally.
    """
    purge.mark_departed(conn, GUILD, delay_days=30, now=NOW)

    marked, rejoined = purge.reconcile_presence(conn, {GUILD}, now=NOW)

    assert rejoined == [GUILD]
    assert marked == []
    assert purge.pending_guilds(conn) == []


def test_reconcile_keeps_an_existing_deadline(conn):
    """Re-marking an already-queued absent guild must not push its deadline out.

    Otherwise a daily sweep would reset the clock every day and the purge would
    never arrive.
    """
    _seed_config(conn, GUILD)
    _set_delay(conn, "7")
    present = _present_apart_from(conn, GUILD)
    purge.reconcile_presence(conn, present, now=NOW)

    purge.reconcile_presence(conn, present, now=NOW + 3 * DAY)

    assert purge.pending_guilds(conn)[0][1] == pytest.approx(NOW + 7 * DAY)
