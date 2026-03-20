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
        member="Focus on this member's direct connections only.",
        min_pct="Hide edges that are less than this % of either user's total interactions (default 5).",
        limit="Max number of members to include in server-wide view (default 40).",
        spread="How spread out the graph is — higher = more space between nodes (default 1.0).",
    )
    async def connection_web(
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        min_pct: app_commands.Range[int, 1, 100] = 5,
        limit: app_commands.Range[int, 5, 60] = 40,
        spread: app_commands.Range[float, 0.5, 5.0] = 1.0,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        with ctx.open_db() as conn:
            all_edges = query_connection_web(
                conn, guild.id,
                min_weight=1,
                limit_users=limit,
            )

        # Total interaction weight per node — used for percentage filtering
        node_total: dict[int, int] = {}
        for u, v, w in all_edges:
            node_total[u] = node_total.get(u, 0) + w
            node_total[v] = node_total.get(v, 0) + w

        threshold = min_pct / 100.0

        def _pct_passes(u: int, v: int, w: int) -> bool:
            """Keep edge if it is >= min_pct% of the less-active endpoint's total."""
            denom = min(node_total.get(u, 1), node_total.get(v, 1))
            return w >= threshold * denom

        second_level_ids: set[int] | None = None

        if member is not None:
            # 1st level: edges directly involving the focused member that pass threshold
            first_level_edges = [
                (u, v, w) for u, v, w in all_edges
                if (u == member.id or v == member.id) and _pct_passes(u, v, w)
            ]
            direct_ids: set[int] = {
                (v if u == member.id else u)
                for u, v, _ in first_level_edges
            }
            # 2nd level: edges from direct connections to their other neighbours
            second_level_edges = [
                (u, v, w) for u, v, w in all_edges
                if u != member.id and v != member.id
                and (u in direct_ids or v in direct_ids)
                and _pct_passes(u, v, w)
            ]
            second_level_ids = {
                uid
                for u, v, _ in second_level_edges
                for uid in (u, v)
                if uid not in direct_ids and uid != member.id
            }
            edges = first_level_edges + second_level_edges
            no_data_msg = (
                f"{member.mention} has no connections that meet the **{min_pct}%** threshold. "
                "Try lowering `min_pct` or running `/interaction_scan`."
            )
        else:
            edges = [(u, v, w) for u, v, w in all_edges if _pct_passes(u, v, w)]
            no_data_msg = (
                f"No edges found where a connection accounts for ≥**{min_pct}%** "
                "of either user's total interactions. "
                "Try lowering `min_pct` or running `/interaction_scan`."
            )

        if not edges:
            await interaction.followup.send(no_data_msg, ephemeral=True)
            return

        # Resolve display names for every node in the edge list
        node_ids = {uid for u, v, _ in edges for uid in (u, v)}
        name_map: dict[int, str] = {}
        for uid in node_ids:
            m = guild.get_member(uid)
            name_map[uid] = m.display_name if m else f"User {uid}"

        chart_bytes = render_connection_web(
            edges,
            name_map,
            guild_name=guild.name,
            focus_user_id=member.id if member else None,
            second_level_ids=second_level_ids,
            spread=spread,
        )
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
