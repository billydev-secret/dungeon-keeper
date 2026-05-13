"""Birthday tracker — users set their own birthday; bot announces on the day."""

from __future__ import annotations

import asyncio
import calendar
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.db_utils import get_config_value, open_db
from bot_modules.services.birthday_service import (
    MAX_DAYS as _MAX_DAYS,
    delete_birthday as _delete_birthday,
    get_birthday_preference as _get_birthday_preference,
    mark_announced as _mark_announced,
    todays_unannounced as _todays_unannounced,
    upsert_birthday as _upsert_birthday,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.birthday")

_DEFAULT_MESSAGE = "Happy birthday, {mention}! 🎂"


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------


async def _announce_for_guild(
    guild: discord.Guild, db_path: Path, today_iso: str
) -> None:
    month = int(today_iso[5:7])
    day = int(today_iso[8:10])

    with open_db(db_path) as conn:
        channel_id = int(get_config_value(conn, "birthday_channel_id", "0", guild.id))
        if not channel_id:
            return
        template = get_config_value(conn, "birthday_message", _DEFAULT_MESSAGE, guild.id)
        unannounced = _todays_unannounced(conn, guild.id, month, day, today_iso)

    if not unannounced:
        return

    channel = guild.get_channel(channel_id)
    if channel is None or not isinstance(channel, discord.TextChannel):
        return

    for user_id in unannounced:
        member = guild.get_member(user_id)
        mention = member.mention if member else f"<@{user_id}>"
        text = template.replace("{mention}", mention)
        with open_db(db_path) as conn:
            preference = _get_birthday_preference(conn, guild.id, user_id)
        if preference:
            text += f"\n*Birthday request: {preference}*"
        try:
            await channel.send(
                text,
                allowed_mentions=discord.AllowedMentions(
                    users=[member] if member else False,
                    roles=False,
                    everyone=False,
                ),
            )
        except (discord.Forbidden, discord.HTTPException):
            log.warning(
                "birthday: failed to post in guild %s channel %s for user %s",
                guild.id, channel_id, user_id,
            )
        # Always mark announced — once we've attempted today's send for a user,
        # we don't want to keep retrying every tick. Send failures show up in
        # the log; a permanently broken channel is an operator config issue,
        # not something we should keep hammering.
        with open_db(db_path) as conn:
            _mark_announced(conn, guild.id, user_id, today_iso)


async def _announce_all_guilds(bot: discord.Client, db_path: Path) -> None:
    """Run today's announcement pass across every guild the bot is in."""
    today_iso = datetime.now(timezone.utc).date().isoformat()
    for guild in bot.guilds:
        try:
            await _announce_for_guild(guild, db_path, today_iso)
        except Exception:
            log.exception("birthday: error for guild %s", guild.id)


async def birthday_loop(bot: discord.Client, db_path: Path) -> None:
    """Once per day at 00:00 UTC, announce today's birthdays.

    Runs once on startup as a catch-up pass — if the bot was offline at the
    last 00:00 UTC, today's birthdays still get announced (the persisted
    ``announced_on`` flag prevents double-announcing).
    """
    await bot.wait_until_ready()

    # Startup catch-up — handle any unannounced birthdays for the current
    # UTC day. Idempotent thanks to mark_announced.
    try:
        await _announce_all_guilds(bot, db_path)
    except Exception:
        log.exception("birthday_loop startup pass failed")

    while not bot.is_closed():
        # Sleep until the next 00:00 UTC tick, then run the daily pass.
        now = datetime.now(timezone.utc)
        next_midnight = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        delay = (next_midnight - now).total_seconds()
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise

        try:
            await _announce_all_guilds(bot, db_path)
        except Exception:
            log.exception("birthday_loop daily pass failed")


# ---------------------------------------------------------------------------
# Modal
# ---------------------------------------------------------------------------


class _BirthdayModal(discord.ui.Modal, title="Set Birthday"):
    month: discord.ui.TextInput = discord.ui.TextInput(
        label="Month (1–12)",
        placeholder="e.g. 7 for July",
        min_length=1,
        max_length=2,
    )
    day: discord.ui.TextInput = discord.ui.TextInput(
        label="Day (1–31)",
        placeholder="e.g. 15",
        min_length=1,
        max_length=2,
    )
    preference: discord.ui.TextInput = discord.ui.TextInput(
        label="Birthday request (optional)",
        placeholder="e.g. Ping me with cake reactions!",
        required=False,
        max_length=100,
    )

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self._ctx = ctx

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            m = int(self.month.value.strip())
            d = int(self.day.value.strip())
        except ValueError:
            await interaction.response.send_message(
                "Month and day must be whole numbers.", ephemeral=True
            )
            return

        if not (1 <= m <= 12):
            await interaction.response.send_message(
                "Month must be between 1 and 12.", ephemeral=True
            )
            return

        if not (1 <= d <= _MAX_DAYS[m]):
            await interaction.response.send_message(
                f"{calendar.month_name[m]} has at most {_MAX_DAYS[m]} days.",
                ephemeral=True,
            )
            return

        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                "Set your birthday from inside a server, not a DM.",
                ephemeral=True,
            )
            return

        pref = self.preference.value.strip() or None
        with self._ctx.open_db() as conn:
            _upsert_birthday(conn, guild_id, interaction.user.id, m, d, interaction.user.id, pref)

        await interaction.response.send_message(
            f"Your birthday has been set to **{calendar.month_name[m]} {d}**.",
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class BirthdayCog(commands.Cog):
    birthday = app_commands.Group(
        name="birthday",
        description="Birthday tracker.",
    )

    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    async def cog_load(self) -> None:
        bot = self.bot
        db_path = self.ctx.db_path
        # ``startup_task_factories`` is consumed exactly once during the
        # initial setup_hook (see app_context.Bot). Appending here from a
        # later hot-reload of the cog has no effect — the original
        # birthday_loop, scheduled at boot, keeps running because it only
        # captures ``bot`` and ``db_path``, not this cog instance.
        self.bot.startup_task_factories.append(lambda: birthday_loop(bot, db_path))

    @birthday.command(name="set", description="Set your birthday.")
    async def birthday_set(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(_BirthdayModal(self.ctx))

    @birthday.command(
        name="remove",
        description="Remove your birthday so the bot stops announcing it.",
    )
    async def birthday_remove(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                "Run this from inside a server, not a DM.", ephemeral=True
            )
            return

        with self.ctx.open_db() as conn:
            removed = _delete_birthday(conn, guild_id, interaction.user.id)

        if removed:
            await interaction.response.send_message(
                "Your birthday has been removed.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "You didn't have a birthday on file.", ephemeral=True
            )


async def setup(bot: Bot) -> None:
    await bot.add_cog(BirthdayCog(bot, bot.ctx))
