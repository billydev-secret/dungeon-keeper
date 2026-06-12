"""Bump Tracker cog — remind a role when listing-site cooldowns expire."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.db_utils import open_db

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.bump_tracker")

_TICK_SECONDS = 60
_WIDGET_MIN_INTERVAL = 300  # only refresh widget every 5 min unless forced


# ── DB helpers ────────────────────────────────────────────────────────────────


def _get_config(conn: sqlite3.Connection, guild_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM bump_tracker_config WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()


def _upsert_config(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    channel_id: int | None = None,
    role_id: int | None = None,
    widget_message_id: int | None = None,
    enabled: bool | None = None,
) -> None:
    existing = _get_config(conn, guild_id)
    if existing is None:
        conn.execute(
            """
            INSERT INTO bump_tracker_config (guild_id, channel_id, role_id, widget_message_id, enabled)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                channel_id or 0,
                role_id or 0,
                widget_message_id or 0,
                int(enabled) if enabled is not None else 1,
            ),
        )
    else:
        fields, vals = [], []
        if channel_id is not None:
            fields.append("channel_id = ?")
            vals.append(channel_id)
        if role_id is not None:
            fields.append("role_id = ?")
            vals.append(role_id)
        if widget_message_id is not None:
            fields.append("widget_message_id = ?")
            vals.append(widget_message_id)
        if enabled is not None:
            fields.append("enabled = ?")
            vals.append(int(enabled))
        if fields:
            vals.append(guild_id)
            conn.execute(
                f"UPDATE bump_tracker_config SET {', '.join(fields)} WHERE guild_id = ?",
                vals,
            )


def _add_site(
    conn: sqlite3.Connection,
    guild_id: int,
    site_name: str,
    cooldown_seconds: int,
    *,
    detector_bot_id: int = 0,
    detector_pattern: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO bump_tracker_sites
            (guild_id, site_name, cooldown_seconds, detector_bot_id, detector_pattern)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (guild_id, site_name) DO UPDATE SET
            cooldown_seconds = excluded.cooldown_seconds,
            detector_bot_id  = excluded.detector_bot_id,
            detector_pattern = excluded.detector_pattern
        """,
        (guild_id, site_name, cooldown_seconds, detector_bot_id, detector_pattern),
    )


def _set_detector(
    conn: sqlite3.Connection,
    guild_id: int,
    site_name: str,
    detector_bot_id: int,
    detector_pattern: str,
) -> bool:
    cur = conn.execute(
        """
        UPDATE bump_tracker_sites
        SET detector_bot_id = ?, detector_pattern = ?
        WHERE guild_id = ? AND site_name = ?
        """,
        (detector_bot_id, detector_pattern, guild_id, site_name),
    )
    return cur.rowcount > 0


def _remove_site(conn: sqlite3.Connection, guild_id: int, site_name: str) -> bool:
    cur = conn.execute(
        "DELETE FROM bump_tracker_sites WHERE guild_id = ? AND site_name = ?",
        (guild_id, site_name),
    )
    conn.execute(
        "DELETE FROM bump_tracker_log WHERE guild_id = ? AND site_name = ?",
        (guild_id, site_name),
    )
    return cur.rowcount > 0


def _list_sites(conn: sqlite3.Connection, guild_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT site_name, cooldown_seconds, detector_bot_id, detector_pattern
        FROM bump_tracker_sites WHERE guild_id = ? ORDER BY site_name
        """,
        (guild_id,),
    ).fetchall()


def _get_sites_with_detectors(
    conn: sqlite3.Connection, guild_id: int
) -> list[sqlite3.Row]:
    """Return sites that have a detector bot configured."""
    return conn.execute(
        """
        SELECT site_name, detector_bot_id, detector_pattern
        FROM bump_tracker_sites
        WHERE guild_id = ? AND detector_bot_id != 0
        """,
        (guild_id,),
    ).fetchall()


def _log_bump(conn: sqlite3.Connection, guild_id: int, site_name: str) -> None:
    conn.execute(
        """
        INSERT INTO bump_tracker_log (guild_id, site_name, bumped_at, notified)
        VALUES (?, ?, ?, 0)
        ON CONFLICT (guild_id, site_name) DO UPDATE SET
            bumped_at = excluded.bumped_at,
            notified  = 0
        """,
        (guild_id, site_name, time.time()),
    )


def _mark_notified(conn: sqlite3.Connection, guild_id: int, site_name: str) -> None:
    conn.execute(
        "UPDATE bump_tracker_log SET notified = 1 WHERE guild_id = ? AND site_name = ?",
        (guild_id, site_name),
    )


def _get_all_logs(conn: sqlite3.Connection, guild_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.site_name, s.cooldown_seconds, l.bumped_at, l.notified
        FROM bump_tracker_sites s
        LEFT JOIN bump_tracker_log l ON s.guild_id = l.guild_id AND s.site_name = l.site_name
        WHERE s.guild_id = ?
        ORDER BY s.site_name
        """,
        (guild_id,),
    ).fetchall()


def _get_all_enabled_guilds(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT guild_id, channel_id, role_id, widget_message_id FROM bump_tracker_config WHERE enabled = 1 AND channel_id != 0",
    ).fetchall()


# ── Widget builder ────────────────────────────────────────────────────────────


@dataclass
class _SiteStatus:
    name: str
    cooldown_seconds: int
    bumped_at: float | None
    notified: int

    @property
    def ready(self) -> bool:
        if self.bumped_at is None:
            return True
        return (time.time() - self.bumped_at) >= self.cooldown_seconds

    @property
    def seconds_remaining(self) -> float:
        if self.bumped_at is None:
            return 0.0
        remaining = self.cooldown_seconds - (time.time() - self.bumped_at)
        return max(0.0, remaining)


def _build_widget_embed(statuses: list[_SiteStatus]) -> discord.Embed:
    embed = discord.Embed(
        title="Bump Tracker",
        color=discord.Color.blurple(),
    )
    if not statuses:
        embed.description = "No sites configured. Add sites from the web dashboard."
        return embed

    lines = []
    for s in statuses:
        if s.ready:
            lines.append(f"✅ **{s.name}** — Ready to bump!")
        else:
            total = int(s.seconds_remaining)
            h, rem = divmod(total, 3600)
            m = rem // 60
            if h:
                time_str = f"{h}h {m}m"
            else:
                time_str = f"{m}m"
            lines.append(f"⏰ **{s.name}** — {time_str} remaining")

    embed.description = "\n".join(lines)
    embed.set_footer(text="Use /bump log <site> after bumping to reset the timer.")
    return embed


# ── Background loop ───────────────────────────────────────────────────────────


async def _bump_tracker_loop(bot: discord.Client, db_path: Path) -> None:
    await bot.wait_until_ready()

    last_widget_update: dict[int, float] = {}

    while not bot.is_closed():
        try:
            await _tick(bot, db_path, last_widget_update)
        except Exception:
            log.exception("bump_tracker_loop tick failed")

        await asyncio.sleep(_TICK_SECONDS)


async def _tick(
    bot: discord.Client,
    db_path: Path,
    last_widget_update: dict[int, float],
) -> None:
    def _load():
        with open_db(db_path) as conn:
            guilds = _get_all_enabled_guilds(conn)
            result = []
            for g in guilds:
                logs = _get_all_logs(conn, g["guild_id"])
                result.append((dict(g), logs))
            return result

    guild_data = await asyncio.to_thread(_load)

    for cfg, log_rows in guild_data:
        guild_id = cfg["guild_id"]
        channel_id = cfg["channel_id"]
        role_id = cfg["role_id"]

        statuses = [
            _SiteStatus(
                name=r["site_name"],
                cooldown_seconds=r["cooldown_seconds"],
                bumped_at=r["bumped_at"],
                notified=r["notified"] if r["notified"] is not None else 0,
            )
            for r in log_rows
        ]

        pinged_any = False
        to_notify = [s for s in statuses if s.ready and not s.notified]

        if to_notify:
            channel = bot.get_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                role_mention = f"<@&{role_id}>" if role_id else ""
                site_names = ", ".join(f"**{s.name}**" for s in to_notify)
                content = f"{role_mention} {site_names} {'is' if len(to_notify) == 1 else 'are'} ready to bump!".strip()
                try:
                    await channel.send(content)
                    pinged_any = True
                except discord.HTTPException as exc:
                    log.warning("bump_tracker: failed to send ping in %d: %s", channel_id, exc)

            def _mark_all():
                with open_db(db_path) as conn:
                    for s in to_notify:
                        _mark_notified(conn, guild_id, s.name)

            await asyncio.to_thread(_mark_all)
            for s in to_notify:
                s.notified = 1

        now = time.time()
        stale = (now - last_widget_update.get(guild_id, 0)) >= _WIDGET_MIN_INTERVAL
        if pinged_any or stale:
            await _refresh_widget(bot, db_path, cfg, statuses, last_widget_update, force_resend=pinged_any)


async def _refresh_widget(
    bot: discord.Client,
    db_path: Path,
    cfg: dict,
    statuses: list[_SiteStatus],
    last_widget_update: dict[int, float],
    *,
    force_resend: bool = False,
) -> None:
    guild_id = cfg["guild_id"]
    channel_id = cfg["channel_id"]
    widget_message_id = cfg["widget_message_id"]

    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return

    embed = _build_widget_embed(statuses)

    # Edit in place when nothing new was posted to the channel — avoids
    # firing the unread-message indicator unnecessarily.
    if not force_resend and widget_message_id:
        try:
            old = await channel.fetch_message(widget_message_id)
            await old.edit(embed=embed)
            last_widget_update[guild_id] = time.time()
            return
        except (discord.NotFound, discord.HTTPException):
            pass  # fall through and send a fresh message

    # A new message landed in the channel (ping or detected bump), so the
    # widget needs to move to the bottom.
    if widget_message_id:
        try:
            old = await channel.fetch_message(widget_message_id)
            await old.delete()
        except (discord.NotFound, discord.HTTPException):
            pass

    try:
        msg = await channel.send(embed=embed)
    except discord.HTTPException as exc:
        log.warning("bump_tracker: failed to post widget in %d: %s", channel_id, exc)
        return

    last_widget_update[guild_id] = time.time()

    new_id = msg.id
    if new_id != widget_message_id:
        def _save_id():
            with open_db(db_path) as conn:
                _upsert_config(conn, guild_id, widget_message_id=new_id)

        await asyncio.to_thread(_save_id)


# ── Cog ───────────────────────────────────────────────────────────────────────


class BumpTrackerCog(commands.Cog):
    bump = app_commands.Group(
        name="bump",
        description="Manage listing-site bump reminders.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    async def cog_load(self) -> None:
        bot = self.bot
        db_path = self.ctx.db_path
        self.bot.startup_task_factories.append(lambda: _bump_tracker_loop(bot, db_path))

    # ── autocomplete ──────────────────────────────────────────────────────

    async def _site_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if not interaction.guild:
            return []

        guild_id = interaction.guild.id

        def _q():
            with open_db(self.ctx.db_path) as conn:
                return _list_sites(conn, guild_id)

        rows = await asyncio.to_thread(_q)
        return [
            app_commands.Choice(name=r["site_name"], value=r["site_name"])
            for r in rows
            if current.lower() in r["site_name"].lower()
        ][:25]

    # ── /bump log ─────────────────────────────────────────────────────────

    @bump.command(name="log", description="Record that you just bumped a site.")
    @app_commands.describe(name="Site you bumped.")
    @app_commands.autocomplete(name=_site_autocomplete)
    async def bump_log(
        self,
        interaction: discord.Interaction,
        name: str,
    ) -> None:
        assert interaction.guild is not None
        guild_id = interaction.guild.id

        def _load():
            with open_db(self.ctx.db_path) as conn:
                sites = [r["site_name"] for r in _list_sites(conn, guild_id)]
                if name not in sites:
                    return None, None
                _log_bump(conn, guild_id, name)
                cfg = _get_config(conn, guild_id)
                logs = _get_all_logs(conn, guild_id)
                return cfg, logs

        cfg, log_rows = await asyncio.to_thread(_load)
        if cfg is None or log_rows is None:
            await interaction.response.send_message(
                f"No site named **{name}** found.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"Logged bump for **{name}**. Timer reset!", ephemeral=True
        )

        if not cfg["channel_id"]:
            return

        statuses = [
            _SiteStatus(
                name=r["site_name"],
                cooldown_seconds=r["cooldown_seconds"],
                bumped_at=r["bumped_at"],
                notified=r["notified"] if r["notified"] is not None else 0,
            )
            for r in log_rows
        ]
        await _refresh_widget(self.bot, self.ctx.db_path, dict(cfg), statuses, {}, force_resend=False)

    # ── /bump status ──────────────────────────────────────────────────────

    @bump.command(name="status", description="Show current bump cooldown status.")
    async def bump_status(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        guild_id = interaction.guild.id

        def _q():
            with open_db(self.ctx.db_path) as conn:
                return _get_all_logs(conn, guild_id)

        log_rows = await asyncio.to_thread(_q)
        if not log_rows:
            await interaction.response.send_message(
                "No sites configured. Add sites from the web dashboard.", ephemeral=True
            )
            return

        statuses = [
            _SiteStatus(
                name=r["site_name"],
                cooldown_seconds=r["cooldown_seconds"],
                bumped_at=r["bumped_at"],
                notified=r["notified"] if r["notified"] is not None else 0,
            )
            for r in log_rows
        ]
        embed = _build_widget_embed(statuses)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── on_message — auto-detect bumps ───────────────────────────────────

    @commands.Cog.listener("on_message")
    async def _on_message(self, message: discord.Message) -> None:
        if not message.author.bot:
            return
        if not message.guild:
            return

        guild_id = message.guild.id

        def _load():
            with open_db(self.ctx.db_path) as conn:
                cfg = _get_config(conn, guild_id)
                if cfg is None or not cfg["enabled"] or not cfg["channel_id"]:
                    return None, []
                if message.channel.id != cfg["channel_id"]:
                    return None, []
                sites = _get_sites_with_detectors(conn, guild_id)
                return cfg, sites

        cfg, detector_sites = await asyncio.to_thread(_load)
        if cfg is None or not detector_sites:
            return

        matched_site: str | None = None
        for site in detector_sites:
            if message.author.id != site["detector_bot_id"]:
                continue
            pattern = site["detector_pattern"]
            if pattern:
                content = message.content or ""
                embed_text = " ".join(
                    e.description or "" for e in message.embeds if e.description
                )
                if pattern.lower() not in content.lower() and pattern.lower() not in embed_text.lower():
                    continue
            matched_site = site["site_name"]
            break

        if matched_site is None:
            return

        def _do_log():
            with open_db(self.ctx.db_path) as conn:
                _log_bump(conn, guild_id, matched_site)  # type: ignore[arg-type]
                logs = _get_all_logs(conn, guild_id)
                return logs

        log_rows = await asyncio.to_thread(_do_log)
        log.info("bump_tracker: auto-detected bump for %r in guild %d", matched_site, guild_id)

        statuses = [
            _SiteStatus(
                name=r["site_name"],
                cooldown_seconds=r["cooldown_seconds"],
                bumped_at=r["bumped_at"],
                notified=r["notified"] if r["notified"] is not None else 0,
            )
            for r in log_rows
        ]
        await _refresh_widget(self.bot, self.ctx.db_path, dict(cfg), statuses, {}, force_resend=True)


async def setup(bot: Bot) -> None:
    await bot.add_cog(BumpTrackerCog(bot, bot.ctx))
