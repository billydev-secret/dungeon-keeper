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

_REPORT_CTX_MENU_NAME = "Report Rule Violation"


class _ReportViolationModal(discord.ui.Modal, title="Report Rule Violation"):
    """Mod-initiated manual report: logs a rules-watch event pre-labeled as a
    confirmed violation (a high-value positive training example)."""

    rule: discord.ui.TextInput = discord.ui.TextInput(  # type: ignore[assignment]
        label="Rule number (optional)",
        placeholder="e.g. 3",
        required=False,
        max_length=16,
    )
    note: discord.ui.TextInput = discord.ui.TextInput(  # type: ignore[assignment]
        label="Note (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, source_message: discord.Message, ctx: AppContext) -> None:
        super().__init__()
        self.source_message = source_message
        self._ctx = ctx

    async def on_submit(self, interaction: discord.Interaction) -> None:
        ctx = self._ctx
        msg = self.source_message
        guild_id = interaction.guild_id or 0
        rule_val = self.rule.value.strip() or None
        note_val = self.note.value.strip() or None

        # Pull everything from the live message object — this repo drops stored
        # content by default, so the DB row may not exist.
        message_id = msg.id
        author_id = msg.author.id
        channel_id = msg.channel.id
        reporter_id = interaction.user.id
        content_excerpt = (msg.content or "")[:500] or None

        from bot_modules.rules_watch import service

        def _do_report() -> int:
            with ctx.open_db() as conn:
                event_id = service.insert_event(
                    conn,
                    guild_id=guild_id,
                    message_id=message_id,
                    author_id=author_id,
                    channel_id=channel_id,
                    guard_verdict="manual",
                    guard_rule=rule_val,
                    guard_reason=content_excerpt,
                    priority_score=10.0,
                    priority_tier="immediate",
                    priority_reason="Manually reported by moderator",
                )
                service.upsert_label(
                    conn,
                    event_id,
                    is_violation=True,
                    corrected_rule=rule_val,
                    labeled_by=reporter_id,
                    notes=note_val,
                )
                return event_id

        event_id = await asyncio.to_thread(_do_report)

        rule_str = f"Rule {rule_val}" if rule_val else "a rule violation"
        await interaction.response.send_message(
            f"✅ Logged {rule_str} against {msg.author.mention} as a confirmed "
            f"violation (event #{event_id}).",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


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

    async def cog_load(self) -> None:
        ctx = self.ctx

        async def _report_ctx_cb(
            interaction: discord.Interaction, message: discord.Message
        ) -> None:
            if not ctx.is_mod(interaction):
                await interaction.response.send_message("Permission denied.", ephemeral=True)
                return
            if message.author.bot:
                await interaction.response.send_message(
                    "Can't report a bot message.", ephemeral=True
                )
                return
            await interaction.response.send_modal(_ReportViolationModal(message, ctx))

        menu = app_commands.ContextMenu(
            name=_REPORT_CTX_MENU_NAME, callback=_report_ctx_cb
        )
        menu.default_permissions = discord.Permissions(manage_guild=True)
        self.bot.tree.add_command(menu)
        self._report_context_menu = menu

    async def cog_unload(self) -> None:
        if hasattr(self, "_report_context_menu"):
            self.bot.tree.remove_command(
                _REPORT_CTX_MENU_NAME, type=discord.AppCommandType.message
            )

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
            else discord.Color.blurple()
        )
        embed = discord.Embed(title="Rules Watch — Stats", color=accent)
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
