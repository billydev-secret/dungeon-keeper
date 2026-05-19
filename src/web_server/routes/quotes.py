from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query
from web_server.helpers import resolve_names as _resolve_names
from web_server.schemas import QuoteAuditLogResponse

router = APIRouter()


@router.get("/quotes/audit", response_model=QuoteAuditLogResponse)
async def quote_audit_log(
    request: Request,
    limit: int = 50,
    theme: str | None = None,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    limit = min(limit, 200)

    def _q():
        with ctx.open_db() as conn:
            clauses = ["guild_id = ?"]
            params: list = [guild_id]
            if theme:
                clauses.append("theme = ?")
                params.append(theme)
            where = " AND ".join(clauses)
            total = conn.execute(
                f"SELECT COUNT(*) FROM quote_audit_log WHERE {where}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM quote_audit_log WHERE {where} ORDER BY ts DESC LIMIT ?",
                params + [limit],
            ).fetchall()
            entries = [
                {
                    "id": r["id"],
                    "ts": r["ts"],
                    "channel_id": str(r["channel_id"]),
                    "quoter_id": str(r["quoter_id"]),
                    "quoter_name": "",
                    "quoted_user_id": str(r["quoted_user_id"]),
                    "quoted_user_name": "",
                    "quoted_message_id": str(r["quoted_message_id"]),
                    "posted_message_id": str(r["posted_message_id"]),
                    "theme": r["theme"],
                    "font": r["font"],
                }
                for r in rows
            ]
            return {"total": total, "entries": entries}

    result = await run_query(_q)
    await _resolve_names(
        ctx,
        guild,
        result["entries"],
        ("quoter_id", "quoter_name"),
        ("quoted_user_id", "quoted_user_name"),
    )
    return result
