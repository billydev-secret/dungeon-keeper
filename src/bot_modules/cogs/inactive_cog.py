"""Inactive-channel moderation cog.

Moves inactive members to a single shared inactive channel: their roles are
snapshotted and stripped, they get the ``@Inactive`` role (which can only see
the inactive channel), and a persistent panel there invites them to open a
ticket to be reactivated. Mirrors the jail cog's patterns but is deliberately
smaller — no per-user channels, transcripts, or policy machinery.

Two ways in:

* ``/inactive mark @user`` — manual, mirrors ``/jail``.
* Automatic sweep — a background loop (opt-in via the web dashboard's
  Inactive Sweep panel) that moves members idle past a configurable
  threshold. The sweep is a
  destructive mass role-strip, so it never touches bots/mods/admins/the owner,
  is hard-capped per run, and the dashboard's Check Now is a dry-run preview.

One way out: ``/inactive release @user`` restores roles and removes ``@Inactive``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.commands.jail_commands import (
    _is_mod,
)
from bot_modules.core.db_utils import get_config_value, set_config_value
from bot_modules.inactive.apply import (
    apply_inactive,
    check_inactive_preconditions,
    reactivate_member,
)
from bot_modules.inactive.sweep_service import (
    auto_sweep_enabled,
    compute_candidates,
    read_inactive_channel_id,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.inactive")

_SWEEP_INTERVAL_SECONDS = 6 * 3600  # background loop cadence

# Candidate gathering and the config readers live in inactive/sweep_service.py
# so the dashboard's dry-run preview selects from exactly the same rules this
# cog sweeps by.


# ── Cog ───────────────────────────────────────────────────────────────


class InactiveCog(commands.Cog):
    inactive = app_commands.Group(
        name="inactive", description="Inactive-channel management."
    )

    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    async def cog_load(self) -> None:
        # The panel reuses the ticket system's persistent "Open Ticket" button,
        # which JailCog already registers via add_dynamic_items — no need to
        # re-register it here. Start the auto-sweep background loop.
        self.bot.startup_task_factories.append(
            lambda: inactive_sweep_loop(self.bot, self.ctx)
        )

    # ── /inactive mark ────────────────────────────────────────────────

    @inactive.command(name="mark", description="Move a member to the inactive channel.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(user="Member to move", reason="Optional note")
    async def inactive_mark(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str | None = None,
    ) -> None:
        ctx = self.ctx
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member) or not _is_mod(member, ctx):
            await interaction.response.send_message("❌ Mod only.", ephemeral=True)
            return

        if not read_inactive_channel_id(ctx, guild.id):
            await interaction.response.send_message(
                "❌ No inactive channel is set up yet. Set one on the dashboard "
                "(Config → Inactive Sweep → Inactive Channel) so moved members have "
                "somewhere to land.",
                ephemeral=True,
            )
            return

        precheck = check_inactive_preconditions(ctx, guild, user, member)
        if precheck is not None:
            await interaction.response.send_message(
                precheck.error_message or "❌ Cannot move this user.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        result = await apply_inactive(
            ctx, guild, user, member, reason=reason or "", source="command"
        )
        if not result.ok:
            await interaction.followup.send(
                result.error_message or "❌ Failed to move user.", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"💤 {user} moved to the inactive channel. Their roles are saved.",
            ephemeral=True,
        )

    # ── /reactivate (top-level for discoverability, like /unjail) ──────

    @inactive.command(name="release", description="Reactivate a member and restore their roles.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(user="Member to reactivate", reason="Release reason")
    async def inactive_release(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str | None = None,
    ) -> None:
        ctx = self.ctx
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member) or not _is_mod(member, ctx):
            await interaction.response.send_message("❌ Mod only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await reactivate_member(ctx, guild, user, reason=reason or "", actor=member)
        await interaction.followup.send(result, ephemeral=True)

    # ── /inactive panel ───────────────────────────────────────────────

    # /inactive panel and /inactive sweep were replaced by Config → Inactive
    # Sweep on 2026-07-28. The panel page had been telling admins to go run
    # /inactive panel in Discord — the one place the dashboard depended on a
    # command. Both flows live in inactive/sweep_service.py now
    # (setup_inactive_channel, run_inactive_sweep) so the routes and the
    # auto-sweep loop share one implementation.


def _read_config(ctx: AppContext, key: str, guild_id: int) -> str:
    with ctx.open_db() as conn:
        return get_config_value(conn, key, "0", guild_id) or "0"


def _set_config(ctx: AppContext, key: str, value: str, guild_id: int) -> None:
    with ctx.open_db() as conn:
        set_config_value(conn, key, value, guild_id)


# ── Background auto-sweep loop ─────────────────────────────────────────


async def inactive_sweep_loop(bot: discord.Client, ctx: AppContext) -> None:
    """Move idle members to the inactive channel when auto-sweep is enabled.

    Runs every few hours but does nothing unless ``inactive_auto_sweep`` is on
    for the home guild. Respects the per-run cap and the same exclusions as the
    manual sweep (bots/mods/admins/owner/already-inactive).
    """
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            guild = bot.get_guild(ctx.guild_id)
            if (
                guild is not None
                and guild.me is not None
                and auto_sweep_enabled(ctx, guild.id)
                and read_inactive_channel_id(ctx, guild.id)
            ):
                selection = await compute_candidates(ctx, guild)
                overflow = selection.overflow
                moved = 0
                for c in selection.candidates:
                    target = guild.get_member(c.user_id)
                    if target is None:
                        continue
                    result = await apply_inactive(
                        ctx, guild, target, guild.me, reason="Auto inactivity sweep",
                        source="auto",
                    )
                    if result.ok:
                        moved += 1
                if moved:
                    log.info(
                        "Auto-swept %d member(s) to inactive in guild %s (%d held by cap)",
                        moved, guild.id, overflow,
                    )
        except Exception:
            log.exception("Error in inactive sweep loop")
        await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)


async def setup(bot: Bot) -> None:
    await bot.add_cog(InactiveCog(bot, bot.ctx))
