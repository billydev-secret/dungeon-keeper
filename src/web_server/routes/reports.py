"""Report endpoints — one per chart/table report."""

from __future__ import annotations

from typing import Literal

from discord import app_commands
from fastapi import APIRouter, Depends, HTTPException, Request

from bot_modules.core.bot_exclusion import bot_filter_clause, bot_ids_subquery
from bot_modules.core.db_utils import get_tz_offset_hours
from bot_modules.services import reports_data
from bot_modules.services import usage_telemetry_service as usage_telemetry
from bot_modules.services.member_quality_score import (
    MemberStandIn,
    build_quality_report,
)
from bot_modules.services.message_store import get_known_channels_bulk
from bot_modules.services.reports_data import MemberSnapshot
from web_server.auth import AuthenticatedUser
from web_server.deps import (
    cached_run_query,
    get_active_guild_id,
    get_ctx,
    invalidate_report_cache,
    require_perms,
    run_query,
)
from web_server.helpers import resolve_names as _resolve_names
from web_server.schemas import (
    ActivityResponse,
    ChannelComparisonResponse,
    GrantAuditResponse,
    GreeterResponseResponse,
    InactiveReportResponse,
    IntakeReportResponse,
    InteractionGraphResponse,
    InviteEffectivenessResponse,
    JoinTimesResponse,
    NsfwGenderResponse,
    OneSidedAttentionResponse,
    QualityScoreResponse,
    RetentionResponse,
    TimeToLevel5Response,
    UsageReportResponse,
    VoiceActivityResponse,
    XpLeaderboardResponse,
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


# ── Join times ───────────────────────────────────────────────────────────


@router.get("/join-times", response_model=JoinTimesResponse)
async def join_times(
    request: Request,
    resolution: Literal["hour_of_day", "day_of_week"] = "hour_of_day",
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _get_tz():
        with ctx.open_db() as conn:
            return get_tz_offset_hours(conn, guild_id)

    tz = await run_query(_get_tz)

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
    include_bots: bool = False,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

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
            tz = get_tz_offset_hours(conn, guild_id)
            return reports_data.get_nsfw_gender_data(
                conn,
                guild_id,
                resolution,
                target_ids,
                tz,
                media_only,
                include_bots=include_bots,
            )

    return await cached_run_query(
        "nsfw-gender",
        guild_id,
        {
            "resolution": resolution,
            "media_only": media_only,
            "channel_id": channel_id,
            "include_bots": include_bots,
        },
        _q,
    )


# ── Intake report ────────────────────────────────────────────────────────


@router.get("/intake-report", response_model=IntakeReportResponse)
async def intake_report(
    request: Request,
    days: int = 30,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    """Intake-card analytics: the open queue, outcomes, per-welcomer counts,
    and which steps get skipped (the procedure's own feedback)."""
    import time as _time

    from bot_modules.services import intake_service as intake_svc

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    days = max(1, min(365, days))
    since_ts = _time.time() - days * 86400.0

    def _q():
        with ctx.open_db() as conn:
            outcomes = intake_svc.report_outcomes(conn, guild_id, since_ts)
            return {
                "enabled": intake_svc.is_enabled(conn, guild_id),
                "window_label": f"Last {days} days",
                "open_cards": [
                    {**c, "user_id": str(c["user_id"])}
                    for c in intake_svc.report_open_cards(conn, guild_id)
                ],
                "resolved": outcomes["resolved"],
                "counts": outcomes["counts"],
                "mean_seconds": outcomes["mean_seconds"],
                "median_seconds": outcomes["median_seconds"],
                "welcomers": [
                    {**w, "user_id": str(w["user_id"])}
                    for w in intake_svc.report_welcomers(conn, guild_id, since_ts)
                ],
                "skipped_steps": intake_svc.report_skipped_steps(
                    conn, guild_id, since_ts
                ),
            }

    return await cached_run_query("intake-report", guild_id, {"days": days}, _q)


# ── Greeter response ────────────────────────────────────────────────────


@router.get("/greeter-response", response_model=GreeterResponseResponse)
async def greeter_response(
    request: Request,
    days: int | None = None,
    include_bots: bool = False,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot is not None else None

    cfg = ctx.guild_config(guild_id)
    greeter_channel_id = cfg.greeter_chat_channel_id or cfg.welcome_channel_id
    log_channel_id = cfg.join_leave_log_channel_id or cfg.leave_channel_id
    greeter_role_id = cfg.greeter_role_id

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
                g_bot_clause, g_bot_params = bot_filter_clause(
                    guild_id, include_bots=include_bots
                )
                rows = conn.execute(
                    f"""
                    SELECT author_id, COUNT(*) AS cnt FROM messages
                    WHERE guild_id = ? AND channel_id = ? AND ts >= ?{g_bot_clause}
                    GROUP BY author_id HAVING cnt >= 5
                    """,
                    (guild_id, greeter_channel_id, cutoff_ts, *g_bot_params),
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
                include_bots=include_bots,
            )

        if days is not None:
            data["window_label"] = f"Last {days} Days"
        return data

    result = await cached_run_query(
        "greeter-response",
        guild_id,
        {"days": days, "include_bots": include_bots},
        _q,
    )
    if result is None or result["total_joins"] == 0:
        raise HTTPException(
            status_code=404,
            detail="No greeter response data found for the selected period.",
        )

    await _resolve_names(
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
    include_bots: bool = False,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
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

    def _q():
        with ctx.open_db() as conn:
            tz = get_tz_offset_hours(conn, guild_id)
            # Bots are excluded by default. This used to scan live
            # ``guild.members`` for ``.bot`` plus a guild_config allowlist, which
            # missed bots that had left the server; known_users retains them, so
            # it is now the only source consulted. Resolved inside the worker
            # thread so the route never opens a DB connection on the event loop.
            excluded_users: set[int] = set()
            if not include_bots:
                excluded_users = {
                    r[0]
                    for r in conn.execute(bot_ids_subquery(), (guild_id,)).fetchall()
                }
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
            "include_bots": include_bots,
        },
        _q,
    )


# ── Invite effectiveness ───────────────────────────────────────────────


@router.get("/invite-effectiveness", response_model=InviteEffectivenessResponse)
async def invite_effectiveness(
    request: Request,
    days: int | None = None,
    active_days: int = 30,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
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
    await _resolve_names(
        ctx, guild, result.get("inviters", []), ("inviter_id", "inviter_name")
    )
    all_invitees = [
        invitee
        for inviter in result.get("inviters", [])
        for invitee in inviter.get("invitees", [])
    ]
    await _resolve_names(ctx, guild, all_invitees, ("invitee_id", "invitee_name"))
    return result


# ── Interaction graph ──────────────────────────────────────────────────


@router.get("/interaction-graph", response_model=InteractionGraphResponse)
async def interaction_graph(
    request: Request,
    days: int | None = None,
    limit: int = 50,
    include_metrics: int = 0,
    resolution: float = 1.2,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
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
    await _resolve_names(ctx, guild, result.get("nodes", []), ("user_id", "user_name"))
    await _resolve_names(
        ctx,
        guild,
        result.get("edges", []),
        ("from_id", "from_name"),
        ("to_id", "to_name"),
    )
    await _resolve_names(
        ctx,
        guild,
        result.get("top_pairs", []),
        ("from_id", "from_name"),
        ("to_id", "to_name"),
    )
    metrics = result.get("metrics")
    if metrics and metrics.get("bridge_users"):
        await _resolve_names(ctx, guild, metrics["bridge_users"], ("user_id", "user_name"))
    return result


# ── One-sided attention (moderator review) ─────────────────────────────


@router.get("/one-sided-attention", response_model=OneSidedAttentionResponse)
async def one_sided_attention(
    request: Request,
    window_days: int = 30,
    limit: int = 50,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot is not None else None
    window_days = max(7, min(int(window_days), 180))

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_one_sided_attention_data(
                conn, guild_id, window_days=window_days, limit=min(limit, 100)
            )

    result = await cached_run_query(
        "one-sided-attention",
        guild_id,
        {"window_days": window_days, "limit": limit},
        _q,
    )
    await _resolve_names(
        ctx,
        guild,
        result.get("candidates", []),
        ("from_id", "from_name"),
        ("to_id", "to_name"),
    )
    return result


# ── Member retention ───────────────────────────────────────────────────


@router.get("/retention", response_model=RetentionResponse)
async def retention(
    request: Request,
    period_days: int = 3,
    min_previous: int = 5,
    include_bots: bool = False,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
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
                include_bots=include_bots,
            )

    result = await cached_run_query(
        "retention",
        guild_id,
        {
            "period_days": period_days,
            "min_previous": min_previous,
            "include_bots": include_bots,
        },
        _q,
    )
    await _resolve_names(ctx, guild, result.get("entries", []), ("user_id", "user_name"))
    return result


# ── Voice activity ─────────────────────────────────────────────────────


@router.get("/voice-activity", response_model=VoiceActivityResponse)
async def voice_activity(
    request: Request,
    days: int | None = None,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot is not None else None

    def _q():
        with ctx.open_db() as conn:
            tz = get_tz_offset_hours(conn, guild_id)
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
    await _resolve_names(ctx, guild, result.get("top_users", []), ("user_id", "user_name"))
    return result


# ── XP leaderboard ────────────────────────────────────────────────────


@router.get("/xp-leaderboard", response_model=XpLeaderboardResponse)
async def xp_leaderboard(
    request: Request,
    days: int | None = None,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
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
    await _resolve_names(ctx, guild, result.get("leaderboard", []), ("user_id", "user_name"))
    return result


# ── Reaction analytics ─────────────────────────────────────────────────


@router.get("/channel-comparison", response_model=ChannelComparisonResponse)
async def channel_comparison(
    request: Request,
    days: int = 1,
    include_bots: bool = False,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
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
                include_bots=include_bots,
            )

    result = await cached_run_query(
        "channel-comparison",
        guild_id,
        {"days": days, "include_bots": include_bots},
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
            def _fetch_known():
                with ctx.open_db() as conn:
                    return get_known_channels_bulk(conn, guild_id, unresolved_ids)

            known = await run_query(_fetch_known)
            for ch_row in result["channels"]:
                if not ch_row.get("channel_name"):
                    cid = int(ch_row["channel_id"])
                    if cid in known:
                        ch_row["channel_name"] = known[cid]
    return result


# ── Quality score ─────────────────────────────────────────────────────


@router.get("/quality-score", response_model=QualityScoreResponse)
async def quality_score(
    request: Request,
    days: int | None = None,
    min_active_days: int | None = None,
    include_bots: bool = False,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot is not None else None

    def _q():
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        with ctx.open_db() as conn:
            if guild is not None:
                members = guild.members
            else:
                # Offline: build stand-in members from DB message authors
                q_bot_clause, q_bot_params = bot_filter_clause(
                    guild_id, include_bots=include_bots
                )
                rows = conn.execute(
                    f"""
                    SELECT DISTINCT author_id, MIN(ts) AS first_seen
                    FROM messages WHERE guild_id = ?{q_bot_clause}
                    GROUP BY author_id
                    """,
                    (guild_id, *q_bot_params),
                ).fetchall()
                members = [
                    MemberStandIn(
                        int(r[0]), False, _dt.fromtimestamp(float(r[1]), tz=_tz.utc)
                    )
                    for r in rows
                ]

            return build_quality_report(
                conn,
                guild_id,
                members,  # type: ignore[arg-type]
                window_days=days,
                min_active_days=min_active_days,
                include_bots=include_bots,
            )

    result = await cached_run_query(
        "quality-score",
        guild_id,
        {
            "days": days,
            "min_active_days": min_active_days,
            "include_bots": include_bots,
        },
        _q,
        ttl=300,
    )
    await _resolve_names(ctx, guild, result.get("entries", []), ("user_id", "user_name"))
    return result


# ── Time to level 5 ────────────────────────────────────────────────────


@router.get("/time-to-level-5", response_model=TimeToLevel5Response)
async def time_to_level_5(
    request: Request,
    days: int | None = None,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    import statistics
    from collections import Counter
    from datetime import datetime, timedelta, timezone

    from bot_modules.core.xp_system import get_time_to_level_details, xp_required_for_level

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
                # Left blank on purpose: _resolve_names only fills a name field
                # that is falsy, so seeding the id here would defeat both its
                # known_users lookup and its "User <id>" fallback, and members
                # who have left the cache would render as raw snowflakes.
                "display_name": "",
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

    await _resolve_names(ctx, guild, result.get("members", []), ("user_id", "display_name"))

    return result


# ── Inactive report (merged member lists) ─────────────────────────────────


@router.get("/inactive-report", response_model=InactiveReportResponse)
async def inactive_report(
    request: Request,
    days: int = 7,
    role_id: str | None = None,
    role_mode: Literal["with", "without"] = "with",
    channel_id: str | None = None,
    limit: int = 500,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    """One member list over last-activity data: everyone / role holders /
    role non-holders, idle at least *days* (0 = list the whole scope),
    oldest activity first."""
    import time as _time

    from bot_modules.core.xp_system import get_member_last_activity_map
    from bot_modules.services.inactive_report_service import (
        MemberScope,
        build_inactive_report,
        channel_activity_map,
        scope_members,
    )

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is None:
        raise HTTPException(503, "Guild not available")

    role = None
    if role_id:
        role = guild.get_role(int(role_id))
        if role is None:
            raise HTTPException(404, "Role not found")

    days = max(0, min(365, days))
    limit = max(1, min(2000, limit))
    ch_id_int: int | None = int(channel_id) if channel_id else None

    members = [
        MemberScope(
            user_id=m.id,
            display_name=m.display_name,
            is_bot=m.bot,
            role_ids=tuple(r.id for r in m.roles),
        )
        for m in guild.members
    ]
    scoped = scope_members(
        members, role_id=role.id if role else None, role_mode=role_mode
    )
    member_ids = [m.user_id for m in scoped]

    def _q():
        with ctx.open_db() as conn:
            if ch_id_int is not None:
                return channel_activity_map(conn, guild_id, member_ids, ch_id_int)
            return get_member_last_activity_map(conn, guild_id, member_ids)

    activities = await run_query(_q)
    report = build_inactive_report(
        scoped, activities, now_ts=_time.time(), days=days, limit=limit
    )

    return {
        "days": days,
        "role_id": role_id,
        "role_name": role.name if role else None,
        "role_mode": role_mode,
        "channel_id": channel_id,
        **report,
    }


@router.get("/grant-audit", response_model=GrantAuditResponse)
async def grant_audit(
    request: Request,
    grant_name: str = "nsfw",
    min_level: int = 5,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    """Three-bucket audit of members missing a grant role, backed by the
    role_prune_events ledger: waiting for their first grant, stripped by the
    inactivity prune but active again, and recently stripped + still inactive."""
    import time as _time

    from bot_modules.services.role_grant_audit_service import (
        gather_grant_audit,
        resolve_grant_audit_buckets,
    )

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is None:
        raise HTTPException(503, "Guild not available")

    cfg = ctx.guild_config(guild_id).grant_roles.get(grant_name)
    if cfg is None or int(cfg["role_id"]) <= 0:
        raise HTTPException(404, "Grant role not configured")
    role = guild.get_role(int(cfg["role_id"]))
    if role is None:
        raise HTTPException(404, "Configured role no longer exists")

    min_level = max(1, min_level)
    role_id = role.id
    role_name = role.name

    def _q():
        with ctx.open_db() as conn:
            return gather_grant_audit(conn, guild_id, role_id, min_level, role_name)

    gathered = await run_query(_q)
    snap = resolve_grant_audit_buckets(guild, role, gathered, min_level, _time.time())

    def _rows(bucket: list[dict]) -> list[dict]:
        return [{**r, "user_id": str(r["user_id"])} for r in bucket]

    return {
        "grant_name": grant_name,
        "label": cfg["label"],
        "role_id": str(role_id),
        "min_level": min_level,
        "inactivity_days": snap.inactivity_days,
        "waiting_first_grant": _rows(snap.waiting),
        "stripped_returned": _rows(snap.returned),
        "recent_inactive": _rows(snap.inactive),
    }




# ── Usage telemetry ──────────────────────────────────────────────────────


def _registered_command_names(ctx) -> set[str]:
    """Every *invocable* slash command the running bot has registered.

    Walks the tree so subcommands come back space-joined as ``"quest board"``,
    matching what the cog records.

    ``walk_commands()`` yields Groups as well as Commands, and a Group (``/quest``
    on its own) can never be invoked — including them would park every command
    group permanently in the never-run list, which is exactly the list that has
    to stay trustworthy. So Groups are filtered out.

    Returns an empty set in standalone mode (no bot attached), which makes the
    never-used list empty rather than claiming every command is unused.
    """
    bot = getattr(ctx, "bot", None)
    tree = getattr(bot, "tree", None) if bot is not None else None
    if tree is None:
        return set()
    try:
        return {
            c.qualified_name
            for c in tree.walk_commands()
            if isinstance(c, app_commands.Command)
        }
    except Exception:
        return set()


@router.get("/usage", response_model=UsageReportResponse)
async def usage_report(
    request: Request,
    days: int = 30,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Slash-command and dashboard-panel usage.

    Never-opened panels are *not* computed here. The dashboard nav lives in
    ``app.js``, so the browser is the only source of truth for "every panel
    that exists" — and that list is ~139 ids / 2.4 KB and grows with every
    panel added, which is more than belongs in a query string behind a proxy.
    Instead this returns ``seen_panels`` (the distinct names actually
    recorded, a much smaller set) and the client subtracts it from its own
    list. Commands are different: the bot's own command tree is server-side,
    so ``unused_commands`` is computed here.
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    days = max(1, min(int(days), 365))

    def _q():
        with ctx.open_db() as conn:
            tz = get_tz_offset_hours(conn, guild_id)
            return {
                "tz": tz,
                "totals": usage_telemetry.totals(conn, guild_id, days=days),
                "commands": usage_telemetry.name_usage(
                    conn, guild_id, usage_telemetry.KIND_COMMAND, days=days
                ),
                "panels": usage_telemetry.name_usage(
                    conn, guild_id, usage_telemetry.KIND_PANEL, days=days
                ),
                # Never-used is judged against ALL history, not the window —
                # a command last run 90 days ago is "unpopular", not "unused",
                # and calling it deletable would be wrong.
                "seen_commands": usage_telemetry.used_names(
                    conn, guild_id, usage_telemetry.KIND_COMMAND, days=0
                ),
                "seen_panels": usage_telemetry.used_names(
                    conn, guild_id, usage_telemetry.KIND_PANEL, days=0
                ),
                "top_users": usage_telemetry.user_usage(
                    conn, guild_id, usage_telemetry.KIND_COMMAND, days=days, limit=25
                ),
                "dashboard_users": usage_telemetry.user_usage(
                    conn, guild_id, usage_telemetry.KIND_PANEL, days=days, limit=25
                ),
                "daily_commands": usage_telemetry.daily_series(
                    conn, guild_id, usage_telemetry.KIND_COMMAND, days=days,
                    tz_offset_hours=tz,
                ),
                "daily_panels": usage_telemetry.daily_series(
                    conn, guild_id, usage_telemetry.KIND_PANEL, days=days,
                    tz_offset_hours=tz,
                ),
                "hours": usage_telemetry.hour_histogram(
                    conn, guild_id, usage_telemetry.KIND_COMMAND, days=days,
                    tz_offset_hours=tz,
                ),
            }

    data = await run_query(_q)

    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot is not None else None

    def _names(rows):
        return [
            {
                "name": r.name,
                "uses": r.uses,
                "users": r.users,
                "errors": r.errors,
                "last_ts": r.last_ts,
            }
            for r in rows
        ]

    def _users(rows):
        return [
            {
                # str() — snowflakes must not cross the wire as JSON numbers.
                "user_id": str(r.user_id),
                "name": "",
                "uses": r.uses,
                "distinct_names": r.distinct_names,
                "last_ts": r.last_ts,
            }
            for r in rows
        ]

    top_users = _users(data["top_users"])
    dashboard_users = _users(data["dashboard_users"])
    await _resolve_names(ctx, guild, top_users, ("user_id", "name"))
    await _resolve_names(ctx, guild, dashboard_users, ("user_id", "name"))

    return {
        "days": days,
        "totals": data["totals"],
        "commands": _names(data["commands"]),
        "panels": _names(data["panels"]),
        "unused_commands": usage_telemetry.unused_names(
            _registered_command_names(ctx), data["seen_commands"]
        ),
        # The client subtracts this from its own nav list — see the docstring.
        "seen_panels": sorted(data["seen_panels"]),
        "top_users": top_users,
        "dashboard_users": dashboard_users,
        "daily_commands": [
            {"day": d.day, "count": d.total} for d in data["daily_commands"]
        ],
        "daily_panels": [
            {"day": d.day, "count": d.total} for d in data["daily_panels"]
        ],
        "hours": data["hours"],
    }
