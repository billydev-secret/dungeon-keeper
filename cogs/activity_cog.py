"""Activity graph slash command.

Note: /dropoff, /session_burst, and /burst_ranking used to live here but
were moved to the web dashboard (see web/routes/reports.py). Only the
single-image /activity command remains in Discord — useful for quick mod
spot-checks without leaving the chat.
"""

from __future__ import annotations

import asyncio
import io
from typing import TYPE_CHECKING, Literal, cast

import discord
from discord import app_commands
from discord.ext import commands

from services.embeds import ACTIVITY_DANGER, ACTIVITY_PRIMARY
from services.activity_graphs import (
    _WINDOW_LABELS,
    query_message_activity,
    query_message_histogram,
    query_xp_activity_with_breakdown,
    query_xp_histogram_with_breakdown,
    render_activity_chart,
)
from services.health_metrics import compute_user_churn_score
from services.member_quality_score import compute_quality_scores

if TYPE_CHECKING:
    from core.app_context import AppContext, Bot


class ActivityCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    @app_commands.command(
        name="activity",
        description="Bar chart of messages or XP over time for the server, a member, or a channel.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        resolution="Bucket size: hour, day, week, month, hour_of_day, or day_of_week.",
        member="Scope to one member. Omit for server-wide.",
        channel="Scope to one channel.",
        mode="Chart messages or XP earned.",
    )
    async def activity(
        self,
        interaction: discord.Interaction,
        resolution: Literal[
            "hour", "day", "week", "month", "hour_of_day", "day_of_week"
        ] = "day",
        member: discord.User | None = None,
        channel: discord.TextChannel | None = None,
        mode: Literal["messages", "xp"] = "xp",
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
                "This command only works in a server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        utc_offset = ctx.tz_offset_hours
        window_label = _WINDOW_LABELS[resolution]
        if utc_offset:
            tz_label = f"UTC{utc_offset:+g}"
        else:
            tz_label = "UTC"

        mode_label = "XP" if mode == "xp" else "Activity"
        if member is not None and channel is not None:
            title = f"{member.display_name} in #{channel.name} — {mode_label} ({window_label}, {tz_label})"
        elif member is not None:
            title = f"{member.display_name} — {mode_label} ({window_label}, {tz_label})"
        elif channel is not None:
            title = f"#{channel.name} — {mode_label} ({window_label}, {tz_label})"
        else:
            title = f"{guild.name} — {mode_label} ({window_label}, {tz_label})"

        user_id = member.id if member is not None else None
        channel_id = channel.id if channel is not None else None

        if mode == "xp":
            def _query_xp():
                with ctx.open_db() as conn:
                    if resolution in ("hour_of_day", "day_of_week"):
                        _labels, _xp_totals, _by_source = query_xp_histogram_with_breakdown(
                            conn,
                            guild.id,
                            cast(Literal["hour_of_day", "day_of_week"], resolution),
                            user_id=user_id,
                            channel_id=channel_id,
                            utc_offset_hours=utc_offset,
                        )
                        return _labels, _xp_totals, [], False, _by_source
                    else:
                        (
                            _labels,
                            _xp_totals,
                            _member_counts,
                            _by_source,
                        ) = query_xp_activity_with_breakdown(
                            conn,
                            guild.id,
                            resolution,
                            user_id=user_id,
                            channel_id=channel_id,
                            utc_offset_hours=utc_offset,
                        )
                        return (
                            _labels,
                            _xp_totals,
                            _member_counts,
                            member is None and channel is None,
                            _by_source,
                        )

            (
                labels,
                counts,
                member_counts,
                show_members,
                by_source,
            ) = await asyncio.to_thread(_query_xp)
            y_label = "XP Earned"
            bar_label = "XP"
            empty_msg = f"No XP activity recorded for the {window_label.lower()}."
        else:
            def _query_activity():
                with ctx.open_db() as conn:
                    if resolution in ("hour_of_day", "day_of_week"):
                        _labels, _msg_counts = query_message_histogram(
                            conn,
                            guild.id,
                            cast(Literal["hour_of_day", "day_of_week"], resolution),
                            user_id=user_id,
                            channel_id=channel_id,
                            utc_offset_hours=utc_offset,
                        )
                        return _labels, _msg_counts, [], False
                    else:
                        _labels, _msg_counts, _member_counts = query_message_activity(
                            conn,
                            guild.id,
                            resolution,
                            user_id=user_id,
                            channel_id=channel_id,
                            utc_offset_hours=utc_offset,
                        )
                        return (
                            _labels,
                            _msg_counts,
                            _member_counts,
                            member is None and channel is None,
                        )

            labels, counts, member_counts, show_members = await asyncio.to_thread(_query_activity)
            by_source = {}
            y_label = "Messages"
            bar_label = "Messages"
            empty_msg = f"No message activity recorded for the {window_label.lower()}."

        if not any(c > 0 for c in counts):
            await interaction.followup.send(empty_msg, ephemeral=True)
            return

        chart_bytes = await asyncio.to_thread(
            render_activity_chart,
            labels,
            counts,
            member_counts,
            title=title,
            resolution=resolution,
            show_members=show_members,
            y_label=y_label,
            bar_label=bar_label,
            by_source=by_source if mode == "xp" else None,
        )

        chart_file = discord.File(io.BytesIO(chart_bytes), filename="activity.png")
        if member is not None:
            embed = await self._build_member_profile_embed(guild, member)
            await interaction.followup.send(
                file=chart_file,
                embed=embed,
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                file=chart_file,
                ephemeral=True,
            )

    @staticmethod
    def _quality_stars(score_unit: float) -> str:
        """Render a 0..1 score as a 5-star rating with half-star precision."""
        s = max(0.0, min(1.0, score_unit))
        # Round to nearest half-star
        halves = int(round(s * 10))  # 0..10
        full = halves // 2
        half = halves % 2
        empty = 5 - full - half
        return "★" * full + ("⯪" if half else "") + "☆" * empty

    async def _build_member_profile_embed(
        self,
        guild: discord.Guild,
        member: discord.User,
    ) -> discord.Embed:
        """Compose churn-risk + quality-score summary for one member."""
        ctx = self.ctx
        bot_guild = ctx.bot.get_guild(guild.id) if ctx.bot else guild
        members_seq = list(getattr(bot_guild, "members", []) or [])

        def _compute():
            with ctx.open_db() as conn:
                churn = compute_user_churn_score(conn, guild.id, member.id)
                qs_list = compute_quality_scores(conn, guild.id, members_seq)
                qs = next((q for q in qs_list if q.user_id == member.id), None)
                return churn, qs

        churn, qs = await asyncio.to_thread(_compute)

        tier_color = {
            "clear": ACTIVITY_PRIMARY,
            "watch": 0xF1C40F,        # yellow
            "declining": 0xE67E22,    # orange
            "critical": ACTIVITY_DANGER,
        }.get(churn["tier"], ACTIVITY_PRIMARY)

        embed = discord.Embed(
            title=f"{member.display_name} — Churn & Quality",
            color=tier_color,
        )
        if member.display_avatar:
            embed.set_thumbnail(url=member.display_avatar.url)

        # Churn block
        sig = churn["signals"]
        churn_lines = (
            f"**Score:** {churn['score']}/100 · **Tier:** {churn['tier']}\n"
            f"```\n"
            f"Frequency    {sig['frequency']:>3d}%\n"
            f"Channels     {sig['channels']:>3d}%\n"
            f"Reciprocity  {sig['reciprocity']:>3d}%\n"
            f"Sentiment    {sig['sentiment']:>3d}%\n"
            f"Visit gap    {sig['gap']:>3d}%\n"
            f"```"
        )
        embed.add_field(name="Churn risk", value=churn_lines, inline=False)

        # Quality block
        if qs is None or qs.status != "Active":
            status = qs.status if qs else "No data"
            embed.add_field(
                name="Quality score",
                value=f"_{status}_ — not enough activity in the last 90 days for a percentile-ranked score.",
                inline=False,
            )
        else:
            stars = self._quality_stars(qs.final_score)
            quality_lines = (
                f"**{stars}**  {qs.final_score * 100:.1f} / 100\n"
                f"```\n"
                f"Engagement   {qs.engagement_given * 100:>3.0f}% (40%)\n"
                f"Consistency  {qs.consistency_recency * 100:>3.0f}% (25%)\n"
                f"Resonance    {qs.content_resonance * 100:>3.0f}% (20%)\n"
                f"Posting      {qs.posting_activity * 100:>3.0f}% (15%)\n"
                f"```"
                f"Active days: {qs.active_days} · Active weeks: {qs.active_weeks}"
            )
            embed.add_field(name="Quality score", value=quality_lines, inline=False)

        return embed


async def setup(bot: Bot) -> None:
    await bot.add_cog(ActivityCog(bot, bot.ctx))
