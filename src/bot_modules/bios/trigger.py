"""Persistent trigger-button management.

The "Create / Update Bio" button lives in the bios channel as a
persistent View with a fixed ``custom_id``. After each new bio embed is
posted (or a 404 fallback turns an edit into a fresh post), the new
embed sits at the bottom and the trigger button is now above it. We
move the trigger back to the bottom so it stays one-tap accessible
without scrolling.

State: the trigger's (channel_id, message_id) lives in the ``config``
table under ``bios_trigger_channel_id`` / ``bios_trigger_message_id``,
scoped per guild.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import TYPE_CHECKING

import discord

from bot_modules.bios.views import PersistentTriggerView
from bot_modules.core.db_utils import (
    delete_config_value,
    get_config_value,
    set_config_value,
)
from bot_modules.services.embeds import BIOS_PRIMARY

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext

log = logging.getLogger("dungeonkeeper.bios.trigger")

_MSG_KEY = "bios_trigger_message_id"
_CH_KEY = "bios_trigger_channel_id"
_TITLE_KEY = "bios_trigger_title"
_BODY_KEY = "bios_trigger_body"

DEFAULT_TRIGGER_TITLE = "📝 Share your bio"
DEFAULT_TRIGGER_BODY = (
    "Tap the button below to create or update your member bio. "
    "I'll spin up a private wizard channel and walk you through it."
)


# ── Config-table helpers ─────────────────────────────────────────────


def get_trigger_ref(
    conn: sqlite3.Connection, guild_id: int
) -> tuple[int, int] | None:
    """Return (channel_id, message_id) of the current trigger button, or
    None if no trigger has been seeded yet."""
    mid_raw = get_config_value(conn, _MSG_KEY, "0", guild_id)
    cid_raw = get_config_value(conn, _CH_KEY, "0", guild_id)
    try:
        mid = int(mid_raw)
        cid = int(cid_raw)
    except (TypeError, ValueError):
        return None
    if mid == 0 or cid == 0:
        return None
    return (cid, mid)


def set_trigger_ref(
    conn: sqlite3.Connection, guild_id: int, channel_id: int, message_id: int
) -> None:
    set_config_value(conn, _CH_KEY, str(channel_id), guild_id)
    set_config_value(conn, _MSG_KEY, str(message_id), guild_id)


def clear_trigger_ref(conn: sqlite3.Connection, guild_id: int) -> None:
    delete_config_value(conn, _CH_KEY, guild_id)
    delete_config_value(conn, _MSG_KEY, guild_id)


# ── Embed + posting ──────────────────────────────────────────────────


def get_trigger_content(
    conn: sqlite3.Connection, guild_id: int
) -> tuple[str, str]:
    """Return the ``(title, body)`` for the trigger embed.

    Both are admin-configurable per guild via the ``bios_trigger_title`` /
    ``bios_trigger_body`` config keys, falling back to the built-in
    defaults when unset (or blank).
    """
    title = (
        get_config_value(conn, _TITLE_KEY, DEFAULT_TRIGGER_TITLE, guild_id)
        or DEFAULT_TRIGGER_TITLE
    )
    body = (
        get_config_value(conn, _BODY_KEY, DEFAULT_TRIGGER_BODY, guild_id)
        or DEFAULT_TRIGGER_BODY
    )
    return title, body


def build_trigger_embed(
    title: str = DEFAULT_TRIGGER_TITLE,
    description: str = DEFAULT_TRIGGER_BODY,
    color: int = BIOS_PRIMARY,
) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


async def post_trigger_button(
    ctx: "AppContext",
    bios_channel: discord.TextChannel,
    *,
    replace_existing: bool = True,
    embed_color: int = BIOS_PRIMARY,
) -> discord.Message:
    """Post a fresh trigger button at the bottom of the bios channel.

    When ``replace_existing`` is True (default), deletes the previously
    stored trigger message (ignoring 404 / Forbidden) before posting.
    Persists the new reference and returns the new message.
    """
    guild_id = bios_channel.guild.id

    if replace_existing:
        await _delete_stored_trigger(ctx, bios_channel, guild_id)

    def _read_content() -> tuple[str, str]:
        with ctx.open_db() as conn:
            return get_trigger_content(conn, guild_id)

    title, body = await asyncio.to_thread(_read_content)

    try:
        new_msg = await bios_channel.send(
            embed=build_trigger_embed(title, body, embed_color),
            view=PersistentTriggerView(),
        )
    except discord.HTTPException:
        log.exception("Failed to post trigger button in guild %d", guild_id)
        raise

    def _save() -> None:
        with ctx.open_db() as conn:
            set_trigger_ref(conn, guild_id, bios_channel.id, new_msg.id)

    await asyncio.to_thread(_save)
    return new_msg


async def reposition_trigger_button(
    ctx: "AppContext", bios_channel: discord.TextChannel
) -> None:
    """Move the existing trigger button to the bottom of the bios channel.

    No-op when no trigger has been seeded — the dashboard "Post trigger
    button" action is the only way to create the initial one.
    """
    guild_id = bios_channel.guild.id

    def _read() -> tuple[int, int] | None:
        with ctx.open_db() as conn:
            return get_trigger_ref(conn, guild_id)

    ref = await asyncio.to_thread(_read)
    if ref is None:
        return

    await post_trigger_button(ctx, bios_channel, replace_existing=True)


async def _delete_stored_trigger(
    ctx: "AppContext", bios_channel: discord.TextChannel, guild_id: int
) -> None:
    """Best-effort delete of the previously-stored trigger message."""

    def _read() -> tuple[int, int] | None:
        with ctx.open_db() as conn:
            return get_trigger_ref(conn, guild_id)

    ref = await asyncio.to_thread(_read)
    if ref is None:
        return

    old_channel_id, old_message_id = ref
    old_channel = bios_channel
    if old_channel.id != old_channel_id:
        fetched = bios_channel.guild.get_channel(old_channel_id)
        if isinstance(fetched, discord.TextChannel):
            old_channel = fetched
        else:
            return  # stale ref to a channel that no longer exists
    try:
        old_msg = await old_channel.fetch_message(old_message_id)
        await old_msg.delete()
    except (discord.NotFound, discord.Forbidden):
        pass
    except discord.HTTPException:
        log.exception(
            "Failed to delete old trigger button in guild %d", guild_id
        )


def resolve_bio_placeholders(
    conn: sqlite3.Connection, guild_id: int
) -> tuple[str, str]:
    """Return ``(bio_link, bios_channel_mention)`` for the welcome-template
    placeholders.

    - ``bio_link`` is a jump URL to the trigger-button message when one
      exists, otherwise empty. (Falls back to the channel mention if the
      trigger ref is missing but a bios channel is configured.)
    - ``bios_channel_mention`` is ``<#channel_id>`` for the bios channel
      when configured, otherwise empty.
    """
    bios_channel_id_raw = get_config_value(conn, "bios_channel_id", "0", guild_id)
    try:
        bios_channel_id = int(bios_channel_id_raw)
    except (TypeError, ValueError):
        bios_channel_id = 0

    bios_channel_mention = f"<#{bios_channel_id}>" if bios_channel_id else ""

    ref = get_trigger_ref(conn, guild_id)
    if ref is not None:
        cid, mid = ref
        bio_link = f"https://discord.com/channels/{guild_id}/{cid}/{mid}"
    elif bios_channel_id:
        bio_link = f"https://discord.com/channels/{guild_id}/{bios_channel_id}"
    else:
        bio_link = ""

    return bio_link, bios_channel_mention


__all__ = [
    "DEFAULT_TRIGGER_BODY",
    "DEFAULT_TRIGGER_TITLE",
    "build_trigger_embed",
    "clear_trigger_ref",
    "get_trigger_content",
    "get_trigger_ref",
    "post_trigger_button",
    "reposition_trigger_button",
    "resolve_bio_placeholders",
    "set_trigger_ref",
]
