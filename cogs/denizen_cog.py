"""Role grant commands."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from commands.denizen_commands import _execute_grant

if TYPE_CHECKING:
    from app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.denizen")


class DenizenCog(commands.Cog):
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
        for key, cfg in self.ctx.grant_roles.items():
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
        cfg = ctx.grant_roles.get(role)
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


async def setup(bot: Bot) -> None:
    await bot.add_cog(DenizenCog(bot, bot.ctx))
