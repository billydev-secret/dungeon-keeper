"""``/wordcloud`` — a picture of what a channel has been talking about.

Moderator-only, and the reply is ephemeral: the cloud is a read of the message
archive, which the privacy notice already discloses as visible to admins and
moderators through the dashboard. Read permission is the gate — a moderator can
cloud any channel they can read, and no channel they can't.

Two corpus paths sit behind one command. Guilds archiving message content
(``message_storage_level = all``) are read from the database and can reach back
years; every other guild keeps ids and timestamps but no text, so it falls back
to reading recent history straight off Discord — capped at ten minutes, storing
nothing. The reply says which path ran, because "quiet week" and "this server
keeps no message text" are different answers to an empty cloud.

Its two dials — the message cap and the default preset — live on the dashboard
(Channels & Messages → Word Cloud), per CLAUDE.md.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.branding import safe_resolve_accent
from bot_modules.core.db_utils import get_config_value
from bot_modules.word_cloud import corpus, logic, presets, render

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger(__name__)

CAP_KEY = "word_cloud_message_cap"
PRESET_KEY = "word_cloud_default_preset"
DEFAULT_CAP = 12000
#: Ceiling on the dial itself. JakeBot, the most featureful of the existing
#: word cloud bots, stops at 12,000; past that the tail of the cloud is words
#: nobody can read anyway, and the render cost keeps climbing.
MAX_CAP = 12000
#: How many channels a live "everywhere" fans out over. Each is one Discord
#: round trip, so this is the difference between a slow command and a hung one.
LIVE_CHANNEL_FANOUT = 25

_FILENAME = "wordcloud.png"


def _readable_channel_ids(
    guild: discord.Guild, member: discord.Member
) -> list[int]:
    """Every channel in ``guild`` whose history ``member`` may read.

    Read permission is the whole gate on this command, so this is where it is
    enforced for the "everywhere" scope. Threads are included: the archive
    stores a thread's own id as ``channel_id``, so leaving them out would drop
    whole conversations.
    """
    out: list[int] = []
    for channel in list(guild.text_channels) + list(guild.threads):
        perms = channel.permissions_for(member)
        if perms.read_messages and perms.read_message_history:
            out.append(channel.id)
    return out


class WordCloudCog(commands.Cog):
    """The ``/wordcloud`` command."""

    def __init__(self, bot: "Bot") -> None:
        self.bot = bot

    # -- config -----------------------------------------------------------

    def _read_dials(self, guild_id: int) -> tuple[int, str, bool]:
        """Cap, default preset, and whether this guild archives content.

        Bundled into one worker-thread hop: ``guild_config`` loads from sqlite
        on a cache miss, and this handler has already deferred, so there is no
        reason to make the event loop wait for either read.
        """
        retains = self.bot.ctx.guild_config(guild_id).retains_content
        with self.bot.ctx.open_db() as conn:
            raw_cap = get_config_value(conn, CAP_KEY, str(DEFAULT_CAP), guild_id)
            preset = get_config_value(conn, PRESET_KEY, presets.DEFAULT_PRESET, guild_id)
        try:
            cap = int(raw_cap)
        except (TypeError, ValueError):
            cap = DEFAULT_CAP
        # A dial blanked to 0 or saved negative would render nothing at all;
        # treat anything unusable as "no cap set" rather than "cloud nothing".
        if cap <= 0:
            cap = DEFAULT_CAP
        return min(cap, MAX_CAP), preset, retains

    # -- corpus -----------------------------------------------------------

    def _read_archive(
        self,
        guild_id: int,
        channel_ids: list[int],
        since_ts: int,
        cap: int,
        author_id: int | None,
    ) -> tuple[list[logic.Doc], bool]:
        with self.bot.ctx.open_db() as conn:
            docs = corpus.fetch_archive(
                conn,
                guild_id=guild_id,
                channel_ids=channel_ids,
                since_ts=since_ts,
                cap=cap,
                author_id=author_id,
            )
            has_content = corpus.archive_has_content(conn, guild_id)
        return docs, has_content

    def _rank_channels(
        self, guild_id: int, channel_ids: list[int], limit: int
    ) -> list[int]:
        with self.bot.ctx.open_db() as conn:
            return corpus.recent_channel_ids(
                conn, guild_id=guild_id, channel_ids=channel_ids, limit=limit
            )

    async def _read_live(
        self,
        guild: discord.Guild,
        channel_ids: list[int],
        after: discord.abc.Snowflake | None,
        cap: int,
        author_id: int | None,
        since_ts: float,
    ) -> list[logic.Doc]:
        """Read recent history off Discord. Stores nothing.

        Bot authors are skipped here the same way the archive skips them,
        except when one was explicitly named — otherwise the cloud is the
        bot's own card copy.
        """
        docs: list[logic.Doc] = []
        for channel_id in channel_ids:
            if len(docs) >= cap:
                break
            channel = guild.get_channel_or_thread(channel_id)
            if channel is None or not hasattr(channel, "history"):
                continue
            try:
                # oldest_first is implied True whenever ``after`` is given, which
                # would hand back the *start* of the window on a channel busier
                # than the cap. The archive path and apply_cap both mean "most
                # recent", so say so explicitly.
                async for message in channel.history(
                    limit=cap - len(docs), after=after, oldest_first=False
                ):
                    if message.created_at.timestamp() < since_ts:
                        continue
                    if author_id is not None:
                        if message.author.id != author_id:
                            continue
                    elif message.author.bot:
                        continue
                    if message.content:
                        docs.append(logic.Doc(text=message.content))
            except discord.HTTPException:
                # One unreadable channel must not sink the whole command.
                log.debug("word cloud: history failed for %s", channel_id, exc_info=True)
                continue
        return docs

    # -- command ----------------------------------------------------------

    @app_commands.command(
        name="wordcloud",
        description="Picture the words a channel has been using (moderators only).",
    )
    @app_commands.describe(
        window="How far back, like 30m, 6h or 7d. Defaults to 24h.",
        channel="Which channel. Defaults to this one.",
        member="Only this person's words.",
        everywhere="Use every channel you can read instead of just one.",
        preset="Visual style.",
        color="Colour words by the mood of their messages, or by the preset palette.",
    )
    @app_commands.choices(
        preset=[
            app_commands.Choice(name=p.label, value=p.key) for p in presets.PRESETS
        ],
        color=[
            app_commands.Choice(name="By mood", value="sentiment"),
            app_commands.Choice(name="By palette", value="palette"),
        ],
    )
    async def wordcloud(  # noqa: PLR0913 - each option is a distinct user choice
        self,
        interaction: discord.Interaction,
        window: str = "24h",
        channel: discord.TextChannel | None = None,
        member: discord.Member | None = None,
        everywhere: bool = False,
        preset: app_commands.Choice[str] | None = None,
        color: app_commands.Choice[str] | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This only works in a server.", ephemeral=True
            )
            return

        if not self.bot.ctx.is_mod(interaction):
            await interaction.response.send_message(
                "That's a moderator command.", ephemeral=True
            )
            return

        try:
            span = logic.parse_window(window)
        except logic.WindowError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            await self._run(interaction, guild, span, channel, member, everywhere, preset, color)
        except Exception:
            log.exception("word cloud failed")
            await interaction.followup.send(
                "Something went wrong building that cloud.", ephemeral=True
            )

    async def _run(  # noqa: PLR0913 - mirrors the command's own options
        self,
        interaction: discord.Interaction,
        guild: discord.Guild,
        span: timedelta,
        channel: discord.TextChannel | None,
        member: discord.Member | None,
        everywhere: bool,
        preset_choice: app_commands.Choice[str] | None,
        color_choice: app_commands.Choice[str] | None,
    ) -> None:
        invoker = interaction.user
        assert isinstance(invoker, discord.Member)

        cap, default_preset, retains = await asyncio.to_thread(
            self._read_dials, guild.id
        )
        preset = presets.resolve_preset(
            preset_choice.value if preset_choice else default_preset
        )
        want_sentiment = (color_choice.value if color_choice else "sentiment") == "sentiment"

        # -- scope, gated on what the moderator can actually read ----------
        if everywhere:
            # In-memory permission maths over the channel cache — no I/O, so
            # it stays on the loop rather than crossing a thread boundary.
            channel_ids = _readable_channel_ids(guild, invoker)
            scope_label = "every channel you can read"
        else:
            target = channel or interaction.channel
            if target is None or not isinstance(
                target, (discord.TextChannel, discord.Thread)
            ):
                await interaction.followup.send(
                    "Pick a text channel for this.", ephemeral=True
                )
                return
            perms = target.permissions_for(invoker)
            if not (perms.read_messages and perms.read_message_history):
                await interaction.followup.send(
                    "You can't read that channel's history.", ephemeral=True
                )
                return
            channel_ids = [target.id]
            scope_label = target.mention

        if not channel_ids:
            await interaction.followup.send(
                "There's nothing here you can read.", ephemeral=True
            )
            return

        # -- corpus --------------------------------------------------------
        author_id = member.id if member else None
        notes: list[str] = []
        now = time.time()

        if retains:
            since_ts = int(now - span.total_seconds())
            docs, has_content = await asyncio.to_thread(
                self._read_archive, guild.id, channel_ids, since_ts, cap, author_id
            )
            source = "stored history"
            if not docs and not has_content:
                retains = False  # dial says "all" but nothing landed yet
        if not retains:
            live_span, clamped = logic.clamp_live_window(span)
            if clamped:
                notes.append(
                    "This server doesn't keep message text, so this is the last "
                    "10 minutes rather than the window you asked for."
                )
            if len(channel_ids) > LIVE_CHANNEL_FANOUT:
                channel_ids = await asyncio.to_thread(
                    self._rank_channels, guild.id, channel_ids, LIVE_CHANNEL_FANOUT
                )
                notes.append(
                    f"Reading the {LIVE_CHANNEL_FANOUT} busiest channels, to keep "
                    "this quick."
                )
            since_ts = now - live_span.total_seconds()
            # Ask Discord to start after this point rather than paging back
            # through the whole channel and filtering client-side.
            after = discord.Object(
                id=discord.utils.time_snowflake(discord.utils.utcnow() - live_span)
            )
            docs = await self._read_live(
                guild, channel_ids, after, cap, author_id, since_ts
            )
            source = "the last few minutes of live chat"

        docs, capped = logic.apply_cap(docs, cap)
        if capped:
            notes.append(
                f"Capped at the most recent {cap:,} messages, so this covers less "
                "than the full window."
            )

        stats = await asyncio.to_thread(logic.build_stats, docs)
        if not stats:
            await interaction.followup.send(
                self._empty_message(retains, member, scope_label), ephemeral=True
            )
            return

        by_sentiment = want_sentiment and any(s.sentiment is not None for s in stats)
        if want_sentiment and not by_sentiment:
            notes.append("No mood scores on this path, so colours are the preset's.")

        png = await asyncio.to_thread(
            render.render_png, stats, preset, by_sentiment=by_sentiment
        )

        # -- reply ---------------------------------------------------------
        who = f" from {member.display_name}" if member else ""
        embed = discord.Embed(
            title="Word cloud",
            description=(
                f"**{len(docs):,}** messages{who} in {scope_label}, "
                f"over the last {self._span_label(span)} — from {source}."
            ),
            color=await safe_resolve_accent(self.bot, guild),
        )
        if by_sentiment:
            embed.add_field(
                name="Colour",
                value="Warm words came up in happier messages, cool ones in unhappier.",
                inline=False,
            )
        if notes:
            embed.add_field(name="Worth knowing", value="\n".join(notes), inline=False)
        embed.set_image(url=f"attachment://{_FILENAME}")

        await interaction.followup.send(
            embed=embed,
            file=discord.File(fp=io.BytesIO(png), filename=_FILENAME),
            ephemeral=True,
        )

    @staticmethod
    def _span_label(span: timedelta) -> str:
        minutes = int(span.total_seconds() // 60)
        if minutes < 60:
            return f"{minutes} minute{'s' if minutes != 1 else ''}"
        hours = minutes // 60
        if hours < 48:
            return f"{hours} hour{'s' if hours != 1 else ''}"
        days = hours // 24
        return f"{days} day{'s' if days != 1 else ''}"

    @staticmethod
    def _empty_message(
        retains: bool, member: discord.Member | None, scope_label: str
    ) -> str:
        """Say *why* the cloud is empty — the two reasons are not the same."""
        if not retains:
            return (
                "Nothing to draw. This server doesn't keep message text, so I can "
                "only read the last 10 minutes of live chat — and there wasn't "
                "enough of it just now."
            )
        who = f" from {member.display_name}" if member else ""
        return f"Nothing to draw{who} in {scope_label} over that window."


async def setup(bot: "Bot") -> None:
    await bot.add_cog(WordCloudCog(bot))
