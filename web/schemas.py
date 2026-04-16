"""Pydantic response models for the dashboard API."""

from __future__ import annotations

from pydantic import BaseModel


class GuildInfo(BaseModel):
    id: str
    name: str
    icon: str | None = None


class MeResponse(BaseModel):
    user_id: str
    username: str
    perms: list[str]
    role_ids: list[str] = []
    role_names: list[str] = []
    guild_id: str
    guild_name: str | None = None
    guilds: list[GuildInfo] = []
    primary_guild_id: str | None = None
    avatar_url: str | None = None
    status: str | None = None


class RoleMeta(BaseModel):
    id: str
    name: str
    color: str
    member_count: int
    position: int
    managed: bool


class MemberMeta(BaseModel):
    id: str
    name: str
    display_name: str
    left_server: bool = False


class ChannelMeta(BaseModel):
    id: str
    name: str
    type: str
    category: str | None = None
    nsfw: bool = False


# ── Role growth ──────────────────────────────────────────────────────────


class RoleGrowthSeries(BaseModel):
    role: str
    counts: list[int]


class RoleGrowthResponse(BaseModel):
    resolution: str
    window_label: str
    labels: list[str]
    series: list[RoleGrowthSeries]


# ── Message cadence ──────────────────────────────────────────────────────


class CadenceBucketSchema(BaseModel):
    label: str
    min_gap: float
    p20_gap: float
    median_gap: float
    p80_gap: float
    max_gap: float


class MessageCadenceResponse(BaseModel):
    resolution: str
    window_label: str
    channel_id: str | None = None
    buckets: list[CadenceBucketSchema]


# ── Join times ───────────────────────────────────────────────────────────


class JoinTimesResponse(BaseModel):
    resolution: str
    labels: list[str]
    counts: list[int]


# ── NSFW gender activity ────────────────────────────────────────────────


class GenderSeriesSchema(BaseModel):
    gender: str
    counts: list[int]
    color: str


class NsfwGenderResponse(BaseModel):
    resolution: str
    window_label: str
    media_only: bool
    labels: list[str]
    series: list[GenderSeriesSchema]


# ── Message rate ─────────────────────────────────────────────────────────


class MessageRateResponse(BaseModel):
    days: int
    tz_label: str
    buckets: list[int]
    avg_per_day: list[float]


# ── Greeter response ────────────────────────────────────────────────────


class ResponseBucketSchema(BaseModel):
    label: str
    count: int


class GreeterResponseEntry(BaseModel):
    user_id: str
    user_name: str = ""
    joined_at: float
    status: str = "greeted"
    greeted_at: float | None = None
    response_seconds: float | None = None
    wait_seconds: float | None = None
    greeter_id: str = ""
    greeter_name: str = ""
    left_at: float | None = None


class GreeterResponseResponse(BaseModel):
    window_label: str
    total_joins: int = 0
    count: int
    left_before_greeting_count: int = 0
    awaiting_greeting_count: int = 0
    median_seconds: float
    mean_seconds: float
    histogram: list[ResponseBucketSchema]
    response_times_seconds: list[float]
    entries: list[GreeterResponseEntry] = []


# ── Time to level 5 ───────────────────────────────────────────────────


class TimeToLevel5Member(BaseModel):
    user_id: int
    display_name: str
    first_at: str
    reached_at: str
    days: float


class TimeToLevel5Response(BaseModel):
    window_label: str
    count: int
    mean_days: float
    median_days: float
    stddev_days: float
    mode_days: int
    xp_required: float
    histogram: list[ResponseBucketSchema]
    members: list[TimeToLevel5Member]


# ── Activity ────────────────────────────────────────────────────────────


class ActivityResponse(BaseModel):
    resolution: str
    window_label: str
    mode: str
    labels: list[str]
    counts: list[float]
    member_counts: list[int]
    show_members: bool
    y_label: str
    tz_label: str


# ── Invite effectiveness ───────────────────────────────────────────────


class InviterRowSchema(BaseModel):
    inviter_id: str
    inviter_name: str = ""
    invite_count: int
    still_active: int
    retention_pct: float


class InviteEffectivenessResponse(BaseModel):
    total_invites: int
    total_active: int
    overall_retention_pct: float
    inviters: list[InviterRowSchema]


# ── Interaction graph ──────────────────────────────────────────────────


class InteractionEdgeSchema(BaseModel):
    from_id: str
    from_name: str = ""
    to_id: str
    to_name: str = ""
    weight: int


class InteractionNodeSchema(BaseModel):
    user_id: str
    user_name: str = ""
    total_outbound: int
    total_inbound: int
    unique_partners: int
    cluster_id: int = 0


class BridgeUserSchema(BaseModel):
    user_id: str
    user_name: str = ""
    betweenness: float


class ClusterInfoSchema(BaseModel):
    id: int
    size: int


class InteractionGraphMetricsSchema(BaseModel):
    clustering_coefficient: float
    network_density: float
    reciprocity: float
    isolates: int
    bridge_count: int
    bridge_users: list[BridgeUserSchema]
    clusters: list[ClusterInfoSchema]
    avg_path_length: float
    small_world_quotient: float
    node_count: int
    edge_count: int
    badge: str
    cross_cluster_matrix: list[list[float]]
    cross_cluster_labels: list[str]


class InteractionGraphResponse(BaseModel):
    nodes: list[InteractionNodeSchema]
    edges: list[InteractionEdgeSchema]
    top_pairs: list[InteractionEdgeSchema]
    metrics: InteractionGraphMetricsSchema | None = None


# ── Member retention ───────────────────────────────────────────────────


class RetentionEntrySchema(BaseModel):
    user_id: str
    user_name: str = ""
    msgs_prev: int
    msgs_recent: int
    drop_pct: float
    normalized_drop_pct: float = 0.0
    days_active_prev: int
    days_active_recent: int
    last_seen_ts: float | None = None
    level: int
    total_xp: float


class RetentionResponse(BaseModel):
    period_days: int
    total_dropoffs: int
    server_activity_change_pct: float = 0.0
    entries: list[RetentionEntrySchema]


# ── Voice activity ─────────────────────────────────────────────────────


class VoiceUserRowSchema(BaseModel):
    user_id: str
    user_name: str = ""
    total_minutes: float
    session_count: int
    avg_minutes: float


class VoiceHourBucketSchema(BaseModel):
    hour: int
    label: str
    total_minutes: float


class VoiceActivityResponse(BaseModel):
    total_sessions: int
    total_minutes: float
    avg_session_minutes: float
    top_users: list[VoiceUserRowSchema]
    by_hour: list[VoiceHourBucketSchema]


# ── XP leaderboard ────────────────────────────────────────────────────


class XpUserRowSchema(BaseModel):
    user_id: str
    user_name: str = ""
    level: int
    total_xp: float
    text_xp: float
    voice_xp: float
    reply_xp: float
    react_xp: float


class XpLevelBucketSchema(BaseModel):
    level: int
    count: int


class XpLeaderboardResponse(BaseModel):
    total_users: int
    leaderboard: list[XpUserRowSchema]
    level_distribution: list[XpLevelBucketSchema]
    source_totals: dict[str, float]


# ── Reaction analytics ─────────────────────────────────────────────────


class EmojiRowSchema(BaseModel):
    emoji: str
    total_count: int


class ReactionUserRowSchema(BaseModel):
    user_id: str
    user_name: str = ""
    given: int
    received: int


class ReactionAnalyticsResponse(BaseModel):
    top_emoji: list[EmojiRowSchema]
    top_givers: list[ReactionUserRowSchema]
    top_receivers: list[ReactionUserRowSchema]
    total_reactions: int


# ── Message rate drops ─────────────────────────────────────────────────


class RateDropEntrySchema(BaseModel):
    user_id: str
    user_name: str = ""
    prev_count: int
    recent_count: int
    drop_pct: float
    adjusted_drop_pct: float


class MessageRateDropsResponse(BaseModel):
    period_days: int
    server_prev: int
    server_recent: int
    server_drop_pct: float
    entries: list[RateDropEntrySchema]


# ── Burst ranking ──────────────────────────────────────────────────────


class BurstEntrySchema(BaseModel):
    user_id: str
    user_name: str = ""
    pre_avg: float
    post_avg: float
    increase: float
    sessions: int


class BurstRankingResponse(BaseModel):
    entries: list[BurstEntrySchema]


# ── Channel comparison ─────────────────────────────────────────────────


class ChannelRowSchema(BaseModel):
    channel_id: str
    channel_name: str = ""
    message_count: int
    unique_authors: int
    recent_count: int
    prev_count: int
    trend_pct: float
    total_xp: float = 0.0
    gini: float = 0.0
    avg_sentiment: float | None = None


class ChannelComparisonResponse(BaseModel):
    channels: list[ChannelRowSchema]


# ── Quality score ─────────────────────────────────────────────────────


class QualityScoreEntrySchema(BaseModel):
    user_id: str
    user_name: str = ""
    final_score: float
    engagement_given: float
    consistency_recency: float
    content_resonance: float
    posting_activity: float
    status: str
    active_days: int
    active_weeks: int


class QualityScoreResponse(BaseModel):
    total_scored: int
    entries: list[QualityScoreEntrySchema]


# ── Moderation: Jails ────────────────────────────────────────────────────


class JailEntrySchema(BaseModel):
    id: int
    user_id: str
    user_name: str = ""
    moderator_id: str
    moderator_name: str = ""
    reason: str
    status: str
    created_at: float
    expires_at: float | None = None
    released_at: float | None = None
    release_reason: str = ""
    channel_id: str = ""


class JailsResponse(BaseModel):
    active_count: int
    total_count: int
    jails: list[JailEntrySchema]


# ── Moderation: Tickets ──────────────────────────────────────────────────


class TicketEntrySchema(BaseModel):
    id: int
    user_id: str
    user_name: str = ""
    description: str
    status: str
    claimer_id: str | None = None
    claimer_name: str = ""
    escalated: bool = False
    created_at: float
    closed_at: float | None = None
    closed_by: str | None = None
    closer_name: str = ""
    close_reason: str = ""
    channel_id: str = ""
    channel_name: str = ""


class TicketsResponse(BaseModel):
    open_count: int
    closed_count: int
    total_count: int
    tickets: list[TicketEntrySchema]


class TicketSubjectSchema(BaseModel):
    user_id: str
    user_name: str = ""
    joined_at: float | None = None
    warn_count_active: int = 0
    jail_count_total: int = 0


class TicketHistoryEntrySchema(BaseModel):
    kind: str
    body: str
    actor_id: str = ""
    actor_name: str = ""
    date: float


class TicketDetailSchema(TicketEntrySchema):
    subject: TicketSubjectSchema
    history: list[TicketHistoryEntrySchema]


class TicketReasonBody(BaseModel):
    reason: str = ""


class TicketJailBody(BaseModel):
    duration: str = "24h"
    reason: str = ""


class TicketNoteBody(BaseModel):
    body: str


class TicketActionResult(BaseModel):
    ok: bool = True
    ticket_id: int
    status: str = ""
    message: str = ""


# ── Moderation: Warnings ─────────────────────────────────────────────────


class WarningEntrySchema(BaseModel):
    id: int
    user_id: str
    user_name: str = ""
    moderator_id: str
    moderator_name: str = ""
    reason: str
    created_at: float
    revoked: bool = False
    revoked_at: float | None = None
    revoked_by: str | None = None
    revoker_name: str = ""
    revoke_reason: str = ""


class WarningsResponse(BaseModel):
    active_count: int
    total_count: int
    warnings: list[WarningEntrySchema]


# ── Moderation: Policy Tickets ───────────────────────────────────────────


class PolicyTicketEntrySchema(BaseModel):
    id: int
    creator_id: str
    creator_name: str = ""
    title: str
    description: str = ""
    status: str
    vote_text: str = ""
    channel_id: str = ""
    created_at: float
    vote_started_at: float | None = None
    vote_ended_at: float | None = None


class PolicyTicketsResponse(BaseModel):
    open_count: int
    voting_count: int
    closed_count: int
    total_count: int
    policy_tickets: list[PolicyTicketEntrySchema]


# ── Moderation: Audit log ────────────────────────────────────────────────


class AuditEntrySchema(BaseModel):
    id: int
    action: str
    actor_id: str
    actor_name: str = ""
    target_id: str | None = None
    target_name: str = ""
    extra: dict = {}
    created_at: float


class AuditLogResponse(BaseModel):
    total: int
    entries: list[AuditEntrySchema]


# ── Moderation: Summary stats ────────────────────────────────────────────


class TranscriptResponse(BaseModel):
    transcript: dict | None = None


class ModerationStatsResponse(BaseModel):
    active_jails: int
    total_jails: int
    open_tickets: int
    closed_tickets: int
    total_tickets: int
    active_warnings: int
    total_warnings: int
    recent_actions: int


# ── Animated interaction heatmap ────────────────────────────────────────


class AnimatedHeatmapUser(BaseModel):
    user_id: str
    user_name: str = ""


class AnimatedHeatmapFrame(BaseModel):
    label: str
    matrix: list[list[int]]


class AnimatedHeatmapResponse(BaseModel):
    resolution: str
    window_label: str
    users: list[AnimatedHeatmapUser]
    frames: list[AnimatedHeatmapFrame]
    global_max: int
