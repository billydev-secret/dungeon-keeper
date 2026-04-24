"""Backfill member_events from stored message history.

The join/leave log channel (leave_channel_id in config) uses Discord's
built-in system messages, where author_id IS the joining/leaving member.
Discord randomises the join message text, so we match by exclusion
(skip server boost notifications) rather than trying to match every
possible join phrase.

Leave events are identified by content ending with " left the server."

Both sources are idempotent — existing rows are ignored on conflict, so
this script is safe to re-run.

Usage:
    python -m scripts.backfill_member_events --guild-id 123456789
    python -m scripts.backfill_member_events --guild-id 123456789 --db path/to/other.db
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from db_utils import get_config_value, open_db  # noqa: E402
from services.message_store import init_member_events_table, record_member_event  # noqa: E402

DB_PATH = PROJECT_ROOT / "dungeonkeeper.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_member_events")

_BOOST_MARKERS = ("boosted the server", "just boosted")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--guild-id", required=True, type=int, help="Discord guild (server) ID.")
    p.add_argument("--db", default=str(DB_PATH), help="Path to the SQLite database.")
    return p.parse_args()


def backfill_from_system_log(conn, guild_id: int, log_channel_id: int) -> tuple[int, int]:
    rows = conn.execute(
        """
        SELECT author_id, ts, content FROM messages
        WHERE guild_id = ? AND channel_id = ? AND content IS NOT NULL AND content != ''
        ORDER BY ts
        """,
        (guild_id, log_channel_id),
    ).fetchall()

    joins = 0
    leaves = 0
    for row in rows:
        content = row["content"]
        ts = float(row["ts"])
        user_id = int(row["author_id"])

        if any(marker in content for marker in _BOOST_MARKERS):
            continue

        if "left the server" in content:
            record_member_event(conn, guild_id, user_id, "leave", ts)
            leaves += 1
        else:
            record_member_event(conn, guild_id, user_id, "join", ts)
            joins += 1

    return joins, leaves


def main() -> None:
    args = _parse_args()
    guild_id = args.guild_id

    with open_db(args.db) as conn:
        init_member_events_table(conn)

        log_channel_id = int(get_config_value(conn, "leave_channel_id", "0", guild_id))

        if log_channel_id <= 0:
            log.error("leave_channel_id not configured — cannot backfill.")
            return

        log.info("Backfilling from system log channel %d …", log_channel_id)
        joins, leaves = backfill_from_system_log(conn, guild_id, log_channel_id)

    log.info("Done. Joins inserted: %d  |  Leaves inserted: %d", joins, leaves)


if __name__ == "__main__":
    main()
