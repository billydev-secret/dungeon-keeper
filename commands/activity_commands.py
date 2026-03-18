"""Activity graph slash commands."""
from __future__ import annotations

import io
from typing import TYPE_CHECKING, Literal, cast

import discord
from discord import app_commands

from services.activity_graphs import (
    _WINDOW_LABELS,
    query_message_activity,
    query_message_histogram,
    query_message_rate_drops,
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
        channel="Filter activity to a specific channel.",
    )
    async def activity(
        interaction: discord.Interaction,
        resolution: Literal["hour", "day", "week", "month", "hour_of_day", "day_of_week"] = "day",
        member: discord.Member | None = None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        window_label = _WINDOW_LABELS[resolution]

        if member is not None and channel is not None:
            title = f"{member.display_name} in #{channel.name} — Activity ({window_label})"
        elif member is not None:
            title = f"{member.display_name} — Activity ({window_label})"
        elif channel is not None:
            title = f"#{channel.name} — Activity ({window_label})"
        else:
            title = f"{guild.name} — Activity ({window_label})"

        user_id = member.id if member is not None else None
        channel_id = channel.id if channel is not None else None
        with ctx.open_db() as conn:
            if resolution in ("hour_of_day", "day_of_week"):
                labels, msg_counts = query_message_histogram(
                    conn, guild.id, cast(Literal["hour_of_day", "day_of_week"], resolution),
                    user_id=user_id, channel_id=channel_id,
                )
                member_counts: list[int] = []
                show_members = False
            else:
                labels, msg_counts, member_counts = query_message_activity(
                    conn, guild.id, resolution, user_id=user_id, channel_id=channel_id,
                )
                show_members = member is None and channel is None

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
            show_members=show_members,
        )

        await interaction.followup.send(
            file=discord.File(io.BytesIO(chart_bytes), filename="activity.png"),
            ephemeral=True,
        )

    _DROPOFF_PERIOD_SECONDS: dict[str, float] = {
        "day": 24 * 60 * 60,
        "week": 7 * 24 * 60 * 60,
        "month": 30 * 24 * 60 * 60,
    }

    @bot.tree.command(
        name="dropoff",
        description="Show members with the largest drop in message rate between two equal time windows.",
    )
    @app_commands.describe(
        period="Length of each comparison window.",
        limit="Number of members to show (1–25, default 10).",
        channel="Restrict comparison to a specific channel.",
    )
    async def dropoff(
        interaction: discord.Interaction,
        period: Literal["day", "week", "month"] = "week",
        limit: app_commands.Range[int, 1, 25] = 10,
        channel: discord.TextChannel | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        period_secs = _DROPOFF_PERIOD_SECONDS[period]
        period_label = period  # "day" / "week" / "month"
        channel_id = channel.id if channel is not None else None

        with ctx.open_db() as conn:
            results = query_message_rate_drops(
                conn, guild.id, period_secs, channel_id=channel_id, limit=limit
            )

        if not results:
            suffix = f" in #{channel.name}" if channel else ""
            await interaction.followup.send(
                f"No significant message rate drops found{suffix} "
                f"comparing the last {period_label} to the prior {period_label}.",
                ephemeral=True,
            )
            return

        lines: list[str] = []
        for rank, (user_id, prev, recent) in enumerate(results, start=1):
            member = guild.get_member(user_id)
            name = member.mention if member else f"<@{user_id}>"
            drop = prev - recent
            pct = round((drop / prev) * 100)
            lines.append(f"`{rank:>2}.` {name}\n`{prev} → {recent}` (**\u2212{drop}**, \u2212{pct}%)")

        if channel:
            title = f"Message Rate Dropoff in #{channel.name}"
            description = f"Prior {period_label} vs most recent {period_label}"
        else:
            title = f"Message Rate Dropoff — {guild.name}"
            description = f"Prior {period_label} vs most recent {period_label}"

        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.red(),
        )
        embed.add_field(name="Members", value="\n".join(lines), inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)
