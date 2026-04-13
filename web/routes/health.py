"""Community health dashboard API endpoints.

``GET /api/health/tiles`` returns compact tile data for the dashboard grid.
Each ``GET /api/health/{tile}`` endpoint returns full deep-dive data.
"""
from __future__ import annotations

import time

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from services.health_metrics import (
    compute_channel_health,
    compute_churn_risk,
    compute_cohort_retention,
    compute_composite_health,
    compute_dau_mau,
    compute_gini,
    compute_heatmap,
    compute_incidents,
    compute_mod_workload,
    compute_newcomer_funnel,
    compute_sentiment,
    compute_social_graph,
)
from services.health_service import get_cached, set_cached
from services.message_store import get_known_channels_bulk, get_known_users_bulk
from web.auth import AuthenticatedUser
from web.deps import get_ctx, require_perms, run_query

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _guild_extras(ctx, guild):
    """Extract live guild data needed by compute functions."""
    member_count = guild.member_count if guild else 0
    voice_active = 0
    nsfw_ids: list[int] = []
    mod_ids: list[int] = []
    recent_joins: dict[int, float] = {}

    if guild:
        for vc in guild.voice_channels:
            voice_active += sum(1 for m in vc.members if not m.bot)
        nsfw_ids = [ch.id for ch in guild.channels if getattr(ch, "nsfw", False)]
        for m in guild.members:
            if m.bot:
                continue
            perms = m.guild_permissions
            if perms.administrator or perms.manage_guild or perms.kick_members or perms.ban_members:
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
    }


def _resolve_user_names(conn, guild, guild_id, user_ids: set[int]) -> dict[int, str]:
    """Resolve user IDs to display names via guild cache then DB fallback."""
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
    return names


def _resolve_channel_names(conn, guild, guild_id, channel_ids: set[int]) -> dict[int, str]:
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
# Grid endpoint — compact data for all tiles
# ---------------------------------------------------------------------------

@router.get("/health/tiles")
async def health_tiles(
    request: Request,
    tiles_filter: Optional[str] = Query(None, alias="tiles"),
    user: AuthenticatedUser = Depends(require_perms(set())),
):
    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot else None
    extras = _guild_extras(ctx, guild)
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
                    "SELECT COUNT(DISTINCT author_id) FROM messages WHERE guild_id=? AND ts>=?",
                    (ctx.guild_id, int(time.time() - 3600)),
                ).fetchone()[0],
                "active_channels_1h": conn.execute(
                    "SELECT COUNT(DISTINCT channel_id) FROM messages WHERE guild_id=? AND ts>=?",
                    (ctx.guild_id, int(time.time() - 3600)),
                ).fetchone()[0],
                "voice_active": extras["voice_active"],
                "recent_joins_today": sum(
                    1 for ts in extras["recent_joins"].values()
                    if time.time() - ts < 86400
                ),
                "member_count": extras["member_count"],
            }

            # --- Tiles visible to all authenticated users (admin + mod) ---
            if is_admin or is_mod:
                if _want("dau_mau"):
                    cached = get_cached(conn, ctx.guild_id, "dau_mau")
                    if cached is None:
                        cached = compute_dau_mau(
                            conn, ctx.guild_id,
                            member_count=extras["member_count"],
                            voice_active_count=extras["voice_active"],
                        )
                        set_cached(conn, ctx.guild_id, "dau_mau", cached)
                    tiles["dau_mau"] = {
                        "dau_mau": cached["dau_mau"], "wau_mau": cached["wau_mau"],
                        "dau": cached["dau"], "mau": cached["mau"],
                        "badge": cached["badge"], "sparkline": cached["sparkline"],
                    }

                if _want("heatmap"):
                    cached = get_cached(conn, ctx.guild_id, "heatmap")
                    if cached is None:
                        cached = compute_heatmap(conn, ctx.guild_id)
                        set_cached(conn, ctx.guild_id, "heatmap", cached)
                    tiles["heatmap"] = {
                        "grid": cached["grid"],
                        "peak_slot": cached["peak_slot"], "peak_value": cached["peak_value"],
                        "quiet_slot": cached["quiet_slot"],
                        "dead_hours": cached["dead_hours"],
                    }

                if _want("channel_health"):
                    cached = get_cached(conn, ctx.guild_id, "channel_health")
                    if cached is None:
                        cached = compute_channel_health(
                            conn, ctx.guild_id, nsfw_channel_ids=extras["nsfw_ids"],
                        )
                        set_cached(conn, ctx.guild_id, "channel_health", cached)
                    tiles["channel_health"] = {
                        "active_count": cached["active_count"],
                        "flagged_count": cached["flagged_count"],
                        "dormant_count": cached["dormant_count"],
                        "top5": cached["top5"],
                    }

                if _want("mod_workload"):
                    cached = get_cached(conn, ctx.guild_id, "mod_workload")
                    if cached is None:
                        cached = compute_mod_workload(
                            conn, ctx.guild_id, mod_ids=extras["mod_ids"],
                        )
                        set_cached(conn, ctx.guild_id, "mod_workload", cached)
                    if is_admin:
                        tiles["mod_workload"] = {
                            "median_response_time": cached["median_response_time"],
                            "badge": cached["badge"],
                            "workload_gini": cached["workload_gini"],
                            "total_actions_7d": cached["total_actions_7d"],
                            "mod_actions": cached["mod_actions"],
                        }
                    else:
                        own = [m for m in cached["mod_actions"] if m["user_id"] == str(user.user_id)]
                        tiles["mod_workload"] = {
                            "median_response_time": cached["median_response_time"],
                            "badge": cached["badge"],
                            "total_actions_7d": cached["total_actions_7d"],
                            "mod_actions": own,
                        }

                if _want("incidents"):
                    cached = get_cached(conn, ctx.guild_id, "incidents")
                    if cached is None:
                        cached = compute_incidents(conn, ctx.guild_id)
                        set_cached(conn, ctx.guild_id, "incidents", cached)
                    tiles["incidents"] = {
                        "active_count": cached["active_count"],
                        "badge": cached["badge"],
                        "categories": cached["categories"],
                        "timeline": cached["timeline"],
                    }

            # --- Admin-only tiles ---
            if is_admin:
                if _want("gini"):
                    cached = get_cached(conn, ctx.guild_id, "gini")
                    if cached is None:
                        cached = compute_gini(conn, ctx.guild_id)
                        set_cached(conn, ctx.guild_id, "gini", cached)
                    tiles["gini"] = {
                        "gini": cached["gini"], "badge": cached["badge"],
                        "top5_share": cached["top5_share"],
                        "sparkline": cached["sparkline"],
                    }

                if _want("social_graph"):
                    cached = get_cached(conn, ctx.guild_id, "social_graph")
                    if cached is None:
                        cached = compute_social_graph(
                            conn, ctx.guild_id, nsfw_channel_ids=extras["nsfw_ids"],
                        )
                        set_cached(conn, ctx.guild_id, "social_graph", cached)
                    tiles["social_graph"] = {
                        "clustering_coefficient": cached["clustering_coefficient"],
                        "badge": cached["badge"],
                        "network_density": cached["network_density"],
                        "bridge_count": cached["bridge_count"],
                        "isolates": cached["isolates"],
                        "node_count": cached["node_count"],
                    }

                if _want("sentiment"):
                    cached = get_cached(conn, ctx.guild_id, "sentiment")
                    if cached is None:
                        cached = compute_sentiment(conn, ctx.guild_id)
                        set_cached(conn, ctx.guild_id, "sentiment", cached)
                    tiles["sentiment"] = {
                        "avg_sentiment": cached["avg_sentiment"],
                        "badge": cached["badge"],
                        "emotions": cached["emotions"],
                        "spikes_7d": cached["spikes_7d"],
                        "pos_neg_ratio": cached["pos_neg_ratio"],
                        "sparkline": cached["sparkline"],
                    }

                if _want("newcomer_funnel"):
                    cached = get_cached(conn, ctx.guild_id, "newcomer_funnel")
                    if cached is None:
                        cached = compute_newcomer_funnel(
                            conn, ctx.guild_id, recent_join_ids=extras["recent_joins"],
                        )
                        set_cached(conn, ctx.guild_id, "newcomer_funnel", cached)
                    tiles["newcomer_funnel"] = {
                        "activation_rate": cached["activation_rate"],
                        "badge": cached["badge"],
                        "funnel": cached["funnel"],
                        "time_to_first_msg": cached["time_to_first_msg"]["median_hours"],
                        "first_response_latency": cached["first_response_latency"]["median_minutes"],
                    }

                if _want("cohort_retention"):
                    cached = get_cached(conn, ctx.guild_id, "cohort_retention")
                    if cached is None:
                        cached = compute_cohort_retention(
                            conn, ctx.guild_id, join_times=extras["recent_joins"],
                        )
                        set_cached(conn, ctx.guild_id, "cohort_retention", cached)
                    tiles["cohort_retention"] = {
                        "d7": cached["d7"], "d30": cached["d30"],
                        "badge": cached["badge"],
                        "latest_cohort_size": cached["latest_cohort_size"],
                    }

                if _want("churn_risk"):
                    cached = get_cached(conn, ctx.guild_id, "churn_risk")
                    if cached is None:
                        cached = compute_churn_risk(conn, ctx.guild_id)
                        set_cached(conn, ctx.guild_id, "churn_risk", cached)
                    tiles["churn_risk"] = {
                        "at_risk_count": cached["at_risk_count"],
                        "badge": cached["badge"],
                        "critical": cached["critical"],
                        "declining": cached["declining"],
                        "watch": cached["watch"],
                    }

                if _want("composite"):
                    # Composite depends on other tiles being cached — compute
                    # any missing dependencies first so get_cached finds them.
                    for dep_key, dep_fn, dep_kw in [
                        ("dau_mau", compute_dau_mau, {"member_count": extras["member_count"], "voice_active_count": extras["voice_active"]}),
                        ("gini", compute_gini, {}),
                        ("social_graph", compute_social_graph, {"nsfw_channel_ids": extras["nsfw_ids"]}),
                        ("sentiment", compute_sentiment, {}),
                        ("cohort_retention", compute_cohort_retention, {"join_times": extras["recent_joins"]}),
                        ("heatmap", compute_heatmap, {}),
                    ]:
                        if get_cached(conn, ctx.guild_id, dep_key) is None:
                            dep_result = dep_fn(conn, ctx.guild_id, **dep_kw)
                            set_cached(conn, ctx.guild_id, dep_key, dep_result)

                    composite = compute_composite_health(
                        conn, ctx.guild_id,
                        dau_mau_data=get_cached(conn, ctx.guild_id, "dau_mau"),
                        gini_data=get_cached(conn, ctx.guild_id, "gini"),
                        social_data=get_cached(conn, ctx.guild_id, "social_graph"),
                        sentiment_data=get_cached(conn, ctx.guild_id, "sentiment"),
                        retention_data=get_cached(conn, ctx.guild_id, "cohort_retention"),
                        heatmap_data=get_cached(conn, ctx.guild_id, "heatmap"),
                    )
                    tiles["composite"] = {
                        "score": composite["score"],
                        "badge": composite["badge"],
                        "dimensions": composite["dimensions"],
                    }

            # Resolve names for channel health top5
            ch_ids = set()
            for tile_key in ("channel_health",):
                if tile_key in tiles and "top5" in tiles[tile_key]:
                    for ch in tiles[tile_key]["top5"]:
                        ch_ids.add(int(ch["channel_id"]))
            ch_names = _resolve_channel_names(conn, guild, ctx.guild_id, ch_ids) if ch_ids else {}

            # Resolve names for mod workload
            mod_user_ids = set()
            if "mod_workload" in tiles:
                for m in tiles["mod_workload"].get("mod_actions", []):
                    mod_user_ids.add(int(m["user_id"]))
            user_names = _resolve_user_names(conn, guild, ctx.guild_id, mod_user_ids) if mod_user_ids else {}

            return {
                "status_bar": status_bar,
                "tiles": tiles,
                "channel_names": {str(k): v for k, v in ch_names.items()},
                "user_names": {str(k): v for k, v in user_names.items()},
            }

    return await run_query(_q)


# ---------------------------------------------------------------------------
# Deep-dive endpoints
# ---------------------------------------------------------------------------

@router.get("/health/dau-mau")
async def health_dau_mau(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot else None
    extras = _guild_extras(ctx, guild)

    def _q():
        with ctx.open_db() as conn:
            return compute_dau_mau(
                conn, ctx.guild_id,
                member_count=extras["member_count"],
                voice_active_count=extras["voice_active"],
            )
    return await run_query(_q)


@router.get("/health/heatmap")
async def health_heatmap(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            data = compute_heatmap(conn, ctx.guild_id)
            # Resolve channel names
            ch_ids = {int(ch["channel_id"]) for ch in data["per_channel"]}
            bot = getattr(ctx, "bot", None)
            guild = bot.get_guild(ctx.guild_id) if bot else None
            ch_names = _resolve_channel_names(conn, guild, ctx.guild_id, ch_ids)
            for ch in data["per_channel"]:
                ch["channel_name"] = ch_names.get(int(ch["channel_id"]), "")
            return data
    return await run_query(_q)


@router.get("/health/channel-health")
async def health_channel_health(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot else None
    nsfw_ids = [ch.id for ch in guild.channels if getattr(ch, "nsfw", False)] if guild else []

    def _q():
        with ctx.open_db() as conn:
            data = compute_channel_health(conn, ctx.guild_id, nsfw_channel_ids=nsfw_ids)
            ch_ids = {int(ch["channel_id"]) for ch in data["channels"]}
            ch_names = _resolve_channel_names(conn, guild, ctx.guild_id, ch_ids)
            for ch in data["channels"]:
                ch["channel_name"] = ch_names.get(int(ch["channel_id"]), "")
            return data
    return await run_query(_q)


@router.get("/health/gini")
async def health_gini(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot else None

    def _q():
        with ctx.open_db() as conn:
            data = compute_gini(conn, ctx.guild_id)
            ch_ids = {int(ch["channel_id"]) for ch in data["per_channel"]}
            ch_names = _resolve_channel_names(conn, guild, ctx.guild_id, ch_ids)
            for ch in data["per_channel"]:
                ch["channel_name"] = ch_names.get(int(ch["channel_id"]), "")
            return data
    return await run_query(_q)


@router.get("/health/social-graph")
async def health_social_graph(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot else None
    nsfw_ids = [ch.id for ch in guild.channels if getattr(ch, "nsfw", False)] if guild else []

    def _q():
        with ctx.open_db() as conn:
            data = compute_social_graph(conn, ctx.guild_id, nsfw_channel_ids=nsfw_ids)
            # Resolve user names for bridge users and graph nodes
            user_ids = set()
            for b in data["bridge_users"]:
                user_ids.add(int(b["user_id"]))
            for n in data["graph_nodes"]:
                user_ids.add(int(n["id"]))
            names = _resolve_user_names(conn, guild, ctx.guild_id, user_ids)
            for b in data["bridge_users"]:
                b["user_name"] = names.get(int(b["user_id"]), "")
            for n in data["graph_nodes"]:
                n["name"] = names.get(int(n["id"]), "")
            return data
    return await run_query(_q)


@router.get("/health/sentiment")
async def health_sentiment(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot else None

    def _q():
        with ctx.open_db() as conn:
            data = compute_sentiment(conn, ctx.guild_id)
            ch_ids = {int(ch["channel_id"]) for ch in data["per_channel"]}
            ch_names = _resolve_channel_names(conn, guild, ctx.guild_id, ch_ids)
            for ch in data["per_channel"]:
                ch["channel_name"] = ch_names.get(int(ch["channel_id"]), "")
            return data
    return await run_query(_q)


@router.get("/health/newcomer-funnel")
async def health_newcomer_funnel(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot else None
    extras = _guild_extras(ctx, guild)

    def _q():
        with ctx.open_db() as conn:
            return compute_newcomer_funnel(
                conn, ctx.guild_id, recent_join_ids=extras["recent_joins"],
            )
    return await run_query(_q)


@router.get("/health/cohort-retention")
async def health_cohort_retention(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot else None
    extras = _guild_extras(ctx, guild)

    def _q():
        with ctx.open_db() as conn:
            return compute_cohort_retention(
                conn, ctx.guild_id, join_times=extras["recent_joins"],
            )
    return await run_query(_q)


@router.get("/health/churn-risk")
async def health_churn_risk(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot else None

    def _q():
        with ctx.open_db() as conn:
            data = compute_churn_risk(conn, ctx.guild_id)
            user_ids = {int(r["user_id"]) for r in data["at_risk"]}
            names = _resolve_user_names(conn, guild, ctx.guild_id, user_ids)
            for r in data["at_risk"]:
                r["user_name"] = names.get(int(r["user_id"]), "")
            return data
    return await run_query(_q)


@router.get("/health/mod-workload")
async def health_mod_workload(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot else None
    extras = _guild_extras(ctx, guild)

    def _q():
        with ctx.open_db() as conn:
            data = compute_mod_workload(conn, ctx.guild_id, mod_ids=extras["mod_ids"])
            user_ids = {int(m["user_id"]) for m in data["mod_actions"]}
            names = _resolve_user_names(conn, guild, ctx.guild_id, user_ids)
            for m in data["mod_actions"]:
                m["user_name"] = names.get(int(m["user_id"]), "")
            return data
    return await run_query(_q)


@router.get("/health/incidents")
async def health_incidents(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot else None

    def _q():
        with ctx.open_db() as conn:
            data = compute_incidents(conn, ctx.guild_id)
            ch_ids = {int(i["channel_id"]) for i in data["incident_log"] if i["channel_id"]}
            ch_names = _resolve_channel_names(conn, guild, ctx.guild_id, ch_ids) if ch_ids else {}
            for i in data["incident_log"]:
                if i["channel_id"]:
                    i["channel_name"] = ch_names.get(int(i["channel_id"]), "")
            return data
    return await run_query(_q)


@router.get("/health/composite-score")
async def health_composite_score(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot else None
    extras = _guild_extras(ctx, guild)

    def _q():
        with ctx.open_db() as conn:
            dau_data = get_cached(conn, ctx.guild_id, "dau_mau") or compute_dau_mau(
                conn, ctx.guild_id,
                member_count=extras["member_count"],
                voice_active_count=extras["voice_active"],
            )
            gini_data = get_cached(conn, ctx.guild_id, "gini") or compute_gini(conn, ctx.guild_id)
            social_data = get_cached(conn, ctx.guild_id, "social_graph") or compute_social_graph(
                conn, ctx.guild_id, nsfw_channel_ids=extras["nsfw_ids"],
            )
            sentiment_data = get_cached(conn, ctx.guild_id, "sentiment") or compute_sentiment(conn, ctx.guild_id)
            retention_data = get_cached(conn, ctx.guild_id, "cohort_retention") or compute_cohort_retention(
                conn, ctx.guild_id, join_times=extras["recent_joins"],
            )
            heatmap_data = get_cached(conn, ctx.guild_id, "heatmap") or compute_heatmap(conn, ctx.guild_id)
            return compute_composite_health(
                conn, ctx.guild_id,
                dau_mau_data=dau_data, gini_data=gini_data,
                social_data=social_data, sentiment_data=sentiment_data,
                retention_data=retention_data, heatmap_data=heatmap_data,
            )
    return await run_query(_q)
