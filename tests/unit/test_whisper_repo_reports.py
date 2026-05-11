"""Unit tests for whisper report deduplication repo layer (B4)."""
from __future__ import annotations

from pathlib import Path

from db_utils import open_db
from services.whisper_repo import insert_report, insert_whisper

GUILD, SENDER, TARGET, REPORTER = 9001, 1001, 2001, 3001


def test_insert_report_first_report_returns_true(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        wid = insert_whisper(conn, guild_id=GUILD, sender_id=SENDER, target_id=TARGET, message="x")
        result = insert_report(conn, whisper_id=wid, reporter_id=REPORTER, reason="bad")
    assert result is True


def test_insert_report_duplicate_returns_false(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        wid = insert_whisper(conn, guild_id=GUILD, sender_id=SENDER, target_id=TARGET, message="x")
        insert_report(conn, whisper_id=wid, reporter_id=REPORTER, reason="first")
        result = insert_report(conn, whisper_id=wid, reporter_id=REPORTER, reason="second")
    assert result is False


def test_insert_report_different_reporters_both_succeed(sync_db_path: Path):
    reporter2 = 4001
    with open_db(sync_db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        wid = insert_whisper(conn, guild_id=GUILD, sender_id=SENDER, target_id=TARGET, message="x")
        r1 = insert_report(conn, whisper_id=wid, reporter_id=REPORTER, reason="reason a")
        r2 = insert_report(conn, whisper_id=wid, reporter_id=reporter2, reason="reason b")
    assert r1 is True
    assert r2 is True
