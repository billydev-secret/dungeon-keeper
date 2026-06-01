import logging

import discord
from discord.ext import commands
from discord import app_commands

from bot_modules.games_consent.logic import (
    CONSENT_PROMPT_BODY,
    CONSENT_PROMPT_COLOR,
    CONSENT_PROMPT_FOOTER,
    CONSENT_PROMPT_TITLE,
    STATUS_FOOTER,
    STATUS_TITLE,
    format_status_description,
    interpret_consent_status,
    opt_in_summary,
    opt_out_summary,
)

log = logging.getLogger(__name__)


class ConsentView(discord.ui.View):
    def __init__(self, db):
        super().__init__(timeout=120)
        self.db = db

    @discord.ui.button(label="✅ Opt In", style=discord.ButtonStyle.success)
    async def opt_in(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        await self.db.execute(
            """
            INSERT INTO games_consent (user_id, tod_consent)
            VALUES (?, TRUE)
            ON CONFLICT(user_id) DO UPDATE SET tod_consent = TRUE, updated_at = CURRENT_TIMESTAMP
            """,
            (interaction.user.id,),
        )
        title, body, color = opt_in_summary()
        embed = discord.Embed(title=title, description=body, color=color)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="❌ Opt Out", style=discord.ButtonStyle.danger)
    async def opt_out(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        await self.db.execute(
            """
            INSERT INTO games_consent (user_id, tod_consent)
            VALUES (?, FALSE)
            ON CONFLICT(user_id) DO UPDATE SET tod_consent = FALSE, updated_at = CURRENT_TIMESTAMP
            """,
            (interaction.user.id,),
        )
        title, body, color = opt_out_summary()
        embed = discord.Embed(title=title, description=body, color=color)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ConsentCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(name="consent", description="Manage your consent settings for game nights.")
    async def consent(self, interaction: discord.Interaction):
        log.info("%s used /consent in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        embed = discord.Embed(
            title=CONSENT_PROMPT_TITLE,
            description=CONSENT_PROMPT_BODY,
            color=CONSENT_PROMPT_COLOR,
        )
        embed.set_footer(text=CONSENT_PROMPT_FOOTER)
        await interaction.response.send_message(
            embed=embed, view=ConsentView(self.db), ephemeral=True
        )

    @app_commands.command(
        name="consent-status",
        description="Check your current consent status.",
    )
    async def consent_status(self, interaction: discord.Interaction):
        log.info("%s used /consent-status in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        row = await self.db.fetchone(
            "SELECT tod_consent, updated_at FROM games_consent WHERE user_id = ?",
            (interaction.user.id,),
        )
        label, color, updated = interpret_consent_status(row)
        embed = discord.Embed(
            title=STATUS_TITLE,
            description=format_status_description(label, updated),
            color=color,
        )
        embed.set_footer(text=STATUS_FOOTER)
        await interaction.response.send_message(embed=embed, ephemeral=True)


    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.mentions:
            return
        from bot_modules.games.utils.consent_check import scan_mentions_for_consent
        active_channel_ids: set[int] = {
            v._channel_id
            for v in self.bot.active_views.values()  # type: ignore[attr-defined]
            if hasattr(v, "_channel_id")
        }
        if not active_channel_ids:
            rows = await self.db.fetchall("SELECT channel_id FROM games_active_games")
            active_channel_ids = {row["channel_id"] for row in rows}
        await scan_mentions_for_consent(self.db, message, active_channel_ids)


async def setup(bot: commands.Bot):
    await bot.add_cog(ConsentCog(bot))
