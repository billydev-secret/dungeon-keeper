"""Message search endpoints — search and read back stored messages."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from services.message_store import get_known_channels_bulk, get_known_users_bulk
from web.auth import AuthenticatedUser
from web.deps import get_active_guild_id, get_ctx, require_perms, run_query

log = logging.getLogger("dungeonkeeper.messages")

router = APIRouter()

VALID_EMOTIONS = {"joy", "playful", "anger", "frustration", "neutral"}

SORT_OPTIONS = Literal[
    "newest", "oldest", "most_reacted", "longest", "most_positive", "most_negative"
]


@router.get("/messages/search")
async def search_messages(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
    author: str | None = Query(None, description="Filter by author user ID"),
    mentions: str | None = Query(
        None, description="Filter to messages that mention this user ID"
    ),
    reply_to: str | None = Query(
        None, description="Filter to messages that are replies to this user ID"
    ),
    channel: str | None = Query(None, description="Filter by channel ID"),
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
                author_ids = _resolve_user(conn, author)
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
                clauses.append("m.channel_id = ?")
                params.append(int(channel))
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
                        "channel_name": channel_names.get(ch_id, ""),
                        "author_id": str(auth_id),
                        "author_name": user_names.get(auth_id, ""),
                        "content": content or "",
                        "reply_to_id": str(reply_id) if reply_id else None,
                        "reply_to_author_id": str(reply_author_id)
                        if reply_author_id
                        else None,
                        "reply_to_author_name": user_names.get(reply_author_id, "")
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


# ---------------------------------------------------------------------------
# AI query translation
# ---------------------------------------------------------------------------

AVAILABLE_MODELS = [
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-5-20250514",
    "claude-opus-4-6",
]

AI_QUERY_SYSTEM_PROMPT = """\
You are a search filter translator for a Discord message archive. Convert the user's \
natural language query into a JSON object of search filters.

Available filters:
- "author": user ID (as a string) or username substring
- "channel": channel ID (as a string)
- "mentions": user ID or username of a mentioned user
- "reply_to": user ID or username being replied to
- "regex": PCRE pattern to match message content (case-insensitive)
- "before": unix timestamp (integer)
- "after": unix timestamp (integer)
- "sentiment_min": float from -1.0 to 1.0
- "sentiment_max": float from -1.0 to 1.0
- "emotion": comma-separated from: joy, playful, anger, frustration, neutral
- "has_attachments": boolean (true/false)
- "has_reactions": boolean (true/false)
- "min_length": integer (minimum characters)
- "max_length": integer (maximum characters)
- "sort": one of: newest, oldest, most_reacted, longest, most_positive, most_negative

Known users in this server:
{user_list}

Known channels in this server:
{channel_list}

Current UTC time: {now_iso}
Current unix timestamp: {now_ts}

Rules:
1. Output ONLY valid JSON. No markdown fences, no explanation outside the JSON.
2. Only include filters that the query implies. Omit filters that aren't mentioned.
3. When the user mentions a person by name, resolve to their user ID from the known users list. Use the "author" field.
4. When the user mentions a channel by name, resolve to its channel ID.
5. For time references like "today", "this week", "last 24 hours", calculate the appropriate "after" and/or "before" unix timestamps.
6. For "most popular" or "most reacted", use sort: "most_reacted".
7. For "questions", use regex: "\\\\?" to find messages containing question marks.
8. For "long messages" or "essays", use min_length: 200.
9. For "happy" or "positive" messages, use sentiment_min or emotion filters as appropriate.
10. For "angry" or "negative" messages, use sentiment_max or emotion filters as appropriate.
11. Include an "_explanation" key with a one-sentence description of your interpretation.

Example input: "show me all questions from Alice in the general channel today"
Example output: {{"author": "123456", "channel": "789012", "regex": "\\\\?", "after": 1713052800, "sort": "newest", "_explanation": "Questions (containing ?) from Alice in #general since start of today"}}
"""


class AiQueryRequest(BaseModel):
    query: str
    model: str | None = None


@router.get("/messages/ai-models")
async def list_ai_models(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Return available AI model IDs for the query feature."""
    ctx = get_ctx(request)

    # Include the configured moderation model if it's not already in the list
    def _get_mod_model():
        from services.ai_config import get_mod_model

        with ctx.open_db() as conn:
            return get_mod_model(conn)

    mod_model = await run_query(_get_mod_model)
    models = list(AVAILABLE_MODELS)
    if mod_model and mod_model not in models:
        models.insert(0, mod_model)
    return {"models": models}


@router.post("/messages/ai-query")
async def ai_query(
    request: Request,
    body: AiQueryRequest,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Translate a natural language query into structured search filters using AI."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY is not set.")

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    # Determine model
    model = body.model
    if not model:

        def _get_model():
            from services.ai_config import get_mod_model

            with ctx.open_db() as conn:
                return get_mod_model(conn)

        model = await run_query(_get_model)
    if not model:
        model = "claude-haiku-4-5-20251001"

    # Build context about known users and channels
    def _build_context():
        with ctx.open_db() as conn:
            users = conn.execute(
                "SELECT user_id, username, display_name FROM known_users "
                "WHERE guild_id = ? ORDER BY rowid DESC LIMIT 200",
                (guild_id,),
            ).fetchall()
            channels = conn.execute(
                "SELECT channel_id, channel_name FROM known_channels WHERE guild_id = ?",
                (guild_id,),
            ).fetchall()
            return users, channels

    users, channels = await run_query(_build_context)

    # Also pull live guild members if available
    guild = ctx.bot.get_guild(guild_id) if ctx.bot else None
    if guild:
        user_list_parts = []
        for m in list(guild.members)[:200]:
            label = f"{m.display_name} ({m.name})" if m.display_name != m.name else m.name
            user_list_parts.append(f"{label} (id: {m.id})")
        user_list = ", ".join(user_list_parts) if user_list_parts else "(none)"

        channel_list_parts = []
        for ch in guild.text_channels:
            channel_list_parts.append(f"#{ch.name} (id: {ch.id})")
        channel_list = ", ".join(channel_list_parts) if channel_list_parts else "(none)"
    else:
        user_list = ", ".join(
            f"{r[2] or r[1]} (id: {r[0]})" for r in users
        ) if users else "(none)"
        channel_list = ", ".join(
            f"#{r[1]} (id: {r[0]})" for r in channels
        ) if channels else "(none)"

    now = datetime.now(timezone.utc)
    system = AI_QUERY_SYSTEM_PROMPT.format(
        user_list=user_list,
        channel_list=channel_list,
        now_iso=now.isoformat(),
        now_ts=int(now.timestamp()),
    )

    from anthropic import AsyncAnthropic
    from anthropic.types import TextBlock

    client = AsyncAnthropic(api_key=api_key)
    try:
        async with client.messages.stream(
            model=model,
            system=system,
            messages=[{"role": "user", "content": body.query}],
            max_tokens=1024,
        ) as stream:
            message = await stream.get_final_message()

        parts: list[str] = []
        for block in message.content:
            if isinstance(block, TextBlock):
                parts.append(block.text)
        raw = "".join(parts).strip()

        # Parse JSON — strip markdown fences if present
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)

        filters = json.loads(raw)
        explanation = filters.pop("_explanation", "")

        return {"filters": filters, "explanation": explanation}
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=502, detail="AI returned invalid JSON. Please try rephrasing your query."
        )
    except Exception as exc:
        log.exception("AI query failed")
        raise HTTPException(status_code=502, detail=f"AI query failed: {exc}")
