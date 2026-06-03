"""Bio resurrection — rebuild an archived bio embed when a member rejoins.

A bio is **archived** when the member has left the server: the embed
message in the bios channel was deleted (sentinel ``message_id == 0``)
but the snapshotted values + answers stay in the database. On rejoin we
rebuild the embed from the snapshot, post a fresh message, and update
the bios row to point at it.

The public entry point is :func:`resolve_member_bio_link`: it returns
the member's bio jump URL, resurrecting an archived bio if necessary
and returning the empty string when they don't have one.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import TYPE_CHECKING

import discord

from bot_modules.bios import db as bios_db
from bot_modules.bios.config import BiosConfig
from bot_modules.bios.embeds import build_bio_embed
from bot_modules.bios.logic import (
    BioRenderPayload,
    FieldSnapshot,
    QuestionSnapshot,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext

log = logging.getLogger("dungeonkeeper.bios.resurrect")


# ── Embed reconstruction from snapshot ───────────────────────────────


def _load_field_meta(
    conn: sqlite3.Connection, field_ids: list[int]
) -> dict[int, tuple[int, str, bool]]:
    """Return ``{field_id: (sort_order, field_type, is_headline)}`` for the
    given ids. Includes retired fields (no ``active=1`` filter)."""
    if not field_ids:
        return {}
    placeholders = ",".join("?" * len(field_ids))
    rows = conn.execute(
        f"SELECT id, sort_order, field_type, is_headline FROM bio_fields "
        f"WHERE id IN ({placeholders})",
        field_ids,
    ).fetchall()
    return {
        r["id"]: (r["sort_order"], r["field_type"], bool(r["is_headline"]))
        for r in rows
    }


def build_payload_from_stored(
    stored: bios_db.StoredBio,
    *,
    member_display_name: str,
    member_avatar_url: str,
    field_meta: dict[int, tuple[int, str, bool]],
    embed_color: int,
) -> BioRenderPayload:
    """Build a render payload from a stored snapshot + per-field meta."""
    field_entries: list[tuple[int, int, str, str, str, bool]] = []
    for fid, (label, value) in stored.field_values.items():
        sort_order, ftype, is_head = field_meta.get(fid, (0, "short", False))
        field_entries.append((sort_order, fid, label, value, ftype, is_head))
    field_entries.sort()

    field_snaps: list[FieldSnapshot] = []
    headline_value_str = "—"
    headline_set = False
    for _so, _fid, label, value, ftype, is_head in field_entries:
        if is_head and not headline_set:
            headline_value_str = value or label
            headline_set = True
        field_snaps.append(
            FieldSnapshot(
                label=label,
                value=value,
                field_type=ftype,  # type: ignore[arg-type]
                skipped=not value,
            )
        )
    if not headline_set and field_entries:
        first = field_entries[0]
        headline_value_str = first[3] or first[2]

    q_snaps: list[QuestionSnapshot] = []
    for slot in sorted(stored.answers.keys()):
        _qid, qtext, ans = stored.answers[slot]
        q_snaps.append(
            QuestionSnapshot(question_text=qtext, answer=ans, skipped=not ans)
        )

    return BioRenderPayload(
        display_name=member_display_name,
        avatar_url=member_avatar_url,
        headline_value=headline_value_str,
        fields=tuple(field_snaps),
        questions=tuple(q_snaps),
        embed_color=embed_color,
        created_at_iso=stored.created_at,
    )


# ── Public API ──────────────────────────────────────────────────────


async def resurrect_bio(
    ctx: "AppContext",
    bios_channel: discord.TextChannel,
    member: discord.Member,
    embed_color: int,
) -> str | None:
    """Repost the archived bio for ``member`` into ``bios_channel``.

    Returns the new message's jump URL on success, ``None`` when the
    member has no archived bio or the post fails.
    """
    guild_id = bios_channel.guild.id

    def _load() -> tuple[bios_db.StoredBio | None, dict[int, tuple[int, str, bool]]]:
        with ctx.open_db() as conn:
            stored = bios_db.get_user_bio(conn, guild_id, member.id)
            if stored is None:
                return None, {}
            meta = _load_field_meta(conn, list(stored.field_values.keys()))
            return stored, meta

    stored, meta = await asyncio.to_thread(_load)
    if stored is None:
        return None

    payload = build_payload_from_stored(
        stored,
        member_display_name=member.display_name,
        member_avatar_url=member.display_avatar.url,
        field_meta=meta,
        embed_color=embed_color,
    )
    embed = build_bio_embed(payload)

    try:
        new_msg = await bios_channel.send(embed=embed)
    except discord.HTTPException:
        log.exception("Failed to resurrect bio for %d", member.id)
        return None

    def _update() -> None:
        with ctx.open_db() as conn:
            bios_db.update_bio_message_ref(
                conn,
                guild_id=guild_id,
                user_id=member.id,
                message_id=new_msg.id,
                channel_id=bios_channel.id,
            )

    await asyncio.to_thread(_update)
    return new_msg.jump_url


async def resolve_member_bio_link(
    ctx: "AppContext", member: discord.Member
) -> str:
    """Return the jump URL for ``member``'s bio, resurrecting it if archived.

    Returns the empty string when the member has no bio (either truly
    deleted or never created). Resurrection is automatic — if the bios
    row exists with ``message_id == 0``, we rebuild the embed, post it,
    and return the new URL.

    Also moves the trigger button to the bottom of the bios channel
    after a resurrection (the new embed pushed it up).
    """
    guild = member.guild

    def _load() -> bios_db.StoredBio | None:
        with ctx.open_db() as conn:
            return bios_db.get_user_bio(conn, guild.id, member.id)

    stored = await asyncio.to_thread(_load)
    if stored is None:
        return ""

    if stored.message_id != 0 and stored.channel_id != 0:
        return (
            f"https://discord.com/channels/{guild.id}/"
            f"{stored.channel_id}/{stored.message_id}"
        )

    # Archived — resurrect.
    def _load_cfg() -> BiosConfig:
        with ctx.open_db() as conn:
            return BiosConfig.load(conn, guild.id)

    cfg = await asyncio.to_thread(_load_cfg)
    if not cfg.bios_channel_id:
        return ""

    bios_channel = guild.get_channel(cfg.bios_channel_id)
    if not isinstance(bios_channel, discord.TextChannel):
        return ""

    url = await resurrect_bio(ctx, bios_channel, member, cfg.embed_color)
    if url is None:
        return ""

    # Trigger button is now above the resurrected embed — move it back.
    try:
        from bot_modules.bios.trigger import reposition_trigger_button

        await reposition_trigger_button(ctx, bios_channel)
    except Exception:
        log.exception("Failed to reposition trigger after resurrection")

    return url


__all__ = [
    "build_payload_from_stored",
    "resurrect_bio",
    "resolve_member_bio_link",
]
