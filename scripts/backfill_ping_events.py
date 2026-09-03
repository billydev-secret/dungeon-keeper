"""Recover historical role pings by parsing stored message text.

This was a button on the Admin Backfill dashboard panel until that panel was
retired. The job itself is worth keeping: ``ping_events`` only records what the
bot observed live, so any stretch before the feature shipped — or any downtime
since — leaves the Ping Response report opening on an empty chart. The work is
pure SQL over the ``messages`` archive, so it needs no Discord connection and
runs fine against a stopped bot.

**It sees less than the live path does.** It can only find pings in channels
where message content was retained, and it cannot tell a real ``@everyone``
from someone typing the words without permission to send one. It exists so the
report opens with history rather than nothing, not as a substitute for capture.

Idempotent — ``record_ping_event`` is keyed on the message id, so re-running
adds nothing. Safe to run repeatedly.

Two things it deliberately does not do. It does not create ``ping_events``:
migrations own the schema, so run this against a migrated database. And it
cannot clear the dashboard's report cache, which is an in-process dict inside
the running web server — a separate process cannot reach it. That cache is
short-lived, so the Ping Response report picks the new rows up on its own
within minutes; restart the dashboard if you want them immediately.

Usage:
    python -m scripts.backfill_ping_events --guild-id 123456789
    python -m scripts.backfill_ping_events --all-guilds
    python -m scripts.backfill_ping_events --all-guilds --dry-run
    python -m scripts.backfill_ping_events --guild-id 123 --since 2026-08-01
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bot_modules.core.db_utils import open_db  # noqa: E402
from bot_modules.services.ping_tracker_service import (  # noqa: E402
    backfill_ping_events,
    known_bot_ids,
)

DB_PATH = PROJECT_ROOT / "dungeonkeeper.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_ping_events")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--guild-id", type=int, help="Discord guild (server) ID.")
    g.add_argument(
        "--all-guilds",
        action="store_true",
        help="Every guild that has stored messages. The bot serves more than one "
        "server, and a per-guild run silently covers only the one you named.",
    )
    p.add_argument("--db", default=str(DB_PATH), help="Path to the SQLite database.")
    p.add_argument(
        "--since",
        default=None,
        help="Only scan messages from this date onward (YYYY-MM-DD). Default: all history.",
    )
    p.add_argument(
        "--self-id",
        type=int,
        default=0,
        help="The bot's own user id, so its own pings are labelled as such rather "
        "than as an ordinary member's.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be scanned and recorded, then roll back.",
    )
    return p.parse_args()


def _since_ts(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        day = dt.datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        raise SystemExit(f"--since must be YYYY-MM-DD, got {value!r}")
    return day.timestamp()


def _guild_ids(conn, explicit: int | None) -> list[int]:
    if explicit is not None:
        return [explicit]
    return [
        int(r[0])
        for r in conn.execute(
            "SELECT DISTINCT guild_id FROM messages WHERE guild_id IS NOT NULL"
        ).fetchall()
    ]


def main() -> None:
    args = _parse_args()
    since = _since_ts(args.since)

    with open_db(args.db) as conn:
        guild_ids = _guild_ids(conn, args.guild_id)
        if not guild_ids:
            log.error("No guilds with stored messages found in %s", args.db)
            return

        total_scanned = 0
        total_recorded = 0
        for guild_id in guild_ids:
            stats = backfill_ping_events(
                conn,
                guild_id,
                since_ts=since,
                bot_ids=known_bot_ids(conn, guild_id),
                self_id=args.self_id,
            )
            total_scanned += stats["scanned"]
            total_recorded += stats["recorded"]
            log.info(
                "guild %d: scanned %d candidate message(s), recorded %d new ping(s)",
                guild_id,
                stats["scanned"],
                stats["recorded"],
            )

        if args.dry_run:
            # open_db commits on a clean exit, so a dry run has to undo its own
            # writes explicitly rather than just declining to commit.
            conn.rollback()
            log.info("--dry-run: rolled back, nothing was written.")

    log.info(
        "Done. Scanned %d candidate message(s) across %d guild(s), recorded %d new ping(s).",
        total_scanned,
        len(guild_ids),
        total_recorded,
    )


if __name__ == "__main__":
    main()
