"""The two prod-facing preflight scripts, exercised on synthetic databases.

Both are run by /dk-regress against the live database, where they must be
read-only and must not lie. The lying is the real risk: an earlier draft of
each returned a confident, wrong answer — the key audit called `casino_min_bet`
dead because that string is assembled from a prefix and a dataclass field, and
the migration dry-run reported every migration pending because it swallowed an
OperationalError from querying the wrong column. Both bugs produced plausible
output, which is why they get tests rather than a manual eyeball.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import config_key_audit as audit  # noqa: E402
import migration_dryrun as dryrun  # noqa: E402


# ── config key audit ──────────────────────────────────────────────────


def test_a_prefixed_dataclass_field_counts_as_read(tmp_path):
    """The bug that made the first draft useless: keys built as
    PREFIX + field name appear nowhere in the source as literals."""
    mod = tmp_path / "m.py"
    mod.write_text(
        "from dataclasses import dataclass\n"
        'CASINO_PREFIX = "casino_"\n'
        "@dataclass\nclass S:\n    min_bet: int = 5\n",
        encoding="utf-8",
    )
    prefixes, fields = audit._prefixes_and_fields([mod])
    assert "casino_" in prefixes and "min_bet" in fields
    alive = audit._reachable({"casino_min_bet", "veil_gone"}, "", [mod])
    assert alive == {"casino_min_bet"}


def test_a_literal_key_counts_as_read(tmp_path):
    mod = tmp_path / "m.py"
    mod.write_text("x = 1\n", encoding="utf-8")
    alive = audit._reachable({"econ_theme_channel_id"}, 'get("econ_theme_channel_id")', [mod])
    assert alive == {"econ_theme_channel_id"}


def test_the_audit_reads_the_database_read_only(tmp_path):
    db = tmp_path / "d.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE config (guild_id INTEGER, key TEXT, value TEXT)")
    conn.execute("INSERT INTO config VALUES (1, 'a_key', 'v')")
    conn.commit()
    conn.close()
    db.chmod(0o444)
    try:
        assert audit.stored_keys(db) == {"a_key": [1]}
    finally:
        db.chmod(0o644)


# ── migration dry run ─────────────────────────────────────────────────


def test_applied_raises_rather_than_guessing_on_a_bad_schema(tmp_path):
    """The silent-except bug: a wrong column must fail, not report zero."""
    db = tmp_path / "d.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE schema_version (wrong_column TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(sqlite3.OperationalError):
        dryrun.applied(db)


def test_applied_reads_the_migration_column(tmp_path):
    db = tmp_path / "d.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE schema_version (migration TEXT, applied_at REAL)")
    conn.execute("INSERT INTO schema_version VALUES ('001_x.sql', 0.0)")
    conn.commit()
    conn.close()
    assert dryrun.applied(db) == {"001_x.sql"}


@pytest.mark.parametrize(
    ("sql", "flagged"),
    [
        pytest.param("DROP TABLE foo;", True, id="drop-table"),
        pytest.param("DELETE FROM foo WHERE x;", True, id="delete-from"),
        pytest.param("ALTER TABLE foo DROP COLUMN bar;", True, id="drop-column"),
        pytest.param("CREATE TABLE foo (a INT);", False, id="create"),
        pytest.param("CREATE INDEX i ON foo(a);", False, id="index"),
    ],
)
def test_destructive_statements_are_flagged(sql, flagged):
    assert bool(dryrun.DESTRUCTIVE.search(sql)) is flagged


def test_the_snapshot_uses_the_backup_api_not_a_copy(tmp_path):
    """A cp of a live WAL database is very often malformed; the dry-run would
    then fail for a reason that has nothing to do with the migrations."""
    live = tmp_path / "live.db"
    conn = sqlite3.connect(live)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (a INT)")
    conn.execute("INSERT INTO t VALUES (7)")
    conn.commit()
    dest = tmp_path / "snap.db"
    dryrun.snapshot(live, dest)
    conn.close()
    out = sqlite3.connect(dest)
    assert out.execute("SELECT a FROM t").fetchone()[0] == 7
    out.close()


def test_the_snapshot_does_not_default_to_tmpfs():
    """/tmp here is a 5.8 GiB tmpfs the test suite also uses, and the live
    database is over a gigabyte: a dry-run beside a full run could exhaust RAM
    and spray sqlite errors that look like a test failure."""
    assert "tmp" not in str(dryrun.DEFAULT_SNAPSHOT_DIR).split("/")[1:2]
    assert dryrun.DEFAULT_SNAPSHOT_DIR.is_absolute()
