"""Admin backfill endpoints — kick off long-running data jobs.

These run as asyncio tasks on the bot's event loop. The endpoint returns
immediately; results are logged. There is no in-memory job tracker — repeat
runs are idempotent (each job either no-ops on already-processed records or
produces additive events).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from services.backfill_jobs import (
    backfill_interactions_async,
    backfill_roles_sync,
    backfill_xp_async,
)
from web.auth import AuthenticatedUser
from web.deps import get_active_guild_id, get_ctx, require_perms, run_query
from web.schemas import BackfillRequest, BackfillStartedResponse, OkResponse

router = APIRouter()
log = logging.getLogger("dungeonkeeper.web.backfill")


def _get_guild(ctx, guild_id: int):
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is None:
        raise HTTPException(503, "Guild not available — bot may be offline")
    return guild


@router.post("/backfill-roles", response_model=OkResponse)
async def backfill_roles_endpoint(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Sync the role_events log with current server state. Synchronous and
    fast (~few seconds at most for typical guild sizes)."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    guild = _get_guild(ctx, guild_id)

    def _q():
        return backfill_roles_sync(ctx, guild)

    stats = await run_query(_q)
    log.info("backfill-roles done: %s", stats)
    return {
        "ok": True,
        "message": (
            f"Role backfill complete. "
            f"Grants added: {stats['grants_added']}, "
            f"removes added: {stats['removes_added']}."
        ),
    }


def _kick_async_job(coro: Any, label: str) -> None:
    """Schedule *coro* on the running event loop and log the result."""
    async def _runner():
        try:
            stats = await coro
            log.info("%s done: %s", label, stats)
        except Exception:
            log.exception("%s failed", label)
    asyncio.create_task(_runner())


@router.post("/backfill-xp", response_model=BackfillStartedResponse)
async def backfill_xp_endpoint(
    request: Request,
    body: BackfillRequest,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Start an XP-history backfill in the background. Returns immediately.

    Progress is logged. Re-runnable — already-processed messages are skipped.
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    guild = _get_guild(ctx, guild_id)
    days = max(0, min(3650, body.days))

    _kick_async_job(
        backfill_xp_async(ctx, guild, days=days),
        f"backfill-xp(days={days})",
    )

    label = "all available history" if days == 0 else f"last {days} days"
    return {
        "ok": True,
        "job": "backfill-xp",
        "message": f"Started — scanning {label}. Progress in bot logs.",
    }


@router.post("/backfill-interactions", response_model=BackfillStartedResponse)
async def backfill_interactions_endpoint(
    request: Request,
    body: BackfillRequest,
    reset: bool = False,
    channel_id: str | None = None,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Start an interaction-graph backfill in the background.

    *reset=true* wipes existing interaction rows before scanning. *channel_id*
    restricts scanning to a single text channel (and its threads).
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    guild = _get_guild(ctx, guild_id)
    days = max(0, min(3650, body.days))
    ch_id_int = int(channel_id) if channel_id else None

    _kick_async_job(
        backfill_interactions_async(
            ctx, guild, days=days, reset=reset, channel_id=ch_id_int
        ),
        f"backfill-interactions(days={days},reset={reset})",
    )

    label = "all available history" if days == 0 else f"last {days} days"
    scope = f"channel {channel_id}" if channel_id else "all readable channels"
    note = " (existing data cleared)" if reset else ""
    return {
        "ok": True,
        "job": "backfill-interactions",
        "message": f"Started — scanning {scope} over {label}{note}. Progress in bot logs.",
    }
