"""Pydantic response models for the dashboard API."""

from __future__ import annotations

from pydantic import BaseModel, Field


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
    games_editor_role_id: str | None = None
    economy_manager_role_id: str | None = None
    wellness_opted_in: bool = False


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
    #: Whether @everyone can read the channel. None = not known, which is the
    #: honest answer on the DB fallback path (no gateway, so no permission
    #: overwrites to compute from). A caller warning about exposure must treat
    #: None as "don't know", never as "safe".
    everyone_can_read: bool | None = None


# ── Role growth ──────────────────────────────────────────────────────────


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


# ── NSFW tag mix ─────────────────────────────────────────────────────────


class TagSeriesSchema(BaseModel):
    #: The detector's own label, e.g. FEMALE_BREAST_EXPOSED. Kept alongside the
    #: display name so a caller can key off something stable.
    label: str
    display: str
    #: Index in the tagger's fixed vocabulary — the series' palette slot, so a
    #: label keeps its colour when a window changes which labels are present.
    order: int
    counts: list[int]


class NsfwTagMixResponse(BaseModel):
    resolution: str
    window_label: str
    labels: list[str]
    #: No colour field, unlike the gender series: tags have no inherent colour
    #: and are an open-ended list, so the panel assigns them from the validated
    #: categorical palette instead.
    series: list[TagSeriesSchema]


# ── Message rate ─────────────────────────────────────────────────────────


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


# ── Ping response ─────────────────────────────────────────────────────


class PingEntrySchema(BaseModel):
    message_id: str
    channel_id: str
    channel_name: str = ""
    author_id: str
    author_name: str = ""
    role_ids: list[str] = []
    role_labels: list[str] = []
    everyone: bool = False
    source: str = "member"
    ref: str | None = None
    ts: float
    turnout: int = 0
    messages: int = 0
    reactors: int = 0
    # None (not 0) when the ping named no game, or named one that left no
    # roster behind — "we don't know" must not render as "nobody came".
    players: int | None = None


class PingBreakdownSchema(BaseModel):
    id: str
    label: str
    pings: int
    mean_turnout: float
    median_turnout: float
    silent_pings: int
    silent_pct: float


class PingSeriesPointSchema(BaseModel):
    day: str
    pings: int
    mean_turnout: float


class PingResponseResponse(BaseModel):
    window_label: str
    window_minutes: int
    total_pings: int
    total_turnout: int
    median_turnout: float
    mean_turnout: float
    silent_pings: int
    silent_pct: float
    series: list[PingSeriesPointSchema] = []
    by_role: list[PingBreakdownSchema] = []
    by_channel: list[PingBreakdownSchema] = []
    entries: list[PingEntrySchema] = []


# ── Intake report ─────────────────────────────────────────────────────


class IntakeOpenCardSchema(BaseModel):
    user_id: str
    user_name: str = ""
    created_at: float
    nudged: bool = False
    done: int
    total: int
    pending: list[str] = []


class IntakeWelcomerSchema(BaseModel):
    user_id: str
    user_name: str = ""
    completions: int = 0
    ticks: int = 0


class IntakeSkippedStepSchema(BaseModel):
    key: str
    label: str
    appeared: int
    skipped: int


class IntakeReportResponse(BaseModel):
    enabled: bool
    window_label: str
    open_cards: list[IntakeOpenCardSchema] = []
    resolved: int = 0
    counts: dict[str, int] = {}
    mean_seconds: float = 0.0
    median_seconds: float = 0.0
    welcomers: list[IntakeWelcomerSchema] = []
    skipped_steps: list[IntakeSkippedStepSchema] = []


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


class ActivitySeriesSchema(BaseModel):
    source: str
    counts: list[float]


class ActivityResponse(BaseModel):
    resolution: str
    window_label: str
    mode: str
    labels: list[str]
    # Nullable on the overlay views: the current period stops at the hour we
    # are in rather than flooring the rest of the period to zero.
    counts: list[float | None]
    # Overlay views only: the same series under a centred rolling mean, drawn
    # in place of the raw line so a single week reads as a shape rather than
    # hash. Empty where nothing is smoothed; `counts` remains the exact series
    # behind the table and the period total.
    counts_smooth: list[float | None] = []
    smooth_window: int = 1
    member_counts: list[int]
    show_members: bool
    y_label: str
    tz_label: str
    x_label: str = "Period"
    series: list[ActivitySeriesSchema] = []
    # Overlay views only — the p25/p50/p75 envelope the current period is read
    # against, empty when the sample was too thin to summarise.
    band_low: list[float] = []
    band_mid: list[float] = []
    band_high: list[float] = []
    periods_sampled: int = 0
    # What the band summarises, named for the legend and the table: "Typical
    # week", or "Typical Tuesday" when the sample is one weekday's history.
    band_label: str = ""


# ── Invite effectiveness ───────────────────────────────────────────────


class InviteeRowSchema(BaseModel):
    invitee_id: str
    invitee_name: str = ""
    active: bool


class InviterRowSchema(BaseModel):
    inviter_id: str
    inviter_name: str = ""
    invite_count: int
    still_active: int
    retention_pct: float
    # Per-invitee detail behind the dashboard's expand row. Omitting this
    # field used to make FastAPI's response_model silently strip it from
    # every inviter — the summary totals (top-level fields, above) still
    # matched, but expanding any inviter always rendered the empty state.
    invitees: list[InviteeRowSchema] = []


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


class InteractionSeriesNodeSchema(BaseModel):
    user_id: str
    user_name: str = ""
    cluster_id: int = 0
    joins: list[int] = []
    leaves: list[int] = []


class InteractionSeriesPairSchema(BaseModel):
    a: str
    b: str
    w: list[int]


class InteractionSeriesResponse(BaseModel):
    """Weekly-binned pair history for the Connection Graph's replay."""

    bin_seconds: int
    start: int
    weeks: int
    nodes: list[InteractionSeriesNodeSchema]
    pairs: list[InteractionSeriesPairSchema]


# ── One-sided attention (moderator review) ──────────────────────────────


class AttentionCandidateSchema(BaseModel):
    from_id: str
    from_name: str = ""
    to_id: str
    to_name: str = ""
    text_out: int
    react_out: int
    voice_follow_out: int
    weight_out: float
    weight_back: float
    approach_out: float = 0.0
    asymmetry: float
    # How much the target gives back per unit received across their OTHER
    # partners, and how far short of that this initiator falls. 1.0 for the
    # rate means "no other partner to compare against", not "generous".
    target_reciprocation_rate: float = 1.0
    expected_back: float = 0.0
    reciprocation_shortfall: float = 0.0
    concentration: float
    distinct_targets: int
    escalation: float | None = None
    ever_reciprocated: bool
    max_burst: int
    reasons: list[str] = []
    cautions: list[str] = []


class OneSidedAttentionResponse(BaseModel):
    window_days: int
    candidates: list[AttentionCandidateSchema]


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
    level_distribution_active_days: int
    source_totals: dict[str, float]


# ── Reaction analytics ─────────────────────────────────────────────────


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


# ── Contributors ──────────────────────────────────────────────────────


class ContributorEntrySchema(BaseModel):
    user_id: str
    user_name: str = ""
    score: float
    volume: int
    own_rate: float = 0.0
    baseline: float = 0.0
    partners: int = 0
    given: int = 0
    received: int = 0
    concentration: float = 0.0


class ContributorsResponse(BaseModel):
    window_days: int
    members_considered: int
    popular: list[ContributorEntrySchema]
    catalyst: list[ContributorEntrySchema]
    connectors: list[ContributorEntrySchema]
    welcomers: list[ContributorEntrySchema]
    under_attended: list[ContributorEntrySchema]


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
    # True/False when the bot can see the guild, None when it can't — the panel
    # only warns about lost roles on a definite False.
    in_guild: bool | None = None


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


class SimpleActionResult(BaseModel):
    ok: bool = True
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


# ── Moderation: Community ballots ────────────────────────────────────────
#
# Every snowflake is a string: a channel, thread or member id exceeds
# JavaScript's safe integer range, and the precision sweep fails a bare number.


class PolicyBallotVoteSchema(BaseModel):
    user_id: str
    user_name: str = ""
    choice: str


class PolicyBallotEntrySchema(BaseModel):
    id: int
    policy_id: int
    question: str
    channel_id: str = ""
    thread_id: str = ""
    opened_by: str
    opened_by_name: str = ""
    opened_at: float
    #: 0 when the guild's voting deadline is off — the ballot waits for a
    #: moderator's Close press instead of expiring.
    closes_at: float = 0
    closed_at: float | None = None
    closed_by: str = ""
    closed_by_name: str = ""
    #: "" while open, then passed / failed / cancelled.
    outcome: str = ""
    yes_count: int
    no_count: int
    abstain_count: int
    votes: list[PolicyBallotVoteSchema] = []


class PolicyBallotsResponse(BaseModel):
    open_count: int
    total_count: int
    ballots: list[PolicyBallotEntrySchema]


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
    # The distinct actions this guild's log actually contains, so the Action
    # filter offers exactly those. It used to be a hand-kept list in the panel,
    # six of whose twelve entries named strings the bot never writes.
    actions: list[str] = []


class DMAuditEntry(BaseModel):
    id: int
    action: str
    actor_id: str | None = None
    actor_name: str = ""
    user_a_id: str | None = None
    user_a_name: str = ""
    user_b_id: str | None = None
    user_b_name: str = ""
    notes: str | None = None
    timestamp: float


class DMAuditLogResponse(BaseModel):
    total: int
    entries: list[DMAuditEntry]


class WhisperAuditEntry(BaseModel):
    id: int
    sender_id: str
    sender_name: str = ""
    target_id: str
    target_name: str = ""
    state: str
    solved: bool
    exposed: bool
    report_count: int
    created_at: float


class WhisperAuditLogResponse(BaseModel):
    total: int
    entries: list[WhisperAuditEntry]


class ConfessionAuditEntry(BaseModel):
    id: int
    message_id: str | None = None
    author_id: str
    author_name: str = ""
    channel_id: str | None = None
    # "confession" for a root post, "reply" for an anonymous reply to one.
    kind: str = "confession"
    # The confession a reply belongs to; equal to message_id on a root post.
    # A string because it is a snowflake — see migration 145.
    root_message_id: str | None = None
    # The member being replied to, where the root author was still known.
    replied_to_id: str | None = None
    replied_to_name: str = ""
    # Joined from `messages`; null when the guild's storage level is 'none'.
    # The audit table itself never stores content — see migration 145.
    content: str | None = None
    created_at: float


class ConfessionsAuditLogResponse(BaseModel):
    total: int
    entries: list[ConfessionAuditEntry]


class AnonAuditFeature(BaseModel):
    value: str
    label: str


class AnonAuditEntry(BaseModel):
    id: int
    feature: str
    feature_label: str = ""
    event: str
    # Derived from the service's MOD_EVENTS — a moderator acting on someone
    # else's anonymous post, rather than a member posting anonymously.
    is_mod_action: bool = False
    actor_id: str
    actor_name: str = ""
    target_id: str | None = None
    target_name: str = ""
    game_id: str | None = None
    message_id: str | None = None
    channel_id: str | None = None
    # Joined from `messages`; null when the guild's storage level is 'none' or
    # the event produced no guild message at all. The audit table itself never
    # stores content — see migration 145.
    content: str | None = None
    extra: dict = {}
    created_at: float


class AnonAuditLogResponse(BaseModel):
    total: int
    entries: list[AnonAuditEntry]
    features: list[AnonAuditFeature]


class AnonAuditRetentionResponse(BaseModel):
    retention_days: int


class AnonAuditRetentionBody(BaseModel):
    retention_days: int = Field(ge=0, le=3650)


# ── Moderation: NSFW image reports ───────────────────────────────────────


class NsfwTagCount(BaseModel):
    label: str
    count: int
    #: Mean Marqo probability across the images carrying this tag. Reads as
    #: "when NudeNet sees this, how sure is the verdict engine?"
    avg_score: float


class NsfwScoreBucket(BaseModel):
    #: Lower bound of a 0.1-wide bucket of Marqo scores.
    floor: float
    count: int
    explicit: int


class NsfwTagsResponse(BaseModel):
    days: int
    classified: int
    explicit: int
    tagged: int
    #: Images the verdict engine called explicit that the tagger found nothing
    #: on — the NudeNet blind spot this swap exists to cover.
    explicit_untagged: int
    #: The reverse disagreement: tagged as exposed nudity, verdict said no.
    tagged_not_explicit: int
    avg_inference_ms: float
    labels: list[NsfwTagCount]
    scores: list[NsfwScoreBucket]


class NsfwBlockEntry(BaseModel):
    message_id: str
    channel_id: str
    channel_name: str = ""
    author_id: str
    author_name: str = ""
    filename: str
    #: None means the image could not be read at all.
    score: float | None = None
    surface: str
    action: str
    created_at: int


class NsfwBlocksResponse(BaseModel):
    days: int
    total: int
    removed: int
    logged: int
    by_surface: dict[str, int]
    entries: list[NsfwBlockEntry]


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


class MemberRowSchema(BaseModel):
    user_id: str
    display_name: str = ""
    last_message_ts: float | None = None
    last_message_channel_id: str | None = None
    days_since_last: float | None = None


class InactiveReportResponse(BaseModel):
    days: int
    role_id: str | None = None
    role_name: str | None = None
    role_mode: str = "with"
    channel_id: str | None = None
    total_scoped: int
    tracking_coverage: int
    total: int
    members: list[MemberRowSchema]


class GrantAuditMemberRow(BaseModel):
    user_id: str
    display_name: str = ""
    level: int | None = None
    pruned_at: float | None = None


class GrantAuditResponse(BaseModel):
    grant_name: str
    label: str
    role_id: str
    min_level: int
    inactivity_days: int
    waiting_first_grant: list[GrantAuditMemberRow]
    stripped_returned: list[GrantAuditMemberRow]
    recent_inactive: list[GrantAuditMemberRow]


# ── Welcome / leave preview ────────────────────────────────────────────


# ── Gender ─────────────────────────────────────────────────────────────


class GenderEntrySchema(BaseModel):
    user_id: str
    display_name: str = ""
    gender: str
    set_by: str
    set_at: float


class GenderListResponse(BaseModel):
    classified: list[GenderEntrySchema]


class GenderUnclassifiedResponse(BaseModel):
    members: list[MemberRowSchema]
    total: int


class GenderSetRequest(BaseModel):
    user_id: str
    gender: str  # 'male' | 'female' | 'nonbinary'


class OkResponse(BaseModel):
    ok: bool = True
    message: str = ""


# ── Quote audit log ────────────────────────────────────────────────────


class QuoteAuditEntry(BaseModel):
    id: int
    ts: float
    channel_id: str
    quoter_id: str
    quoter_name: str = ""
    quoted_user_id: str
    quoted_user_name: str = ""
    quoted_message_id: str
    posted_message_id: str
    theme: str
    font: str


class QuoteAuditLogResponse(BaseModel):
    total: int
    entries: list[QuoteAuditEntry]


# ── Usage telemetry ────────────────────────────────────────────────────


class UsageNameRow(BaseModel):
    name: str
    uses: int
    users: int
    errors: int
    last_ts: float


class UsageUserRow(BaseModel):
    # Snowflake — string, never a bare JSON number (JS loses precision > 2^53).
    user_id: str
    name: str = ""
    uses: int
    distinct_names: int
    last_ts: float


class UsageDayPoint(BaseModel):
    day: str
    count: int


class UsageReportResponse(BaseModel):
    days: int
    totals: dict[str, int]
    commands: list[UsageNameRow]
    panels: list[UsageNameRow]
    unused_commands: list[str]
    # Panel names actually recorded. The never-opened list is the client's own
    # nav list minus this — the full panel list is too large for a query param.
    seen_panels: list[str]
    top_users: list[UsageUserRow]
    dashboard_users: list[UsageUserRow]
    daily_commands: list[UsageDayPoint]
    daily_panels: list[UsageDayPoint]
    hours: list[int]
