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


# --------------------------------------------------------------------------
# B3 — a failing or stalled backup must stop being invisible.
#
# The dangerous failure is not a crash, it is silence: the old files sit there
# looking healthy while the newest one ages out of usefulness. These cover the
# markers the loop writes and the verdicts the dashboard renders from them.
# --------------------------------------------------------------------------

_HEALTHY = {
    "local_age_seconds": 2 * _HOUR,
    "local_count": 5,
    "interval_hours": 6.0,
    "consecutive_failures": 0,
    "last_error": "",
    "nas_configured": True,
    "nas_age_seconds": 6 * _HOUR,
}


def _assess(**overrides):
    return db_backup.assess_backup_health(**{**_HEALTHY, **overrides})


def _codes(problems) -> set[str]:
    return {p.code for p in problems}


def _add_config_table(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS config (
               guild_id INTEGER NOT NULL DEFAULT 0,
               key TEXT NOT NULL,
               value TEXT,
               PRIMARY KEY (guild_id, key)
           )"""
    )
    conn.commit()
    conn.close()


def test_healthy_backups_report_nothing() -> None:
    assert _assess() == []


def test_missing_backups_are_an_error() -> None:
    problems = _assess(local_age_seconds=None, local_count=0)

    assert _codes(problems) == {"backup_none"}
    assert problems[0].severity == "error"


@pytest.mark.parametrize(
    ("age_hours", "flagged"),
    [
        pytest.param(6.5, False, id="one-interval-late-is-noise"),
        pytest.param(11.9, False, id="just-inside-two-intervals"),
        pytest.param(12.1, True, id="past-two-intervals-is-stale"),
        pytest.param(48.0, True, id="two-days-stale"),
    ],
)
def test_local_staleness_threshold_is_two_intervals(
    age_hours: float, flagged: bool
) -> None:
    problems = _assess(local_age_seconds=age_hours * _HOUR)

    assert ("backup_stale" in _codes(problems)) is flagged


def test_stale_title_names_the_age() -> None:
    (problem,) = _assess(local_age_seconds=19 * _HOUR)

    assert problem.code == "backup_stale"
    assert "19h" in problem.title


@pytest.mark.parametrize(
    ("failures", "severity"),
    [
        pytest.param(0, None, id="no-failures-no-problem"),
        pytest.param(1, "warning", id="one-failure-is-a-warning"),
        pytest.param(2, "warning", id="two-still-a-warning"),
        pytest.param(3, "error", id="three-in-a-row-is-broken"),
    ],
)
def test_failure_streak_escalates(failures: int, severity: str | None) -> None:
    problems = [p for p in _assess(consecutive_failures=failures) if p.code == "backup_failing"]

    if severity is None:
        assert problems == []
    else:
        assert problems[0].severity == severity


def test_failure_message_carries_the_last_error() -> None:
    (problem,) = _assess(consecutive_failures=2, last_error="OperationalError: disk I/O error")

    assert "disk I/O error" in problem.message


def test_unconfigured_offsite_is_silent_not_a_fault() -> None:
    # A dev box has no NAS config. Reporting "off-device backup missing" there
    # would be crying wolf, and a warning that fires everywhere gets ignored
    # on the one machine where it matters.
    assert _assess(nas_configured=False, nas_age_seconds=None) == []


def test_configured_but_never_synced_offsite_is_an_error() -> None:
    problems = _assess(nas_age_seconds=None)

    assert _codes(problems) == {"offsite_never"}


@pytest.mark.parametrize(
    ("age_hours", "flagged"),
    [
        pytest.param(25.0, False, id="a-day-late-is-fine"),
        pytest.param(47.0, False, id="just-inside-two-days"),
        pytest.param(49.0, True, id="past-two-days-is-stale"),
    ],
)
def test_offsite_staleness_threshold_is_two_days(age_hours: float, flagged: bool) -> None:
    problems = _assess(nas_age_seconds=age_hours * _HOUR)

    assert ("offsite_stale" in _codes(problems)) is flagged


def test_local_and_offsite_fail_independently() -> None:
    # The exact live state on 2026-08-11: local rotation healthy, off-device
    # timer dead since install. One green row must not hide the other.
    problems = _assess(nas_age_seconds=5 * 24 * _HOUR)

    assert _codes(problems) == {"offsite_stale"}


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        pytest.param(45 * 60, "45m", id="minutes"),
        pytest.param(3 * _HOUR, "3h", id="hours"),
        pytest.param(2 * 24 * _HOUR, "2d", id="whole-days"),
        pytest.param(2 * 24 * _HOUR + 4 * _HOUR, "2d 4h", id="days-and-hours"),
    ],
)
def test_describe_age(seconds: float, expected: str) -> None:
    assert db_backup._describe_age(seconds) == expected


def test_missing_nas_conf_reads_as_unconfigured() -> None:
    assert db_backup._read_nas_conf(Path("/nonexistent/nas.conf")) == {}


def test_nas_conf_reads_keys_with_trailing_comments(tmp_path: Path) -> None:
    path = tmp_path / "nas.conf"
    path.write_text(
        "# a comment line\n"
        "NAS_HOST=192.168.174.3          # NaturewoodNAS\n"
        "\n"
        "RETENTION_DAYS=14\n"
        "STATUS_FILE=/home/ben/.local/state/dk-backup/last-nas-sync\n",
        encoding="utf-8",
    )

    conf = db_backup._read_nas_conf(path)

    assert conf["NAS_HOST"] == "192.168.174.3"
    assert conf["RETENTION_DAYS"] == "14"
    assert conf["STATUS_FILE"].endswith("last-nas-sync")


def test_success_marker_clears_a_failure_streak(source_db: Path) -> None:
    _add_config_table(source_db)

    db_backup._record_failure(source_db, RuntimeError("disk full"))
    db_backup._record_failure(source_db, RuntimeError("disk full"))
    health = db_backup.gather_backup_health(source_db, nas_conf_path=Path("/nope"))
    assert health["local"]["consecutive_failures"] == 2
    assert "disk full" in health["local"]["last_error"]

    db_backup._record_success(source_db)
    health = db_backup.gather_backup_health(source_db, nas_conf_path=Path("/nope"))

    assert health["local"]["consecutive_failures"] == 0
    assert health["local"]["last_error"] == ""
    assert health["local"]["last_ok_at"] is not None


def test_failure_marker_records_type_and_message(source_db: Path) -> None:
    _add_config_table(source_db)

    db_backup._record_failure(source_db, sqlite3.OperationalError("disk I/O error"))
    health = db_backup.gather_backup_health(source_db, nas_conf_path=Path("/nope"))

    assert health["local"]["last_error"] == "OperationalError: disk I/O error"


def test_markers_never_raise_when_the_database_is_unwritable(tmp_path: Path) -> None:
    # The likeliest cause of a backup failing is a sick database — which is
    # also the likeliest cause of recording that failure failing. Neither may
    # take the loop down.
    missing = tmp_path / "nothing" / "dungeonkeeper.db"

    db_backup._record_failure(missing, RuntimeError("boom"))
    db_backup._record_success(missing)


def test_gather_reports_the_real_backup_directory(source_db: Path) -> None:
    _add_config_table(source_db)
    backup_dir = source_db.parent / "backups"
    _make_backup(backup_dir, "dungeonkeeper_20260810_034715.db", age_hours=20)
    _make_backup(backup_dir, "dungeonkeeper_20260811_034726.db", age_hours=2)
    # Ad-hoc snapshots are not part of the rotation and must not be counted as
    # one (B5) — a hand-made copy is not evidence the loop is running.
    _make_backup(backup_dir, "dungeonkeeper-pre-migration.db", age_hours=1)

    health = db_backup.gather_backup_health(source_db, nas_conf_path=Path("/nope"))

    assert health["local"]["count"] == 2
    assert 1.5 * _HOUR < health["local"]["age_seconds"] < 2.5 * _HOUR
    assert health["offsite"]["configured"] is False
    assert health["problems"] == []


def test_gather_flags_an_empty_backup_directory(source_db: Path) -> None:
    _add_config_table(source_db)

    health = db_backup.gather_backup_health(source_db, nas_conf_path=Path("/nope"))

    assert [p["code"] for p in health["problems"]] == ["backup_none"]


def test_gather_reads_the_nas_breadcrumb(source_db: Path, tmp_path: Path) -> None:
    _add_config_table(source_db)
    _make_backup(source_db.parent / "backups", "dungeonkeeper_20260811_034726.db")
    status = tmp_path / "last-nas-sync"
    status.write_text("2026-08-05T04:30:00-07:00", encoding="utf-8")
    _age(status, 5 * 24)
    conf = tmp_path / "nas.conf"
    conf.write_text(
        f"NAS_HOST=192.168.174.3\nRETENTION_DAYS=14\nSTATUS_FILE={status}\n",
        encoding="utf-8",
    )

    health = db_backup.gather_backup_health(source_db, nas_conf_path=conf)

    assert health["offsite"]["configured"] is True
    assert health["offsite"]["host"] == "192.168.174.3"
    assert [p["code"] for p in health["problems"]] == ["offsite_stale"]


async def _noop_sleep(_seconds: float) -> None:
    """Collapse the 6-hour wait so a loop test finishes instantly."""
    return None


class _FakeBot:
    """Bot stand-in that lets the loop run exactly ``passes`` iterations."""

    def __init__(self, passes: int = 1) -> None:
        self._left = passes

    async def wait_until_ready(self) -> None:
        return None

    def is_closed(self) -> bool:
        if self._left <= 0:
            return True
        self._left -= 1
        return False


@pytest.mark.asyncio
async def test_loop_records_success(source_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _add_config_table(source_db)
    monkeypatch.setattr(db_backup.asyncio, "sleep", _noop_sleep)

    await db_backup.db_backup_loop(_FakeBot(), source_db)

    health = db_backup.gather_backup_health(source_db, nas_conf_path=Path("/nope"))
    assert health["local"]["last_ok_at"] is not None
    assert health["local"]["consecutive_failures"] == 0
    assert health["local"]["count"] == 1


@pytest.mark.asyncio
async def test_loop_records_failure_and_keeps_running(
    source_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The `except` sits inside the `while`, so a failure must not kill the loop
    # — but it must no longer vanish into the log either (B3).
    _add_config_table(source_db)
    monkeypatch.setattr(db_backup.asyncio, "sleep", _noop_sleep)

    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db_backup, "_run_backup", _boom)

    await db_backup.db_backup_loop(_FakeBot(passes=2), source_db)

    health = db_backup.gather_backup_health(source_db, nas_conf_path=Path("/nope"))
    assert health["local"]["consecutive_failures"] == 2
    assert health["local"]["last_error"] == "OperationalError: disk I/O error"
    assert health["local"]["last_ok_at"] is None


@pytest.mark.asyncio
async def test_loop_treats_a_deliberate_skip_as_success(
    source_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A skip (B2's restart guard) means a fresh backup already exists. That is
    # the mechanism working, not failing, and must not raise a false alarm.
    _add_config_table(source_db)
    _make_backup(source_db.parent / "backups", "dungeonkeeper_20260811_034726.db")
    monkeypatch.setattr(db_backup.asyncio, "sleep", _noop_sleep)

    await db_backup.db_backup_loop(_FakeBot(), source_db)

    health = db_backup.gather_backup_health(source_db, nas_conf_path=Path("/nope"))
    assert health["local"]["count"] == 1  # nothing new written
    assert health["local"]["consecutive_failures"] == 0
    assert health["local"]["last_ok_at"] is not None
