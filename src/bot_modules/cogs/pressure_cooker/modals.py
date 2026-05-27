"""Discord modals for Pressure Cooker."""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

import discord

log = logging.getLogger("dungeonkeeper.pressure")


class NicknameModal(discord.ui.Modal, title="Set the Loser's Nickname"):
    nick_input = discord.ui.TextInput(
        label="Nickname for the loser",
        placeholder="Keep it fun, not harmful",
        style=discord.TextStyle.short,
        min_length=1,
        max_length=32,
        required=True,
    )

    def __init__(
        self,
        game_id: int,
        on_submit: Callable[[discord.Interaction, int, str], Awaitable[None]],
    ) -> None:
        super().__init__()
        self.game_id = game_id
        self._on_submit = on_submit

    async def on_submit(self, interaction: discord.Interaction) -> None:
        log.info(
            "%s submitted nickname modal (game %d): %r",
            interaction.user.display_name,
            self.game_id,
            self.nick_input.value,
        )
        await self._on_submit(interaction, self.game_id, self.nick_input.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("NicknameModal error (game %d)", self.game_id, exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong.", ephemeral=True)
