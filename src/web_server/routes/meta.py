"""Metadata endpoints: /api/me, /api/meta/* lookups, and /api/system/stats."""

from __future__ import annotations

import os
import time

import psutil
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from bot_modules.services.economy_service import load_econ_settings
from web_server.auth import AuthenticatedUser, DiscordOAuthAuth, SESSION_COOKIE
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query
from web_server.schemas import ChannelMeta, GuildInfo, MemberMeta, MeResponse, RoleMeta

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


def _me_extras(ctx, guild_id: int, user_id: int) -> tuple[str | None, str | None, bool]:
    """Per-guild feature fields for MeResponse: games editor role, economy
    manager role, and whether this user is an active wellness participant
    (drives the Wellness nav gate — the panels' own opted-in truth, not a
    role-name string match)."""
    with ctx.open_db() as conn:
        row = conn.execute(
            "SELECT role_id FROM games_editor_role WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        games_role = str(row["role_id"]) if row else None
        econ_role_id = load_econ_settings(conn, guild_id).manager_role_id
        econ_role = str(econ_role_id) if econ_role_id else None
        wrow = conn.execute(
            "SELECT opted_in_at, opted_out_at FROM wellness_users "
            "WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        wellness = bool(
            wrow
            and wrow["opted_in_at"] is not None
            and wrow["opted_out_at"] is None
        )
        return games_role, econ_role, wellness


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

    games_editor_role_id, economy_manager_role_id, wellness_opted_in = await run_query(
        _me_extras, ctx, guild_id, user.user_id
    )

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
        games_editor_role_id=games_editor_role_id,
        economy_manager_role_id=economy_manager_role_id,
        wellness_opted_in=wellness_opted_in,
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
    support_user_id = int(os.getenv("SUPPORT_USER_ID", "0") or "0")
    is_support = support_user_id != 0 and user.user_id == support_user_id
    if target_guild:
        member = target_guild.get_member(user.user_id)
        if not member and not is_support:
            raise HTTPException(403, "You are not a member of that server")

    # Re-capture permissions for the *target* guild before re-signing. The
    # session's stored bits describe the guild they were captured in; carrying
    # them across a switch would let a cache-miss fallback replay guild A's
    # admin rights inside guild B (B-SEC1). When the member can't be resolved
    # (bot offline / not a member) they are cleared, not inherited.
    from web_server.auth import resolve_discord_perms

    perms: list[str] = []
    role_ids: list[str] = []
    role_names: list[str] = []
    status: str | None = None
    permission_bits = 0
    if target_guild:
        member = target_guild.get_member(user.user_id)
        if member:
            permission_bits = member.guild_permissions.value
            perms = sorted(resolve_discord_perms(permission_bits))
            role_ids = [str(r.id) for r in member.roles if not r.is_default()]
            role_names = [r.name for r in member.roles if not r.is_default()]
            status = str(member.status)
    if is_support:
        perms = sorted({"admin", "moderator", "manage_server"})

    new_cookie = auth.update_session_guild(
        cookie,
        guild_id,
        permission_bits=permission_bits,
        role_ids=[int(r) for r in role_ids],
        role_names=list(role_names),
    )
    if not new_cookie:
        raise HTTPException(400, "Invalid guild selection")

    games_editor_role_id, economy_manager_role_id, wellness_opted_in = await run_query(
        _me_extras, ctx, guild_id, user.user_id
    )

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
        games_editor_role_id=games_editor_role_id,
        economy_manager_role_id=economy_manager_role_id,
        wellness_opted_in=wellness_opted_in,
    )

    from web_server.routes.oauth import _is_secure

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
    def _q():
        with ctx.open_db() as conn:
            return conn.execute(
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

    rows = await run_query(_q)
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


@router.get("/meta/bots", response_model=list[MemberMeta])
async def meta_bots(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    """Bot accounts in the guild.

    The counterpart to ``/meta/members``, which filters bots *out* — most
    pickers want humans. External game tracking wants the opposite: the thing
    being watched is another bot. Needs the live gateway member list, since
    ``known_users`` only tracks humans; returns empty rather than erroring when
    the bot is offline, so the panel can say "no bots found" instead of failing
    to load.
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot is not None else None
    if guild is None:
        return []
    return sorted(
        (
            MemberMeta(id=str(m.id), name=m.name, display_name=m.display_name)
            for m in guild.members
            if m.bot and m.id != (guild.me.id if guild.me else 0)
        ),
        key=lambda m: m.display_name.lower(),
    )


# ── /meta/members: bounded, but nobody unreachable ───────────────────
#
# This used to return every current member PLUS every `known_users` row ever
# recorded, unpaginated — a payload that grows monotonically with server churn
# and is fetched on almost every config panel's mount. A naive cap was rejected
# once already, and rightly: the dashboard's pickers filter a cached list
# CLIENT-side, so anything the cap drops becomes silently unselectable.
#
# Three modes, which together bound the payload without losing anyone:
#
#   (no params)  The default page: current members first (alphabetical), then
#                departed members to fill whatever budget is left. The live
#                roster is bounded by guild size — it does not grow with churn —
#                so a normal server still gets exactly the old payload, while
#                the `known_users` tail that *does* grow forever is what the
#                budget trims.
#   ?q=…         Server-side search across BOTH populations (username, display
#                name, and — for an all-digit query — the id). This is what
#                makes the cap safe: the departed member 5,000 rows down is one
#                keystroke away instead of unreachable.
#   ?ids=a,b,c   Exact resolution. A config pointing at someone who left years
#                ago still renders their name instead of a bare snowflake.
#
# The response shape is unchanged (a flat `list[MemberMeta]`, ids as strings),
# so every existing consumer keeps working against the default page.
MEMBER_PAGE_DEFAULT = 1000
MEMBER_PAGE_MAX = 2000
MEMBER_IDS_MAX = 200


def _parse_member_ids(ids: str) -> list[str]:
    """Digit-only, de-duplicated, capped id list from the `ids` query param."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in ids.split(","):
        tok = raw.strip()
        if not tok.isdigit() or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
        if len(out) >= MEMBER_IDS_MAX:
            break
    return out


def _like_escape(needle: str) -> str:
    """Neutralize LIKE wildcards so a search for "50%" isn't a match-all."""
    return needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _known_user_filter(needle: str, want_ids: list[str]) -> tuple[str, list]:
    """Extra WHERE fragment + params narrowing known_users to the request."""
    if want_ids:
        placeholders = ",".join("?" * len(want_ids))
        return f" AND user_id IN ({placeholders})", [int(i) for i in want_ids]
    if not needle:
        return "", []
    like = f"%{_like_escape(needle)}%"
    clause = (
        " AND (LOWER(username) LIKE ? ESCAPE '\\'"
        " OR LOWER(display_name) LIKE ? ESCAPE '\\'"
    )
    params: list = [like, like]
    if needle.isdigit():
        clause += " OR CAST(user_id AS TEXT) LIKE ? ESCAPE '\\'"
        params.append(like)
    return clause + ")", params


def _member_matches(member, needle: str, want_ids: list[str]) -> bool:
    """Does a live guild member belong in this request's slice?"""
    if want_ids:
        return str(member.id) in want_ids
    if not needle:
        return True
    if needle in (member.name or "").lower():
        return True
    if needle in (member.display_name or "").lower():
        return True
    return needle.isdigit() and needle in str(member.id)


def _query_known_users(
    ctx,
    guild_id: int,
    needle: str,
    want_ids: list[str],
    budget: int,
    exclude_ids: set[str],
    *,
    mark_left: bool,
) -> list[MemberMeta]:
    """Up to `budget` known_users rows, skipping ids in `exclude_ids`."""
    clause, params = _known_user_filter(needle, want_ids)
    out: list[MemberMeta] = []
    with ctx.open_db() as conn:
        cursor = conn.execute(
            "SELECT user_id, username, display_name FROM known_users"
            f" WHERE guild_id = ?{clause}"
            " ORDER BY display_name COLLATE NOCASE",
            [guild_id, *params],
        )
        # Streamed, not fetchall(): rows that are still current members are
        # dropped here, and excluding them in SQL would mean inlining every id
        # in the guild. Stopping as soon as `budget` survivors are found keeps
        # the work O(guild size + budget) rather than O(every user ever seen).
        for row in cursor:
            uid = str(row[0])
            if uid in exclude_ids:
                continue
            out.append(
                MemberMeta(
                    id=uid,
                    name=row[1] or uid,
                    display_name=row[2] or row[1] or uid,
                    left_server=mark_left,
                )
            )
            if len(out) >= budget:
                break
    # SQLite's NOCASE collation folds ASCII only; re-sort the page the way the
    # rest of the payload is sorted. Same rows either way — only their order.
    return sorted(out, key=lambda m: m.display_name.lower())


@router.get("/meta/members", response_model=list[MemberMeta])
async def meta_members(
    request: Request,
    q: str = "",
    ids: str = "",
    limit: int = MEMBER_PAGE_DEFAULT,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    needle = q.strip().lower()
    want_ids = _parse_member_ids(ids)
    budget = max(1, min(int(limit), MEMBER_PAGE_MAX))
    if want_ids:
        # An explicit id list is never trimmed below what was asked for: its
        # whole job is rendering the ids a config already references.
        budget = min(max(budget, len(want_ids)), MEMBER_PAGE_MAX)

    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot is not None else None
    if guild is not None:
        # Every human in the guild, filter or no filter — this is the exclusion
        # set for the departed half, so it must not be narrowed by the search.
        current_ids = {str(m.id) for m in guild.members if not m.bot}
        current_members = sorted(
            (
                MemberMeta(
                    id=str(m.id),
                    name=m.name,
                    display_name=m.display_name,
                )
                for m in guild.members
                if not m.bot and _member_matches(m, needle, want_ids)
            ),
            key=lambda m: m.display_name.lower(),
        )[:budget]

        remaining = budget - len(current_members)
        if remaining <= 0:
            return current_members
        left_members = await run_query(
            _query_known_users,
            ctx,
            guild_id,
            needle,
            want_ids,
            remaining,
            current_ids,
            mark_left=True,
        )
        return current_members + left_members

    # Fallback: known_users table (no live gateway cache). Nobody can be marked
    # as departed here — without the guild there is nothing to compare against.
    return await run_query(
        _query_known_users,
        ctx,
        guild_id,
        needle,
        want_ids,
        budget,
        set(),
        mark_left=False,
    )


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

    def _q_ch():
        with ctx.open_db() as conn:
            return conn.execute(
                """
                SELECT DISTINCT channel_id
                FROM processed_messages
                WHERE guild_id = ?
                ORDER BY channel_id
                """,
                (guild_id,),
            ).fetchall()

    rows = await run_query(_q_ch)
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
