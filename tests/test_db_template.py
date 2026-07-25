"""The template-copy DB helper must be indistinguishable from real migrations."""

from __future__ import annotations

import sqlite3

from migrations import _migration_files, apply_migrations_sync
from tests import db_template
from tests.db_template import migrated_db, template_db


def _schema(path) -> set[tuple[str, str]]:
    with sqlite3.connect(path) as conn:
        return set(
            conn.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        )


def test_copy_matches_a_real_migration_run(tmp_path):
    real = tmp_path / "real.db"
    apply_migrations_sync(real)
    copy = migrated_db(tmp_path / "copy.db")
    assert _schema(copy) == _schema(real)


def test_copy_records_every_migration_as_applied(tmp_path):
    copy = migrated_db(tmp_path / "copy.db")
    with sqlite3.connect(copy) as conn:
        applied = {row[0] for row in conn.execute("SELECT migration FROM schema_version")}
    assert applied == {p.name for p in _migration_files()}


def test_template_is_built_once_per_process():
    assert template_db() == template_db()


def test_reap_deletes_copies_and_sidecars(tmp_path):
    copy = migrated_db(tmp_path / "copy.db")
    wal = tmp_path / "copy.db-wal"
    wal.write_bytes(b"")
    db_template.reap()
    assert not copy.exists() and not wal.exists()


def test_reap_spares_the_template(tmp_path):
    migrated_db(tmp_path / "copy.db")
    db_template.reap()
    assert template_db().exists()


def test_unreaped_copy_survives_for_wider_scoped_fixtures(tmp_path):
    # Module-scoped consumers (browser-suite servers) opt out of per-test
    # reaping; their DB must outlive a reap cycle.
    keeper = migrated_db(tmp_path / "module.db", reap=False)
    victim = migrated_db(tmp_path / "test.db")
    db_template.reap()
    assert keeper.exists() and not victim.exists()
