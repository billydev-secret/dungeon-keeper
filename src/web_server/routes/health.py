"""Community health dashboard API endpoints.

``GET /api/health/tiles`` returns compact tile data for the dashboard grid.
Each ``GET /api/health/{tile}`` endpoint returns full deep-dive data.
"""

from __future__ import annotations

import time
from functools import partial
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, Query, Request

from bot_modules.core.bot_exclusion import bot_filter_clause
from bot_modules.core.db_utils import get_tz_offset_hours
from bot_modules.services.channel_rollup import build_resolver, guild_channel_ids
from bot_modules.services.health_metrics import (
    compute_channel_health,
    compute_cohort_retention,
    compute_composite_health,
    compute_dau_mau,
    compute_gini,
    compute_heatmap,
    compute_mod_engagement,
    compute_mod_workload,
    compute_newcomer_funnel,
    compute_sentiment,
    compute_social_graph,
)
from bot_modules.services.health_service import cache_key, get_cached, set_cached
from bot_modules.services.message_store import get_known_channels_bulk, get_known_users_bulk
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _guild_extras(ctx, guild):
    """Extract live guild data needed by compute functions.

    ``degraded`` reports that the live member list ``mod_ids`` /
    ``recent_joins`` are derived from wasn't there to read: the bot is
    mid-startup, the gateway's member cache hasn't been chunked yet, or the bot
    isn't in this guild at all. Any guild that exists has at least its owner, so
    "no non-bot member visible" is the cache being cold, not a real answer.

    Metrics built from an empty member list come back zeroed. That payload is
    still returned to the caller — a blank tile is better than an error — but it
    must never be written into the 15-minute cache, or a few seconds of startup
    get served as fact for the next quarter of an hour. See
    ``_cache_unless_degraded``.
    """
    member_count = guild.member_count if guild else 0
    voice_active = 0
    nsfw_ids: list[int] = []
    mod_ids: list[int] = []
    recent_joins: dict[int, float] = {}
    humans_seen = 0

    if guild:
        for vc in guild.voice_channels:
            voice_active += sum(1 for m in vc.members if not m.bot)
        nsfw_ids = [ch.id for ch in guild.channels if getattr(ch, "nsfw", False)]
        for m in guild.members:
            if m.bot:
                continue
            humans_seen += 1
            perms = m.guild_permissions
            if (
                perms.administrator
                or perms.manage_guild
                or perms.kick_members
                or perms.ban_members
            ):
                mod_ids.append(m.id)
            if m.joined_at:
                age = time.time() - m.joined_at.timestamp()
                if age < 90 * 86400:
                    recent_joins[m.id] = m.joined_at.timestamp()

    return {
        "member_count": member_count,
        "voice_active": voice_active,
        "nsfw_ids": nsfw_ids,
        "mod_ids": mod_ids,
        "recent_joins": recent_joins,
        "degraded": guild is None or humans_seen == 0,
    }


def _cache_unless_degraded(conn, guild_id: int, key: str, payload: dict, *, degraded: bool) -> None:
    """Write *payload* to the 15-minute cache unless it was computed degraded.

    Callers always read ``get_cached`` first, so an existing good value keeps
    being served for the rest of its TTL; skipping the write just means a
    degraded payload never *becomes* that value.
    """
    if not degraded:
        set_cached(conn, guild_id, key, payload)


def _resolve_user_names(conn, guild, guild_id, user_ids: set[int]) -> dict[int, str]:
    """Resolve user IDs to display names via guild cache then DB fallback.

    Any IDs that can't be resolved get a friendly "User <id>" placeholder
    so frontend consumers never have to render a raw ID.
    """
    names: dict[int, str] = {}
    if guild:
        for uid in user_ids:
            m = guild.get_member(uid)
            if m:
                names[uid] = m.display_name
    missing = user_ids - set(names.keys())
    if missing:
        db_names = get_known_users_bulk(conn, guild_id, list(missing))
        names.update(db_names)
    for uid in user_ids:
        if not names.get(uid):
            names[uid] = f"User {uid}"
    return names


def _resolve_channel_names(
    conn, guild, guild_id, channel_ids: set[int]
) -> dict[int, str]:
    names: dict[int, str] = {}
    if guild:
        for cid in channel_ids:
            ch = guild.get_channel(cid)
            if ch:
                names[cid] = ch.name
    missing = channel_ids - set(names.keys())
    if missing:
        db_names = get_known_channels_bulk(conn, guild_id, list(missing))
        names.update(db_names)
    return names


# ---------------------------------------------------------------------------
# Sentiment reads
#
# ``messages`` carries duplicated ``sentiment``/``emotion`` columns alongside
# the ``message_sentiment`` table, written by the same code paths (events_cog
# on ingest, sentiment_service on backfill), and indexed as
# ``idx_messages_sentiment (guild_id, sentiment)``. Reading ``messages``
# directly therefore returns identical rows while dropping the join: prod at
# the time of writing had 502,051 rows on both sides, zero rows present in one
# and not the other, and zero value or emotion mismatches.
#
# The join form could only seek ``message_sentiment`` on ``guild_id`` (the
# indexes there are ``(guild_id, computed_at)`` and ``(guild_id, channel_id)``),
# so every query walked the whole guild's sentiment rows, did a rowid lookup
# into ``messages`` per row, then sorted in a temp b-tree. Measured on prod:
# feed 278 ms -> 0.1 ms, 24 h counts 247 ms -> 153 ms, stddev 254 ms -> 52 ms,
# outliers 249 ms -> 0.1 ms.
# ---------------------------------------------------------------------------


def _sentiment_row(r) -> dict:
    return {
        "message_id": str(r["message_id"]),
        "channel_id": str(r["channel_id"]),
        "author_id": str(r["author_id"]),
        "content": r["content"],
        "sentiment": r["sentiment"],
        "emotion": r["emotion"],
        "ts": r["ts"],
    }


def _sentiment_feed_payload(
    conn,
    guild_id: int,
    bot_clause: str,
    bot_params: tuple,
    *,
    limit: int,
    snippet: int | None,
) -> dict:
    """Strongly-positive/negative messages plus 24 h positive/negative counts."""
    # Both interpolations are ints from this module's own call sites, never
    # request input — they cannot be bound as parameters inside substr()/LIMIT
    # without SQLite re-planning per call.
    content_expr = (
        "content" if snippet is None else f"substr(content, 1, {int(snippet)})"
    )
    rows = conn.execute(
        f"""SELECT message_id, channel_id, author_id,
                   {content_expr} AS content, sentiment, emotion, ts
            FROM messages
            WHERE guild_id = ?
              AND (sentiment >= 0.5 OR sentiment <= -0.5)
              {bot_clause}
            ORDER BY ts DESC LIMIT {int(limit)}""",
        (guild_id, *bot_params),
    ).fetchall()
    day_ago = time.time() - 86400
    pos_count = conn.execute(
        f"SELECT COUNT(*) FROM messages "
        f"WHERE guild_id = ? AND sentiment >= 0.5 AND ts >= ?{bot_clause}",
        (guild_id, day_ago, *bot_params),
    ).fetchone()[0]
    neg_count = conn.execute(
        f"SELECT COUNT(*) FROM messages "
        f"WHERE guild_id = ? AND sentiment <= -0.5 AND ts >= ?{bot_clause}",
        (guild_id, day_ago, *bot_params),
    ).fetchone()[0]
    return {
        "messages": [_sentiment_row(r) for r in rows],
        "positive_24h": pos_count,
        "negative_24h": neg_count,
    }


def _sentiment_outliers(
    conn, guild_id: int, bot_clause: str, bot_params: tuple, *, avg: float
) -> dict:
    """The two most positive / most negative messages beyond 1 sigma of *avg*."""
    # ``sentiment IS NOT NULL`` replaces what the join to message_sentiment used
    # to do implicitly — without it, unscored messages would drag the mean.
    std_row = conn.execute(
        f"SELECT COALESCE(SQRT(AVG((sentiment - ?) * (sentiment - ?))), 0.3) AS sd "
        f"FROM messages "
        f"WHERE guild_id = ? AND ts >= ? AND sentiment IS NOT NULL{bot_clause}",
        (avg, avg, guild_id, time.time() - 86400 * 30, *bot_params),
    ).fetchone()
    sd = max(std_row["sd"], 0.1)
    top2 = conn.execute(
        f"""SELECT message_id, channel_id, author_id,
                   substr(content, 1, 100) AS content, sentiment, emotion, ts
            FROM messages
            WHERE guild_id = ? AND sentiment >= ?{bot_clause}
            ORDER BY sentiment DESC, ts DESC LIMIT 2""",
        (guild_id, avg + sd, *bot_params),
    ).fetchall()
    bot2 = conn.execute(
        f"""SELECT message_id, channel_id, author_id,
                   substr(content, 1, 100) AS content, sentiment, emotion, ts
            FROM messages
            WHERE guild_id = ? AND sentiment <= ?{bot_clause}
            ORDER BY sentiment ASC, ts DESC LIMIT 2""",
        (guild_id, avg - sd, *bot_params),
    ).fetchall()
    return {
        "top": [_sentiment_row(r) for r in top2],
        "bottom": [_sentiment_row(r) for r in bot2],
        "threshold": round(sd, 3),
    }


# ---------------------------------------------------------------------------
# Grid endpoint — compact data for all tiles
# ---------------------------------------------------------------------------


@router.get("/health/tiles")
async def health_tiles(
    request: Request,
    tiles_filter: Optional[str] = Query(None, alias="tiles"),
    include_bots: bool = Query(False),
    user: AuthenticatedUser = Depends(require_perms(set())),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot_clause, bot_params = bot_filter_clause(guild_id, include_bots=include_bots)
    ck = partial(cache_key, include_bots=include_bots)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    extras = _guild_extras(ctx, guild)
    # Read off the guild here, on the event loop, not inside the threaded _q.
    tile_live_ids = guild_channel_ids(guild)
    is_admin = "admin" in user.perms
    is_mod = "moderator" in user.perms

    # If tiles filter is provided, only compute those tiles.
    wanted_tiles: set[str] | None = None
    if tiles_filter:
        wanted_tiles = {t.strip() for t in tiles_filter.split(",") if t.strip()}

    def _want(tile_key: str) -> bool:
        return wanted_tiles is None or tile_key in wanted_tiles

    def _q():
        with ctx.open_db() as conn:
            tiles = {}
            # Status bar data
            status_bar = {
                "active_users_1h": conn.execute(
                    f"SELECT COUNT(DISTINCT author_id) FROM messages "
                    f"WHERE guild_id=? AND ts>=?{bot_clause}",
                    (guild_id, int(time.time() - 3600), *bot_params),
                ).fetchone()[0],
                "active_channels_1h": conn.execute(
                    f"SELECT COUNT(DISTINCT channel_id) FROM messages "
                    f"WHERE guild_id=? AND ts>=?{bot_clause}",
                    (guild_id, int(time.time() - 3600), *bot_params),
                ).fetchone()[0],
                "voice_active": extras["voice_active"],
                "recent_joins_today": sum(
                    1
                    for ts in extras["recent_joins"].values()
                    if time.time() - ts < 86400
                ),
                "member_count": extras["member_count"],
            }

            # --- Tiles visible to all authenticated users (admin + mod) ---
            if is_admin or is_mod:
                if _want("dau_mau"):
                    cached = get_cached(conn, guild_id, ck("dau_mau"))
                    if cached is None:
                        cached = compute_dau_mau(
                            conn,
                            guild_id,
                            member_count=extras["member_count"],
                            voice_active_count=extras["voice_active"],
                            include_bots=include_bots,
                        )
                        set_cached(conn, guild_id, ck("dau_mau"), cached)
                    tiles["dau_mau"] = {
                        "dau_mau": cached["dau_mau"],
                        "wau_mau": cached["wau_mau"],
                        "dau": cached["dau"],
                        "mau": cached["mau"],
                        "badge": cached["badge"],
                        "sparkline": cached["sparkline"],
                    }

                if _want("heatmap"):
                    cached = get_cached(conn, guild_id, ck("heatmap"))
                    if cached is None:
                        cached = compute_heatmap(
                            conn, guild_id, include_bots=include_bots
                        )
                        set_cached(conn, guild_id, ck("heatmap"), cached)
                    tiles["heatmap"] = {
                        "grid": cached["grid"],
                        "peak_slot": cached["peak_slot"],
                        "peak_value": cached["peak_value"],
                        "quiet_slot": cached["quiet_slot"],
                        "dead_hours": cached["dead_hours"],
                    }

                if _want("channel_health"):
                    cached = get_cached(conn, guild_id, ck("channel_health"))
                    if cached is None:
                        cached = compute_channel_health(
                            conn,
                            guild_id,
                            nsfw_channel_ids=extras["nsfw_ids"],
                            include_bots=include_bots,
                            resolver=build_resolver(
                                conn, guild_id, live_channel_ids=tile_live_ids
                            ),
                        )
                        set_cached(conn, guild_id, ck("channel_health"), cached)
                    tiles["channel_health"] = {
                        "active_count": cached["active_count"],
                        "flagged_count": cached["flagged_count"],
                        "dormant_count": cached["dormant_count"],
                        "top5": cached["top5"],
                    }

                if _want("mod_workload"):
                    cached = get_cached(conn, guild_id, ck("mod_workload"))
                    if cached is None:
                        cached = compute_mod_workload(
                            conn,
                            guild_id,
                            mod_ids=extras["mod_ids"],
                        )
                        _cache_unless_degraded(
                            conn,
                            guild_id,
                            ck("mod_workload"),
                            cached,
                            degraded=extras["degraded"],
                        )
                    if is_admin:
                        tiles["mod_workload"] = {
                            "median_response_time": cached["median_response_time"],
                            "badge": cached["badge"],
                            "workload_gini": cached["workload_gini"],
                            "total_actions_7d": cached["total_actions_7d"],
                            "mod_actions": cached["mod_actions"],
                        }
                    else:
                        own = [
                            m
                            for m in cached["mod_actions"]
                            if m["user_id"] == str(user.user_id)
                        ]
                        tiles["mod_workload"] = {
                            "median_response_time": cached["median_response_time"],
                            "badge": cached["badge"],
                            "total_actions_7d": cached["total_actions_7d"],
                            "mod_actions": own,
                        }

                if _want("sentiment_feed"):
                    cached = get_cached(conn, guild_id, ck("sentiment_feed"))
                    if cached is None:
                        cached = _sentiment_feed_payload(
                            conn,
                            guild_id,
                            bot_clause,
                            bot_params,
                            limit=8,
                            snippet=120,
                        )
                        set_cached(conn, guild_id, ck("sentiment_feed"), cached)
                    tiles["sentiment_feed"] = cached

            # --- Admin-only tiles ---
            if is_admin:
                if _want("gini"):
                    cached = get_cached(conn, guild_id, ck("gini"))
                    if cached is None:
                        cached = compute_gini(
                            conn, guild_id, include_bots=include_bots
                        )
                        set_cached(conn, guild_id, ck("gini"), cached)
                    tiles["gini"] = {
                        "gini": cached["gini"],
                        "badge": cached["badge"],
                        "top5_share": cached["top5_share"],
                        "sparkline": cached["sparkline"],
                    }

                if _want("social_graph"):
                    cached = get_cached(conn, guild_id, ck("social_graph"))
                    if cached is None:
                        cached = compute_social_graph(
                            conn,
                            guild_id,
                            nsfw_channel_ids=extras["nsfw_ids"],
                        )
                        set_cached(conn, guild_id, ck("social_graph"), cached)
                    tiles["social_graph"] = {
                        "clustering_coefficient": cached["clustering_coefficient"],
                        "badge": cached["badge"],
                        "network_density": cached["network_density"],
                        "bridge_count": cached["bridge_count"],
                        "isolates": cached["isolates"],
                        "node_count": cached["node_count"],
                    }

                if _want("sentiment"):
                    cached = get_cached(conn, guild_id, ck("sentiment"))
                    if cached is None:
                        cached = compute_sentiment(
                            conn, guild_id, include_bots=include_bots
                        )
                        set_cached(conn, guild_id, ck("sentiment"), cached)

                    # Outlier messages: 1 sigma above / below the mean. Cached
                    # under their own key (compute_sentiment builds the tile
                    # payload and doesn't know about them); same 15-min TTL, so
                    # the mean they key off is at most one TTL out of step.
                    _outliers = get_cached(conn, guild_id, ck("sentiment_outliers"))
                    if _outliers is None:
                        _outliers = _sentiment_outliers(
                            conn,
                            guild_id,
                            bot_clause,
                            bot_params,
                            avg=cached["avg_sentiment"],
                        )
                        set_cached(
                            conn, guild_id, ck("sentiment_outliers"), _outliers
                        )

                    tiles["sentiment"] = {
                        "avg_sentiment": cached["avg_sentiment"],
                        "badge": cached["badge"],
                        "emotions": cached["emotions"],
                        "spikes_7d": cached["spikes_7d"],
                        "pos_neg_ratio": cached["pos_neg_ratio"],
                        "sparkline": cached["sparkline"],
                        "outliers": _outliers,
                    }

                if _want("newcomer_funnel"):
                    cached = get_cached(conn, guild_id, ck("newcomer_funnel"))
                    if cached is None:
                        cached = compute_newcomer_funnel(
                            conn,
                            guild_id,
                            recent_join_ids=extras["recent_joins"],
                            include_bots=include_bots,
                        )
                        _cache_unless_degraded(
                            conn,
                            guild_id,
                            ck("newcomer_funnel"),
                            cached,
                            degraded=extras["degraded"],
                        )
                    tiles["newcomer_funnel"] = {
                        "activation_rate": cached["activation_rate"],
                        "badge": cached["badge"],
                        "funnel": cached["funnel"],
                        "time_to_first_msg": cached["time_to_first_msg"][
                            "median_hours"
                        ],
                        "first_response_latency": cached["first_response_latency"][
                            "median_minutes"
                        ],
                    }

                if _want("cohort_retention"):
                    cached = get_cached(conn, guild_id, ck("cohort_retention"))
                    if cached is None:
                        cached = compute_cohort_retention(
                            conn,
                            guild_id,
                            join_times=extras["recent_joins"],
                            include_bots=include_bots,
                        )
                        _cache_unless_degraded(
                            conn,
                            guild_id,
                            ck("cohort_retention"),
                            cached,
                            degraded=extras["degraded"],
                        )
                    tiles["cohort_retention"] = {
                        "d7": cached["d7"],
                        "d30": cached["d30"],
                        "badge": cached["badge"],
                        "latest_cohort_size": cached["latest_cohort_size"],
                    }


                if _want("composite"):
                    # Composite depends on other tiles being cached — compute
                    # any missing dependencies first so get_cached finds them.
                    # The trailing bool is "this dep is built from the live
                    # member list", i.e. it must not be cached while degraded.
                    composite_deps: list[
                        tuple[str, Callable[..., Any], dict[str, Any], bool]
                    ] = [
                        (
                            "dau_mau",
                            compute_dau_mau,
                            {
                                "member_count": extras["member_count"],
                                "voice_active_count": extras["voice_active"],
                                "include_bots": include_bots,
                            },
                            False,
                        ),
                        ("gini", compute_gini, {"include_bots": include_bots}, False),
                        (
                            "social_graph",
                            compute_social_graph,
                            {"nsfw_channel_ids": extras["nsfw_ids"]},
                            False,
                        ),
                        (
                            "sentiment",
                            compute_sentiment,
                            {"include_bots": include_bots},
                            False,
                        ),
                        (
                            "cohort_retention",
                            compute_cohort_retention,
                            {
                                "join_times": extras["recent_joins"],
                                "include_bots": include_bots,
                            },
                            True,
                        ),
                        (
                            "heatmap",
                            compute_heatmap,
                            {"include_bots": include_bots},
                            False,
                        ),
                    ]
                    dep_payloads: dict[str, Any] = {}
                    for dep_key, dep_fn, dep_kw, needs_guild in composite_deps:
                        dep_payloads[dep_key] = get_cached(conn, guild_id, ck(dep_key))
                        if dep_payloads[dep_key] is None:
                            dep_result = dep_fn(conn, guild_id, **dep_kw)
                            _cache_unless_degraded(
                                conn,
                                guild_id,
                                ck(dep_key),
                                dep_result,
                                degraded=needs_guild and extras["degraded"],
                            )
                            dep_payloads[dep_key] = dep_result

                    # Read the payloads collected above, not the cache: a
                    # degraded dep is deliberately absent from the cache, and
                    # re-reading would hand ``None`` to the composite.
                    composite = compute_composite_health(
                        conn,
                        guild_id,
                        dau_mau_data=dep_payloads["dau_mau"],
                        gini_data=dep_payloads["gini"],
                        social_data=dep_payloads["social_graph"],
                        sentiment_data=dep_payloads["sentiment"],
                        retention_data=dep_payloads["cohort_retention"],
                        heatmap_data=dep_payloads["heatmap"],
                    )
                    tiles["composite"] = {
                        "score": composite["score"],
                        "badge": composite["badge"],
                        "dimensions": composite["dimensions"],
                    }

            # Resolve names for channel health top5 + sentiment feed
            ch_ids = set()
            for tile_key in ("channel_health",):
                if tile_key in tiles and "top5" in tiles[tile_key]:
                    for ch in tiles[tile_key]["top5"]:
                        ch_ids.add(int(ch["channel_id"]))
            if "sentiment_feed" in tiles:
                for msg in tiles["sentiment_feed"].get("messages", []):
                    ch_ids.add(int(msg["channel_id"]))
            if "sentiment" in tiles:
                for msg in tiles["sentiment"].get("outliers", {}).get("top", []):
                    ch_ids.add(int(msg["channel_id"]))
                for msg in tiles["sentiment"].get("outliers", {}).get("bottom", []):
                    ch_ids.add(int(msg["channel_id"]))
            ch_names = (
                _resolve_channel_names(conn, guild, guild_id, ch_ids)
                if ch_ids
                else {}
            )

            # Resolve names for mod workload + sentiment feed
            mod_user_ids = set()
            if "mod_workload" in tiles:
                for m in tiles["mod_workload"].get("mod_actions", []):
                    mod_user_ids.add(int(m["user_id"]))
            if "sentiment_feed" in tiles:
                for msg in tiles["sentiment_feed"].get("messages", []):
                    mod_user_ids.add(int(msg["author_id"]))
            if "sentiment" in tiles:
                for msg in tiles["sentiment"].get("outliers", {}).get("top", []):
                    mod_user_ids.add(int(msg["author_id"]))
                for msg in tiles["sentiment"].get("outliers", {}).get("bottom", []):
                    mod_user_ids.add(int(msg["author_id"]))
            user_names = (
                _resolve_user_names(conn, guild, guild_id, mod_user_ids)
                if mod_user_ids
                else {}
            )

            return {
                "status_bar": status_bar,
                "tiles": tiles,
                "channel_names": {str(k): v for k, v in ch_names.items()},
                "user_names": {str(k): v for k, v in user_names.items()},
            }

    return await run_query(_q)


# ---------------------------------------------------------------------------
# Deep-dive endpoints
#
# Each returns a superset of its tile (per-channel breakdowns, full graphs,
# 50-row feeds) and used to recompute from scratch on every request. They now
# share the tiles' 15-minute ``health_metrics_cache``, under a ``deep:``
# namespace so a deep-dive payload can never be served where a tile payload is
# expected (the two have different shapes for the same metric).
#
# Display names are deliberately resolved *after* the cache read and are never
# stored: ``set_cached`` serialises before the merge, so a nickname change shows
# up on the next request rather than after the TTL.
# ---------------------------------------------------------------------------


def _deep_key(metric: str, *, include_bots: bool = False, **params: object) -> str:
    """Cache key for a deep-dive payload.

    Every request parameter that changes the result must be folded in — a
    collision here would serve one population's numbers under another's
    filters, which is worse than recomputing. ``include_bots`` rides on
    ``cache_key``'s existing ``+bots`` suffix; anything else (``days``, ranges)
    is appended as a sorted ``|name=value`` list.
    """
    suffix = "".join(f"|{k}={params[k]}" for k in sorted(params))
    return f"deep:{cache_key(metric, include_bots=include_bots)}{suffix}"


@router.get("/health/dau-mau")
async def health_dau_mau(
    request: Request,
    include_bots: bool = Query(False),
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    extras = _guild_extras(ctx, guild)
    key = _deep_key("dau_mau", include_bots=include_bots)

    def _q():
        with ctx.open_db() as conn:
            data = get_cached(conn, guild_id, key)
            if data is None:
                data = compute_dau_mau(
                    conn,
                    guild_id,
                    member_count=extras["member_count"],
                    voice_active_count=extras["voice_active"],
                    include_bots=include_bots,
                )
                set_cached(conn, guild_id, key, data)
            return data

    return await run_query(_q)


@router.get("/health/heatmap")
async def health_heatmap(
    request: Request,
    include_bots: bool = Query(False),
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    key = _deep_key("heatmap", include_bots=include_bots)
    bot = getattr(ctx, "bot", None)
    live_ids = guild_channel_ids(bot.get_guild(guild_id) if bot else None)

    def _q():
        with ctx.open_db() as conn:
            data = get_cached(conn, guild_id, key)
            if data is None:
                tz = get_tz_offset_hours(conn, guild_id)
                data = compute_heatmap(
                    conn,
                    guild_id,
                    utc_offset_hours=tz,
                    include_bots=include_bots,
                    resolver=build_resolver(
                        conn, guild_id, live_channel_ids=live_ids
                    ),
                )
                set_cached(conn, guild_id, key, data)
            # Resolve channel names
            ch_ids = {int(ch["channel_id"]) for ch in data["per_channel"]}
            bot = getattr(ctx, "bot", None)
            guild = bot.get_guild(guild_id) if bot else None
            ch_names = _resolve_channel_names(conn, guild, guild_id, ch_ids)
            for ch in data["per_channel"]:
                ch["channel_name"] = ch_names.get(int(ch["channel_id"]), "")
            return data

    return await run_query(_q)


@router.get("/health/channel-health")
async def health_channel_health(
    request: Request,
    include_bots: bool = Query(False),
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    nsfw_ids = (
        [ch.id for ch in guild.channels if getattr(ch, "nsfw", False)] if guild else []
    )
    live_ids = guild_channel_ids(guild)

    key = _deep_key("channel_health", include_bots=include_bots)

    def _q():
        with ctx.open_db() as conn:
            data = get_cached(conn, guild_id, key)
            if data is None:
                data = compute_channel_health(
                    conn,
                    guild_id,
                    nsfw_channel_ids=nsfw_ids,
                    include_bots=include_bots,
                    resolver=build_resolver(
                        conn, guild_id, live_channel_ids=live_ids
                    ),
                )
                set_cached(conn, guild_id, key, data)
            ch_ids = {int(ch["channel_id"]) for ch in data["channels"]}
            ch_names = _resolve_channel_names(conn, guild, guild_id, ch_ids)
            for ch in data["channels"]:
                ch["channel_name"] = ch_names.get(int(ch["channel_id"]), "")
            return data

    return await run_query(_q)


@router.get("/health/gini")
async def health_gini(
    request: Request,
    include_bots: bool = Query(False),
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None

    key = _deep_key("gini", include_bots=include_bots)

    def _q():
        with ctx.open_db() as conn:
            data = get_cached(conn, guild_id, key)
            if data is None:
                data = compute_gini(conn, guild_id, include_bots=include_bots)
                set_cached(conn, guild_id, key, data)
            ch_ids = {int(ch["channel_id"]) for ch in data["per_channel"]}
            ch_names = _resolve_channel_names(conn, guild, guild_id, ch_ids)
            for ch in data["per_channel"]:
                ch["channel_name"] = ch_names.get(int(ch["channel_id"]), "")
            return data

    return await run_query(_q)


@router.get("/health/social-graph")
async def health_social_graph(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    nsfw_ids = (
        [ch.id for ch in guild.channels if getattr(ch, "nsfw", False)] if guild else []
    )

    key = _deep_key("social_graph")

    def _q():
        with ctx.open_db() as conn:
            data = get_cached(conn, guild_id, key)
            if data is None:
                data = compute_social_graph(conn, guild_id, nsfw_channel_ids=nsfw_ids)
                set_cached(conn, guild_id, key, data)
            # Resolve user names for bridge users and graph nodes
            user_ids = set()
            for b in data["bridge_users"]:
                user_ids.add(int(b["user_id"]))
            for n in data["graph_nodes"]:
                user_ids.add(int(n["id"]))
            names = _resolve_user_names(conn, guild, guild_id, user_ids)
            for b in data["bridge_users"]:
                b["user_name"] = names.get(int(b["user_id"]), "")
            for n in data["graph_nodes"]:
                n["name"] = names.get(int(n["id"]), "")
            return data

    return await run_query(_q)


@router.get("/health/sentiment")
async def health_sentiment(
    request: Request,
    include_bots: bool = Query(False),
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None

    key = _deep_key("sentiment", include_bots=include_bots)

    def _q():
        with ctx.open_db() as conn:
            data = get_cached(conn, guild_id, key)
            if data is None:
                data = compute_sentiment(conn, guild_id, include_bots=include_bots)
                set_cached(conn, guild_id, key, data)
            ch_ids = {int(ch["channel_id"]) for ch in data["per_channel"]}
            ch_names = _resolve_channel_names(conn, guild, guild_id, ch_ids)
            for ch in data["per_channel"]:
                ch["channel_name"] = ch_names.get(int(ch["channel_id"]), "")
            return data

    return await run_query(_q)


@router.get("/health/sentiment-feed")
async def health_sentiment_feed(
    request: Request,
    include_bots: bool = Query(False),
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    bot_clause, bot_params = bot_filter_clause(guild_id, include_bots=include_bots)
    key = _deep_key("sentiment_feed", include_bots=include_bots)

    def _q():
        with ctx.open_db() as conn:
            data = get_cached(conn, guild_id, key)
            if data is None:
                data = _sentiment_feed_payload(
                    conn, guild_id, bot_clause, bot_params, limit=50, snippet=None
                )
                set_cached(conn, guild_id, key, data)
            messages = data["messages"]
            ch_ids = {int(m["channel_id"]) for m in messages}
            user_ids = {int(m["author_id"]) for m in messages}
            ch_names = (
                _resolve_channel_names(conn, guild, guild_id, ch_ids)
                if ch_ids
                else {}
            )
            u_names = (
                _resolve_user_names(conn, guild, guild_id, user_ids)
                if user_ids
                else {}
            )
            for m in messages:
                m["channel_name"] = ch_names.get(int(m["channel_id"]), "")
                m["author_name"] = u_names.get(int(m["author_id"]), "")
            return data

    return await run_query(_q)


@router.get("/health/newcomer-funnel")
async def health_newcomer_funnel(
    request: Request,
    include_bots: bool = Query(False),
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    extras = _guild_extras(ctx, guild)

    key = _deep_key("newcomer_funnel", include_bots=include_bots)

    def _q():
        with ctx.open_db() as conn:
            data = get_cached(conn, guild_id, key)
            if data is None:
                data = compute_newcomer_funnel(
                    conn,
                    guild_id,
                    recent_join_ids=extras["recent_joins"],
                    include_bots=include_bots,
                )
                _cache_unless_degraded(
                    conn, guild_id, key, data, degraded=extras["degraded"]
                )
            return data

    return await run_query(_q)


@router.get("/health/cohort-retention")
async def health_cohort_retention(
    request: Request,
    include_bots: bool = Query(False),
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    extras = _guild_extras(ctx, guild)

    key = _deep_key("cohort_retention", include_bots=include_bots)

    def _q():
        with ctx.open_db() as conn:
            data = get_cached(conn, guild_id, key)
            if data is None:
                data = compute_cohort_retention(
                    conn,
                    guild_id,
                    join_times=extras["recent_joins"],
                    include_bots=include_bots,
                )
                _cache_unless_degraded(
                    conn, guild_id, key, data, degraded=extras["degraded"]
                )
            return data

    return await run_query(_q)


@router.get("/health/mod-workload")
async def health_mod_workload(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    extras = _guild_extras(ctx, guild)

    key = _deep_key("mod_workload")

    def _q():
        with ctx.open_db() as conn:
            data = get_cached(conn, guild_id, key)
            if data is None:
                data = compute_mod_workload(conn, guild_id, mod_ids=extras["mod_ids"])
                _cache_unless_degraded(
                    conn, guild_id, key, data, degraded=extras["degraded"]
                )
            user_ids = {int(m["user_id"]) for m in data["mod_actions"]}
            names = _resolve_user_names(conn, guild, guild_id, user_ids)
            for m in data["mod_actions"]:
                m["user_name"] = names.get(int(m["user_id"]), "")
            return data

    return await run_query(_q)


@router.get("/health/mod-engagement")
async def health_mod_engagement(
    request: Request,
    days: int = Query(7, ge=1, le=365),
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    extras = _guild_extras(ctx, guild)

    # ``days`` changes the population, so it must be part of the key.
    key = _deep_key("mod_engagement", days=days)

    def _q():
        with ctx.open_db() as conn:
            data = get_cached(conn, guild_id, key)
            if data is None:
                data = compute_mod_engagement(
                    conn,
                    guild_id,
                    mod_ids=extras["mod_ids"],
                    recent_joins=extras["recent_joins"],
                    days=days,
                )
                _cache_unless_degraded(
                    conn, guild_id, key, data, degraded=extras["degraded"]
                )
            user_ids = {int(m["user_id"]) for m in data["mods"]}
            names = _resolve_user_names(conn, guild, guild_id, user_ids)
            for m in data["mods"]:
                m["user_name"] = names.get(int(m["user_id"]), "")
            return data

    return await run_query(_q)


@router.get("/health/composite-score")
async def health_composite_score(
    request: Request,
    include_bots: bool = Query(False),
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    extras = _guild_extras(ctx, guild)
    ck = partial(cache_key, include_bots=include_bots)

    def _q():
        with ctx.open_db() as conn:
            dau_data = get_cached(conn, guild_id, ck("dau_mau")) or compute_dau_mau(
                conn,
                guild_id,
                member_count=extras["member_count"],
                voice_active_count=extras["voice_active"],
                include_bots=include_bots,
            )
            gini_data = get_cached(conn, guild_id, ck("gini")) or compute_gini(
                conn, guild_id, include_bots=include_bots
            )
            social_data = get_cached(
                conn, guild_id, ck("social_graph")
            ) or compute_social_graph(
                conn,
                guild_id,
                nsfw_channel_ids=extras["nsfw_ids"],
            )
            sentiment_data = get_cached(
                conn, guild_id, ck("sentiment")
            ) or compute_sentiment(conn, guild_id, include_bots=include_bots)
            retention_data = get_cached(
                conn, guild_id, ck("cohort_retention")
            ) or compute_cohort_retention(
                conn,
                guild_id,
                join_times=extras["recent_joins"],
                include_bots=include_bots,
            )
            heatmap_data = get_cached(conn, guild_id, ck("heatmap")) or compute_heatmap(
                conn, guild_id, include_bots=include_bots
            )
            return compute_composite_health(
                conn,
                guild_id,
                dau_mau_data=dau_data,
                gini_data=gini_data,
                social_data=social_data,
                sentiment_data=sentiment_data,
                retention_data=retention_data,
                heatmap_data=heatmap_data,
            )

    return await run_query(_q)
