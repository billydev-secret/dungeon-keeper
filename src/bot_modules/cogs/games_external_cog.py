"""Collect results from an external game bot (e.g. "Gamebot" Cards Against
Humanity) so we can build our own leaderboards/streaks over games we don't run.

Design (per review): a format-agnostic collector. An on_message listener scoped
to one configured channel + bot user banks every watched message RAW into
games_external_messages, keyed on message_id so restarts/edits/backfills all
de-duplicate. Nothing is parsed here — metrics are derived later from the raw
table, so re-parsing on a format change never loses history.
"""
from __future__ import annotations

import io
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.games.command_groups import games
from bot_modules.games_config.logic import has_mod_or_admin_permissions
from bot_modules.games_external import logic

log = logging.getLogger(__name__)


def is_mod_or_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        return has_mod_or_admin_permissions(interaction.user.guild_permissions)

    return app_commands.check(predicate)


class GamesExternalCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot
        # guild_id -> (channel_id, bot_user_id). Warmed on load; kept in sync by
        # the config commands so the on_message hot path never touches the DB.
        self._watch: dict[int, tuple[int, int]] = {}

    @property
    def db(self):
        return self.bot.games_db

    async def cog_load(self) -> None:
        try:
            for row in await logic.load_all_watches(self.db):
                self._watch[int(row["guild_id"])] = (
                    int(row["channel_id"]),
                    int(row["bot_user_id"]),
                )
            if self._watch:
                log.info("External game tracking: watching %d guild(s)", len(self._watch))
        except Exception:
            log.exception("External game tracking: failed to warm watch cache")

    # ── collection ────────────────────────────────────────────────────────
    def _is_watched(self, message: discord.Message) -> bool:
        if message.guild is None:
            return False
        cfg = self._watch.get(message.guild.id)
        if cfg is None:
            return False
        channel_id, bot_user_id = cfg
        return message.channel.id == channel_id and message.author.id == bot_user_id

    async def _capture(self, message: discord.Message) -> None:
        try:
            await logic.store_message(self.db, message)
        except Exception:
            log.exception("External game tracking: failed to store message %s", message.id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if self._is_watched(message):
            await self._capture(message)

    @commands.Cog.listener()
    async def on_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> None:
        # Gamebot posts "Loading…" then edits in the real embed — re-capture so
        # we keep the final content, not the placeholder.
        if self._is_watched(after):
            await self._capture(after)

    # ── config commands: /games track … ───────────────────────────────────
    track = app_commands.Group(
        name="track",
        description="Track results from an external game bot (mods only).",
    )

    @track.command(name="watch", description="Watch a channel + bot and start banking its game results.")
    @is_mod_or_admin()
    @app_commands.describe(
        channel="The channel the external game bot posts results in.",
        bot="The external game bot to track (e.g. Gamebot).",
    )
    async def track_watch(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        bot: discord.User,
    ):
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        if not bot.bot:
            await interaction.response.send_message(
                f"⚠️ {bot.mention} isn't a bot account. Pick the game bot itself "
                "(the one that posts the results).",
                ephemeral=True,
            )
            return
        await logic.set_watch(
            self.db, interaction.guild.id, channel.id, bot.id, interaction.user.id
        )
        self._watch[interaction.guild.id] = (channel.id, bot.id)
        log.info(
            "External game tracking enabled by %s: #%s watching bot %s",
            interaction.user.display_name, channel.name, bot.id,
        )
        await interaction.response.send_message(
            f"✅ Now banking {bot.mention}'s messages in {channel.mention}. "
            f"Run `/games track sample` after a game or two to confirm the format.",
            ephemeral=True,
        )

    @track.command(name="status", description="Show external game-tracking status for this server.")
    @is_mod_or_admin()
    async def track_status(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        row = await logic.get_watch(self.db, interaction.guild.id)
        if row is None:
            await interaction.response.send_message(
                "No external game bot is being tracked. Use `/games track watch`.",
                ephemeral=True,
            )
            return
        n = await logic.count_messages(self.db, interaction.guild.id)
        state = "enabled" if row["enabled"] else "disabled (paused)"
        await interaction.response.send_message(
            f"**External game tracking** — {state}\n"
            f"Channel: <#{row['channel_id']}>\n"
            f"Bot: <@{row['bot_user_id']}>\n"
            f"Messages banked: **{n}**",
            ephemeral=True,
        )

    @track.command(name="disable", description="Pause banking (keeps all data).")
    @is_mod_or_admin()
    async def track_disable(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        ok = await logic.set_watch_enabled(self.db, interaction.guild.id, False)
        self._watch.pop(interaction.guild.id, None)
        msg = "⏸️ Paused external game tracking." if ok else "Nothing was being tracked."
        await interaction.response.send_message(msg, ephemeral=True)

    @track.command(name="enable", description="Resume banking a previously-configured bot.")
    @is_mod_or_admin()
    async def track_enable(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        ok = await logic.set_watch_enabled(self.db, interaction.guild.id, True)
        if not ok:
            await interaction.response.send_message(
                "Nothing configured yet — use `/games track watch` first.",
                ephemeral=True,
            )
            return
        row = await logic.get_watch(self.db, interaction.guild.id)
        if row:
            self._watch[interaction.guild.id] = (
                int(row["channel_id"]), int(row["bot_user_id"])
            )
        await interaction.response.send_message("▶️ Resumed external game tracking.", ephemeral=True)

    @track.command(name="sample", description="Dump recent bot messages (raw content + embeds) to confirm the format.")
    @is_mod_or_admin()
    @app_commands.describe(
        channel="Channel to sample (defaults to the watched channel).",
        count="How many recent messages to scan (1–100, default 40).",
    )
    async def track_sample(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        count: app_commands.Range[int, 1, 100] = 40,
    ):
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        target = channel
        watched_bot: int | None = None
        if target is None:
            row = await logic.get_watch(self.db, interaction.guild.id)
            if row is None:
                await interaction.followup.send(
                    "No watched channel — pass one with the `channel` option.",
                    ephemeral=True,
                )
                return
            watched_bot = int(row["bot_user_id"])
            ch = interaction.guild.get_channel(int(row["channel_id"]))
            if not isinstance(ch, discord.TextChannel):
                await interaction.followup.send(
                    "Watched channel is missing or not a text channel.", ephemeral=True
                )
                return
            target = ch

        dumped = []
        try:
            async for msg in target.history(limit=count):
                if not msg.author.bot:
                    continue
                if watched_bot is not None and msg.author.id != watched_bot:
                    continue
                dumped.append(
                    {
                        "message_id": msg.id,
                        "author": f"{msg.author} ({msg.author.id})",
                        "created_at": msg.created_at.isoformat(),
                        "content": msg.content,
                        "embeds": [e.to_dict() for e in msg.embeds],
                    }
                )
        except discord.Forbidden:
            await interaction.followup.send(
                f"I can't read history in {target.mention} (missing permission).",
                ephemeral=True,
            )
            return

        if not dumped:
            await interaction.followup.send(
                f"No bot messages found in the last {count} messages of {target.mention}.",
                ephemeral=True,
            )
            return

        blob = json.dumps(dumped, indent=2, ensure_ascii=False)
        file = discord.File(
            io.BytesIO(blob.encode("utf-8")), filename="gamebot_sample.json"
        )
        await interaction.followup.send(
            f"Dumped **{len(dumped)}** bot message(s) from {target.mention}.",
            file=file,
            ephemeral=True,
        )

    @track_watch.error
    @track_status.error
    @track_disable.error
    @track_enable.error
    @track_sample.error
    async def _track_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CheckFailure):
            try:
                await interaction.response.send_message(
                    "❌ You need moderator or admin permissions for this command.",
                    ephemeral=True,
                )
            except discord.NotFound:
                pass
        else:
            log.error("Error in /games track command: %s", error, exc_info=True)


async def setup(bot: "Bot"):
    cog = GamesExternalCog(bot)
    await bot.add_cog(cog)
    games.add_command(cog.track, override=True)
