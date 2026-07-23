"""AI advisor endpoints — the Help panel's ask box + its config.

The assistant's guild-facing name is branding (default "Billy-bot"), so both
the ask box and the config panel read it from ``branding_service`` rather than
printing a baked-in name.

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


# ── GET /help/advisor/name — what this guild calls its assistant ───────────


@router.get("/help/advisor/name")
async def help_advisor_name(
    request: Request,
    guild_id: int = Depends(get_active_guild_id),
    # Member-facing: the Help panel labels its ask box with this.
    _: AuthenticatedUser = Depends(require_perms(set())),
):
    from bot_modules.services.branding_service import resolve_assistant_name_conn

    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            return resolve_assistant_name_conn(conn, guild_id)

    return {"assistant_name": await run_query(_q)}


# ── POST /help/advisor — ask the assistant ─────────────────────────────────


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
        FEATURE_KEYS,
        build_asker_context,
        can_see_config,
        fetch_feature_settings,
        is_staff,
        visible_text_channels,
    )
    from bot_modules.services.advisor_service import (
        MODEL,
        AdvisorTools,
        answer_advisor,
        get_advisor_context_enabled,
        get_advisor_tools_enabled,
        resolve_advisor_model,
    )
    from bot_modules.services.branding_service import (
        DEFAULT_ASSISTANT_NAME,
        resolve_assistant_name_conn,
    )

    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None

    model = MODEL
    assistant_name = DEFAULT_ASSISTANT_NAME
    guild_context = None
    tools = None
    channels: dict[str, str] = {}
    if guild is not None:
        # Resolve the dashboard user to a guild member up front: it decides
        # which model handles the ask, and (when live context is on) scopes the
        # context to what THEY can see. Falls back to public (@everyone) when
        # they aren't a resolvable member.
        member = guild.get_member(user.user_id)
        with ctx.open_db() as conn:
            model = resolve_advisor_model(conn, guild_id, staff=is_staff(member))
            assistant_name = resolve_assistant_name_conn(conn, guild_id)
            context_on = get_advisor_context_enabled(conn, guild_id)
            tools_on = get_advisor_tools_enabled(conn, guild_id)
        if context_on:
            # Admins get on-demand settings lookup instead of the inline dump.
            # Read-only here: the dashboard edits settings in its own panels,
            # and the confirm-button flow only exists on the Discord surface.
            if tools_on and member is not None and can_see_config(member):
                from bot_modules.services.advisor_gaps import fetch_setup_gaps

                tools = AdvisorTools(
                    feature_keys=FEATURE_KEYS,
                    fetch_settings=lambda f, _m=member: fetch_feature_settings(
                        guild, _m, ctx.db_path, f
                    ),
                    fetch_gaps=lambda _m=member: fetch_setup_gaps(
                        ctx.db_path, guild_id, _m
                    ),
                )
            guild_context = build_asker_context(
                guild, member, ctx.db_path, include_config=tools is None
            )
            # Only the asker's visible channels — used to turn <#id> mentions in
            # the answer into links, and never a channel they can't see.
            channels = {str(ch.id): ch.name for ch in visible_text_channels(guild, member)}

    result = await answer_advisor(
        body.question, body.history, model=model, guild_context=guild_context,
        tools=tools, assistant_name=assistant_name,
    )
    return {
        "ok": result.ok,
        "answer": result.answer,
        "guild_id": str(guild_id),
        "channels": channels,
        "assistant_name": assistant_name,
    }


# ── GET /help/suggestions — "what isn't this server using?" ────────────────


@router.get("/help/suggestions")
async def help_suggestions(
    request: Request,
    limit: int = 3,
    guild_id: int = Depends(get_active_guild_id),
    # Admin-gated for the same reason the gap tool is: a list of what a server
    # hasn't set up is reconnaissance, and only admins can act on it anyway.
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """The dashboard suggestion widget's data — the pushed half of setup help.

    Same scan as Billy-bot's ``find_setup_gaps`` tool, rendered as structured
    rows instead of prose. No model call: this is a DB read against the
    registry, so it's cheap enough to sit on the home page.
    """
    from bot_modules.services.advisor_gaps import suggestions

    ctx = get_ctx(request)
    limit = max(1, min(int(limit), 10))

    def _q():
        with ctx.open_db() as conn:
            return suggestions(conn, guild_id, limit)

    gaps = await run_query(_q)
    return {
        "guild_id": str(guild_id),
        "suggestions": [
            {
                "slug": g.feature.slug,
                "label": g.feature.label,
                "blurb": g.feature.blurb,
                "panel": g.feature.panel,
                "status": g.status,
                "effort": g.effort,
                "missing": [{"key": s.key, "label": s.label} for s in g.missing],
            }
            for g in gaps
        ],
    }


# ── GET/PUT /config/advisor — the AI assistant config panel ────────────────


class AdvisorConfigBody(BaseModel):
    model: str
    staff_model: str
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
        get_advisor_staff_model,
    )
    from bot_modules.services.branding_service import resolve_assistant_name_conn

    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            return {
                "model": get_advisor_model(conn, guild_id),
                "staff_model": get_advisor_staff_model(conn, guild_id),
                "server_context": get_advisor_context_enabled(conn, guild_id),
                # Read-only here — the name is edited on the Branding panel.
                "assistant_name": resolve_assistant_name_conn(conn, guild_id),
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
        set_advisor_staff_model,
    )

    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            set_advisor_model(conn, body.model, guild_id)
            set_advisor_staff_model(conn, body.staff_model, guild_id)
            set_advisor_context_enabled(conn, body.server_context, guild_id)

    try:
        await run_query(_q)
    except ValueError:
        raise HTTPException(400, "Unknown model")
    return {"ok": True}
