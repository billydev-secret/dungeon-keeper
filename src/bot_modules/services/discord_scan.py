"""Direct Discord history walking — used by message backfill and by the
privacy cog to authoritatively find every message a user has posted, without
relying on the local index."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import discord

log = logging.getLogger("dungeonkeeper.discord_scan")

MessageableChannel = (
    discord.TextChannel | discord.VoiceChannel | discord.StageChannel | discord.Thread
)

ScanProgress = Callable[[int, int, int], Awaitable[None]]
"""(channels_done, channels_total, messages_found) -> awaitable"""


async def collect_messageable_channels(
    guild: discord.Guild,
    me: discord.Member | None,
    *,
    include_unjoined_private_threads: bool = False,
) -> list[MessageableChannel]:
    """All readable channels and threads where members can post.

    Includes text, voice, and stage channels (which all carry text chat),
    plus active and archived threads of text and forum channels. Forum
    channels themselves are skipped — only their threads (the "posts")
    contain messages.

    ``include_unjoined_private_threads`` controls how private archived
    threads are listed. ``False`` (the default, used by backfill) only
    returns threads the bot has joined — fast, and sufficient because
    other private threads were never indexed live anyway. ``True`` (used
    by the privacy scan) tries ``manage_threads`` first to surface every
    private thread the bot can see, falling back to joined-only on
    Forbidden.
    """
    channels: list[MessageableChannel] = []
    seen_ids: set[int] = set()

    def _can_read(ch: discord.abc.GuildChannel | discord.Thread) -> bool:
        return not me or ch.permissions_for(me).read_message_history

    async def _add_threads(parent: discord.TextChannel | discord.ForumChannel) -> None:
        for thread in parent.threads:
            if thread.id in seen_ids or not _can_read(thread):
                continue
            channels.append(thread)
            seen_ids.add(thread.id)

        try:
            async for archived in parent.archived_threads(limit=None):
                if archived.id in seen_ids or not _can_read(archived):
                    continue
                channels.append(archived)
                seen_ids.add(archived.id)
        except (discord.Forbidden, discord.HTTPException):
            pass

        # Private archived threads (text channels only — forums have none).
        if isinstance(parent, discord.TextChannel):
            await _add_private_archived_threads(parent)

    async def _add_private_archived_threads(parent: discord.TextChannel) -> None:
        if include_unjoined_private_threads:
            try:
                async for archived in parent.archived_threads(
                    private=True, limit=None
                ):
                    if archived.id in seen_ids:
                        continue
                    channels.append(archived)
                    seen_ids.add(archived.id)
                return
            except discord.Forbidden:
                pass  # fall back to joined-only below
            except discord.HTTPException:
                pass

        try:
            async for archived in parent.archived_threads(
                private=True, joined=True, limit=None
            ):
                if archived.id in seen_ids:
                    continue
                channels.append(archived)
                seen_ids.add(archived.id)
        except (discord.Forbidden, discord.HTTPException):
            pass

    for channel in guild.text_channels:
        if not _can_read(channel):
            continue
        channels.append(channel)
        seen_ids.add(channel.id)
        await _add_threads(channel)

    for forum in getattr(guild, "forums", []):
        if not _can_read(forum):
            continue
        await _add_threads(forum)

    for vc in guild.voice_channels:
        if vc.id in seen_ids or not _can_read(vc):
            continue
        channels.append(vc)
        seen_ids.add(vc.id)

    for sc in getattr(guild, "stage_channels", []):
        if sc.id in seen_ids or not _can_read(sc):
            continue
        channels.append(sc)
        seen_ids.add(sc.id)

    return channels


async def find_user_messages(
    guild: discord.Guild,
    user_id: int,
    *,
    on_progress: ScanProgress | None = None,
    predicate: Callable[[discord.Message], bool] | None = None,
) -> list[tuple[int, int]]:
    """Walk every readable channel and return (message_id, channel_id) tuples
    for every message authored by *user_id*.

    This is authoritative — it does not consult the local index. It also
    surfaces messages in private archived threads when the bot has
    ``manage_threads``. Slow on large servers (Discord history reads are
    ~100 msg/sec); use only when completeness matters more than latency
    (e.g. ``/delete_me``).

    *predicate*: when given, only messages it accepts are collected. The full
    ``discord.Message`` is only in scope here, so callers that need to select
    on its content (``/delete_me mode:``, which filters on attachments and
    embeds) must decide during the scan — the returned ids carry no such
    detail. A message the predicate rejects is never collected and so is never
    deleted.
    """
    me = guild.me
    channels = await collect_messageable_channels(
        guild, me, include_unjoined_private_threads=True
    )
    total_channels = len(channels)
    rows: list[tuple[int, int]] = []

    for idx, channel in enumerate(channels, start=1):
        try:
            async for msg in channel.history(limit=None):
                if msg.author.id == user_id and (predicate is None or predicate(msg)):
                    rows.append((msg.id, channel.id))
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning(
                "Cannot scan channel %d (%s) — skipping",
                channel.id,
                exc,
            )
        if on_progress:
            await on_progress(idx, total_channels, len(rows))

    return rows
