"""Discord adapter for :mod:`channel_health_logic`.

Resolves configured channel ids against a live ``discord.Guild`` and counts who
can actually see them, producing the plain snapshots the rules operate on. All
the judgement lives in the logic module; this file only measures.

The channel list comes from ``settings_registry`` — the same curated inventory
``advisor_gaps`` uses — so a feature that registers a channel setting is checked
here automatically, with the label and panel an admin needs to go fix it.
"""

from __future__ import annotations

import logging
import sqlite3

import discord

from bot_modules.core.db_utils import get_config_value
from bot_modules.services.channel_health_logic import ChannelSnapshot
from bot_modules.services.settings_registry import FEATURES

log = logging.getLogger("dungeonkeeper.channel_health")

# Channel types a feature can post into. Voice and stage channels carry text
# chat and accept ``.send()``; forums and categories don't (a forum post has to
# go into a thread), so a forum id saved in a "post here" setting is a
# misconfiguration worth naming.
_POSTABLE = (
    discord.TextChannel,
    discord.Thread,
    discord.VoiceChannel,
    discord.StageChannel,
)


def _human_viewers(
    guild: discord.Guild, channel: discord.abc.GuildChannel | discord.Thread
) -> tuple[int, int]:
    """``(non-bot members who can view, non-bot members seen)``.

    Both counts come from the same pass so they can't disagree, and the total
    is returned so the caller can tell "nobody can see it" from "I couldn't
    see anybody" — an unpopulated member cache must not read as a fault.

    The viewer count stops at one. The rules only ever ask whether it is zero,
    and ``permissions_for`` walks a channel's overwrites per member — on a
    large guild, resolving all of them for every configured channel would block
    the event loop for seconds to compute a number nothing reads. So a healthy
    channel costs one permission resolution and only a genuinely invisible one
    pays for the full sweep.
    """
    total = viewers = 0
    for member in guild.members:
        if member.bot:
            continue
        total += 1
        if viewers:
            continue
        try:
            if channel.permissions_for(member).view_channel:
                viewers = 1
        except Exception:  # pragma: no cover — defensive against odd cache state
            log.exception("permission check failed for member %d", member.id)
    return viewers, total


def snapshot_channel(
    guild: discord.Guild,
    *,
    key: str,
    label: str,
    panel: str,
    channel_id: int,
) -> ChannelSnapshot:
    """Resolve one configured channel id into a snapshot for the rules."""
    channel = guild.get_channel_or_thread(channel_id) if channel_id else None
    if channel is None:
        return ChannelSnapshot(
            key=key,
            label=label,
            panel=panel,
            channel_id=channel_id,
            exists=False,
        )

    me = guild.me
    bot_perms = channel.permissions_for(me) if me is not None else None
    viewers, total = _human_viewers(guild, channel)

    return ChannelSnapshot(
        key=key,
        label=label,
        panel=panel,
        channel_id=channel_id,
        exists=True,
        channel_name=getattr(channel, "name", ""),
        accepts_messages=isinstance(channel, _POSTABLE),
        human_viewers=viewers,
        total_humans=total,
        bot_can_view=bool(bot_perms and bot_perms.view_channel),
        bot_can_send=bool(bot_perms and bot_perms.send_messages),
        bot_can_embed=bool(bot_perms and bot_perms.embed_links),
        is_category=isinstance(channel, discord.CategoryChannel),
        bot_can_manage_channels=bool(bot_perms and bot_perms.manage_channels),
    )


def configured_channel_settings() -> list[tuple[str, str, str]]:
    """``(config key, label, panel)`` for every channel setting in the registry."""
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for feature in FEATURES:
        for setting in feature.settings:
            if setting.kind != "channel" or setting.key in seen:
                continue
            seen.add(setting.key)
            out.append((setting.key, setting.label, feature.panel))
    return out


def snapshot_configured_channels(
    conn: sqlite3.Connection, guild: discord.Guild
) -> list[ChannelSnapshot]:
    """Snapshot every channel this guild has actually configured.

    Unset keys are skipped — "not set up" is ``advisor_gaps``' job, and
    reporting it here too would bury the channels that *are* set up and broken.
    """
    snaps: list[ChannelSnapshot] = []
    for key, label, panel in configured_channel_settings():
        raw = get_config_value(conn, key, "0", guild.id)
        try:
            channel_id = int(str(raw).strip() or "0")
        except (TypeError, ValueError):
            continue
        if channel_id <= 0:
            continue
        snaps.append(
            snapshot_channel(
                guild, key=key, label=label, panel=panel, channel_id=channel_id
            )
        )
    return snaps


__all__ = [
    "configured_channel_settings",
    "snapshot_channel",
    "snapshot_configured_channels",
]
