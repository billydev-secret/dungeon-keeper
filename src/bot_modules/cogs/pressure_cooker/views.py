"""Discord UI views specific to Pressure Cooker."""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

import discord

from bot_modules.core.utils import disable_all_items

log = logging.getLogger("dungeonkeeper.pressure")


def gauge_bar(gauge: int, width: int = 20) -> str:
    """Render a text progress bar. e.g. ▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱ 42/100"""
    filled = round(max(0, min(gauge, 100)) / 100 * width)
    return f"{'▰' * filled}{'▱' * (width - filled)} {gauge}/100"


class GameView(discord.ui.View):
    """PUMP button — persistent (timeout=None), custom_id encodes game_id."""

    def __init__(
        self,
        game_id: int,
        on_pump: Callable[[discord.Interaction, int], Awaitable[None]],
    ) -> None:
        super().__init__(timeout=None)
        self.game_id = game_id
        self._on_pump = on_pump

        pump_btn = discord.ui.Button(
            label="Pump",
            style=discord.ButtonStyle.danger,
            emoji="💨",
            custom_id=f"pump:{game_id}",
        )
        pump_btn.callback = self._pump_callback
        self.add_item(pump_btn)

    async def _pump_callback(self, interaction: discord.Interaction) -> None:
        log.info("%s pumped (game %d)", interaction.user.display_name, self.game_id)
        await self._on_pump(interaction, self.game_id)

    def disable(self) -> None:
        disable_all_items(self)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        log.exception("GameView error (game %d)", self.game_id, exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong.", ephemeral=True)
