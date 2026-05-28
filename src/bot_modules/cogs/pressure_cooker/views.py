"""Discord UI views for Pressure Cooker."""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

import discord

log = logging.getLogger("dungeonkeeper.pressure")


def gauge_bar(gauge: int, width: int = 20) -> str:
    """Render a text progress bar. e.g. |████████░░░░░░░░░░░░| 42/100"""
    filled = round(max(0, min(gauge, 100)) / 100 * width)
    return f"|{'█' * filled}{'░' * (width - filled)}| {gauge}/100"


# ── ChallengeView ─────────────────────────────────────────────────────────────

class ChallengeView(discord.ui.View):
    """Accept/Decline embed — target only, 60s timeout, NOT persistent."""

    def __init__(
        self,
        game_id: int,
        target_id: int,
        on_accept: Callable[[discord.Interaction, int], Awaitable[None]],
        on_decline: Callable[[discord.Interaction, int], Awaitable[None]],
    ) -> None:
        super().__init__(timeout=60)
        self.game_id = game_id
        self.target_id = target_id
        self._on_accept = on_accept
        self._on_decline = on_decline

        accept_btn = discord.ui.Button(
            label="Accept",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id=f"challenge_accept:{game_id}",
        )
        accept_btn.callback = self._accept_callback

        decline_btn = discord.ui.Button(
            label="Decline",
            style=discord.ButtonStyle.danger,
            emoji="❌",
            custom_id=f"challenge_decline:{game_id}",
        )
        decline_btn.callback = self._decline_callback

        self.add_item(accept_btn)
        self.add_item(decline_btn)

    def _disable_all(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def _accept_callback(self, interaction: discord.Interaction) -> None:
        log.info("%s accepted challenge (game %d)", interaction.user.display_name, self.game_id)
        if interaction.user.id != self.target_id:
            await interaction.response.send_message(
                "Only the challenged player can accept.", ephemeral=True
            )
            return
        self.stop()
        self._disable_all()
        await self._on_accept(interaction, self.game_id)

    async def _decline_callback(self, interaction: discord.Interaction) -> None:
        log.info("%s declined challenge (game %d)", interaction.user.display_name, self.game_id)
        if interaction.user.id != self.target_id:
            await interaction.response.send_message(
                "Only the challenged player can decline.", ephemeral=True
            )
            return
        self.stop()
        self._disable_all()
        await self._on_decline(interaction, self.game_id)

    async def on_timeout(self) -> None:
        self._disable_all()

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        log.exception("ChallengeView error (game %d)", self.game_id, exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong.", ephemeral=True)


# ── GameView ──────────────────────────────────────────────────────────────────

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
            label="PUMP",
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
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        log.exception("GameView error (game %d)", self.game_id, exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong.", ephemeral=True)


# ── ResultView ────────────────────────────────────────────────────────────────

class ResultView(discord.ui.View):
    """Post-game buttons — persistent (timeout=None).

    mode="nick"    → Set Nickname + Rematch
    mode="stakes"  → I'll Honor This + Rematch
    Additional modes can be added as the game expands.
    """

    def __init__(
        self,
        game_id: int,
        winner_id: int,
        loser_id: int,
        on_set_nick: Callable[[discord.Interaction, int], Awaitable[None]],
        on_honor: Callable[[discord.Interaction, int], Awaitable[None]],
        on_rematch: Callable[[discord.Interaction, int], Awaitable[None]],
        *,
        mode: str = "nick",
    ) -> None:
        super().__init__(timeout=None)
        self.game_id = game_id
        self.winner_id = winner_id
        self.loser_id = loser_id
        self._on_set_nick = on_set_nick
        self._on_honor = on_honor
        self._on_rematch = on_rematch

        if mode == "nick":
            nick_btn = discord.ui.Button(
                label="Set Nickname",
                style=discord.ButtonStyle.primary,
                emoji="📝",
                custom_id=f"set_nick:{game_id}",
            )
            nick_btn.callback = self._set_nick_callback
            self.add_item(nick_btn)
        elif mode == "stakes":
            honor_btn = discord.ui.Button(
                label="I'll honor this",
                style=discord.ButtonStyle.secondary,
                emoji="🤝",
                custom_id=f"honor:{game_id}",
            )
            honor_btn.callback = self._honor_callback
            self.add_item(honor_btn)

        rematch_btn = discord.ui.Button(
            label="Rematch",
            style=discord.ButtonStyle.success,
            emoji="🔁",
            custom_id=f"rematch:{game_id}",
        )
        rematch_btn.callback = self._rematch_callback
        self.add_item(rematch_btn)

    async def _set_nick_callback(self, interaction: discord.Interaction) -> None:
        log.info(
            "%s pressed Set Nickname (game %d)", interaction.user.display_name, self.game_id
        )
        if interaction.user.id != self.winner_id:
            await interaction.response.send_message(
                "Only the winner can set the nickname.", ephemeral=True
            )
            return
        await self._on_set_nick(interaction, self.game_id)

    async def _honor_callback(self, interaction: discord.Interaction) -> None:
        log.info(
            "%s pressed Honor (game %d)", interaction.user.display_name, self.game_id
        )
        if interaction.user.id != self.loser_id:
            await interaction.response.send_message(
                "Only the loser can press this button.", ephemeral=True
            )
            return
        await self._on_honor(interaction, self.game_id)

    async def _rematch_callback(self, interaction: discord.Interaction) -> None:
        log.info(
            "%s pressed Rematch (game %d)", interaction.user.display_name, self.game_id
        )
        if interaction.user.id not in (self.winner_id, self.loser_id):
            await interaction.response.send_message(
                "Only players from this game can rematch.", ephemeral=True
            )
            return
        await self._on_rematch(interaction, self.game_id)

    def disable(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        log.exception("ResultView error (game %d)", self.game_id, exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong.", ephemeral=True)
