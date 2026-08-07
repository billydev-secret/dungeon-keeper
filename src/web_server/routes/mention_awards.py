"""Mention Awards API — CRUD over condition-chip award rules.

A rule is a channel, an amount, and a list of conditions ("chips") that must
all match. The listener (``mention_awards_cog``) reads them; this is the only
way they're written.

Snowflakes go out as strings — a channel, role, or user id past 2^53 loses
precision as a bare JSON number (see the dashboard's snowflake-precision
sweep). Ids inside condition values are already strings in storage for the
same reason.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from bot_modules.mention_awards import store
from bot_modules.mention_awards.logic import Condition
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query

router = APIRouter()


class ConditionBody(BaseModel):
    """One chip. ``value`` is text for contains_text, an id-string otherwise."""

    kind: str
    value: str = Field(max_length=store.MAX_TEXT_LEN)
    regex: bool = False


class RuleBody(BaseModel):
    channel_id: str
    amount: int = Field(ge=0, le=store.MAX_AMOUNT)
    conditions: list[ConditionBody] = Field(max_length=store.MAX_CONDITIONS)


def _as_id(raw: str, field: str) -> int:
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{field} must be a numeric id")


def _row(r) -> dict:
    return {
        "id": int(r["id"]),
        "channel_id": str(r["channel_id"]),
        "amount": int(r["amount"]),
        "conditions": [
            {"kind": c.kind, "value": c.value, "regex": c.regex}
            for c in store.conditions_from_json(r["conditions"])
        ],
        "created_by": str(r["created_by"]) if r["created_by"] else None,
        "created_at": r["created_at"],
    }


def _validated(body: RuleBody) -> tuple[int, list[Condition]]:
    """(channel_id, conditions) after the shared validation gate."""
    conditions = [
        Condition(kind=c.kind, value=c.value.strip(), regex=c.regex)
        for c in body.conditions
    ]
    problem = store.validate(body.amount, conditions)
    if problem:
        raise HTTPException(400, problem)
    channel_id = _as_id(body.channel_id, "channel_id")
    if not channel_id:
        raise HTTPException(400, "Pick a channel for the rule to watch.")
    return channel_id, conditions


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
    channel_id, conditions = _validated(body)
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            rule_id = store.create_rule(
                conn, guild_id, channel_id=channel_id, amount=body.amount,
                conditions=conditions, created_by=int(user.user_id),
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
    channel_id, conditions = _validated(body)
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            if not store.update_rule(
                conn, guild_id, rule_id, channel_id=channel_id,
                amount=body.amount, conditions=conditions,
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
