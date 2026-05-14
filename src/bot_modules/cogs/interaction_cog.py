"""Interaction graph commands."""

from __future__ import annotations

import asyncio
import functools
import io
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.services.interaction_graph import render_connection_web
from bot_modules.services.invite_tracker import query_invite_web
from bot_modules.services.name_resolver import resolve_display_names

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.interaction_commands")


class InteractionCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

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
                for u, v, _ in all_edges:
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
        name_map = await resolve_display_names(
            bot=self.bot,
            guild=guild,
            db_path=self.ctx.db_path,
            user_ids=candidate_ids,
        )

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
