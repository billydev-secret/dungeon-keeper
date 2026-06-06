"""Discord view for Hot Potato (group) — a single persistent Pass button."""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

import discord

log = logging.getLogger("dungeonkeeper.hot_potato_group")


class PassGroupView(discord.ui.View):
    """Persistent view with one 🤲 Pass button. Holder/min-hold validation happens
    server-side in the cog's handle_interaction."""

    def __init__(
        self,
        game_id: int,
        on_press: Callable[[discord.Interaction, int], Awaitable[None]],
    ) -> None:
        super().__init__(timeout=None)
        self.game_id = game_id
        self._on_press = on_press

        btn = discord.ui.Button(
            label="Pass",
            style=discord.ButtonStyle.danger,
            emoji="🤲",
            custom_id=f"hpg_pass:{game_id}",
        )
        btn.callback = self._press_cb
        self.add_item(btn)

    def disable(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def _press_cb(self, interaction: discord.Interaction) -> None:
        await self._on_press(interaction, self.game_id)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        log.exception("PassGroupView error (game %d)", self.game_id, exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong.", ephemeral=True)
