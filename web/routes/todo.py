"""Todo endpoints — shared server todo list."""

from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from services.message_store import get_known_users_bulk
from web.auth import AuthenticatedUser
from web.deps import get_active_guild_id, get_ctx, require_perms, run_query

router = APIRouter()

_MOD = Depends(require_perms({"moderator"}))


def _resolve_names(ctx, guild, entries, *id_name_pairs):
    if not entries:
        return
    guild_id = guild.id if guild else 0
    unresolved: set[int] = set()
    for entry in entries:
        for id_field, name_field in id_name_pairs:
            uid = entry.get(id_field)
            if uid:
                if guild:
                    member = guild.get_member(int(uid))
                    if member:
                        entry[name_field] = member.display_name
                        continue
                unresolved.add(int(uid))
    if unresolved:
        with ctx.open_db() as conn:
            known = get_known_users_bulk(conn, guild_id, list(unresolved))
        for entry in entries:
            for id_field, name_field in id_name_pairs:
                if entry.get(name_field):
                    continue
                uid = entry.get(id_field)
                if uid and int(uid) in known:
                    entry[name_field] = known[int(uid)]
    for entry in entries:
        for id_field, name_field in id_name_pairs:
            if entry.get(name_field):
                continue
            uid = entry.get(id_field)
            if uid:
                entry[name_field] = f"User {uid}"


@router.get("/todos")
async def list_todos(
    request: Request,
    status: Optional[str] = None,
    _: AuthenticatedUser = _MOD,
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        where = "guild_id = ?"
        params: list = [guild_id]
        if status == "pending":
            where += " AND completed_at IS NULL"
        elif status == "completed":
            where += " AND completed_at IS NOT NULL"

        with ctx.open_db() as conn:
            rows = conn.execute(
                f"SELECT id, added_by, task, created_at, completed_at, completed_by"
                f" FROM todos WHERE {where} ORDER BY created_at DESC LIMIT 200",
                params,
            ).fetchall()

        todos = [
            {
                "id": r["id"],
                "added_by": str(r["added_by"]),
                "added_by_name": "",
                "task": r["task"],
                "created_at": r["created_at"],
                "completed_at": r["completed_at"],
                "completed_by": str(r["completed_by"]) if r["completed_by"] else None,
                "completed_by_name": "",
            }
            for r in rows
        ]
        pending = sum(1 for t in todos if t["completed_at"] is None)
        return {"pending_count": pending, "completed_count": len(todos) - pending, "todos": todos}

    result = await run_query(_q)

    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    _resolve_names(
        ctx, guild, result["todos"],
        ("added_by", "added_by_name"),
        ("completed_by", "completed_by_name"),
    )
    return result


@router.post("/todos/{todo_id}/complete")
async def complete_todo(
    request: Request,
    todo_id: int,
    user: AuthenticatedUser = _MOD,
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            cur = conn.execute(
                "UPDATE todos SET completed_at = ?, completed_by = ?"
                " WHERE id = ? AND guild_id = ? AND completed_at IS NULL",
                (time.time(), user.user_id, todo_id, guild_id),
            )
            return cur.rowcount

    updated = await run_query(_q)
    if updated == 0:
        raise HTTPException(status_code=404, detail="Todo not found or already completed.")
    return {"ok": True}
