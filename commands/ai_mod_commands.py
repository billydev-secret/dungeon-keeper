"""AI-powered moderation commands backed by the OpenAI API.

Provides four slash commands (all mod-only, all ephemeral):
  /ai_review        — fetch a user's recent messages and have the AI flag concerns
  /ai_scan          — scan the last N messages in the current channel
  /ai_channel_query — ask the AI a free-form question about a channel's recent activity
  /ai_query         — ask the AI a free-form question about a user's message history

Requires OPENAI_API_KEY to be set in the bot's environment.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from openai import AsyncOpenAI

from reports import send_ephemeral_text
from services.ai_moderation_service import (
    ai_query_channel,
    ai_query_user,
    ai_review_user,
    ai_scan_channel,
)

if TYPE_CHECKING:
    from app_context import AppContext, Bot


def _openai_client() -> AsyncOpenAI | None:
    """Return a configured AsyncOpenAI client, or None if OPENAI_API_KEY is not set."""
    api_key = os.getenv("OPENAI_API_KEY")
    return AsyncOpenAI(api_key=api_key) if api_key else None


def register_ai_mod_commands(bot: Bot, ctx: AppContext) -> None:
    @bot.tree.command(
        name="ai_review",
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
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not ctx.is_mod(interaction):
            await interaction.followup.send(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        client = _openai_client()
        if client is None:
            await interaction.followup.send(
                "OPENAI_API_KEY is not set in the bot's environment.", ephemeral=True
            )
            return

        guild = interaction.guild
        if not guild:
            await interaction.followup.send(
                "This command only works in a server.", ephemeral=True
            )
            return

        result = await ai_review_user(client, guild, user, days=days)
        header = (
            f"**AI Review — {user.display_name}** "
            f"(last {days}d · {result.message_count} messages · "
            f"{result.channels_checked} channels scanned)\n\n"
        )
        await send_ephemeral_text(interaction, header + result.analysis)

    @bot.tree.command(
        name="ai_scan",
        description="AI scan of recent messages in this channel for rule violations or concerns.",
    )
    @app_commands.describe(count="How many recent messages to scan (default 50).")
    async def ai_scan(
        interaction: discord.Interaction,
        count: app_commands.Range[int, 10, 200] = 50,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not ctx.is_mod(interaction):
            await interaction.followup.send(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        client = _openai_client()
        if client is None:
            await interaction.followup.send(
                "OPENAI_API_KEY is not set in the bot's environment.", ephemeral=True
            )
            return

        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.followup.send(
                "This command only works in text channels and threads.", ephemeral=True
            )
            return

        result = await ai_scan_channel(client, channel, count=count)
        header = f"**AI Channel Scan** — {result.message_count} messages reviewed\n\n"
        await send_ephemeral_text(interaction, header + result.analysis)

    @bot.tree.command(
        name="ai_channel_query",
        description="Ask the AI a question about recent activity in a channel.",
    )
    @app_commands.describe(
        question="Your question about the channel's recent activity.",
        minutes="How many minutes of history to include (default 60, max 1440).",
        channel="Channel to query — defaults to the current channel.",
    )
    async def ai_channel_query(
        interaction: discord.Interaction,
        question: str,
        minutes: app_commands.Range[int, 1, 1440] = 60,
        channel: discord.TextChannel | None = None,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not ctx.is_mod(interaction):
            await interaction.followup.send(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        client = _openai_client()
        if client is None:
            await interaction.followup.send(
                "OPENAI_API_KEY is not set in the bot's environment.", ephemeral=True
            )
            return

        target = channel or interaction.channel
        if not isinstance(target, (discord.TextChannel, discord.Thread)):
            await interaction.followup.send(
                "This command only works in text channels and threads.", ephemeral=True
            )
            return

        result = await ai_query_channel(client, target, question, minutes=minutes)
        channel_name = getattr(target, "name", str(target.id))
        label = f"{minutes} minute{'s' if minutes != 1 else ''}"
        header = (
            f"**AI Channel Query — #{channel_name}** (last {label} · {result.message_count} messages)\n"
            f"**Q:** {question}\n\n"
        )
        await send_ephemeral_text(interaction, header + result.analysis)

    @bot.tree.command(
        name="ai_query",
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
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not ctx.is_mod(interaction):
            await interaction.followup.send(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        client = _openai_client()
        if client is None:
            await interaction.followup.send(
                "OPENAI_API_KEY is not set in the bot's environment.", ephemeral=True
            )
            return

        guild = interaction.guild
        if not guild:
            await interaction.followup.send(
                "This command only works in a server.", ephemeral=True
            )
            return

        result = await ai_query_user(client, guild, user, question, days=days)
        header = (
            f"**AI Query — {user.display_name}** "
            f"(last {days}d · {result.message_count} messages)\n"
            f"**Q:** {question}\n\n"
        )
        await send_ephemeral_text(interaction, header + result.analysis)
