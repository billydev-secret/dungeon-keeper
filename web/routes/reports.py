"""Report endpoints — one per chart/table report."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request

from services import reports_data
from services.member_quality_score import compute_quality_scores
from services.message_store import get_known_channels_bulk, get_known_users_bulk
from services.reports_data import MemberSnapshot
from web.auth import AuthenticatedUser
from web.deps import get_ctx, require_perms, run_query
from web.schemas import (
    ActivityResponse,
    BurstRankingResponse,
    ChannelComparisonResponse,
    GreeterResponseResponse,
    InteractionGraphResponse,
    InviteEffectivenessResponse,
    JoinTimesResponse,
    MessageCadenceResponse,
    MessageRateDropsResponse,
    MessageRateResponse,
    NsfwGenderResponse,
    QualityScoreResponse,
    ReactionAnalyticsResponse,
    RetentionResponse,
    RoleGrowthResponse,
    VoiceActivityResponse,
    XpLeaderboardResponse,
)

router = APIRouter()


def _resolve_names(ctx, guild, entries, *id_name_pairs):
    """Resolve user IDs to display names in a list of dicts.

    Each pair is (id_field, name_field). Tries the live guild cache first,
    then falls back to the known_users DB table.
    """
    if not entries:
        return

    # Collect all IDs that need resolving
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

    # DB fallback for any still-unresolved IDs
    if unresolved:
        with ctx.open_db() as conn:
            known = get_known_users_bulk(conn, ctx.guild_id, list(unresolved))
        for entry in entries:
            for id_field, name_field in id_name_pairs:
                if entry.get(name_field):
                    continue
                uid = entry.get(id_field)
                if uid and int(uid) in known:
                    entry[name_field] = known[int(uid)]


# ── Role growth ──────────────────────────────────────────────────────────

@router.get("/role-growth", response_model=RoleGrowthResponse)
async def role_growth(
    request: Request,
    resolution: Literal["day", "week", "month"] = "week",
    roles: str | None = None,
    _: AuthenticatedUser = Depends(require_perms({"manage_roles"})),
):
    ctx = get_ctx(request)
    tz = getattr(ctx, "tz_offset_hours", 0.0)
    role_filter: set[str] | None = None
    if roles is not None:
        role_filter = {r.strip().lower() for r in roles.split(",") if r.strip()}

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_role_growth_data(
                conn, ctx.guild_id, resolution, role_filter, utc_offset_hours=tz
            )

    return await run_query(_q)


# ── Message cadence ──────────────────────────────────────────────────────

@router.get("/message-cadence", response_model=MessageCadenceResponse)
async def message_cadence(
    request: Request,
    resolution: Literal["hour", "day", "week", "month", "hour_of_day", "day_of_week"] = "day",
    channel_id: str | None = None,
    _: AuthenticatedUser = Depends(require_perms({"manage_roles"})),
):
    ctx = get_ctx(request)
    ch_id = int(channel_id) if channel_id else None
    tz = getattr(ctx, "tz_offset_hours", 0.0)

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_message_cadence_data(
                conn, ctx.guild_id, resolution, tz, ch_id,
            )

    return await run_query(_q)


# ── Join times ───────────────────────────────────────────────────────────

@router.get("/join-times", response_model=JoinTimesResponse)
async def join_times(
    request: Request,
    resolution: Literal["hour_of_day", "day_of_week"] = "hour_of_day",
    _: AuthenticatedUser = Depends(require_perms({"manage_guild"})),
):
    ctx = get_ctx(request)
    tz = getattr(ctx, "tz_offset_hours", 0.0)

    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot is not None else None

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
                    (ctx.guild_id,),
                ).fetchall()
                if rows:
                    return [
                        MemberSnapshot(
                            user_id=int(r[0]), display_name=str(r[0]), is_bot=False,
                            joined_at=float(r[1]), role_ids=(),
                        )
                        for r in rows
                    ]
                # Fallback: first role grant per user as join proxy
                rows = conn.execute(
                    """SELECT user_id, MIN(granted_at) AS first_grant
                       FROM role_events
                       WHERE guild_id = ? AND action = 'grant'
                       GROUP BY user_id""",
                    (ctx.guild_id,),
                ).fetchall()
                return [
                    MemberSnapshot(
                        user_id=int(r[0]), display_name=str(r[0]), is_bot=False,
                        joined_at=float(r[1]), role_ids=(),
                    )
                    for r in rows
                ]
        members = await run_query(_load_members)

    def _q():
        return reports_data.get_join_times_data(members, resolution, tz)

    return await run_query(_q)


# ── NSFW gender activity ────────────────────────────────────────────────

@router.get("/nsfw-gender", response_model=NsfwGenderResponse)
async def nsfw_gender(
    request: Request,
    resolution: Literal["day", "week", "month"] = "week",
    media_only: bool = False,
    channel_id: str | None = None,
    _: AuthenticatedUser = Depends(require_perms({"manage_guild"})),
):
    ctx = get_ctx(request)
    tz = getattr(ctx, "tz_offset_hours", 0.0)

    if channel_id:
        target_ids = [int(channel_id)]
    else:
        # Auto-discover NSFW channels from live guild cache
        bot = getattr(ctx, "bot", None)
        guild = bot.get_guild(ctx.guild_id) if bot is not None else None
        if guild is not None:
            target_ids = [
                ch.id for ch in guild.channels if getattr(ch, "nsfw", False)
            ]
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
                        (ctx.guild_id,),
                    ).fetchall()
                    return [int(r[0]) for r in rows]
            target_ids = await run_query(_discover)

    if not target_ids:
        return NsfwGenderResponse(
            resolution=resolution, window_label="", media_only=media_only,
            labels=[], series=[],
        )

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_nsfw_gender_data(
                conn, ctx.guild_id, resolution, target_ids, tz, media_only,
            )

    return await run_query(_q)


# ── Message rate ─────────────────────────────────────────────────────────

@router.get("/message-rate", response_model=MessageRateResponse)
async def message_rate(
    request: Request,
    days: int = 7,
    _: AuthenticatedUser = Depends(require_perms({"manage_roles"})),
):
    ctx = get_ctx(request)
    days = max(1, min(365, days))
    tz = getattr(ctx, "tz_offset_hours", 0.0)

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_message_rate_data(
                conn, ctx.guild_id, days, tz,
            )

    return await run_query(_q)


# ── Greeter response ────────────────────────────────────────────────────

@router.get("/greeter-response", response_model=GreeterResponseResponse)
async def greeter_response(
    request: Request,
    days: int | None = None,
    _: AuthenticatedUser = Depends(require_perms({"manage_roles"})),
):
    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot is not None else None

    welcome_channel_id = getattr(ctx, "welcome_channel_id", 0)
    greeter_role_id = getattr(ctx, "greeter_role_id", 0)

    from datetime import datetime, timedelta, timezone

    cutoff_ts = 0.0
    if days is not None:
        cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()

    def _q():
        greeter_ids: set[int] = set()
        join_map: dict[int, float] = {}

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
                    (ctx.guild_id, ctx.guild_id),
                ).fetchone()
                if role_name_row:
                    rows = conn.execute(
                        """
                        SELECT user_id FROM role_events
                        WHERE guild_id = ? AND role_name = ? AND action = 'grant'
                        """,
                        (ctx.guild_id, role_name_row[0]),
                    ).fetchall()
                    greeter_ids = {int(r[0]) for r in rows}

            # Broader fallback: frequent posters in the welcome channel
            # (at least 5 messages — filters out one-time joiners posting intros)
            if not greeter_ids and welcome_channel_id:
                rows = conn.execute(
                    """
                    SELECT author_id, COUNT(*) AS cnt FROM messages
                    WHERE guild_id = ? AND channel_id = ?
                    GROUP BY author_id HAVING cnt >= 5
                    """,
                    (ctx.guild_id, welcome_channel_id),
                ).fetchall()
                greeter_ids = {int(r[0]) for r in rows}

            if not greeter_ids:
                return None

            # Join times: invite_edges first, then role_events first-grant fallback
            rows = conn.execute(
                "SELECT invitee_id, joined_at FROM invite_edges WHERE guild_id = ? AND joined_at >= ?",
                (ctx.guild_id, cutoff_ts),
            ).fetchall()
            for r in rows:
                join_map[int(r[0])] = float(r[1])

            if not join_map:
                # Fallback: first role grant per user as join proxy
                rows = conn.execute(
                    """SELECT user_id, MIN(granted_at) AS first_grant
                       FROM role_events
                       WHERE guild_id = ? AND action = 'grant' AND granted_at >= ?
                       GROUP BY user_id""",
                    (ctx.guild_id, cutoff_ts),
                ).fetchall()
                for r in rows:
                    join_map[int(r[0])] = float(r[1])

            # Supplement with live guild members if available
            if guild:
                for m in guild.members:
                    if m.bot or not m.joined_at:
                        continue
                    ts = m.joined_at.timestamp()
                    if ts >= cutoff_ts:
                        join_map[m.id] = ts

            if not join_map:
                return None

            data = reports_data.get_greeter_response_data(
                conn, ctx.guild_id, welcome_channel_id, greeter_ids, join_map,
            )

        if days is not None:
            data["window_label"] = f"Last {days} Days"
        return data

    result = await run_query(_q)
    if result is None or result["count"] == 0:
        raise HTTPException(status_code=404, detail="No greeter response data found for the selected period.")

    _resolve_names(ctx, guild, result.get("entries", []),
                   ("user_id", "user_name"), ("greeter_id", "greeter_name"))
    return result


# ── Activity ────────────────────────────────────────────────────────────

@router.get("/activity", response_model=ActivityResponse)
async def activity(
    request: Request,
    resolution: Literal["hour", "day", "week", "month", "hour_of_day", "day_of_week"] = "day",
    mode: Literal["messages", "xp"] = "messages",
    user_id: str | None = None,
    channel_id: str | None = None,
    _: AuthenticatedUser = Depends(require_perms({"manage_guild"})),
):
    ctx = get_ctx(request)
    tz = getattr(ctx, "tz_offset_hours", 0.0)
    uid = int(user_id) if user_id else None
    cid = int(channel_id) if channel_id else None

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_activity_data(
                conn, ctx.guild_id, resolution, tz,
                mode=mode, user_id=uid, channel_id=cid,
            )

    return await run_query(_q)



# ── Invite effectiveness ───────────────────────────────────────────────

@router.get("/invite-effectiveness", response_model=InviteEffectivenessResponse)
async def invite_effectiveness(
    request: Request,
    days: int | None = None,
    active_days: int = 30,
    _: AuthenticatedUser = Depends(require_perms({"manage_guild"})),
):
    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot is not None else None

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_invite_effectiveness_data(
                conn, ctx.guild_id, days=days, active_days=active_days,
            )

    result = await run_query(_q)
    _resolve_names(ctx, guild, result.get("inviters", []),
                   ("inviter_id", "inviter_name"))
    return result


# ── Interaction graph ──────────────────────────────────────────────────

@router.get("/interaction-graph", response_model=InteractionGraphResponse)
async def interaction_graph(
    request: Request,
    days: int | None = None,
    limit: int = 50,
    _: AuthenticatedUser = Depends(require_perms({"manage_guild"})),
):
    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot is not None else None

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_interaction_graph_data(
                conn, ctx.guild_id, days=days, limit=min(limit, 100),
            )

    result = await run_query(_q)
    _resolve_names(ctx, guild, result.get("nodes", []),
                   ("user_id", "user_name"))
    _resolve_names(ctx, guild, result.get("edges", []),
                   ("from_id", "from_name"), ("to_id", "to_name"))
    _resolve_names(ctx, guild, result.get("top_pairs", []),
                   ("from_id", "from_name"), ("to_id", "to_name"))
    return result


# ── Member retention ───────────────────────────────────────────────────

@router.get("/retention", response_model=RetentionResponse)
async def retention(
    request: Request,
    period_days: int = 30,
    min_previous: int = 5,
    _: AuthenticatedUser = Depends(require_perms({"manage_guild"})),
):
    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot is not None else None

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_retention_data(
                conn, ctx.guild_id, period_days=period_days,
                min_previous=min_previous,
            )

    result = await run_query(_q)
    _resolve_names(ctx, guild, result.get("entries", []),
                   ("user_id", "user_name"))
    return result


# ── Voice activity ─────────────────────────────────────────────────────

@router.get("/voice-activity", response_model=VoiceActivityResponse)
async def voice_activity(
    request: Request,
    days: int | None = None,
    _: AuthenticatedUser = Depends(require_perms({"manage_guild"})),
):
    ctx = get_ctx(request)
    tz = getattr(ctx, "tz_offset_hours", 0.0)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot is not None else None

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_voice_activity_data(
                conn, ctx.guild_id, days=days, utc_offset_hours=tz,
            )

    result = await run_query(_q)
    _resolve_names(ctx, guild, result.get("top_users", []),
                   ("user_id", "user_name"))
    return result


# ── XP leaderboard ────────────────────────────────────────────────────

@router.get("/xp-leaderboard", response_model=XpLeaderboardResponse)
async def xp_leaderboard(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"manage_guild"})),
):
    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot is not None else None

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_xp_leaderboard_data(conn, ctx.guild_id)

    result = await run_query(_q)
    _resolve_names(ctx, guild, result.get("leaderboard", []),
                   ("user_id", "user_name"))
    return result


# ── Reaction analytics ─────────────────────────────────────────────────

@router.get("/reaction-analytics", response_model=ReactionAnalyticsResponse)
async def reaction_analytics(
    request: Request,
    days: int | None = None,
    _: AuthenticatedUser = Depends(require_perms({"manage_guild"})),
):
    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot is not None else None

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_reaction_analytics_data(
                conn, ctx.guild_id, days=days,
            )

    result = await run_query(_q)
    _resolve_names(ctx, guild, result.get("top_givers", []),
                   ("user_id", "user_name"))
    _resolve_names(ctx, guild, result.get("top_receivers", []),
                   ("user_id", "user_name"))
    return result


# ── Message rate drops ─────────────────────────────────────────────────

@router.get("/message-rate-drops", response_model=MessageRateDropsResponse)
async def message_rate_drops(
    request: Request,
    period_days: int = 14,
    min_previous: int = 5,
    _: AuthenticatedUser = Depends(require_perms({"manage_guild"})),
):
    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot is not None else None

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_message_rate_drops_data(
                conn, ctx.guild_id, period_days=period_days,
                min_previous=min_previous,
            )

    result = await run_query(_q)
    _resolve_names(ctx, guild, result.get("entries", []),
                   ("user_id", "user_name"))
    return result


# ── Burst ranking ──────────────────────────────────────────────────────

@router.get("/burst-ranking", response_model=BurstRankingResponse)
async def burst_ranking(
    request: Request,
    min_sessions: int = 3,
    days: int | None = None,
    _: AuthenticatedUser = Depends(require_perms({"manage_guild"})),
):
    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot is not None else None

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_burst_ranking_data(
                conn, ctx.guild_id, min_sessions=min_sessions, days=days,
            )

    result = await run_query(_q)
    _resolve_names(ctx, guild, result.get("entries", []),
                   ("user_id", "user_name"))
    return result


# ── Channel comparison ─────────────────────────────────────────────────

@router.get("/channel-comparison", response_model=ChannelComparisonResponse)
async def channel_comparison(
    request: Request,
    days: int = 30,
    _: AuthenticatedUser = Depends(require_perms({"manage_guild"})),
):
    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot is not None else None

    def _q():
        with ctx.open_db() as conn:
            return reports_data.get_channel_comparison_data(
                conn, ctx.guild_id, days=max(1, min(365, days)),
            )

    result = await run_query(_q)
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
                known = get_known_channels_bulk(conn, ctx.guild_id, unresolved_ids)
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
    joined_at: "object"

    def __init__(self, user_id: int, joined_at_ts: float | None):
        self.id = user_id
        self.bot = False
        if joined_at_ts is not None:
            from datetime import datetime as _dt, timezone as _tz
            self.joined_at = _dt.fromtimestamp(joined_at_ts, tz=_tz.utc)
        else:
            self.joined_at = None


@router.get("/quality-score", response_model=QualityScoreResponse)
async def quality_score(
    request: Request,
    days: int | None = None,
    min_active_days: int | None = None,
    _: AuthenticatedUser = Depends(require_perms({"manage_guild"})),
):
    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot is not None else None

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
                    (ctx.guild_id,),
                ).fetchall()
                members = [_FakeMember(int(r[0]), float(r[1])) for r in rows]

            scores = compute_quality_scores(
                conn, ctx.guild_id, members,
                window_days=days, min_active_days=min_active_days,
            )
            entries = []
            for s in scores:
                entries.append({
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
                })
            scored = sum(1 for e in entries if e["status"] == "Active")
            return {"total_scored": scored, "entries": entries}

    result = await run_query(_q)
    _resolve_names(ctx, guild, result.get("entries", []),
                   ("user_id", "user_name"))
    return result
