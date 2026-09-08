"""Erase a guild's data once the bot is removed from it.

Thin, like ``retention_cog``: every decision — which tables, which order, what
is bot-global and must survive — lives in
``bot_modules.services.guild_purge_service``. This file only decides *when*,
and there are three whens:

* **On removal.** ``on_guild_remove`` queues the guild and snapshots its
  channel list, which is the only moment that list exists.
* **On the daily sweep.** Queued guilds past their deadline are purged. The
  sweep also reconciles: a guild the bot lost while it was offline never fired
  a removal event, so it is found by comparing the database against the
  connected guild list — and only ever *marked*, never purged on the spot, so
  a short guild list from a Discord outage heals itself.
* **On rejoin.** ``on_guild_join`` cancels a pending purge. An accidental kick
  followed by a re-invite inside the grace window costs nothing.

Nothing here does anything until an operator sets the grace period on the
dashboard's privacy panel. Unset means never purge, which is what every
deployment starts as.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import discord

    from bot_modules.core.app_context import Bot

from discord.ext import commands, tasks

from bot_modules.services import guild_purge_service as purge

log = logging.getLogger(__name__)


class GuildPurgeCog(commands.Cog):
    def __init__(self, bot: "Bot") -> None:
        self.bot = bot
        self._purge_loop.start()

    async def cog_unload(self) -> None:
        self._purge_loop.cancel()

    # ------------------------------------------------------------ events

    @commands.Cog.listener("on_guild_remove")
    async def _on_guild_remove(self, guild: "discord.Guild") -> None:
        """Queue the departing guild, snapshotting what only exists right now.

        Discord routes an outage's GUILD_DELETE to ``on_guild_unavailable``
        instead, so reaching here means the bot really is out. The channel ids
        are read synchronously off the object we were handed, before anything
        awaits — the cache is gone by the time a thread gets a look at it.
        """
        channel_ids = tuple(c.id for c in guild.channels)
        guild_id = guild.id

        def _mark() -> float | None:
            with self.bot.ctx.open_db() as conn:
                delay = purge.purge_delay_days(conn)
                if delay is None:
                    return None
                return purge.mark_departed(
                    conn, guild_id, delay_days=delay, channel_ids=channel_ids
                )

        try:
            deadline = await asyncio.to_thread(_mark)
        except Exception:
            log.exception("Guild purge: failed to queue guild %s", guild_id)
            return

        if deadline is None:
            log.info(
                "Guild purge: left guild %s, but no purge delay is configured "
                "— its data is retained",
                guild_id,
            )
            return
        log.info("Guild purge: queued guild %s for erasure at %.0f", guild_id, deadline)
        await self._run_due()

    @commands.Cog.listener("on_guild_join")
    async def _on_guild_join(self, guild: "discord.Guild") -> None:
        """Cancel a pending purge — the bot is back before the deadline."""
        guild_id = guild.id

        def _clear() -> bool:
            with self.bot.ctx.open_db() as conn:
                return purge.clear_departed(conn, guild_id)

        try:
            if await asyncio.to_thread(_clear):
                log.info("Guild purge: guild %s rejoined, erasure cancelled", guild_id)
        except Exception:
            log.exception("Guild purge: failed to cancel erasure for %s", guild_id)

    # ------------------------------------------------------------- sweep

    @tasks.loop(hours=24)
    async def _purge_loop(self) -> None:
        await self._reconcile()
        await self._run_due()

    async def _reconcile(self) -> None:
        """Queue guilds that vanished while the bot was not watching.

        Refuses to run on an empty guild list. ``bot.guilds`` empty means the
        bot has not finished receiving guilds, not that it was removed from all
        of them, and acting on that reading would queue every server at once.
        """
        present = {g.id for g in self.bot.guilds}
        if not present:
            log.warning("Guild purge: no guilds connected, skipping reconciliation")
            return

        def _work() -> tuple[list[int], list[int]]:
            with self.bot.ctx.open_db() as conn:
                return purge.reconcile_presence(conn, present)

        try:
            marked, rejoined = await asyncio.to_thread(_work)
        except Exception:
            log.exception("Guild purge: reconciliation failed")
            return
        if rejoined:
            log.info("Guild purge: erasure cancelled for rejoined guilds %s", rejoined)
        if marked:
            log.info("Guild purge: queued absent guilds %s for erasure", marked)

    async def _run_due(self) -> None:
        """Purge every queued guild whose grace period has expired.

        One connection, and so one transaction, per guild: a guild that fails
        rolls back alone and stays queued for tomorrow instead of taking its
        predecessors' erasures down with it.
        """

        def _work() -> dict[int, dict[str, int]]:
            with self.bot.ctx.open_db() as conn:
                due = purge.due_guilds(conn)
            done: dict[int, dict[str, int]] = {}
            for guild_id in due:
                try:
                    with self.bot.ctx.open_db() as conn:
                        done[guild_id] = purge.run_departed_purge(conn, guild_id)
                except Exception:
                    log.exception("Guild purge failed for guild %s", guild_id)
            return done

        try:
            done = await asyncio.to_thread(_work)
        except Exception:
            log.exception("Guild purge sweep failed")
            return

        if not done:
            return
        for guild_id, counts in done.items():
            log.info(
                "Guild purge: erased guild %s — %d rows across %d tables (%s)",
                guild_id,
                sum(counts.values()),
                len(counts),
                ", ".join(f"{k}={v}" for k, v in sorted(counts.items())),
            )

        # A purge of a busy guild frees hundreds of thousands of pages to the
        # freelist, and in WAL mode they sit in the -wal until a checkpoint.
        # Same trap the retention sweep documents.
        def _checkpoint() -> None:
            with self.bot.ctx.open_db() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        try:
            await asyncio.to_thread(_checkpoint)
        except Exception:
            log.exception("WAL checkpoint after a guild purge failed")

    @_purge_loop.before_loop
    async def _before_purge(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: "Bot") -> None:
    await bot.add_cog(GuildPurgeCog(bot))
