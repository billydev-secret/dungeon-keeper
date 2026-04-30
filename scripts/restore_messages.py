#!/usr/bin/env python3
"""Restore message rows that were deleted from the working DB by copying
them back from one or more backups.

Use this once after stopping the bot, to recover messages lost to the old
on_raw_message_delete listener (now removed). Idempotent — safe to re-run.

By default, walks every backup in backups/ matching the working DB's
filename prefix (oldest -> newest) and applies them in sequence. INSERT
OR IGNORE handles duplicates, so each backup contributes any messages
that were lost in its own time window. Pass a single backup path
explicitly to restore from just that one.

The script restores the ``messages`` table plus its per-message child
tables (attachments, mentions, embeds, reactions, sentiment). It does not
touch other historical tables (XP, activity, role events, etc.); those
have different ownership semantics and may have been intentionally cleared
by /delete_me.

Usage:
    python scripts/restore_messages.py [BACKUP] [WORKING] [--dry-run]

Defaults match scripts/diff_db.py:
    BACKUP  = every *.db file in backups/ matching the working DB's
              filename prefix (e.g. dungeonkeeper.db -> dungeonkeeper_*.db),
              applied oldest first. Pass an explicit path to use just one.
    WORKING = dungeonkeeper.db, or dk_dev.db if dungeonkeeper.db is absent

The bot MUST be stopped before running — the working DB is opened in
write mode and SQLite will fail noisily on a locked file.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# Tables whose rows are tied to a parent message_id and should be restored
# alongside the message row itself.
CHILD_TABLES = (
    "message_attachments",
    "message_mentions",
    "message_embeds",
    "message_reactions",
    "message_sentiment",
)

BATCH = 500


def _list_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


def _common_columns(
    backup: sqlite3.Connection, working: sqlite3.Connection, table: str
) -> list[str]:
    backup_cols = [r[1] for r in backup.execute(f"PRAGMA table_info({table})")]
    working_cols = [r[1] for r in working.execute(f"PRAGMA table_info({table})")]
    working_set = set(working_cols)
    return [c for c in backup_cols if c in working_set]


def _missing_message_ids(
    backup: sqlite3.Connection, working: sqlite3.Connection
) -> list[int]:
    backup_ids = {r[0] for r in backup.execute("SELECT message_id FROM messages")}
    working_ids = {r[0] for r in working.execute("SELECT message_id FROM messages")}
    return sorted(backup_ids - working_ids)


def _restore_table(
    backup: sqlite3.Connection,
    working: sqlite3.Connection,
    table: str,
    message_ids: list[int],
    *,
    dry_run: bool,
) -> int:
    """Copy rows whose message_id is in *message_ids* from backup to working.
    Returns the number of rows actually inserted (excludes duplicates)."""
    cols = _common_columns(backup, working, table)
    if not cols or "message_id" not in cols:
        return 0

    inserted = 0
    placeholders = ",".join("?" * len(cols))
    col_list = ",".join(cols)

    for i in range(0, len(message_ids), BATCH):
        batch = message_ids[i : i + BATCH]
        ph = ",".join("?" * len(batch))
        rows = backup.execute(
            f"SELECT {col_list} FROM {table} WHERE message_id IN ({ph})",
            batch,
        ).fetchall()
        if not rows:
            continue
        if dry_run:
            inserted += len(rows)
            continue
        cursor = working.cursor()
        cursor.executemany(
            f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({placeholders})",
            rows,
        )
        # rowcount on executemany reflects affected rows for INSERT OR IGNORE
        inserted += cursor.rowcount if cursor.rowcount >= 0 else len(rows)

    return inserted


def _backups_for(working: Path) -> list[Path]:
    """All backups matching *working*'s filename prefix, oldest → newest."""
    backups_dir = working.parent / "backups"
    if not backups_dir.is_dir():
        backups_dir = Path("backups")
    if not backups_dir.is_dir():
        return []
    prefix = working.stem + "_"
    return sorted(backups_dir.glob(f"{prefix}*.db"))


def _resolve_working(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    for candidate in ("dungeonkeeper.db", "dk_dev.db"):
        p = Path(candidate)
        if p.is_file():
            return p
    return Path("dungeonkeeper.db")


def _restore_from_one(
    backup_path: Path, working: sqlite3.Connection, *, dry_run: bool
) -> dict[str, int]:
    """Apply restorations from one backup. Returns per-table insert counts."""
    backup = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    try:
        backup_tables = _list_tables(backup)
        working_tables = _list_tables(working)

        if "messages" not in backup_tables or "messages" not in working_tables:
            return {}

        missing = _missing_message_ids(backup, working)
        counts: dict[str, int] = {}
        if not missing:
            return counts

        counts["messages"] = _restore_table(
            backup, working, "messages", missing, dry_run=dry_run
        )
        for table in CHILD_TABLES:
            if table not in backup_tables or table not in working_tables:
                continue
            counts[table] = _restore_table(
                backup, working, table, missing, dry_run=dry_run
            )
        if not dry_run:
            working.commit()
        return counts
    finally:
        backup.close()


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("-")]
    dry_run = "--dry-run" in argv or "-n" in argv

    backup_arg = args[0] if len(args) >= 1 else None
    working_arg = args[1] if len(args) >= 2 else None

    working_path = _resolve_working(working_arg)
    if not working_path.is_file():
        print(f"Working DB not found: {working_path}", file=sys.stderr)
        return 2

    if backup_arg:
        backup_path = Path(backup_arg)
        if not backup_path.is_file():
            print(f"Backup not found: {backup_path}", file=sys.stderr)
            return 2
        backups = [backup_path]
    else:
        backups = _backups_for(working_path)
        if not backups:
            print(
                f"No backups found in backups/ matching prefix '{working_path.stem}_'. "
                f"Pass a path as the first argument.",
                file=sys.stderr,
            )
            return 2

    print(f"Working DB: {working_path}")
    print(f"Backups:    {len(backups)} file(s), oldest -> newest")
    for b in backups:
        print(f"            {b.name}")
    print(f"Mode:       {'dry run (no writes)' if dry_run else 'writing'}")
    print()

    # Working DB opened read-write. Will raise OperationalError if the bot
    # is still running and holds a lock — that's the intended safety check.
    working = sqlite3.connect(working_path)

    grand_total: dict[str, int] = {}

    try:
        for backup_path in backups:
            counts = _restore_from_one(backup_path, working, dry_run=dry_run)
            if not counts or counts.get("messages", 0) == 0:
                print(f"{backup_path.name}: nothing new to restore")
                continue
            details = ", ".join(f"{t}={n}" for t, n in counts.items() if n > 0)
            print(f"{backup_path.name}: {details}")
            for table, n in counts.items():
                grand_total[table] = grand_total.get(table, 0) + n

        print()
        if not grand_total or grand_total.get("messages", 0) == 0:
            print("Working DB already has every message from every backup.")
            return 0

        total_msgs = grand_total.get("messages", 0)
        all_counts = ", ".join(
            f"{t}={n}" for t, n in grand_total.items() if n > 0
        )
        verb = "would restore" if dry_run else "restored"
        print(f"Total {verb}: {all_counts}")
        print(f"  ({total_msgs} message row(s) across {len(backups)} backup(s))")
        return 0
    finally:
        working.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
