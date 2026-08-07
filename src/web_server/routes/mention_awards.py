"""Mention Awards API — CRUD over the four-lever award rules.

A rule is (channel, trigger phrase, amount, announcer role). The listener
(``mention_awards_cog``) reads them; this is the only way they're written.

Snowflakes go out as strings — a channel or role id past 2^53 loses precision
as a bare JSON number (see the dashboard's snowflake-precision sweep).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from bot_modules.mention_awards import store
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query

router = APIRouter()


class RuleBody(BaseModel):
    """The four levers. Ids arrive as strings for snowflake precision."""

    channel_id: str
    phrase: str = Field(min_length=1, max_length=store.MAX_PHRASE_LEN)
    amount: int = Field(ge=0, le=store.MAX_AMOUNT)
    announcer_role_id: str = "0"


def _as_id(raw: str, field: str) -> int:
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{field} must be a numeric id")


def _row(r) -> dict:
    return {
        "id": int(r["id"]),
        "channel_id": str(r["channel_id"]),
        "phrase": r["phrase"],
        "amount": int(r["amount"]),
        "announcer_role_id": str(r["announcer_role_id"] or 0),
        "created_by": str(r["created_by"]) if r["created_by"] else None,
        "created_at": r["created_at"],
    }


def _validated(body: RuleBody) -> tuple[int, int]:
    """Channel/role ids after the shared validation gate."""
    problem = store.validate(body.phrase, body.amount)
    if problem:
        raise HTTPException(400, problem)
    channel_id = _as_id(body.channel_id, "channel_id")
    if not channel_id:
        raise HTTPException(400, "Pick a channel for the rule to watch.")
    return channel_id, _as_id(body.announcer_role_id, "announcer_role_id")


@router.get("/mention-awards/rules")
async def list_rules(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            return [_row(r) for r in store.list_rules(conn, guild_id)]

    return await run_query(_q)


@router.post("/mention-awards/rules")
async def create_rule(
    body: RuleBody,
    request: Request,
    user: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    channel_id, role_id = _validated(body)
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            rule_id = store.create_rule(
                conn, guild_id, channel_id=channel_id, phrase=body.phrase,
                amount=body.amount, announcer_role_id=role_id,
                created_by=int(user.user_id),
            )
            return {"id": rule_id}

    return await run_query(_q)


@router.put("/mention-awards/rules/{rule_id}")
async def update_rule(
    rule_id: int,
    body: RuleBody,
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    channel_id, role_id = _validated(body)
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            if not store.update_rule(
                conn, guild_id, rule_id, channel_id=channel_id,
                phrase=body.phrase, amount=body.amount,
                announcer_role_id=role_id,
            ):
                raise HTTPException(404, "No such rule.")
            return {"ok": True}

    return await run_query(_q)


@router.delete("/mention-awards/rules/{rule_id}")
async def delete_rule(
    rule_id: int,
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            if not store.delete_rule(conn, guild_id, rule_id):
                raise HTTPException(404, "No such rule.")
            return {"ok": True}

    return await run_query(_q)
