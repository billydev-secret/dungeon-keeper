"""Birthday tracker — mods set birthdays for members; bot announces on the day."""

from __future__ import annotations

import asyncio
import calendar
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from db_utils import get_config_value, open_db, set_config_value

if TYPE_CHECKING:
    from app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.birthday")

_DEFAULT_MESSAGE = "Happy birthday, {mention}! 🎂"
# Max valid day per month; Feb capped at 28 (Feb 29 would silently skip 3/4 years)
_MAX_DAYS = [0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _upsert_birthday(
    conn, guild_id: int, user_id: int, month: int, day: int, set_by: int
) -> None:
    conn.execute(
        """
        INSERT INTO member_birthdays (guild_id, user_id, birth_month, birth_day, set_by, set_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            birth_month = excluded.birth_month,
            birth_day   = excluded.birth_day,
            set_by      = excluded.set_by,
            set_at      = excluded.set_at
        """,
        (guild_id, user_id, month, day, set_by, time.time()),
    )


def _delete_birthday(conn, guild_id: int, user_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM member_birthdays WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    return (cur.rowcount or 0) > 0


def _list_all_birthdays(conn, guild_id: int) -> list[tuple[int, int, int]]:
    rows = conn.execute(
        "SELECT user_id, birth_month, birth_day FROM member_birthdays "
        "WHERE guild_id = ? ORDER BY birth_month, birth_day",
        (guild_id,),
    ).fetchall()
    return [(row["user_id"], row["birth_month"], row["birth_day"]) for row in rows]


def _todays_unannounced(
    conn, guild_id: int, month: int, day: int, date_iso: str
) -> list[int]:
    """Return user_ids whose birthday is today and haven't been announced yet."""
    rows = conn.execute(
        """
        SELECT b.user_id
        FROM member_birthdays b
        LEFT JOIN birthday_announcements a
            ON a.guild_id = b.guild_id AND a.user_id = b.user_id AND a.announced_date = ?
        WHERE b.guild_id = ? AND b.birth_month = ? AND b.birth_day = ? AND a.user_id IS NULL
        """,
        (date_iso, guild_id, month, day),
    ).fetchall()
    return [row["user_id"] for row in rows]


def _mark_announced(conn, guild_id: int, user_id: int, date_iso: str) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO birthday_announcements (guild_id, user_id, announced_date) VALUES (?, ?, ?)",
        (guild_id, user_id, date_iso),
    )
    return (cur.rowcount or 0) > 0


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
