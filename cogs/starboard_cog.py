"""Starboard cog — reposts starred messages to a dedicated channel."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from db_utils import add_config_id, get_config_id_set, remove_config_id
from services.starboard_service import (
    add_reactor,
    get_effective_star_count,
    get_starboard_config,
    get_starboard_post,
    insert_starboard_post,
    remove_reactor,
    update_starboard_post_count,
    upsert_starboard_config,
)

if TYPE_CHECKING:
    from app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.starboard")

_EXCLUDED_BUCKET = "starboard_excluded_channels"


# ---------------------------------------------------------------------------
# Embed helpers
# ---------------------------------------------------------------------------


def _build_embed(message: discord.Message, star_count: int, emoji: str) -> discord.Embed:
    embed = discord.Embed(
        description=message.content[:2000] if message.content else None,
        color=discord.Color.gold(),
        timestamp=message.created_at,
    )
    channel_name = getattr(message.channel, "name", str(message.channel.id))
    embed.set_author(
        name=f"{message.author.display_name} in #{channel_name}",
        icon_url=message.author.display_avatar.url,
    )
    embed.add_field(name="Original", value=f"[Jump to message]({message.jump_url})", inline=False)
    embed.set_footer(text=f"{emoji} {star_count}")

    for attachment in message.attachments:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            embed.set_image(url=attachment.url)
            break

    return embed


def _updated_embed(old_embed: discord.Embed, star_count: int, emoji: str) -> discord.Embed:
    new_embed = old_embed.copy()
    new_embed.set_footer(text=f"{emoji} {star_count}")
    return new_embed


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class StarboardCog(commands.Cog):
    starboard = app_commands.Group(
        name="starboard",
        description="Starboard settings and management.",
    )

    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    async def _fetch_sb_message(self, channel_id: int, message_id: int) -> Optional[discord.Message]:
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return None
        try:
            return await channel.fetch_message(message_id)
        except discord.NotFound:
            return None
        except discord.HTTPException:
            log.warning("starboard: failed to fetch starboard message %s", message_id)
            return None

    def _default_cfg(self, conn, guild_id: int) -> dict:
        row = get_starboard_config(conn, guild_id)
        if row:
            return {
                "channel_id": row["channel_id"],
                "threshold": row["threshold"],
                "emoji": row["emoji"],
                "enabled": row["enabled"],
            }
        return {"channel_id": 0, "threshold": 3, "emoji": "⭐", "enabled": 1}

    # ------------------------------------------------------------------
    # Reaction events
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None or payload.guild_id != self.ctx.guild_id:
            return

        guild_id = payload.guild_id
        message_id = payload.message_id

        with self.ctx.open_db() as conn:
            cfg = get_starboard_config(conn, guild_id)
            if cfg is None or not cfg["enabled"] or not cfg["channel_id"]:
                return
            emoji: str = cfg["emoji"]
            threshold: int = cfg["threshold"]
            sb_channel_id: int = cfg["channel_id"]

            if str(payload.emoji) != emoji:
                return
            if payload.channel_id == sb_channel_id:
                return
            if payload.channel_id in get_config_id_set(conn, _EXCLUDED_BUCKET, guild_id):
                return

            add_reactor(conn, guild_id, message_id, payload.user_id)
            existing_post = get_starboard_post(conn, guild_id, message_id)

        # Existing post: just update the count, no need to fetch the original message.
        if existing_post is not None:
            author_id = int(existing_post["author_id"])
            sb_message_id = int(existing_post["starboard_message_id"])
            with self.ctx.open_db() as conn:
                effective_count = get_effective_star_count(conn, guild_id, message_id, author_id)
                update_starboard_post_count(conn, guild_id, message_id, effective_count)
            sb_msg = await self._fetch_sb_message(sb_channel_id, sb_message_id)
            if sb_msg and sb_msg.embeds:
                await sb_msg.edit(
                    embed=_updated_embed(sb_msg.embeds[0], effective_count, emoji),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            return

        # No existing post — fetch original to get author and content.
        orig_channel = self.bot.get_channel(payload.channel_id)
        if not isinstance(
            orig_channel,
            (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.Thread),
        ):
            return
        try:
            message = await orig_channel.fetch_message(message_id)
        except (discord.NotFound, discord.HTTPException):
            return

        author_id = message.author.id

        with self.ctx.open_db() as conn:
            effective_count = get_effective_star_count(conn, guild_id, message_id, author_id)
            if effective_count < threshold:
                return
            # Guard against a concurrent reaction creating the post between our checks.
            if get_starboard_post(conn, guild_id, message_id) is not None:
                return

        sb_channel = self.bot.get_channel(sb_channel_id)
        if not isinstance(sb_channel, discord.TextChannel):
            return

        embed = _build_embed(message, effective_count, emoji)
        try:
            sb_msg = await sb_channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("starboard: failed to send starboard post for message %s", message_id)
            return

        with self.ctx.open_db() as conn:
            insert_starboard_post(
                conn, guild_id, message_id, sb_msg.id,
                payload.channel_id, author_id, effective_count,
            )

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None or payload.guild_id != self.ctx.guild_id:
            return

        guild_id = payload.guild_id
        message_id = payload.message_id

        with self.ctx.open_db() as conn:
            cfg = get_starboard_config(conn, guild_id)
            if cfg is None or not cfg["enabled"] or not cfg["channel_id"]:
                return
            emoji: str = cfg["emoji"]
            sb_channel_id: int = cfg["channel_id"]

            if str(payload.emoji) != emoji:
                return
            if payload.channel_id == sb_channel_id:
                return
            if payload.channel_id in get_config_id_set(conn, _EXCLUDED_BUCKET, guild_id):
                return

            remove_reactor(conn, guild_id, message_id, payload.user_id)
            existing_post = get_starboard_post(conn, guild_id, message_id)
            if existing_post is None:
                return

            author_id = int(existing_post["author_id"])
            sb_message_id = int(existing_post["starboard_message_id"])
            effective_count = get_effective_star_count(conn, guild_id, message_id, author_id)
            update_starboard_post_count(conn, guild_id, message_id, effective_count)

        sb_msg = await self._fetch_sb_message(sb_channel_id, sb_message_id)
        if sb_msg and sb_msg.embeds:
            await sb_msg.edit(
                embed=_updated_embed(sb_msg.embeds[0], effective_count, emoji),
                allowed_mentions=discord.AllowedMentions.none(),
            )

    # ------------------------------------------------------------------
    # Config commands
    # ------------------------------------------------------------------

    @starboard.command(name="channel", description="Set the starboard channel.")
    @app_commands.describe(channel="Channel where starred messages will be posted.")
    async def sb_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        if not self.ctx.is_mod(interaction):
            await interaction.response.send_message("Mod only.", ephemeral=True)
            return
        guild_id = interaction.guild_id
        if guild_id is None:
            return
        with self.ctx.open_db() as conn:
            cfg = self._default_cfg(conn, guild_id)
            cfg["channel_id"] = channel.id
            upsert_starboard_config(conn, guild_id, **cfg)
        await interaction.response.send_message(
            f"Starboard channel set to {channel.mention}.", ephemeral=True
        )

    @starboard.command(name="threshold", description="Set the minimum star count to post.")
    @app_commands.describe(count="Number of stars required (minimum 1).")
    async def sb_threshold(self, interaction: discord.Interaction, count: int) -> None:
        if not self.ctx.is_mod(interaction):
            await interaction.response.send_message("Mod only.", ephemeral=True)
            return
        if count < 1:
            await interaction.response.send_message("Threshold must be at least 1.", ephemeral=True)
            return
        guild_id = interaction.guild_id
        if guild_id is None:
            return
        with self.ctx.open_db() as conn:
            cfg = self._default_cfg(conn, guild_id)
            cfg["threshold"] = count
            upsert_starboard_config(conn, guild_id, **cfg)
        await interaction.response.send_message(
            f"Starboard threshold set to **{count}**.", ephemeral=True
        )

    @starboard.command(name="emoji", description="Set the reaction emoji that triggers the starboard.")
    @app_commands.describe(emoji="Emoji to watch for (e.g. ⭐).")
    async def sb_emoji(self, interaction: discord.Interaction, emoji: str) -> None:
        if not self.ctx.is_mod(interaction):
            await interaction.response.send_message("Mod only.", ephemeral=True)
            return
        emoji = emoji.strip()
        if not emoji:
            await interaction.response.send_message("Emoji cannot be empty.", ephemeral=True)
            return
        guild_id = interaction.guild_id
        if guild_id is None:
            return
        with self.ctx.open_db() as conn:
            cfg = self._default_cfg(conn, guild_id)
            cfg["emoji"] = emoji
            upsert_starboard_config(conn, guild_id, **cfg)
        await interaction.response.send_message(f"Starboard emoji set to **{emoji}**.", ephemeral=True)

    @starboard.command(name="toggle", description="Enable or disable the starboard.")
    async def sb_toggle(self, interaction: discord.Interaction) -> None:
        if not self.ctx.is_mod(interaction):
            await interaction.response.send_message("Mod only.", ephemeral=True)
            return
        guild_id = interaction.guild_id
        if guild_id is None:
            return
        with self.ctx.open_db() as conn:
            cfg = self._default_cfg(conn, guild_id)
            cfg["enabled"] = 0 if cfg["enabled"] else 1
            upsert_starboard_config(conn, guild_id, **cfg)
        state = "enabled" if cfg["enabled"] else "disabled"
        await interaction.response.send_message(f"Starboard **{state}**.", ephemeral=True)

    @starboard.command(name="exclude", description="Exclude a channel from the starboard.")
    @app_commands.describe(channel="Channel to exclude.")
    async def sb_exclude(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        if not self.ctx.is_mod(interaction):
            await interaction.response.send_message("Mod only.", ephemeral=True)
            return
        guild_id = interaction.guild_id
        if guild_id is None:
            return
        with self.ctx.open_db() as conn:
            add_config_id(conn, _EXCLUDED_BUCKET, channel.id, guild_id)
        await interaction.response.send_message(
            f"{channel.mention} excluded from the starboard.", ephemeral=True
        )

    @starboard.command(name="unexclude", description="Remove a channel from the exclusion list.")
    @app_commands.describe(channel="Channel to unexclude.")
    async def sb_unexclude(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        if not self.ctx.is_mod(interaction):
            await interaction.response.send_message("Mod only.", ephemeral=True)
            return
        guild_id = interaction.guild_id
        if guild_id is None:
            return
        with self.ctx.open_db() as conn:
            remove_config_id(conn, _EXCLUDED_BUCKET, channel.id, guild_id)
        await interaction.response.send_message(
            f"{channel.mention} removed from exclusion list.", ephemeral=True
        )

    @starboard.command(name="status", description="Show the current starboard configuration.")
    async def sb_status(self, interaction: discord.Interaction) -> None:
        if not self.ctx.is_mod(interaction):
            await interaction.response.send_message("Mod only.", ephemeral=True)
            return
        guild_id = interaction.guild_id
        if guild_id is None:
            return

        with self.ctx.open_db() as conn:
            cfg = self._default_cfg(conn, guild_id)
            excluded_ids = get_config_id_set(conn, _EXCLUDED_BUCKET, guild_id)

        channel_mention = f"<#{cfg['channel_id']}>" if cfg["channel_id"] else "*not set*"
        state = "enabled" if cfg["enabled"] else "disabled"
        excluded_text = (
            " ".join(f"<#{cid}>" for cid in sorted(excluded_ids)) if excluded_ids else "*none*"
        )

        embed = discord.Embed(title="Starboard Configuration", color=discord.Color.gold())
        embed.add_field(name="Status", value=state, inline=True)
        embed.add_field(name="Channel", value=channel_mention, inline=True)
        embed.add_field(name="Threshold", value=str(cfg["threshold"]), inline=True)
        embed.add_field(name="Emoji", value=cfg["emoji"], inline=True)
        embed.add_field(name="Excluded channels", value=excluded_text, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: Bot) -> None:
    await bot.add_cog(StarboardCog(bot, bot.ctx))
