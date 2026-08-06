"""Tests for the anonymous-features audit trail.

Covers the write path (including the deliberate error-swallow contract), the
filtered read path the dashboard panel issues, and the retention purge —
especially its boundaries, since "keep forever" and "exactly at the cutoff"
are where a purge silently destroys evidence or fails to bound the table.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.anon_audit_service import (
    DEFAULT_RETENTION_DAYS,
    FEATURE_AMA,
    FEATURE_CONFESSIONS,
    FEATURE_WYR,
    KNOWN_FEATURES,
    RETENTION_FOREVER,
    SECONDS_PER_DAY,
    count_events,
    feature_label,
    get_retention_days,
    insert_event,
    list_events,
    purge_expired,
    record_event,
    set_retention_days,
)

GUILD = 4242
OTHER_GUILD = 9999


def _seed(conn: sqlite3.Connection, **kw) -> int:
    base = dict(
        guild_id=GUILD,
        feature=FEATURE_AMA,
        event="question_asked",
        actor_id=7001,
    )
    base.update(kw)
    return insert_event(conn, **base)


# ── Writing ───────────────────────────────────────────────────────────────────


def test_record_event_persists_every_column(sync_db_path):
    record_event(
        sync_db_path,
        guild_id=GUILD,
        feature=FEATURE_AMA,
        event="question_asked",
        actor_id=7001,
        target_id=8002,
        game_id="game-uuid",
        message_id=123456789012345678,
        channel_id=555,
        extra={"question_idx": 3},
    )

    with open_db(sync_db_path) as conn:
        (ev,) = list_events(conn, GUILD)

    assert ev.feature == FEATURE_AMA
    assert ev.event == "question_asked"
    assert ev.actor_id == 7001
    assert ev.target_id == 8002
    assert ev.game_id == "game-uuid"
    assert ev.message_id == 123456789012345678
    assert ev.channel_id == 555
    assert ev.extra == {"question_idx": 3}
    assert ev.created_at > 0


def test_record_event_allows_null_pointer_columns(sync_db_path):
    """A rejected screened question or a DM-delivered compliment has no
    message to point at — the row must still be written."""
    record_event(
        sync_db_path,
        guild_id=GUILD,
        feature=FEATURE_AMA,
        event="question_asked",
        actor_id=7001,
    )

    with open_db(sync_db_path) as conn:
        (ev,) = list_events(conn, GUILD)

    assert ev.message_id is None
    assert ev.channel_id is None
    assert ev.target_id is None
    assert ev.extra == {}


def test_record_event_swallows_db_errors(tmp_path, caplog):
    """An audit failure must never propagate into the member-facing flow —
    a member should not lose the question they typed because the DB was busy.
    """
    missing = tmp_path / "no-such-dir" / "test.db"

    record_event(
        missing,
        guild_id=GUILD,
        feature=FEATURE_AMA,
        event="question_asked",
        actor_id=7001,
    )  # must not raise

    assert "anon audit write failed" in caplog.text


def test_corrupt_extra_json_degrades_to_empty_dict(sync_db_path):
    with open_db(sync_db_path) as conn:
        conn.execute(
            "INSERT INTO anon_audit_log "
            "(guild_id, feature, event, actor_id, extra, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (GUILD, FEATURE_AMA, "question_asked", 7001, "not json", time.time()),
        )

    with open_db(sync_db_path) as conn:
        (ev,) = list_events(conn, GUILD)

    assert ev.extra == {}


# ── Reading ───────────────────────────────────────────────────────────────────


def test_list_events_is_guild_scoped_and_newest_first(sync_db_path):
    now = time.time()
    with open_db(sync_db_path) as conn:
        _seed(conn, actor_id=1, created_at=now - 30)
        _seed(conn, actor_id=2, created_at=now - 10)
        _seed(conn, guild_id=OTHER_GUILD, actor_id=3, created_at=now)

    with open_db(sync_db_path) as conn:
        events = list_events(conn, GUILD)

    assert [e.actor_id for e in events] == [2, 1]


@pytest.mark.parametrize(
    "kwargs, expected_actors",
    [
        pytest.param({"feature": FEATURE_WYR}, [22], id="by-feature"),
        pytest.param({"actor_id": 11}, [11], id="by-actor"),
        pytest.param(
            {"feature": FEATURE_AMA, "actor_id": 11}, [11], id="feature-and-actor"
        ),
        pytest.param({"feature": "nonexistent"}, [], id="no-match"),
    ],
)
def test_list_events_filters(sync_db_path, kwargs, expected_actors):
    with open_db(sync_db_path) as conn:
        _seed(conn, actor_id=11, feature=FEATURE_AMA)
        _seed(conn, actor_id=22, feature=FEATURE_WYR, event="vote")

    with open_db(sync_db_path) as conn:
        events = list_events(conn, GUILD, **kwargs)
        total = count_events(conn, GUILD, **kwargs)

    assert [e.actor_id for e in events] == expected_actors
    assert total == len(expected_actors)


def test_list_events_paginates(sync_db_path):
    now = time.time()
    with open_db(sync_db_path) as conn:
        for i in range(5):
            _seed(conn, actor_id=i, created_at=now - i)

    with open_db(sync_db_path) as conn:
        page = list_events(conn, GUILD, limit=2, offset=2)
        assert count_events(conn, GUILD) == 5

    assert [e.actor_id for e in page] == [2, 3]


# ── Retention ─────────────────────────────────────────────────────────────────


def test_retention_defaults_without_a_config_row(sync_db_path):
    with open_db(sync_db_path) as conn:
        assert get_retention_days(conn, GUILD) == DEFAULT_RETENTION_DAYS


def test_set_retention_days_round_trips_and_upserts(sync_db_path):
    with open_db(sync_db_path) as conn:
        set_retention_days(conn, GUILD, 30)
    with open_db(sync_db_path) as conn:
        assert get_retention_days(conn, GUILD) == 30
        set_retention_days(conn, GUILD, 7)
    with open_db(sync_db_path) as conn:
        assert get_retention_days(conn, GUILD) == 7


def test_set_retention_rejects_negative(sync_db_path):
    with open_db(sync_db_path) as conn:
        with pytest.raises(ValueError):
            set_retention_days(conn, GUILD, -1)


def test_purge_removes_only_rows_past_the_window(sync_db_path):
    now = time.time()
    with open_db(sync_db_path) as conn:
        set_retention_days(conn, GUILD, 30)
        _seed(conn, actor_id=1, created_at=now - 31 * SECONDS_PER_DAY)  # expired
        _seed(conn, actor_id=2, created_at=now - 29 * SECONDS_PER_DAY)  # kept

    assert purge_expired(sync_db_path, now=now) == 1

    with open_db(sync_db_path) as conn:
        assert [e.actor_id for e in list_events(conn, GUILD)] == [2]


def test_purge_keeps_the_row_exactly_at_the_cutoff(sync_db_path):
    """The comparison is strict (< cutoff), so a row landing exactly on the
    boundary survives rather than being destroyed a tick early."""
    now = time.time()
    with open_db(sync_db_path) as conn:
        set_retention_days(conn, GUILD, 30)
        _seed(conn, created_at=now - 30 * SECONDS_PER_DAY)

    assert purge_expired(sync_db_path, now=now) == 0

    with open_db(sync_db_path) as conn:
        assert count_events(conn, GUILD) == 1


def test_purge_skips_guilds_set_to_keep_forever(sync_db_path):
    now = time.time()
    with open_db(sync_db_path) as conn:
        set_retention_days(conn, GUILD, RETENTION_FOREVER)
        _seed(conn, created_at=now - 3650 * SECONDS_PER_DAY)

    assert purge_expired(sync_db_path, now=now) == 0

    with open_db(sync_db_path) as conn:
        assert count_events(conn, GUILD) == 1


def test_purge_applies_the_default_to_a_guild_with_no_config_row(sync_db_path):
    """A server that never opens the panel is still bounded."""
    now = time.time()
    with open_db(sync_db_path) as conn:
        _seed(conn, created_at=now - (DEFAULT_RETENTION_DAYS + 1) * SECONDS_PER_DAY)

    assert purge_expired(sync_db_path, now=now) == 1

    with open_db(sync_db_path) as conn:
        assert count_events(conn, GUILD) == 0


def test_purge_is_per_guild(sync_db_path):
    """One guild opting out of purging must not preserve another's rows."""
    now = time.time()
    old = now - 400 * SECONDS_PER_DAY
    with open_db(sync_db_path) as conn:
        set_retention_days(conn, GUILD, RETENTION_FOREVER)
        set_retention_days(conn, OTHER_GUILD, 30)
        _seed(conn, guild_id=GUILD, created_at=old)
        _seed(conn, guild_id=OTHER_GUILD, created_at=old)

    assert purge_expired(sync_db_path, now=now) == 1

    with open_db(sync_db_path) as conn:
        assert count_events(conn, GUILD) == 1
        assert count_events(conn, OTHER_GUILD) == 0


# ── Feature labels ────────────────────────────────────────────────────
#
# Confessions is audited here but is not a game, so it has no GAME_NAMES entry.
# The dashboard renders row labels and the filter dropdown from one lookup, so
# a slug must not appear under two different names depending on the call site.


def test_confessions_is_a_known_feature():
    assert FEATURE_CONFESSIONS in KNOWN_FEATURES


@pytest.mark.parametrize(
    "slug,expected",
    [
        # Non-game surface: resolved from FEATURE_LABELS, not GAME_NAMES.
        (FEATURE_CONFESSIONS, "Confessions"),
        # Games still resolve through GAME_NAMES.
        (FEATURE_WYR, "Would You Rather"),
        # Unknown slugs fall through to themselves rather than blowing up.
        ("not_a_feature", "not_a_feature"),
    ],
)
def test_feature_label_resolution(slug, expected):
    assert feature_label(slug) == expected


def test_confessions_rows_obey_the_shared_retention_window(sync_db_path):
    """Confessions is purged on the same guild-wide dial as the games."""
    stale = time.time() - (DEFAULT_RETENTION_DAYS + 1) * SECONDS_PER_DAY
    with open_db(sync_db_path) as conn:
        insert_event(
            conn, guild_id=GUILD, feature=FEATURE_CONFESSIONS,
            event="confession_posted", actor_id=7001, created_at=stale,
        )
        insert_event(
            conn, guild_id=GUILD, feature=FEATURE_AMA,
            event="question_asked", actor_id=7001, created_at=stale,
        )

    purge_expired(sync_db_path)

    with open_db(sync_db_path) as conn:
        assert count_events(conn, GUILD) == 0
