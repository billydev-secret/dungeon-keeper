"""AI-powered moderation commands backed by the Anthropic API.

Provides four slash commands (all mod-only, all ephemeral, under the /ai group):
  /ai review   — fetch a user's recent messages and have the AI flag concerns
  /ai scan     — scan the last N messages in the current channel
  /ai channel  — ask the AI a free-form question about a channel's recent activity
  /ai query    — ask the AI a free-form question about a user's message history

Requires ANTHROPIC_API_KEY to be set in the bot's environment.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

import discord
from anthropic import AsyncAnthropic
from discord import app_commands

from reports import send_ephemeral_text
from services.ai_moderation_service import (
    ai_query_channel,
    ai_query_user,
    ai_review_user,
    ai_scan_channel,
)

if TYPE_CHECKING:
    from app_context import AppContext, Bot


def _anthropic_client() -> AsyncAnthropic | None:
    """Return a configured AsyncAnthropic client, or None if ANTHROPIC_API_KEY is not set."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    return AsyncAnthropic(api_key=api_key) if api_key else None


def register_ai_mod_commands(bot: Bot, ctx: AppContext) -> None:
    ai_group = app_commands.Group(
        name="ai",
        description="AI-powered moderation tools (requires ANTHROPIC_API_KEY).",
    )

    @ai_group.command(
        name="review",
        description="AI review of a user's recent messages for rule violations or concerns.",
    )
    @app_commands.describe(
        user="The member to review.",
        days="How many days of message history to scan (default 7).",
    )
    async def ai_review(
        interaction: discord.Interaction,
        user: discord.Member,
        days: app_commands.Range[int, 1, 30] = 7,
    ):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        client = _anthropic_client()
        if client is None:
            await interaction.response.send_message(
                "ANTHROPIC_API_KEY is not set in the bot's environment.", ephemeral=True
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
            result = await ai_review_user(client, conn, guild, user, days=days)
        header = (
            f"**AI Review — {user.display_name}** "
            f"(last {days}d · {result.message_count} messages · "
            f"{result.channels_checked} channels)\n\n"
        )
        await send_ephemeral_text(interaction, header + result.analysis)

    @ai_group.command(
        name="scan",
        description="AI scan of recent messages in this channel for rule violations or concerns.",
    )
    @app_commands.describe(count="How many recent messages to scan (default 50).")
    async def ai_scan(
        interaction: discord.Interaction,
        count: app_commands.Range[int, 10, 200] = 50,
    ):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        client = _anthropic_client()
        if client is None:
            await interaction.response.send_message(
                "ANTHROPIC_API_KEY is not set in the bot's environment.", ephemeral=True
            )
            return

        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "This command only works in text channels and threads.", ephemeral=True
            )
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        with ctx.open_db() as conn:
            result = await ai_scan_channel(client, conn, guild, channel, count=count)
        header = f"**AI Channel Scan** — {result.message_count} messages reviewed\n\n"
        await send_ephemeral_text(interaction, header + result.analysis)

    @ai_group.command(
        name="channel",
        description="Ask the AI a question about recent activity in a channel.",
    )
    @app_commands.describe(
        question="Your question about the channel's recent activity.",
        minutes="How many minutes of history to include (default 60, max 1440).",
        channel="Channel to query — defaults to the current channel.",
    )
    async def ai_channel(
        interaction: discord.Interaction,
        question: str,
        minutes: app_commands.Range[int, 1, 1440] = 60,
        channel: discord.TextChannel | None = None,
    ):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        client = _anthropic_client()
        if client is None:
            await interaction.response.send_message(
                "ANTHROPIC_API_KEY is not set in the bot's environment.", ephemeral=True
            )
            return

        target = channel or interaction.channel
        if not isinstance(target, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "This command only works in text channels and threads.", ephemeral=True
            )
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        with ctx.open_db() as conn:
            result = await ai_query_channel(client, conn, guild, target, question, minutes=minutes)
        channel_name = getattr(target, "name", str(target.id))
        label = f"{minutes} minute{'s' if minutes != 1 else ''}"
        header = (
            f"**AI Channel Query — #{channel_name}** (last {label} · {result.message_count} messages)\n"
            f"**Q:** {question}\n\n"
        )
        await send_ephemeral_text(interaction, header + result.analysis)

    @ai_group.command(
        name="query",
        description="Ask the AI a specific question about a user based on their recent messages.",
    )
    @app_commands.describe(
        user="The member to investigate.",
        question="Your specific question or investigation prompt.",
        days="How many days of history to pull (default 14).",
    )
    async def ai_query(
        interaction: discord.Interaction,
        user: discord.Member,
        question: str,
        days: app_commands.Range[int, 1, 30] = 14,
    ):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        client = _anthropic_client()
        if client is None:
            await interaction.response.send_message(
                "ANTHROPIC_API_KEY is not set in the bot's environment.", ephemeral=True
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
            result = await ai_query_user(client, conn, guild, user, question, days=days)
        header = (
            f"**AI Query — {user.display_name}** "
            f"(last {days}d · {result.message_count} messages)\n"
            f"**Q:** {question}\n\n"
        )
        await send_ephemeral_text(interaction, header + result.analysis)

    bot.tree.add_command(ai_group)
