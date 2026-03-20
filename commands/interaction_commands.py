"""Interaction graph slash commands.

Commands:
  /connection_web    — render the server's reply/mention interaction network
  /interaction_scan  — backfill interaction history from message history
"""
from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from services.interaction_graph import (
    query_connection_web,
    record_interactions,
    render_connection_web,
)

if TYPE_CHECKING:
    from app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.interaction_commands")


def register_interaction_commands(bot: "Bot", ctx: "AppContext") -> None:

    @bot.tree.command(
        name="connection_web",
        description="Show the web of replies and mentions between server members.",
    )
    @app_commands.describe(
        min_interactions="Only show pairs with at least this many interactions (default 3).",
        limit="Max number of members to include (default 40).",
    )
    async def connection_web(
        interaction: discord.Interaction,
        min_interactions: app_commands.Range[int, 1, 500] = 3,
        limit: app_commands.Range[int, 5, 60] = 40,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        with ctx.open_db() as conn:
            edges = query_connection_web(
                conn, guild.id,
                min_weight=min_interactions,
                limit_users=limit,
            )

        if not edges:
            await interaction.followup.send(
                f"No interaction data found with at least **{min_interactions}** interactions. "
                "Use `/interaction_scan` to backfill history, or lower `min_interactions`.",
                ephemeral=True,
            )
            return

        # Resolve display names for every node in the edge list
        node_ids = {uid for u, v, _ in edges for uid in (u, v)}
        name_map: dict[int, str] = {}
        for uid in node_ids:
            member = guild.get_member(uid)
            name_map[uid] = member.display_name if member else f"User {uid}"

        chart_bytes = render_connection_web(edges, name_map, guild_name=guild.name)
        await interaction.followup.send(
            file=discord.File(io.BytesIO(chart_bytes), filename="connection_web.png"),
            ephemeral=True,
        )

    @bot.tree.command(
        name="interaction_scan",
        description="Scan message history to build the reply/mention interaction graph.",
    )
    @app_commands.describe(
        days="How many days back to scan. Use 0 for all available history.",
    )
    async def interaction_scan(
        interaction: discord.Interaction,
        days: app_commands.Range[int, 0, 3650] = 0,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        from datetime import datetime, timedelta, timezone
        now_dt = datetime.now(timezone.utc)
        after_dt = None if days == 0 else now_dt - timedelta(days=days)

        me = guild.get_member(bot.user.id) if bot.user else None

        # Collect all readable text channels and their active threads
        channels: list[discord.TextChannel | discord.Thread] = []
        seen_ids: set[int] = set()

        for channel in guild.text_channels:
            if me and not channel.permissions_for(me).read_message_history:
                continue
            if channel.id not in seen_ids:
                channels.append(channel)
                seen_ids.add(channel.id)
            try:
                async for thread in channel.archived_threads(limit=None):
                    if thread.id not in seen_ids:
                        channels.append(thread)
                        seen_ids.add(thread.id)
            except (discord.Forbidden, discord.HTTPException):
                pass
            for thread in channel.threads:
                if thread.id not in seen_ids:
                    channels.append(thread)
                    seen_ids.add(thread.id)

        stats = {"channels": 0, "messages": 0, "interactions": 0}

        with ctx.open_db() as conn:
            for ch in channels:
                if me and not ch.permissions_for(me).read_message_history:
                    continue
                stats["channels"] += 1
                try:
                    async for message in ch.history(
                        limit=None, after=after_dt, oldest_first=True
                    ):
                        if message.author.bot or not message.guild:
                            continue

                        stats["messages"] += 1
                        targets: list[int] = []

                        # Reply target — use pre-resolved ref only (no per-message fetches)
                        if message.reference and isinstance(
                            message.reference.resolved, discord.Message
                        ):
                            ref = message.reference.resolved
                            if not ref.author.bot and ref.author.id != message.author.id:
                                targets.append(ref.author.id)

                        # Explicit @mentions
                        for user in message.mentions:
                            if (
                                not user.bot
                                and user.id != message.author.id
                                and user.id not in targets
                            ):
                                targets.append(user.id)

                        if targets:
                            record_interactions(conn, guild.id, message.author.id, targets)
                            stats["interactions"] += len(targets)

                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.warning("Could not scan channel %s: %s", ch.id, exc)

        window_label = "all available history" if days == 0 else f"last {days} day{'s' if days != 1 else ''}"
        await interaction.followup.send(
            f"Interaction scan complete for {window_label}.\n"
            f"Channels scanned: **{stats['channels']}**\n"
            f"Messages read: **{stats['messages']}**\n"
            f"Interactions recorded: **{stats['interactions']}**",
            ephemeral=True,
        )
