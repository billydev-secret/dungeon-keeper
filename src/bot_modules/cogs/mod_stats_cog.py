"""The moderator stats panel — server activity, sticky in a mod channel.

A read-only board: today's traffic drawn against the last 8 days and the last
30, with a short volume-and-pace block underneath. It is a *report* delivered
where the mod team already is, on the theory that a chart nobody opens the
dashboard for is a chart nobody reads.

Everything configurable about it lives on the dashboard (Reports → Activity),
per CLAUDE.md: there is no slash command, and the only setting is which channel
it goes in. Posting runs through ``services/panel_registry.py`` and the shared
``POST /api/panels/{key}/post`` route like every other channel panel.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import time, timezone
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks

from bot_modules.core.bot_exclusion import bot_ids_subquery
from bot_modules.core.branding import safe_resolve_accent
from bot_modules.core.db_utils import (
    get_config_value,
    get_tz_offset_hours,
    set_config_value,
)
from bot_modules.core.sticky import PanelContent, PanelImage, StickyPanel
from bot_modules.services.activity_graphs import (
    OverlayChart,
    OverlayResult,
    render_overlay_panel,
)
from bot_modules.services.mod_stats_service import (
    FAR_WINDOW_DAYS,
    NEAR_WINDOW_DAYS,
    ModStatsData,
    build_mod_stats,
    render_description,
    render_stats_lines,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger(__name__)

CHANNEL_KEY = "mod_stats_panel_channel_id"
MESSAGE_KEY = "mod_stats_panel_message_id"

IMAGE_FILENAME = "activity.png"

#: On the hour, every hour. A list of times rather than ``hours=1`` because an
#: interval loop fires an hour after *boot*, so a restart at 14:20 would leave
#: the panel repainting at :20 past forever. The charts bucket by hour, so this
#: is the finest cadence that changes anything. Guilds on a whole-hour offset
#: (the ones this bot serves) see the repaint land on their own hour boundary.
_REFRESH_TIMES = [time(hour=h, tzinfo=timezone.utc) for h in range(24)]


class ModStatsCog(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self.panel = StickyPanel(
            "mod stats panel",
            bot,
            load_ids=self._read_ids,
            save_ids=self._write_ids,
            build=self.build_panel,
        )
        # guild → (data signature, rendered PNG). A sticky repost rebuilds the
        # panel, and a busy mod channel can do that every few seconds; without
        # this each one would re-run matplotlib for a picture identical to the
        # one already on screen. Rendering is ~100ms of CPU under a process-wide
        # lock shared with every other chart in the bot, so it is worth not
        # paying twice.
        self._renders: dict[int, tuple[tuple[object, ...], bytes]] = {}
        super().__init__()

    async def cog_load(self) -> None:
        self.refresh_loop.start()

    async def cog_unload(self) -> None:
        self.refresh_loop.cancel()
        self.panel.cancel_all()

    # ── stored ids ───────────────────────────────────────────────────────

    def _read_ids(self, guild_id: int) -> tuple[int, int]:
        with self.bot.ctx.open_db() as conn:
            return self._read_ids_conn(conn, guild_id)

    @staticmethod
    def _read_ids_conn(conn: sqlite3.Connection, guild_id: int) -> tuple[int, int]:
        def _int(key: str) -> int:
            # Strictly guild-scoped: these are ids for one server's message in
            # one server's channel, and the legacy guild_id=0 fallback would
            # hand a second guild the home guild's panel to edit.
            raw = get_config_value(
                conn, key, "0", guild_id, allow_legacy_fallback=False
            )
            try:
                return int(raw)
            except (TypeError, ValueError):
                return 0

        return _int(CHANNEL_KEY), _int(MESSAGE_KEY)

    def _write_ids(self, guild_id: int, channel_id: int, message_id: int) -> None:
        with self.bot.ctx.open_db() as conn:
            set_config_value(conn, CHANNEL_KEY, str(channel_id or 0), guild_id)
            set_config_value(conn, MESSAGE_KEY, str(message_id or 0), guild_id)

    def _panel_guilds(self) -> set[int]:
        with self.bot.ctx.open_db() as conn:
            return {
                int(row[0])
                for row in conn.execute(
                    "SELECT guild_id FROM config WHERE key = ? AND value NOT IN ('', '0')",
                    (CHANNEL_KEY,),
                ).fetchall()
            }

    # ── the panel itself ─────────────────────────────────────────────────

    def _read_data(self, guild_id: int) -> ModStatsData:
        with self.bot.ctx.open_db() as conn:
            tz = get_tz_offset_hours(conn, guild_id)
            # Bots excluded, matching the Activity report's own default. Read
            # from known_users rather than live guild members so a bot that has
            # since left the server is still excluded from its own history.
            bot_ids = {
                int(row[0])
                for row in conn.execute(bot_ids_subquery(), (guild_id,)).fetchall()
            }
            return build_mod_stats(
                conn,
                guild_id,
                utc_offset_hours=tz,
                exclude_user_ids=bot_ids or None,
            )

    def _render(self, guild_id: int, data: ModStatsData) -> bytes:
        signature = data.signature
        cached = self._renders.get(guild_id)
        if cached is not None and cached[0] == signature:
            return cached[1]
        png = render_overlay_panel(
            [
                _chart(f"Today vs the last {NEAR_WINDOW_DAYS} days", data.near),
                _chart(f"Today vs the last {FAR_WINDOW_DAYS} days", data.far),
            ]
        )
        self._renders[guild_id] = (signature, png)
        return png

    async def build_panel(self, guild: discord.Guild) -> PanelContent:
        data = await asyncio.to_thread(self._read_data, guild.id)
        # Both the SQL and matplotlib are blocking, but they are two threads
        # rather than one: the render is skipped entirely on a cache hit, and
        # bundling them would hold a worker for the query either way.
        png = await asyncio.to_thread(self._render, guild.id, data)
        image = PanelImage(filename=IMAGE_FILENAME, data=png)

        accent = await safe_resolve_accent(
            self.bot.ctx, guild, log_label="mod stats"
        )
        # Everything readable goes in the description. Discord always renders
        # an embed's image *below* its fields, so a field cannot sit under the
        # charts however it is declared — and the numbers are the headline the
        # charts are evidence for, so above them is where they belong anyway.
        embed = discord.Embed(
            title="📈 Server Activity",
            description=(
                f"{render_description(data)}\n\n{render_stats_lines(data)}"
            ),
            color=accent,
        )
        embed.set_image(url=image.attachment_url)
        embed.set_footer(text="Bots excluded · updates every hour")
        return PanelContent(
            embed=embed,
            signature=data.signature,
            image=image,
        )

    # ── the registry's entry point, and the loop ─────────────────────────

    async def post_mod_stats_panel(
        self, guild: discord.Guild, channel: discord.TextChannel
    ) -> discord.Message | discord.PartialMessage | None:
        message = await self.panel.place_or_refresh(guild, channel)
        if message is not None:
            self.panel.set_known_guilds(await asyncio.to_thread(self._panel_guilds))
        return message

    # ``repost_if_missing=False`` is how the panel is **removed**: the shared
    # post control has no Remove button, so deleting the message is the only
    # gesture staff have — and a healing refresh would put it straight back at
    # the top of the hour, forever. Retiring instead clears the stored ids, and
    # posting again from the dashboard is the way back. Nothing is lost by it:
    # this panel is a read-only chart, so a deleted one costs discoverability
    # rather than a capability the way a button-carrying board would.
    @tasks.loop(time=_REFRESH_TIMES)
    async def refresh_loop(self) -> None:
        guilds = await asyncio.to_thread(self._panel_guilds)
        # Republish every tick: the set changes when an admin posts or removes a
        # panel from the dashboard, and the listener's fast path is only as
        # good as the last time it was told.
        self.panel.set_known_guilds(guilds)
        # take_retries drained exactly once, and only into guilds that still
        # have a panel — an undrained queue is how pen pals lost its failed
        # edits (docs/reviews/2026-08-06-sticky-panel-machinery.md).
        for guild_id in sorted(guilds | (self.panel.take_retries() & guilds)):
            try:
                await self.panel.refresh(guild_id, repost_if_missing=False)
            except Exception:
                log.exception("mod stats panel refresh failed for %s", guild_id)

    @refresh_loop.before_loop
    async def _before_refresh(self) -> None:
        await self.bot.wait_until_ready()
        # One pass on boot. Without it a bot restarted at 14:20 shows numbers
        # from before the restart until 15:00, and a panel left behind by a
        # previous release never gets repainted at all.
        try:
            guilds = await asyncio.to_thread(self._panel_guilds)
            self.panel.set_known_guilds(guilds)
            for guild_id in sorted(guilds):
                await self.panel.refresh(guild_id, repost_if_missing=False)
        except Exception:
            log.exception("mod stats panel boot refresh failed")

    # ── sticky behaviour ─────────────────────────────────────────────────

    @commands.Cog.listener("on_message")
    async def _restick_panel(self, message: discord.Message) -> None:
        await self.panel.on_message(message)

    @commands.Cog.listener("on_guild_channel_delete")
    async def _forget_deleted_channel(
        self, channel: discord.abc.GuildChannel
    ) -> None:
        await self.panel.on_channel_delete(channel)


def _chart(title: str, result: OverlayResult) -> OverlayChart:
    """One overlay's series, dressed for the renderer."""
    return OverlayChart(
        title=title,
        labels=result.labels,
        # current_smooth is empty for a day overlay (OVERLAY_SMOOTH_WINDOW),
        # but read it when it is there so this panel and the dashboard chart
        # can never draw the same series two different ways.
        current=list(result.current_smooth or result.current),
        band_low=list(result.band_low),
        band_mid=list(result.band_mid),
        band_high=list(result.band_high),
        empty_note="Not enough history to compare against yet.",
    )


async def setup(bot: Bot) -> None:
    await bot.add_cog(ModStatsCog(bot))
