"""Auto-delete slash commands."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from pathlib import Path
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
            next_delete_at = (
                time.monotonic() + AUTO_DELETE_SETTINGS.delete_pause_seconds
            )
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


# ---------------------------------------------------------------------------
# Config panel — embed, select, modal, view
# ---------------------------------------------------------------------------


def _build_config_embed(rules: list[Any], guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="🗑️  Auto-Delete Schedules",
        color=discord.Color.from_str("#992D22"),
    )
    lines: list[str] = []
    for i, rule in enumerate(rules, 1):
        channel_id = int(rule["channel_id"])
        channel = get_guild_channel_or_thread(guild, channel_id)
        channel_label = (
            channel.mention if channel is not None else f"<#{channel_id}> (missing)"
        )
        age_label = format_duration_seconds(int(rule["max_age_seconds"]))
        interval_label = format_duration_seconds(int(rule["interval_seconds"]))
        last_run_ts = float(rule["last_run_ts"])
        if last_run_ts > 0:
            next_run_ts = int(last_run_ts + int(rule["interval_seconds"]))
            timing = f"last <t:{int(last_run_ts)}:R> · next <t:{next_run_ts}:R>"
        else:
            timing = "not yet run"
        lines.append(
            f"`{i}.` {channel_label}  |  age `{age_label}`  |  every `{interval_label}`\n"
            f"\u3000{timing}"
        )
    embed.description = "\n\n".join(lines)
    embed.set_footer(text="Select a schedule below to edit or remove it.")
    return embed


class _RuleSelect(discord.ui.Select):
    def __init__(self, rules: list[Any], guild: discord.Guild, invoker_id: int):
        self.invoker_id = invoker_id
        options: list[discord.SelectOption] = []
        for i, rule in enumerate(rules):
            channel_id = int(rule["channel_id"])
            channel = get_guild_channel_or_thread(guild, channel_id)
            label = (channel.name if channel is not None else f"#{channel_id}")[:100]
            age_label = format_duration_seconds(int(rule["max_age_seconds"]))
            interval_label = format_duration_seconds(int(rule["interval_seconds"]))
            options.append(
                discord.SelectOption(
                    label=label,
                    value=str(i),
                    description=f"age {age_label} · every {interval_label}"[:100],
                    emoji="🗑️",
                )
            )
        super().__init__(
            placeholder="Choose a schedule to manage…", options=options[:25]
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.defer()
            return
        view: _AutoDeleteConfigView = self.view  # type: ignore[assignment]
        view.selected_index = int(self.values[0])
        view.edit_btn.disabled = False
        view.remove_btn.disabled = False
        await interaction.response.edit_message(view=view)


class _EditAutoDeleteModal(discord.ui.Modal, title="Edit Auto-Delete Schedule"):
    def __init__(
        self,
        rule: Any,
        db_path: Path,
        guild_id: int,
        guild: discord.Guild,
        original_interaction: discord.Interaction,
        invoker_id: int,
    ):
        super().__init__()
        self._rule = rule
        self._db_path = db_path
        self._guild_id = guild_id
        self._guild = guild
        self._original = original_interaction
        self._invoker_id = invoker_id

        self.del_age: discord.ui.TextInput = discord.ui.TextInput(
            label="Delete age",
            placeholder="e.g. 30d, 2h, 15m",
            max_length=20,
            default=format_duration_seconds(int(rule["max_age_seconds"])),
        )
        self.run_interval: discord.ui.TextInput = discord.ui.TextInput(
            label="Run interval (or 'off' to disable schedule)",
            placeholder="e.g. 1d, 12h, 30m, off",
            max_length=20,
            default=format_duration_seconds(int(rule["interval_seconds"])),
        )
        self.add_item(self.del_age)
        self.add_item(self.run_interval)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        age_seconds = parse_duration_seconds(self.del_age.value.strip())
        if age_seconds is None or age_seconds < AUTO_DELETE_SETTINGS.min_age_seconds:
            await interaction.response.send_message(
                f"Invalid delete age. Must be at least "
                f"`{format_duration_seconds(AUTO_DELETE_SETTINGS.min_age_seconds)}`.",
                ephemeral=True,
            )
            return

        run_token = self.run_interval.value.strip().lower()
        if AUTO_DELETE_KEYWORDS.run_keywords.get(run_token) == "off":
            remove_auto_delete_rule(
                self._db_path, self._guild_id, int(self._rule["channel_id"])
            )
            await interaction.response.defer()
            await _refresh_config_panel(
                self._original,
                self._db_path,
                self._guild_id,
                self._guild,
                self._invoker_id,
            )
            return

        interval_seconds = parse_duration_seconds(run_token)
        if (
            interval_seconds is None
            or interval_seconds < AUTO_DELETE_SETTINGS.min_interval_seconds
        ):
            await interaction.response.send_message(
                "Invalid interval. Use a duration like `1d`, `12h`, `30m`, or `off` to disable.",
                ephemeral=True,
            )
            return

        upsert_auto_delete_rule(
            self._db_path,
            self._guild_id,
            int(self._rule["channel_id"]),
            age_seconds,
            interval_seconds,
        )
        await interaction.response.defer()
        await _refresh_config_panel(
            self._original, self._db_path, self._guild_id, self._guild, self._invoker_id
        )


class _AutoDeleteConfigView(discord.ui.View):
    def __init__(
        self,
        rules: list[Any],
        guild: discord.Guild,
        invoker_id: int,
        db_path: Path,
        guild_id: int,
        original_interaction: discord.Interaction,
    ):
        super().__init__(timeout=120)
        self.rules = rules
        self.guild = guild
        self.invoker_id = invoker_id
        self.db_path = db_path
        self.guild_id = guild_id
        self.original_interaction = original_interaction
        self.selected_index: int | None = None

        self.rule_select = _RuleSelect(rules, guild, invoker_id)
        self.add_item(self.rule_select)

        self.edit_btn: discord.ui.Button = discord.ui.Button(
            label="Edit", style=discord.ButtonStyle.primary, disabled=True, row=1
        )
        self.edit_btn.callback = self._on_edit  # type: ignore[method-assign]
        self.add_item(self.edit_btn)

        self.remove_btn: discord.ui.Button = discord.ui.Button(
            label="Remove", style=discord.ButtonStyle.danger, disabled=True, row=1
        )
        self.remove_btn.callback = self._on_remove  # type: ignore[method-assign]
        self.add_item(self.remove_btn)

        missing_count = sum(
            1
            for r in rules
            if get_guild_channel_or_thread(guild, int(r["channel_id"])) is None
        )
        if missing_count:
            self.cleanup_btn: discord.ui.Button = discord.ui.Button(
                label=f"Clean up {missing_count} missing",
                style=discord.ButtonStyle.secondary,
                row=1,
            )
            self.cleanup_btn.callback = self._on_cleanup  # type: ignore[method-assign]
            self.add_item(self.cleanup_btn)

    async def _on_edit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id or self.selected_index is None:
            await interaction.response.defer()
            return
        modal = _EditAutoDeleteModal(
            self.rules[self.selected_index],
            self.db_path,
            self.guild_id,
            self.guild,
            self.original_interaction,
            self.invoker_id,
        )
        await interaction.response.send_modal(modal)

    async def _on_remove(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id or self.selected_index is None:
            await interaction.response.defer()
            return
        rule = self.rules[self.selected_index]
        remove_auto_delete_rule(self.db_path, self.guild_id, int(rule["channel_id"]))
        await interaction.response.defer()
        await _refresh_config_panel(
            self.original_interaction,
            self.db_path,
            self.guild_id,
            self.guild,
            self.invoker_id,
        )

    async def _on_cleanup(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.defer()
            return
        for rule in self.rules:
            channel_id = int(rule["channel_id"])
            if get_guild_channel_or_thread(self.guild, channel_id) is None:
                remove_auto_delete_rule(self.db_path, self.guild_id, channel_id)
        await interaction.response.defer()
        await _refresh_config_panel(
            self.original_interaction,
            self.db_path,
            self.guild_id,
            self.guild,
            self.invoker_id,
        )


async def _refresh_config_panel(
    original_interaction: discord.Interaction,
    db_path: Path,
    guild_id: int,
    guild: discord.Guild,
    invoker_id: int,
) -> None:
    rules = list_auto_delete_rules_for_guild(db_path, guild_id)
    if rules:
        embed = _build_config_embed(rules, guild)
        view = _AutoDeleteConfigView(
            rules, guild, invoker_id, db_path, guild_id, original_interaction
        )
        await original_interaction.edit_original_response(embed=embed, view=view)
    else:
        await original_interaction.edit_original_response(
            content="No active auto-delete schedules remaining.", embed=None, view=None
        )


def register_auto_delete_commands(bot: Bot, ctx: AppContext) -> None:
    @bot.tree.command(
        name="auto_delete",
        description="Delete messages older than a given age, with optional recurring schedule.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        del_age="Delete messages older than this (e.g. 30d, 2h, 15m, 1h30m).",
        run="once = run now only, off = cancel schedule, or interval like 1d, 12h.",
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
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
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
            (
                scanned,
                deleted,
                skipped_pinned,
                failed,
            ) = await _delete_messages_older_than(channel, cutoff, reason=reason)
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
        description="View, edit, or remove all auto-delete schedules in this server.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def auto_delete_configs(interaction: discord.Interaction):
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

        rules = list_auto_delete_rules_for_guild(ctx.db_path, guild.id)
        if not rules:
            await interaction.response.send_message(
                "No active auto-delete schedules are configured in this server.",
                ephemeral=True,
            )
            return

        embed = _build_config_embed(rules, guild)
        view = _AutoDeleteConfigView(
            rules, guild, interaction.user.id, ctx.db_path, guild.id, interaction
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
