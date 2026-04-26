"""Voice Master admin endpoints — config, channels, profile inspection."""

from __future__ import annotations

import logging
from typing import Any

import discord
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from services.moderation import write_audit
from services.voice_master_service import (
    add_name_blocklist,
    delete_active_channel,
    delete_profile,
    get_active_channel,
    list_active_channels,
    list_blocked,
    list_name_blocklist,
    list_trusted,
    load_profile,
    load_voice_master_config,
    remove_name_blocklist,
    set_owner,
    set_voice_master_config_value,
)
from web.auth import AuthenticatedUser
from web.deps import get_active_guild_id, get_ctx, require_perms, run_query

router = APIRouter()
log = logging.getLogger("dungeonkeeper.web.voice_master")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ConfigPayload(BaseModel):
    hub_channel_id: str
    category_id: str
    control_channel_id: str
    default_name_template: str
    default_user_limit: int
    default_bitrate: int
    create_cooldown_s: int
    max_per_member: int
    trust_cap: int
    block_cap: int
    owner_grace_s: int
    empty_grace_s: int
    trusted_prune_days: int
    disable_saves: bool
    saveable_fields: list[str]
    post_inline_panel: bool


class NameBlocklistAdd(BaseModel):
    pattern: str


class ForceTransferPayload(BaseModel):
    new_owner_id: int


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@router.get("/voice-master/config")
async def get_config(
    request: Request,
    user: AuthenticatedUser = Depends(require_perms({"admin"})),
) -> dict[str, Any]:
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q() -> dict[str, Any]:
        with ctx.open_db() as conn:
            cfg = load_voice_master_config(conn, guild_id)
            patterns = list_name_blocklist(conn, guild_id)
        return {
            "hub_channel_id": str(cfg.hub_channel_id),
            "category_id": str(cfg.category_id),
            "control_channel_id": str(cfg.control_channel_id),
            "panel_message_id": str(cfg.panel_message_id),
            "default_name_template": cfg.default_name_template,
            "default_user_limit": cfg.default_user_limit,
            "default_bitrate": cfg.default_bitrate,
            "create_cooldown_s": cfg.create_cooldown_s,
            "max_per_member": cfg.max_per_member,
            "trust_cap": cfg.trust_cap,
            "block_cap": cfg.block_cap,
            "owner_grace_s": cfg.owner_grace_s,
            "empty_grace_s": cfg.empty_grace_s,
            "trusted_prune_days": cfg.trusted_prune_days,
            "disable_saves": cfg.disable_saves,
            "saveable_fields": sorted(cfg.saveable_fields),
            "post_inline_panel": cfg.post_inline_panel,
            "name_blocklist": patterns,
        }

    return await run_query(_q)


@router.post("/voice-master/config")
async def set_config(
    request: Request,
    payload: ConfigPayload,
    user: AuthenticatedUser = Depends(require_perms({"admin"})),
) -> dict[str, str]:
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    valid_fields = {"name", "limit", "locked", "hidden", "trusted", "blocked"}
    chosen = {f.lower() for f in payload.saveable_fields}
    if not chosen.issubset(valid_fields):
        raise HTTPException(400, f"Unknown fields: {chosen - valid_fields}")

    def _to_id(raw: str) -> str:
        s = (raw or "").strip()
        if not s:
            return "0"
        try:
            return str(int(s))
        except ValueError:
            raise HTTPException(400, f"Invalid channel ID: {raw!r}")

    hub_id = _to_id(payload.hub_channel_id)
    category_id = _to_id(payload.category_id)
    control_id = _to_id(payload.control_channel_id)

    def _q() -> None:
        with ctx.open_db() as conn:
            set_voice_master_config_value(conn, guild_id, "voice_master_hub_channel_id", hub_id)
            set_voice_master_config_value(conn, guild_id, "voice_master_category_id", category_id)
            set_voice_master_config_value(conn, guild_id, "voice_master_control_channel_id", control_id)
            set_voice_master_config_value(conn, guild_id, "voice_master_default_name_template", payload.default_name_template)
            set_voice_master_config_value(conn, guild_id, "voice_master_default_user_limit", str(payload.default_user_limit))
            set_voice_master_config_value(conn, guild_id, "voice_master_default_bitrate", str(payload.default_bitrate))
            set_voice_master_config_value(conn, guild_id, "voice_master_create_cooldown_s", str(payload.create_cooldown_s))
            set_voice_master_config_value(conn, guild_id, "voice_master_max_per_member", str(payload.max_per_member))
            set_voice_master_config_value(conn, guild_id, "voice_master_trust_cap", str(payload.trust_cap))
            set_voice_master_config_value(conn, guild_id, "voice_master_block_cap", str(payload.block_cap))
            set_voice_master_config_value(conn, guild_id, "voice_master_owner_grace_s", str(payload.owner_grace_s))
            set_voice_master_config_value(conn, guild_id, "voice_master_empty_grace_s", str(payload.empty_grace_s))
            set_voice_master_config_value(conn, guild_id, "voice_master_trusted_prune_days", str(payload.trusted_prune_days))
            set_voice_master_config_value(conn, guild_id, "voice_master_disable_saves", "1" if payload.disable_saves else "0")
            set_voice_master_config_value(conn, guild_id, "voice_master_saveable_fields", ",".join(sorted(chosen)))
            set_voice_master_config_value(conn, guild_id, "voice_master_post_inline_panel", "1" if payload.post_inline_panel else "0")
            write_audit(
                conn,
                guild_id=guild_id,
                action="vm_config_set",
                actor_id=int(user.user_id),
                extra={"via": "web"},
            )

    await run_query(_q)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Name blocklist
# ---------------------------------------------------------------------------


@router.post("/voice-master/name-blocklist")
async def add_blocklist(
    request: Request,
    payload: NameBlocklistAdd,
    user: AuthenticatedUser = Depends(require_perms({"admin"})),
) -> dict[str, Any]:
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    pattern = payload.pattern.strip().lower()
    if not pattern:
        raise HTTPException(400, "Pattern can't be empty")

    def _q() -> bool:
        with ctx.open_db() as conn:
            added = add_name_blocklist(conn, guild_id, pattern, int(user.user_id))
            write_audit(
                conn,
                guild_id=guild_id,
                action="vm_name_blocklist_add",
                actor_id=int(user.user_id),
                extra={"pattern": pattern, "via": "web"},
            )
            return added

    added = await run_query(_q)
    return {"status": "ok", "added": added, "pattern": pattern}


@router.delete("/voice-master/name-blocklist/{pattern}")
async def remove_blocklist(
    pattern: str,
    request: Request,
    user: AuthenticatedUser = Depends(require_perms({"admin"})),
) -> dict[str, Any]:
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q() -> bool:
        with ctx.open_db() as conn:
            removed = remove_name_blocklist(conn, guild_id, pattern)
            if removed:
                write_audit(
                    conn,
                    guild_id=guild_id,
                    action="vm_name_blocklist_remove",
                    actor_id=int(user.user_id),
                    extra={"pattern": pattern, "via": "web"},
                )
            return removed

    removed = await run_query(_q)
    return {"status": "ok", "removed": removed}


# ---------------------------------------------------------------------------
# Active channels
# ---------------------------------------------------------------------------


@router.get("/voice-master/channels")
async def list_channels(
    request: Request,
    user: AuthenticatedUser = Depends(require_perms({"admin"})),
) -> dict[str, Any]:
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = ctx.bot
    guild = bot.get_guild(guild_id)

    def _db_q() -> list[Any]:
        with ctx.open_db() as conn:
            return list_active_channels(conn, guild_id)

    rows = await run_query(_db_q)

    out: list[dict[str, Any]] = []
    for r in rows:
        ch = guild.get_channel(r.channel_id) if guild else None
        owner = guild.get_member(r.owner_id) if guild else None
        member_count = (
            len([m for m in ch.members if not m.bot])
            if isinstance(ch, discord.VoiceChannel)
            else 0
        )
        out.append({
            "channel_id": r.channel_id,
            "channel_name": ch.name if ch else "(deleted)",
            "owner_id": r.owner_id,
            "owner_name": owner.display_name if owner else None,
            "owner_in_channel": (
                owner.voice is not None and owner.voice.channel is not None
                and owner.voice.channel.id == r.channel_id
            ) if owner else False,
            "members_count": member_count,
            "created_at": r.created_at,
            "owner_left_at": r.owner_left_at,
        })
    return {"channels": out}


@router.post("/voice-master/channels/{channel_id}/force-delete")
async def force_delete(
    channel_id: int,
    request: Request,
    user: AuthenticatedUser = Depends(require_perms({"admin"})),
) -> dict[str, str]:
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    guild = ctx.bot.get_guild(guild_id)
    if guild is None:
        raise HTTPException(404, "Guild not found")

    def _row_q() -> Any:
        with ctx.open_db() as conn:
            return get_active_channel(conn, channel_id)

    row = await run_query(_row_q)
    if row is None:
        raise HTTPException(404, "Channel not tracked by Voice Master")

    ch = guild.get_channel(channel_id)
    if not isinstance(ch, discord.VoiceChannel):
        # Already gone — just clean up the DB row.
        def _del() -> None:
            with ctx.open_db() as conn:
                delete_active_channel(conn, channel_id)
        await run_query(_del)
        return {"status": "ok", "note": "channel was already deleted"}

    try:
        await ch.delete(reason=f"Voice Master: web admin force-delete by {user.user_id}")
    except (discord.Forbidden, discord.HTTPException) as e:
        raise HTTPException(500, f"Discord error: {e}")

    def _persist() -> None:
        with ctx.open_db() as conn:
            delete_active_channel(conn, channel_id)
            write_audit(
                conn,
                guild_id=guild_id,
                action="vm_admin_force_delete",
                actor_id=int(user.user_id),
                target_id=row.owner_id,
                extra={"channel_id": channel_id, "via": "web"},
            )

    await run_query(_persist)
    return {"status": "ok"}


@router.post("/voice-master/channels/{channel_id}/force-transfer")
async def force_transfer(
    channel_id: int,
    payload: ForceTransferPayload,
    request: Request,
    user: AuthenticatedUser = Depends(require_perms({"admin"})),
) -> dict[str, str]:
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    guild = ctx.bot.get_guild(guild_id)
    if guild is None:
        raise HTTPException(404, "Guild not found")
    new_owner = guild.get_member(payload.new_owner_id)
    if new_owner is None:
        raise HTTPException(404, "New owner not in guild")
    if new_owner.bot:
        raise HTTPException(400, "Can't transfer ownership to a bot")
    ch = guild.get_channel(channel_id)
    if not isinstance(ch, discord.VoiceChannel):
        raise HTTPException(404, "Voice channel not found")

    def _row_q() -> Any:
        with ctx.open_db() as conn:
            return get_active_channel(conn, channel_id)

    row = await run_query(_row_q)
    if row is None:
        raise HTTPException(404, "Channel not tracked by Voice Master")

    overwrite = ch.overwrites_for(new_owner)
    overwrite.connect = True
    overwrite.view_channel = True
    try:
        await ch.set_permissions(
            new_owner,
            overwrite=overwrite,
            reason=f"Voice Master: web admin force-transfer by {user.user_id}",
        )
    except (discord.Forbidden, discord.HTTPException) as e:
        raise HTTPException(500, f"Discord error: {e}")

    def _persist() -> None:
        with ctx.open_db() as conn:
            set_owner(conn, channel_id, new_owner.id)
            write_audit(
                conn,
                guild_id=guild_id,
                action="vm_admin_force_transfer",
                actor_id=int(user.user_id),
                target_id=row.owner_id,
                extra={
                    "channel_id": channel_id,
                    "new_owner_id": new_owner.id,
                    "via": "web",
                },
            )

    await run_query(_persist)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Profile inspection (audit-logged)
# ---------------------------------------------------------------------------


@router.get("/voice-master/profiles/{user_id}")
async def get_profile(
    user_id: int,
    request: Request,
    user: AuthenticatedUser = Depends(require_perms({"admin"})),
) -> dict[str, Any]:
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q() -> dict[str, Any]:
        with ctx.open_db() as conn:
            profile = load_profile(conn, guild_id, user_id)
            trusted = list_trusted(conn, guild_id, user_id)
            blocked = list_blocked(conn, guild_id, user_id)
            write_audit(
                conn,
                guild_id=guild_id,
                action="vm_admin_view_profile",
                actor_id=int(user.user_id),
                target_id=user_id,
                extra={"via": "web"},
            )
        return {
            "user_id": user_id,
            "profile": (
                {
                    "saved_name": profile.saved_name,
                    "saved_limit": profile.saved_limit,
                    "locked": profile.locked,
                    "hidden": profile.hidden,
                    "bitrate": profile.bitrate,
                }
                if profile is not None
                else None
            ),
            "trusted": trusted,
            "blocked": blocked,
        }

    return await run_query(_q)


@router.post("/voice-master/profiles/{user_id}/clear")
async def clear_profile(
    user_id: int,
    request: Request,
    user: AuthenticatedUser = Depends(require_perms({"admin"})),
) -> dict[str, str]:
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q() -> None:
        with ctx.open_db() as conn:
            delete_profile(conn, guild_id, user_id)
            conn.execute(
                "DELETE FROM voice_master_trusted WHERE guild_id = ? AND owner_id = ?",
                (guild_id, user_id),
            )
            conn.execute(
                "DELETE FROM voice_master_blocked WHERE guild_id = ? AND owner_id = ?",
                (guild_id, user_id),
            )
            write_audit(
                conn,
                guild_id=guild_id,
                action="vm_admin_clear_profile",
                actor_id=int(user.user_id),
                target_id=user_id,
                extra={"via": "web"},
            )

    await run_query(_q)
    return {"status": "ok"}
