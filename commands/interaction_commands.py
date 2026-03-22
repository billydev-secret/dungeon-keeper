"""Interaction graph slash commands.

Commands:
  /connection_web    — render the server's reply/mention interaction network
  /interaction_scan  — backfill interaction history from message history
"""
from __future__ import annotations

import asyncio
import functools
import io
import logging
import time as _time
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from services.interaction_graph import (
    clear_interaction_data,
    query_connection_web,
    record_interactions,
    render_connection_web,
)
from services.message_store import set_reaction_count, store_message

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
        timescale="Time window to consider (default: all time).",
        min_pct="Hide edges that are less than this % of either user's total interactions (default 5).",
        layers="When focusing on a member, how many layers of connections to expand (default 2).",
        limit="Max number of members to include in server-wide view (default 40).",
        spread="How spread out the graph is — higher = more space between nodes (default 1.0).",
        max_per_node="Keep only the top N edges per node by weight. 0 = no limit (default 0).",
    )
    @app_commands.choices(timescale=[
        app_commands.Choice(name="hour",  value="hour"),
        app_commands.Choice(name="day",   value="day"),
        app_commands.Choice(name="week",  value="week"),
        app_commands.Choice(name="month", value="month"),
        app_commands.Choice(name="all time", value="all"),
    ])
    async def connection_web(
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        timescale: str = "all",
        min_pct: app_commands.Range[int, 1, 100] = 5,
        layers: app_commands.Range[int, 1, 5] = 2,
        limit: app_commands.Range[int, 5, 60] = 40,
        spread: app_commands.Range[float, 0.5, 5.0] = 1.0,
        max_per_node: app_commands.Range[int, 0, 20] = 0,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        _TIMESCALE_SECONDS = {"hour": 3600, "day": 86400, "week": 604800, "month": 2592000}
        after_ts = int(_time.time()) - _TIMESCALE_SECONDS[timescale] if timescale in _TIMESCALE_SECONDS else None

        with ctx.open_db() as conn:
            all_edges = query_connection_web(
                conn, guild.id,
                min_weight=1,
                limit_users=limit,
                after_ts=after_ts,
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
            # Expand outward layer by layer. Layer 1 = direct connections (blurple),
            # layers 2+ go into second_level_ids (green).
            included_ids: set[int] = {member.id}
            frontier: set[int] = {member.id}
            second_level_ids = set()

            for layer_idx in range(layers):
                new_nodes: set[int] = set()
                for u, v, w in all_edges:
                    if not _pct_passes(u, v, w):
                        continue
                    if u in frontier and v not in included_ids:
                        new_nodes.add(v)
                    elif v in frontier and u not in included_ids:
                        new_nodes.add(u)
                if not new_nodes:
                    break
                if layer_idx > 0:
                    second_level_ids |= new_nodes
                included_ids |= new_nodes
                frontier = new_nodes

            # Collect every edge whose both endpoints are included and pass the threshold
            edges = [
                (u, v, w) for u, v, w in all_edges
                if u in included_ids and v in included_ids and _pct_passes(u, v, w)
            ]
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

        if max_per_node > 0:
            node_edges: dict[int, list[tuple[int, int, int]]] = {}
            for u, v, w in edges:
                node_edges.setdefault(u, []).append((u, v, w))
                node_edges.setdefault(v, []).append((u, v, w))
            kept: set[tuple[int, int]] = set()
            for ne in node_edges.values():
                ne.sort(key=lambda e: e[2], reverse=True)
                for eu, ev, _ in ne[:max_per_node]:
                    kept.add((min(eu, ev), max(eu, ev)))
            edges = [(u, v, w) for u, v, w in edges if (min(u, v), max(u, v)) in kept]

        if not edges:
            await interaction.followup.send(no_data_msg, ephemeral=True)
            return

        # Resolve display names via an authoritative gateway fetch so we don't
        # rely on the member cache, which can be stale (left members still
        # cached, current members not yet cached).
        candidate_ids = list({uid for u, v, _ in edges for uid in (u, v)})
        name_map: dict[int, str] = {}
        for i in range(0, len(candidate_ids), 100):
            batch = candidate_ids[i:i + 100]
            try:
                fetched = await guild.query_members(user_ids=batch, limit=100)
                for m in fetched:
                    name_map[m.id] = m.display_name
            except (discord.ClientException, discord.HTTPException):
                # Members intent not available — fall back to cache
                for uid in batch:
                    cached = guild.get_member(uid)
                    if cached:
                        name_map[uid] = cached.display_name

        edges = [(u, v, w) for u, v, w in edges if u in name_map and v in name_map]

        loop = asyncio.get_running_loop()
        chart_bytes = await loop.run_in_executor(
            None,
            functools.partial(
                render_connection_web,
                edges,
                name_map,
                guild_name=guild.name,
                focus_user_id=member.id if member else None,
                second_level_ids=second_level_ids,
                spread=spread,
            ),
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
        reset="Clear all existing interaction data for this server before scanning (fixes inflated counts).",
    )
    async def interaction_scan(
        interaction: discord.Interaction,
        days: app_commands.Range[int, 0, 3650] = 0,
        reset: bool = False,
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
            if reset:
                clear_interaction_data(conn, guild.id)

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
                        msg_ts = int(message.created_at.timestamp())

                        # Reply and mention targets
                        reply_to_id: int | None = (
                            message.reference.message_id
                            if message.reference and message.reference.message_id
                            else None
                        )
                        mention_ids = [
                            u.id for u in message.mentions
                            if not u.bot and u.id != message.author.id
                        ]

                        store_message(
                            conn,
                            message_id=message.id,
                            guild_id=guild.id,
                            channel_id=ch.id,
                            author_id=message.author.id,
                            content=message.content or None,
                            reply_to_id=reply_to_id,
                            ts=msg_ts,
                            attachment_urls=[a.url for a in message.attachments],
                            mention_ids=mention_ids,
                        )

                        for reaction in message.reactions:
                            set_reaction_count(conn, message.id, str(reaction.emoji), reaction.count)

                        # Interaction graph targets
                        targets: list[int] = list(mention_ids)
                        if message.reference and isinstance(
                            message.reference.resolved, discord.Message
                        ):
                            ref = message.reference.resolved
                            if not ref.author.bot and ref.author.id != message.author.id and ref.author.id not in targets:
                                targets.insert(0, ref.author.id)

                        if targets:
                            record_interactions(
                                conn, guild.id, message.author.id, targets,
                                ts=msg_ts,
                                message_id=message.id,
                            )
                            stats["interactions"] += len(targets)

                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.warning("Could not scan channel %s: %s", ch.id, exc)

        window_label = "all available history" if days == 0 else f"last {days} day{'s' if days != 1 else ''}"
        reset_note = " (existing data was cleared first)" if reset else ""
        await interaction.followup.send(
            f"Interaction scan complete for {window_label}{reset_note}.\n"
            f"Channels scanned: **{stats['channels']}**\n"
            f"Messages read: **{stats['messages']}**\n"
            f"Interactions recorded: **{stats['interactions']}**",
            ephemeral=True,
        )
