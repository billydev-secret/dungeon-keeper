"""Rules Watch API — alert queue, event detail, label capture, stats."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from bot_modules.rules_watch import ledger, service
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class LabelBody(BaseModel):
    is_violation: bool
    corrected_rule: str | None = None
    notes: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row) -> dict:
    if row is None:
        return {}
    return dict(row)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/rules-watch/events")
async def list_events(
    request: Request,
    tier: str | None = Query(None, description="Filter by tier: immediate, digest, logged"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    pending_only: bool = Query(True, description="Only return unlabeled events"),
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            if pending_only:
                rows = service.get_pending_events(
                    conn, guild_id, tier=tier, limit=limit, offset=offset
                )
            else:
                rows = service.get_all_events(
                    conn, guild_id, tier=tier, limit=limit, offset=offset
                )
            return [_row_to_dict(r) for r in rows]

    return await run_query(_q)


@router.get("/rules-watch/events/{event_id}")
async def get_event(
    event_id: int,
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            row = service.get_event(conn, event_id)
            if row is None:
                return None
            return _row_to_dict(row)

    result = await run_query(_q)
    if result is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return result


@router.post("/rules-watch/events/{event_id}/label")
async def label_event(
    event_id: int,
    body: LabelBody,
    request: Request,
    user: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            ev = service.get_event(conn, event_id)
            if ev is None:
                return False
            service.upsert_label(
                conn,
                event_id,
                is_violation=body.is_violation,
                corrected_rule=body.corrected_rule,
                labeled_by=user.user_id if hasattr(user, "user_id") else None,
                notes=body.notes,
            )
            return True

    ok = await run_query(_q)
    if not ok:
        raise HTTPException(status_code=404, detail="Event not found")
    return {"ok": True}


@router.get("/rules-watch/stats")
async def get_stats(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            return service.get_stats(conn, guild_id)

    return await run_query(_q)


# ---------------------------------------------------------------------------
# Ledger — concrete acts, not verdicts. Read-only; there is nothing to label
# because a ledger row makes no claim to agree or disagree with.
# ---------------------------------------------------------------------------

@router.get("/rules-watch/ledger")
async def list_ledger(
    request: Request,
    kind: str | None = Query(None, description="dm_consent | cross_platform"),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            rows = ledger.get_ledger(
                conn, guild_id, kind=kind, limit=limit, offset=offset
            )
            out = []
            for r in rows:
                d = _row_to_dict(r)
                # Snowflakes must cross as strings or JS rounds them.
                for key in ("message_id", "channel_id", "author_id", "target_id"):
                    if d.get(key) is not None:
                        d[key] = str(d[key])
                out.append(d)
            return out

    return await run_query(_q)


@router.get("/rules-watch/ledger/repeats")
async def list_ledger_repeats(
    request: Request,
    kind: str = Query("cross_platform"),
    min_targets: int = Query(2, ge=2, le=10),
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    """Authors hitting the same ledger against several distinct people."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            rows = ledger.get_repeat_authors(
                conn, guild_id, kind=kind, min_targets=min_targets
            )
            out = []
            for r in rows:
                d = _row_to_dict(r)
                d["author_id"] = str(d["author_id"])
                out.append(d)
            return out

    return await run_query(_q)
