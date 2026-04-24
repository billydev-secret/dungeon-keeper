"""Birthday tracker — mods set birthdays for members; bot announces on the day."""

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

from db_utils import get_config_value, open_db, set_config_value
from services.birthday_service import (
    MAX_DAYS as _MAX_DAYS,
    delete_birthday as _delete_birthday,
    list_all_birthdays as _list_all_birthdays,
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

    def __init__(self, ctx: AppContext, member: discord.Member) -> None:
        super().__init__()
        self._ctx = ctx
        self._member = member

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
            _upsert_birthday(conn, guild_id, self._member.id, m, d, interaction.user.id)

        await interaction.response.send_message(
            f"{self._member.mention}'s birthday set to **{calendar.month_name[m]} {d}**.",
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

    @birthday.command(name="set", description="Set a member's birthday (mod only).")
    @app_commands.describe(member="Member whose birthday to record.")
    async def birthday_set(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        if not self.ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You need mod permissions to set a birthday.", ephemeral=True
            )
            return
        await interaction.response.send_modal(_BirthdayModal(self.ctx, member))

    @birthday.command(
        name="clear",
        description="Remove a birthday. Leave blank to clear your own; mods can clear any member.",
    )
    @app_commands.describe(
        member="Member whose birthday to remove (mod only; omit to clear your own)."
    )
    async def birthday_clear(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        ctx = self.ctx
        guild_id = interaction.guild_id
        if guild_id is None:
            return

        clearing_other = member is not None and member.id != interaction.user.id
        if clearing_other and not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You need mod permissions to clear someone else's birthday.",
                ephemeral=True,
            )
            return

        target_id = member.id if member is not None else interaction.user.id
        with ctx.open_db() as conn:
            removed = _delete_birthday(conn, guild_id, target_id)

        if removed:
            label = member.mention if member is not None else "Your"
            await interaction.response.send_message(
                f"{label} birthday has been removed.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "No birthday found to remove.", ephemeral=True
            )

    @birthday.command(name="list", description="List all registered birthdays (mod only).")
    async def birthday_list(self, interaction: discord.Interaction) -> None:
        ctx = self.ctx
        guild = interaction.guild
        guild_id = interaction.guild_id
        if guild is None or guild_id is None:
            return

        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You need mod permissions to view the birthday list.", ephemeral=True
            )
            return

        with ctx.open_db() as conn:
            rows = _list_all_birthdays(conn, guild_id)

        if not rows:
            await interaction.response.send_message(
                "No birthdays registered yet.", ephemeral=True
            )
            return

        lines: list[str] = []
        for user_id, month, day in rows:
            member = guild.get_member(user_id)
            name = member.display_name if member else f"<@{user_id}>"
            lines.append(f"**{name}** — {calendar.month_name[month]} {day}")

        body = "\n".join(lines)
        if len(body) > 4000:
            body = body[:4000] + "\n…"

        embed = discord.Embed(
            title=f"Birthdays ({len(rows)})",
            description=body,
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @birthday.command(
        name="channel",
        description="Set the channel for birthday announcements (mod only).",
    )
    @app_commands.describe(channel="Channel where birthday messages will be posted.")
    async def birthday_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        if not self.ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You need mod permissions to configure birthdays.", ephemeral=True
            )
            return
        guild_id = interaction.guild_id
        if guild_id is None:
            return
        with self.ctx.open_db() as conn:
            set_config_value(conn, "birthday_channel_id", str(channel.id), guild_id)
        await interaction.response.send_message(
            f"Birthday announcements will post in {channel.mention}.", ephemeral=True
        )

    @birthday.command(
        name="message",
        description="Set the birthday announcement template (mod only). Use {mention} for the member.",
    )
    @app_commands.describe(
        message="Template text. Use {mention} where the member's ping should appear."
    )
    async def birthday_message(
        self, interaction: discord.Interaction, message: str
    ) -> None:
        if not self.ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You need mod permissions to configure birthdays.", ephemeral=True
            )
            return
        guild_id = interaction.guild_id
        if guild_id is None:
            return
        with self.ctx.open_db() as conn:
            set_config_value(conn, "birthday_message", message, guild_id)
        await interaction.response.send_message(
            f"Birthday message updated to: {message}", ephemeral=True
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(BirthdayCog(bot, bot.ctx))
