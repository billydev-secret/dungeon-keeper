"""Billy-bot endpoints — the Help panel's "Ask Billy-bot" box + its config.

The advisor is grounded, member-facing help (not admin config), so the ask
endpoint is open to any authenticated dashboard user; the config endpoints are
admin-gated. Heavy lifting lives in ``bot_modules.services.advisor_service`` and
``advisor_context``; this is thin glue. Rate limiting on the ask endpoint is the
``ai`` tier in ``server.py`` (see ``_TIER_ROUTES``).

Live per-server context is opt-in per guild and off by default — when disabled,
the ask endpoint answers from the manual alone.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from web_server.auth import AuthenticatedUser
from web_server.deps import (
    get_active_guild_id,
    get_ctx,
    require_perms,
    run_query,
)

router = APIRouter()


# ── POST /help/advisor — ask Billy-bot ─────────────────────────────────────


class AdvisorBody(BaseModel):
    question: str
    # Prior [{role, content}] turns for a multi-message chat; sanitized service-side.
    history: list[dict] | None = None


@router.post("/help/advisor")
async def help_advisor(
    request: Request,
    body: AdvisorBody,
    guild_id: int = Depends(get_active_guild_id),
    # Empty perm set = "any authenticated user" (help is not admin config).
    user: AuthenticatedUser = Depends(require_perms(set())),
):
    from bot_modules.services.advisor_context import (
        build_asker_context,
        visible_text_channels,
    )
    from bot_modules.services.advisor_service import (
        MODEL,
        answer_advisor,
        get_advisor_context_enabled,
        get_advisor_model,
    )

    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None

    model = MODEL
    guild_context = None
    channels: dict[str, str] = {}
    if guild is not None:
        with ctx.open_db() as conn:
            model = get_advisor_model(conn, guild_id)
            context_on = get_advisor_context_enabled(conn, guild_id)
        if context_on:
            # Resolve the dashboard user to a guild member so the context is
            # scoped to what THEY can see; fall back to public (@everyone) if
            # they aren't a resolvable member.
            member = guild.get_member(user.user_id)
            guild_context = build_asker_context(guild, member, ctx.db_path)
            # Only the asker's visible channels — used to turn <#id> mentions in
            # the answer into links, and never a channel they can't see.
            channels = {str(ch.id): ch.name for ch in visible_text_channels(guild, member)}

    result = await answer_advisor(
        body.question, body.history, model=model, guild_context=guild_context
    )
    return {
        "ok": result.ok,
        "answer": result.answer,
        "guild_id": str(guild_id),
        "channels": channels,
    }


# ── GET/PUT /config/advisor — the "Billy-bot" config panel ─────────────────


class AdvisorConfigBody(BaseModel):
    model: str
    server_context: bool


@router.get("/config/advisor")
async def get_advisor_config(
    request: Request,
    guild_id: int = Depends(get_active_guild_id),
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    from bot_modules.services.advisor_service import (
        ADVISOR_MODELS,
        get_advisor_context_enabled,
        get_advisor_model,
    )

    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            return {
                "model": get_advisor_model(conn, guild_id),
                "server_context": get_advisor_context_enabled(conn, guild_id),
            }

    cfg = await run_query(_q)
    return {**cfg, "models": ADVISOR_MODELS}


@router.put("/config/advisor")
async def put_advisor_config(
    request: Request,
    body: AdvisorConfigBody,
    guild_id: int = Depends(get_active_guild_id),
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    from bot_modules.services.advisor_service import (
        set_advisor_context_enabled,
        set_advisor_model,
    )

    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            set_advisor_model(conn, body.model, guild_id)
            set_advisor_context_enabled(conn, body.server_context, guild_id)

    try:
        await run_query(_q)
    except ValueError:
        raise HTTPException(400, "Unknown model")
    return {"ok": True}
