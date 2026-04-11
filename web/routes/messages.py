"""Message search endpoints — search and read back stored messages."""
from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from services.message_store import get_known_channels_bulk, get_known_users_bulk
from web.auth import AuthenticatedUser
from web.deps import get_ctx, require_perms, run_query

router = APIRouter()


@router.get("/messages/search")
async def search_messages(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
    author: str | None = Query(None, description="Filter by author user ID"),
    mentions: str | None = Query(None, description="Filter to messages that mention this user ID"),
    reply_to: str | None = Query(None, description="Filter to messages that are replies to this user ID"),
    channel: str | None = Query(None, description="Filter by channel ID"),
    regex: str | None = Query(None, description="PCRE-style regex to match against message content"),
    before: int | None = Query(None, description="Only messages before this unix timestamp"),
    after: int | None = Query(None, description="Only messages after this unix timestamp"),
    sort: Literal["newest", "oldest"] = "newest",
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
):
    ctx = get_ctx(request)

    # Validate regex early so we return 400 before hitting the DB
    compiled_re = None
    if regex:
        try:
            compiled_re = re.compile(regex, re.IGNORECASE)
        except re.error as exc:
            raise HTTPException(status_code=400, detail=f"Invalid regex: {exc}")

    def _resolve_user(conn, value: str) -> list[int]:
        """Resolve a username or user ID string to a list of matching user IDs."""
        # If it looks like a numeric ID, use it directly
        try:
            return [int(value)]
        except ValueError:
            pass
        # Try guild cache first
        guild = ctx.bot.get_guild(ctx.guild_id) if ctx.bot else None
        if guild:
            matches = [
                m.id for m in guild.members
                if value.lower() in m.display_name.lower()
                or value.lower() in m.name.lower()
            ]
            if matches:
                return matches
        # Fall back to known_users table
        rows = conn.execute(
            "SELECT user_id FROM known_users WHERE guild_id = ? AND (username LIKE ? OR display_name LIKE ?)",
            (ctx.guild_id, f"%{value}%", f"%{value}%"),
        ).fetchall()
        return [r[0] for r in rows] if rows else []

    def _q():
        with ctx.open_db() as conn:
            clauses = ["m.guild_id = ?"]
            params: list[object] = [ctx.guild_id]

            if author:
                author_ids = _resolve_user(conn, author)
                if not author_ids:
                    return {"messages": [], "total": 0, "page": 1, "per_page": per_page, "pages": 1}
                if len(author_ids) == 1:
                    clauses.append("m.author_id = ?")
                    params.append(author_ids[0])
                else:
                    placeholders = ",".join("?" * len(author_ids))
                    clauses.append(f"m.author_id IN ({placeholders})")
                    params.extend(author_ids)
            if channel:
                clauses.append("m.channel_id = ?")
                params.append(int(channel))
            if reply_to:
                reply_to_ids = _resolve_user(conn, reply_to)
                if not reply_to_ids:
                    return {"messages": [], "total": 0, "page": 1, "per_page": per_page, "pages": 1}
                rt_placeholders = ",".join("?" * len(reply_to_ids))
                clauses.append(f"""
                    m.reply_to_id IN (
                        SELECT message_id FROM messages
                        WHERE author_id IN ({rt_placeholders}) AND guild_id = ?
                    )
                """)
                params.extend([*reply_to_ids, ctx.guild_id])
            if mentions:
                mention_ids = _resolve_user(conn, mentions)
                if not mention_ids:
                    return {"messages": [], "total": 0, "page": 1, "per_page": per_page, "pages": 1}
                mn_placeholders = ",".join("?" * len(mention_ids))
                clauses.append(f"""
                    m.message_id IN (
                        SELECT message_id FROM message_mentions WHERE user_id IN ({mn_placeholders})
                    )
                """)
                params.extend(mention_ids)
            if before:
                clauses.append("m.ts <= ?")
                params.append(before)
            if after:
                clauses.append("m.ts >= ?")
                params.append(after)

            where = " AND ".join(clauses)
            order = "DESC" if sort == "newest" else "ASC"

            # If regex is used, we need to fetch more and filter in Python
            if compiled_re:
                # Fetch a larger window and filter in Python
                sql = f"""
                    SELECT m.message_id, m.channel_id, m.author_id,
                           m.content, m.reply_to_id, m.ts
                    FROM messages m
                    WHERE {where}
                    ORDER BY m.ts {order}
                """
                rows = conn.execute(sql, params).fetchall()

                matched = []
                for r in rows:
                    content = r[3] or ""
                    if compiled_re.search(content):
                        matched.append(r)

                total = len(matched)
                offset = (page - 1) * per_page
                page_rows = matched[offset : offset + per_page]
            else:
                # Count total
                count_sql = f"SELECT COUNT(*) FROM messages m WHERE {where}"
                total = conn.execute(count_sql, params).fetchone()[0]

                offset = (page - 1) * per_page
                sql = f"""
                    SELECT m.message_id, m.channel_id, m.author_id,
                           m.content, m.reply_to_id, m.ts
                    FROM messages m
                    WHERE {where}
                    ORDER BY m.ts {order}
                    LIMIT ? OFFSET ?
                """
                page_rows = conn.execute(sql, [*params, per_page, offset]).fetchall()

            # Collect IDs for name resolution
            user_ids: set[int] = set()
            channel_ids: set[int] = set()
            reply_msg_ids: list[int] = []

            for r in page_rows:
                user_ids.add(r[2])       # author_id
                channel_ids.add(r[1])    # channel_id
                if r[4]:                 # reply_to_id
                    reply_msg_ids.append(r[4])

            # Resolve reply targets to author IDs
            reply_authors: dict[int, int] = {}
            if reply_msg_ids:
                placeholders = ",".join("?" * len(reply_msg_ids))
                reply_rows = conn.execute(
                    f"SELECT message_id, author_id FROM messages WHERE message_id IN ({placeholders})",
                    reply_msg_ids,
                ).fetchall()
                for rr in reply_rows:
                    reply_authors[rr[0]] = rr[1]
                    user_ids.add(rr[1])

            # Resolve user names
            user_names: dict[int, str] = {}
            guild = ctx.bot.get_guild(ctx.guild_id) if ctx.bot else None
            if guild:
                for uid in user_ids:
                    member = guild.get_member(uid)
                    if member:
                        user_names[uid] = member.display_name
            still_needed = user_ids - set(user_names.keys())
            if still_needed:
                known = get_known_users_bulk(conn, ctx.guild_id, list(still_needed))
                user_names.update(known)

            # Resolve channel names
            channel_names: dict[int, str] = {}
            if guild:
                for cid in channel_ids:
                    ch = guild.get_channel(cid)
                    if ch:
                        channel_names[cid] = ch.name
            still_needed_ch = channel_ids - set(channel_names.keys())
            if still_needed_ch:
                known_ch = get_known_channels_bulk(conn, ctx.guild_id, list(still_needed_ch))
                channel_names.update(known_ch)

            # Resolve attachment URLs for these messages
            msg_ids = [r[0] for r in page_rows]
            attachments: dict[int, list[str]] = {}
            if msg_ids:
                placeholders = ",".join("?" * len(msg_ids))
                att_rows = conn.execute(
                    f"SELECT message_id, url FROM message_attachments WHERE message_id IN ({placeholders})",
                    msg_ids,
                ).fetchall()
                for ar in att_rows:
                    attachments.setdefault(ar[0], []).append(ar[1])

            # Build results
            results = []
            for r in page_rows:
                msg_id, ch_id, auth_id, content, reply_id, ts = r
                reply_author_id = reply_authors.get(reply_id) if reply_id else None
                results.append({
                    "message_id": str(msg_id),
                    "channel_id": str(ch_id),
                    "channel_name": channel_names.get(ch_id, ""),
                    "author_id": str(auth_id),
                    "author_name": user_names.get(auth_id, ""),
                    "content": content or "",
                    "reply_to_id": str(reply_id) if reply_id else None,
                    "reply_to_author_id": str(reply_author_id) if reply_author_id else None,
                    "reply_to_author_name": user_names.get(reply_author_id, "") if reply_author_id else None,
                    "attachments": attachments.get(msg_id, []),
                    "ts": ts,
                })

            return {
                "messages": results,
                "total": total,
                "page": page,
                "per_page": per_page,
                "pages": max(1, -(-total // per_page)),
            }

    return await run_query(_q)
