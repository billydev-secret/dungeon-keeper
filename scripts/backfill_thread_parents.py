"""Record which known channels are threads, and which channel each hangs off.

The channel analytics attribute a thread's messages to the channel it was
started from (see bot_modules/services/channel_rollup). Going forward the
ingest records that as each message arrives, and migration 159 recovered the
threads whose starter message happened to be in the archive — roughly a fifth
of them. This fills in the rest by asking Discord, which is the only place the
answer still exists for a thread that has since been archived.

Read-only against Discord; the only writes are parent_id/is_thread on
known_channels. Safe to re-run — every write is an upsert, and a thread whose
parent is already recorded is left alone.

Usage:
    python -m scripts.backfill_thread_parents [--guild-id 111] [--dry-run]

Runs against the production database at the repo root by default. It logs in as
a second session on the same token, which Discord allows and which the live bot
does not notice.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import discord
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bot_modules.core.db_utils import open_db  # noqa: E402

DB_PATH = PROJECT_ROOT / "dungeonkeeper.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill-threads")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--guild-id", type=int, default=0, help="Guild id (defaults to GUILD_ID env)."
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be written without touching the database.",
    )
    p.add_argument("--db", default=str(DB_PATH), help="Database path.")
    return p.parse_args()


async def _collect(guild: discord.Guild) -> dict[int, int]:
    """Every thread the guild can still see, mapped to its parent channel.

    Three sources, because Discord exposes archived threads separately from
    live ones and private ones separately again:

      guild.threads              — active, already in the gateway cache, free
      channel.archived_threads() — public archived, paginated per channel
      ... private=True           — private archived, needs Manage Threads

    A thread Discord has actually deleted appears in none of them and stays
    unresolved; the resolver drops those rather than showing them as channels.
    """
    parents: dict[int, int] = {}

    for thread in guild.threads:
        parents[thread.id] = thread.parent_id
    log.info("%d active threads from the gateway cache", len(parents))

    me = guild.me

    async def _drain(iterator, channel: discord.abc.GuildChannel) -> None:
        try:
            async for thread in iterator:
                parents[thread.id] = thread.parent_id
        except discord.Forbidden:
            pass
        except discord.HTTPException as exc:
            log.warning("HTTP error listing %s (%s): %s", channel.name, channel.id, exc)

    for text in guild.text_channels:
        perms = text.permissions_for(me) if me is not None else None
        if perms is not None and not perms.read_message_history:
            continue
        await _drain(text.archived_threads(limit=None), text)
        # Private archived threads are a separate listing and need the perm.
        if perms is not None and perms.manage_threads:
            await _drain(text.archived_threads(limit=None, private=True), text)

    # Forum posts are threads too, and the forum's listing has no public/private
    # split — every post is reachable from the one iterator.
    for forum in guild.forums:
        perms = forum.permissions_for(me) if me is not None else None
        if perms is not None and not perms.read_message_history:
            continue
        await _drain(forum.archived_threads(limit=None), forum)

    log.info("%d threads total after walking archives", len(parents))
    return parents


def _write(
    db_path: Path, guild_id: int, parents: dict[int, int], dry_run: bool
) -> tuple[int, int]:
    """Upsert the map. Returns (rows written, threads not previously known)."""
    now = time.time()
    written = 0
    newly_flagged = 0
    with open_db(db_path) as conn:
        known = {
            int(r[0]): (r[1], r[2])
            for r in conn.execute(
                "SELECT channel_id, parent_id, is_thread FROM known_channels WHERE guild_id = ?",
                (guild_id,),
            )
        }
        for thread_id, parent_id in parents.items():
            if parent_id is None:
                continue
            current = known.get(thread_id)
            if current == (parent_id, 1):
                continue  # already recorded
            if current is None or not current[1]:
                newly_flagged += 1
            written += 1
            if dry_run:
                continue
            # No updated_at guard here, unlike the ingest path: this is the
            # authoritative answer from Discord itself, and it must win over
            # whatever a message-time upsert last wrote. The row may not exist
            # at all for a thread that predates the channel registry.
            conn.execute(
                """
                INSERT INTO known_channels
                    (guild_id, channel_id, channel_name, updated_at, parent_id, is_thread)
                VALUES (?, ?, '', ?, ?, 1)
                ON CONFLICT(guild_id, channel_id) DO UPDATE SET
                    parent_id = excluded.parent_id,
                    is_thread = 1
                """,
                (guild_id, thread_id, now, parent_id),
            )
        if not dry_run:
            conn.commit()
    return written, newly_flagged


async def _run(args: argparse.Namespace) -> None:
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN not set in environment.")

    guild_id = args.guild_id or int(os.getenv("GUILD_ID", "0") or 0)
    if not guild_id:
        raise SystemExit("guild id missing: pass --guild-id or set GUILD_ID env.")

    intents = discord.Intents.default()
    intents.guilds = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            guild = client.get_guild(guild_id)
            if guild is None:
                log.error("Guild %s not visible to this bot.", guild_id)
                return
            log.info("Connected as %s — walking %s (%s)", client.user, guild.name, guild.id)
            parents = await _collect(guild)
            written, newly = _write(Path(args.db), guild_id, parents, args.dry_run)
            verb = "would write" if args.dry_run else "wrote"
            log.info(
                "%s %d rows (%d threads not previously flagged as such)",
                verb, written, newly,
            )
        finally:
            await client.close()

    await client.start(token)


if __name__ == "__main__":
    asyncio.run(_run(_parse_args()))
