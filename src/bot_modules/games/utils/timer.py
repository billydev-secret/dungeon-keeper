import asyncio
import time
import logging
import discord

log = logging.getLogger(__name__)


def format_deadline(deadline: int) -> str:
    """Render a Discord dynamic relative timestamp that ticks down on the client."""
    return f"⏰ ends <t:{deadline}:R>"


def now_plus(seconds: int) -> int:
    """Return a Unix timestamp `seconds` from now."""
    return int(time.time()) + seconds


class GameTimer:
    """Countdown timer that ticks down live on Discord clients via <t:UNIX:R>.

    The bot only edits the message twice: once at start to install the dynamic
    timestamp, and once when the timer expires (color flash + callback).
    """

    def __init__(
        self,
        duration: int,
        message: discord.Message,
        callback,
        timer_field_index: int = 0,
    ):
        self.duration = duration
        self.message = message
        self.callback = callback
        self.timer_field_index = timer_field_index
        self.deadline: int = 0
        self._task: asyncio.Task | None = None
        self._cancelled = False
        self._skipped = False

    async def start(self):
        if self.duration <= 0:
            return
        self.deadline = now_plus(self.duration)
        await self._install_field()
        self._task = asyncio.create_task(self._run())

    async def _install_field(self):
        """Install the dynamic timestamp once. Skipped if the embed builder
        already rendered a `<t:...:R>` markup in the timer field."""
        try:
            if not self.message.embeds:
                return
            embed = self.message.embeds[0]
            if self.timer_field_index >= len(embed.fields):
                return
            field = embed.fields[self.timer_field_index]
            if field.value and "<t:" in field.value:
                return  # Already installed by the embed builder
            new_embed = embed.copy()
            new_embed.set_field_at(
                self.timer_field_index,
                name=field.name,
                value=format_deadline(self.deadline),
                inline=field.inline,
            )
            await self.message.edit(embed=new_embed)
        except Exception as e:
            log.debug("Timer field install error: %s", e)

    async def _run(self):
        # Sleep until the deadline (or until cancelled/skipped)
        try:
            remaining = self.deadline - int(time.time())
            if remaining > 0:
                await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            pass  # Either skip() or cancel() — decide below

        if self._cancelled:
            return

        try:
            await self.callback()
        except Exception as e:
            log.error("GameTimer callback error: %s", e)

    def cancel(self):
        self._cancelled = True
        if self._task:
            self._task.cancel()

    def skip(self):
        """Immediately fire the callback by cancelling the wait."""
        self._skipped = True
        if self._task:
            self._task.cancel()

    @property
    def remaining(self) -> int:
        """Seconds left until deadline (clamped to 0)."""
        if self.deadline == 0:
            return self.duration
        return max(0, self.deadline - int(time.time()))
