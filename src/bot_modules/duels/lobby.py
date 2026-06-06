"""LobbyView — shared join/start lobby for N-player (BaseGame) games.

Persistent view (timeout=None); expiry is handled by BaseGame's sweep loop, not the
view timeout. Button custom_ids embed the game_id so they survive a bot restart and are
rebuilt in BaseGame.cog_load.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

import discord

log = logging.getLogger("dungeonkeeper.duels")

_Handler = Callable[[discord.Interaction, int], Awaitable[None]]


class LobbyView(discord.ui.View):
    """Join lobby for multiplayer games. `✋ Join` / `🚪 Leave` for anyone,
    `▶️ Start` / `🚫 Cancel` gated to the host inside the handlers."""

    def __init__(
        self,
        game_id: int,
        host_id: int,
        on_join: _Handler,
        on_leave: _Handler,
        on_start: _Handler,
        on_cancel: _Handler,
    ) -> None:
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self._on_join = on_join
        self._on_leave = on_leave
        self._on_start = on_start
        self._on_cancel = on_cancel

        join_btn = discord.ui.Button(
            label="Join", style=discord.ButtonStyle.success, emoji="✋",
            custom_id=f"lobby_join:{game_id}",
        )
        join_btn.callback = self._join_cb

        leave_btn = discord.ui.Button(
            label="Leave", style=discord.ButtonStyle.secondary, emoji="🚪",
            custom_id=f"lobby_leave:{game_id}",
        )
        leave_btn.callback = self._leave_cb

        start_btn = discord.ui.Button(
            label="Start", style=discord.ButtonStyle.primary, emoji="▶️",
            custom_id=f"lobby_start:{game_id}",
        )
        start_btn.callback = self._start_cb

        cancel_btn = discord.ui.Button(
            label="Cancel", style=discord.ButtonStyle.danger, emoji="🚫",
            custom_id=f"lobby_cancel:{game_id}",
        )
        cancel_btn.callback = self._cancel_cb

        self.add_item(join_btn)
        self.add_item(leave_btn)
        self.add_item(start_btn)
        self.add_item(cancel_btn)

    def disable(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def _join_cb(self, interaction: discord.Interaction) -> None:
        await self._on_join(interaction, self.game_id)

    async def _leave_cb(self, interaction: discord.Interaction) -> None:
        await self._on_leave(interaction, self.game_id)

    async def _start_cb(self, interaction: discord.Interaction) -> None:
        await self._on_start(interaction, self.game_id)

    async def _cancel_cb(self, interaction: discord.Interaction) -> None:
        await self._on_cancel(interaction, self.game_id)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        log.exception("LobbyView error (game %d)", self.game_id, exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong.", ephemeral=True)
