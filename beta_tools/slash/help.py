"""/beta help — overview embed."""

from __future__ import annotations

import discord

from beta_tools.slash._base import reject_if_not_mod


def register(bot) -> None:
    guild_obj = discord.Object(id=bot.main_cfg.guild_id)

    @bot.tree.command(name="beta-help", description="Show DK Tools beta-mode commands", guild=guild_obj)
    async def beta_help(interaction: discord.Interaction) -> None:
        if not await reject_if_not_mod(interaction):
            return
        embed = discord.Embed(
            title="DK Tools — Beta Tester Commands",
            description="Slash commands available while running against the beta server.",
            color=discord.Color.dark_purple(),
        )
        embed.add_field(
            name="Puppets",
            value=(
                "`/beta-puppets-list` — show roster + connection state\n"
                "`/beta-puppets-reload` — re-read fixtures/beta_puppets.yaml\n"
                "`/beta-puppets-reconnect <key>` — reconnect a single puppet\n"
                "`/beta-puppets-impersonate <key> <channel> <text>` — drive a puppet/ghost manually\n"
            ),
            inline=False,
        )
        embed.set_footer(text="More commands ship in later beta_tools plans.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
