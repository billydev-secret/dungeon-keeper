"""Admin ``/hidden`` — hide a channel from everyone and restore it later.

``/hidden hide #channel`` snapshots the channel's permission overwrites and its
current placement (parent category + position), then denies ``@everyone`` and
parks it under a "Hidden Channels" category. ``/hidden restore #channel`` reads
that snapshot back to move the channel home and reinstate its exact overwrites.
``/hidden list`` shows what's currently hidden.

Note: "hidden from everyone" means ``@everyone`` is denied View Channel. Members
with Administrator still see it — Discord always exempts Administrator from
channel overwrites; there's no way around that.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Union

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.hidden_channels.overwrites import (
    rebuild_overwrites,
    serialize_overwrites,
)
from bot_modules.hidden_channels.store import (
    create_hidden,
    get_active_hidden,
    list_active_hidden,
    mark_restored,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.hidden_channels")

HIDDEN_CATEGORY_NAME = "Hidden Channels"

# Channel kinds a member can pick that carry their own overwrites. Categories
# are excluded — hiding a category would orphan its children.
HideableChannel = Union[
    discord.TextChannel,
    discord.VoiceChannel,
    discord.StageChannel,
    discord.ForumChannel,
]


async def _ensure_hidden_category(
    guild: discord.Guild, reason: str
) -> discord.CategoryChannel:
    """Return the guild's "Hidden Channels" category, creating it if absent."""
    for cat in guild.categories:
        if cat.name == HIDDEN_CATEGORY_NAME:
            return cat
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True),
    }
    return await guild.create_category(
        HIDDEN_CATEGORY_NAME, overwrites=overwrites, reason=reason
    )


class HiddenChannelsCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    hidden = app_commands.Group(
        name="hidden",
        description="Hide channels from everyone and restore them later (admin).",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    def _preflight(
        self, interaction: discord.Interaction
    ) -> tuple[discord.Guild, discord.Member] | str:
        """Shared admin + bot-permission gate. Returns (guild, me) or an error."""
        if not self.ctx.is_admin(interaction):
            return "❌ You need to be an admin to use this command."
        guild = interaction.guild
        if guild is None or guild.me is None:
            return "❌ This command can only be used in a server."
        perms = guild.me.guild_permissions
        if not (perms.manage_channels and perms.manage_roles):
            return (
                "❌ I need the **Manage Channels** and **Manage Roles** permissions "
                "to move channels and edit their permissions."
            )
        return guild, guild.me

    @hidden.command(name="hide", description="Hide a channel from everyone.")
    @app_commands.describe(channel="The channel to hide.")
    async def hide(
        self, interaction: discord.Interaction, channel: HideableChannel
    ) -> None:
        pre = self._preflight(interaction)
        if isinstance(pre, str):
            await interaction.response.send_message(pre, ephemeral=True)
            return
        guild, me = pre

        with self.ctx.open_db() as conn:
            existing = get_active_hidden(conn, guild.id, channel.id)
        if existing is not None:
            await interaction.response.send_message(
                f"❌ {channel.mention} is already hidden. Use `/hidden restore` to bring it back.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        original_parent_id = channel.category.id if channel.category else None
        original_position = channel.position
        stored = serialize_overwrites(channel.overwrites)
        reason = f"Hidden by {interaction.user} ({interaction.user.id})"

        try:
            hidden_cat = await _ensure_hidden_category(guild, reason)
            await channel.edit(
                category=hidden_cat,
                overwrites={
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    me: discord.PermissionOverwrite(view_channel=True),
                },
                reason=reason,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I'm not allowed to move or edit that channel — check my role's "
                "position and permissions.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            log.exception("Failed to hide channel %s", channel.id)
            await interaction.followup.send(
                "❌ Something went wrong talking to Discord. Please try again.",
                ephemeral=True,
            )
            return

        with self.ctx.open_db() as conn:
            create_hidden(
                conn,
                guild_id=guild.id,
                channel_id=channel.id,
                original_parent_id=original_parent_id,
                original_position=original_position,
                stored_overwrites=stored,
                hidden_by=interaction.user.id,
            )

        await interaction.followup.send(
            f"Hid **{channel.name}** under **{HIDDEN_CATEGORY_NAME}** and saved its "
            f"permissions. Use `/hidden restore` to put it back.",
            ephemeral=True,
        )

    @hidden.command(
        name="restore", description="Restore a hidden channel to where it was."
    )
    @app_commands.describe(channel="The hidden channel to restore.")
    async def restore(
        self, interaction: discord.Interaction, channel: HideableChannel
    ) -> None:
        pre = self._preflight(interaction)
        if isinstance(pre, str):
            await interaction.response.send_message(pre, ephemeral=True)
            return
        guild, _me = pre

        with self.ctx.open_db() as conn:
            row = get_active_hidden(conn, guild.id, channel.id)
        if row is None:
            await interaction.response.send_message(
                f"❌ {channel.mention} isn't currently hidden.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        stored = json.loads(row["stored_overwrites"])
        rebuilt = rebuild_overwrites(stored, guild)

        # Original category may have been deleted while the channel was hidden —
        # fall back to top-level (no category) in that case.
        parent: discord.CategoryChannel | None = None
        if row["original_parent_id"] is not None:
            candidate = guild.get_channel(row["original_parent_id"])
            if isinstance(candidate, discord.CategoryChannel):
                parent = candidate

        reason = f"Restored by {interaction.user} ({interaction.user.id})"
        try:
            await channel.edit(category=parent, overwrites=rebuilt, reason=reason)
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I'm not allowed to move or edit that channel — check my role's "
                "position and permissions.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            log.exception("Failed to restore channel %s", channel.id)
            await interaction.followup.send(
                "❌ Something went wrong talking to Discord. Please try again.",
                ephemeral=True,
            )
            return

        # Position is best-effort: Discord may reject a stale index, and a
        # misplaced-but-visible channel is a far better outcome than a failed
        # restore that leaves it hidden.
        try:
            await channel.edit(position=row["original_position"], reason=reason)
        except discord.HTTPException:
            log.warning("Could not restore position for channel %s", channel.id)

        with self.ctx.open_db() as conn:
            mark_restored(conn, row["id"])

        where = f"**{parent.name}**" if parent else "the top level"
        await interaction.followup.send(
            f"Restored **{channel.name}** to {where} with its original permissions.",
            ephemeral=True,
        )

    @hidden.command(name="list", description="List channels currently hidden.")
    async def list_hidden(self, interaction: discord.Interaction) -> None:
        pre = self._preflight(interaction)
        if isinstance(pre, str):
            await interaction.response.send_message(pre, ephemeral=True)
            return
        guild, _me = pre

        with self.ctx.open_db() as conn:
            rows = list_active_hidden(conn, guild.id)
        if not rows:
            await interaction.response.send_message(
                "No channels are currently hidden.", ephemeral=True
            )
            return

        lines = []
        for row in rows:
            ch = guild.get_channel(row["channel_id"])
            label = ch.mention if ch else f"(deleted channel {row['channel_id']})"
            lines.append(f"• {label} — hidden by <@{row['hidden_by']}>")
        await interaction.response.send_message(
            "**Hidden channels:**\n" + "\n".join(lines), ephemeral=True
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(HiddenChannelsCog(bot, bot.ctx))
