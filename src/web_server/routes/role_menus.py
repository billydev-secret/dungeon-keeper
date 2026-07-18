"""Role Menus endpoints — build, preview data, and publish self-service menus.

The dashboard is the only authoring surface (no slash commands). Saving a
published menu re-renders the live message immediately, docs-style. Every
mutation lands in the ``audit_log``; using the elevated-role override is its
own loud audit action.
"""

from __future__ import annotations

import time

import discord
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from bot_modules.core.utils import get_bot_member
from bot_modules.role_menus import db as menus_db
from bot_modules.role_menus import sync as menus_sync
from bot_modules.services.moderation import write_audit
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query

router = APIRouter()

_MOD = Depends(require_perms({"moderator"}))

# Permission bits that make a role dangerous to hand out via self-service.
# Hidden in the picker unless the per-option "elevated" override is checked,
# and refused server-side without it (spec §3.2: misconfiguration should be
# nearly impossible, not merely handled).
_DANGEROUS_PERMS = (
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "manage_messages",
    "manage_webhooks",
    "kick_members",
    "ban_members",
    "moderate_members",
    "mention_everyone",
)


class MenuCreateBody(BaseModel):
    title: str = ""


class OptionBody(BaseModel):
    role_id: str
    label: str = ""
    emoji: str = ""
    description: str = ""
    button_color: str = "secondary"
    elevated: bool = False


class MenuUpdateBody(BaseModel):
    title: str = ""
    description: str = ""
    accent: str = ""
    thumbnail_url: str = ""
    style: str = "buttons"
    mode: str = "toggle"
    max_roles: int = Field(default=0, ge=0, le=25)
    required_role_id: str = "0"
    cooldown_seconds: int = Field(default=0, ge=0, le=3600)
    placeholder: str = ""
    options: list[OptionBody] = Field(default_factory=list)


class PublishBody(BaseModel):
    channel_id: str


class EnabledBody(BaseModel):
    enabled: bool


def _menu_json(menu: dict, options: list[dict], health: list[dict] | None = None) -> dict:
    return {
        "id": menu["id"],
        "title": menu["title"],
        "description": menu["description"],
        "accent": menu["accent"],
        "thumbnail_url": menu["thumbnail_url"],
        "style": menu["style"],
        "mode": menu["mode"],
        "max_roles": menu["max_roles"],
        "required_role_id": str(menu["required_role_id"]),
        "cooldown_seconds": menu["cooldown_seconds"],
        "placeholder": menu["placeholder"],
        "enabled": menu["enabled"],
        "channel_id": str(menu["channel_id"]),
        "message_id": str(menu["message_id"]),
        "published": bool(menu["message_id"]),
        "created_at": menu["created_at"],
        "updated_at": menu["updated_at"],
        "option_count": menu.get("option_count", len(options)),
        "options": [
            {
                "role_id": str(o["role_id"]),
                "label": o["label"],
                "emoji": o["emoji"],
                "description": o["description"],
                "button_color": o["button_color"],
                "elevated": o["elevated"],
            }
            for o in options
        ],
        "health": health if health is not None else [],
    }


def _sync_json(r: menus_sync.SyncResult) -> dict:
    return {
        "channel_id": str(r.channel_id),
        "status": r.status,
        "message_id": str(r.message_id),
        "detail": r.detail,
    }


def _require_guild(ctx, guild_id: int) -> discord.Guild:
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Bot is not connected to this server right now.",
        )
    return guild


def _load_menu(ctx, guild_id: int, menu_id: int) -> tuple[dict, list[dict]]:
    with ctx.open_db() as conn:
        menu = menus_db.get_menu(conn, menu_id)
        if menu is None or menu["guild_id"] != guild_id:
            raise HTTPException(status_code=404, detail="Menu not found.")
        return menu, menus_db.list_options(conn, menu_id)


def _guild_or_none(ctx, guild_id: int) -> discord.Guild | None:
    bot = getattr(ctx, "bot", None)
    return bot.get_guild(guild_id) if bot else None


def _dangerous(role: discord.Role) -> bool:
    perms = role.permissions
    return any(getattr(perms, name, False) for name in _DANGEROUS_PERMS)


# ── roles for the picker ────────────────────────────────────────────

@router.get("/role-menus/roles")
async def list_assignable_roles(request: Request, _: AuthenticatedUser = _MOD):
    """Roles DK can actually manage, with the danger flag for the picker."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    guild = _require_guild(ctx, guild_id)
    bot_member = get_bot_member(guild)
    top = bot_member.top_role if bot_member else None
    out = []
    for role in sorted(guild.roles, key=lambda r: r.position, reverse=True):
        if role.is_default() or role.managed:
            continue
        out.append(
            {
                "id": str(role.id),
                "name": role.name,
                "color": f"#{role.color.value:06x}" if role.color.value else "",
                "position": role.position,
                "assignable": top is not None and role < top,
                "dangerous": _dangerous(role),
                "member_count": len(role.members),
            }
        )
    return {"roles": out}


# ── list / read ─────────────────────────────────────────────────────

@router.get("/role-menus")
async def list_menus(request: Request, _: AuthenticatedUser = _MOD):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    guild = _guild_or_none(ctx, guild_id)

    def _q():
        with ctx.open_db() as conn:
            menus = menus_db.list_menus(conn, guild_id)
            return [(m, menus_db.list_options(conn, m["id"])) for m in menus]

    rows = await run_query(_q)
    out = []
    for menu, options in rows:
        health = menus_sync.menu_health(guild, menu, options) if guild else []
        out.append(_menu_json(menu, options, health))
    return {"menus": out}


@router.get("/role-menus/{menu_id}")
async def get_menu(request: Request, menu_id: int, _: AuthenticatedUser = _MOD):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    menu, options = await run_query(lambda: _load_menu(ctx, guild_id, menu_id))
    guild = _guild_or_none(ctx, guild_id)
    health = menus_sync.menu_health(guild, menu, options) if guild else []
    return _menu_json(menu, options, health)


# ── create / update / delete ────────────────────────────────────────

@router.post("/role-menus")
async def create_menu(
    request: Request, body: MenuCreateBody, user: AuthenticatedUser = _MOD
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    title = body.title.strip()[: menus_db.TITLE_MAX_LEN]

    def _q():
        with ctx.open_db() as conn:
            menu_id = menus_db.create_menu(conn, guild_id, title, user.user_id, time.time())
            write_audit(
                conn, guild_id=guild_id, action="role_menu.create",
                actor_id=user.user_id, target_id=menu_id, extra={"title": title},
            )
            menu = menus_db.get_menu(conn, menu_id)
            assert menu is not None  # just inserted
            return menu

    menu = await run_query(_q)
    return _menu_json(menu, [])


def _validate_update(body: MenuUpdateBody) -> list[dict]:
    """Field checks that don't need the guild; returns normalized options."""
    if body.style not in menus_db.STYLES:
        raise HTTPException(status_code=400, detail="Unknown style.")
    if body.mode not in menus_db.MODES:
        raise HTTPException(status_code=400, detail="Unknown mode.")
    if len(body.title) > menus_db.TITLE_MAX_LEN:
        raise HTTPException(status_code=400, detail="Title is too long.")
    if len(body.description) > menus_db.DESCRIPTION_MAX_LEN:
        raise HTTPException(status_code=400, detail="Description is too long.")
    if len(body.placeholder) > menus_db.PLACEHOLDER_MAX_LEN:
        raise HTTPException(status_code=400, detail="Placeholder is too long.")
    if len(body.options) > menus_db.MAX_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"A menu can hold at most {menus_db.MAX_OPTIONS} choices.",
        )

    options: list[dict] = []
    seen: set[int] = set()
    for opt in body.options:
        try:
            role_id = int(opt.role_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid role in options.")
        if role_id <= 0:
            raise HTTPException(status_code=400, detail="Every choice needs a role.")
        if role_id in seen:
            raise HTTPException(
                status_code=400, detail="The same role appears twice in this menu."
            )
        seen.add(role_id)
        label = opt.label.strip()
        if not label:
            raise HTTPException(status_code=400, detail="Every choice needs a label.")
        if len(label) > menus_db.LABEL_MAX_LEN:
            raise HTTPException(status_code=400, detail="A label is too long.")
        if len(opt.description) > menus_db.OPTION_DESC_MAX_LEN:
            raise HTTPException(status_code=400, detail="A choice description is too long.")
        if opt.button_color not in menus_db.BUTTON_COLORS:
            raise HTTPException(status_code=400, detail="Unknown button color.")
        options.append(
            {
                "role_id": role_id,
                "label": label,
                "emoji": opt.emoji.strip(),
                "description": opt.description.strip(),
                "button_color": opt.button_color,
                "elevated": opt.elevated,
            }
        )
    return options


def _check_roles_against_guild(
    guild: discord.Guild, options: list[dict]
) -> list[dict]:
    """Refuse unmanageable roles; return the dangerous ones actually used."""
    bot_member = get_bot_member(guild)
    elevated_used: list[dict] = []
    for opt in options:
        role = guild.get_role(opt["role_id"])
        if role is None:
            raise HTTPException(
                status_code=400,
                detail=f"The role for “{opt['label']}” doesn't exist (anymore).",
            )
        if role.managed or role.is_default():
            raise HTTPException(
                status_code=400,
                detail=f"“{role.name}” is managed by an integration and can't be self-assigned.",
            )
        if bot_member is not None and role >= bot_member.top_role:
            raise HTTPException(
                status_code=400,
                detail=f"“{role.name}” is above my highest role — I can't grant it.",
            )
        if _dangerous(role):
            if not opt["elevated"]:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"“{role.name}” carries elevated permissions. Check the"
                        " elevated-role override on that choice if you really mean it."
                    ),
                )
            elevated_used.append({"role_id": role.id, "name": role.name})
    return elevated_used


@router.put("/role-menus/{menu_id}")
async def update_menu(
    request: Request, menu_id: int, body: MenuUpdateBody, user: AuthenticatedUser = _MOD
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    options = _validate_update(body)
    guild = _require_guild(ctx, guild_id)
    elevated_used = _check_roles_against_guild(guild, options)

    try:
        required_role_id = int(body.required_role_id or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid required role.")

    def _save():
        now = time.time()
        with ctx.open_db() as conn:
            menu = menus_db.get_menu(conn, menu_id)
            if menu is None or menu["guild_id"] != guild_id:
                return None
            menus_db.update_menu(
                conn, menu_id,
                title=body.title.strip(),
                description=body.description,
                accent=body.accent.strip(),
                thumbnail_url=body.thumbnail_url.strip(),
                style=body.style,
                mode=body.mode,
                max_roles=body.max_roles,
                required_role_id=required_role_id,
                cooldown_seconds=body.cooldown_seconds,
                placeholder=body.placeholder.strip(),
                user_id=user.user_id,
                now=now,
            )
            menus_db.replace_options(conn, menu_id, options, now)
            write_audit(
                conn, guild_id=guild_id, action="role_menu.update",
                actor_id=user.user_id, target_id=menu_id,
                extra={"title": body.title.strip(), "options": len(options)},
            )
            for used in elevated_used:
                write_audit(
                    conn, guild_id=guild_id, action="role_menu.elevated_override",
                    actor_id=user.user_id, target_id=menu_id, extra=used,
                )
            menu = menus_db.get_menu(conn, menu_id)
            assert menu is not None  # just updated
            return menu, menus_db.list_options(conn, menu_id)

    result = await run_query(_save)
    if result is None:
        raise HTTPException(status_code=404, detail="Menu not found.")
    menu, saved_options = result

    sync_result = None
    if menu["message_id"]:
        sync_result = await menus_sync.sync_menu(ctx, guild, menu, saved_options)

    health = menus_sync.menu_health(guild, menu, saved_options)
    return {
        "ok": True,
        "menu": _menu_json(menu, saved_options, health),
        "sync": _sync_json(sync_result) if sync_result else None,
    }


@router.delete("/role-menus/{menu_id}")
async def delete_menu(request: Request, menu_id: int, user: AuthenticatedUser = _MOD):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    menu, _options = await run_query(lambda: _load_menu(ctx, guild_id, menu_id))

    await menus_sync.delete_menu_message(ctx, menu)

    def _del():
        with ctx.open_db() as conn:
            menus_db.delete_menu(conn, menu_id)
            write_audit(
                conn, guild_id=guild_id, action="role_menu.delete",
                actor_id=user.user_id, target_id=menu_id,
                extra={"title": menu["title"]},
            )

    await run_query(_del)
    return {"ok": True}


# ── publish lifecycle ───────────────────────────────────────────────

@router.post("/role-menus/{menu_id}/publish")
async def publish_menu(
    request: Request, menu_id: int, body: PublishBody, user: AuthenticatedUser = _MOD
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    guild = _require_guild(ctx, guild_id)
    try:
        channel_id = int(body.channel_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid channel.")

    menu, options = await run_query(lambda: _load_menu(ctx, guild_id, menu_id))
    if not options:
        raise HTTPException(
            status_code=400, detail="Add at least one choice before publishing."
        )
    _check_roles_against_guild(guild, options)

    result = await menus_sync.publish_menu(ctx, guild, menu, options, channel_id)
    if result.status == "missing_channel":
        raise HTTPException(status_code=400, detail="That channel isn't available.")

    def _audit():
        with ctx.open_db() as conn:
            write_audit(
                conn, guild_id=guild_id, action="role_menu.publish",
                actor_id=user.user_id, target_id=menu_id,
                extra={"channel_id": channel_id, "status": result.status},
            )

    await run_query(_audit)
    return {"ok": result.status == "ok", "sync": _sync_json(result)}


@router.put("/role-menus/{menu_id}/enabled")
async def set_menu_enabled(
    request: Request, menu_id: int, body: EnabledBody, user: AuthenticatedUser = _MOD
):
    """The list-view on/off toggle and the publish bar's Unpublish button.

    Off = menu rejects clicks and the live post greys out but stays as decor.
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    guild = _require_guild(ctx, guild_id)
    menu, options = await run_query(lambda: _load_menu(ctx, guild_id, menu_id))

    result = await menus_sync.set_menu_live_state(ctx, guild, menu, options, body.enabled)

    def _audit():
        with ctx.open_db() as conn:
            write_audit(
                conn, guild_id=guild_id,
                action="role_menu.enable" if body.enabled else "role_menu.disable",
                actor_id=user.user_id, target_id=menu_id,
            )

    await run_query(_audit)
    return {"ok": True, "enabled": body.enabled, "sync": _sync_json(result)}
