"""Hot Potato game view: a single PASS button."""
from __future__ import annotations

from typing import Callable

import discord

from bot_modules.core.utils import disable_all_items


class PassView(discord.ui.View):
    def __init__(self, game_id: int, on_pass: Callable) -> None:
        super().__init__(timeout=None)
        btn = discord.ui.Button(
            label="🥔 PASS",
            style=discord.ButtonStyle.primary,
            custom_id=f"pass:{game_id}",
        )

        async def _cb(interaction: discord.Interaction) -> None:
            await on_pass(interaction, game_id)

        btn.callback = _cb
        self.add_item(btn)

    def disable(self) -> None:
        disable_all_items(self)
