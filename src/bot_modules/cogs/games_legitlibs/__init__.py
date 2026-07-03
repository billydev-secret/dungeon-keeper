import os
import logging

import discord
from discord.ext import commands
from discord import app_commands

from bot_modules.games.utils.game_manager import channel_name, check_allowed_channel, get_active_game
from bot_modules.games.command_groups import play
from .data import seed_templates_from_file
from .modes.quiplash import run_quiplash
from .modes.classic import run_classic

log = logging.getLogger(__name__)

_SEED_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "templates_seed.json")

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
        mode="Game mode: classic (default) or quiplash",
        tier="Heat tier 1–4 (1=Flirty, 2=Spicy, 3=Filthy, 4=Unhinged). Default: 2",
        template_id="Optional: use a specific template by ID",
        tag="Optional: filter templates by tag",
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="Classic (sequential fill)", value="classic"),
        app_commands.Choice(name="Quiplash (everyone fills, all revealed)", value="quiplash"),
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
        log.info("%s used /games play legitlibs in #%s", interaction.user.display_name, channel_name(interaction.channel))

        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it from the web dashboard.",
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

        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={"mode": mode, "tier": tier, "template_id": template_id, "tag": tag},
        )
        if game_id is None:
            try:
                await interaction.followup.send(
                    "Couldn't start LegitLibs — no published templates for that tier/tag, "
                    "or I'm missing permission to post here.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass

    async def launch(self, *, channel, host_id, host_name, guild_id, options) -> str | None:
        """Interaction-free launch (slash command + scheduler). Returns game_id, or None."""
        mode = options.get("mode", "classic")
        tier = int(options.get("tier", 2))
        template_id = options.get("template_id") or None
        tag = options.get("tag") or None
        guild = getattr(channel, "guild", None)
        if mode == "quiplash":
            return await run_quiplash(self, channel=channel, guild=guild, host_id=host_id, host_name=host_name, tier=tier, template_id=template_id, tag=tag)
        if mode == "classic":
            return await run_classic(self, channel=channel, guild=guild, host_id=host_id, host_name=host_name, tier=tier, template_id=template_id, tag=tag)
        # hotseat not implemented
        log.info("legitlibs launch: mode %r not available", mode)
        return None



async def setup(bot):
    cog = LegitLibsCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("legitlibs")
    play.add_command(cog.legitlibs)
    bot.game_launchers["legitlibs"] = cog.launch
