"""Report endpoints — one per chart/table report."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request

from services import reports_data
from services.member_quality_score import compute_quality_scores
from services.message_store import get_known_channels_bulk
from services.reports_data import MemberSnapshot
from web.auth import AuthenticatedUser
from web.deps import (
    cached_run_query,
    get_active_guild_id,
    get_ctx,
    invalidate_report_cache,
    require_perms,
    run_query,
)
from web.helpers import resolve_names as _resolve_names
from web.schemas import (
    ActivityResponse,
    AnimatedHeatmapResponse,
    BurstRankingResponse,
    ChannelComparisonResponse,
    ChillingEffectResponse,
    DropoffResponse,
    GreeterResponseResponse,
    InactiveResponse,
    InactiveRoleResponse,
    InteractionGraphResponse,
    InviteEffectivenessResponse,
    JoinTimesResponse,
    ListRoleResponse,
    MessageCadenceResponse,
    MessageRateDropsResponse,
    MessageRateResponse,
    NsfwGenderResponse,
    OldestSfwResponse,
    QualityScoreResponse,
    ReactionAnalyticsResponse,
    RetentionResponse,
    RoleGrowthResponse,
    SessionBurstResponse,
    TimeToLevel5Response,
    VoiceActivityResponse,
    XpLeaderboardResponse,
    XpLevelReviewResponse,
)

router = APIRouter()


@router.post("/cache/clear")
async def clear_cache(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    guild_id = get_active_guild_id(request)
    removed = invalidate_report_cache(guild_id=guild_id)
    return {"cleared": removed}




# ── Role growth ──────────────────────────────────────────────────────────


@router.get("/role-growth", response_model=RoleGrowthResponse)
async def role_growth(
    request: Request,
    resolution: Literal["day", "week", "month"] = "week",
    roles: str | None = None,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    tz = getattr(ctx, "tz_offset_hours", 0.0)
    role_filter: set[str] | None = None
    if roles is not None:
        role_filter = {r.strip().lower() for r in roles.split(",") if r.strip()}

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_role_growth_data(
                conn, guild_id, resolution, role_filter, utc_offset_hours=tz
            )

    return await cached_run_query(
        "role-growth",
        guild_id,
        {"resolution": resolution, "roles": roles},
        _q,
    )


# ── Message cadence ──────────────────────────────────────────────────────


@router.get("/message-cadence", response_model=MessageCadenceResponse)
async def message_cadence(
    request: Request,
    resolution: Literal[
        "hour", "day", "week", "month", "hour_of_day", "day_of_week"
    ] = "hour",
    channel_id: str | None = None,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    ch_id = int(channel_id) if channel_id else None
    tz = getattr(ctx, "tz_offset_hours", 0.0)

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_message_cadence_data(
                conn,
                guild_id,
                resolution,
                tz,
                ch_id,
            )

    return await cached_run_query(
        "message-cadence",
        guild_id,
        {"resolution": resolution, "channel_id": channel_id},
        _q,
    )


# ── Join times ───────────────────────────────────────────────────────────


@router.get("/join-times", response_model=JoinTimesResponse)
async def join_times(
    request: Request,
    resolution: Literal["hour_of_day", "day_of_week"] = "hour_of_day",
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    tz = getattr(ctx, "tz_offset_hours", 0.0)

    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot is not None else None

    if guild is not None:
        members = [
            MemberSnapshot(
                user_id=m.id,
                display_name=m.display_name,
                is_bot=m.bot,
                joined_at=m.joined_at.timestamp() if m.joined_at else None,
                role_ids=tuple(r.id for r in m.roles),
            )
            for m in guild.members
        ]
    else:
        # Standalone: estimate join times from first role grant or invite_edges
        def _load_members():
            with ctx.open_db() as conn:
                # Prefer invite_edges if populated
                rows = conn.execute(
                    "SELECT invitee_id, joined_at FROM invite_edges WHERE guild_id = ?",
                    (guild_id,),
                ).fetchall()
                if rows:
                    return [
                        MemberSnapshot(
                            user_id=int(r[0]),
                            display_name=str(r[0]),
                            is_bot=False,
                            joined_at=float(r[1]),
                            role_ids=(),
                        )
                        for r in rows
                    ]
                # Fallback: first role grant per user as join proxy
                rows = conn.execute(
                    """SELECT user_id, MIN(granted_at) AS first_grant
                       FROM role_events
                       WHERE guild_id = ? AND action = 'grant'
                       GROUP BY user_id""",
                    (guild_id,),
                ).fetchall()
                return [
                    MemberSnapshot(
                        user_id=int(r[0]),
                        display_name=str(r[0]),
                        is_bot=False,
                        joined_at=float(r[1]),
                        role_ids=(),
                    )
                    for r in rows
                ]

        members = await run_query(_load_members)

    def _q():
        return reports_data.get_join_times_data(members, resolution, tz)

    return await cached_run_query(
        "join-times",
        guild_id,
        {"resolution": resolution},
        _q,
    )


# ── NSFW gender activity ────────────────────────────────────────────────


@router.get("/nsfw-gender", response_model=NsfwGenderResponse)
async def nsfw_gender(
    request: Request,
    resolution: Literal["day", "week", "month"] = "week",
    media_only: bool = False,
    channel_id: str | None = None,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    tz = getattr(ctx, "tz_offset_hours", 0.0)

    if channel_id:
        target_ids = [int(channel_id)]
    else:
        # Auto-discover NSFW channels from live guild cache
        bot = getattr(ctx, "bot", None)
        guild = bot.get_guild(guild_id) if bot is not None else None
        if guild is not None:
            target_ids = [ch.id for ch in guild.channels if getattr(ch, "nsfw", False)]
        else:
            # Standalone fallback: use all channels that have gender-tagged
            # posts — these are the channels the query would return data for.
            def _discover():
                with ctx.open_db() as conn:
                    rows = conn.execute(
                        """
                        SELECT DISTINCT m.channel_id
                        FROM messages m
                        INNER JOIN member_gender mg
                            ON mg.guild_id = m.guild_id AND mg.user_id = m.author_id
                        WHERE m.guild_id = ?
                        """,
                        (guild_id,),
                    ).fetchall()
                    return [int(r[0]) for r in rows]

            target_ids = await run_query(_discover)

    if not target_ids:
        return NsfwGenderResponse(
            resolution=resolution,
            window_label="",
            media_only=media_only,
            labels=[],
            series=[],
        )

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_nsfw_gender_data(
                conn,
                guild_id,
                resolution,
                target_ids,
                tz,
                media_only,
            )

    return await cached_run_query(
        "nsfw-gender",
        guild_id,
        {"resolution": resolution, "media_only": media_only, "channel_id": channel_id},
        _q,
    )


# ── Message rate ─────────────────────────────────────────────────────────


@router.get("/message-rate", response_model=MessageRateResponse)
async def message_rate(
    request: Request,
    days: int = 30,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    days = max(1, min(365, days))
    tz = getattr(ctx, "tz_offset_hours", 0.0)

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_message_rate_data(
                conn,
                guild_id,
                days,
                tz,
            )

    return await cached_run_query(
        "message-rate",
        guild_id,
        {"days": days},
        _q,
    )


# ── Greeter response ────────────────────────────────────────────────────


@router.get("/greeter-response", response_model=GreeterResponseResponse)
async def greeter_response(
    request: Request,
    days: int | None = None,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot is not None else None

    greeter_channel_id = (
        getattr(ctx, "greeter_chat_channel_id", 0)
        or getattr(ctx, "welcome_channel_id", 0)
    )
    log_channel_id = (
        getattr(ctx, "join_leave_log_channel_id", 0)
        or getattr(ctx, "leave_channel_id", 0)
    )
    greeter_role_id = getattr(ctx, "greeter_role_id", 0)

    from datetime import datetime, timedelta, timezone

    cutoff_ts = 0.0
    if days is not None:
        cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()

    def _q():
        greeter_ids: set[int] = set()

        with ctx.open_db() as conn:
            # Resolve greeter IDs: live guild cache first, then DB fallback
            if guild and greeter_role_id:
                role = guild.get_role(greeter_role_id)
                if role:
                    greeter_ids = {m.id for m in role.members}

            if not greeter_ids and greeter_role_id:
                # Fallback: find the role name, then find users granted it
                role_name_row = conn.execute(
                    """
                    SELECT DISTINCT role_name FROM role_events
                    WHERE guild_id = ? AND role_name IN (
                        SELECT role_name FROM role_events
                        WHERE guild_id = ? AND action = 'grant'
                        GROUP BY role_name
                        HAVING role_name LIKE '%greet%' OR role_name LIKE '%welcome%'
                    )
                    LIMIT 1
                    """,
                    (guild_id, guild_id),
                ).fetchone()
                if role_name_row:
                    rows = conn.execute(
                        """
                        SELECT user_id FROM role_events
                        WHERE guild_id = ? AND role_name = ? AND action = 'grant'
                        """,
                        (guild_id, role_name_row[0]),
                    ).fetchall()
                    greeter_ids = {int(r[0]) for r in rows}

            # Broader fallback: frequent greeters in the configured greeter channel.
            # (at least 5 messages — filters out one-time joiners posting intros)
            if not greeter_ids and greeter_channel_id:
                rows = conn.execute(
                    """
                    SELECT author_id, COUNT(*) AS cnt FROM messages
                    WHERE guild_id = ? AND channel_id = ? AND ts >= ?
                    GROUP BY author_id HAVING cnt >= 5
                    """,
                    (guild_id, greeter_channel_id, cutoff_ts),
                ).fetchall()
                greeter_ids = {int(r[0]) for r in rows}

            if not greeter_ids or greeter_channel_id <= 0 or log_channel_id <= 0:
                return None

            sessions = reports_data.get_greeter_log_sessions(
                conn,
                guild_id,
                since_ts=cutoff_ts,
            )
            if not sessions:
                return None

            data = reports_data.get_greeter_response_data(
                conn,
                guild_id,
                greeter_channel_id,
                greeter_ids,
                sessions,
            )

        if days is not None:
            data["window_label"] = f"Last {days} Days"
        return data

    result = await cached_run_query(
        "greeter-response",
        guild_id,
        {"days": days},
        _q,
    )
    if result is None or result["total_joins"] == 0:
        raise HTTPException(
            status_code=404,
            detail="No greeter response data found for the selected period.",
        )

    _resolve_names(
        ctx,
        guild,
        result.get("entries", []),
        ("user_id", "user_name"),
        ("greeter_id", "greeter_name"),
    )
    return result


# ── Activity ────────────────────────────────────────────────────────────


@router.get("/activity", response_model=ActivityResponse)
async def activity(
    request: Request,
    resolution: Literal[
        "hour", "day", "week", "month", "hour_of_day", "day_of_week"
    ] = "day",
    mode: Literal["messages", "xp"] = "xp",
    user_id: str | None = None,
    channel_id: str | None = None,
    exclude_channel_ids: str | None = None,
    exclude_bots: bool = False,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    tz = getattr(ctx, "tz_offset_hours", 0.0)
    uid = int(user_id) if user_id else None
    cid = int(channel_id) if channel_id else None

    excluded_channels: set[int] = set()
    if exclude_channel_ids:
        for part in exclude_channel_ids.split(","):
            part = part.strip()
            if part:
                try:
                    excluded_channels.add(int(part))
                except ValueError:
                    continue

    excluded_users: set[int] = set()
    if exclude_bots:
        bot = getattr(ctx, "bot", None)
        guild = bot.get_guild(guild_id) if bot is not None else None
        if guild is not None:
            excluded_users.update(m.id for m in guild.members if m.bot)
        excluded_users.update(getattr(ctx, "recorded_bot_user_ids", set()))

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_activity_data(
                conn,
                guild_id,
                resolution,
                tz,
                mode=mode,
                user_id=uid,
                channel_id=cid,
                exclude_user_ids=excluded_users or None,
                exclude_channel_ids=excluded_channels or None,
            )

    return await cached_run_query(
        "activity",
        guild_id,
        {
            "resolution": resolution,
            "mode": mode,
            "user_id": user_id,
            "channel_id": channel_id,
            "exclude_channel_ids": ",".join(str(c) for c in sorted(excluded_channels)),
            "exclude_user_ids": ",".join(str(u) for u in sorted(excluded_users)),
        },
        _q,
    )


# ── Invite effectiveness ───────────────────────────────────────────────


@router.get("/invite-effectiveness", response_model=InviteEffectivenessResponse)
async def invite_effectiveness(
    request: Request,
    days: int | None = None,
    active_days: int = 30,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot is not None else None

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_invite_effectiveness_data(
                conn,
                guild_id,
                days=days,
                active_days=active_days,
            )

    result = await cached_run_query(
        "invite-effectiveness",
        guild_id,
        {"days": days, "active_days": active_days},
        _q,
    )
    _resolve_names(
        ctx, guild, result.get("inviters", []), ("inviter_id", "inviter_name")
    )
    all_invitees = [
        invitee
        for inviter in result.get("inviters", [])
        for invitee in inviter.get("invitees", [])
    ]
    _resolve_names(ctx, guild, all_invitees, ("invitee_id", "invitee_name"))
    return result


# ── Interaction graph ──────────────────────────────────────────────────


@router.get("/interaction-graph", response_model=InteractionGraphResponse)
async def interaction_graph(
    request: Request,
    days: int | None = None,
    limit: int = 50,
    include_metrics: int = 0,
    resolution: float = 1.2,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot is not None else None
    want_metrics = bool(include_metrics)
    resolution = max(0.3, min(3.0, float(resolution)))

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_interaction_graph_data(
                conn,
                guild_id,
                days=days,
                limit=min(limit, 100),
                include_metrics=want_metrics,
                clustering_resolution=resolution,
            )

    result = await cached_run_query(
        "interaction-graph",
        guild_id,
        {"days": days, "limit": limit, "metrics": want_metrics, "res": round(resolution, 2)},
        _q,
    )
    _resolve_names(ctx, guild, result.get("nodes", []), ("user_id", "user_name"))
    _resolve_names(
        ctx,
        guild,
        result.get("edges", []),
        ("from_id", "from_name"),
        ("to_id", "to_name"),
    )
    _resolve_names(
        ctx,
        guild,
        result.get("top_pairs", []),
        ("from_id", "from_name"),
        ("to_id", "to_name"),
    )
    metrics = result.get("metrics")
    if metrics and metrics.get("bridge_users"):
        _resolve_names(ctx, guild, metrics["bridge_users"], ("user_id", "user_name"))
    return result


# ── Member retention ───────────────────────────────────────────────────


@router.get("/retention", response_model=RetentionResponse)
async def retention(
    request: Request,
    period_days: int = 3,
    min_previous: int = 5,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot is not None else None

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_retention_data(
                conn,
                guild_id,
                period_days=period_days,
                min_previous=min_previous,
            )

    result = await cached_run_query(
        "retention",
        guild_id,
        {"period_days": period_days, "min_previous": min_previous},
        _q,
    )
    _resolve_names(ctx, guild, result.get("entries", []), ("user_id", "user_name"))
    return result


# ── Voice activity ─────────────────────────────────────────────────────


@router.get("/voice-activity", response_model=VoiceActivityResponse)
async def voice_activity(
    request: Request,
    days: int | None = None,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    tz = getattr(ctx, "tz_offset_hours", 0.0)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot is not None else None

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_voice_activity_data(
                conn,
                guild_id,
                days=days,
                utc_offset_hours=tz,
            )

    result = await cached_run_query(
        "voice-activity",
        guild_id,
        {"days": days},
        _q,
    )
    _resolve_names(ctx, guild, result.get("top_users", []), ("user_id", "user_name"))
    return result


# ── XP leaderboard ────────────────────────────────────────────────────


@router.get("/xp-leaderboard", response_model=XpLeaderboardResponse)
async def xp_leaderboard(
    request: Request,
    days: int | None = None,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot is not None else None

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_xp_leaderboard_data(conn, guild_id, days=days)

    result = await cached_run_query(
        "xp-leaderboard",
        guild_id,
        {"days": days},
        _q,
    )
    _resolve_names(ctx, guild, result.get("leaderboard", []), ("user_id", "user_name"))
    return result


# ── Reaction analytics ─────────────────────────────────────────────────


@router.get("/reaction-analytics", response_model=ReactionAnalyticsResponse)
async def reaction_analytics(
    request: Request,
    days: int | None = None,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot is not None else None

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_reaction_analytics_data(
                conn,
                guild_id,
                days=days,
            )

    result = await cached_run_query(
        "reaction-analytics",
        guild_id,
        {"days": days},
        _q,
    )
    _resolve_names(ctx, guild, result.get("top_givers", []), ("user_id", "user_name"))
    _resolve_names(
        ctx, guild, result.get("top_receivers", []), ("user_id", "user_name")
    )
    return result


# ── Message rate drops ─────────────────────────────────────────────────


@router.get("/message-rate-drops", response_model=MessageRateDropsResponse)
async def message_rate_drops(
    request: Request,
    period_days: int = 2,
    min_previous: int = 100,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot is not None else None

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_message_rate_drops_data(
                conn,
                guild_id,
                period_days=period_days,
                min_previous=min_previous,
            )

    result = await cached_run_query(
        "message-rate-drops",
        guild_id,
        {"period_days": period_days, "min_previous": min_previous},
        _q,
    )
    _resolve_names(ctx, guild, result.get("entries", []), ("user_id", "user_name"))
    return result


# ── Burst ranking ──────────────────────────────────────────────────────


@router.get("/burst-ranking", response_model=BurstRankingResponse)
async def burst_ranking(
    request: Request,
    min_sessions: int = 3,
    days: int | None = None,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot is not None else None

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_burst_ranking_data(
                conn,
                guild_id,
                min_sessions=min_sessions,
                days=days,
            )

    result = await cached_run_query(
        "burst-ranking",
        guild_id,
        {"min_sessions": min_sessions, "days": days},
        _q,
    )
    _resolve_names(ctx, guild, result.get("entries", []), ("user_id", "user_name"))
    return result


# ── Channel comparison ─────────────────────────────────────────────────


@router.get("/channel-comparison", response_model=ChannelComparisonResponse)
async def channel_comparison(
    request: Request,
    days: int = 1,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot is not None else None

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_channel_comparison_data(
                conn,
                guild_id,
                days=max(1, min(365, days)),
            )

    result = await cached_run_query(
        "channel-comparison",
        guild_id,
        {"days": days},
        _q,
    )
    # Resolve channel names: guild cache first, then known_channels DB
    if result.get("channels"):
        unresolved_ids: list[int] = []
        for ch_row in result["channels"]:
            if guild:
                channel = guild.get_channel(int(ch_row["channel_id"]))
                if channel:
                    ch_row["channel_name"] = channel.name
                    continue
            unresolved_ids.append(int(ch_row["channel_id"]))
        if unresolved_ids:
            with ctx.open_db() as conn:
                known = get_known_channels_bulk(conn, guild_id, unresolved_ids)
            for ch_row in result["channels"]:
                if not ch_row.get("channel_name"):
                    cid = int(ch_row["channel_id"])
                    if cid in known:
                        ch_row["channel_name"] = known[cid]
    return result


# ── Quality score ─────────────────────────────────────────────────────


class _FakeMember:
    """Lightweight stand-in for discord.Member used when the bot is offline."""

    __slots__ = ("id", "bot", "joined_at")

    id: int
    bot: bool
    joined_at: object

    def __init__(self, user_id: int, joined_at_ts: float | None):
        self.id = user_id
        self.bot = False
        if joined_at_ts is not None:
            from datetime import datetime as _dt
            from datetime import timezone as _tz

            self.joined_at = _dt.fromtimestamp(joined_at_ts, tz=_tz.utc)
        else:
            self.joined_at = None


@router.get("/quality-score", response_model=QualityScoreResponse)
async def quality_score(
    request: Request,
    days: int | None = None,
    min_active_days: int | None = None,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot is not None else None

    def _q():
        with ctx.open_db() as conn:
            if guild is not None:
                members = guild.members
            else:
                # Offline: build fake members from DB message authors
                rows = conn.execute(
                    """
                    SELECT DISTINCT author_id, MIN(ts) AS first_seen
                    FROM messages WHERE guild_id = ?
                    GROUP BY author_id
                    """,
                    (guild_id,),
                ).fetchall()
                members = [_FakeMember(int(r[0]), float(r[1])) for r in rows]

            scores = compute_quality_scores(
                conn,
                guild_id,
                members,  # type: ignore[arg-type]
                window_days=days,
                min_active_days=min_active_days,
            )
            entries = []
            for s in scores:
                entries.append(
                    {
                        "user_id": str(s.user_id),
                        "user_name": "",
                        "final_score": s.final_score,
                        "engagement_given": s.engagement_given,
                        "consistency_recency": s.consistency_recency,
                        "content_resonance": s.content_resonance,
                        "posting_activity": s.posting_activity,
                        "status": s.status,
                        "active_days": s.active_days,
                        "active_weeks": s.active_weeks,
                    }
                )
            scored = sum(1 for e in entries if e["status"] == "Active")
            return {"total_scored": scored, "entries": entries}

    result = await cached_run_query(
        "quality-score",
        guild_id,
        {"days": days, "min_active_days": min_active_days},
        _q,
        ttl=300,
    )
    _resolve_names(ctx, guild, result.get("entries", []), ("user_id", "user_name"))
    return result


# ── Time to level 5 ────────────────────────────────────────────────────


@router.get("/time-to-level-5", response_model=TimeToLevel5Response)
async def time_to_level_5(
    request: Request,
    days: int | None = None,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    import statistics
    from collections import Counter
    from datetime import datetime, timedelta, timezone

    from xp_system import get_time_to_level_details, xp_required_for_level

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    guild = ctx.bot.get_guild(guild_id) if ctx.bot else None

    since_ts: float | None = None
    if days is not None:
        since_ts = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()

    def _q():
        with ctx.open_db() as conn:
            details = get_time_to_level_details(
                conn, guild_id, 5, since_ts=since_ts
            )

        if not details:
            return {
                "window_label": f"Last {days} Days" if days else "All Time",
                "count": 0,
                "mean_days": 0.0,
                "median_days": 0.0,
                "stddev_days": 0.0,
                "mode_days": 0,
                "xp_required": xp_required_for_level(5),
                "histogram": [],
                "members": [],
            }

        durations = [d["seconds"] for d in details]
        days_list = [s / 86400.0 for s in durations]
        mean_d = statistics.mean(days_list)
        median_d = statistics.median(days_list)
        stddev_d = statistics.pstdev(days_list) if len(days_list) > 1 else 0.0

        day_ints = [int(d) for d in days_list]
        counts = Counter(day_ints)
        mode_d = counts.most_common(1)[0][0] if counts else 0

        max_day = max(day_ints)
        histogram = []
        for d in range(0, max_day + 1):
            histogram.append({"label": f"{d}d", "count": counts.get(d, 0)})

        members = [
            {
                "user_id": d["user_id"],
                "display_name": str(d["user_id"]),
                "first_at": datetime.fromtimestamp(
                    d["first_at"], tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M"),
                "reached_at": datetime.fromtimestamp(
                    d["reached_at"], tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M"),
                "days": round(d["seconds"] / 86400.0, 1),
            }
            for d in details
        ]

        return {
            "window_label": f"Last {days} Days" if days else "All Time",
            "count": len(durations),
            "mean_days": round(mean_d, 1),
            "median_days": round(median_d, 1),
            "stddev_days": round(stddev_d, 1),
            "mode_days": mode_d,
            "xp_required": xp_required_for_level(5),
            "histogram": histogram,
            "members": members,
        }

    result = await cached_run_query(
        "time-to-level-5",
        guild_id,
        {"days": days},
        _q,
        ttl=300,
    )

    _resolve_names(ctx, guild, result.get("members", []), ("user_id", "display_name"))

    return result


# ── Animated interaction heatmap ──────────────────────────────────────────


@router.get("/interaction-heatmap", response_model=AnimatedHeatmapResponse)
async def interaction_heatmap(
    request: Request,
    resolution: Literal["day", "week"] = "week",
    days: int = 90,
    top_n: int = 20,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot is not None else None

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_animated_heatmap_data(
                conn,
                guild_id,
                resolution=resolution,
                days=days,
                top_n=top_n,
            )

    result = await cached_run_query(
        "interaction-heatmap",
        guild_id,
        {"resolution": resolution, "days": days, "top_n": top_n},
        _q,
        ttl=300,
    )
    _resolve_names(ctx, guild, result.get("users", []), ("user_id", "user_name"))
    return result


# ── Dropoff ────────────────────────────────────────────────────────────


_PERIOD_SECONDS: dict[str, int] = {
    "hour": 3600,
    "day": 86400,
    "week": 7 * 86400,
    "month": 30 * 86400,
}


@router.get("/dropoff", response_model=DropoffResponse)
async def dropoff(
    request: Request,
    period: Literal["hour", "day", "week", "month"] = "week",
    channel_id: str | None = None,
    limit: int = 10,
    user_id: str | None = None,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Members who disengaged most over the last *period* vs the period before."""
    from services.activity_graphs import query_dropoff_profiles

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None

    period_secs = _PERIOD_SECONDS[period]
    ch_id_int: int | None = int(channel_id) if channel_id else None
    user_id_int: int | None = int(user_id) if user_id else None

    def _q():
        with ctx.open_db() as conn:
            profiles = query_dropoff_profiles(
                conn,
                guild_id,
                period_secs,
                channel_id=ch_id_int,
                limit=max(1, min(50, limit)),
                target_user_id=user_id_int,
            )
        return {
            "period_label": f"Last {period}",
            "entries": [
                {
                    "user_id": str(p.user_id),
                    "user_name": "",
                    "msgs_prev": p.msgs_prev,
                    "msgs_recent": p.msgs_recent,
                    "drop_pct": (
                        round((p.msgs_prev - p.msgs_recent) / p.msgs_prev * 100.0, 1)
                        if p.msgs_prev > 0
                        else 0.0
                    ),
                    "channels_recent": p.channels_recent,
                    "replies_recent": p.replies_recent,
                    "initiations_recent": p.initiations_recent,
                    "avg_msg_len_recent": round(p.avg_len_recent, 1),
                    "deep_convos_recent": p.deep_convos_recent,
                    "first_activity_day": p.first_activity_day,
                    "server_msgs_prev": p.server_msgs_prev,
                    "server_msgs_recent": p.server_msgs_recent,
                }
                for p in profiles
            ],
        }

    result = await cached_run_query(
        "dropoff",
        guild_id,
        {"period": period, "channel_id": channel_id, "limit": limit, "user_id": user_id},
        _q,
        ttl=300,
    )
    _resolve_names(ctx, guild, result.get("entries", []), ("user_id", "user_name"))
    return result


# ── Session burst (per-member) ─────────────────────────────────────────


@router.get("/session-burst", response_model=SessionBurstResponse)
async def session_burst(
    request: Request,
    user_id: str,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Per-member session burst — server activity in 2-min bins around their session starts."""
    from services.activity_graphs import (
        _BIN_MINUTES,
        _POST_WINDOW_MINUTES,
        _PRE_WINDOW_MINUTES,
        query_session_burst,
    )

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    user_id_int = int(user_id)

    def _q():
        with ctx.open_db() as conn:
            pre_sessions, post_sessions, overall_rate = query_session_burst(
                conn, guild_id, user_id_int
            )
        n = len(pre_sessions)
        if n == 0:
            return {
                "user_id": str(user_id_int),
                "user_name": "",
                "pre_bins": [],
                "post_bins": [],
                "sessions": 0,
                "pre_avg": 0.0,
                "post_avg": 0.0,
                "overall_rate": overall_rate,
                "pre_window_minutes": _PRE_WINDOW_MINUTES,
                "post_window_minutes": _POST_WINDOW_MINUTES,
                "bin_minutes": _BIN_MINUTES,
            }
        pre_bins = [sum(s[i] for s in pre_sessions) / n for i in range(len(pre_sessions[0]))]
        post_bins = [sum(s[i] for s in post_sessions) / n for i in range(len(post_sessions[0]))]
        pre_avg = sum(pre_bins) / len(pre_bins) if pre_bins else 0.0
        post_avg = sum(post_bins) / len(post_bins) if post_bins else 0.0
        return {
            "user_id": str(user_id_int),
            "user_name": "",
            "pre_bins": pre_bins,
            "post_bins": post_bins,
            "sessions": n,
            "pre_avg": round(pre_avg, 2),
            "post_avg": round(post_avg, 2),
            "overall_rate": round(overall_rate, 2),
            "pre_window_minutes": _PRE_WINDOW_MINUTES,
            "post_window_minutes": _POST_WINDOW_MINUTES,
            "bin_minutes": _BIN_MINUTES,
        }

    result = await run_query(_q)
    _resolve_names(ctx, guild, [result], ("user_id", "user_name"))
    return result


# ── XP level review (any level) ────────────────────────────────────────


@router.get("/xp-level-review", response_model=XpLevelReviewResponse)
async def xp_level_review(
    request: Request,
    level: int,
    days: int | None = None,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Generalized version of /time-to-level-5 — works for any level (2–100)."""
    import statistics
    from collections import Counter
    from datetime import datetime, timedelta, timezone

    from xp_system import get_time_to_level_details, xp_required_for_level

    if not (2 <= level <= 100):
        raise HTTPException(400, "level must be between 2 and 100")

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    guild = ctx.bot.get_guild(guild_id) if ctx.bot else None

    since_ts: float | None = None
    if days is not None:
        since_ts = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()

    def _q():
        with ctx.open_db() as conn:
            details = get_time_to_level_details(
                conn, guild_id, level, since_ts=since_ts
            )

        if not details:
            return {
                "level": level,
                "window_label": f"Last {days} Days" if days else "All Time",
                "count": 0,
                "mean_days": 0.0,
                "median_days": 0.0,
                "stddev_days": 0.0,
                "mode_days": 0,
                "xp_required": xp_required_for_level(level),
                "histogram": [],
                "members": [],
            }

        durations = [d["seconds"] for d in details]
        days_list = [s / 86400.0 for s in durations]
        mean_d = statistics.mean(days_list)
        median_d = statistics.median(days_list)
        stddev_d = statistics.pstdev(days_list) if len(days_list) > 1 else 0.0

        day_ints = [int(d) for d in days_list]
        counts = Counter(day_ints)
        mode_d = counts.most_common(1)[0][0] if counts else 0

        max_day = max(day_ints)
        histogram = [{"label": f"{d}d", "count": counts.get(d, 0)} for d in range(0, max_day + 1)]

        members = [
            {
                "user_id": str(d["user_id"]),
                "display_name": "",
                "days": round(d["seconds"] / 86400.0, 1),
            }
            for d in details
        ]

        return {
            "level": level,
            "window_label": f"Last {days} Days" if days else "All Time",
            "count": len(durations),
            "mean_days": round(mean_d, 1),
            "median_days": round(median_d, 1),
            "stddev_days": round(stddev_d, 1),
            "mode_days": mode_d,
            "xp_required": xp_required_for_level(level),
            "histogram": histogram,
            "members": members,
        }

    result = await cached_run_query(
        "xp-level-review",
        guild_id,
        {"level": level, "days": days},
        _q,
        ttl=300,
    )
    _resolve_names(ctx, guild, result.get("members", []), ("user_id", "display_name"))
    return result


# ── Chilling effect ────────────────────────────────────────────────────


@router.get("/chilling-effect", response_model=ChillingEffectResponse)
async def chilling_effect(
    request: Request,
    lookback_days: int = 30,
    channel_id: str | None = None,
    entry_gap_minutes: int = 60,
    window_minutes: int = 30,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Identify members whose arrival in a channel correlates with others going quiet."""
    from datetime import datetime, timezone

    from commands.drama_commands import _analyze_chilling_effect

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None

    cutoff_ts = int(datetime.now(timezone.utc).timestamp()) - lookback_days * 86400
    ch_id_int: int | None = int(channel_id) if channel_id else None

    def _q():
        with ctx.open_db() as conn:
            events, channel_count = _analyze_chilling_effect(
                conn,
                guild_id,
                cutoff_ts=cutoff_ts,
                entry_gap_seconds=entry_gap_minutes * 60,
                window_seconds=window_minutes * 60,
                channel_id=ch_id_int,
            )

        # Aggregate by entry user
        by_user: dict[int, dict] = {}
        for ch_id, ts, author_id, content, victims in events:
            bucket = by_user.setdefault(
                author_id,
                {"silence_count": 0, "total_victims": 0, "events": []},
            )
            bucket["silence_count"] += 1
            bucket["total_victims"] += len(victims)
            bucket["events"].append((ch_id, ts, author_id, content, victims))

        ranked = []
        for uid, info in sorted(
            by_user.items(),
            key=lambda kv: (kv[1]["total_victims"], kv[1]["silence_count"]),
            reverse=True,
        ):
            sample_events = []
            for ch_id, ts, author_id, content, victims in info["events"][:2]:
                sample_events.append(
                    {
                        "channel_id": str(ch_id),
                        "channel_name": "",
                        "entry_ts": ts,
                        "entry_user_id": str(author_id),
                        "entry_user_name": "",
                        "entry_preview": (content or "")[:90],
                        "victims": [
                            {
                                "user_id": str(v_uid),
                                "user_name": "",
                                "last_message_ts": v_ts,
                                "last_message_preview": (v_content or "")[:90],
                            }
                            for v_uid, v_ts, v_content in victims[:5]
                        ],
                    }
                )
            ranked.append(
                {
                    "user_id": str(uid),
                    "user_name": "",
                    "silence_count": info["silence_count"],
                    "total_victims": info["total_victims"],
                    "sample_events": sample_events,
                }
            )

        return {
            "lookback_days": lookback_days,
            "channel_id": channel_id,
            "channel_count": channel_count,
            "total_events": len(events),
            "ranked": ranked,
        }

    result = await cached_run_query(
        "chilling-effect",
        guild_id,
        {
            "lookback_days": lookback_days,
            "channel_id": channel_id,
            "entry_gap_minutes": entry_gap_minutes,
            "window_minutes": window_minutes,
        },
        _q,
        ttl=600,
    )

    # Resolve display names
    ranked = result.get("ranked", [])
    _resolve_names(ctx, guild, ranked, ("user_id", "user_name"))
    for entry in ranked:
        for ev in entry.get("sample_events", []):
            _resolve_names(ctx, guild, [ev], ("entry_user_id", "entry_user_name"))
            _resolve_names(ctx, guild, ev.get("victims", []), ("user_id", "user_name"))
            if guild:
                ch = guild.get_channel(int(ev["channel_id"]))
                if ch:
                    ev["channel_name"] = ch.name
    return result


# ── Member list reports ────────────────────────────────────────────────


def _activity_to_row(uid: int, display_name: str, activity, now_ts: float) -> dict:
    last_ts: float | None = activity.created_at if activity else None
    return {
        "user_id": str(uid),
        "display_name": display_name,
        "last_message_ts": last_ts,
        "last_message_channel_id": str(activity.channel_id) if activity else None,
        "days_since_last": (
            round((now_ts - last_ts) / 86400.0, 1) if last_ts else None
        ),
    }


@router.get("/list-role", response_model=ListRoleResponse)
async def list_role(
    request: Request,
    role_id: str,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    import time as _time

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is None:
        raise HTTPException(503, "Guild not available")

    role = guild.get_role(int(role_id))
    if role is None:
        raise HTTPException(404, "Role not found")

    members_sorted = sorted(role.members, key=lambda m: m.display_name.lower())
    member_ids = [m.id for m in members_sorted]

    def _q():
        with ctx.open_db() as conn:
            return ctx.get_member_last_activity_map(conn, guild_id, member_ids)

    activities = await run_query(_q)
    now_ts = _time.time()
    rows = [_activity_to_row(m.id, m.display_name, activities.get(m.id), now_ts) for m in members_sorted]

    return {
        "role_id": role_id,
        "role_name": role.name,
        "total": len(rows),
        "members": rows,
    }


@router.get("/inactive-role", response_model=InactiveRoleResponse)
async def inactive_role(
    request: Request,
    role_id: str,
    days: int = 7,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    import time as _time

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is None:
        raise HTTPException(503, "Guild not available")

    role = guild.get_role(int(role_id))
    if role is None:
        raise HTTPException(404, "Role not found")

    days = max(1, min(365, days))
    cutoff_ts = _time.time() - days * 86400
    members_sorted = sorted(role.members, key=lambda m: m.display_name.lower())
    member_ids = [m.id for m in members_sorted]

    def _q():
        with ctx.open_db() as conn:
            return ctx.get_member_last_activity_map(conn, guild_id, member_ids)

    activities = await run_query(_q)
    now_ts = _time.time()

    inactive = []
    for m in members_sorted:
        a = activities.get(m.id)
        if a is None or a.created_at < cutoff_ts:
            inactive.append(_activity_to_row(m.id, m.display_name, a, now_ts))

    return {
        "role_id": role_id,
        "role_name": role.name,
        "days": days,
        "total": len(member_ids),
        "inactive_count": len(inactive),
        "tracking_coverage": len(activities),
        "members": inactive,
    }


@router.get("/inactive", response_model=InactiveResponse)
async def inactive(
    request: Request,
    period_seconds: int = 7 * 86400,
    channel_id: str | None = None,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    import time as _time

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is None:
        raise HTTPException(503, "Guild not available")

    period_seconds = max(60, min(365 * 86400, period_seconds))
    cutoff_ts = _time.time() - period_seconds

    members_all = [m for m in guild.members if not m.bot]
    member_ids = [m.id for m in members_all]
    ch_id_int: int | None = int(channel_id) if channel_id else None

    def _q():
        from xp_system import MemberActivity

        with ctx.open_db() as conn:
            if ch_id_int is not None:
                act_map: dict[int, MemberActivity] = {}
                for uid in member_ids:
                    row = conn.execute(
                        """
                        SELECT channel_id, message_id, created_at
                        FROM xp_events
                        WHERE guild_id = ? AND user_id = ? AND channel_id = ?
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (guild_id, uid, ch_id_int),
                    ).fetchone()
                    if row:
                        act_map[uid] = MemberActivity(
                            user_id=uid,
                            channel_id=int(row[0]),
                            message_id=int(row[1] or 0),
                            created_at=float(row[2]),
                        )
                return act_map
            return ctx.get_member_last_activity_map(conn, guild_id, member_ids)

    activities = await run_query(_q)
    now_ts = _time.time()
    rows = []
    for m in members_all:
        a = activities.get(m.id)
        if a is None or a.created_at < cutoff_ts:
            rows.append(_activity_to_row(m.id, m.display_name, a, now_ts))
    rows.sort(key=lambda r: r["last_message_ts"] or 0)

    days = period_seconds / 86400.0
    label = f"{days:.0f}d" if days >= 1 else f"{period_seconds // 3600}h"

    return {
        "period_seconds": period_seconds,
        "period_label": label,
        "channel_id": channel_id,
        "total": len(rows),
        "members": rows,
    }


@router.get("/oldest-sfw", response_model=OldestSfwResponse)
async def oldest_sfw(
    request: Request,
    count: int = 10,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    import time as _time

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is None:
        raise HTTPException(503, "Guild not available")

    count = max(1, min(100, count))

    nsfw_cfg = ctx.grant_roles.get("nsfw") if hasattr(ctx, "grant_roles") else None
    nsfw_role_id = nsfw_cfg["role_id"] if nsfw_cfg else 0
    nsfw_role = guild.get_role(int(nsfw_role_id)) if nsfw_role_id else None

    sfw_members = [
        m for m in guild.members
        if not m.bot and (nsfw_role is None or nsfw_role not in m.roles)
    ]
    member_ids = [m.id for m in sfw_members]

    def _q():
        with ctx.open_db() as conn:
            return ctx.get_member_last_activity_map(conn, guild_id, member_ids)

    activities = await run_query(_q)
    now_ts = _time.time()

    sorted_members = sorted(
        sfw_members,
        key=lambda m: activities[m.id].created_at if m.id in activities else 0,
    )
    top = sorted_members[:count]
    rows = [_activity_to_row(m.id, m.display_name, activities.get(m.id), now_ts) for m in top]

    return {
        "nsfw_role_id": str(nsfw_role.id) if nsfw_role else None,
        "nsfw_role_name": nsfw_role.name if nsfw_role else "",
        "sfw_total": len(sfw_members),
        "members": rows,
    }
