import logging

import discord
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import GOLDEN_MEADOW_COLOR, SUCCESS_COLOR, ERROR_COLOR

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
        embed = discord.Embed(
            title="✅ Preference Updated",
            description="You've **opted in** — recorded as happy to participate fully.",
            color=SUCCESS_COLOR,
        )
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
        embed = discord.Embed(
            title="❌ Preference Updated",
            description="You've **opted out** — recorded as preferring not to be included.",
            color=ERROR_COLOR,
        )
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
            title="🌸 Consent Settings",
            description=(
                "Record your participation preference for game nights.\n\n"
                "**Opt in** — happy to be included, mentioned, and participate fully.\n"
                "**Opt out** — prefer to observe or participate on your own terms.\n\n"
                "You can change this at any time."
            ),
            color=GOLDEN_MEADOW_COLOR,
        )
        embed.set_footer(text="Golden Meadow Games")
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
        if row and row[0]:
            status = "✅ **Opted In**"
            color = SUCCESS_COLOR
        else:
            status = "❌ **Opted Out** (or no record found)"
            color = ERROR_COLOR

        updated = row[1] if row else "Never"
        embed = discord.Embed(
            title="Consent Status",
            description=f"Your current status: {status}\nLast updated: `{updated}`",
            color=color,
        )
        embed.set_footer(text="Use /consent to change your preference.")
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
