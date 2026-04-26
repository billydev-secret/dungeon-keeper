"""Gender classification endpoints — admin-only NSFW analytics tagging."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from services.gender_service import (
    VALID_GENDERS,
    get_gender_map,
    get_unclassified_member_ids,
    set_gender,
)
from web.auth import AuthenticatedUser
from web.deps import get_active_guild_id, get_ctx, require_perms, run_query
from web.helpers import resolve_names as _resolve_names
from web.schemas import (
    GenderListResponse,
    GenderSetRequest,
    GenderUnclassifiedResponse,
    OkResponse,
)

router = APIRouter()


@router.get("/list", response_model=GenderListResponse)
async def list_classified(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is None:
        raise HTTPException(503, "Guild not available")

    member_ids = [m.id for m in guild.members if not m.bot]

    def _q():
        with ctx.open_db() as conn:
            gmap = get_gender_map(conn, guild_id, member_ids)
            rows: list[dict] = []
            for uid, gender in gmap.items():
                meta = conn.execute(
                    "SELECT set_by, set_at FROM member_gender "
                    "WHERE guild_id = ? AND user_id = ?",
                    (guild_id, uid),
                ).fetchone()
                rows.append(
                    {
                        "user_id": str(uid),
                        "display_name": "",
                        "gender": gender,
                        "set_by": str(int(meta["set_by"])) if meta else "0",
                        "set_at": float(meta["set_at"]) if meta else 0.0,
                    }
                )
            rows.sort(key=lambda r: r["set_at"], reverse=True)
            return {"classified": rows}

    result = await run_query(_q)
    _resolve_names(ctx, guild, result["classified"], ("user_id", "display_name"))
    return result


@router.get("/unclassified", response_model=GenderUnclassifiedResponse)
async def list_unclassified(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is None:
        raise HTTPException(503, "Guild not available")

    members_by_id = {m.id: m for m in guild.members if not m.bot}
    member_ids = list(members_by_id.keys())

    def _q():
        with ctx.open_db() as conn:
            return get_unclassified_member_ids(conn, guild_id, member_ids)

    unclassified_ids = await run_query(_q)
    rows = [
        {
            "user_id": str(uid),
            "display_name": members_by_id[uid].display_name if uid in members_by_id else "",
            "last_message_ts": None,
            "last_message_channel_id": None,
            "days_since_last": None,
        }
        for uid in unclassified_ids
        if uid in members_by_id
    ]
    rows.sort(key=lambda r: r["display_name"].lower())
    return {"members": rows, "total": len(rows)}


@router.post("/set", response_model=OkResponse)
async def set_member_gender(
    request: Request,
    body: GenderSetRequest,
    user: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    if body.gender not in VALID_GENDERS:
        raise HTTPException(
            400, f"gender must be one of {VALID_GENDERS}"
        )

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    user_id_int = int(body.user_id)
    actor_id = int(user.user_id)

    def _q():
        with ctx.open_db() as conn:
            set_gender(conn, guild_id, user_id_int, body.gender, set_by=actor_id)
            return True

    await run_query(_q)
    return {"ok": True, "message": f"Set gender for {body.user_id}"}
