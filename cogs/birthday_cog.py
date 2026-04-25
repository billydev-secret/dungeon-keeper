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

from db_utils import get_config_value, open_db
from services.birthday_service import (
    MAX_DAYS as _MAX_DAYS,
    mark_announced as _mark_announced,
    todays_unannounced as _todays_unannounced,
    upsert_birthday as _upsert_birthday,
)

if TYPE_CHECKING:
    from app_context import AppContext, Bot

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
        try:
            await channel.send(
                text,
                allowed_mentions=discord.AllowedMentions(
                    users=[member] if member else False,
                    roles=False,
                    everyone=False,
                ),
            )
            # Mark after successful send so a failed send retries next tick.
            with open_db(db_path) as conn:
                _mark_announced(conn, guild.id, user_id, today_iso)
        except (discord.Forbidden, discord.HTTPException):
            log.warning(
                "birthday: failed to post in guild %s channel %s", guild.id, channel_id
            )


async def birthday_loop(bot: discord.Client, db_path: Path) -> None:
    """Every 5 minutes: announce today's birthdays at or after 9am server time."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            for guild in bot.guilds:
                try:
                    with open_db(db_path) as conn:
                        tz_raw = get_config_value(conn, "tz_offset_hours", "0", guild.id)
                    tz_offset = float(tz_raw or "0")
                    now_local = datetime.now(timezone.utc) + timedelta(hours=tz_offset)
                    if now_local.hour >= 9:
                        await _announce_for_guild(
                            guild, db_path, now_local.date().isoformat()
                        )
                except Exception:
                    log.exception("birthday: error for guild %s", guild.id)
        except Exception:
            log.exception("birthday_loop top-level error")
        await asyncio.sleep(300)


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
            return

        with self._ctx.open_db() as conn:
            _upsert_birthday(conn, guild_id, interaction.user.id, m, d, interaction.user.id)

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
        self.bot.startup_task_factories.append(lambda: birthday_loop(bot, db_path))

    @birthday.command(name="set", description="Set your birthday.")
    async def birthday_set(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(_BirthdayModal(self.ctx))


async def setup(bot: Bot) -> None:
    await bot.add_cog(BirthdayCog(bot, bot.ctx))
