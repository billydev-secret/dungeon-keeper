"""DB-layer tests for bios archive/purge retention.

Covers the 12-month purge of archived bios (review finding G2,
docs/reviews/2026-08-05-penpals-bios.md): archived-old rows are
permanently deleted, archived-recent and ACTIVE rows are untouched,
the purge is idempotent, resurrection clears the archive clock, and a
purged member's rejoin is a clean "no bio" no-op.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from bot_modules.bios import db as bios_db
from bot_modules.bios.resurrect import resolve_member_bio_link

GUILD = 9001
USER = 3001


def _conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _seed_bio(conn: sqlite3.Connection, user_id: int = USER) -> None:
    bios_db.upsert_bio(
        conn,
        guild_id=GUILD,
        user_id=user_id,
        message_id=111,
        channel_id=222,
        field_rows=[(1, "About", "hello")],
        answer_rows=[(0, 5, "Q?", "A!")],
    )


def _backdate_archive(conn: sqlite3.Connection, user_id: int, days: int) -> None:
    conn.execute(
        "UPDATE bios SET archived_at = datetime('now', ?) "
        "WHERE user_id = ? AND guild_id = ?",
        (f"-{days} days", user_id, GUILD),
    )


def _row_counts(conn: sqlite3.Connection, user_id: int) -> tuple[int, int, int]:
    def count(table: str) -> int:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE user_id = ? AND guild_id = ?",
            (user_id, GUILD),
        ).fetchone()
        return int(row["n"])

    return count("bios"), count("bio_field_values"), count("bio_answers")


def _archived_at(conn: sqlite3.Connection, user_id: int = USER) -> str | None:
    row = conn.execute(
        "SELECT archived_at FROM bios WHERE user_id = ? AND guild_id = ?",
        (user_id, GUILD),
    ).fetchone()
    return None if row is None else row["archived_at"]


# ── archive stamps the clock, resurrection clears it ─────────────────


def test_archive_sets_archived_at(sync_db_path):
    with _conn(sync_db_path) as conn:
        _seed_bio(conn)
        assert _archived_at(conn) is None
        bios_db.archive_user_bio(conn, GUILD, USER)
        assert _archived_at(conn) is not None


def test_resurrection_message_ref_clears_archived_at(sync_db_path):
    with _conn(sync_db_path) as conn:
        _seed_bio(conn)
        bios_db.archive_user_bio(conn, GUILD, USER)
        bios_db.update_bio_message_ref(
            conn, guild_id=GUILD, user_id=USER, message_id=333, channel_id=444
        )
        assert _archived_at(conn) is None


def test_rejoin_wizard_upsert_clears_archived_at(sync_db_path):
    with _conn(sync_db_path) as conn:
        _seed_bio(conn)
        bios_db.archive_user_bio(conn, GUILD, USER)
        _seed_bio(conn)  # rejoiner re-runs the wizard over the archived row
        assert _archived_at(conn) is None


# ── purge: window boundaries ─────────────────────────────────────────


def test_purge_deletes_archived_older_than_window(sync_db_path):
    with _conn(sync_db_path) as conn:
        _seed_bio(conn)
        bios_db.archive_user_bio(conn, GUILD, USER)
        _backdate_archive(conn, USER, days=366)
        assert bios_db.purge_stale_archived_bios(conn) == 1
        assert _row_counts(conn, USER) == (0, 0, 0)


def test_purge_keeps_recently_archived(sync_db_path):
    with _conn(sync_db_path) as conn:
        _seed_bio(conn)
        bios_db.archive_user_bio(conn, GUILD, USER)
        _backdate_archive(conn, USER, days=300)
        assert bios_db.purge_stale_archived_bios(conn) == 0
        assert _row_counts(conn, USER) == (1, 1, 1)


def test_purge_never_touches_active_bios_regardless_of_age(sync_db_path):
    with _conn(sync_db_path) as conn:
        _seed_bio(conn)
        # An ancient but still-live bio: old timestamps, archived_at NULL.
        conn.execute(
            "UPDATE bios SET created_at = datetime('now', '-900 days'), "
            "updated_at = datetime('now', '-900 days') "
            "WHERE user_id = ? AND guild_id = ?",
            (USER, GUILD),
        )
        assert bios_db.purge_stale_archived_bios(conn) == 0
        assert _row_counts(conn, USER) == (1, 1, 1)


def test_purge_skips_resurrected_bio_with_old_history(sync_db_path):
    with _conn(sync_db_path) as conn:
        _seed_bio(conn)
        bios_db.archive_user_bio(conn, GUILD, USER)
        _backdate_archive(conn, USER, days=366)
        # Member came back before the sweep ran — repost cleared the clock.
        bios_db.update_bio_message_ref(
            conn, guild_id=GUILD, user_id=USER, message_id=333, channel_id=444
        )
        assert bios_db.purge_stale_archived_bios(conn) == 0
        assert _row_counts(conn, USER) == (1, 1, 1)


def test_purge_is_idempotent_and_scoped_per_row(sync_db_path):
    with _conn(sync_db_path) as conn:
        _seed_bio(conn, user_id=USER)
        _seed_bio(conn, user_id=USER + 1)
        bios_db.archive_user_bio(conn, GUILD, USER)
        _backdate_archive(conn, USER, days=400)
        assert bios_db.purge_stale_archived_bios(conn) == 1
        # Second sweep finds nothing; the untouched live bio survives both.
        assert bios_db.purge_stale_archived_bios(conn) == 0
        assert _row_counts(conn, USER) == (0, 0, 0)
        assert _row_counts(conn, USER + 1) == (1, 1, 1)


def test_purge_honors_custom_window(sync_db_path):
    with _conn(sync_db_path) as conn:
        _seed_bio(conn)
        bios_db.archive_user_bio(conn, GUILD, USER)
        _backdate_archive(conn, USER, days=2)
        assert bios_db.purge_stale_archived_bios(conn, older_than_seconds=86400) == 1
        assert _row_counts(conn, USER) == (0, 0, 0)


# ── purged member rejoining is a clean no-op ─────────────────────────


def test_resurrection_after_purge_is_clean_noop(sync_db_path):
    with _conn(sync_db_path) as conn:
        _seed_bio(conn)
        bios_db.archive_user_bio(conn, GUILD, USER)
        _backdate_archive(conn, USER, days=400)
        bios_db.purge_stale_archived_bios(conn)
        assert bios_db.get_user_bio(conn, GUILD, USER) is None
        conn.commit()

    class _Ctx:
        def open_db(self):
            return _conn(sync_db_path)

    member = SimpleNamespace(id=USER, guild=SimpleNamespace(id=GUILD))
    link = asyncio.run(resolve_member_bio_link(_Ctx(), member))  # type: ignore[arg-type]
    assert link == ""
