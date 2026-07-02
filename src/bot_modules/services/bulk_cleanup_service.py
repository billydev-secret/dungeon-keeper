"""Server-wide bulk cleanup of old messages.

Deletes messages older than a configured age (default 30 days) across every
text channel and thread in a guild, with a per-guild channel-exception list.

Because Discord's bulk-delete API rejects anything older than 14 days, every
message this feature targets (age > 30d by default) is removed via individual
deletes throttled to ~1/s per channel. A first sweep on a server with years of
history can therefore take hours; it is safe to interrupt (a restart simply
re-scans, and already-deleted messages are gone) and resumes on the next cycle.

Config (per guild, in the ``config`` KV table — read with legacy fallback OFF so
an unconfigured guild is never armed by the home guild's settings):
  bulk_cleanup_enabled   "0"/"1"   off by default
  bulk_cleanup_age_days   int       default 30
  bulk_cleanup_last_run   float     unix ts of last COMPLETED sweep
Channel exceptions live in the ``config_ids`` bucket ``EXCLUDED_BUCKET``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from pathlib import Path

import discord

from bot_modules.core.db_utils import (
    get_config_id_set,
    get_config_value,
    open_db,
    set_config_value,
)
from bot_modules.core.settings import AUTO_DELETE_SETTINGS
from bot_modules.core.utils import format_guild_for_log

# Reuse the auto-delete history scanner: it walks channel.history() oldest-first,
# skips pinned messages, and deletes everything older than the cutoff (bulk for
# <13d, individual otherwise) with the standard rate-limit pauses. Called without
# db_path/guild_id it is a pure scan-and-delete and does not touch the
# auto_delete_messages tracking queue.
from bot_modules.services.auto_delete_service import _scan_and_delete_channel_history

log = logging.getLogger("dungeonkeeper.bulk_cleanup")

EXCLUDED_BUCKET = "bulk_cleanup_excluded_channels"
_ENABLED_KEY = "bulk_cleanup_enabled"
_AGE_DAYS_KEY = "bulk_cleanup_age_days"
_LAST_RUN_KEY = "bulk_cleanup_last_run"

DEFAULT_AGE_DAYS = 30
MIN_AGE_DAYS = 1

# How often the loop checks which guilds are due (seconds).
POLL_SECONDS = 3600
# Minimum time between COMPLETED sweeps per guild (seconds).
RUN_INTERVAL_SECONDS = 86_400


# ── Config reads (legacy fallback OFF — see module docstring) ──────────────────


def _is_enabled(conn, guild_id: int) -> bool:
    return (
        get_config_value(conn, _ENABLED_KEY, "0", guild_id, allow_legacy_fallback=False)
        == "1"
    )


def _age_days(conn, guild_id: int) -> int:
    raw = get_config_value(
        conn, _AGE_DAYS_KEY, str(DEFAULT_AGE_DAYS), guild_id, allow_legacy_fallback=False
    )
    try:
        return max(MIN_AGE_DAYS, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_AGE_DAYS


def _last_run(conn, guild_id: int) -> float:
    raw = get_config_value(
        conn, _LAST_RUN_KEY, "0", guild_id, allow_legacy_fallback=False
    )
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _excluded_ids(conn, guild_id: int) -> set[int]:
    return get_config_id_set(conn, EXCLUDED_BUCKET, guild_id, allow_legacy_fallback=False)


# ── Sweep ──────────────────────────────────────────────────────────────────────


async def _collect_targets(
    guild: discord.Guild, excluded: set[int]
) -> list[discord.TextChannel | discord.Thread]:
    """Gather every sweepable channel/thread in the guild, minus exclusions.

    Covers top-level text channels (which hold messages directly) plus the active
    and public-archived threads of text and forum channels. A channel listed in
    ``excluded`` is skipped along with all of its threads. Channels the bot can't
    read history in or can't manage messages in are skipped cleanly.
    """
    me = guild.me
    if me is None:
        return []

    def _usable(ch: discord.abc.GuildChannel | discord.Thread) -> bool:
        perms = ch.permissions_for(me)
        return perms.read_message_history and perms.manage_messages

    targets: list[discord.TextChannel | discord.Thread] = []
    parents: list[discord.TextChannel | discord.ForumChannel] = [
        *guild.text_channels,
        *guild.forums,
    ]
    for parent in parents:
        if parent.id in excluded:
            continue  # excluding a channel excludes its threads too

        # Text channels carry messages directly; forums only via their threads.
        if isinstance(parent, discord.TextChannel) and _usable(parent):
            targets.append(parent)

        for th in parent.threads:  # cached active threads / forum posts
            if th.id not in excluded and _usable(th):
                targets.append(th)

        try:
            async for th in parent.archived_threads(limit=None):
                if th.id not in excluded and _usable(th):
                    targets.append(th)
        except (discord.Forbidden, discord.HTTPException):
            log.debug(
                "bulk-cleanup: cannot list archived threads in #%s",
                getattr(parent, "name", parent.id),
            )

    return targets


async def run_bulk_cleanup_for_guild(
    bot: discord.Client, db_path: Path, guild: discord.Guild
) -> None:
    """Sweep one guild. Sets ``last_run`` only on completion (so cadence is
    24h-after-finish, and an interrupted sweep re-runs next cycle)."""
    with open_db(db_path) as conn:
        if not _is_enabled(conn, guild.id):
            return
        age_days = _age_days(conn, guild.id)
        excluded = _excluded_ids(conn, guild.id)

    cutoff = discord.utils.utcnow() - timedelta(days=age_days)
    reason = f"Bulk cleanup: messages older than {age_days}d"
    targets = await _collect_targets(guild, excluded)

    log.info(
        "bulk-cleanup %s: starting sweep of %d channels/threads (age > %dd)",
        format_guild_for_log(guild, guild.id),
        len(targets),
        age_days,
    )

    sem = asyncio.Semaphore(AUTO_DELETE_SETTINGS.startup_concurrency)
    total_deleted = 0
    total_failed = 0

    async def _one(ch: discord.TextChannel | discord.Thread) -> None:
        nonlocal total_deleted, total_failed
        async with sem:
            try:
                deleted, failed = await _scan_and_delete_channel_history(
                    ch, cutoff, reason=reason
                )
                total_deleted += deleted
                total_failed += failed
                if deleted or failed:
                    log.info(
                        "bulk-cleanup %s: #%s deleted=%d failed=%d",
                        guild.name,
                        getattr(ch, "name", ch.id),
                        deleted,
                        failed,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "bulk-cleanup %s: error sweeping #%s",
                    guild.name,
                    getattr(ch, "name", getattr(ch, "id", "?")),
                )

    await asyncio.gather(*(_one(ch) for ch in targets))

    with open_db(db_path) as conn:
        set_config_value(conn, _LAST_RUN_KEY, str(time.time()), guild.id)

    log.info(
        "bulk-cleanup %s: sweep complete, deleted=%d failed=%d",
        guild.name,
        total_deleted,
        total_failed,
    )


async def _run_due_guilds(bot: discord.Client, db_path: Path) -> None:
    now = time.time()
    for guild in list(bot.guilds):
        with open_db(db_path) as conn:
            if not _is_enabled(conn, guild.id):
                continue
            if now - _last_run(conn, guild.id) < RUN_INTERVAL_SECONDS:
                continue
        await run_bulk_cleanup_for_guild(bot, db_path, guild)


async def bulk_cleanup_loop(bot: discord.Client, db_path: Path) -> None:
    """Background task: periodically sweep guilds whose cleanup is enabled and due."""
    await bot.wait_until_ready()

    while not bot.is_closed():
        try:
            await _run_due_guilds(bot, db_path)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("bulk-cleanup loop iteration failed.")

        await asyncio.sleep(POLL_SECONDS)
