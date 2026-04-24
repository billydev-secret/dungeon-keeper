"""Interaction graph commands."""

from __future__ import annotations

import asyncio
import functools
import io
import logging
import time as _time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from services.interaction_graph import (
    query_connection_web,
    render_connection_web,
    render_interaction_heatmap,
)
from services.invite_tracker import query_invite_web

if TYPE_CHECKING:
    from app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.interaction_commands")


class InteractionCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    async def connection_web(
        self,
        interaction: discord.Interaction,
        member: discord.User | None = None,
        timescale: str = "all",
        min_pct: app_commands.Range[int, 1, 100] = 5,
        layers: app_commands.Range[int, 1, 5] = 2,
        limit: app_commands.Range[int, 5, 60] = 40,
        spread: app_commands.Range[float, 0.5, 5.0] = 1.0,
        max_per_node: app_commands.Range[int, 0, 20] = 3,
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

        _TIMESCALE_SECONDS = {
            "hour": 3600,
            "day": 86400,
            "week": 604800,
            "month": 2592000,
        }
        after_ts = (
            int(_time.time()) - _TIMESCALE_SECONDS[timescale]
            if timescale in _TIMESCALE_SECONDS
            else None
        )

        def _query_web():
            with ctx.open_db() as conn:
                return query_connection_web(
                    conn,
                    guild.id,
                    min_weight=1,
                    limit_users=limit,
                    after_ts=after_ts,
                )

        all_edges = await asyncio.to_thread(_query_web)

        node_total: dict[int, int] = {}
        for u, v, w in all_edges:
            node_total[u] = node_total.get(u, 0) + w
            node_total[v] = node_total.get(v, 0) + w

        threshold = min_pct / 100.0

        def _pct_passes(u: int, v: int, w: int) -> bool:
            denom = min(node_total.get(u, 1), node_total.get(v, 1))
            return w >= threshold * denom

        second_level_ids: set[int] | None = None

        if member is not None:
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

            edges = [
                (u, v, w)
                for u, v, w in all_edges
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
            node_top: dict[int, set[int]] = {}
            adj: dict[int, list[tuple[int, int, int]]] = {}
            for u, v, w in edges:
                adj.setdefault(u, []).append((u, v, w))
                adj.setdefault(v, []).append((u, v, w))
            for node, ne in adj.items():
                ne.sort(key=lambda e: e[2], reverse=True)
                node_top[node] = {
                    (ev if eu == node else eu) for eu, ev, _ in ne[:max_per_node]
                }
            edges = [
                (u, v, w)
                for u, v, w in edges
                if v in node_top.get(u, set()) and u in node_top.get(v, set())
            ]

            if member is not None and edges:
                _adj: dict[int, set[int]] = {}
                for u, v, _ in edges:
                    _adj.setdefault(u, set()).add(v)
                    _adj.setdefault(v, set()).add(u)
                reachable: set[int] = set()
                stack = [member.id]
                while stack:
                    cur = stack.pop()
                    if cur in reachable:
                        continue
                    reachable.add(cur)
                    stack.extend(_adj.get(cur, set()) - reachable)
                edges = [
                    (u, v, w) for u, v, w in edges if u in reachable and v in reachable
                ]

        if not edges:
            await interaction.followup.send(no_data_msg, ephemeral=True)
            return

        candidate_ids = list({uid for u, v, _ in edges for uid in (u, v)})
        name_map: dict[int, str] = {}
        for i in range(0, len(candidate_ids), 100):
            batch = candidate_ids[i : i + 100]
            try:
                fetched = await guild.query_members(user_ids=batch, limit=100)
                for m in fetched:
                    name_map[m.id] = m.display_name
            except (discord.ClientException, discord.HTTPException):
                for uid in batch:
                    cached = guild.get_member(uid)
                    if cached:
                        name_map[uid] = cached.display_name

        for uid in candidate_ids:
            if uid not in name_map:
                try:
                    fetched_user = await self.bot.fetch_user(uid)
                    name_map[uid] = fetched_user.display_name
                except discord.NotFound:
                    pass

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

    async def interaction_heatmap(
        self,
        interaction: discord.Interaction,
        timescale: str = "all",
        min_pct: app_commands.Range[int, 1, 100] = 5,
        limit: app_commands.Range[int, 5, 60] = 30,
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

        _TIMESCALE_SECONDS = {
            "hour": 3600,
            "day": 86400,
            "week": 604800,
            "month": 2592000,
        }
        after_ts = (
            int(_time.time()) - _TIMESCALE_SECONDS[timescale]
            if timescale in _TIMESCALE_SECONDS
            else None
        )

        def _query_heatmap():
            with ctx.open_db() as conn:
                return query_connection_web(
                    conn,
                    guild.id,
                    min_weight=1,
                    limit_users=limit,
                    after_ts=after_ts,
                )

        all_edges = await asyncio.to_thread(_query_heatmap)

        node_total: dict[int, int] = {}
        for u, v, w in all_edges:
            node_total[u] = node_total.get(u, 0) + w
            node_total[v] = node_total.get(v, 0) + w

        threshold = min_pct / 100.0
        edges = [
            (u, v, w)
            for u, v, w in all_edges
            if w >= threshold * min(node_total.get(u, 1), node_total.get(v, 1))
        ]

        if not edges:
            await interaction.followup.send(
                f"No edges found where a connection accounts for ≥**{min_pct}%** "
                "of either user's total interactions. "
                "Try lowering `min_pct` or running `/interaction_scan`.",
                ephemeral=True,
            )
            return

        candidate_ids = list({uid for u, v, _ in edges for uid in (u, v)})
        name_map: dict[int, str] = {}
        for i in range(0, len(candidate_ids), 100):
            batch = candidate_ids[i : i + 100]
            try:
                fetched = await guild.query_members(user_ids=batch, limit=100)
                for m in fetched:
                    name_map[m.id] = m.display_name
            except (discord.ClientException, discord.HTTPException):
                for uid in batch:
                    cached = guild.get_member(uid)
                    if cached:
                        name_map[uid] = cached.display_name

        edges = [(u, v, w) for u, v, w in edges if u in name_map and v in name_map]

        if not edges:
            await interaction.followup.send(
                "Could not resolve any member names for the interaction data.",
                ephemeral=True,
            )
            return

        loop = asyncio.get_running_loop()
        chart_bytes = await loop.run_in_executor(
            None,
            functools.partial(
                render_interaction_heatmap,
                edges,
                name_map,
                guild_name=guild.name,
            ),
        )
        await interaction.followup.send(
            file=discord.File(
                io.BytesIO(chart_bytes), filename="interaction_heatmap.png"
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="invite_web",
        description="Network graph of who invited whom.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        member="Focus on one person's invite tree. Omit for everyone.",
        spread="Visual spacing between nodes. Higher = more spread out.",
    )
    async def invite_web(
        self,
        interaction: discord.Interaction,
        member: discord.User | None = None,
        spread: app_commands.Range[float, 0.5, 5.0] = 1.0,
    ) -> None:
        ctx = self.ctx
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.",
                ephemeral=True,
            )
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works in a server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        def _query_invites():
            with ctx.open_db() as conn:
                return query_invite_web(conn, guild.id)

        all_edges = await asyncio.to_thread(_query_invites)

        if member is not None:
            included: set[int] = {member.id}
            changed = True
            while changed:
                changed = False
                for u, v, _w in all_edges:
                    if u in included and v not in included:
                        included.add(v)
                        changed = True
                    elif v in included and u not in included:
                        included.add(u)
                        changed = True
            edges = [
                (u, v, w) for u, v, w in all_edges if u in included and v in included
            ]
        else:
            edges = all_edges

        if not edges:
            await interaction.followup.send(
                "No invite data recorded yet. Invites are tracked as new members join.",
                ephemeral=True,
            )
            return

        candidate_ids = list({uid for u, v, _ in edges for uid in (u, v)})
        name_map: dict[int, str] = {}
        for i in range(0, len(candidate_ids), 100):
            batch = candidate_ids[i : i + 100]
            try:
                fetched = await guild.query_members(user_ids=batch, limit=100)
                for m in fetched:
                    name_map[m.id] = m.display_name
            except (discord.ClientException, discord.HTTPException):
                for uid in batch:
                    cached = guild.get_member(uid)
                    if cached:
                        name_map[uid] = cached.display_name

        for uid in candidate_ids:
            if uid not in name_map:
                try:
                    fetched_user = await self.bot.fetch_user(uid)
                    name_map[uid] = fetched_user.display_name
                except discord.NotFound:
                    pass

        edges = [(u, v, w) for u, v, w in edges if u in name_map and v in name_map]
        if not edges:
            await interaction.followup.send(
                "No invite edges with resolvable users found.",
                ephemeral=True,
            )
            return

        loop = asyncio.get_running_loop()
        chart_bytes = await loop.run_in_executor(
            None,
            functools.partial(
                render_connection_web,
                edges,
                name_map,
                guild_name=f"{guild.name} — Invite Tree",
                focus_user_id=member.id if member else None,
                spread=spread,
            ),
        )
        await interaction.followup.send(
            file=discord.File(io.BytesIO(chart_bytes), filename="invite_web.png"),
            ephemeral=True,
        )



async def setup(bot: Bot) -> None:
    await bot.add_cog(InteractionCog(bot, bot.ctx))
