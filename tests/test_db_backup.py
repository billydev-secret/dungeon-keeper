"""Tests for the automated SQLite backup service.

Covers the guards added after the 2026-08-06 backup/DR review: the atomic
rename (B4), the restart-burst skip and prune age floor (B2), sidecar cleanup
(B9), and the deliberate hands-off treatment of ad-hoc snapshots (B5).
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from bot_modules.services import db_backup
from bot_modules.services.db_backup import (
    DEFAULT_MIN_AGE_HOURS,
    _env_float,
    _env_int,
    _prune_old_backups,
    _run_backup,
    run_backup_now,
)

_HOUR = 3600.0


@pytest.fixture
def source_db(tmp_path: Path) -> Path:
    """A small WAL-mode database standing in for the live one."""
    path = tmp_path / "dungeonkeeper.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT)")
    conn.executemany(
        "INSERT INTO messages (content) VALUES (?)", [(f"msg-{i}",) for i in range(50)]
    )
    conn.commit()
    conn.close()
    return path


def _age(path: Path, hours: float) -> None:
    """Backdate a file's mtime by ``hours``."""
    stamp = time.time() - hours * _HOUR
    os.utime(path, (stamp, stamp))


def _make_backup(backup_dir: Path, name: str, age_hours: float = 0.0) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / name
    path.write_bytes(b"stand-in")
    if age_hours:
        _age(path, age_hours)
    return path


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_backup_produces_a_readable_copy(source_db: Path, tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"

    result = _run_backup(source_db, backup_dir)

    assert result is not None
    assert result.parent == backup_dir
    conn = sqlite3.connect(result)
    assert conn.execute("PRAGMA quick_check").fetchone() == ("ok",)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 50
    conn.close()


def test_backup_leaves_no_partial_or_sidecar_files(
    source_db: Path, tmp_path: Path
) -> None:
    backup_dir = tmp_path / "backups"

    _run_backup(source_db, backup_dir)

    leftovers = [p.name for p in backup_dir.iterdir() if not p.name.endswith(".db")]
    assert leftovers == []


def test_run_backup_now_returns_the_new_path(source_db: Path) -> None:
    result = run_backup_now(source_db)

    assert result.exists()
    assert result.parent.name == "backups"


def test_source_database_is_not_modified(source_db: Path, tmp_path: Path) -> None:
    before = source_db.read_bytes()

    _run_backup(source_db, tmp_path / "backups")

    assert source_db.read_bytes() == before


# --------------------------------------------------------------------------
# B4 — a failed copy must never occupy the real filename
# --------------------------------------------------------------------------


class _SourceThatFailsMidCopy:
    """Wraps the read-only source connection so ``backup()`` blows up.

    ``sqlite3.Connection`` is an immutable type, so the failure is injected by
    swapping the module's ``connect`` rather than patching the method.
    """

    def __init__(self, real: sqlite3.Connection, message: str) -> None:
        self._real = real
        self._message = message

    def backup(self, *args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError(self._message)

    def close(self) -> None:
        self._real.close()


def _fail_the_copy(
    monkeypatch: pytest.MonkeyPatch, source: Path, message: str
) -> None:
    """Make the source connection's ``backup()`` raise, however it was opened.

    Matched on the source filename rather than on the ``mode=ro`` URI, so this
    injects the failure against both the current code and the pre-fix version
    that opened the source read-write — i.e. it is a real regression test.
    """
    real_connect = sqlite3.connect

    def fake_connect(target, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        conn = real_connect(target, *args, **kwargs)
        name = Path(str(target).removeprefix("file:").split("?")[0]).name
        # The destination stays a real connection so the partial file genuinely
        # gets created and has to be cleaned up.
        if name == source.name:
            return _SourceThatFailsMidCopy(conn, message)
        return conn

    monkeypatch.setattr(db_backup.sqlite3, "connect", fake_connect)


def test_failed_backup_leaves_no_file_at_the_real_name(
    source_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before the atomic-rename fix this left a truncated .db as newest."""
    backup_dir = tmp_path / "backups"
    _fail_the_copy(monkeypatch, source_db, "disk I/O error")

    with pytest.raises(sqlite3.OperationalError):
        _run_backup(source_db, backup_dir)

    assert list(backup_dir.glob("*.db")) == []
    assert list(backup_dir.glob("*.partial")) == []


def test_failed_backup_does_not_prune_existing_backups(
    source_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure must be fail-safe for retention — old copies stay put."""
    backup_dir = tmp_path / "backups"
    keepers = [
        _make_backup(backup_dir, f"dungeonkeeper_2026080{i}_000000.db", age_hours=200 + i)
        for i in range(1, 8)
    ]
    _fail_the_copy(monkeypatch, source_db, "disk full")

    with pytest.raises(sqlite3.OperationalError):
        _run_backup(source_db, backup_dir, retention_count=2)

    assert all(p.exists() for p in keepers)


# --------------------------------------------------------------------------
# B2 — restart bursts must not burn the recovery window
# --------------------------------------------------------------------------


def test_backup_is_skipped_when_a_recent_one_exists(
    source_db: Path, tmp_path: Path
) -> None:
    backup_dir = tmp_path / "backups"
    _make_backup(backup_dir, "dungeonkeeper_20260805_120000.db", age_hours=0.25)

    result = _run_backup(source_db, backup_dir, min_gap_seconds=3 * _HOUR)

    assert result is None
    assert len(list(backup_dir.glob("dungeonkeeper_*.db"))) == 1


def test_backup_proceeds_once_the_gap_has_elapsed(
    source_db: Path, tmp_path: Path
) -> None:
    backup_dir = tmp_path / "backups"
    _make_backup(backup_dir, "dungeonkeeper_20260805_060000.db", age_hours=4)

    result = _run_backup(source_db, backup_dir, min_gap_seconds=3 * _HOUR)

    assert result is not None
    assert len(list(backup_dir.glob("dungeonkeeper_*.db"))) == 2


def test_backup_proceeds_when_no_backups_exist_yet(
    source_db: Path, tmp_path: Path
) -> None:
    result = _run_backup(source_db, tmp_path / "backups", min_gap_seconds=3 * _HOUR)

    assert result is not None


def test_manual_backup_ignores_the_skip_gap(source_db: Path) -> None:
    """run_backup_now is a deliberate act — it must never be skipped."""
    first = run_backup_now(source_db)
    assert first is not None

    second = run_backup_now(source_db)

    assert second is not None


# --------------------------------------------------------------------------
# Pruning
# --------------------------------------------------------------------------


def test_prune_removes_backups_beyond_retention(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    old = [
        _make_backup(backup_dir, f"dungeonkeeper_2026080{i}_000000.db", age_hours=100 + i)
        for i in range(1, 8)
    ]

    _prune_old_backups(backup_dir, "dungeonkeeper", retention_count=3)

    survivors = sorted(p.name for p in backup_dir.glob("*.db"))
    assert len(survivors) == 3
    # The three youngest (smallest age) survive.
    assert survivors == sorted(p.name for p in old[:3])


def test_prune_keeps_backups_inside_the_age_floor(tmp_path: Path) -> None:
    """A restart burst makes 8 backups in an hour; none may be pruned."""
    backup_dir = tmp_path / "backups"
    burst = [
        _make_backup(backup_dir, f"dungeonkeeper_20260805_00000{i}.db", age_hours=0.1 * i)
        for i in range(1, 9)
    ]

    _prune_old_backups(
        backup_dir, "dungeonkeeper", retention_count=5, min_age_hours=DEFAULT_MIN_AGE_HOURS
    )

    assert all(p.exists() for p in burst)


def test_prune_removes_aged_out_backups_past_the_floor(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    fresh = [
        _make_backup(backup_dir, f"dungeonkeeper_20260805_00000{i}.db", age_hours=i)
        for i in range(1, 4)
    ]
    stale = _make_backup(backup_dir, "dungeonkeeper_20260701_000000.db", age_hours=200)

    _prune_old_backups(backup_dir, "dungeonkeeper", retention_count=2, min_age_hours=48)

    assert all(p.exists() for p in fresh)  # inside the floor
    assert not stale.exists()  # beyond count *and* beyond the floor


def test_prune_removes_wal_and_shm_sidecars(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    _make_backup(backup_dir, "dungeonkeeper_20260805_000001.db", age_hours=100)
    doomed = _make_backup(backup_dir, "dungeonkeeper_20260701_000000.db", age_hours=200)
    shm = backup_dir / (doomed.name + "-shm")
    wal = backup_dir / (doomed.name + "-wal")
    shm.write_bytes(b"x")
    wal.write_bytes(b"")

    _prune_old_backups(backup_dir, "dungeonkeeper", retention_count=1, min_age_hours=48)

    assert not doomed.exists()
    assert not shm.exists()
    assert not wal.exists()


def test_prune_leaves_ad_hoc_snapshots_alone(tmp_path: Path) -> None:
    """Hand-made copies are deliberately out of scope — see finding B5."""
    backup_dir = tmp_path / "backups"
    manual = _make_backup(
        backup_dir, "dungeonkeeper-pre-tod-backfill-20260729.db", age_hours=500
    )
    _make_backup(backup_dir, "dungeonkeeper_20260805_000001.db", age_hours=100)

    _prune_old_backups(backup_dir, "dungeonkeeper", retention_count=1, min_age_hours=48)

    assert manual.exists()


# --------------------------------------------------------------------------
# B8 — environment overrides
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("12", 12.0, id="valid"),
        pytest.param("1.5", 1.5, id="fractional"),
        pytest.param("", 6.0, id="unset-falls-back"),
        pytest.param("   ", 6.0, id="whitespace-falls-back"),
        pytest.param("nightly", 6.0, id="non-numeric-falls-back"),
        pytest.param("0", 6.0, id="zero-rejected"),
        pytest.param("-3", 6.0, id="negative-rejected"),
    ],
)
def test_env_float_parsing(
    raw: str, expected: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DB_BACKUP_INTERVAL_HOURS", raw)

    assert _env_float("DB_BACKUP_INTERVAL_HOURS", 6.0) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("9", 9, id="valid"),
        pytest.param("", 5, id="unset-falls-back"),
        pytest.param("lots", 5, id="non-numeric-falls-back"),
        pytest.param("0", 5, id="zero-rejected-would-delete-everything"),
        pytest.param("-1", 5, id="negative-rejected"),
    ],
)
def test_env_int_parsing(raw: str, expected: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_BACKUP_RETENTION", raw)

    assert _env_int("DB_BACKUP_RETENTION", 5) == expected


def test_env_defaults_apply_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DB_BACKUP_INTERVAL_HOURS", raising=False)
    monkeypatch.delenv("DB_BACKUP_RETENTION", raising=False)

    assert _env_float("DB_BACKUP_INTERVAL_HOURS", db_backup.DEFAULT_INTERVAL_HOURS) == 6.0
    assert _env_int("DB_BACKUP_RETENTION", db_backup.DEFAULT_RETENTION_COUNT) == 5
