"""Auto-delete slash commands."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import discord
from discord import app_commands

from services.auto_delete_service import (
    format_duration_seconds,
    list_auto_delete_rules_for_guild,
    parse_duration_seconds,
    remove_auto_delete_rule,
    upsert_auto_delete_rule,
)
from settings import AUTO_DELETE_KEYWORDS, AUTO_DELETE_SETTINGS
from utils import format_user_for_log, get_bot_member, get_guild_channel_or_thread

if TYPE_CHECKING:
    from app_context import AppContext, Bot


async def _delete_messages_older_than(
    channel: discord.TextChannel | discord.Thread,
    cutoff: datetime,
    *,
    reason: str,
) -> tuple[int, int, int, int]:
    scanned = 0
    deleted = 0
    skipped_pinned = 0
    failed = 0
    next_delete_at = 0.0

    async for message in channel.history(limit=None, before=cutoff, oldest_first=True):
        scanned += 1
        if message.pinned:
            skipped_pinned += 1
            continue
        now_monotonic = time.monotonic()
        if now_monotonic < next_delete_at:
            await asyncio.sleep(next_delete_at - now_monotonic)
        try:
            delete_call = cast(Any, message.delete)
            try:
                await delete_call(reason=reason)
            except TypeError:
                await message.delete()
            deleted += 1
            next_delete_at = time.monotonic() + AUTO_DELETE_SETTINGS.delete_pause_seconds
        except discord.Forbidden:
            failed += 1
            break
        except discord.HTTPException:
            failed += 1

    return scanned, deleted, skipped_pinned, failed


async def _send_ephemeral_text_chunks(
    interaction: discord.Interaction, text: str, chunk_size: int = 1900
) -> None:
    remaining = text
    while remaining:
        if len(remaining) <= chunk_size:
            await interaction.followup.send(remaining, ephemeral=True)
            return
        split_at = remaining.rfind("\n", 0, chunk_size + 1)
        if split_at <= 0:
            split_at = chunk_size
        chunk = remaining[:split_at]
        remaining = remaining[split_at:].lstrip("\n")
        await interaction.followup.send(chunk, ephemeral=True)


def register_auto_delete_commands(bot: Bot, ctx: AppContext) -> None:
    @bot.tree.command(
        name="auto_delete",
        description="Delete old posts now and optionally schedule recurring cleanup.",
    )
    @app_commands.describe(
        del_age="Delete posts older than this duration (examples: 30d, 2h, 15m, 1h30m).",
        run="Run once, disable schedule, or set interval (examples: once, off, 1h, 30m, 1d).",
    )
    async def auto_delete(
        interaction: discord.Interaction,
        del_age: str = "30d",
        run: str = "once",
    ):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        channel = ctx.get_xp_config_target_channel(interaction)
        if channel is None:
            await interaction.response.send_message(
                "This command only works in text channels or threads.", ephemeral=True
            )
            return

        bot_member = get_bot_member(guild)
        if bot_member is None:
            await interaction.response.send_message(
                "Bot member context is unavailable right now.", ephemeral=True
            )
            return
        if not channel.permissions_for(bot_member).manage_messages:
            await interaction.response.send_message(
                "I need the Manage Messages permission in this channel to delete posts.",
                ephemeral=True,
            )
            return

        age_seconds = parse_duration_seconds(del_age)
        if age_seconds is None:
            await interaction.response.send_message(
                "Invalid `del_age`. Use durations like `30d`, `2h`, `15m`, or `1h30m`.",
                ephemeral=True,
            )
            return
        if age_seconds < AUTO_DELETE_SETTINGS.min_age_seconds:
            await interaction.response.send_message(
                f"`del_age` must be at least {format_duration_seconds(AUTO_DELETE_SETTINGS.min_age_seconds)}.",
                ephemeral=True,
            )
            return

        run_token = run.strip().lower()
        schedule_mode = AUTO_DELETE_KEYWORDS.run_keywords.get(run_token)
        interval_seconds: int | None = None
        if schedule_mode is None:
            interval_seconds = parse_duration_seconds(run_token)
            if interval_seconds is None:
                await interaction.response.send_message(
                    "Invalid `run`. Use `once`, `off`, or a duration like `30m`, `1h`, `1d`.",
                    ephemeral=True,
                )
                return
            if interval_seconds < AUTO_DELETE_SETTINGS.min_interval_seconds:
                await interaction.response.send_message(
                    f"`run` interval must be at least "
                    f"{format_duration_seconds(AUTO_DELETE_SETTINGS.min_interval_seconds)}.",
                    ephemeral=True,
                )
                return
            schedule_mode = "schedule"

        await interaction.response.defer(ephemeral=True, thinking=True)

        cutoff = discord.utils.utcnow() - timedelta(seconds=age_seconds)
        actor = ctx.get_interaction_member(interaction)
        reason = f"Auto-delete requested by {format_user_for_log(actor, interaction.user.id)}"

        try:
            scanned, deleted, skipped_pinned, failed = await _delete_messages_older_than(
                channel, cutoff, reason=reason
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I couldn't delete messages in this channel due to missing permissions.",
                ephemeral=True,
            )
            return

        schedule_status = "Recurring cleanup unchanged."
        if schedule_mode == "schedule" and interval_seconds is not None:
            upsert_auto_delete_rule(
                ctx.db_path,
                guild.id,
                channel.id,
                age_seconds,
                interval_seconds,
                last_run_ts=time.time(),
            )
            schedule_status = (
                f"Recurring cleanup enabled: every `{format_duration_seconds(interval_seconds)}` "
                f"(age `{format_duration_seconds(age_seconds)}`)."
            )
        elif schedule_mode == "off":
            removed = remove_auto_delete_rule(ctx.db_path, guild.id, channel.id)
            schedule_status = (
                "Recurring cleanup disabled for this channel."
                if removed
                else "No recurring cleanup rule was set for this channel."
            )

        await interaction.followup.send(
            (
                f"Deleted **{deleted}** messages older than `{format_duration_seconds(age_seconds)}` "
                f"in {channel.mention}.\n"
                f"Scanned: `{scanned}` | Pinned skipped: `{skipped_pinned}` | Failed: `{failed}`\n"
                f"{schedule_status}"
            ),
            ephemeral=True,
        )

    @bot.tree.command(
        name="auto_delete_configs",
        description="List auto-delete schedules configured for this server.",
    )
    async def auto_delete_configs(interaction: discord.Interaction):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        rules = list_auto_delete_rules_for_guild(ctx.db_path, guild.id)
        if not rules:
            await interaction.response.send_message(
                "No active auto-delete schedules are configured in this server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        lines = [f"**Active Auto-Delete Schedules ({len(rules)})**", ""]
        for index, rule in enumerate(rules, start=1):
            channel_id = int(rule["channel_id"])
            channel = get_guild_channel_or_thread(guild, channel_id)
            channel_label = channel.mention if channel is not None else f"<#{channel_id}> (missing)"

            age_seconds = int(rule["max_age_seconds"])
            interval_seconds = int(rule["interval_seconds"])
            age_label = format_duration_seconds(age_seconds)
            interval_label = format_duration_seconds(interval_seconds)

            last_run_ts = float(rule["last_run_ts"])
            if last_run_ts > 0:
                last_run_display = f"<t:{int(last_run_ts)}:R>"
                next_run_ts = int(last_run_ts + interval_seconds)
                next_run_display = f"<t:{next_run_ts}:R>"
            else:
                last_run_display = "never"
                next_run_display = "as soon as the scheduler runs"

            lines.append(
                f"{index}. {channel_label} | age `{age_label}` | every `{interval_label}`\n"
                f"Last run: {last_run_display} | Next run: {next_run_display}"
            )

        await _send_ephemeral_text_chunks(interaction, "\n\n".join(lines))
