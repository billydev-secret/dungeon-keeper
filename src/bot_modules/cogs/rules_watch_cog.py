"""Rules Watch — the mod-facing "Report Rule Violation" message context menu.

Right-clicking a message and reporting it is in-the-moment mod work, so it
stays in Discord. Everything else about Rules Watch is on the web dashboard:
enable/disable and the alert channel in ``rules-watch-settings.js``
(``PUT /api/config/rules-watch``), and the alert queue, manual labeling, and
signal stats in ``rules-watch.js`` (``/api/rules-watch/events``,
``…/events/{id}/label``, ``…/stats``). The ``/rules-watch`` slash commands that
duplicated those three panels were removed 2026-07-28.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot_modules.services.replies import NO_PERMISSION

_purge_log = logging.getLogger("dungeonkeeper.rules_watch")

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

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
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        super().__init__()

    async def cog_load(self) -> None:
        ctx = self.bot.ctx

        async def _report_ctx_cb(
            interaction: discord.Interaction, message: discord.Message
        ) -> None:
            if not ctx.is_mod(interaction):
                await interaction.response.send_message(NO_PERMISSION, ephemeral=True)
                return
            if message.author.bot:
                await interaction.response.send_message(
                    "❌ Can't report a bot message.", ephemeral=True
                )
                return
            await interaction.response.send_modal(_ReportViolationModal(message, ctx))

        menu = app_commands.ContextMenu(
            name=_REPORT_CTX_MENU_NAME, callback=_report_ctx_cb
        )
        menu.default_permissions = discord.Permissions(manage_guild=True)
        self.bot.tree.add_command(menu)
        self._report_context_menu = menu
        self._dismissed_purge_loop.start()

    async def cog_unload(self) -> None:
        self._dismissed_purge_loop.cancel()
        if hasattr(self, "_report_context_menu"):
            self.bot.tree.remove_command(
                _REPORT_CTX_MENU_NAME, type=discord.AppCommandType.message
            )

    @tasks.loop(hours=24)
    async def _dismissed_purge_loop(self) -> None:
        """Retention: mod-dismissed events (labeled false positives) age out
        after 180 days — their excerpt/context-window content outlives its
        tuning value. Confirmed violations are never touched."""

        def _purge() -> int:
            from bot_modules.core.db_utils import open_db
            from bot_modules.rules_watch.ledger import purge_old_dismissed_events

            with open_db(self.bot.ctx.db_path) as conn:
                return purge_old_dismissed_events(conn)

        try:
            removed = await asyncio.to_thread(_purge)
            if removed:
                _purge_log.info("purged %d dismissed rules events past retention", removed)
        except Exception:
            _purge_log.exception("dismissed-event purge failed")

    @_dismissed_purge_loop.before_loop
    async def _before_dismissed_purge(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: Bot) -> None:
    await bot.add_cog(RulesWatchCog(bot))
