"""Role grant commands."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.commands.role_grant_commands import (
    _execute_grant,
    _execute_grant_audit_post,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.role_grant")


class RoleGrantCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    async def _role_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        choices: list[app_commands.Choice[str]] = []
        for key, cfg in self.ctx.guild_config(interaction.guild_id or 0).grant_roles.items():
            if (
                current.lower() in key.lower()
                or current.lower() in cfg["label"].lower()
            ):
                choices.append(app_commands.Choice(name=cfg["label"], value=key))
        return choices[:25]

    @app_commands.command(
        name="grant", description="Give a configured community role to a member."
    )
    @app_commands.describe(
        role="Role to grant (from your configured grant roles).",
        member="Member to receive the role.",
    )
    @app_commands.autocomplete(role=_role_autocomplete)
    async def grant_cmd(
        self,
        interaction: discord.Interaction,
        role: str,
        member: discord.Member,
    ) -> None:
        ctx = self.ctx
        if not ctx.can_use_grant_role(interaction, role):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return
        cfg = ctx.guild_config(interaction.guild_id or 0).grant_roles.get(role)
        if cfg is None:
            await interaction.response.send_message(
                "This grant role is not configured.", ephemeral=True
            )
            return
        await _execute_grant(
            interaction,
            member,
            role_id=cfg["role_id"],
            log_channel_id=cfg["log_channel_id"],
            announce_channel_id=cfg["announce_channel_id"],
            grant_message=cfg["grant_message"],
            ctx=ctx,
        )

    @app_commands.command(
        name="grant_audit",
        description="Post (or refresh) the auto-updating grant-audit card (mods).",
    )
    @app_commands.describe(
        role="Grant role to audit.",
        min_level="Minimum XP level for the waiting bucket (default 5).",
        channel="Where the card lives — defaults to this channel.",
    )
    @app_commands.autocomplete(role=_role_autocomplete)
    async def grant_audit_cmd(
        self,
        interaction: discord.Interaction,
        role: str = "nsfw",
        min_level: int = 5,
        channel: discord.TextChannel | None = None,
    ) -> None:
        ctx = self.ctx
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return
        await _execute_grant_audit_post(interaction, role, min_level, channel, ctx)


async def setup(bot: Bot) -> None:
    await bot.add_cog(RoleGrantCog(bot, bot.ctx))
