"""Activity graph slash commands."""
from __future__ import annotations

import io
from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands

from services.activity_graphs import (
    _WINDOW_LABELS,
    query_message_activity,
    render_activity_chart,
)

if TYPE_CHECKING:
    from app_context import AppContext, Bot


def register_activity_commands(bot: "Bot", ctx: "AppContext") -> None:
    @bot.tree.command(
        name="activity",
        description="Show a message activity chart for the server or a specific member.",
    )
    @app_commands.describe(
        resolution="Time resolution for the chart buckets.",
        member="Show activity for this member only (default: whole server).",
    )
    async def activity(
        interaction: discord.Interaction,
        resolution: Literal["hour", "day", "week", "month"] = "day",
        member: discord.Member | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        window_label = _WINDOW_LABELS[resolution]

        if member is not None:
            title = f"{member.display_name} — Activity ({window_label})"
        else:
            title = f"{guild.name} — Activity ({window_label})"

        with ctx.open_db() as conn:
            labels, msg_counts, member_counts = query_message_activity(
                conn,
                guild.id,
                resolution,
                user_id=member.id if member is not None else None,
            )

        if not any(c > 0 for c in msg_counts):
            await interaction.followup.send(
                f"No message activity recorded for the {window_label.lower()}.",
                ephemeral=True,
            )
            return

        chart_bytes = render_activity_chart(
            labels,
            msg_counts,
            member_counts,
            title=title,
            resolution=resolution,
            show_members=(member is None),
        )

        await interaction.followup.send(
            file=discord.File(io.BytesIO(chart_bytes), filename="activity.png"),
            ephemeral=True,
        )
