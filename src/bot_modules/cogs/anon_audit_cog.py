"""Retention sweep for the anonymous-features audit trail.

Deliberately a cog of its own rather than another loop bolted onto
``events_cog``: ``anon_audit_log`` spans seven features across the games
suite, so no single feature cog is its natural owner, and a one-loop cog keeps
the sweep independently testable and independently disableable.

Nothing else lives here. Writes happen at the call sites
(``games/utils/audit.py``); reads happen on the dashboard.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from discord.ext import commands, tasks

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

from bot_modules.services.anon_audit_service import purge_expired

log = logging.getLogger(__name__)

# Six hours, not hourly: retention is measured in days, so a sweep that lands
# within a quarter-day of the cutoff is exact enough, and the sweep scans every
# guild in the table.
PURGE_INTERVAL_HOURS = 6


class AnonAuditCog(commands.Cog):
    def __init__(self, bot: "Bot") -> None:
        self.bot = bot
        self.ctx = bot.ctx

    async def cog_load(self) -> None:
        self._purge_loop.start()

    async def cog_unload(self) -> None:
        self._purge_loop.cancel()

    @tasks.loop(hours=PURGE_INTERVAL_HOURS)
    async def _purge_loop(self) -> None:
        try:
            removed = await asyncio.to_thread(purge_expired, self.ctx.db_path)
        except Exception:
            log.exception("anon audit retention sweep failed")
            return
        if removed:
            log.info("anon audit retention sweep removed %d row(s)", removed)

    @_purge_loop.before_loop
    async def _before_purge(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: "Bot") -> None:
    await bot.add_cog(AnonAuditCog(bot))
