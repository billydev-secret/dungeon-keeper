"""AI moderation commands (backed by a local Ollama instance)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.reports import send_ephemeral_text
from bot_modules.services import ollama_client
from bot_modules.services.ai_moderation_service import (
    ai_query_channel,
    ai_query_user,
    ai_review_user,
    ai_scan_channel,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot


def _ollama_unavailable_msg() -> str:
    return "OLLAMA_BASE_URL is not set — AI features require a local Ollama instance."


class AiModCog(commands.Cog):
    ai = app_commands.Group(
        name="ai",
        description="AI-powered moderation — review members, scan channels, ask questions.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    @ai.command(
        name="review",
        description="AI flags rule violations and concerning patterns in a member's recent messages.",
    )
    @app_commands.describe(
        user="Member to review.",
        days="Days of history to scan.",
    )
    async def ai_review(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        days: app_commands.Range[int, 1, 30] = 7,
    ) -> None:
        ctx = self.ctx
        if not ctx.is_admin(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        if not ollama_client.is_available():
            await interaction.response.send_message(_ollama_unavailable_msg(), ephemeral=True)
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        with ctx.open_db() as conn:
            result = await ai_review_user(conn, guild, user, days=days)
        header = (
            f"**AI Review — {user.display_name}** "
            f"(last {days}d · {result.message_count} messages · "
            f"{result.channels_checked} channels)\n\n"
        )
        await send_ephemeral_text(interaction, header + result.analysis)

    @ai.command(
        name="scan",
        description="AI scans the last N messages in this channel for rule violations.",
    )
    @app_commands.describe(count="Number of recent messages to scan.")
    async def ai_scan(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, 10, 200] = 50,
    ) -> None:
        ctx = self.ctx
        if not ctx.is_admin(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        if not ollama_client.is_available():
            await interaction.response.send_message(_ollama_unavailable_msg(), ephemeral=True)
            return

        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "This command only works in text channels and threads.", ephemeral=True
            )
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        with ctx.open_db() as conn:
            result = await ai_scan_channel(conn, guild, channel, count=count)
        header = f"**AI Channel Scan** — {result.message_count} messages reviewed\n\n"
        await send_ephemeral_text(interaction, header + result.analysis)

    @ai.command(
        name="channel",
        description="Ask the AI a free-form question about what happened in a channel.",
    )
    @app_commands.describe(
        question="What do you want to know?",
        minutes="Minutes of history to include (up to 1440 = 24h).",
        channel="Channel to query. Defaults to this one.",
    )
    async def ai_channel(
        self,
        interaction: discord.Interaction,
        question: str,
        minutes: app_commands.Range[int, 1, 1440] = 60,
        channel: discord.TextChannel | None = None,
    ) -> None:
        ctx = self.ctx
        if not ctx.is_admin(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        if not ollama_client.is_available():
            await interaction.response.send_message(_ollama_unavailable_msg(), ephemeral=True)
            return

        target = channel or interaction.channel
        if not isinstance(target, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "This command only works in text channels and threads.", ephemeral=True
            )
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        with ctx.open_db() as conn:
            result = await ai_query_channel(conn, guild, target, question, minutes=minutes)
        channel_name = getattr(target, "name", str(target.id))
        label = f"{minutes} minute{'s' if minutes != 1 else ''}"
        header = (
            f"**AI Channel Query — #{channel_name}** (last {label} · {result.message_count} messages)\n"
            f"**Q:** {question}\n\n"
        )
        await send_ephemeral_text(interaction, header + result.analysis)

    @ai.command(
        name="query",
        description="Ask the AI a specific question about a member's message history.",
    )
    @app_commands.describe(
        user="Member to investigate.",
        question="What do you want to know about this member?",
        days="Days of history to include.",
    )
    async def ai_query(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        question: str,
        days: app_commands.Range[int, 1, 30] = 14,
    ) -> None:
        ctx = self.ctx
        if not ctx.is_admin(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        if not ollama_client.is_available():
            await interaction.response.send_message(_ollama_unavailable_msg(), ephemeral=True)
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        with ctx.open_db() as conn:
            result = await ai_query_user(conn, guild, user, question, days=days)
        header = (
            f"**AI Query — {user.display_name}** "
            f"(last {days}d · {result.message_count} messages)\n"
            f"**Q:** {question}\n\n"
        )
        await send_ephemeral_text(interaction, header + result.analysis)


async def setup(bot: Bot) -> None:
    await bot.add_cog(AiModCog(bot, bot.ctx))
