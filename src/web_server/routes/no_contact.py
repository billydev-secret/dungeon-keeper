"""No-contact list dashboard API — the moderator management surface.

Two things live here that deliberately do NOT live in Discord:

* **Server config** (which channel alerts go to, which role is pinged) —
  ordinary admin configuration, which belongs on the web.
* **Acting on someone else's behalf** — adding a separation after a report
  from a third party, or for a member who will not file it themselves, plus
  the only view of the whole list and the event log.

What is NOT here is a member protecting themselves; that is self-service and
lives on ``/nocontact`` in Discord, two taps from the message that upset
them. See ``bot_modules/cogs/no_contact_cog.py``.

Every response stringifies snowflakes — a Discord id exceeds what a JS
``Number`` can hold exactly.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from bot_modules.services import no_contact_service
from bot_modules.services.no_contact_logic import (
    is_self_pair,
    resolve_protected_user,
    surface_label,
)
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query

router = APIRouter()
_MOD = Depends(require_perms({"moderator"}))


class PairBody(BaseModel):
    user_a: int
    user_b: int
    # Which of the two the entry protects, and therefore the only member who
    # may lift it. Omitted / null means a mutual separation that neither can
    # lift alone — the right shape for a mod-imposed cooling-off.
    protected_user_id: Optional[int] = None
    reason: str = Field("", max_length=500)


class SettingsBody(BaseModel):
    alert_channel_id: int = 0
    alert_role_id: int = 0


def _pair_json(row: dict[str, Any]) -> dict[str, Any]:
    protected = row["protected_user_id"]
    return {
        "user_low": str(row["user_low"]),
        "user_high": str(row["user_high"]),
        "protected_user_id": str(protected) if protected is not None else None,
        "created_by": str(row["created_by"]),
        "reason": row["reason"],
        "created_at": row["created_at"],
    }


def _event_json(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "actor_id": str(row["actor_id"]),
        "target_id": str(row["target_id"]),
        "kind": row["kind"],
        "surface": row["surface"],
        "surface_label": surface_label(row["surface"]) if row["surface"] else "",
        "channel_id": str(row["channel_id"]) if row["channel_id"] else None,
        "message_id": str(row["message_id"]) if row["message_id"] else None,
        "created_at": row["created_at"],
    }


@router.get("/no-contact/overview")
async def overview(
    _user=_MOD,
    guild_id: int = Depends(get_active_guild_id),
    ctx=Depends(get_ctx),
) -> dict[str, Any]:
    def _q():
        return (
            no_contact_service.list_pairs(ctx.db_path, guild_id),
            no_contact_service.get_settings(ctx.db_path, guild_id),
            no_contact_service.list_events(ctx.db_path, guild_id, limit=100),
        )

    pairs, settings, events = await run_query(_q)
    return {
        "pairs": [_pair_json(p) for p in pairs],
        "settings": {
            "alert_channel_id": str(settings["alert_channel_id"]),
            "alert_role_id": str(settings["alert_role_id"]),
        },
        "events": [_event_json(e) for e in events],
    }


@router.post("/no-contact/pairs")
async def add_pair(
    body: PairBody,
    _user=_MOD,
    guild_id: int = Depends(get_active_guild_id),
    ctx=Depends(get_ctx),
) -> dict[str, Any]:
    if is_self_pair(body.user_a, body.user_b):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A no-contact entry needs two different members.",
        )
    # A protected member outside the pair would grant removal rights to
    # someone who isn't in it, so anything unexpected collapses to mutual.
    protected = resolve_protected_user(
        user_a=body.user_a, user_b=body.user_b, protect=body.protected_user_id
    )

    def _q():
        return no_contact_service.add_pair(
            ctx.db_path,
            guild_id,
            body.user_a,
            body.user_b,
            created_by=int(getattr(_user, "id", 0) or 0),
            protected_user_id=protected,
            reason=body.reason.strip(),
        )

    await run_query(_q)
    return {"ok": True}


@router.delete("/no-contact/pairs/{user_a}/{user_b}")
async def delete_pair(
    user_a: int,
    user_b: int,
    _user=_MOD,
    guild_id: int = Depends(get_active_guild_id),
    ctx=Depends(get_ctx),
) -> dict[str, Any]:
    """Remove an entry. Moderators may remove any entry, including one a
    member set for themselves — staff need to fix mistakes and handle pairs
    where the protected member has since left."""

    def _q():
        return no_contact_service.remove_pair(ctx.db_path, guild_id, user_a, user_b)

    removed = await run_query(_q)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such entry."
        )
    return {"ok": True}


@router.post("/no-contact/settings")
async def save_settings(
    body: SettingsBody,
    _user=_MOD,
    guild_id: int = Depends(get_active_guild_id),
    ctx=Depends(get_ctx),
) -> dict[str, Any]:
    def _q():
        no_contact_service.set_settings(
            ctx.db_path,
            guild_id,
            alert_channel_id=body.alert_channel_id,
            alert_role_id=body.alert_role_id,
        )

    await run_query(_q)
    return {"ok": True}
