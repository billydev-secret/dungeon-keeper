"""Moderator /rename — change a member's nickname."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

# Discord caps nicknames at 32 characters.
MAX_NICK_LENGTH = 32


class RenameCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    @app_commands.command(
        name="rename",
        description="Change a member's nickname (moderators only).",
    )
    @app_commands.default_permissions(manage_nicknames=True)
    @app_commands.guild_only()
    @app_commands.describe(
        target="The member to rename.",
        new_name="The new nickname (leave blank to reset to their username).",
    )
    async def rename(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        new_name: str | None = None,
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
                "This command can only be used in a server.", ephemeral=True
            )
            return

        nick = new_name.strip() if new_name else None
        if nick is not None and len(nick) > MAX_NICK_LENGTH:
            await interaction.response.send_message(
                f"Nicknames can be at most {MAX_NICK_LENGTH} characters "
                f"(that one is {len(nick)}).",
                ephemeral=True,
            )
            return

        if target == guild.owner:
            await interaction.response.send_message(
                "I can't rename the server owner — Discord doesn't allow it.",
                ephemeral=True,
            )
            return

        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_nicknames:
            await interaction.response.send_message(
                "I need the **Manage Nicknames** permission to do this.",
                ephemeral=True,
            )
            return

        if target.top_role >= bot_member.top_role:
            await interaction.response.send_message(
                f"I can't rename {target.mention} — their highest role is above mine.",
                ephemeral=True,
            )
            return

        old_display = target.display_name
        try:
            await target.edit(
                nick=nick,
                reason=f"Renamed by {interaction.user} ({interaction.user.id})",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                f"I'm not allowed to rename {target.mention} (role hierarchy or "
                "permissions).",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            await interaction.response.send_message(
                "Something went wrong talking to Discord. Please try again.",
                ephemeral=True,
            )
            return

        if nick is None:
            msg = f"Reset {target.mention}'s nickname (was **{old_display}**)."
        else:
            msg = f"Renamed {target.mention} from **{old_display}** to **{nick}**."
        await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: Bot) -> None:
    await bot.add_cog(RenameCog(bot, bot.ctx))
