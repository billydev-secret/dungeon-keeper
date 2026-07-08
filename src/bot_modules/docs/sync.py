"""Reconcile a doc's rendered embeds against live Discord messages.

The hard part of "maintain in one place" is drift: a doc grows or shrinks, or a
message gets manually deleted. ``_sync_channel`` reconciles position-by-position
— edit where a message exists, post where it doesn't, delete the surplus — and
is tolerant of ``NotFound``/``Forbidden``.

The orchestration helpers (``sync_doc``/``post_doc``/``unpost_doc``) run on the
bot's event loop (shared by the dashboard), read/write the ``docs`` tables via
``asyncio.to_thread``, and are called from both the cog and the web route.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import discord

from bot_modules.core.branding import resolve_accent_color
from bot_modules.docs import db as docs_db
from bot_modules.docs.render import EmbedSpec, render_doc

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.docs")

# Channel types we can post a doc into.
_POSTABLE = (discord.TextChannel, discord.Thread, discord.VoiceChannel)


@dataclass
class SyncResult:
    channel_id: int
    status: str = "ok"  # ok | missing_channel | forbidden | error
    created: int = 0
    edited: int = 0
    deleted: int = 0
    message_ids: list[int] = field(default_factory=list)
    detail: str = ""
    pinned: bool = False
    # Non-empty when the messages posted fine but pinning them didn't (missing
    # Manage Messages, or Discord's 50-pin channel limit). Kept separate from
    # ``status`` so a pin hiccup never reports the whole placement as broken.
    pin_detail: str = ""


def specs_to_embeds(specs: list[EmbedSpec], color: discord.Colour) -> list[discord.Embed]:
    embeds: list[discord.Embed] = []
    for spec in specs:
        # Headings live inside the description as ``#`` markdown (bigger than the
        # embed title field), so we never set embed.title here.
        embed = discord.Embed(description=spec.description or None, color=color)
        if spec.image_url:
            embed.set_image(url=spec.image_url)
        embeds.append(embed)
    return embeds


async def _resolve_color(
    ctx: "AppContext", guild: discord.Guild, doc_row: dict
) -> discord.Colour:
    accent = (doc_row.get("accent") or "").strip().lstrip("#")
    if accent:
        try:
            return discord.Colour(int(accent, 16))
        except ValueError:
            pass
    return await resolve_accent_color(ctx.db_path, guild)


async def _resolve_channel(bot: "Bot", channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            channel = None
    return channel if isinstance(channel, _POSTABLE) else None


async def _sync_channel(
    bot: "Bot",
    channel_id: int,
    embeds: list[discord.Embed],
    existing_ids: list[int],
    pinned: bool = False,
) -> SyncResult:
    channel = await _resolve_channel(bot, channel_id)
    if channel is None:
        return SyncResult(channel_id, status="missing_channel")

    result = SyncResult(channel_id)
    # Message objects for the ids we end up tracking, in order, so the pin pass
    # below can read each message's current ``pinned`` state without re-fetching.
    live_msgs: dict[int, discord.Message] = {}

    def _bail(status: str, detail: str) -> SyncResult:
        # Preserve every still-known message id so an error never loses track of
        # a live message (which would orphan it). Order isn't guaranteed here —
        # the next successful sync reconciles it.
        tracked = set(result.message_ids)
        result.message_ids.extend(m for m in existing_ids if m not in tracked)
        result.status = status
        result.detail = detail
        return result

    # ``torn`` flips once a tracked message is missing (manually deleted).
    # ``channel.send`` always appends at the bottom, so once the position→message
    # mapping tears we can't keep editing later slots in place — that would leave
    # the channel visually out of order versus what we store. From the tear down
    # we rebuild fresh, and every non-reused tracked message is deleted at the end.
    torn = False
    for i, embed in enumerate(embeds):
        if not torn and i < len(existing_ids):
            mid = existing_ids[i]
            try:
                msg = await channel.fetch_message(mid)
                await msg.edit(embed=embed)
                result.message_ids.append(mid)
                live_msgs[mid] = msg
                result.edited += 1
                continue
            except discord.NotFound:
                torn = True  # this slot's message is gone — rebuild the tail
            except discord.Forbidden:
                return _bail("forbidden", "Missing permission to edit an existing message.")
            except discord.HTTPException as exc:
                log.warning("doc edit failed in %d: %s", channel_id, exc)
                return _bail("error", str(exc))
        try:
            msg = await channel.send(embed=embed)
        except discord.Forbidden:
            return _bail("forbidden", "Missing permission to post in this channel.")
        except discord.HTTPException as exc:
            log.warning("doc post failed in %d: %s", channel_id, exc)
            return _bail("error", str(exc))
        result.message_ids.append(msg.id)
        live_msgs[msg.id] = msg
        result.created += 1

    # Delete every tracked message we didn't reuse: surplus from a now-shorter
    # doc, plus (if torn) the messages after the tear that we re-sent fresh.
    reused = set(result.message_ids)
    for mid in existing_ids:
        if mid in reused:
            continue
        try:
            msg = await channel.fetch_message(mid)
            await msg.delete()
            result.deleted += 1
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    await _reconcile_pins(channel, result, live_msgs, pinned)
    return result


async def _reconcile_pins(
    channel,
    result: SyncResult,
    live_msgs: dict[int, "discord.Message"],
    pinned: bool,
) -> None:
    """Pin/unpin the placement's messages to match ``pinned`` — delta-only.

    Pinning a message posts a "pinned a message" system notice; unpinning does
    not. So we only ever act on a message whose current state differs from the
    target, making a steady-state sync a true no-op (no notice spam on re-sync
    or edit). When pinning, we go in reverse: Discord shows the most recently
    pinned message first, so pinning bottom-up leaves the pins list reading
    top-to-bottom in doc order. A permission or pin-limit failure is recorded in
    ``pin_detail`` and never downgrades ``status`` — the messages are fine.
    """
    result.pinned = pinned
    ordered = [live_msgs[mid] for mid in result.message_ids if mid in live_msgs]
    try:
        if pinned:
            for msg in reversed(ordered):
                if not msg.pinned:
                    await msg.pin(reason="doc pinned")
        else:
            for msg in ordered:
                if msg.pinned:
                    await msg.unpin(reason="doc unpinned")
    except discord.Forbidden:
        result.pin_detail = "missing Manage Messages — couldn't pin"
    except discord.HTTPException as exc:
        # e.g. code 30003 "Maximum number of pins reached (50)".
        result.pin_detail = f"couldn't pin: {getattr(exc, 'text', '') or exc}"


# ── orchestration (called from cog + web route) ─────────────────────

async def _render_embeds(
    ctx: "AppContext", guild: discord.Guild, doc_row: dict
) -> list[discord.Embed]:
    color = await _resolve_color(ctx, guild, doc_row)
    specs = render_doc(doc_row.get("title", ""), doc_row.get("body_md", ""))
    return specs_to_embeds(specs, color)


async def _sync_one(
    ctx: "AppContext",
    bot: "Bot",
    placement: dict,
    embeds: list[discord.Embed],
) -> SyncResult:
    placement_id = placement["id"]
    channel_id = placement["channel_id"]

    existing = await asyncio.to_thread(
        _read_message_ids, ctx, placement_id
    )
    result = await _sync_channel(
        bot, channel_id, embeds, existing, bool(placement.get("pinned"))
    )

    # Persist whatever ids we ended up with (unless the channel vanished).
    if result.status != "missing_channel":
        await asyncio.to_thread(
            _write_message_ids, ctx, placement_id, result.message_ids
        )
    return result


def _read_message_ids(ctx: "AppContext", placement_id: int) -> list[int]:
    with ctx.open_db() as conn:
        return docs_db.get_placement_message_ids(conn, placement_id)


def _write_message_ids(
    ctx: "AppContext", placement_id: int, message_ids: list[int]
) -> None:
    with ctx.open_db() as conn:
        docs_db.set_placement_message_ids(conn, placement_id, message_ids, time.time())


async def sync_doc(
    ctx: "AppContext", guild: discord.Guild, doc_row: dict
) -> list[SyncResult]:
    """Re-render the doc into every channel it's placed in."""
    bot = ctx.bot
    if bot is None:
        return []
    placements = await asyncio.to_thread(_read_placements, ctx, doc_row["id"])
    if not placements:
        return []
    embeds = await _render_embeds(ctx, guild, doc_row)
    return [await _sync_one(ctx, bot, p, embeds) for p in placements]


async def post_doc(
    ctx: "AppContext", guild: discord.Guild, doc_row: dict, channel_id: int
) -> SyncResult:
    """Ensure a placement exists for ``channel_id`` and render the doc into it."""
    bot = ctx.bot
    if bot is None:
        return SyncResult(channel_id, status="error", detail="Bot unavailable.")
    placement = await asyncio.to_thread(
        _ensure_placement, ctx, doc_row["id"], channel_id
    )
    embeds = await _render_embeds(ctx, guild, doc_row)
    return await _sync_one(ctx, bot, placement, embeds)


async def set_pin(
    ctx: "AppContext",
    guild: discord.Guild,
    doc_row: dict,
    channel_id: int,
    pinned: bool,
) -> SyncResult | None:
    """Set a placement's pin flag and re-sync just that channel to enforce it.

    Returns ``None`` if the doc isn't placed in ``channel_id``.
    """
    bot = ctx.bot
    placement = await asyncio.to_thread(_read_placement, ctx, doc_row["id"], channel_id)
    if placement is None:
        return None
    await asyncio.to_thread(_set_pinned, ctx, placement["id"], pinned)
    placement["pinned"] = pinned
    if bot is None:
        return SyncResult(channel_id, status="error", detail="Bot unavailable.")
    embeds = await _render_embeds(ctx, guild, doc_row)
    return await _sync_one(ctx, bot, placement, embeds)


async def unpost_doc(
    ctx: "AppContext", doc_row: dict, channel_id: int
) -> bool:
    """Delete the doc's messages in ``channel_id`` and drop the placement."""
    bot = ctx.bot
    placement = await asyncio.to_thread(_read_placement, ctx, doc_row["id"], channel_id)
    if placement is None:
        return False
    existing = await asyncio.to_thread(_read_message_ids, ctx, placement["id"])
    if bot is not None:
        channel = await _resolve_channel(bot, channel_id)
        if channel is not None:
            for mid in existing:
                try:
                    msg = await channel.fetch_message(mid)
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
    await asyncio.to_thread(_delete_placement, ctx, placement["id"])
    return True


# ── tiny threaded db shims ──────────────────────────────────────────

def _read_placements(ctx: "AppContext", doc_id: int) -> list[dict]:
    with ctx.open_db() as conn:
        return docs_db.list_placements(conn, doc_id)


def _read_placement(ctx: "AppContext", doc_id: int, channel_id: int) -> dict | None:
    with ctx.open_db() as conn:
        return docs_db.get_placement(conn, doc_id, channel_id)


def _ensure_placement(ctx: "AppContext", doc_id: int, channel_id: int) -> dict:
    with ctx.open_db() as conn:
        docs_db.upsert_placement(conn, doc_id, channel_id, time.time())
        placement = docs_db.get_placement(conn, doc_id, channel_id)
        assert placement is not None  # just upserted
        return placement


def _set_pinned(ctx: "AppContext", placement_id: int, pinned: bool) -> None:
    with ctx.open_db() as conn:
        docs_db.set_placement_pinned(conn, placement_id, pinned, time.time())


def _delete_placement(ctx: "AppContext", placement_id: int) -> None:
    with ctx.open_db() as conn:
        docs_db.delete_placement(conn, placement_id)
