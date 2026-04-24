"""Scrape all historical messages authored by a specific bot (e.g. Risky Roller)
and dump them to JSONL for downstream use (question-bank building, etc.).

Usage:
    python -m scripts.backfill_bot_messages --bot-id 1234567890 \
        [--guild-id 111] [--channels 123,456|all] [--since 2024-01-01] \
        [--out risky_roller.jsonl] [--persist-override]

--persist-override inserts the bot id into the `recorded_bot_user_ids` bucket
in config_ids so ongoing messages are captured by the live bot going forward
(same effect as filling the field in the web UI's Global config panel).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import discord
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from db_utils import add_config_id, open_db  # noqa: E402

DB_PATH = PROJECT_ROOT / "dungeonkeeper.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("backfill")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bot-id", type=int, required=True, help="Target bot user id.")
    p.add_argument("--guild-id", type=int, default=0, help="Guild id (defaults to GUILD_ID env).")
    p.add_argument(
        "--channels",
        default="all",
        help="Comma-separated channel ids, or 'all' for every readable text channel + thread.",
    )
    p.add_argument(
        "--since",
        default=None,
        help="ISO date (YYYY-MM-DD) — only fetch messages at/after this date.",
    )
    p.add_argument("--out", default="risky_roller_backfill.jsonl", help="Output JSONL path.")
    p.add_argument(
        "--persist-override",
        action="store_true",
        help="Also insert the bot id into config_ids.recorded_bot_user_ids for live capture.",
    )
    return p.parse_args()


def _persist_override(guild_id: int, bot_id: int) -> None:
    with open_db(DB_PATH) as conn:
        add_config_id(conn, "recorded_bot_user_ids", bot_id, guild_id)
        conn.commit()
    log.info("Inserted %s into recorded_bot_user_ids for guild %s", bot_id, guild_id)


def _embed_to_dict(e: discord.Embed) -> dict:
    return {
        "title": e.title,
        "description": e.description,
        "url": e.url,
        "author": e.author.name if e.author else None,
        "footer": e.footer.text if e.footer else None,
        "fields": [{"name": f.name, "value": f.value, "inline": f.inline} for f in e.fields],
    }


def _message_to_record(msg: discord.Message) -> dict:
    return {
        "message_id": msg.id,
        "guild_id": msg.guild.id if msg.guild else None,
        "channel_id": msg.channel.id,
        "channel_name": getattr(msg.channel, "name", None),
        "author_id": msg.author.id,
        "author_name": str(msg.author),
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
        "content": msg.content or None,
        "embeds": [_embed_to_dict(e) for e in msg.embeds],
        "attachments": [a.url for a in msg.attachments],
        "referenced_message_id": msg.reference.message_id if msg.reference else None,
        "interaction_user_id": (
            _meta.user.id
            if (_meta := getattr(msg, "interaction_metadata", None)) is not None and _meta.user
            else None
        ),
    }


def _iter_targets(guild: discord.Guild, channel_filter: set[int] | None):
    me = guild.me
    for ch in guild.text_channels:
        if channel_filter and ch.id not in channel_filter:
            continue
        if ch.permissions_for(me).read_message_history:
            yield ch
    for thread in guild.threads:
        if channel_filter and thread.id not in channel_filter:
            continue
        parent_perms = thread.parent.permissions_for(me) if thread.parent else None
        if parent_perms and parent_perms.read_message_history:
            yield thread


async def _scrape(args: argparse.Namespace) -> None:
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN not set in environment.")

    guild_id = args.guild_id or int(os.getenv("GUILD_ID", "0") or 0)
    if not guild_id:
        raise SystemExit("guild id missing: pass --guild-id or set GUILD_ID env.")

    after = None
    if args.since:
        after = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)

    channel_filter: set[int] | None = None
    if args.channels and args.channels != "all":
        channel_filter = {int(x.strip()) for x in args.channels.split(",") if x.strip()}

    if args.persist_override:
        _persist_override(guild_id, args.bot_id)

    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    client = discord.Client(intents=intents)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    total_written = 0

    @client.event
    async def on_ready():
        nonlocal total_written
        try:
            guild = client.get_guild(guild_id) or await client.fetch_guild(guild_id)
            if guild is None:
                log.error("Guild %s not visible to this bot.", guild_id)
                return
            log.info("Connected as %s — scanning guild %s (%s)", client.user, guild.id, guild.name)

            with out_path.open("w", encoding="utf-8") as out:
                for ch in _iter_targets(guild, channel_filter):
                    n = 0
                    try:
                        async for msg in ch.history(limit=None, after=after, oldest_first=True):
                            if msg.author.id != args.bot_id:
                                continue
                            out.write(json.dumps(_message_to_record(msg), ensure_ascii=False) + "\n")
                            n += 1
                    except discord.Forbidden:
                        log.warning("Forbidden: %s (%s)", getattr(ch, "name", "?"), ch.id)
                        continue
                    except discord.HTTPException as e:
                        log.warning("HTTP error in %s: %s", getattr(ch, "name", "?"), e)
                        continue
                    if n:
                        log.info("%s (%s): %d msgs", getattr(ch, "name", "?"), ch.id, n)
                        total_written += n
        finally:
            await client.close()

    await client.start(token)
    log.info("Done. Wrote %d messages to %s", total_written, out_path)


if __name__ == "__main__":
    asyncio.run(_scrape(_parse_args()))
