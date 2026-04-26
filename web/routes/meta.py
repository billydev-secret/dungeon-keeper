"""Metadata endpoints: /api/me, /api/meta/* lookups, and /api/system/stats."""

from __future__ import annotations

import time

import psutil
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from web.auth import AuthenticatedUser, DiscordOAuthAuth, SESSION_COOKIE
from web.deps import get_active_guild_id, get_ctx, require_perms, run_query
from web.schemas import ChannelMeta, GuildInfo, MemberMeta, MeResponse, RoleMeta

router = APIRouter()


def _guilds_from_session(request: Request) -> list[dict]:
    """Read the mutual guild list from the session cookie."""
    auth = request.app.state.auth
    if not isinstance(auth, DiscordOAuthAuth):
        return []
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return []
    session = auth.read_session(cookie)
    return session.get("guilds", []) if session else []


@router.get("/me", response_model=MeResponse)
async def me(
    request: Request,
    user: AuthenticatedUser = Depends(require_perms(set())),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    session_guilds = _guilds_from_session(request)
    status: str | None = None
    if guild is not None:
        member = guild.get_member(user.user_id)
        if member is not None:
            status = str(member.status)
    return MeResponse(
        user_id=str(user.user_id),
        username=user.username,
        perms=sorted(user.perms),
        role_ids=[str(r) for r in user.role_ids],
        role_names=list(user.role_names),
        guild_id=str(guild_id),
        guild_name=guild.name if guild else None,
        guilds=[
            GuildInfo(id=str(g["id"]), name=g["name"], icon=g.get("icon"))
            for g in session_guilds
        ],
        primary_guild_id=str(ctx.guild_id),
        avatar_url=user.avatar_url,
        status=status,
    )


@router.get("/guilds")
async def list_guilds(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms(set())),
):
    ctx = get_ctx(request)
    active = get_active_guild_id(request)
    guilds = _guilds_from_session(request)
    return {
        "active_guild_id": str(active),
        "primary_guild_id": str(ctx.guild_id),
        "guilds": [
            {"id": str(g["id"]), "name": g["name"], "icon": g.get("icon")}
            for g in guilds
        ],
    }


@router.post("/guilds/{guild_id}/select")
async def select_guild(
    guild_id: int,
    request: Request,
    user: AuthenticatedUser = Depends(require_perms(set())),
):
    auth = request.app.state.auth
    if not isinstance(auth, DiscordOAuthAuth):
        raise HTTPException(400, "Guild switching is not available in LAN mode")

    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        raise HTTPException(401, "Not authenticated")

    # Validate user is still a member of the target guild
    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    target_guild = bot.get_guild(guild_id) if bot else None
    if target_guild:
        member = target_guild.get_member(user.user_id)
        if not member:
            raise HTTPException(403, "You are not a member of that server")

    new_cookie = auth.update_session_guild(cookie, guild_id)
    if not new_cookie:
        raise HTTPException(400, "Invalid guild selection")

    # Build the response with updated user info for the new guild
    from web.auth import resolve_discord_perms

    perms: list[str] = []
    role_ids: list[str] = []
    role_names: list[str] = []
    status: str | None = None
    if target_guild:
        member = target_guild.get_member(user.user_id)
        if member:
            perms = sorted(resolve_discord_perms(member.guild_permissions.value))
            role_ids = [str(r.id) for r in member.roles if not r.is_default()]
            role_names = [r.name for r in member.roles if not r.is_default()]
            status = str(member.status)

    session_guilds = _guilds_from_session(request)
    body = MeResponse(
        user_id=str(user.user_id),
        username=user.username,
        perms=perms,
        role_ids=role_ids,
        role_names=role_names,
        guild_id=str(guild_id),
        guild_name=target_guild.name if target_guild else None,
        guilds=[
            GuildInfo(id=str(g["id"]), name=g["name"], icon=g.get("icon"))
            for g in session_guilds
        ],
        primary_guild_id=str(ctx.guild_id),
        avatar_url=user.avatar_url,
        status=status,
    )

    from web.routes.oauth import _is_secure

    response = JSONResponse(body.model_dump())
    response.set_cookie(
        SESSION_COOKIE,
        new_cookie,
        max_age=30 * 86400,
        httponly=True,
        samesite="lax",
        secure=_is_secure(),
        path="/",
    )
    return response


@router.get("/meta/roles", response_model=list[RoleMeta])
async def meta_roles(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot is not None else None
    if guild is not None:
        return [
            RoleMeta(
                id=str(role.id),
                name=role.name,
                color=f"#{role.color.value:06x}" if role.color.value else "#99aab5",
                member_count=len(role.members),
                position=role.position,
                managed=role.managed,
            )
            for role in sorted(guild.roles, key=lambda r: r.position, reverse=True)
            if not role.is_default()
        ]

    # Fallback: no live Discord cache (e.g. standalone dashboard mode).
    # Derive the list of roles from role_events history in the DB.
    with ctx.open_db() as conn:
        rows = conn.execute(
            """
            SELECT role_name,
                   SUM(CASE WHEN action = 'grant' THEN 1 ELSE -1 END) AS net
            FROM role_events
            WHERE guild_id = ?
            GROUP BY role_name
            ORDER BY role_name COLLATE NOCASE
            """,
            (guild_id,),
        ).fetchall()
    return [
        RoleMeta(
            id=str(
                abs(hash(r[0])) % (10**18)
            ),  # synthetic stable id; frontend filters by name
            name=str(r[0]),
            color="#99aab5",
            member_count=max(0, int(r[1] or 0)),
            position=0,
            managed=False,
        )
        for r in rows
    ]


@router.get("/meta/members", response_model=list[MemberMeta])
async def meta_members(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot is not None else None
    if guild is not None:
        current_members = [
            MemberMeta(
                id=str(m.id),
                name=m.name,
                display_name=m.display_name,
            )
            for m in guild.members
            if not m.bot
        ]
        current_ids = {m.id for m in current_members}

        def _q_left():
            with ctx.open_db() as conn:
                rows = conn.execute(
                    "SELECT user_id, username, display_name FROM known_users WHERE guild_id = ? ORDER BY display_name COLLATE NOCASE",
                    (guild_id,),
                ).fetchall()
            return [
                MemberMeta(
                    id=str(r[0]),
                    name=r[1] or str(r[0]),
                    display_name=r[2] or r[1] or str(r[0]),
                    left_server=True,
                )
                for r in rows
                if str(r[0]) not in current_ids
            ]

        left_members = await run_query(_q_left)
        return sorted(current_members, key=lambda m: m.display_name.lower()) + sorted(
            left_members, key=lambda m: m.display_name.lower()
        )

    # Fallback: known_users table
    def _q():
        with ctx.open_db() as conn:
            rows = conn.execute(
                "SELECT user_id, username, display_name FROM known_users WHERE guild_id = ? ORDER BY display_name COLLATE NOCASE",
                (guild_id,),
            ).fetchall()
        return [
            MemberMeta(
                id=str(r[0]),
                name=r[1] or str(r[0]),
                display_name=r[2] or r[1] or str(r[0]),
            )
            for r in rows
        ]

    return await run_query(_q)


@router.get("/meta/channels", response_model=list[ChannelMeta])
async def meta_channels(
    request: Request,
    types: str = "text,thread",
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    import discord

    requested = {t.strip().lower() for t in types.split(",") if t.strip()}
    type_map: list[tuple[type, str]] = [
        (discord.TextChannel, "text"),
        (discord.VoiceChannel, "voice"),
        (discord.CategoryChannel, "category"),
        (discord.Thread, "thread"),
    ]
    allowed = [(cls, label) for cls, label in type_map if label in requested]

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot is not None else None
    if guild is not None:
        out: list[ChannelMeta] = []
        for ch in guild.channels:
            label = next((lab for cls, lab in allowed if isinstance(ch, cls)), None)
            if label is None:
                continue
            parent = getattr(ch, "category", None)
            out.append(
                ChannelMeta(
                    id=str(ch.id),
                    name=ch.name,
                    type=label,
                    category=parent.name if parent is not None else None,
                    nsfw=getattr(ch, "nsfw", False),
                )
            )
        return out

    # Fallback: derive channel list from messages table (text channels only).
    if "text" not in requested:
        return []
    with ctx.open_db() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT channel_id
            FROM processed_messages
            WHERE guild_id = ?
            ORDER BY channel_id
            """,
            (guild_id,),
        ).fetchall()
    return [
        ChannelMeta(
            id=str(r[0]),
            name=str(r[0]),
            type="text",
        )
        for r in rows
    ]


# ── System stats ─────────────────────────────────────────────────────

# Snapshot of counters from the previous poll, used to compute rates.
_prev_net: dict[str, dict] = {}
_prev_net_ts: float = 0.0


@router.get("/system/stats")
async def system_stats(
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    global _prev_net, _prev_net_ts

    now = time.monotonic()
    net = psutil.net_io_counters(pernic=True)
    elapsed = now - _prev_net_ts if _prev_net_ts else 0.0

    interfaces: list[dict] = []
    for name, counters in sorted(net.items()):
        entry: dict = {
            "name": name,
            "bytes_sent": counters.bytes_sent,
            "bytes_recv": counters.bytes_recv,
            "packets_sent": counters.packets_sent,
            "packets_recv": counters.packets_recv,
            "errin": counters.errin,
            "errout": counters.errout,
            "dropin": counters.dropin,
            "dropout": counters.dropout,
        }
        if elapsed > 0 and name in _prev_net:
            prev = _prev_net[name]
            entry["send_rate"] = (
                max(0, (counters.bytes_sent - prev["bytes_sent"])) / elapsed
            )
            entry["recv_rate"] = (
                max(0, (counters.bytes_recv - prev["bytes_recv"])) / elapsed
            )
        else:
            entry["send_rate"] = 0
            entry["recv_rate"] = 0
        interfaces.append(entry)

    _prev_net = {
        name: {"bytes_sent": c.bytes_sent, "bytes_recv": c.bytes_recv}
        for name, c in net.items()
    }
    _prev_net_ts = now

    total = psutil.net_io_counters()
    total_send_rate = 0.0
    total_recv_rate = 0.0
    if elapsed > 0:
        total_send_rate = sum(i.get("send_rate", 0) for i in interfaces)
        total_recv_rate = sum(i.get("recv_rate", 0) for i in interfaces)

    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "memory": {
            "total": mem.total,
            "used": mem.used,
            "percent": mem.percent,
        },
        "disk": {
            "total": disk.total,
            "used": disk.used,
            "percent": disk.percent,
        },
        "network": {
            "total_bytes_sent": total.bytes_sent,
            "total_bytes_recv": total.bytes_recv,
            "send_rate": total_send_rate,
            "recv_rate": total_recv_rate,
        },
        "interfaces": interfaces,
        "uptime": time.time() - psutil.boot_time(),
    }
