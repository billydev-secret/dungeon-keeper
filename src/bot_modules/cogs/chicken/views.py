"""Discord view for Chicken — a single BAIL button (all players hold at start)."""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

import discord

from bot_modules.core.utils import disable_all_items

log = logging.getLogger("dungeonkeeper.chicken")


class ChickenView(discord.ui.View):
    def __init__(
        self,
        game_id: int,
        on_press: Callable[[discord.Interaction, int], Awaitable[None]],
    ) -> None:
        super().__init__(timeout=None)
        self.game_id = game_id
        self._on_press = on_press

        btn = discord.ui.Button(
            label="Bail",
            style=discord.ButtonStyle.danger,
            emoji="🐔",
            custom_id=f"chicken_bail:{game_id}",
        )
        btn.callback = self._press_cb
        self.add_item(btn)

    def disable(self) -> None:
        disable_all_items(self)

    async def _press_cb(self, interaction: discord.Interaction) -> None:
        await self._on_press(interaction, self.game_id)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        log.exception("ChickenView error (game %d)", self.game_id, exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong.", ephemeral=True)
