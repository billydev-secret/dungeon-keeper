"""Tests for the age-based retention sweep (2026-09-05 review).

The unit under test is ``retention_service`` plus its message arm in
``message_store.redact_message_content_older_than``. Two properties matter more
than the row counts and are asserted repeatedly: **the window is a floor, not a
suggestion** (nothing inside it is ever touched), and **redaction is not
deletion** (the message row and every derivation survive losing their text).
"""

from __future__ import annotations

import itertools
import sqlite3

import pytest

from bot_modules.core.db_utils import set_config_value
from bot_modules.services import retention_service
from bot_modules.services.message_store import (
    init_message_tables,
    redact_message_content_older_than,
    store_message,
)

NOW = 2_000_000_000.0
DAY = 86400.0
GUILD = 1
OTHER_GUILD = 2


def _conn(*, enabled: bool = True) -> sqlite3.Connection:
    """A schema plus, by default, retention switched ON for ``GUILD``.

    The dial ships off, so a sweep test that forgot to enable it would pass
    vacuously — "nothing was deleted" is trivially true when nothing runs.
    Enabling here by default makes the vacuous case impossible to write by
    accident; the two tests that are *about* the default pass ``enabled=False``.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_message_tables(conn)
    conn.execute(
        """
        CREATE TABLE config (
            guild_id INTEGER NOT NULL,
            key      TEXT NOT NULL,
            value    TEXT NOT NULL,
            PRIMARY KEY (guild_id, key)
        )
        """
    )
    # reaction_log and ping_events already exist with their real schemas —
    # init_message_tables owns them. Only the three it does not create are
    # stubbed, and those are stubbed minimally: the sweep reads guild_id and
    # the timestamp and nothing else.
    for table, ts_col in retention_service.BEHAVIOURAL_TABLES:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            f"guild_id INTEGER NOT NULL, user_id INTEGER, "
            f"{ts_col} REAL NOT NULL)"
        )
    if enabled:
        for key in (
            retention_service.MESSAGE_RETENTION_CONFIG_KEY,
            retention_service.BEHAVIOURAL_RETENTION_CONFIG_KEY,
        ):
            set_config_value(conn, key, "1", GUILD)
    return conn


def _msg(conn, *, message_id: int, age_days: float, guild_id: int = GUILD) -> None:
    store_message(
        conn,
        message_id=message_id,
        guild_id=guild_id,
        channel_id=10,
        author_id=50,
        content="secret text",
        reply_to_id=None,
        ts=NOW - age_days * DAY,
        attachment_urls=["https://cdn.example/img.png"],
        mention_ids=[77],
        sentiment=0.5,
        emotion="joy",
        embeds=[{"title": "t", "description": "d"}],
        retain_content=True,
    )


#: Distinct values for id-ish columns, so a real primary key (reaction_log is
#: keyed on guild+message+reactor) does not silently collapse several inserted
#: rows into one and make a sweep look more effective than it was.
_seq = itertools.count(1)


def _behav(conn, table, ts_col, *, age_days: float, guild_id: int = GUILD) -> None:
    """Insert one row, filling whatever the real schema demands.

    Two of these five tables are created by ``init_message_tables`` with their
    production schemas and several NOT NULL columns; the rest are stubs. Rather
    than keep a per-table insert list in step with both, fill every NOT NULL
    column that has no default with a unique integer — the sweep only ever
    reads ``guild_id`` and the timestamp, so the rest just has to be valid.
    """
    n = next(_seq)
    values = {"guild_id": guild_id, ts_col: NOW - age_days * DAY}
    for col in conn.execute(f"PRAGMA table_info({table})").fetchall():
        name = col["name"]
        if name in values:
            continue
        if col["notnull"] and col["dflt_value"] is None:
            values[name] = n
    cols = ", ".join(values)
    marks = ", ".join("?" * len(values))
    conn.execute(
        f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values())
    )


def _content(conn, message_id: int):
    row = conn.execute(
        "SELECT content FROM messages WHERE message_id = ?", (message_id,)
    ).fetchone()
    return row["content"] if row else None


# ── the switch ────────────────────────────────────────────────────────


@pytest.mark.parametrize("predicate", [
    retention_service.message_retention_enabled,
    retention_service.behavioural_retention_enabled,
])
def test_absent_config_row_means_retention_is_off(predicate):
    """Ships dark: nothing sweeps until a guild opts in. Both arms."""
    conn = _conn(enabled=False)
    assert predicate(conn, GUILD) is False


@pytest.mark.parametrize("key,predicate", [
    (retention_service.MESSAGE_RETENTION_CONFIG_KEY,
     retention_service.message_retention_enabled),
    (retention_service.BEHAVIOURAL_RETENTION_CONFIG_KEY,
     retention_service.behavioural_retention_enabled),
])
@pytest.mark.parametrize("stored,expected", [
    ("1", True), ("true", True), ("on", True), ("yes", True),
    ("0", False), ("", False), ("no", False),
])
def test_switch_reads_truthy_values_as_enabled(key, predicate, stored, expected):
    conn = _conn(enabled=False)
    set_config_value(conn, key, stored, GUILD)
    assert predicate(conn, GUILD) is expected


def test_each_arm_runs_without_the_other():
    """The whole point of two dials: the settled arm is not held hostage.

    Message retention on, behavioural off — the text goes and the interaction
    rows stay — and then the reverse.
    """
    conn = _conn(enabled=False)
    set_config_value(
        conn, retention_service.MESSAGE_RETENTION_CONFIG_KEY, "1", GUILD
    )
    _msg(conn, message_id=1, age_days=400)
    table, ts_col = retention_service.BEHAVIOURAL_TABLES[0]
    _behav(conn, table, ts_col, age_days=200)

    result = retention_service.run_retention(conn, GUILD, now=NOW)
    assert result["messages_redacted"] == 1
    assert result[table] == 0
    assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1

    conn2 = _conn(enabled=False)
    set_config_value(
        conn2, retention_service.BEHAVIOURAL_RETENTION_CONFIG_KEY, "1", GUILD
    )
    _msg(conn2, message_id=1, age_days=400)
    _behav(conn2, table, ts_col, age_days=200)

    result2 = retention_service.run_retention(conn2, GUILD, now=NOW)
    assert result2["messages_redacted"] == 0
    assert result2[table] == 1
    assert _content(conn2, 1) == "secret text"


def test_member_events_is_not_swept():
    """Tenure reads MIN(ts) FROM member_events with no window at all.

    ``rules_watch.compute_tenure_days`` wants a member's first-ever join, and
    ``scorer`` up-weights ``tenure_days < 7`` — so sweeping this table would
    score a long-standing member who rejoined as a newcomer. It must stay out
    of the list however the periods are re-decided.
    """
    swept = {table for table, _ in retention_service.BEHAVIOURAL_TABLES}
    assert "member_events" not in swept


def test_disabled_guild_keeps_everything():
    conn = _conn(enabled=False)
    _msg(conn, message_id=1, age_days=900)
    for table, ts_col in retention_service.BEHAVIOURAL_TABLES:
        _behav(conn, table, ts_col, age_days=900)

    result = retention_service.run_retention(conn, GUILD, now=NOW)

    # Same keys as an enabled pass, all zero — a caller reading result[table]
    # must not KeyError only for the guilds that opted out.
    assert result == {
        "messages_redacted": 0,
        **{table: 0 for table, _ in retention_service.BEHAVIOURAL_TABLES},
    }
    assert _content(conn, 1) == "secret text"
    for table, _ in retention_service.BEHAVIOURAL_TABLES:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1


def test_one_guild_opting_out_does_not_shield_another():
    conn = _conn()
    for key in (
        retention_service.MESSAGE_RETENTION_CONFIG_KEY,
        retention_service.BEHAVIOURAL_RETENTION_CONFIG_KEY,
    ):
        set_config_value(conn, key, "0", OTHER_GUILD)
    _msg(conn, message_id=1, age_days=900, guild_id=GUILD)
    _msg(conn, message_id=2, age_days=900, guild_id=OTHER_GUILD)

    retention_service.run_retention(conn, GUILD, now=NOW)

    assert _content(conn, 1) is None
    assert _content(conn, 2) == "secret text"


# ── the message arm: redaction, not deletion ──────────────────────────


def test_old_message_loses_text_but_survives_with_its_derivations():
    conn = _conn()
    _msg(conn, message_id=1, age_days=400)

    n = redact_message_content_older_than(
        conn, GUILD, older_than_days=365, now=NOW
    )

    assert n == 1
    row = conn.execute(
        "SELECT * FROM messages WHERE message_id = 1"
    ).fetchone()
    assert row is not None, "the row must survive — only the text goes"
    assert row["content"] is None
    assert row["sentiment"] == 0.5
    assert row["emotion"] == "joy"
    # Mention edges are a derivation and stay; attachments and embeds are
    # content and go.
    assert conn.execute("SELECT COUNT(*) FROM message_mentions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM message_embeds").fetchone()[0] == 0


@pytest.mark.parametrize("age_days", [0, 100, 364, 364.9])
def test_message_inside_the_window_is_untouched(age_days):
    conn = _conn()
    _msg(conn, message_id=1, age_days=age_days)

    n = redact_message_content_older_than(
        conn, GUILD, older_than_days=365, now=NOW
    )

    assert n == 0
    assert _content(conn, 1) == "secret text"
    assert conn.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0] == 1


def test_redaction_is_idempotent():
    """A second pass must report 0, not re-count already-nulled rows."""
    conn = _conn()
    _msg(conn, message_id=1, age_days=400)

    first = redact_message_content_older_than(
        conn, GUILD, older_than_days=365, now=NOW
    )
    second = redact_message_content_older_than(
        conn, GUILD, older_than_days=365, now=NOW
    )

    assert (first, second) == (1, 0)


def test_message_arm_is_scoped_to_its_guild():
    conn = _conn()
    _msg(conn, message_id=1, age_days=400, guild_id=GUILD)
    _msg(conn, message_id=2, age_days=400, guild_id=OTHER_GUILD)

    redact_message_content_older_than(
        conn, GUILD, older_than_days=365, now=NOW
    )

    assert _content(conn, 1) is None
    assert _content(conn, 2) == "secret text"


# ── the behavioural arm: deletion ─────────────────────────────────────


@pytest.mark.parametrize(
    "table,ts_col", retention_service.BEHAVIOURAL_TABLES
)
def test_each_behavioural_table_is_swept(table, ts_col):
    conn = _conn()
    _behav(conn, table, ts_col, age_days=200)
    _behav(conn, table, ts_col, age_days=10)

    deleted = retention_service.sweep_behavioural(
        conn, GUILD, older_than_days=180, now=NOW
    )

    assert deleted[table] == 1
    remaining = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    assert remaining == 1, "the row inside the window must survive"


@pytest.mark.parametrize("age_days", [0, 90, 179, 179.9])
def test_behavioural_row_inside_the_window_is_untouched(age_days):
    conn = _conn()
    for table, ts_col in retention_service.BEHAVIOURAL_TABLES:
        _behav(conn, table, ts_col, age_days=age_days)

    deleted = retention_service.sweep_behavioural(
        conn, GUILD, older_than_days=180, now=NOW
    )

    assert not any(deleted.values())
    for table, _ in retention_service.BEHAVIOURAL_TABLES:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1


def test_behavioural_sweep_is_scoped_to_its_guild():
    conn = _conn()
    for table, ts_col in retention_service.BEHAVIOURAL_TABLES:
        _behav(conn, table, ts_col, age_days=200, guild_id=GUILD)
        _behav(conn, table, ts_col, age_days=200, guild_id=OTHER_GUILD)

    retention_service.sweep_behavioural(
        conn, GUILD, older_than_days=180, now=NOW
    )

    for table, _ in retention_service.BEHAVIOURAL_TABLES:
        rows = conn.execute(
            f"SELECT guild_id FROM {table}"
        ).fetchall()
        assert [r["guild_id"] for r in rows] == [OTHER_GUILD]


def test_sweep_deletes_past_a_single_chunk(monkeypatch):
    """The chunked delete loop must drain, not stop at one chunk."""
    monkeypatch.setattr(retention_service, "DELETE_CHUNK", 10)
    conn = _conn()
    table, ts_col = retention_service.BEHAVIOURAL_TABLES[0]
    for _ in range(25):
        _behav(conn, table, ts_col, age_days=200)

    deleted = retention_service.sweep_behavioural(
        conn, GUILD, older_than_days=180, now=NOW
    )

    assert deleted[table] == 25
    assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


# ── counting without deleting ─────────────────────────────────────────


def test_prunable_count_reports_without_deleting():
    conn = _conn()
    for table, ts_col in retention_service.BEHAVIOURAL_TABLES:
        _behav(conn, table, ts_col, age_days=200)

    counts = retention_service.prunable_behavioural_count(
        conn, GUILD, older_than_days=180, now=NOW
    )

    assert all(n == 1 for n in counts.values())
    for table, _ in retention_service.BEHAVIOURAL_TABLES:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1


# ── both arms together ────────────────────────────────────────────────


def test_run_retention_reports_both_arms():
    conn = _conn()
    _msg(conn, message_id=1, age_days=400)
    for table, ts_col in retention_service.BEHAVIOURAL_TABLES:
        _behav(conn, table, ts_col, age_days=200)

    result = retention_service.run_retention(conn, GUILD, now=NOW)

    assert result["messages_redacted"] == 1
    for table, _ in retention_service.BEHAVIOURAL_TABLES:
        assert result[table] == 1


def test_the_two_periods_differ_so_a_shared_default_cannot_hide_a_bug():
    """180 and 365 are different numbers on purpose.

    A message at 200 days is inside its window while a behavioural row at the
    same age is past its own — the case that would pass regardless if both
    arms accidentally read one constant.
    """
    conn = _conn()
    _msg(conn, message_id=1, age_days=200)
    table, ts_col = retention_service.BEHAVIOURAL_TABLES[0]
    _behav(conn, table, ts_col, age_days=200)

    result = retention_service.run_retention(conn, GUILD, now=NOW)

    assert result["messages_redacted"] == 0
    assert result[table] == 1
    assert _content(conn, 1) == "secret text"
