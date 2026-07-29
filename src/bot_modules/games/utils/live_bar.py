import asyncio
import time
import logging
import discord

from bot_modules.core.meters import fill, mono

log = logging.getLogger(__name__)

BAR_WIDTH = 14


def build_bar(count: int, total: int, width: int = BAR_WIDTH) -> tuple[str, str]:
    """Returns (bar_string, percentage_string) for a live vote meter.

    The bar comes back already wrapped in a code span. It has to: these bars
    sit next to each other as one field per option, and a bare ``▰▱`` run
    renders proportionally, so a 0% bar is visibly *longer* than a 50% one
    even though both are exactly ``width`` characters. See
    ``bot_modules.core.meters`` for the full explanation.
    """
    pct = f"{round(count / total * 100)}%" if total else "0%"
    return mono(fill(count, total, width)), pct


class LiveBarUpdater:
    """Rate-limits embed edits to once per 3 seconds to avoid Discord API rate limits."""

    def __init__(self, min_interval: float = 3.0):
        self._last_update: float = 0.0
        self._pending: bool = False
        self._min_interval = min_interval
        self._lock = asyncio.Lock()

    async def schedule_update(self, message: discord.Message | None, build_embed_fn):
        """
        Call build_embed_fn() to get the new embed and edit the message.
        Rate-limited to once per min_interval seconds. Accepts None (no-op)
        so callers can pass interaction.message without narrowing.
        """
        if message is None:
            return
        async with self._lock:
            now = time.monotonic()
            gap = now - self._last_update
            if gap < self._min_interval:
                if self._pending:
                    return  # Another update is already queued; drop this one
                self._pending = True
            else:
                self._last_update = time.monotonic()
                self._pending = False

        # If we need to wait, do so outside the lock
        if self._pending:
            await asyncio.sleep(self._min_interval - gap)
            async with self._lock:
                self._last_update = time.monotonic()
                self._pending = False

        try:
            embed = build_embed_fn()
            await message.edit(embed=embed)
        except Exception as e:
            log.debug("LiveBarUpdater edit error: %s", e)
