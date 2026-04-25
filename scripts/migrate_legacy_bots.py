"""
Migrate data from openConfess (confess.sql) and dm_perms_bot (accord.db)
into dungeonkeeper.db.

Usage:
    python scripts/migrate_legacy_bots.py [--dry-run] [--confess PATH] [--accord PATH] [--dest PATH]

Defaults:
    --confess  ../openConfess/confess.sql
    --accord   ../dm_perms_bot/accord.db
    --dest     dungeonkeeper.db
"""

from __future__ import annotations

import argparse
import datetime
import sqlite3
import sys
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(text: Optional[str]) -> Optional[float]:
    """Parse an ISO timestamp string to a POSIX float, or return None."""
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.datetime.strptime(text, fmt).replace(tzinfo=datetime.timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _or_none(value: int, sentinel: int = 0) -> Optional[int]:
    """Return None if value equals sentinel, otherwise return value."""
    return None if value == sentinel else value


# ---------------------------------------------------------------------------
# openConfess migration
# ---------------------------------------------------------------------------

def migrate_confessions(src_path: Path, dst: sqlite3.Connection, dry_run: bool) -> dict[str, int]:
    counts: dict[str, int] = {}

    if not src_path.exists():
        print(f"  [confess] Source not found: {src_path} — skipping.")
        return counts

    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row

    dst_cur = dst.cursor()

    # ── guild_config → confession_config ─────────────────────────────────────
    rows = src.execute("SELECT * FROM guild_config").fetchall()
    guild_dest_channel: dict[int, int] = {}
    n = 0
    for r in rows:
        guild_dest_channel[int(r["guild_id"])] = int(r["dest_channel_id"] or 0)
        params = (
            r["guild_id"],
            r["dest_channel_id"],
            r["log_channel_id"],
            r["cooldown_seconds"],
            r["max_chars"],
            r["panic"],
            r["replies_enabled"],
            r["notify_op_on_reply"],
            r["per_day_limit"],
            r["blocked_user_ids"],
            r["launcher_channel_id"],
            r["launcher_message_id"],
        )
        if not dry_run:
            dst_cur.execute(
                """INSERT OR IGNORE INTO confession_config
                   (guild_id, dest_channel_id, log_channel_id, cooldown_seconds, max_chars,
                    panic, replies_enabled, notify_op_on_reply, per_day_limit, blocked_user_ids,
                    launcher_channel_id, launcher_message_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                params,
            )
            n += dst_cur.rowcount
        else:
            n += 1
    counts["confession_config"] = n

    # ── rate_limits → confession_rate_limits ──────────────────────────────────
    rows = src.execute("SELECT * FROM rate_limits").fetchall()
    n = 0
    for r in rows:
        params = (
            r["guild_id"],
            r["author_id"],
            int(r["last_confess_at"] or 0),
            int(r["last_reply_at"] or 0),
            r["day_key"] or "",
            r["day_count"] or 0,
        )
        if not dry_run:
            dst_cur.execute(
                """INSERT OR IGNORE INTO confession_rate_limits
                   (guild_id, author_id, last_confess_at, last_reply_at, day_key, day_count)
                   VALUES (?,?,?,?,?,?)""",
                params,
            )
            n += dst_cur.rowcount
        else:
            n += 1
    counts["confession_rate_limits"] = n

    # ── thread_posts → confession_threads ─────────────────────────────────────
    # New schema requires channel_id (NOT NULL). The old schema didn't track it,
    # so we fall back to the guild's dest_channel_id — where confessions are posted.
    rows = src.execute("SELECT * FROM thread_posts").fetchall()
    n = 0
    for r in rows:
        params = (
            r["guild_id"],
            r["message_id"],
            guild_dest_channel.get(int(r["guild_id"]), 0),
            r["root_message_id"],
            r["original_author_id"],
            -1,   # notify_original_author — unknown, use "unset" sentinel
            0,    # discord_thread_id — not tracked in old schema
            0,    # reply_button_message_id
            int(float(r["created_at"] or 0)),
        )
        if not dry_run:
            dst_cur.execute(
                """INSERT OR IGNORE INTO confession_threads
                   (guild_id, message_id, channel_id, root_message_id,
                    original_author_id, notify_original_author,
                    discord_thread_id, reply_button_message_id, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                params,
            )
            n += dst_cur.rowcount
        else:
            n += 1
    counts["confession_threads"] = n

    src.close()
    if not dry_run:
        dst.commit()
    return counts


# ---------------------------------------------------------------------------
# dm_perms_bot migration
# ---------------------------------------------------------------------------

def migrate_dm_perms(src_path: Path, dst: sqlite3.Connection, dry_run: bool) -> dict[str, int]:
    counts: dict[str, int] = {}

    if not src_path.exists():
        print(f"  [accord] Source not found: {src_path} — skipping.")
        return counts

    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row

    dst_cur = dst.cursor()

    # ── consent_pairs → dm_consent_pairs ──────────────────────────────────────
    # Both schemas use (user_low, user_high) as the deduped pair.
    rows = src.execute("SELECT * FROM consent_pairs").fetchall()
    n = 0
    for r in rows:
        params = (
            r["guild_id"],
            r["user_low"],
            r["user_high"],
            "dm",   # rel_type unknown — default to dm
            "",     # reason (NOT NULL DEFAULT '')
            0.0,    # created_at (NOT NULL DEFAULT 0)
            None,   # source_msg_id
            None,   # source_channel_id
        )
        if not dry_run:
            dst_cur.execute(
                """INSERT OR IGNORE INTO dm_consent_pairs
                   (guild_id, user_low, user_high, rel_type, reason,
                    created_at, source_msg_id, source_channel_id)
                   VALUES (?,?,?,?,?,?,?,?)""",
                params,
            )
            n += dst_cur.rowcount
        else:
            n += 1
    counts["dm_consent_pairs"] = n

    # ── request_channels → dm_request_channels ────────────────────────────────
    rows = src.execute("SELECT * FROM request_channels").fetchall()
    n = 0
    for r in rows:
        if not dry_run:
            dst_cur.execute(
                "INSERT OR IGNORE INTO dm_request_channels (guild_id, channel_id) VALUES (?,?)",
                (r["guild_id"], r["channel_id"]),
            )
            n += dst_cur.rowcount
        else:
            n += 1
    counts["dm_request_channels"] = n

    # ── audit_channels → dm_audit_channels ────────────────────────────────────
    rows = src.execute("SELECT * FROM audit_channels").fetchall()
    n = 0
    for r in rows:
        if not dry_run:
            dst_cur.execute(
                "INSERT OR IGNORE INTO dm_audit_channels (guild_id, channel_id) VALUES (?,?)",
                (r["guild_id"], r["channel_id"]),
            )
            n += dst_cur.rowcount
        else:
            n += 1
    counts["dm_audit_channels"] = n

    # ── dm_panel_settings → dm_panel_settings ─────────────────────────────────
    # Old schema has an extra target_channel_id (their request channel).
    # Migrate it to dm_request_channels if it isn't already there.
    rows = src.execute("SELECT * FROM dm_panel_settings").fetchall()
    n = 0
    for r in rows:
        if not dry_run:
            dst_cur.execute(
                """INSERT OR IGNORE INTO dm_panel_settings
                   (guild_id, panel_channel_id, panel_message_id)
                   VALUES (?,?,?)""",
                (r["guild_id"], r["panel_channel_id"], r["panel_message_id"]),
            )
            n += dst_cur.rowcount
            # promote target_channel_id → dm_request_channels if set
            if r["target_channel_id"]:
                dst_cur.execute(
                    "INSERT OR IGNORE INTO dm_request_channels (guild_id, channel_id) VALUES (?,?)",
                    (r["guild_id"], r["target_channel_id"]),
                )
        else:
            n += 1
    counts["dm_panel_settings"] = n

    # ── audit_log → dm_audit_log ──────────────────────────────────────────────
    rows = src.execute("SELECT * FROM audit_log").fetchall()
    n = 0
    for r in rows:
        ts = _ts(r["timestamp"])
        req_type = r["request_type"] or ""
        notes = r["message"] or ""
        if req_type:
            notes = f"[type={req_type}] {notes}" if notes else f"[type={req_type}]"
        params = (
            r["guild_id"],
            r["actor_id"],
            r["user1_id"],
            r["user2_id"],
            r["action"] or "",   # NOT NULL
            ts if ts is not None else 0.0,   # NOT NULL
            notes or None,
        )
        if not dry_run:
            dst_cur.execute(
                """INSERT INTO dm_audit_log
                   (guild_id, actor_id, user_a_id, user_b_id, action, timestamp, notes)
                   VALUES (?,?,?,?,?,?,?)""",
                params,
            )
            n += dst_cur.rowcount
        else:
            n += 1
    counts["dm_audit_log"] = n

    src.close()
    if not dry_run:
        dst.commit()
    return counts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate legacy bot data into dungeonkeeper.db")
    parser.add_argument("--confess", default="../openConfess/confess.sql",
                        help="Path to openConfess SQLite DB (default: ../openConfess/confess.sql)")
    parser.add_argument("--accord", default="../dm_perms_bot/accord.db",
                        help="Path to dm_perms_bot SQLite DB (default: ../dm_perms_bot/accord.db)")
    parser.add_argument("--dest", default="dungeonkeeper.db",
                        help="Path to dungeonkeeper.db (default: dungeonkeeper.db)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Count rows that would be migrated without writing anything")
    args = parser.parse_args()

    confess_path = Path(args.confess)
    accord_path = Path(args.accord)
    dest_path = Path(args.dest)

    if not dest_path.exists():
        print(f"ERROR: destination DB not found: {dest_path}", file=sys.stderr)
        sys.exit(1)

    mode = "DRY RUN — no data written" if args.dry_run else "LIVE — writing to DB"
    print(f"\n=== migrate_legacy_bots ({mode}) ===\n")

    dst = sqlite3.connect(dest_path)

    print("[ openConfess ]")
    c_counts = migrate_confessions(confess_path, dst, args.dry_run)
    for table, n in c_counts.items():
        verb = "would insert" if args.dry_run else "inserted"
        print(f"  {table}: {verb} {n} row(s)")

    print()
    print("[ dm_perms_bot ]")
    d_counts = migrate_dm_perms(accord_path, dst, args.dry_run)
    for table, n in d_counts.items():
        verb = "would insert" if args.dry_run else "inserted"
        print(f"  {table}: {verb} {n} row(s)")

    dst.close()
    print()
    if args.dry_run:
        print("Dry run complete. Run without --dry-run to apply.")
    else:
        print("Migration complete.")


if __name__ == "__main__":
    main()
