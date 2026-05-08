"""Veil cog — Phase 1 infrastructure stub.

NudeNet and mediapipe are NOT imported here; they are loaded lazily inside
the services layer (veil_pipeline / veil_nudenet / veil_face_detector).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

import services.veil_pipeline  # noqa: F401 — safe; heavy libs lazy-loaded inside
import services.veil_repo  # noqa: F401 — safe; only stdlib + db_utils imports

if TYPE_CHECKING:
    from app_context import Bot

log = logging.getLogger("dungeonkeeper.veil")


class VeilCog(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        super().__init__()

    # ── Commands ─────────────────────────────────────────────────────────────

    @app_commands.command(name="veil_status", description="Show current Veil system status.")
    async def veil_status(self, interaction: discord.Interaction) -> None:
        """Placeholder command — Phase 1 infrastructure ready."""
        await interaction.response.send_message(
            "Veil system: Phase 1 infrastructure ready.", ephemeral=True
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(VeilCog(bot))
