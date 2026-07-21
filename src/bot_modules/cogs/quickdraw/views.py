"""Quickdraw game view: a single FIRE button."""
from __future__ import annotations

from typing import Callable

import discord

from bot_modules.core.utils import disable_all_items


class FireView(discord.ui.View):
    def __init__(self, game_id: int, on_fire: Callable) -> None:
        super().__init__(timeout=None)
        btn = discord.ui.Button(
            label="🔫 Fire",
            style=discord.ButtonStyle.danger,
            custom_id=f"fire:{game_id}",
        )

        async def _cb(interaction: discord.Interaction) -> None:
            await on_fire(interaction, game_id)

        btn.callback = _cb
        self.add_item(btn)

    def disable(self) -> None:
        disable_all_items(self)
