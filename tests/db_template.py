"""Per-process template database: run migrations once, copy per test.

Building a test DB used to mean running all ~140 migrations — ~1s and a
2.2MB file per test, and WAL sidecars pushing it to ~4 inodes each. Across
~7,800 tests that wrote 1-2GB of tmp per full run and once exhausted the
remote runner's tmp entirely. Copying a pre-migrated template file is ~450x
faster, and the conftest autouse reaper deletes each copy at teardown so
tmp usage stays flat instead of accumulating for the whole session.

Deliberately plain functions rather than a pytest fixture: dozens of
per-file db fixtures call ``migrated_db()`` as a drop-in replacement for
``apply_migrations_sync()`` without threading a session fixture through
every signature. The cache is per process, which under xdist means one
template per worker.

Tests that exercise migration behaviour itself (idempotency, upgrades from
partial schemas) must keep calling the real ``apply_migrations_sync`` — a
template copy would skip exactly the code they test.

Two invariants the reaping depends on: every ``migrated_db()`` caller must
run under ``tests/conftest.py``'s autouse ``_reap_template_dbs`` fixture
(true for anything under tests/; a consumer outside pytest would silently
accumulate copies), and only function-scoped fixtures may call it — a
wider-scoped fixture's DB would be deleted after the first test that uses it.
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path

from migrations import apply_migrations_sync

_template: Path | None = None
_handed_out: list[Path] = []


def template_db() -> Path:
    """The fully-migrated template for this process, built on first use."""
    global _template
    if _template is None:
        base = Path(tempfile.mkdtemp(prefix="dk-db-template-"))
        atexit.register(shutil.rmtree, base, ignore_errors=True)
        path = base / "template.db"
        apply_migrations_sync(path)
        _template = path
    return _template


def migrated_db(db_path: str | Path) -> Path:
    """Drop-in for ``apply_migrations_sync(db_path)``: same schema, ~450x faster.

    Overwrites whatever is at db_path — callers always hand it a fresh
    tmp_path file. The copy is recorded so the conftest reaper can delete it
    (plus WAL sidecars) when the test finishes.
    """
    path = Path(db_path)
    shutil.copyfile(template_db(), path)
    _handed_out.append(path)
    return path


def reap() -> None:
    """Delete every copy handed out since the last call, sidecars included.

    Unlink failures are ignored: on the Windows remote runner a connection a
    test forgot to close keeps the file locked, and leaking one file there
    is better than failing an otherwise-green test in teardown.
    """
    while _handed_out:
        base = _handed_out.pop()
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(base) + suffix).unlink(missing_ok=True)
            except OSError:
                pass
