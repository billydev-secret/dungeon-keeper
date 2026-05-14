import asyncio
import time
import logging
import discord

log = logging.getLogger(__name__)

BAR_WIDTH = 14

# Eighth-block characters: index 1 = 1/8 filled … index 7 = 7/8 filled
_EIGHTHS = "▏▎▍▌▋▊▉"


def build_bar(count: int, total: int, width: int = BAR_WIDTH) -> tuple[str, str]:
    """Returns (bar_string, percentage_string) with sub-character precision."""
    if total == 0:
        return f"`{'░' * width}`", "0%"
    ratio = count / total
    pct = f"{round(ratio * 100)}%"

    units = ratio * width * 8  # total eighth-units to fill
    full_blocks = int(units // 8)
    remainder = int(units % 8)

    bar = "█" * full_blocks
    if full_blocks < width:
        if remainder > 0:
            bar += _EIGHTHS[remainder - 1]
            bar += "░" * (width - full_blocks - 1)
        else:
            bar += "░" * (width - full_blocks)

    return f"`{bar}`", pct


class LiveBarUpdater:
    """Rate-limits embed edits to once per 3 seconds to avoid Discord API rate limits."""

    def __init__(self, min_interval: float = 3.0):
        self._last_update: float = 0.0
        self._pending: bool = False
        self._min_interval = min_interval
        self._lock = asyncio.Lock()

    async def schedule_update(self, message: discord.Message, build_embed_fn):
        """
        Call build_embed_fn() to get the new embed and edit the message.
        Rate-limited to once per min_interval seconds.
        """
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
