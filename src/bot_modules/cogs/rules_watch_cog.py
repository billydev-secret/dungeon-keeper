"""Rules Watch — slash commands for enable/disable, config, digest, and manual labeling."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.branding import resolve_accent_color
from bot_modules.core.db_utils import get_config_value, set_config_value

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.rules_watch")


class RulesWatchCog(commands.Cog):
    rules_watch = app_commands.Group(
        name="rules-watch",
        description="Passive AI moderation monitor — alert queue and labeling.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    # ------------------------------------------------------------------
    # Enable / disable
    # ------------------------------------------------------------------

    @rules_watch.command(name="enable", description="Start passive monitoring of all public channels.")
    async def rw_enable(self, interaction: discord.Interaction) -> None:
        if not self.ctx.is_mod(interaction):
            await interaction.response.send_message("Permission denied.", ephemeral=True)
            return
        guild_id = interaction.guild_id or 0

        def _do_enable():
            with self.ctx.open_db() as conn:
                set_config_value(conn, "rules_watch_enabled", "1", guild_id)

        await asyncio.to_thread(_do_enable)
        await interaction.response.send_message(
            "✅ Rules Watch enabled. The monitor will start screening public messages.\n"
            "Use `/rules-watch set-channel` to configure where immediate alerts go.",
            ephemeral=True,
        )

    @rules_watch.command(name="disable", description="Stop passive monitoring.")
    async def rw_disable(self, interaction: discord.Interaction) -> None:
        if not self.ctx.is_mod(interaction):
            await interaction.response.send_message("Permission denied.", ephemeral=True)
            return
        guild_id = interaction.guild_id or 0

        def _do_disable():
            with self.ctx.open_db() as conn:
                set_config_value(conn, "rules_watch_enabled", "0", guild_id)

        await asyncio.to_thread(_do_disable)
        await interaction.response.send_message("Rules Watch disabled.", ephemeral=True)

    # ------------------------------------------------------------------
    # Alert channel
    # ------------------------------------------------------------------

    @rules_watch.command(
        name="set-channel",
        description="Set the channel where immediate alerts are posted.",
    )
    @app_commands.describe(channel="Channel for immediate alert embeds.")
    async def rw_set_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        if not self.ctx.is_mod(interaction):
            await interaction.response.send_message("Permission denied.", ephemeral=True)
            return
        guild_id = interaction.guild_id or 0
        channel_id = channel.id

        def _do_set_channel():
            with self.ctx.open_db() as conn:
                set_config_value(conn, "rules_watch_channel_id", str(channel_id), guild_id)

        await asyncio.to_thread(_do_set_channel)
        await interaction.response.send_message(
            f"Immediate alerts will be posted to {channel.mention}.", ephemeral=True
        )

    # ------------------------------------------------------------------
    # Digest
    # ------------------------------------------------------------------

    @rules_watch.command(
        name="digest",
        description="Post a summary of all unlabeled digest-tier events.",
    )
    async def rw_digest(self, interaction: discord.Interaction) -> None:
        if not self.ctx.is_mod(interaction):
            await interaction.response.send_message("Permission denied.", ephemeral=True)
            return
        guild_id = interaction.guild_id or 0
        guild = interaction.guild

        from bot_modules.rules_watch import service

        def _do_get_events():
            with self.ctx.open_db() as conn:
                return service.get_pending_events(conn, guild_id, tier="digest", limit=25)

        events = await asyncio.to_thread(_do_get_events)
        if not events:
            await interaction.response.send_message(
                "No pending digest events.", ephemeral=True
            )
            return

        from bot_modules.rules_watch.alert import build_digest_embed
        embed = build_digest_embed(list(events), guild)  # type: ignore[arg-type]
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    @rules_watch.command(
        name="stats",
        description="Show signal firing rates, label counts, and false-positive rate.",
    )
    async def rw_stats(self, interaction: discord.Interaction) -> None:
        if not self.ctx.is_mod(interaction):
            await interaction.response.send_message("Permission denied.", ephemeral=True)
            return
        guild_id = interaction.guild_id or 0

        from bot_modules.rules_watch import service

        def _do_get_stats():
            with self.ctx.open_db() as conn:
                return service.get_stats(conn, guild_id)

        stats = await asyncio.to_thread(_do_get_stats)
        guild = interaction.guild
        accent = (
            await resolve_accent_color(self.ctx.db_path, guild)
            if guild is not None
            else discord.Colour.blurple()
        )
        embed = discord.Embed(title="Rules Watch — Stats", colour=accent)
        embed.add_field(
            name="Events",
            value=f"Total: {stats['total']} | Labeled: {stats['labeled']} | "
                  f"Confirmed: {stats['confirmed']} | FP: {stats['false_positives']}",
            inline=False,
        )
        if stats["fp_rate"] is not None:
            embed.add_field(name="False Positive Rate", value=f"{stats['fp_rate']:.1%}", inline=True)
        by_tier = stats.get("by_tier") or {}
        tier_str = " | ".join(f"{k}: {v}" for k, v in by_tier.items())
        if tier_str:
            embed.add_field(name="By Tier", value=tier_str, inline=False)
        by_rule = stats.get("by_rule") or {}
        rule_str = " | ".join(f"Rule {k}: {v}" for k, v in list(by_rule.items())[:6])
        if rule_str:
            embed.add_field(name="By Rule", value=rule_str, inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # Manual label
    # ------------------------------------------------------------------

    @rules_watch.command(
        name="label",
        description="Manually label a rules-watch event (for digest-tier events).",
    )
    @app_commands.describe(
        event_id="The numeric event ID shown in the digest.",
        verdict="'violation' or 'fp' (false positive).",
        corrected_rule="Optional: the correct rule number if the guard was wrong.",
    )
    async def rw_label(
        self,
        interaction: discord.Interaction,
        event_id: int,
        verdict: str,
        corrected_rule: str | None = None,
    ) -> None:
        if not self.ctx.is_mod(interaction):
            await interaction.response.send_message("Permission denied.", ephemeral=True)
            return

        is_violation = verdict.lower().startswith("v")

        from bot_modules.rules_watch import service
        with self.ctx.open_db() as conn:
            ev = service.get_event(conn, event_id)
            if ev is None:
                await interaction.response.send_message(
                    f"Event #{event_id} not found.", ephemeral=True
                )
                return
            service.upsert_label(
                conn,
                event_id,
                is_violation=is_violation,
                corrected_rule=corrected_rule,
                labeled_by=interaction.user.id,
            )

        label_str = "violation" if is_violation else "false positive"
        await interaction.response.send_message(
            f"Event #{event_id} labeled as **{label_str}**.", ephemeral=True
        )

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @rules_watch.command(name="status", description="Show whether Rules Watch is currently active.")
    async def rw_status(self, interaction: discord.Interaction) -> None:
        if not self.ctx.is_mod(interaction):
            await interaction.response.send_message("Permission denied.", ephemeral=True)
            return
        guild_id = interaction.guild_id or 0

        def _do_get_status():
            with self.ctx.open_db() as conn:
                _enabled = get_config_value(conn, "rules_watch_enabled", "0", guild_id).strip() == "1"
                _ch_raw = get_config_value(conn, "rules_watch_channel_id", "0", guild_id)
            return _enabled, _ch_raw

        enabled, ch_raw = await asyncio.to_thread(_do_get_status)
        ch_id = int(ch_raw.strip()) if ch_raw.strip().isdigit() else 0
        ch_mention = f"<#{ch_id}>" if ch_id else "*(not set)*"
        status = "✅ Enabled" if enabled else "⏸ Disabled"
        await interaction.response.send_message(
            f"Rules Watch: **{status}**\nAlert channel: {ch_mention}", ephemeral=True
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(RulesWatchCog(bot, bot.ctx))
