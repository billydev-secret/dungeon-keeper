"""Message search endpoints — search and read back stored messages."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from bot_modules.core.bot_exclusion import bot_ids_subquery
from bot_modules.services.message_store import get_known_channels_bulk, get_known_users_bulk
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query

log = logging.getLogger("dungeonkeeper.messages")

router = APIRouter()

VALID_EMOTIONS = {"joy", "playful", "anger", "frustration", "neutral"}

SORT_OPTIONS = Literal[
    "newest", "oldest", "most_reacted", "longest", "most_positive", "most_negative"
]


# ── Regex-search safety rails ────────────────────────────────────────────
#
# A moderator-supplied pattern is matched in Python against rows this process
# reads out of SQLite. Two facts make that dangerous rather than merely slow:
# the dashboard runs inside the bot's own process, and CPython's ``re`` engine
# holds the GIL for the whole of a single match — so one catastrophic-
# backtracking pattern stalls the Discord gateway, not just the request. The
# old code compounded it by running the pattern over a **no-LIMIT** query, so
# every message in the guild was pulled into memory with its content first.
#
# The stdlib has no per-match timeout (the third-party ``regex`` module's
# ``timeout=`` would give one, but it is not a dependency here and adding one
# is not this change's call). The budget is therefore enforced on four axes:
# what the pattern may contain, how much text a single match sees, how many
# rows the scan may read, and a wall-clock deadline across the whole scan.

REGEX_MAX_PATTERN_LEN = 300
REGEX_MAX_QUANTIFIERS = 12  # unbounded quantifiers in one pattern
REGEX_MAX_REPEAT = 200  # largest {n,m} bound accepted
REGEX_MAX_CONTENT = 4096  # Discord's own message ceiling
REGEX_SCAN_LIMIT = 50_000  # hard LIMIT on rows a regex search may read
REGEX_MAX_MATCHES = 5_000  # matches retained in memory for pagination
REGEX_CHUNK = 500  # fetchmany() batch size
REGEX_TIME_BUDGET = 5.0  # seconds of wall clock for one scan

_TOO_EXPENSIVE = (
    "That search is too expensive to finish — narrow your filters (channel,"
    " author, or a date range) and try again."
)

# ``(?:``, ``(?=``, ``(?!``, ``(?<=``, ``(?P<name>``, ``(?i)`` … — group syntax,
# not quantifiers. Blanked before the quantifier scan so ``(?:ab)+`` doesn't
# look like a quantified group whose body contains ``?``.
_GROUP_PREFIX = re.compile(r"\(\?(?:P?<[^>]{0,32}>|[aimsxLu]*[:=!>]|<[=!]|[aimsxLu]+\))")
_UNBOUNDED_IN_BODY = re.compile(r"[*+?]|\{\d*,\}")


_META = set("*+?{}()|[]^$.\\")


def _blank_regex_literals(pattern: str) -> str:
    """Neutralize escapes and character-class bodies, preserving length.

    Lets the structural checks below reason about grouping without tripping
    over ``\\+`` (a literal plus) or ``[+*]`` (a class of them). Non-meta
    characters survive so that ``[a-z]`` and ``[0-9]`` still look different
    from each other — the alternation check needs that.
    """
    out: list[str] = []
    i = 0
    in_class = False
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            nxt = pattern[i + 1]
            out.append("xx" if nxt in _META else "x" + nxt)
            i += 2
            continue
        if in_class:
            if ch == "]":
                in_class = False
                out.append("]")
            else:
                out.append("x" if ch in _META else ch)
            i += 1
            continue
        if ch == "[":
            in_class = True
            out.append("[")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _split_alternatives(body: str) -> list[str]:
    """Split a group body on its *top-level* ``|`` only."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "|" and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    parts.append("".join(current))
    return parts


def _has_ambiguous_alternation(body: str) -> bool:
    """True when a repeated group's branches can match the same text.

    ``(a|a)+`` and ``(a|ab)*`` backtrack exponentially for the same reason
    ``(a+)+`` does: more than one way to split the input across repetitions.
    Distinct, non-prefixing branches (``(cat|dog)+``) are unambiguous and fine.
    """
    parts = [p for p in _split_alternatives(body) if p]
    if len(parts) < 2:
        return False
    for i, a in enumerate(parts):
        for b in parts[i + 1 :]:
            if a.startswith(b) or b.startswith(a):
                return True
    return False


def _has_nested_quantifier(normalized: str) -> bool:
    """True when a quantified group itself contains an unbounded quantifier.

    ``(a+)+``, ``(a*)*``, ``(x?)+`` and friends — the classic exponential-
    backtracking shapes. A single one of these against a 100-character string
    can run for longer than the heat death of the request.
    """
    stack: list[int] = []
    for i, ch in enumerate(normalized):
        if ch == "(":
            stack.append(i)
        elif ch == ")" and stack:
            start = stack.pop()
            if normalized[i + 1 : i + 2] in ("*", "+", "{"):
                body = normalized[start + 1 : i]
                if _UNBOUNDED_IN_BODY.search(body) or _has_ambiguous_alternation(body):
                    return True
    return False


def compile_search_regex(pattern: str) -> re.Pattern[str]:
    """Compile a caller-supplied pattern, refusing the pathological ones.

    Raises ``HTTPException(400)`` — an unusable pattern is a bad request, and
    the moderator gets told which rail they hit.
    """
    if len(pattern) > REGEX_MAX_PATTERN_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Regex is too long (limit {REGEX_MAX_PATTERN_LEN} characters).",
        )

    normalized = _GROUP_PREFIX.sub(lambda m: "(" + "x" * (len(m.group(0)) - 1), _blank_regex_literals(pattern))

    if _has_nested_quantifier(normalized):
        raise HTTPException(
            status_code=400,
            detail=(
                "That pattern nests a repeat inside a repeated group (like"
                " “(a+)+”), which can take effectively forever to match."
                " Rewrite it without the nested repeat."
            ),
        )
    if len(re.findall(r"[*+]", normalized)) > REGEX_MAX_QUANTIFIERS:
        raise HTTPException(
            status_code=400,
            detail=f"Regex uses too many repeats (limit {REGEX_MAX_QUANTIFIERS}).",
        )
    for bound in re.findall(r"\{(\d+)(?:,(\d*))?\}", normalized):
        if any(int(n) > REGEX_MAX_REPEAT for n in bound if n):
            raise HTTPException(
                status_code=400,
                detail=f"Regex repeat counts are capped at {REGEX_MAX_REPEAT}.",
            )

    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise HTTPException(status_code=400, detail=f"Invalid regex: {exc}")


def _regex_deadline() -> float:
    return time.monotonic() + REGEX_TIME_BUDGET


def _check_regex_deadline(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise HTTPException(status_code=400, detail=_TOO_EXPENSIVE)


def scan_regex_rows(
    cursor, compiled: re.Pattern[str], deadline: float, max_matches: int
) -> tuple[list, int, bool]:
    """Stream a cursor, keeping only rows whose content matches.

    Streaming with ``fetchmany`` is the point: the previous ``fetchall()``
    materialized every row *with its content* (~450k messages, hundreds of MB)
    before deciding which ones mattered.

    Returns ``(matches, rows_scanned, capped)``. ``capped`` means the match
    cap stopped the scan early, so the result set is a prefix, not the whole
    answer. Blowing the wall-clock budget raises 400 instead — a search that
    can't finish should say so, not return a silently partial answer.
    """
    matched: list = []
    scanned = 0
    while True:
        chunk = cursor.fetchmany(REGEX_CHUNK)
        if not chunk:
            return matched, scanned, False
        for row in chunk:
            scanned += 1
            _check_regex_deadline(deadline)
            if compiled.search((row[3] or "")[:REGEX_MAX_CONTENT]):
                matched.append(row)
                if len(matched) >= max_matches:
                    return matched, scanned, True


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
    include_bots: bool = Query(False),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    # Validate regex early so we return 400 before hitting the DB
    compiled_re = compile_search_regex(regex) if regex else None

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

            # Bots are excluded from the browser by default, matching every
            # other message-volume surface. An explicit ``author`` filter is an
            # override: searching *for* a bot must still return its messages.
            if not include_bots and not author:
                clauses.append(f"m.author_id NOT IN ({bot_ids_subquery()})")
                params.append(guild_id)

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

            # Regex can't be pushed into SQL, so it is matched here — but under
            # a row cap, a match cap and a wall-clock deadline, streaming so
            # only the matches stay resident (B-SEC2 / B-PERF2).
            regex_capped = False
            regex_scan_limited = False
            if compiled_re:
                sql = f"""
                    SELECT m.message_id, m.channel_id, m.author_id,
                           m.content, m.reply_to_id, m.ts,
                           m.sentiment, m.emotion{extra_select}
                    FROM messages m
                    {reaction_join}
                    WHERE {where}
                    ORDER BY {order_clause}
                    LIMIT ?
                """
                cursor = conn.execute(sql, [*params, REGEX_SCAN_LIMIT])
                matched, scanned, regex_capped = scan_regex_rows(
                    cursor, compiled_re, _regex_deadline(), REGEX_MAX_MATCHES
                )
                regex_scan_limited = scanned >= REGEX_SCAN_LIMIT

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

            payload = {
                "messages": results,
                "total": total,
                "page": page,
                "per_page": per_page,
                "pages": max(1, -(-total // per_page)),
            }
            # Only present when the regex scan stopped at a rail rather than at
            # the end of the data — a signal that "total" is a floor. Added
            # rather than always-present so the ordinary response shape (which
            # several callers compare wholesale) is unchanged.
            if regex_capped or regex_scan_limited:
                payload["truncated"] = True
            return payload

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
    include_bots: bool = Query(False),
):
    """Export all matching messages as a downloadable JSON file (capped at 5000 rows)."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    compiled_re = compile_search_regex(regex) if regex else None

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

            # Bots are excluded from the browser by default, matching every
            # other message-volume surface. An explicit ``author`` filter is an
            # override: searching *for* a bot must still return its messages.
            if not include_bots and not author:
                clauses.append(f"m.author_id NOT IN ({bot_ids_subquery()})")
                params.append(guild_id)

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
            if compiled_re:
                # Same rails as the search path: stream, keep only matches,
                # and abandon the scan if it blows the wall-clock budget.
                rows, _scanned, _capped = scan_regex_rows(
                    conn.execute(sql, params),
                    compiled_re,
                    _regex_deadline(),
                    REGEX_MAX_MATCHES,
                )
            else:
                rows = conn.execute(sql, params).fetchall()

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


