"""Daily data-retention sweep — the loop behind the periods in the register.

Thin by design: every decision lives in
``bot_modules.services.retention_service``, which is where the periods, the
per-guild switch and the reasoning are written down. This file only decides
*when*.

There is no central housekeeping cog in this repo — each feature owns its own
``@tasks.loop(hours=24)`` — but retention spans six tables across five
features, so it gets its own thin cog rather than being scattered across theirs
or bolted onto one arbitrarily.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

from discord.ext import commands, tasks

from bot_modules.services import retention_service

log = logging.getLogger(__name__)


class RetentionCog(commands.Cog):
    def __init__(self, bot: "Bot") -> None:
        self.bot = bot
        self._retention_loop.start()

    async def cog_unload(self) -> None:
        self._retention_loop.cancel()

    @tasks.loop(hours=24)
    async def _retention_loop(self) -> None:
        """Redact aged message content and delete aged behavioural rows.

        Runs per guild so one guild switching retention off does not stop the
        others. Deliberately logs only when something actually happened: on a
        steady-state server every pass clears nothing, and a daily "0 rows"
        line would train the reader to skip exactly the message that matters
        on the day the numbers are wrong.
        """
        try:
            def _sweep() -> dict[str, int]:
                totals: dict[str, int] = {}
                with self.bot.ctx.open_db() as conn:
                    for guild in self.bot.guilds:
                        try:
                            result = retention_service.run_retention(
                                conn, guild.id
                            )
                        except Exception:
                            log.exception(
                                "Retention sweep failed for guild %s", guild.id
                            )
                            continue
                        for key, n in result.items():
                            totals[key] = totals.get(key, 0) + n
                return totals

            totals = await asyncio.to_thread(_sweep)
        except Exception:
            log.exception("Retention sweep failed")
            return

        if not any(totals.values()):
            return
        log.info(
            "Retention: %s",
            ", ".join(f"{k}={v}" for k, v in sorted(totals.items()) if v),
        )

        # Deleting rows returns pages to SQLite's freelist without shrinking
        # the file, and in WAL mode they sit in the -wal until a checkpoint.
        # The first pass clears ~26k behavioural rows, so force it rather than
        # waiting — same trap the XP prune documents.
        def _checkpoint() -> None:
            with self.bot.ctx.open_db() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        try:
            await asyncio.to_thread(_checkpoint)
        except Exception:
            log.exception("WAL checkpoint after the retention sweep failed")

    @_retention_loop.before_loop
    async def _before_retention(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: "Bot") -> None:
    await bot.add_cog(RetentionCog(bot))
