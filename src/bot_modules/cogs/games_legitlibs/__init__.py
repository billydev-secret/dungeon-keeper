import os
import logging

import discord
from discord.ext import commands
from discord import app_commands

from bot_modules.games.utils.game_manager import check_allowed_channel, get_active_game
from bot_modules.games.command_groups import play
from .data import seed_templates_from_file
from .modes.quiplash import run_quiplash
from .modes.classic import run_classic

log = logging.getLogger(__name__)

_SEED_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "templates_seed.json")

# Kill-switch flag — set to True by /legitlibs-admin killswitch
_MODULE_DISABLED = False


class LegitLibsCog(commands.Cog, name="LegitLibsCog"):
    def __init__(self, bot):
        self.bot = bot
        self._game_canceled: set[str] = set()

    @property
    def db(self):
        return self.bot.games_db

    async def cog_load(self):
        await seed_templates_from_file(self.db, _SEED_PATH, author_id=0)
        log.info("LegitLibsCog loaded.")

    # ── /games play legitlibs ─────────────────────────────────────────────────────────────
    @app_commands.command(name="legitlibs", description="Start a LegitLibs round!")
    @app_commands.describe(
        mode="Game mode: classic (default), quiplash, or hotseat",
        tier="Heat tier 1–4 (1=Flirty, 2=Spicy, 3=Filthy, 4=Unhinged). Default: 2",
        template_id="Optional: use a specific template by ID",
        tag="Optional: filter templates by tag",
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="Classic (sequential fill)", value="classic"),
        app_commands.Choice(name="Quiplash (everyone fills, all revealed)", value="quiplash"),
        app_commands.Choice(name="Hot Seat (author picks best fills)", value="hotseat"),
    ])
    @app_commands.choices(tier=[
        app_commands.Choice(name="1 — Flirty 🌶️", value=1),
        app_commands.Choice(name="2 — Spicy 🌶️🌶️", value=2),
        app_commands.Choice(name="3 — Filthy 🌶️🌶️🌶️", value=3),
        app_commands.Choice(name="4 — Unhinged 💀", value=4),
    ])
    async def legitlibs(
        self,
        interaction: discord.Interaction,
        mode: str = "classic",
        tier: int = 2,
        template_id: str = None,
        tag: str = None,
    ):
        global _MODULE_DISABLED
        log.info("%s used /games play legitlibs in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")

        if _MODULE_DISABLED:
            await interaction.response.send_message(
                "LegitLibs is currently disabled. Ask an admin to re-enable it.", ephemeral=True
            )
            return

        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it with `/games config allow-channel`.",
                ephemeral=True,
            )
            return

        existing = await get_active_game(self.db, interaction.channel_id)
        if existing and existing["game_type"] == "legitlibs":
            await interaction.response.send_message(
                "A LegitLibs round is already in progress here. Cancel it first.", ephemeral=True
            )
            return

        await interaction.response.defer()

        if mode == "quiplash":
            await run_quiplash(self, interaction, tier, template_id, tag)
        elif mode == "classic":
            await run_classic(self, interaction, tier, template_id, tag)
        elif mode == "hotseat":
            await interaction.followup.send("Hot Seat mode coming soon!", ephemeral=True)
        else:
            await interaction.followup.send("Unknown mode.", ephemeral=True)



async def setup(bot):
    cog = LegitLibsCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("legitlibs")
    play.add_command(cog.legitlibs)
