"""Message search endpoints — search and read back stored messages."""

from __future__ import annotations

import json
import logging
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from bot_modules.services.message_store import get_known_channels_bulk, get_known_users_bulk
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query

log = logging.getLogger("dungeonkeeper.messages")

router = APIRouter()

VALID_EMOTIONS = {"joy", "playful", "anger", "frustration", "neutral"}

SORT_OPTIONS = Literal[
    "newest", "oldest", "most_reacted", "longest", "most_positive", "most_negative"
]


@router.get("/messages/search")
async def search_messages(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
    author: list[str] | None = Query(None, description="Filter by author user ID(s)"),
    mentions: str | None = Query(
        None, description="Filter to messages that mention this user ID"
    ),
    reply_to: str | None = Query(
        None, description="Filter to messages that are replies to this user ID"
    ),
    channel: list[str] | None = Query(None, description="Filter by channel ID(s)"),
    regex: str | None = Query(
        None, description="PCRE-style regex to match against message content"
    ),
    before: int | None = Query(
        None, description="Only messages before this unix timestamp"
    ),
    after: int | None = Query(
        None, description="Only messages after this unix timestamp"
    ),
    sentiment_min: float | None = Query(
        None, ge=-1.0, le=1.0, description="Minimum sentiment score"
    ),
    sentiment_max: float | None = Query(
        None, ge=-1.0, le=1.0, description="Maximum sentiment score"
    ),
    emotion: str | None = Query(
        None, description="Comma-separated emotions: joy,playful,anger,frustration,neutral"
    ),
    has_attachments: bool | None = Query(None, description="Filter by attachment presence"),
    has_reactions: bool | None = Query(None, description="Filter by reaction presence"),
    min_length: int | None = Query(None, ge=0, description="Minimum content length"),
    max_length: int | None = Query(None, ge=0, description="Maximum content length"),
    sort: SORT_OPTIONS = "newest",
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

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
        guild = ctx.bot.get_guild(guild_id) if ctx.bot else None
        if guild:
            matches = [
                m.id
                for m in guild.members
                if value.lower() in m.display_name.lower()
                or value.lower() in m.name.lower()
            ]
            if matches:
                return matches
        # Fall back to known_users table
        rows = conn.execute(
            "SELECT user_id FROM known_users WHERE guild_id = ? AND (username LIKE ? OR display_name LIKE ?)",
            (guild_id, f"%{value}%", f"%{value}%"),
        ).fetchall()
        return [r[0] for r in rows] if rows else []

    def _q():
        with ctx.open_db() as conn:
            clauses = ["m.guild_id = ?"]
            params: list[object] = [guild_id]

            if author:
                author_ids: list[int] = []
                for a in author:
                    author_ids.extend(_resolve_user(conn, a))
                # Dedupe while preserving order
                author_ids = list(dict.fromkeys(author_ids))
                if not author_ids:
                    return {
                        "messages": [],
                        "total": 0,
                        "page": 1,
                        "per_page": per_page,
                        "pages": 1,
                    }
                if len(author_ids) == 1:
                    clauses.append("m.author_id = ?")
                    params.append(author_ids[0])
                else:
                    placeholders = ",".join("?" * len(author_ids))
                    clauses.append(f"m.author_id IN ({placeholders})")
                    params.extend(author_ids)
            if channel:
                channel_filter_ids = [int(c) for c in channel]
                if len(channel_filter_ids) == 1:
                    clauses.append("m.channel_id = ?")
                    params.append(channel_filter_ids[0])
                else:
                    placeholders = ",".join("?" * len(channel_filter_ids))
                    clauses.append(f"m.channel_id IN ({placeholders})")
                    params.extend(channel_filter_ids)
            if reply_to:
                reply_to_ids = _resolve_user(conn, reply_to)
                if not reply_to_ids:
                    return {
                        "messages": [],
                        "total": 0,
                        "page": 1,
                        "per_page": per_page,
                        "pages": 1,
                    }
                rt_placeholders = ",".join("?" * len(reply_to_ids))
                clauses.append(f"""
                    m.reply_to_id IN (
                        SELECT message_id FROM messages
                        WHERE author_id IN ({rt_placeholders}) AND guild_id = ?
                    )
                """)
                params.extend([*reply_to_ids, guild_id])
            if mentions:
                mention_ids = _resolve_user(conn, mentions)
                if not mention_ids:
                    return {
                        "messages": [],
                        "total": 0,
                        "page": 1,
                        "per_page": per_page,
                        "pages": 1,
                    }
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
            if sentiment_min is not None:
                clauses.append("m.sentiment >= ?")
                params.append(sentiment_min)
            if sentiment_max is not None:
                clauses.append("m.sentiment <= ?")
                params.append(sentiment_max)
            if emotion:
                emotions = [e.strip() for e in emotion.split(",") if e.strip() in VALID_EMOTIONS]
                if emotions:
                    placeholders = ",".join("?" * len(emotions))
                    clauses.append(f"m.emotion IN ({placeholders})")
                    params.extend(emotions)
            if has_attachments is not None:
                if has_attachments:
                    clauses.append(
                        "EXISTS (SELECT 1 FROM message_attachments a WHERE a.message_id = m.message_id)"
                    )
                else:
                    clauses.append(
                        "NOT EXISTS (SELECT 1 FROM message_attachments a WHERE a.message_id = m.message_id)"
                    )
            if has_reactions is not None:
                if has_reactions:
                    clauses.append(
                        "EXISTS (SELECT 1 FROM message_reactions r WHERE r.message_id = m.message_id)"
                    )
                else:
                    clauses.append(
                        "NOT EXISTS (SELECT 1 FROM message_reactions r WHERE r.message_id = m.message_id)"
                    )
            if min_length is not None:
                clauses.append("LENGTH(m.content) >= ?")
                params.append(min_length)
            if max_length is not None:
                clauses.append("LENGTH(m.content) <= ?")
                params.append(max_length)

            where = " AND ".join(clauses)

            # Build ORDER BY and optional JOIN for sort modes
            needs_reaction_join = sort == "most_reacted"
            reaction_join = ""
            if needs_reaction_join:
                reaction_join = """
                    LEFT JOIN (
                        SELECT message_id, SUM(count) AS total_reactions
                        FROM message_reactions GROUP BY message_id
                    ) mr ON mr.message_id = m.message_id
                """

            order_clause = {
                "newest": "m.ts DESC",
                "oldest": "m.ts ASC",
                "most_reacted": "COALESCE(mr.total_reactions, 0) DESC, m.ts DESC",
                "longest": "LENGTH(m.content) DESC, m.ts DESC",
                "most_positive": "m.sentiment DESC, m.ts DESC",
                "most_negative": "m.sentiment ASC, m.ts DESC",
            }[sort]

            extra_select = ""
            if needs_reaction_join:
                extra_select = ", COALESCE(mr.total_reactions, 0) AS total_reactions"

            # If regex is used, we need to fetch more and filter in Python
            if compiled_re:
                sql = f"""
                    SELECT m.message_id, m.channel_id, m.author_id,
                           m.content, m.reply_to_id, m.ts,
                           m.sentiment, m.emotion{extra_select}
                    FROM messages m
                    {reaction_join}
                    WHERE {where}
                    ORDER BY {order_clause}
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
                count_sql = f"SELECT COUNT(*) FROM messages m {reaction_join} WHERE {where}"
                total = conn.execute(count_sql, params).fetchone()[0]

                offset = (page - 1) * per_page
                sql = f"""
                    SELECT m.message_id, m.channel_id, m.author_id,
                           m.content, m.reply_to_id, m.ts,
                           m.sentiment, m.emotion{extra_select}
                    FROM messages m
                    {reaction_join}
                    WHERE {where}
                    ORDER BY {order_clause}
                    LIMIT ? OFFSET ?
                """
                page_rows = conn.execute(sql, [*params, per_page, offset]).fetchall()

            # Collect IDs for name resolution
            user_ids: set[int] = set()
            channel_ids: set[int] = set()
            reply_msg_ids: list[int] = []

            for r in page_rows:
                user_ids.add(r[2])  # author_id
                channel_ids.add(r[1])  # channel_id
                if r[4]:  # reply_to_id
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
            guild = ctx.bot.get_guild(guild_id) if ctx.bot else None
            if guild:
                for uid in user_ids:
                    member = guild.get_member(uid)
                    if member:
                        user_names[uid] = member.display_name
            still_needed = user_ids - set(user_names.keys())
            if still_needed:
                known = get_known_users_bulk(conn, guild_id, list(still_needed))
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
                known_ch = get_known_channels_bulk(
                    conn, guild_id, list(still_needed_ch)
                )
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
                msg_id, ch_id, auth_id, content, reply_id, ts = r[0], r[1], r[2], r[3], r[4], r[5]
                msg_sentiment = r[6]
                msg_emotion = r[7]
                reply_author_id = reply_authors.get(reply_id) if reply_id else None
                results.append(
                    {
                        "message_id": str(msg_id),
                        "channel_id": str(ch_id),
                        "channel_name": channel_names.get(ch_id) or f"channel {ch_id}",
                        "author_id": str(auth_id),
                        "author_name": user_names.get(auth_id) or f"User {auth_id}",
                        "content": content or "",
                        "reply_to_id": str(reply_id) if reply_id else None,
                        "reply_to_author_id": str(reply_author_id)
                        if reply_author_id
                        else None,
                        "reply_to_author_name": (
                            user_names.get(reply_author_id)
                            or f"User {reply_author_id}"
                        )
                        if reply_author_id
                        else None,
                        "attachments": attachments.get(msg_id, []),
                        "ts": ts,
                        "sentiment": msg_sentiment,
                        "emotion": msg_emotion,
                    }
                )

            return {
                "messages": results,
                "total": total,
                "page": page,
                "per_page": per_page,
                "pages": max(1, -(-total // per_page)),
            }

    return await run_query(_q)


@router.get("/messages/search/export")
async def export_messages(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
    author: list[str] | None = Query(None),
    mentions: str | None = Query(None),
    reply_to: str | None = Query(None),
    channel: list[str] | None = Query(None),
    regex: str | None = Query(None),
    before: int | None = Query(None),
    after: int | None = Query(None),
    sentiment_min: float | None = Query(None, ge=-1.0, le=1.0),
    sentiment_max: float | None = Query(None, ge=-1.0, le=1.0),
    emotion: str | None = Query(None),
    has_attachments: bool | None = Query(None),
    has_reactions: bool | None = Query(None),
    min_length: int | None = Query(None, ge=0),
    max_length: int | None = Query(None, ge=0),
    sort: SORT_OPTIONS = "newest",
):
    """Export all matching messages as a downloadable JSON file (capped at 5000 rows)."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    compiled_re = None
    if regex:
        try:
            compiled_re = re.compile(regex, re.IGNORECASE)
        except re.error as exc:
            raise HTTPException(status_code=400, detail=f"Invalid regex: {exc}")

    def _resolve_user(conn, value: str) -> list[int]:
        try:
            return [int(value)]
        except ValueError:
            pass
        guild = ctx.bot.get_guild(guild_id) if ctx.bot else None
        if guild:
            matches = [
                m.id
                for m in guild.members
                if value.lower() in m.display_name.lower()
                or value.lower() in m.name.lower()
            ]
            if matches:
                return matches
        rows = conn.execute(
            "SELECT user_id FROM known_users WHERE guild_id = ? AND (username LIKE ? OR display_name LIKE ?)",
            (guild_id, f"%{value}%", f"%{value}%"),
        ).fetchall()
        return [r[0] for r in rows] if rows else []

    def _q():
        with ctx.open_db() as conn:
            clauses = ["m.guild_id = ?"]
            params: list[object] = [guild_id]

            if author:
                author_ids: list[int] = []
                for a in author:
                    author_ids.extend(_resolve_user(conn, a))
                author_ids = list(dict.fromkeys(author_ids))
                if not author_ids:
                    return []
                if len(author_ids) == 1:
                    clauses.append("m.author_id = ?")
                    params.append(author_ids[0])
                else:
                    placeholders = ",".join("?" * len(author_ids))
                    clauses.append(f"m.author_id IN ({placeholders})")
                    params.extend(author_ids)
            if channel:
                channel_filter_ids = [int(c) for c in channel]
                if len(channel_filter_ids) == 1:
                    clauses.append("m.channel_id = ?")
                    params.append(channel_filter_ids[0])
                else:
                    placeholders = ",".join("?" * len(channel_filter_ids))
                    clauses.append(f"m.channel_id IN ({placeholders})")
                    params.extend(channel_filter_ids)
            if reply_to:
                reply_to_ids = _resolve_user(conn, reply_to)
                if not reply_to_ids:
                    return []
                rt_placeholders = ",".join("?" * len(reply_to_ids))
                clauses.append(f"""
                    m.reply_to_id IN (
                        SELECT message_id FROM messages
                        WHERE author_id IN ({rt_placeholders}) AND guild_id = ?
                    )
                """)
                params.extend([*reply_to_ids, guild_id])
            if mentions:
                mention_ids = _resolve_user(conn, mentions)
                if not mention_ids:
                    return []
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
            if sentiment_min is not None:
                clauses.append("m.sentiment >= ?")
                params.append(sentiment_min)
            if sentiment_max is not None:
                clauses.append("m.sentiment <= ?")
                params.append(sentiment_max)
            if emotion:
                emotions = [e.strip() for e in emotion.split(",") if e.strip() in VALID_EMOTIONS]
                if emotions:
                    placeholders = ",".join("?" * len(emotions))
                    clauses.append(f"m.emotion IN ({placeholders})")
                    params.extend(emotions)
            if has_attachments is not None:
                if has_attachments:
                    clauses.append(
                        "EXISTS (SELECT 1 FROM message_attachments a WHERE a.message_id = m.message_id)"
                    )
                else:
                    clauses.append(
                        "NOT EXISTS (SELECT 1 FROM message_attachments a WHERE a.message_id = m.message_id)"
                    )
            if has_reactions is not None:
                if has_reactions:
                    clauses.append(
                        "EXISTS (SELECT 1 FROM message_reactions r WHERE r.message_id = m.message_id)"
                    )
                else:
                    clauses.append(
                        "NOT EXISTS (SELECT 1 FROM message_reactions r WHERE r.message_id = m.message_id)"
                    )
            if min_length is not None:
                clauses.append("LENGTH(m.content) >= ?")
                params.append(min_length)
            if max_length is not None:
                clauses.append("LENGTH(m.content) <= ?")
                params.append(max_length)

            where = " AND ".join(clauses)

            needs_reaction_join = sort == "most_reacted"
            reaction_join = ""
            if needs_reaction_join:
                reaction_join = """
                    LEFT JOIN (
                        SELECT message_id, SUM(count) AS total_reactions
                        FROM message_reactions GROUP BY message_id
                    ) mr ON mr.message_id = m.message_id
                """

            order_clause = {
                "newest": "m.ts DESC",
                "oldest": "m.ts ASC",
                "most_reacted": "COALESCE(mr.total_reactions, 0) DESC, m.ts DESC",
                "longest": "LENGTH(m.content) DESC, m.ts DESC",
                "most_positive": "m.sentiment DESC, m.ts DESC",
                "most_negative": "m.sentiment ASC, m.ts DESC",
            }[sort]

            extra_select = ""
            if needs_reaction_join:
                extra_select = ", COALESCE(mr.total_reactions, 0) AS total_reactions"

            sql = f"""
                SELECT m.message_id, m.channel_id, m.author_id,
                       m.content, m.reply_to_id, m.ts,
                       m.sentiment, m.emotion{extra_select}
                FROM messages m
                {reaction_join}
                WHERE {where}
                ORDER BY {order_clause}
                LIMIT 5000
            """
            rows = conn.execute(sql, params).fetchall()

            if compiled_re:
                rows = [r for r in rows if compiled_re.search(r[3] or "")]

            user_ids: set[int] = set()
            channel_ids: set[int] = set()
            reply_msg_ids: list[int] = []

            for r in rows:
                user_ids.add(r[2])
                channel_ids.add(r[1])
                if r[4]:
                    reply_msg_ids.append(r[4])

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

            user_names: dict[int, str] = {}
            guild = ctx.bot.get_guild(guild_id) if ctx.bot else None
            if guild:
                for uid in user_ids:
                    member = guild.get_member(uid)
                    if member:
                        user_names[uid] = member.display_name
            still_needed = user_ids - set(user_names.keys())
            if still_needed:
                known = get_known_users_bulk(conn, guild_id, list(still_needed))
                user_names.update(known)

            channel_names: dict[int, str] = {}
            if guild:
                for cid in channel_ids:
                    ch = guild.get_channel(cid)
                    if ch:
                        channel_names[cid] = ch.name
            still_needed_ch = channel_ids - set(channel_names.keys())
            if still_needed_ch:
                known_ch = get_known_channels_bulk(conn, guild_id, list(still_needed_ch))
                channel_names.update(known_ch)

            msg_ids = [r[0] for r in rows]
            attachments: dict[int, list[str]] = {}
            if msg_ids:
                placeholders = ",".join("?" * len(msg_ids))
                att_rows = conn.execute(
                    f"SELECT message_id, url FROM message_attachments WHERE message_id IN ({placeholders})",
                    msg_ids,
                ).fetchall()
                for ar in att_rows:
                    attachments.setdefault(ar[0], []).append(ar[1])

            results = []
            for r in rows:
                msg_id, ch_id, auth_id, content, reply_id, ts = r[0], r[1], r[2], r[3], r[4], r[5]
                msg_sentiment = r[6]
                msg_emotion = r[7]
                reply_author_id = reply_authors.get(reply_id) if reply_id else None
                results.append(
                    {
                        "message_id": str(msg_id),
                        "channel_id": str(ch_id),
                        "channel_name": channel_names.get(ch_id) or f"channel {ch_id}",
                        "author_id": str(auth_id),
                        "author_name": user_names.get(auth_id) or f"User {auth_id}",
                        "content": content or "",
                        "reply_to_id": str(reply_id) if reply_id else None,
                        "reply_to_author_id": str(reply_author_id) if reply_author_id else None,
                        "reply_to_author_name": (
                            user_names.get(reply_author_id) or f"User {reply_author_id}"
                        ) if reply_author_id else None,
                        "attachments": attachments.get(msg_id, []),
                        "ts": ts,
                        "sentiment": msg_sentiment,
                        "emotion": msg_emotion,
                    }
                )
            return results

    results = await run_query(_q)
    body = json.dumps({"messages": results, "total": len(results)}, indent=2)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="messages.json"'},
    )


