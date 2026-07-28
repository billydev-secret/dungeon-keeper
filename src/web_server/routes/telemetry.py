"""Panel-view telemetry ingest.

The dashboard pings this once per successful panel mount. Deliberately *not* a
``BaseHTTPMiddleware`` logging every request: several panels poll on timers
(``home.js``, ``mod-tickets.js``, ``mod-jails.js``, ``economy-stats.js``,
``config-ai.js``, ``config-bump-tracker.js``), so a middleware would mostly
record background refreshes from an open tab and the "how often is the dashboard
used" number would measure tab uptime instead of people. One row per panel open
is the thing actually being asked about, and it is ~50-200 rows/day instead of
~5-15k.

The panel id comes from the client, so it is validated against the server's
knowledge of nothing — it is length-capped and stored as-is, then rendered
escaped. It is never used to build SQL.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from bot_modules.services.usage_telemetry_service import KIND_PANEL, record_event
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query

router = APIRouter()

log = logging.getLogger("dungeonkeeper.usage_telemetry")


class PanelViewIn(BaseModel):
    # Panel ids are the dashboard's own kebab-case route ids. Constraining the
    # shape keeps junk out of the `name` column.
    panel: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")


@router.post("/telemetry/panel")
async def record_panel_view(
    request: Request,
    body: PanelViewIn,
    user: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    """Record one dashboard panel open. Moderators and above only.

    Gated on ``moderator`` rather than "any authenticated user" because the
    never-opened list is the whole point of this report, and an unprivileged
    writer can silently invalidate it: any guild member can authenticate (the
    Wellness section is member-accessible), so a member could post a
    well-formed id for a panel they cannot see and make a genuinely
    never-opened panel look used.

    The cost is that members' own Wellness panel views are no longer recorded.
    That is accepted — dashboard usage was always the mod/admin question, and a
    trustworthy deletion list is worth more than member wellness traffic. The
    client skips the ping entirely for non-mods (``app.js``), so this returns
    403 only to a caller bypassing the dashboard.
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _write():
        with ctx.open_db() as conn:
            record_event(
                conn,
                guild_id,
                KIND_PANEL,
                body.panel,
                user.user_id,
            )

    try:
        await run_query(_write)
    except Exception:
        # A telemetry write must never turn into a visible dashboard error.
        log.exception("failed to record panel view %r", body.panel)
        return {"ok": False}
    return {"ok": True}
