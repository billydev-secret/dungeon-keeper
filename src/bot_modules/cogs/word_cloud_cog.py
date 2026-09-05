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
from bot_modules.word_cloud import corpus, embeds, logic, presets, render
from bot_modules.word_cloud.embeds import FILENAME

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger(__name__)



def _can_read(channel: object, member: discord.Member) -> bool:
    """Whether ``member`` may read ``channel``'s history.

    The single definition of this command's gate, used both for a named
    channel and for every candidate in the "everywhere" fan-out, so the two
    can never disagree about what "you can read it" means.

    A *private* thread needs more than the inherited answer:
    ``Thread.permissions_for`` only applies the parent channel's overwrites and
    knows nothing about who was invited, so a moderator never added to one
    still comes back "allowed". Discord's own rule is invitation or Manage
    Threads, and only the second half is knowable from the cache.
    """
    try:
        perms = channel.permissions_for(member)  # type: ignore[attr-defined]
    except discord.ClientException:
        # A thread whose parent isn't cached can't be permission-checked, and
        # one stale thread must not sink the whole command.
        return False
    if not (perms.read_messages and perms.read_message_history):
        return False
    if isinstance(channel, discord.Thread) and channel.is_private():
        return bool(perms.manage_threads)
    return True


def _readable_channel_ids(
    guild: discord.Guild, member: discord.Member
) -> list[int]:
    """Every channel in ``guild`` whose history ``member`` may read.

    Threads are included: the archive stores a thread's own id as
    ``channel_id``, so leaving them out would drop whole conversations.
    Archived threads are absent from ``guild.threads`` and so are never swept
    up by "everywhere" — running the command inside one still clouds it, since
    being in it is proof of access.
    """
    return [
        channel.id
        for channel in list(guild.text_channels) + list(guild.threads)
        if _can_read(channel, member)
    ]


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

        Both dials are read strictly, without the legacy ``guild_id=0``
        fallback: these keys are new, so a row at 0 could only be the home
        guild's, and a second guild inheriting someone else's cap and colour
        scheme is exactly the silent cross-guild bleed that flag exists to
        stop. The dashboard reads them the same way, so panel and command
        can't disagree.
        """
        retains = self.bot.ctx.guild_config(guild_id).retains_content
        with self.bot.ctx.open_db() as conn:
            raw_cap = get_config_value(
                conn,
                logic.CAP_KEY,
                str(logic.DEFAULT_CAP),
                guild_id,
                allow_legacy_fallback=False,
            )
            preset = get_config_value(
                conn,
                logic.PRESET_KEY,
                presets.DEFAULT_PRESET,
                guild_id,
                allow_legacy_fallback=False,
            )
        return logic.clamp_cap(raw_cap), preset, retains

    # -- corpus -----------------------------------------------------------

    def _read_archive(
        self,
        guild_id: int,
        channel_ids: list[int],
        since_ts: int,
        cap: int,
        author_id: int | None,
    ) -> tuple[list[logic.Doc], bool]:
        """Read the window, plus the "is there any text at all" answer.

        ``cap`` is asked for one *over* the caller's ceiling so ``apply_cap``
        can tell a window that merely fits from one that was truncated — a
        query capped at exactly N always looks like it fit.

        ``archive_has_content`` only matters when nothing came back: it exists
        to tell "quiet week" apart from "this guild keeps no text", and with
        docs in hand the first is already established.
        """
        with self.bot.ctx.open_db() as conn:
            docs = corpus.fetch_archive(
                conn,
                guild_id=guild_id,
                channel_ids=channel_ids,
                since_ts=since_ts,
                cap=cap + 1,
                author_id=author_id,
            )
            has_content = bool(docs) or corpus.archive_has_content(conn, guild_id)
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
        # Every gate lives in logic.plan_scope; this resolves the Discord
        # objects it needs and does what it is told.
        guild = interaction.guild
        invoker = interaction.user
        in_guild = guild is not None and isinstance(invoker, discord.Member)

        target = channel or interaction.channel if in_guild else None
        if not isinstance(target, (discord.TextChannel, discord.Thread)):
            target = None

        target_readable = False
        if target is not None and isinstance(invoker, discord.Member):
            target_readable = _can_read(target, invoker)

        scope = logic.plan_scope(
            is_mod=in_guild and self.bot.ctx.is_mod(interaction),
            in_guild=in_guild,
            everywhere=everywhere,
            everywhere_ids=(
                _readable_channel_ids(guild, invoker)
                if everywhere and in_guild and isinstance(invoker, discord.Member)
                else ()
            ),
            target_id=target.id if target is not None else None,
            target_readable=target_readable,
            target_label=target.mention if target is not None else "",
            picked_channel=channel is not None,
        )
        if isinstance(scope, logic.Refusal):
            await interaction.response.send_message(scope.message, ephemeral=True)
            return

        try:
            span = logic.parse_window(window)
        except logic.WindowError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        assert guild is not None
        try:
            await self._run(interaction, guild, span, scope, member, preset, color)
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
        scope: logic.Scope,
        member: discord.Member | None,
        preset_choice: app_commands.Choice[str] | None,
        color_choice: app_commands.Choice[str] | None,
    ) -> None:
        cap, default_preset, retains = await asyncio.to_thread(
            self._read_dials, guild.id
        )
        preset = presets.resolve_preset(
            preset_choice.value if preset_choice else default_preset
        )
        want_sentiment = (
            color_choice.value if color_choice else "sentiment"
        ) == "sentiment"

        author_id = member.id if member else None
        channel_ids = list(scope.channel_ids)
        notes: list[str] = [scope.note] if scope.note else []
        now = time.time()

        # The span the cloud actually covers. The live path clamps it, and the
        # card is labelled with this rather than the ask.
        effective_span = span
        live_reason: str | None = None

        if retains:
            since_ts = int(now - span.total_seconds())
            docs, has_content = await asyncio.to_thread(
                self._read_archive, guild.id, channel_ids, since_ts, cap, author_id
            )
            source = "stored history"
            if not docs and not has_content:
                # The dial says "all" but nothing has landed yet — a different
                # story from a guild that keeps no text, and it must not be
                # told as if it were.
                live_reason = logic.LIVE_EMPTY_ARCHIVE
        else:
            live_reason = logic.LIVE_NO_STORAGE

        if live_reason is not None:
            live_span, clamped = logic.clamp_live_window(span)
            effective_span = live_span
            if clamped:
                notes.append(logic.live_clamp_note(live_reason))
            if len(channel_ids) > logic.LIVE_CHANNEL_FANOUT:
                channel_ids = await asyncio.to_thread(
                    self._rank_channels,
                    guild.id,
                    channel_ids,
                    logic.LIVE_CHANNEL_FANOUT,
                )
                notes.append(
                    f"Reading the {logic.LIVE_CHANNEL_FANOUT} busiest channels, "
                    "to keep this quick."
                )
            since_ts = now - live_span.total_seconds()
            # Ask Discord to start after this point rather than paging back
            # through the whole channel and filtering client-side.
            after = discord.Object(
                id=discord.utils.time_snowflake(discord.utils.utcnow() - live_span)
            )
            # One over the cap, for the same reason the archive reads one over.
            docs = await self._read_live(
                guild, channel_ids, after, cap + 1, author_id, since_ts
            )
            source = "the last few minutes of live chat"

        docs, capped = logic.apply_cap(docs, cap)
        if capped:
            notes.append(
                f"Capped at the most recent {cap:,} messages, so this covers "
                "less than the full window."
            )

        stats = await asyncio.to_thread(logic.build_stats, docs)
        if not stats:
            await interaction.followup.send(
                logic.empty_message(
                    live_reason,
                    member.display_name if member else None,
                    scope.label,
                ),
                ephemeral=True,
            )
            return

        by_sentiment = want_sentiment and any(s.sentiment is not None for s in stats)
        if want_sentiment and not by_sentiment:
            notes.append("No mood scores on this path, so colours are the preset's.")

        png = await asyncio.to_thread(
            render.render_png, stats, preset, by_sentiment=by_sentiment
        )

        embed = embeds.build_cloud_embed(
            message_count=len(docs),
            member_name=member.display_name if member else None,
            scope_label=scope.label,
            span=effective_span,
            source_label=source,
            by_sentiment=by_sentiment,
            notes=notes,
            color=await safe_resolve_accent(self.bot, guild),
        )
        await interaction.followup.send(
            embed=embed,
            file=discord.File(fp=io.BytesIO(png), filename=FILENAME),
            ephemeral=True,
        )


async def setup(bot: "Bot") -> None:
    await bot.add_cog(WordCloudCog(bot))
