"""Consolidated /config command."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from commands.config_commands import (
    _SECTION_CHOICES,
    _GlobalModal,
    _WelcomeLeaveModal,
    _RolesView,
    _build_roles_embed,
    _XpView,
    _build_xp_embed,
    _build_prune_panel,
    _SpoilerView,
    _build_spoiler_embed,
    _build_booster_overview,
)

if TYPE_CHECKING:
    from app_context import AppContext, Bot


class ConfigCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    @app_commands.command(
        name="config",
        description="Open the settings panel for a bot feature.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(section="Feature to configure.")
    @app_commands.choices(section=_SECTION_CHOICES)
    async def config_cmd(
        self,
        interaction: discord.Interaction,
        section: str,
    ) -> None:
        ctx = self.ctx
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        current_channel_id: int = interaction.channel_id or 0

        if section == "global":
            await interaction.response.send_modal(_GlobalModal(ctx, current_channel_id))

        elif section == "welcome":
            await interaction.response.send_modal(
                _WelcomeLeaveModal(ctx, current_channel_id)
            )

        elif section == "roles":
            await interaction.response.defer(ephemeral=True)
            await interaction.followup.send(
                embed=_build_roles_embed(ctx),
                view=_RolesView(
                    ctx, interaction.user.id, current_channel_id, interaction
                ),
                ephemeral=True,
            )

        elif section == "xp":
            await interaction.response.send_message(
                embed=_build_xp_embed(ctx, guild, current_channel_id),
                view=_XpView(
                    ctx, interaction.user.id, guild, current_channel_id, interaction
                ),
                ephemeral=True,
            )

        elif section == "prune":
            await interaction.response.defer(ephemeral=True)
            prune_embed, prune_view = _build_prune_panel(
                ctx, guild, interaction, interaction.user.id
            )
            await interaction.followup.send(
                embed=prune_embed, view=prune_view, ephemeral=True
            )

        elif section == "spoiler":
            await interaction.response.send_message(
                embed=_build_spoiler_embed(ctx, guild, current_channel_id),
                view=_SpoilerView(
                    ctx, guild, interaction.user.id, current_channel_id, interaction
                ),
                ephemeral=True,
            )

        elif section == "booster":
            await interaction.response.defer(ephemeral=True)
            embed, view = _build_booster_overview(ctx, guild, interaction)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot: Bot) -> None:
    await bot.add_cog(ConfigCog(bot, bot.ctx))
