#!/usr/bin/env python3
"""Compare a backup DB against the working DB and report any rows that exist
in the backup but are missing from the working DB.

Use this to verify that the message archive (and other historical tables)
hasn't lost data — e.g. after running /delete_me, after a migration, or as
a periodic integrity check.

Usage:
    python scripts/diff_db.py [BACKUP] [WORKING]

Defaults:
    BACKUP   = the most recent *.db file in backups/ matching the working
               DB's filename prefix (e.g. dungeonkeeper.db → dungeonkeeper_*.db)
    WORKING  = dungeonkeeper.db, or dk_dev.db if dungeonkeeper.db is absent
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# Tables whose row-level deletion is most concerning. The messages table and
# its child tables hold the user-archived content; XP / activity / known_users
# are also worth checking for unexpected loss.
KEY_TABLES = (
    "messages",
    "message_attachments",
    "message_mentions",
    "message_embeds",
    "message_reactions",
    "message_sentiment",
    "processed_messages",
    "member_xp",
    "member_activity",
    "xp_events",
    "role_events",
    "known_users",
    "member_events",
    "user_interactions",
    "user_interactions_log",
)

# Tables for which a full primary-key set-diff is cheap and useful. These
# have a single integer PK that's also the natural identity ("which message
# row went missing?"). Composite-PK tables are reported by row count only.
PK_DIFF_TABLES = {
    "messages": "message_id",
    "message_sentiment": "message_id",
}


def _list_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _missing_pks(
    backup: sqlite3.Connection,
    working: sqlite3.Connection,
    table: str,
    pk: str,
) -> list[int]:
    backup_pks = {r[0] for r in backup.execute(f"SELECT {pk} FROM {table}")}
    working_pks = {r[0] for r in working.execute(f"SELECT {pk} FROM {table}")}
    return sorted(backup_pks - working_pks)


def _latest_backup_for(working: Path) -> Path | None:
    backups_dir = Path("backups")
    if not backups_dir.is_dir():
        return None
    prefix = working.stem + "_"
    candidates = sorted(backups_dir.glob(f"{prefix}*.db"), reverse=True)
    return candidates[0] if candidates else None


def _resolve_working(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    for candidate in ("dungeonkeeper.db", "dk_dev.db"):
        p = Path(candidate)
        if p.is_file():
            return p
    return Path("dungeonkeeper.db")  # falls through to the not-found error


def main(argv: list[str]) -> int:
    backup_arg = argv[1] if len(argv) >= 2 else None
    working_arg = argv[2] if len(argv) >= 3 else None

    working_path = _resolve_working(working_arg)
    if not working_path.is_file():
        print(f"Working DB not found: {working_path}", file=sys.stderr)
        return 2

    if backup_arg:
        backup_path = Path(backup_arg)
    else:
        latest = _latest_backup_for(working_path)
        if latest is None:
            print(
                f"No backup found in backups/ matching prefix '{working_path.stem}_'. "
                f"Pass a path as the first argument.",
                file=sys.stderr,
            )
            return 2
        backup_path = latest

    if not backup_path.is_file():
        print(f"Backup not found: {backup_path}", file=sys.stderr)
        return 2

    print("Comparing:")
    print(f"  backup:  {backup_path}")
    print(f"  working: {working_path}")
    print()

    backup = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    working = sqlite3.connect(f"file:{working_path}?mode=ro", uri=True)
    try:
        backup_tables = _list_tables(backup)
        working_tables = _list_tables(working)

        print(f"{'table':<28} {'backup':>10} {'working':>10} {'delta':>10}")
        print("-" * 62)

        loss_tables: list[tuple[str, int]] = []

        for table in KEY_TABLES:
            if table not in backup_tables:
                print(f"{table:<28} {'(absent)':>10}")
                continue
            backup_n = _table_count(backup, table)
            working_n = _table_count(working, table) if table in working_tables else 0
            delta = working_n - backup_n
            flag = " *" if delta < 0 else ""
            print(f"{table:<28} {backup_n:>10} {working_n:>10} {delta:>+10}{flag}")
            if delta < 0:
                loss_tables.append((table, -delta))

        # Per-PK diff for message-archive tables (always run, even when the
        # row count went up — new messages can mask deletions).
        archive_losses: dict[str, list[int]] = {}
        for table, pk in PK_DIFF_TABLES.items():
            if table not in backup_tables or table not in working_tables:
                continue
            missing = _missing_pks(backup, working, table, pk)
            if missing:
                archive_losses[table] = missing

        print()
        if not loss_tables and not archive_losses:
            print("OK — no rows missing from the working DB.")
            return 0

        if loss_tables:
            total = sum(n for _, n in loss_tables)
            print(
                f"WARN: {total} row(s) missing across {len(loss_tables)} "
                f"table(s) by row count (marked with *)."
            )

        for table, missing in archive_losses.items():
            pk = PK_DIFF_TABLES[table]
            print(
                f"\n{table}: {len(missing)} {pk}(s) in backup but not in working DB:"
            )
            for value in missing[:25]:
                print(f"  {value}")
            if len(missing) > 25:
                print(f"  ... and {len(missing) - 25} more")

        return 1
    finally:
        backup.close()
        working.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
