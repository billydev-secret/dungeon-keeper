"""Metadata endpoints: /api/me, /api/meta/* lookups, and /api/system/stats."""

from __future__ import annotations

import time

import psutil
from fastapi import APIRouter, Depends, Request

from web.auth import AuthenticatedUser
from web.deps import get_ctx, require_perms, run_query
from web.schemas import ChannelMeta, MemberMeta, MeResponse, RoleMeta

router = APIRouter()


@router.get("/me", response_model=MeResponse)
async def me(
    request: Request,
    user: AuthenticatedUser = Depends(require_perms(set())),
):
    ctx = get_ctx(request)
    guild = ctx.bot.get_guild(ctx.guild_id)
    return MeResponse(
        user_id=str(user.user_id),
        username=user.username,
        perms=sorted(user.perms),
        role_ids=[str(r) for r in user.role_ids],
        role_names=list(user.role_names),
        guild_id=str(ctx.guild_id),
        guild_name=guild.name if guild else None,
    )


@router.get("/meta/roles", response_model=list[RoleMeta])
async def meta_roles(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot is not None else None
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
            (ctx.guild_id,),
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
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot is not None else None
    if guild is not None:
        return sorted(
            [
                MemberMeta(
                    id=str(m.id),
                    name=m.name,
                    display_name=m.display_name,
                )
                for m in guild.members
                if not m.bot
            ],
            key=lambda m: m.display_name.lower(),
        )

    # Fallback: known_users table
    def _q():
        with ctx.open_db() as conn:
            rows = conn.execute(
                "SELECT user_id, username, display_name FROM known_users WHERE guild_id = ? ORDER BY display_name COLLATE NOCASE",
                (ctx.guild_id,),
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
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    import discord

    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot is not None else None
    if guild is not None:
        return [
            ChannelMeta(
                id=str(ch.id),
                name=ch.name,
                type="text" if isinstance(ch, discord.TextChannel) else "thread",
                category=ch.category.name if ch.category is not None else None,
                nsfw=getattr(ch, "nsfw", False),
            )
            for ch in guild.channels
            if isinstance(ch, (discord.TextChannel, discord.Thread))
        ]

    # Fallback: derive channel list from messages table.
    with ctx.open_db() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT channel_id
            FROM processed_messages
            WHERE guild_id = ?
            ORDER BY channel_id
            """,
            (ctx.guild_id,),
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
