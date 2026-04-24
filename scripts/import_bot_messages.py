"""Import bot messages from a JSONL backfill file into the database.

Embed fields are flattened into the content column so messages are searchable.

Usage:
    python -m scripts.import_bot_messages risky_roller_backfill.jsonl
    python -m scripts.import_bot_messages risky_roller_backfill.jsonl --db path/to/other.db
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from db_utils import open_db  # noqa: E402
from services.message_store import init_message_tables, store_message, upsert_known_user  # noqa: E402

DB_PATH = PROJECT_ROOT / "dungeonkeeper.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("import_bot_messages")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("jsonl", help="Path to the JSONL backfill file.")
    p.add_argument("--db", default=str(DB_PATH), help="Path to the SQLite database.")
    return p.parse_args()


def _embeds_to_text(embeds: list[dict]) -> str | None:
    """Flatten Discord embed dicts into a single searchable text string."""
    parts = []
    for e in embeds:
        if e.get("title"):
            parts.append(e["title"])
        if e.get("description"):
            parts.append(e["description"])
        if e.get("author"):
            parts.append(e["author"])
        if e.get("footer"):
            parts.append(e["footer"])
        for field in e.get("fields") or []:
            if field.get("name"):
                parts.append(field["name"])
            if field.get("value"):
                parts.append(field["value"])
    return "\n".join(parts) if parts else None


def _ts(iso: str | None) -> int | None:
    if not iso:
        return None
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def main() -> None:
    args = _parse_args()
    jsonl_path = Path(args.jsonl)
    db_path = Path(args.db)

    if not jsonl_path.exists():
        raise SystemExit(f"File not found: {jsonl_path}")

    inserted = 0
    skipped = 0

    # Ensure schema is up to date (creates message_embeds if missing, etc.)
    with open_db(db_path) as conn:
        init_message_tables(conn)

    with open_db(db_path) as conn:
        with jsonl_path.open(encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as e:
                    log.warning("Line %d: JSON parse error: %s", lineno, e)
                    skipped += 1
                    continue

                msg_id = rec.get("message_id")
                guild_id = rec.get("guild_id")
                channel_id = rec.get("channel_id")
                author_id = rec.get("author_id")
                created_at = rec.get("created_at")

                if not all([msg_id, guild_id, channel_id, author_id, created_at]):
                    log.warning("Line %d: missing required fields, skipping", lineno)
                    skipped += 1
                    continue

                ts = _ts(created_at)
                if ts is None:
                    log.warning("Line %d: could not parse created_at %r, skipping", lineno, created_at)
                    skipped += 1
                    continue

                # Upsert author into known_users so search by name works
                author_name = rec.get("author_name", "")
                upsert_known_user(conn, guild_id, author_id, author_name, author_name, float(ts))

                content = rec.get("content")
                if not content:
                    content = _embeds_to_text(rec.get("embeds") or [])
                reply_to_id = rec.get("referenced_message_id")
                attachments = rec.get("attachments") or []

                store_message(
                    conn,
                    message_id=msg_id,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    author_id=author_id,
                    content=content,
                    reply_to_id=reply_to_id,
                    ts=ts,
                    attachment_urls=attachments,
                    mention_ids=[],
                    embeds=rec.get("embeds") or [],
                )
                # UPDATE content for rows already inserted with NULL content
                if content:
                    conn.execute(
                        "UPDATE messages SET content = ? WHERE message_id = ? AND content IS NULL",
                        (content, msg_id),
                    )
                inserted += 1

        conn.commit()

    log.info("Done. Inserted %d messages, skipped %d.", inserted, skipped)


if __name__ == "__main__":
    main()
