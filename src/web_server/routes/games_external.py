"""External game tracking — watch another bot's results channel and bank them.

Replaced ``/games track watch|status|disable|enable|sample`` (2026-07-28). This
is real sit-down configuration — pick a channel, pick a bot, pick a parser — so
it belongs on the dashboard rather than behind five slash commands.

The panel is strictly better than the commands at one thing: ``disable`` and
``enable`` needed a "which bot?" argument, and refused with "several bots are
tracked, pass the bot option" when a guild watched more than one. A list with a
toggle per row makes that ambiguity impossible.

DB work goes through ``GamesDb(ctx.db_path)`` rather than ``ctx.bot.games_db``,
so listing and toggling still work while the bot is offline. The one thing that
does need the live bot is refreshing the cog's in-memory watch cache — without
it a newly-watched channel is ignored until the next restart — so writes ask for
it and say so when it isn't there.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from bot_modules.games_external import logic
from bot_modules.services.games_db import GamesDb
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms

router = APIRouter()

_MOD = Depends(require_perms({"moderator"}))

_SAMPLE_MAX = 100


class WatchRequest(BaseModel):
    channel_id: str
    bot_id: str
    kind: str


class ToggleRequest(BaseModel):
    bot_id: str
    enabled: bool
    #: Omit to apply to every channel this bot is watched in — pausing a chatty
    #: bot shouldn't mean naming its channels one by one.
    channel_id: str | None = None


def _db(request: Request) -> GamesDb:
    return GamesDb(get_ctx(request).db_path)


async def _refresh_cog_cache(request: Request, guild_id: int) -> bool:
    """Poke the listener's in-memory cache. False if the bot isn't available.

    The write itself has already landed in the database, so a False here means
    "saved, but not live until restart" — which the caller surfaces rather than
    swallowing.
    """
    bot = getattr(get_ctx(request), "bot", None)
    cog = bot.get_cog("GamesExternalCog") if bot else None
    if cog is None:
        return False
    await cog.refresh_watch_cache(guild_id)
    return True


@router.get("/games-external")
async def get_tracking(request: Request, _: AuthenticatedUser = _MOD):
    """Every watch for this guild plus how much each has banked.

    Counts are narrowed by both bot *and* channel: a bot watched in several
    channels has one row each, and an unscoped count would report the bot's
    whole total against every one of them.
    """
    db = _db(request)
    guild_id = get_active_guild_id(request)

    watches = await logic.list_watches(db, guild_id)
    rows = []
    for w in watches:
        bot_user_id = int(w["bot_user_id"])
        channel_id = int(w["channel_id"])
        rows.append(
            {
                "channel_id": str(channel_id),
                "bot_user_id": str(bot_user_id),
                "kind": str(w["kind"]),
                "kind_label": logic.WATCH_KIND_LABELS.get(
                    str(w["kind"]), str(w["kind"])
                ),
                "enabled": bool(w["enabled"]),
                "banked": await logic.count_messages(
                    db, guild_id, bot_user_id, channel_id
                ),
            }
        )
    return {
        "watches": rows,
        "kinds": [
            {"value": key, "label": label}
            for key, label in logic.WATCH_KIND_LABELS.items()
        ],
    }


@router.post("/games-external/watches")
async def add_watch(
    body: WatchRequest, request: Request, user: AuthenticatedUser = _MOD
):
    if body.kind not in logic.VALID_WATCH_KINDS:
        raise HTTPException(400, f"Unknown parser kind: {body.kind}")
    try:
        channel_id = int(body.channel_id)
        bot_id = int(body.bot_id)
    except (TypeError, ValueError):
        raise HTTPException(400, "channel_id and bot_id must be numeric ids")

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    # Watching a human posts nothing useful and silently banks their messages,
    # so refuse when we can actually tell. With the bot offline we can't, and
    # the id is accepted rather than blocking configuration on it.
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is not None:
        member = guild.get_member(bot_id)
        if member is not None and not member.bot:
            raise HTTPException(
                400,
                f"{member.display_name} isn't a bot account — pick the game bot "
                "itself, the one that posts the results.",
            )

    actor_id = int(getattr(user, "user_id", 0) or 0)
    await logic.set_watch(
        _db(request), guild_id, channel_id, bot_id, body.kind, actor_id
    )
    live = await _refresh_cog_cache(request, guild_id)
    return {"ok": True, "live": live}


@router.post("/games-external/watches/toggle")
async def toggle_watch(
    body: ToggleRequest, request: Request, _: AuthenticatedUser = _MOD
):
    try:
        bot_id = int(body.bot_id)
        channel_id = int(body.channel_id) if body.channel_id else None
    except (TypeError, ValueError):
        raise HTTPException(400, "bot_id and channel_id must be numeric ids")

    guild_id = get_active_guild_id(request)
    ok = await logic.set_watch_enabled(
        _db(request), guild_id, bot_id, body.enabled, channel_id
    )
    if not ok:
        raise HTTPException(404, "That bot isn't configured for this server.")
    live = await _refresh_cog_cache(request, guild_id)
    return {"ok": True, "live": live}


@router.get("/games-external/sample")
async def sample_messages(
    request: Request,
    channel_id: str = Query(...),
    bot_id: str = Query(...),
    count: int = Query(40, ge=1, le=_SAMPLE_MAX),
    _: AuthenticatedUser = _MOD,
):
    """Raw banked messages for one (channel, bot), oldest-first.

    The point is confirming a parser matches the bot's actual output, so this
    returns content and embeds verbatim rather than anything prettified.
    """
    try:
        channel = int(channel_id)
        author = int(bot_id)
    except (TypeError, ValueError):
        raise HTTPException(400, "channel_id and bot_id must be numeric ids")

    now_iso = datetime.now(timezone.utc).isoformat()
    rows = await logic.recent_channel_messages(
        _db(request), get_active_guild_id(request), channel, author, now_iso, count
    )
    return {
        "messages": [
            {
                "message_id": str(r["message_id"]),
                "created_at": r["created_at"],
                "content": r["content"],
                "embeds_json": r["embeds_json"],
            }
            for r in rows
        ]
    }
