"""Publish/refresh/unpublish a role menu's single Discord message.

Docs' sync engine reconciles many messages; a role menu is exactly one message
(embed + components), so the shapes here are simpler: publish sends or moves
it, sync edits it in place, unpublish swaps in a disabled view ("post stays as
decor", spec §5), delete pulls it down. All run on the bot's event loop and are
called from the web routes (the cog registers the DynamicItems; there are no
slash commands).

``menu_health`` is the cache-only diagnosis the panel shows ("role missing",
"message missing — republish?") — it never touches the network.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from bot_modules.core.branding import resolve_accent_color
from bot_modules.core.utils import get_bot_member
from bot_modules.role_menus import db as menus_db
from bot_modules.role_menus.views import build_disabled_view, build_view

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.role_menus")

_POSTABLE = (discord.TextChannel, discord.Thread, discord.VoiceChannel)


@dataclass
class SyncResult:
    channel_id: int
    status: str = "ok"  # ok | missing_channel | missing_message | forbidden | error
    message_id: int = 0
    detail: str = ""


async def build_embed(
    ctx: "AppContext", guild: discord.Guild, menu: dict
) -> discord.Embed:
    accent = (menu.get("accent") or "").strip().lstrip("#")
    color: discord.Color | None = None
    if accent:
        try:
            color = discord.Color(int(accent, 16))
        except ValueError:
            color = None
    if color is None:
        color = await resolve_accent_color(ctx.db_path, guild)
    embed = discord.Embed(
        title=menu["title"] or None,
        description=menu["description"] or None,
        color=color,
    )
    thumb = (menu.get("thumbnail_url") or "").strip()
    if thumb.startswith(("http://", "https://")):
        embed.set_thumbnail(url=thumb)
    return embed


async def _resolve_channel(bot: "Bot", channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            channel = None
    return channel if isinstance(channel, _POSTABLE) else None


async def _fetch_message(bot: "Bot", channel_id: int, message_id: int):
    channel = await _resolve_channel(bot, channel_id)
    if channel is None or message_id <= 0:
        return channel, None
    try:
        return channel, await channel.fetch_message(message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return channel, None


async def publish_menu(
    ctx: "AppContext",
    guild: discord.Guild,
    menu: dict,
    options: list[dict],
    channel_id: int,
) -> SyncResult:
    """Send the menu into ``channel_id`` — or edit in place if already there.

    Publishing to a different channel moves the menu: post the new message
    first, then take the old one down best-effort.
    """
    bot = ctx.bot
    if bot is None:
        return SyncResult(channel_id, status="error", detail="Bot unavailable.")

    embed = await build_embed(ctx, guild, menu)
    view = build_view(menu, options)

    if menu["message_id"] and menu["channel_id"] == channel_id:
        _channel, msg = await _fetch_message(bot, channel_id, menu["message_id"])
        if msg is not None:
            try:
                await msg.edit(embed=embed, view=view)
            except discord.Forbidden:
                return SyncResult(channel_id, status="forbidden",
                                  detail="Missing permission to edit the menu message.")
            except discord.HTTPException as exc:
                return SyncResult(channel_id, status="error", detail=str(exc))
            await _persist_published(ctx, menu, channel_id, msg.id)
            return SyncResult(channel_id, message_id=msg.id)

    channel = await _resolve_channel(bot, channel_id)
    if channel is None:
        return SyncResult(channel_id, status="missing_channel",
                          detail="That channel isn't available.")
    try:
        msg = await channel.send(embed=embed, view=view)
    except discord.Forbidden:
        return SyncResult(channel_id, status="forbidden",
                          detail="Missing permission to post in this channel.")
    except discord.HTTPException as exc:
        log.warning("role menu %d publish failed in %d: %s", menu["id"], channel_id, exc)
        return SyncResult(channel_id, status="error", detail=str(exc))

    old_channel_id, old_message_id = menu["channel_id"], menu["message_id"]
    await _persist_published(ctx, menu, channel_id, msg.id)
    if old_message_id and (old_channel_id != channel_id):
        await _delete_message(bot, old_channel_id, old_message_id)
    return SyncResult(channel_id, message_id=msg.id)


async def sync_menu(
    ctx: "AppContext", guild: discord.Guild, menu: dict, options: list[dict]
) -> SyncResult:
    """Push the current definition onto the live message (edit in place)."""
    bot = ctx.bot
    if bot is None:
        return SyncResult(menu["channel_id"], status="error", detail="Bot unavailable.")
    if not menu["message_id"] or not menu["channel_id"]:
        return SyncResult(menu["channel_id"], status="missing_message",
                          detail="Menu isn't published.")
    channel, msg = await _fetch_message(bot, menu["channel_id"], menu["message_id"])
    if channel is None:
        return SyncResult(menu["channel_id"], status="missing_channel",
                          detail="The menu's channel is gone.")
    if msg is None:
        return SyncResult(menu["channel_id"], status="missing_message",
                          detail="The menu's message is gone — republish it.")
    embed = await build_embed(ctx, guild, menu)
    view = build_view(menu, options) if menu["enabled"] else build_disabled_view(menu, options)
    try:
        await msg.edit(embed=embed, view=view)
    except discord.Forbidden:
        return SyncResult(menu["channel_id"], status="forbidden",
                          detail="Missing permission to edit the menu message.")
    except discord.HTTPException as exc:
        return SyncResult(menu["channel_id"], status="error", detail=str(exc))
    await _clear_alert(ctx, menu)
    return SyncResult(menu["channel_id"], message_id=msg.id)


async def set_menu_live_state(
    ctx: "AppContext", guild: discord.Guild, menu: dict, options: list[dict],
    enabled: bool,
) -> SyncResult:
    """Flip enabled on/off and reflect it on the live message (if any)."""
    menu_id = menu["id"]

    def _write() -> None:
        with ctx.open_db() as conn:
            menus_db.set_menu_enabled(conn, menu_id, enabled, time.time())

    await asyncio.to_thread(_write)
    menu["enabled"] = enabled
    if not menu["message_id"]:
        return SyncResult(menu["channel_id"], message_id=0)
    return await sync_menu(ctx, guild, menu, options)


async def delete_menu_message(ctx: "AppContext", menu: dict) -> bool:
    """Take the published message down (used by Delete). Best-effort."""
    bot = ctx.bot
    if bot is None or not menu["message_id"]:
        return False
    _channel, msg = await _fetch_message(bot, menu["channel_id"], menu["message_id"])
    if msg is None:
        return False
    try:
        await msg.delete()
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


def menu_health(guild: discord.Guild, menu: dict, options: list[dict]) -> list[dict]:
    """Cache-only issue list for the panel: what's broken and how to fix it."""
    issues: list[dict] = []
    bot_member = get_bot_member(guild)
    if bot_member is not None and not bot_member.guild_permissions.manage_roles:
        issues.append({"code": "no_manage_roles",
                       "detail": "I don't have the Manage Roles permission."})
    if menu["channel_id"] and guild.get_channel_or_thread(menu["channel_id"]) is None:
        issues.append({"code": "channel_missing",
                       "detail": "The published channel is gone — republish elsewhere."})
    if menu["required_role_id"] and guild.get_role(menu["required_role_id"]) is None:
        issues.append({"code": "required_role_missing",
                       "detail": "The required role no longer exists."})
    for opt in options:
        role = guild.get_role(opt["role_id"])
        if role is None:
            issues.append({"code": "role_missing",
                           "detail": f"Role for “{opt['label']}” no longer exists."})
        elif bot_member is not None and role >= bot_member.top_role:
            issues.append({"code": "role_above_bot",
                           "detail": f"“{opt['label']}” ({role.name}) is above my highest role."})
    return issues


# ── tiny threaded db shims ──────────────────────────────────────────

async def _persist_published(
    ctx: "AppContext", menu: dict, channel_id: int, message_id: int
) -> None:
    menu_id = menu["id"]

    def _write() -> None:
        with ctx.open_db() as conn:
            menus_db.set_menu_published(conn, menu_id, channel_id, message_id, time.time())
            menus_db.set_menu_alerted(conn, menu_id, 0)

    await asyncio.to_thread(_write)
    menu["channel_id"] = channel_id
    menu["message_id"] = message_id
    menu["enabled"] = True
    menu["alerted_at"] = 0


async def _clear_alert(ctx: "AppContext", menu: dict) -> None:
    if not menu["alerted_at"]:
        return
    menu_id = menu["id"]

    def _write() -> None:
        with ctx.open_db() as conn:
            menus_db.set_menu_alerted(conn, menu_id, 0)

    await asyncio.to_thread(_write)
    menu["alerted_at"] = 0


async def _delete_message(bot: "Bot", channel_id: int, message_id: int) -> None:
    channel = await _resolve_channel(bot, channel_id)
    if channel is None:
        return
    try:
        msg = await channel.fetch_message(message_id)
        await msg.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass
