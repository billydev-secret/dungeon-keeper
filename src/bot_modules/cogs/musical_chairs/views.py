"""Discord view for Musical Chairs — a single always-live SIT button.

The button is clickable during MUSIC too (that's the false-start trap); state is
checked server-side in the cog's _on_sit handler.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

import discord

from bot_modules.core.utils import disable_all_items

log = logging.getLogger("dungeonkeeper.musical_chairs")


class SitView(discord.ui.View):
    def __init__(
        self,
        game_id: int,
        on_press: Callable[[discord.Interaction, int], Awaitable[None]],
    ) -> None:
        super().__init__(timeout=None)
        self.game_id = game_id
        self._on_press = on_press

        btn = discord.ui.Button(
            label="Sit",
            style=discord.ButtonStyle.primary,
            emoji="🪑",
            custom_id=f"mc_sit:{game_id}",
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
        log.exception("SitView error (game %d)", self.game_id, exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Something went wrong.", ephemeral=True)
