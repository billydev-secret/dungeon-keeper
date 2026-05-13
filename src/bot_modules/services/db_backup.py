"""Automated SQLite database backup service.

Runs as a background task, creating periodic backups using SQLite's
online backup API (safe even while the database is in use).

Backups are stored alongside the main database with timestamped names.
Old backups beyond the retention count are automatically pruned.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path

_log = logging.getLogger("dungeonkeeper.backup")

# Defaults — configurable via environment variables
DEFAULT_INTERVAL_HOURS = 6
DEFAULT_RETENTION_COUNT = 5  # keep last N backups


async def db_backup_loop(
    bot,  # noqa: ANN001 — discord.Client
    db_path: Path,
    interval_hours: float = DEFAULT_INTERVAL_HOURS,
    retention_count: int = DEFAULT_RETENTION_COUNT,
) -> None:
    """Periodically back up the SQLite database.

    Uses SQLite's ``connection.backup()`` for a consistent snapshot
    even under concurrent writes (WAL mode).
    """
    await bot.wait_until_ready()
    backup_dir = db_path.parent / "backups"
    interval_seconds = interval_hours * 3600

    _log.info(
        "DB backup loop started: every %.1fh, keeping %d backups in %s",
        interval_hours,
        retention_count,
        backup_dir,
    )

    while not bot.is_closed():
        try:
            await asyncio.to_thread(
                _run_backup, db_path, backup_dir, retention_count
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("Database backup failed")
        await asyncio.sleep(interval_seconds)


def _run_backup(
    db_path: Path,
    backup_dir: Path,
    retention_count: int,
) -> None:
    """Perform a single backup (blocking — run in a thread)."""
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_name = f"{db_path.stem}_{timestamp}.db"
    backup_path = backup_dir / backup_name

    _log.info("Starting backup → %s", backup_path.name)
    start = time.monotonic()

    # Use SQLite online backup API for consistency
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(backup_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    size_mb = backup_path.stat().st_size / (1024 * 1024)
    elapsed = time.monotonic() - start
    _log.info(
        "Backup complete: %s (%.1f MB, %.1fs)",
        backup_path.name,
        size_mb,
        elapsed,
    )

    # Prune old backups beyond retention count
    _prune_old_backups(backup_dir, db_path.stem, retention_count)


def _prune_old_backups(
    backup_dir: Path,
    stem: str,
    retention_count: int,
) -> None:
    """Remove oldest backups exceeding ``retention_count``."""
    backups = sorted(
        backup_dir.glob(f"{stem}_*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in backups[retention_count:]:
        _log.info("Pruning old backup: %s", old.name)
        old.unlink(missing_ok=True)


def run_backup_now(db_path: Path, retention_count: int = DEFAULT_RETENTION_COUNT) -> Path:
    """Run a one-off backup immediately (for use from commands or scripts).

    Returns the path to the new backup file.
    """
    backup_dir = db_path.parent / "backups"
    _run_backup(db_path, backup_dir, retention_count)
    # Return the newest backup
    backups = sorted(backup_dir.glob(f"{db_path.stem}_*.db"), key=lambda p: p.stat().st_mtime)
    return backups[-1]
