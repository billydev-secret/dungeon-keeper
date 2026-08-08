"""Automated SQLite database backup service.

Runs as a background task, creating periodic backups using SQLite's
online backup API (safe even while the database is in use).

Backups are stored alongside the main database with timestamped names.
Old backups beyond the retention count are automatically pruned, subject
to an age floor so a burst of restarts cannot erase the recovery window.

Tunable via environment (see ``_env_float`` / ``_env_int``):
``DB_BACKUP_INTERVAL_HOURS``, ``DB_BACKUP_RETENTION``,
``DB_BACKUP_MIN_AGE_HOURS``.

What this is **not**: backups land in ``<db-parent>/backups`` — the same
filesystem as the database itself. That defends against logical damage (a bad
migration, a bad bulk UPDATE), not against disk failure. See
``docs/reviews/2026-08-06-backup-disaster-recovery.md`` (B1) and
``docs/disaster_recovery_runbook.md``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from pathlib import Path

_log = logging.getLogger("dungeonkeeper.backup")

# Defaults — each overridable via the matching environment variable.
DEFAULT_INTERVAL_HOURS = 6.0
DEFAULT_RETENTION_COUNT = 5  # keep last N backups
DEFAULT_MIN_AGE_HOURS = 48.0  # never prune a backup younger than this

# A backup is skipped when a newer one already exists within this fraction of
# the interval. The loop backs up on startup, so without this every restart
# spends a retention slot on a near-duplicate (finding B2).
_SKIP_IF_NEWER_THAN_FRACTION = 0.5


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        _log.warning("Ignoring non-numeric %s=%r; using %s", name, raw, default)
        return default
    if value <= 0:
        _log.warning("Ignoring non-positive %s=%r; using %s", name, raw, default)
        return default
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        _log.warning("Ignoring non-numeric %s=%r; using %s", name, raw, default)
        return default
    if value < 1:
        _log.warning("Ignoring non-positive %s=%r; using %s", name, raw, default)
        return default
    return value


async def db_backup_loop(
    bot,  # noqa: ANN001 — discord.Client
    db_path: Path,
    interval_hours: float | None = None,
    retention_count: int | None = None,
    min_age_hours: float | None = None,
) -> None:
    """Periodically back up the SQLite database.

    Uses SQLite's ``connection.backup()`` for a consistent snapshot
    even under concurrent writes (WAL mode).
    """
    await bot.wait_until_ready()
    backup_dir = db_path.parent / "backups"

    if interval_hours is None:
        interval_hours = _env_float("DB_BACKUP_INTERVAL_HOURS", DEFAULT_INTERVAL_HOURS)
    if retention_count is None:
        retention_count = _env_int("DB_BACKUP_RETENTION", DEFAULT_RETENTION_COUNT)
    if min_age_hours is None:
        min_age_hours = _env_float("DB_BACKUP_MIN_AGE_HOURS", DEFAULT_MIN_AGE_HOURS)

    interval_seconds = interval_hours * 3600
    min_gap_seconds = interval_seconds * _SKIP_IF_NEWER_THAN_FRACTION

    _log.info(
        "DB backup loop started: every %.1fh, keeping %d backups "
        "(never pruning any younger than %.1fh) in %s",
        interval_hours,
        retention_count,
        min_age_hours,
        backup_dir,
    )

    while not bot.is_closed():
        try:
            await asyncio.to_thread(
                _run_backup,
                db_path,
                backup_dir,
                retention_count,
                min_age_hours,
                min_gap_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Deliberately swallowed so one bad run doesn't kill the loop.
            # NOTE: nothing escalates this beyond the log — see finding B3.
            _log.exception("Database backup failed")
        await asyncio.sleep(interval_seconds)


def _newest_backup_age(backup_dir: Path, stem: str) -> float | None:
    """Seconds since the newest existing backup, or None if there are none."""
    backups = list(backup_dir.glob(f"{stem}_*.db"))
    if not backups:
        return None
    return time.time() - max(p.stat().st_mtime for p in backups)


def _run_backup(
    db_path: Path,
    backup_dir: Path,
    retention_count: int = DEFAULT_RETENTION_COUNT,
    min_age_hours: float = DEFAULT_MIN_AGE_HOURS,
    min_gap_seconds: float = 0.0,
) -> Path | None:
    """Perform a single backup (blocking — run in a thread).

    Returns the new backup's path, or ``None`` when the backup was skipped
    because a recent enough one already exists.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)

    if min_gap_seconds > 0:
        age = _newest_backup_age(backup_dir, db_path.stem)
        if age is not None and age < min_gap_seconds:
            _log.info(
                "Skipping backup — newest is only %.1f min old (min gap %.1f min)",
                age / 60,
                min_gap_seconds / 60,
            )
            return None

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_name = f"{db_path.stem}_{timestamp}.db"
    backup_path = backup_dir / backup_name
    # Build under a temp name so a failed copy can never occupy the real
    # filename — otherwise the truncated file is the newest by mtime and
    # gets handed out as "the latest backup" (finding B4).
    partial_path = backup_path.with_name(backup_path.name + ".partial")

    _log.info("Starting backup → %s", backup_path.name)
    start = time.monotonic()

    # Use SQLite online backup API for consistency. Read-only on the source:
    # this path must never be able to write to the live database.
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(partial_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    except BaseException:
        partial_path.unlink(missing_ok=True)
        _cleanup_sidecars(partial_path)
        raise
    finally:
        src.close()

    _cleanup_sidecars(partial_path)
    os.replace(partial_path, backup_path)  # atomic within the filesystem

    size_mb = backup_path.stat().st_size / (1024 * 1024)
    elapsed = time.monotonic() - start
    _log.info(
        "Backup complete: %s (%.1f MB, %.1fs)",
        backup_path.name,
        size_mb,
        elapsed,
    )

    # Prune old backups beyond retention count
    _prune_old_backups(backup_dir, db_path.stem, retention_count, min_age_hours)
    return backup_path


def _cleanup_sidecars(path: Path) -> None:
    """Remove the ``-wal``/``-shm`` siblings SQLite leaves next to ``path``."""
    for suffix in ("-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)


def _prune_old_backups(
    backup_dir: Path,
    stem: str,
    retention_count: int,
    min_age_hours: float = DEFAULT_MIN_AGE_HOURS,
) -> None:
    """Remove oldest backups exceeding ``retention_count``.

    A backup younger than ``min_age_hours`` is kept regardless of position, so
    a burst of restarts cannot evict the whole recovery window (finding B2).
    Only the rotation's own ``<stem>_*.db`` files are considered — hand-made
    snapshots under other names are deliberately left alone, and should live
    in a sibling directory rather than here (finding B5).
    """
    now = time.time()
    floor_seconds = min_age_hours * 3600
    backups = sorted(
        backup_dir.glob(f"{stem}_*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in backups[retention_count:]:
        if (now - old.stat().st_mtime) < floor_seconds:
            continue  # inside the guaranteed window — keep it
        _log.info("Pruning old backup: %s", old.name)
        old.unlink(missing_ok=True)
        _cleanup_sidecars(old)


def run_backup_now(
    db_path: Path,
    retention_count: int = DEFAULT_RETENTION_COUNT,
    min_age_hours: float = DEFAULT_MIN_AGE_HOURS,
) -> Path:
    """Run a one-off backup immediately (for use from commands or scripts).

    Returns the path to the new backup file. Unconditional — no skip gap, so a
    deliberate manual backup is always taken.
    """
    backup_dir = db_path.parent / "backups"
    path = _run_backup(db_path, backup_dir, retention_count, min_age_hours)
    if path is None:  # pragma: no cover — min_gap_seconds is 0 here
        raise RuntimeError("Backup unexpectedly skipped")
    return path
