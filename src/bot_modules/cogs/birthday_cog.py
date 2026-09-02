"""Birthday tracker — users set their own birthday; bot announces on the day."""

from __future__ import annotations

import asyncio
import calendar
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.db_utils import (
    get_tz_offset_hours,
    open_db,
)
from bot_modules.services.birthday_service import (
    announce_hour as _announce_hour,
    clear_pin as _clear_pin,
    delete_birthday as _delete_birthday,
    get_birthday_preference as _get_birthday_preference,
    list_channels as _list_channels,
    mark_announced as _mark_announced,
    month_choices as _month_choices,
    parse_birthday_day as _parse_birthday_day,
    pins_before as _pins_before,
    record_pin as _record_pin,
    todays_unannounced as _todays_unannounced,
    upsert_birthday as _upsert_birthday,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.birthday")

# Announce in each guild's local time (per its ``tz_offset_hours``), at the
# hour that guild set on the Birthdays panel (``birthday_announce_hour``,
# default 09:00). The loop ticks hourly and fires once the local clock has
# reached that hour; the persisted announcement row keeps it to one send per
# local day.


def _render(template: str, *, mention: str, name: str, request: str) -> str:
    """Substitute the birthday placeholders and tidy up empty-request artifacts.

    ``{request}`` is blank when the member set no request, so a placeholder on
    its own line (or trailing one) would otherwise leave a dangling blank line
    or trailing space. We rstrip each line and drop the ones that end up empty.
    """
    text = (
        template.replace("{mention}", mention)
        .replace("{name}", name)
        .replace("{request}", request)
    )
    lines = [ln.rstrip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------


async def _unpin_due_for_guild(
    guild: discord.Guild, db_path: Path, today_iso: str
) -> None:
    """Unpin birthday messages pinned on a previous day (~24h cleanup).

    Runs independently of whether anyone has a birthday today, so a pin from a
    quiet day still clears on the next daily pass rather than lingering until
    the next birthday.
    """
    with open_db(db_path) as conn:
        due = _pins_before(conn, guild.id, today_iso)
    if not due:
        return

    me = guild.me
    for channel_id, message_id in due:
        channel = guild.get_channel(channel_id)
        if (
            isinstance(channel, discord.TextChannel)
            and me is not None
            and channel.permissions_for(me).manage_messages
        ):
            try:
                msg = await channel.fetch_message(message_id)
                await msg.unpin(reason="Birthday pin expired (next-day cleanup)")
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                log.warning(
                    "birthday: could not unpin message %s in channel %s",
                    message_id, channel_id,
                )
        # Always drop the row — whether we unpinned it, it was already gone, or
        # the channel/permission is unavailable. Keeping it would mean retrying
        # a doomed unpin on every daily pass forever.
        with open_db(db_path) as conn:
            _clear_pin(conn, guild.id, channel_id, message_id)


async def _announce_for_guild(
    guild: discord.Guild, db_path: Path, today_iso: str
) -> None:
    month = int(today_iso[5:7])
    day = int(today_iso[8:10])

    with open_db(db_path) as conn:
        configs = _list_channels(conn, guild.id)
        if not configs:
            return
        unannounced = _todays_unannounced(conn, guild.id, month, day, today_iso)

    if not unannounced:
        return

    me = guild.me
    for user_id in unannounced:
        member = guild.get_member(user_id)
        mention = member.mention if member else f"<@{user_id}>"
        name = member.display_name if member else "Someone"
        with open_db(db_path) as conn:
            request = _get_birthday_preference(conn, guild.id, user_id) or ""

        for cfg in configs:
            channel_id, pin = cfg.channel_id, cfg.pin
            channel = guild.get_channel(channel_id)
            if channel is None or not isinstance(channel, discord.TextChannel):
                continue
            text = _render(cfg.message, mention=mention, name=name, request=request)
            if not text:
                continue  # degenerate template (e.g. just {request} with none set)
            try:
                sent = await channel.send(
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
                continue

            if pin and me is not None and channel.permissions_for(me).manage_messages:
                try:
                    await sent.pin(reason="Birthday announcement")
                    with open_db(db_path) as conn:
                        _record_pin(conn, guild.id, channel_id, sent.id, today_iso)
                except (discord.Forbidden, discord.HTTPException):
                    log.warning(
                        "birthday: failed to pin message in guild %s channel %s",
                        guild.id, channel_id,
                    )

        # Always mark announced — once we've attempted today's send for a user,
        # we don't want to keep retrying every tick. Send failures show up in
        # the log; a permanently broken channel is an operator config issue,
        # not something we should keep hammering.
        with open_db(db_path) as conn:
            _mark_announced(conn, guild.id, user_id, today_iso)


async def _announce_all_guilds(bot: discord.Client, db_path: Path) -> None:
    """Run today's unpin cleanup + announcement pass across every guild.

    Each guild's "today" is its *local* calendar day, derived from the configured
    ``tz_offset_hours`` (the same offset reports/games/jail honor). Announcements
    are held until the local clock passes the guild's configured announce hour;
    the unpin cleanup runs every tick so a previous day's pin still clears at
    the start of the new local day.
    """
    now_utc = datetime.now(timezone.utc)
    for guild in bot.guilds:
        with open_db(db_path) as conn:
            offset = get_tz_offset_hours(conn, guild.id)
            hour_gate = _announce_hour(conn, guild.id)
        local_now = now_utc + timedelta(hours=offset)
        today_iso = local_now.date().isoformat()

        try:
            await _unpin_due_for_guild(guild, db_path, today_iso)
        except Exception:
            log.exception("birthday: unpin error for guild %s", guild.id)

        if local_now.hour < hour_gate:
            continue  # before the local announce hour — a later tick handles it

        try:
            await _announce_for_guild(guild, db_path, today_iso)
        except Exception:
            log.exception("birthday: error for guild %s", guild.id)


async def birthday_loop(bot: discord.Client, db_path: Path) -> None:
    """Tick hourly; announce each guild's birthdays at its local announce hour.

    The hourly cadence lets a single loop serve guilds in different timezones —
    each pass computes the guild-local day/hour from its ``tz_offset_hours`` and
    only announces once the local clock reaches that guild's
    ``birthday_announce_hour`` (default 09:00). The first pass runs on startup
    as a catch-up: if the bot was offline across a guild's announce hour,
    today's birthdays still go out (the persisted announcement row prevents
    double-announcing).
    """
    await bot.wait_until_ready()

    # Startup catch-up — handle any still-unannounced birthdays for each guild's
    # current local day. Idempotent thanks to mark_announced.
    try:
        await _announce_all_guilds(bot, db_path)
    except Exception:
        log.exception("birthday_loop startup pass failed")

    while not bot.is_closed():
        # Sleep until the top of the next hour, then run the pass.
        now = datetime.now(timezone.utc)
        next_hour = (now + timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0
        )
        delay = (next_hour - now).total_seconds()
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise

        try:
            await _announce_all_guilds(bot, db_path)
        except Exception:
            log.exception("birthday_loop hourly pass failed")


# ---------------------------------------------------------------------------
# Modal
# ---------------------------------------------------------------------------


class _BirthdayModal(discord.ui.Modal, title="Set Birthday"):
    """Month is picked, day and request are typed.

    Twelve months fit a select comfortably, so the month stopped being a
    number to type — and the "must be between 1 and 12" error it produced
    stopped existing. The day stays a text box: 31 values overflow
    Discord's 25-option select cap, and its valid range depends on which
    month was chosen anyway.
    """

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self._ctx = ctx
        self.month: discord.ui.Select = discord.ui.Select(
            placeholder="Pick your birth month",
            options=[
                discord.SelectOption(label=name, value=str(number))
                for name, number in _month_choices()
            ],
        )
        self.day: discord.ui.TextInput = discord.ui.TextInput(
            placeholder="e.g. 15", min_length=1, max_length=2
        )
        self.preference: discord.ui.TextInput = discord.ui.TextInput(
            placeholder="e.g. Ping me with cake reactions!",
            required=False,
            max_length=100,
        )
        self.add_item(discord.ui.Label(text="Month", component=self.month))
        self.add_item(discord.ui.Label(text="Day (1–31)", component=self.day))
        self.add_item(
            discord.ui.Label(
                text="Birthday request (optional)", component=self.preference
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        m = int(self.month.values[0])
        d, day_err = _parse_birthday_day(str(self.day.value), m)
        if day_err is not None or d is None:
            await interaction.response.send_message(
                day_err or "❌ Day must be a whole number.", ephemeral=True
            )
            return

        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                "❌ Set your birthday from inside a server, not a DM.",
                ephemeral=True,
            )
            return

        pref = self.preference.value.strip() or None
        gid = guild_id
        user_id = interaction.user.id

        def _do_upsert_birthday():
            with self._ctx.open_db() as conn:
                _upsert_birthday(conn, gid, user_id, m, d, user_id, pref)

        await asyncio.to_thread(_do_upsert_birthday)

        await interaction.response.send_message(
            f"Your birthday has been set to **{calendar.month_name[m]} {d}**.",
            ephemeral=True,
        )

        # Quest hook: the bio_set pattern — occurrence "set" makes an event
        # quest pay once ever; updates collide on the same key. Guarded,
        # never raised into the modal flow.
        from bot_modules.economy.game_rewards import fire_member_trigger  # noqa: PLC0415

        await fire_member_trigger(
            cast("Bot", interaction.client), gid, user_id, "birthday_set",
            occurrence="set",
        )


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class BirthdayCog(commands.Cog):
    birthday = app_commands.Group(
        name="birthday",
        description="Birthday tracker.",
    )

    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        super().__init__()

    async def cog_load(self) -> None:
        bot = self.bot
        db_path = self.bot.ctx.db_path
        # ``startup_task_factories`` is consumed exactly once during the
        # initial setup_hook (see app_context.Bot). Appending here from a
        # later hot-reload of the cog has no effect — the original
        # birthday_loop, scheduled at boot, keeps running because it only
        # captures ``bot`` and ``db_path``, not this cog instance.
        self.bot.startup_task_factories.append(lambda: birthday_loop(bot, db_path))

    @birthday.command(name="set", description="Set your birthday.")
    async def birthday_set(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(_BirthdayModal(self.bot.ctx))

    @birthday.command(
        name="remove",
        description="Remove your birthday so the bot stops announcing it.",
    )
    async def birthday_remove(self, interaction: discord.Interaction) -> None:
        await self.remove_impl(interaction)

    async def remove_impl(self, interaction: discord.Interaction) -> None:
        """Shared by ``/birthday remove`` and the ``/info`` panel's button."""
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                "❌ Run this from inside a server, not a DM.", ephemeral=True
            )
            return

        gid = guild_id
        user_id = interaction.user.id

        def _do_delete_birthday():
            with self.bot.ctx.open_db() as conn:
                return _delete_birthday(conn, gid, user_id)

        removed = await asyncio.to_thread(_do_delete_birthday)

        if removed:
            await interaction.response.send_message(
                "Your birthday has been removed.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "You didn't have a birthday on file.", ephemeral=True
            )


async def setup(bot: Bot) -> None:
    await bot.add_cog(BirthdayCog(bot))
