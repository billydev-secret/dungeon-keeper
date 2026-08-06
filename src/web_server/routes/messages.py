"""Message search endpoints — search and read back stored messages."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from bot_modules.services.message_search_service import (
    BASE_COLUMNS,
    DELETED_ANY,
    SORT_ORDERS,
    MessageFilters,
    build_where,
    fetch_context_page,
    fetch_context_window,
    hydrate_rows,
    reaction_join,
    reaction_select,
)
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query

log = logging.getLogger("dungeonkeeper.messages")

router = APIRouter()

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

EXPORT_ROW_LIMIT = 5000  # hard cap on rows a single JSON export may contain

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
    deleted: str = Query(
        DELETED_ANY,
        description="any (default), only, live, or a source name (discord, auto_delete)",
    ),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    # Validate regex early so we return 400 before hitting the DB
    compiled_re = compile_search_regex(regex) if regex else None

    filters = MessageFilters(
        author=author,
        mentions=mentions,
        reply_to=reply_to,
        channel=channel,
        before=before,
        after=after,
        sentiment_min=sentiment_min,
        sentiment_max=sentiment_max,
        emotion=emotion,
        has_attachments=has_attachments,
        has_reactions=has_reactions,
        min_length=min_length,
        max_length=max_length,
        include_bots=include_bots,
        sort=sort,
        deleted=deleted,
    )

    def _q():
        with ctx.open_db() as conn:
            guild = ctx.bot.get_guild(guild_id) if ctx.bot else None
            where = build_where(conn, guild_id, filters, guild)
            if where.impossible:
                # A name filter matched nobody — nothing can satisfy the query.
                return {
                    "messages": [],
                    "total": 0,
                    "page": 1,
                    "per_page": per_page,
                    "pages": 1,
                }

            join = reaction_join(sort)
            extra_select = reaction_select(sort)
            order_clause = SORT_ORDERS[sort]
            params = where.params

            # Regex can't be pushed into SQL, so it is matched here — but under
            # a row cap, a match cap and a wall-clock deadline, streaming so
            # only the matches stay resident (B-SEC2 / B-PERF2).
            regex_capped = False
            regex_scan_limited = False
            if compiled_re:
                sql = f"""
                    SELECT {BASE_COLUMNS}{extra_select}
                    FROM messages m
                    {join}
                    WHERE {where.sql}
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
                count_sql = (
                    f"SELECT COUNT(*) FROM messages m {join} WHERE {where.sql}"
                )
                total = conn.execute(count_sql, params).fetchone()[0]

                offset = (page - 1) * per_page
                sql = f"""
                    SELECT {BASE_COLUMNS}{extra_select}
                    FROM messages m
                    {join}
                    WHERE {where.sql}
                    ORDER BY {order_clause}
                    LIMIT ? OFFSET ?
                """
                page_rows = conn.execute(sql, [*params, per_page, offset]).fetchall()

            payload = {
                "messages": hydrate_rows(conn, guild_id, page_rows, guild),
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
    deleted: str = Query(DELETED_ANY),
):
    """Export all matching messages as a downloadable JSON file (capped at 5000 rows)."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    compiled_re = compile_search_regex(regex) if regex else None

    filters = MessageFilters(
        author=author,
        mentions=mentions,
        reply_to=reply_to,
        channel=channel,
        before=before,
        after=after,
        sentiment_min=sentiment_min,
        sentiment_max=sentiment_max,
        emotion=emotion,
        has_attachments=has_attachments,
        has_reactions=has_reactions,
        min_length=min_length,
        max_length=max_length,
        include_bots=include_bots,
        sort=sort,
        deleted=deleted,
    )

    def _q():
        with ctx.open_db() as conn:
            guild = ctx.bot.get_guild(guild_id) if ctx.bot else None
            where = build_where(conn, guild_id, filters, guild)
            if where.impossible:
                return []

            sql = f"""
                SELECT {BASE_COLUMNS}{reaction_select(sort)}
                FROM messages m
                {reaction_join(sort)}
                WHERE {where.sql}
                ORDER BY {SORT_ORDERS[sort]}
                LIMIT {EXPORT_ROW_LIMIT}
            """
            if compiled_re:
                # Same rails as the search path: stream, keep only matches,
                # and abandon the scan if it blows the wall-clock budget.
                rows, _scanned, _capped = scan_regex_rows(
                    conn.execute(sql, where.params),
                    compiled_re,
                    _regex_deadline(),
                    REGEX_MAX_MATCHES,
                )
            else:
                rows = conn.execute(sql, where.params).fetchall()

            return hydrate_rows(conn, guild_id, rows, guild)

    results = await run_query(_q)
    body = json.dumps({"messages": results, "total": len(results)}, indent=2)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="messages.json"'},
    )


CONTEXT_WINDOW = 25  # messages shown on each side of a hit when it expands
CONTEXT_PAGE = 25  # messages added per load-older / load-newer click


@router.get("/messages/context")
async def message_context(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
    message_id: int = Query(..., description="The message to read around"),
    direction: str | None = Query(
        None, description="Omit for the initial window; 'older' or 'newer' to page"
    ),
):
    """Stored messages surrounding one hit, for the panel's inline context view.

    Reads the local archive rather than Discord: that is the point — it shows
    what the bot has, including messages Discord no longer holds.
    """
    if direction is not None and direction not in ("older", "newer"):
        raise HTTPException(status_code=400, detail="direction must be 'older' or 'newer'")

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            guild = ctx.bot.get_guild(guild_id) if ctx.bot else None
            if direction is None:
                window = fetch_context_window(
                    conn,
                    guild_id,
                    message_id,
                    before=CONTEXT_WINDOW,
                    after=CONTEXT_WINDOW,
                )
            else:
                window = fetch_context_page(
                    conn, guild_id, message_id, direction, CONTEXT_PAGE
                )
            if window is None:
                return None
            rows = window.pop("rows")
            return {**window, "messages": hydrate_rows(conn, guild_id, rows, guild)}

    result = await run_query(_q)
    if result is None:
        # The archive has no row for it — from before the bot joined, from a
        # guild at storage level "none", or already hard-erased on request.
        raise HTTPException(status_code=404, detail="That message isn't in the archive.")
    return result
