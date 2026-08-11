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
migration, a bad bulk UPDATE), not against disk failure. The off-device copy
that does cover disk failure is ``scripts/backup_to_nas.sh`` on its own systemd
timer; this module only *reports* on it (see :func:`gather_backup_health`). See
``docs/reviews/2026-08-06-backup-disaster-recovery.md`` (B1) and
``docs/disaster_recovery_runbook.md``.

Outcomes are recorded to ``config`` and surfaced on the dashboard — finding B3.
A backup that quietly stops is the dangerous failure, because nothing else
changes: the old files sit there looking healthy while the newest one ages out
of usefulness. journald has the ERROR, but only for someone who thinks to look.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_log = logging.getLogger("dungeonkeeper.backup")

# Defaults — each overridable via the matching environment variable.
DEFAULT_INTERVAL_HOURS = 6.0
DEFAULT_RETENTION_COUNT = 5  # keep last N backups
DEFAULT_MIN_AGE_HOURS = 48.0  # never prune a backup younger than this

# A backup is skipped when a newer one already exists within this fraction of
# the interval. The loop backs up on startup, so without this every restart
# spends a retention slot on a near-duplicate (finding B2).
_SKIP_IF_NEWER_THAN_FRACTION = 0.5

# ── Outcome markers (finding B3) ─────────────────────────────────────────────
# Global (guild_id=0) keys, written by the loop and read by the dashboard.
# Deliberately NOT in settings_registry: these are bot-written state, not
# settings anyone may edit.
CONFIG_LAST_OK_AT = "backup_last_ok_at"
CONFIG_CONSECUTIVE_FAILURES = "backup_consecutive_failures"
CONFIG_LAST_ERROR = "backup_last_error"

# A backup is "stale" once it is this many intervals old — one missed run is
# noise (a restart lands mid-cycle), two means the loop is not running.
STALE_AFTER_INTERVALS = 2.0

# The NAS timer is daily (deploy/dk-nas-backup.timer), so the same 2× rule puts
# the off-device copy's patience at 48h.
NAS_EXPECTED_INTERVAL_HOURS = 24.0

# Where scripts/backup_to_nas.sh leaves its breadcrumb, and the config file it
# reads. Both are parsed rather than assumed so the two cannot drift: the conf
# is what the script actually uses.
NAS_CONF_PATH = "~/.config/dk-backup/nas.conf"
NAS_STATUS_FILE_DEFAULT = "~/.local/state/dk-backup/last-nas-sync"

Severity = Literal["error", "warning"]


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
        except Exception as exc:
            # Still swallowed so one bad run doesn't kill the loop — but no
            # longer silent: the failure is recorded where the dashboard can
            # see it (finding B3). A deliberate skip is NOT a failure and does
            # not touch the markers; only a real exception does.
            _log.exception("Database backup failed")
            await asyncio.to_thread(_record_failure, db_path, exc)
        else:
            await asyncio.to_thread(_record_success, db_path)
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


# ── Outcome markers and health (finding B3) ──────────────────────────────────


def _record_success(db_path: Path) -> None:
    """Stamp a successful run and clear the failure streak.

    Never raises: a marker that cannot be written must not take the backup loop
    down with it. Losing the marker degrades to "looks stale", which is the
    safe direction to fail — it over-reports rather than under-reports.
    """
    from bot_modules.core.db_utils import open_db, set_config_value

    try:
        with open_db(db_path) as conn:
            set_config_value(conn, CONFIG_LAST_OK_AT, str(int(time.time())), 0)
            set_config_value(conn, CONFIG_CONSECUTIVE_FAILURES, "0", 0)
            set_config_value(conn, CONFIG_LAST_ERROR, "", 0)
    except Exception:  # pragma: no cover — defensive
        _log.exception("Could not record backup success marker")


def _record_failure(db_path: Path, exc: BaseException) -> None:
    """Increment the failure streak and store the last error text.

    Best-effort for the same reason as :func:`_record_success` — and more so
    here, since the most likely cause of a backup failure (a sick database) is
    also the most likely cause of this write failing.
    """
    from bot_modules.core.db_utils import get_config_value, open_db, set_config_value

    try:
        with open_db(db_path) as conn:
            try:
                streak = int(get_config_value(conn, CONFIG_CONSECUTIVE_FAILURES, "0", 0))
            except ValueError:
                streak = 0
            set_config_value(conn, CONFIG_CONSECUTIVE_FAILURES, str(streak + 1), 0)
            # Type plus message: "OperationalError: disk I/O error" says more
            # at 3am than either half alone.
            detail = f"{type(exc).__name__}: {exc}"[:500]
            set_config_value(conn, CONFIG_LAST_ERROR, detail, 0)
    except Exception:  # pragma: no cover — defensive
        _log.exception("Could not record backup failure marker")


@dataclass(frozen=True)
class BackupProblem:
    """One thing wrong with the backups, in dashboard-ready form."""

    code: str
    severity: Severity
    title: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "title": self.title,
            "message": self.message,
        }


def _describe_age(seconds: float) -> str:
    """Human age: '3h', '2d 4h', '45m'. Rounded down — never overstates."""
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    return f"{days}d" if hours == 0 else f"{days}d {hours}h"


def assess_backup_health(
    *,
    local_age_seconds: float | None,
    local_count: int,
    interval_hours: float,
    consecutive_failures: int,
    last_error: str,
    nas_configured: bool,
    nas_age_seconds: float | None,
) -> list[BackupProblem]:
    """Decide what (if anything) is wrong. Pure — all IO is the caller's.

    Ordered worst-first so a caller that truncates keeps the important one.
    ``local_age_seconds is None`` means no backup file exists at all;
    ``nas_age_seconds is None`` with ``nas_configured`` means the off-device
    job is set up but has never reported a successful sync.
    """
    problems: list[BackupProblem] = []

    if local_age_seconds is None or local_count == 0:
        problems.append(
            BackupProblem(
                code="backup_none",
                severity="error",
                title="No database backup exists",
                message=(
                    "Nothing has been written to the backups directory. "
                    "If the bot has only just started, wait one cycle."
                ),
            )
        )
    else:
        stale_after = interval_hours * STALE_AFTER_INTERVALS * 3600
        if local_age_seconds > stale_after:
            problems.append(
                BackupProblem(
                    code="backup_stale",
                    severity="error",
                    title=f"Database backup is {_describe_age(local_age_seconds)} old",
                    message=(
                        f"Backups should run every {interval_hours:g}h. The newest "
                        "one is well past that, so the backup loop is probably not "
                        "running — check the bot's log."
                    ),
                )
            )

    if consecutive_failures > 0:
        # Distinguish "one bad run, likely transient" from "this is broken and
        # staying broken" — the second wants attention tonight.
        severity: Severity = "error" if consecutive_failures >= 3 else "warning"
        plural = "" if consecutive_failures == 1 else "s"
        problems.append(
            BackupProblem(
                code="backup_failing",
                severity=severity,
                title=(
                    "Last backup attempt failed"
                    if consecutive_failures == 1
                    else f"{consecutive_failures} backup attempts in a row failed"
                ),
                message=(
                    f"{consecutive_failures} consecutive failure{plural}. "
                    f"Last error: {last_error}"
                    if last_error
                    else f"{consecutive_failures} consecutive failure{plural}."
                ),
            )
        )

    if nas_configured:
        nas_stale_after = NAS_EXPECTED_INTERVAL_HOURS * STALE_AFTER_INTERVALS * 3600
        if nas_age_seconds is None:
            problems.append(
                BackupProblem(
                    code="offsite_never",
                    severity="error",
                    title="Off-device backup has never run",
                    message=(
                        "The NAS copy is configured but has never reported a "
                        "successful sync. Check: systemctl status dk-nas-backup"
                    ),
                )
            )
        elif nas_age_seconds > nas_stale_after:
            problems.append(
                BackupProblem(
                    code="offsite_stale",
                    severity="error",
                    title=(
                        f"Off-device backup is {_describe_age(nas_age_seconds)} old"
                    ),
                    message=(
                        "The NAS copy runs daily. Everything on this machine is on "
                        "one disk, so while this is stale a disk failure loses "
                        "everything since the last sync. Check: "
                        "systemctl status dk-nas-backup"
                    ),
                )
            )

    return problems


def _read_nas_conf(conf_path: Path) -> dict[str, str]:
    """Parse the ``KEY=value  # comment`` lines of the NAS backup config."""
    values: dict[str, str] = {}
    try:
        text = conf_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        # Trailing comments are the norm in this file ("NAS_HOST=1.2.3.4  # name")
        values[key.strip()] = raw.split("#", 1)[0].strip()
    return values


def gather_backup_health(
    db_path: Path,
    *,
    now: float | None = None,
    nas_conf_path: Path | None = None,
) -> dict:
    """Collect backup state from disk and ``config``, and diagnose it.

    Read-only and cheap (a directory listing plus one config read), so it is
    safe to call from a polled dashboard endpoint.
    """
    now = time.time() if now is None else now
    backup_dir = db_path.parent / "backups"
    interval_hours = _env_float("DB_BACKUP_INTERVAL_HOURS", DEFAULT_INTERVAL_HOURS)

    files = sorted(
        backup_dir.glob(f"{db_path.stem}_*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ) if backup_dir.is_dir() else []

    newest_at = files[0].stat().st_mtime if files else None
    total_bytes = sum(p.stat().st_size for p in files)

    last_ok_at: float | None = None
    consecutive_failures = 0
    last_error = ""
    try:
        from bot_modules.core.db_utils import get_config_value, open_db

        with open_db(db_path) as conn:
            raw_ok = get_config_value(conn, CONFIG_LAST_OK_AT, "", 0)
            last_ok_at = float(raw_ok) if raw_ok else None
            consecutive_failures = int(
                get_config_value(conn, CONFIG_CONSECUTIVE_FAILURES, "0", 0) or 0
            )
            last_error = get_config_value(conn, CONFIG_LAST_ERROR, "", 0)
    except (ValueError, sqlite3.Error):
        # Unreadable markers must not blank the whole panel — the filesystem
        # numbers above are the load-bearing half and are already gathered.
        _log.debug("Backup markers unreadable", exc_info=True)

    conf_path = (
        Path(NAS_CONF_PATH).expanduser() if nas_conf_path is None else nas_conf_path
    )
    conf = _read_nas_conf(conf_path)
    nas_configured = bool(conf)
    status_file = Path(
        conf.get("STATUS_FILE") or NAS_STATUS_FILE_DEFAULT
    ).expanduser()
    try:
        nas_synced_at: float | None = status_file.stat().st_mtime
    except OSError:
        nas_synced_at = None

    problems = assess_backup_health(
        local_age_seconds=(now - newest_at) if newest_at is not None else None,
        local_count=len(files),
        interval_hours=interval_hours,
        consecutive_failures=consecutive_failures,
        last_error=last_error,
        nas_configured=nas_configured,
        nas_age_seconds=(now - nas_synced_at) if nas_synced_at is not None else None,
    )

    return {
        "local": {
            "newest_at": newest_at,
            "age_seconds": (now - newest_at) if newest_at is not None else None,
            "count": len(files),
            "total_bytes": total_bytes,
            "interval_hours": interval_hours,
            "last_ok_at": last_ok_at,
            "consecutive_failures": consecutive_failures,
            "last_error": last_error,
        },
        "offsite": {
            "configured": nas_configured,
            "host": conf.get("NAS_HOST", ""),
            "retention_days": conf.get("RETENTION_DAYS", ""),
            "synced_at": nas_synced_at,
            "age_seconds": (
                (now - nas_synced_at) if nas_synced_at is not None else None
            ),
        },
        "problems": [p.to_dict() for p in problems],
    }


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
