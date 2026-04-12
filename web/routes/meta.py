"""Metadata endpoints: /api/me and /api/meta/* lookups for frontend dropdowns."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from web.auth import AuthenticatedUser
from web.deps import get_ctx, require_perms, run_query
from web.schemas import ChannelMeta, MeResponse, MemberMeta, RoleMeta

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
            id=str(abs(hash(r[0])) % (10**18)),  # synthetic stable id; frontend filters by name
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
