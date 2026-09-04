#!/usr/bin/env python3
"""Apply the pending migrations to a throwaway copy of the live database.

Migrations run at boot. A migration that passes against a freshly-created test
schema and fails against production's actual columns takes the bot down at the
exact moment new code reaches members — and the failure is discovered by the
restart, which is the worst possible discoverer.

This snapshots the live database, applies whatever has not been applied yet to
the copy, and reports. The live file is never written to.

    python scripts/migration_dryrun.py [--db dungeonkeeper.db] [--keep]
                                      [--snapshot-dir DIR]

The snapshot uses sqlite3's backup API, never a filesystem copy: the live
database runs in WAL mode, so a `cp` of it is very often malformed and the
dry-run would fail for a reason that has nothing to do with the migrations.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "src" / "migrations"

#: Not /tmp. That is a 5.8 GiB tmpfs — RAM — which the test suite also uses,
#: and the production database is over a gigabyte. A dry-run running beside a
#: full pytest run could exhaust it, and the symptom is hundreds of bogus
#: sqlite errors that look like a test problem and are not. Disk, not memory.
DEFAULT_SNAPSHOT_DIR = Path.home() / ".cache" / "dk-dryrun"

#: Statements that discard data. Not a reason to stop — several are deliberate
#: — but a reason to take a backup before the restart rather than after.
DESTRUCTIVE = re.compile(
    r"\b(DROP\s+TABLE|DROP\s+COLUMN|DELETE\s+FROM|TRUNCATE)\b", re.I
)


def snapshot(live: Path, dest: Path) -> None:
    src = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _remove(db: Path) -> None:
    """Delete a snapshot and its write-ahead sidecars.

    ``unlink`` on the .db alone leaves ``-wal`` and ``-shm`` behind — the
    migrations run in WAL mode, so the sidecars are tens of megabytes and every
    dry-run leaked them into the cache directory.
    """
    for suffix in ("", "-wal", "-shm"):
        db.with_name(db.name + suffix).unlink(missing_ok=True)


def applied(db: Path) -> set[str]:
    """Migration filenames already recorded in the database.

    Deliberately not wrapped in a try/except. An earlier draft swallowed
    OperationalError and returned an empty set, which turned "I queried the
    wrong column" into "nothing has ever been applied" — the dry-run then
    cheerfully reported 221 pending migrations instead of 2. A check that
    fails silently into a plausible-looking answer is worse than no check.
    """
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {r[0] for r in conn.execute("SELECT migration FROM schema_version")}
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(ROOT / "dungeonkeeper.db"))
    ap.add_argument("--keep", action="store_true", help="leave the snapshot on disk")
    ap.add_argument(
        "--snapshot-dir", default=str(DEFAULT_SNAPSHOT_DIR),
        help=f"where to write the working copy (default {DEFAULT_SNAPSHOT_DIR})",
    )
    args = ap.parse_args()

    live = Path(args.db)
    if not live.exists():
        print(f"no such database: {live}", file=sys.stderr)
        return 2

    done = applied(live)
    pending = [p for p in sorted(MIGRATIONS.glob("*.sql")) if p.name not in done]
    if not pending:
        print("No pending migrations — a restart changes nothing in the schema.")
        return 0

    print(f"{len(pending)} migration(s) will run at the next restart:")
    risky = []
    for p in pending:
        sql = p.read_text(encoding="utf-8")
        hits = sorted({m.upper() for m in DESTRUCTIVE.findall(sql)})
        flag = f"   ⚠ {', '.join(hits)}" if hits else ""
        if hits:
            risky.append(p.name)
        print(f"  {p.name}{flag}")

    snap_dir = Path(args.snapshot_dir).expanduser()
    snap_dir.mkdir(parents=True, exist_ok=True)
    need = live.stat().st_size
    free = shutil.disk_usage(snap_dir).free
    if free < need * 2:
        print(
            f"\nNot enough room in {snap_dir}: the database is "
            f"{need / 2**30:.1f} GiB and only {free / 2**30:.1f} GiB is free. "
            "Pass --snapshot-dir somewhere with space.",
            file=sys.stderr,
        )
        return 2

    tmp = snap_dir / f"dk-dryrun-{int(time.time())}.db"
    print(f"\nSnapshotting {live} → {tmp}  ({need / 2**30:.1f} GiB)")
    snapshot(live, tmp)

    sys.path.insert(0, str(ROOT / "src"))
    from migrations import apply_migrations_sync  # noqa: E402

    print("Applying to the snapshot…")
    try:
        apply_migrations_sync(tmp)
    except Exception as exc:  # noqa: BLE001 - the whole point is to report it
        print(f"\nFAILED against the live schema: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        print(f"Snapshot kept for inspection (with its -wal/-shm): {tmp}", file=sys.stderr)
        return 1

    still = [p.name for p in pending if p.name not in applied(tmp)]
    if still:
        print(f"\nRan without error, but these are still unrecorded: {still}",
              file=sys.stderr)
        return 1

    print(f"\nAll {len(pending)} applied cleanly against the live schema.")
    if risky:
        print(f"\n⚠ Destructive statements in: {', '.join(risky)}")
        print("  Take a snapshot before restarting:")
        print(f"    sqlite3 {live} \".backup '/home/ben/backups/pre-restart-$(date +%F).db'\"")
    if args.keep:
        print(f"\nSnapshot kept: {tmp}")
    else:
        _remove(tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
