from __future__ import annotations

import math
import re
import sqlite3
import statistics
import time
from dataclasses import dataclass

URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
CUSTOM_EMOJI_RE = re.compile(r"<a?:[A-Za-z0-9_]+:\d+>$")
COLLAPSE_WHITESPACE_RE = re.compile(r"\s+")
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
STRIP_CHARS = " \t\r\n`*_~|<>[](){}\"'“”‘’.,!?;:;/\\"


@dataclass(frozen=True)
class XpSettings:
    message_word_xp: float = 0.08
    reply_bonus_xp: float = 0.33
    image_reaction_received_xp: float = 0.17
    cooldown_thresholds_seconds: tuple[int, int, int] = (10, 30, 60)
    cooldown_multipliers: tuple[float, float, float] = (0.35, 0.6, 0.85)
    duplicate_multiplier: float = 0.2
    pair_streak_threshold: int = 4
    pair_streak_multiplier: float = 0.5
    voice_award_xp: float = 1.67
    voice_interval_seconds: int = 60
    voice_min_humans: int = 2
    voice_poll_seconds: int = 30
    manual_grant_xp: float = 20.0
    level_curve_factor: float = 15.6
    role_grant_level: int = 5


DEFAULT_XP_SETTINGS = XpSettings()

# Keys used to persist XP coefficients in the config table.
_XP_COEFF_PREFIX = "xp_coeff_"

_FLOAT_COEFFS = [
    "message_word_xp",
    "reply_bonus_xp",
    "image_reaction_received_xp",
    "duplicate_multiplier",
    "pair_streak_multiplier",
    "voice_award_xp",
    "manual_grant_xp",
    "level_curve_factor",
]

_INT_COEFFS = [
    "pair_streak_threshold",
    "voice_interval_seconds",
    "voice_min_humans",
]

_TUPLE_FLOAT_COEFFS = ["cooldown_multipliers"]
_TUPLE_INT_COEFFS = ["cooldown_thresholds_seconds"]


def load_xp_settings(conn: sqlite3.Connection) -> XpSettings:
    """Build an XpSettings from stored config values, falling back to defaults."""
    from db_utils import get_config_value

    defaults = DEFAULT_XP_SETTINGS
    kwargs: dict[str, object] = {}

    for key in _FLOAT_COEFFS:
        raw = get_config_value(conn, f"{_XP_COEFF_PREFIX}{key}", "")
        if raw:
            try:
                kwargs[key] = float(raw)
            except ValueError:
                pass

    for key in _INT_COEFFS:
        raw = get_config_value(conn, f"{_XP_COEFF_PREFIX}{key}", "")
        if raw:
            try:
                kwargs[key] = int(raw)
            except ValueError:
                pass

    for key in _TUPLE_FLOAT_COEFFS:
        raw = get_config_value(conn, f"{_XP_COEFF_PREFIX}{key}", "")
        if raw:
            try:
                vals = tuple(float(v.strip()) for v in raw.split(",") if v.strip())
                default_len = len(getattr(defaults, key))
                if len(vals) == default_len:
                    kwargs[key] = vals
            except ValueError:
                pass

    for key in _TUPLE_INT_COEFFS:
        raw = get_config_value(conn, f"{_XP_COEFF_PREFIX}{key}", "")
        if raw:
            try:
                vals = tuple(int(v.strip()) for v in raw.split(",") if v.strip())
                default_len = len(getattr(defaults, key))
                if len(vals) == default_len:
                    kwargs[key] = vals
            except ValueError:
                pass

    if not kwargs:
        return defaults
    # Merge with defaults for any unset fields
    for f in defaults.__dataclass_fields__:
        if f not in kwargs:
            kwargs[f] = getattr(defaults, f)
    return XpSettings(**kwargs)  # type: ignore[arg-type]


XP_SOURCE_TEXT = "text"
XP_SOURCE_REPLY = "reply"
XP_SOURCE_VOICE = "voice"
XP_SOURCE_IMAGE_REACT = "image_react"
XP_SOURCE_GRANT = "grant"


@dataclass(frozen=True)
class MessageXpContext:
    content: str
    seconds_since_last_message: float | None = None
    is_duplicate: bool = False
    is_reply_to_human: bool = False
    pair_streak: int = 0


@dataclass(frozen=True)
class MessageXpBreakdown:
    qualified_words: int
    normalized_content: str
    base_xp: float
    reply_bonus_xp: float
    cooldown_multiplier: float
    duplicate_multiplier: float
    pair_multiplier: float
    awarded_xp: float


@dataclass(frozen=True)
class MemberXpState:
    total_xp: float
    level: int
    last_message_at: float | None
    last_message_norm: str | None


@dataclass(frozen=True)
class AwardResult:
    awarded_xp: float
    total_xp: float
    old_level: int
    new_level: int
    role_grant_due: bool


@dataclass(frozen=True)
class LeaderboardEntry:
    user_id: int
    xp: float


@dataclass(frozen=True)
class XpDistributionStats:
    member_count: int
    median_xp: float
    stddev_xp: float


@dataclass(frozen=True)
class UserXpStanding:
    user_id: int
    xp: float
    rank: int | None


@dataclass(frozen=True)
class VoiceSession:
    guild_id: int
    user_id: int
    channel_id: int
    session_started_at: float
    qualified_since: float | None
    awarded_intervals: int


@dataclass(frozen=True)
class PairState:
    last_author_id: int | None = None
    active_pair: tuple[int, int] | None = None
    alternating_streak: int = 0


@dataclass(frozen=True)
class MemberActivity:
    user_id: int
    channel_id: int
    message_id: int
    created_at: float


def is_channel_xp_eligible(
    channel_id: int, parent_id: int | None, excluded_channel_ids: set[int]
) -> bool:
    """Return True if the channel (or its parent thread) is not in the XP exclusion list."""
    if channel_id in excluded_channel_ids:
        return False
    if parent_id is not None and parent_id in excluded_channel_ids:
        return False
    return True


def qualified_words(text: str) -> list[str]:
    """Return words from text that count toward XP.

    Excludes URLs, custom Discord emoji, text-emoji shortcodes, and tokens
    shorter than 3 characters or containing no alphanumeric characters.
    """
    clean_text = URL_RE.sub(" ", text)
    words: list[str] = []

    for raw in clean_text.split():
        if CUSTOM_EMOJI_RE.fullmatch(raw):
            continue
        token = raw.strip(STRIP_CHARS)
        if not token:
            continue
        if CUSTOM_EMOJI_RE.fullmatch(token):
            continue
        if token.startswith(":") and token.endswith(":") and token.count(":") >= 2:
            continue
        if len(token) < 3:
            continue
        if not any(ch.isalnum() for ch in token):
            continue
        words.append(token)

    return words


def normalize_message_content(text: str) -> str:
    """Return a canonical lowercase string used for duplicate-message detection."""
    normalized_words = []
    for word in qualified_words(text):
        compact = NON_ALNUM_RE.sub("", word.lower())
        if compact:
            normalized_words.append(compact)

    if normalized_words:
        return " ".join(normalized_words)

    stripped = URL_RE.sub(" ", text).strip().lower()
    return COLLAPSE_WHITESPACE_RE.sub(" ", stripped)


def cooldown_multiplier(
    seconds_since_last_message: float | None, settings: XpSettings = DEFAULT_XP_SETTINGS
) -> float:
    """Return an XP multiplier (0–1) based on how quickly the user posted again.

    None (first message or no prior timestamp) → 1.0 (no penalty).
    Otherwise the three cooldown thresholds in XpSettings are checked in order;
    if none match, 1.0 is returned.
    """
    if seconds_since_last_message is None:
        return 1.0

    for threshold, multiplier in zip(
        settings.cooldown_thresholds_seconds, settings.cooldown_multipliers
    ):
        if seconds_since_last_message < threshold:
            return multiplier

    return 1.0


def pair_multiplier(
    pair_streak: int, settings: XpSettings = DEFAULT_XP_SETTINGS
) -> float:
    """Return a reduced XP multiplier when two users have been exclusively messaging each other.

    Once the alternating streak between a pair exceeds ``pair_streak_threshold``,
    the multiplier drops to ``pair_streak_multiplier`` to discourage XP farming.
    """
    if pair_streak >= settings.pair_streak_threshold:
        return settings.pair_streak_multiplier
    return 1.0


def calculate_message_xp(
    context: MessageXpContext,
    settings: XpSettings = DEFAULT_XP_SETTINGS,
) -> MessageXpBreakdown:
    normalized_content = normalize_message_content(context.content)
    word_count = len(qualified_words(context.content))
    base_xp = word_count * settings.message_word_xp
    reply_bonus_xp = settings.reply_bonus_xp if context.is_reply_to_human else 0.0
    cooldown = cooldown_multiplier(context.seconds_since_last_message, settings)
    duplicate = settings.duplicate_multiplier if context.is_duplicate else 1.0
    pair = pair_multiplier(context.pair_streak, settings)
    awarded_xp = round((base_xp + reply_bonus_xp) * cooldown * duplicate * pair, 2)

    return MessageXpBreakdown(
        qualified_words=word_count,
        normalized_content=normalized_content,
        base_xp=base_xp,
        reply_bonus_xp=reply_bonus_xp,
        cooldown_multiplier=cooldown,
        duplicate_multiplier=duplicate,
        pair_multiplier=pair,
        awarded_xp=awarded_xp,
    )


def xp_required_for_level(
    level: int, settings: XpSettings = DEFAULT_XP_SETTINGS
) -> float:
    """Return the total XP required to reach ``level``.

    Uses a quadratic curve: ``factor × (level - 1)²``.  Level 1 always requires 0 XP.
    """
    if level <= 1:
        return 0.0

    factor = max(0.01, settings.level_curve_factor)
    return round(factor * ((level - 1) ** 2), 2)


def level_for_xp(total_xp: float, settings: XpSettings = DEFAULT_XP_SETTINGS) -> int:
    """Return the level a member is at given their total accumulated XP.

    Inverse of ``xp_required_for_level``: ``floor(sqrt(xp / factor)) + 1``.
    Always returns at least 1.
    """
    if total_xp <= 0:
        return 1

    factor = max(0.01, settings.level_curve_factor)
    return int(math.sqrt(total_xp / factor)) + 1


def role_grant_due(
    previous_level: int, new_level: int, settings: XpSettings = DEFAULT_XP_SETTINGS
) -> bool:
    """Return True if this level-up crosses the role-grant threshold for the first time."""
    return previous_level < settings.role_grant_level <= new_level


def init_xp_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS member_xp (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            total_xp REAL NOT NULL DEFAULT 0,
            level INTEGER NOT NULL DEFAULT 1,
            last_message_at REAL,
            last_message_norm TEXT,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS voice_sessions (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            session_started_at REAL NOT NULL,
            qualified_since REAL,
            awarded_intervals INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS xp_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            amount REAL NOT NULL,
            created_at REAL NOT NULL,
            channel_id INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_xp_events_lookup
        ON xp_events (guild_id, source, created_at, user_id)
        """
    )
    # Leaderboard: SELECT ... FROM member_xp WHERE guild_id=? ORDER BY total_xp DESC LIMIT ?
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_member_xp_leaderboard
        ON member_xp (guild_id, total_xp DESC)
        """
    )
    # Migration: add channel_id to existing xp_events tables
    cols = {row[1] for row in conn.execute("PRAGMA table_info(xp_events)").fetchall()}
    _needs_channel_backfill = "channel_id" not in cols
    if _needs_channel_backfill:
        conn.execute("ALTER TABLE xp_events ADD COLUMN channel_id INTEGER")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_xp_events_channel
        ON xp_events (guild_id, channel_id, created_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_messages (
            guild_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            created_at REAL NOT NULL,
            processed_at REAL NOT NULL,
            PRIMARY KEY (guild_id, message_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS member_activity (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            last_channel_id INTEGER NOT NULL,
            last_message_id INTEGER NOT NULL,
            last_message_at REAL NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )
    # Activity lookups: WHERE guild_id=? AND last_message_at >= ?
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_member_activity_guild_ts
        ON member_activity (guild_id, last_message_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS role_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role_name TEXT NOT NULL,
            action TEXT NOT NULL,
            granted_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_role_events_lookup
        ON role_events (guild_id, role_name, action, granted_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_processed_messages_backfill
        ON processed_messages (guild_id, user_id, created_at, channel_id)
        """
    )
    # Backfill channel_id from processed_messages (one-time, only on migration)
    if _needs_channel_backfill:
        conn.execute(
            """
            UPDATE xp_events
            SET channel_id = (
                SELECT pm.channel_id
                FROM processed_messages pm
                WHERE pm.guild_id = xp_events.guild_id
                  AND pm.user_id = xp_events.user_id
                  AND pm.created_at = xp_events.created_at
                LIMIT 1
            )
            WHERE channel_id IS NULL
              AND source IN ('text', 'reply', 'image_react')
            """
        )


def log_role_event(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    role_name: str,
    action: str,
    ts: float | None = None,
) -> None:
    """Record a role grant or removal event."""
    conn.execute(
        "INSERT INTO role_events (guild_id, user_id, role_name, action, granted_at) VALUES (?, ?, ?, ?, ?)",
        (guild_id, user_id, role_name, action, ts if ts is not None else time.time()),
    )


def get_member_xp_state(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    settings: XpSettings = DEFAULT_XP_SETTINGS,
) -> MemberXpState:
    row = conn.execute(
        """
        SELECT total_xp, last_message_at, last_message_norm
        FROM member_xp
        WHERE guild_id = ? AND user_id = ?
        """,
        (guild_id, user_id),
    ).fetchone()
    if not row:
        return MemberXpState(
            total_xp=0.0, level=1, last_message_at=None, last_message_norm=None
        )

    total_xp = float(row["total_xp"])
    return MemberXpState(
        total_xp=total_xp,
        level=level_for_xp(total_xp, settings),
        last_message_at=row["last_message_at"],
        last_message_norm=row["last_message_norm"],
    )


def apply_xp_award(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    xp_delta: float,
    *,
    message_timestamp: float | None = None,
    message_norm: str | None = None,
    event_source: str | None = None,
    event_timestamp: float | None = None,
    channel_id: int | None = None,
    settings: XpSettings = DEFAULT_XP_SETTINGS,
) -> AwardResult:
    """Atomically add XP to a member and update member_xp, optionally recording an xp_events row.

    ``xp_delta`` is clamped to ≥ 0.  If ``event_source`` is provided and the
    delta is positive, an event row is inserted for leaderboard tracking.
    Returns an ``AwardResult`` with old/new levels and whether a role grant is due.
    """
    state = get_member_xp_state(conn, guild_id, user_id, settings)
    old_level = state.level
    new_total_xp = round(state.total_xp + max(0.0, xp_delta), 2)
    new_level = level_for_xp(new_total_xp, settings)
    last_message_at = (
        state.last_message_at if message_timestamp is None else message_timestamp
    )
    last_message_norm = (
        state.last_message_norm if message_norm is None else message_norm
    )

    conn.execute(
        """
        INSERT INTO member_xp (
            guild_id,
            user_id,
            total_xp,
            level,
            last_message_at,
            last_message_norm
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            total_xp = excluded.total_xp,
            level = excluded.level,
            last_message_at = excluded.last_message_at,
            last_message_norm = excluded.last_message_norm
        """,
        (
            guild_id,
            user_id,
            new_total_xp,
            new_level,
            last_message_at,
            last_message_norm,
        ),
    )

    positive_xp = round(max(0.0, xp_delta), 2)
    if positive_xp > 0 and event_source:
        created_at = event_timestamp
        if created_at is None:
            created_at = (
                message_timestamp if message_timestamp is not None else time.time()
            )
        conn.execute(
            """
            INSERT INTO xp_events (guild_id, user_id, source, amount, created_at, channel_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (guild_id, user_id, event_source, positive_xp, created_at, channel_id),
        )

    return AwardResult(
        awarded_xp=positive_xp,
        total_xp=new_total_xp,
        old_level=old_level,
        new_level=new_level,
        role_grant_due=role_grant_due(old_level, new_level, settings),
    )


def record_xp_event(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    source: str,
    amount: float,
    created_at: float | None = None,
    channel_id: int | None = None,
) -> None:
    positive_amount = round(max(0.0, amount), 2)
    if positive_amount <= 0:
        return

    event_created_at = time.time() if created_at is None else created_at
    conn.execute(
        """
        INSERT INTO xp_events (guild_id, user_id, source, amount, created_at, channel_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (guild_id, user_id, source, positive_amount, event_created_at, channel_id),
    )


def is_message_processed(
    conn: sqlite3.Connection, guild_id: int, message_id: int
) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM processed_messages
        WHERE guild_id = ? AND message_id = ?
        LIMIT 1
        """,
        (guild_id, message_id),
    ).fetchone()
    return row is not None


def mark_message_processed(
    conn: sqlite3.Connection,
    guild_id: int,
    message_id: int,
    channel_id: int,
    user_id: int,
    created_at: float,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO processed_messages (
            guild_id,
            message_id,
            channel_id,
            user_id,
            created_at,
            processed_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (guild_id, message_id, channel_id, user_id, created_at, time.time()),
    )


def record_member_activity(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    channel_id: int,
    message_id: int,
    created_at: float,
) -> None:
    conn.execute(
        """
        INSERT INTO member_activity (
            guild_id,
            user_id,
            last_channel_id,
            last_message_id,
            last_message_at
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            last_channel_id = excluded.last_channel_id,
            last_message_id = excluded.last_message_id,
            last_message_at = excluded.last_message_at
        WHERE excluded.last_message_at >= member_activity.last_message_at
        """,
        (guild_id, user_id, channel_id, message_id, created_at),
    )


def get_member_last_activity_map(
    conn: sqlite3.Connection,
    guild_id: int,
    user_ids: list[int],
) -> dict[int, MemberActivity]:
    if not user_ids:
        return {}

    def batched_ids(values: list[int], batch_size: int = 800) -> list[list[int]]:
        return [values[i : i + batch_size] for i in range(0, len(values), batch_size)]

    activity_map: dict[int, MemberActivity] = {}

    # Primary source: explicit member activity table.
    for batch in batched_ids(user_ids):
        placeholders = ", ".join("?" for _ in batch)
        query = """
            SELECT user_id, last_channel_id, last_message_id, last_message_at
            FROM member_activity
            WHERE guild_id = ? AND user_id IN ({})
            """.format(placeholders)
        rows = conn.execute(query, [guild_id, *batch]).fetchall()
        for row in rows:
            activity_map[int(row["user_id"])] = MemberActivity(
                user_id=int(row["user_id"]),
                channel_id=int(row["last_channel_id"]),
                message_id=int(row["last_message_id"]),
                created_at=float(row["last_message_at"]),
            )

    missing_ids = [user_id for user_id in user_ids if user_id not in activity_map]

    # Fallback: processed message ledger (historical coverage before member_activity existed).
    if missing_ids:
        for batch in batched_ids(missing_ids):
            placeholders = ", ".join("?" for _ in batch)
            query = """
                SELECT pm.user_id, pm.channel_id, pm.message_id, pm.created_at
                FROM processed_messages pm
                INNER JOIN (
                    SELECT user_id, MAX(created_at) AS max_created_at
                    FROM processed_messages
                    WHERE guild_id = ? AND user_id IN ({})
                    GROUP BY user_id
                ) latest
                    ON latest.user_id = pm.user_id
                   AND latest.max_created_at = pm.created_at
                WHERE pm.guild_id = ? AND pm.user_id IN ({})
                """.format(placeholders, placeholders)
            rows = conn.execute(query, [guild_id, *batch, guild_id, *batch]).fetchall()
            for row in rows:
                user_id = int(row["user_id"])
                if user_id in activity_map:
                    continue
                activity_map[user_id] = MemberActivity(
                    user_id=user_id,
                    channel_id=int(row["channel_id"]),
                    message_id=int(row["message_id"]),
                    created_at=float(row["created_at"]),
                )

    missing_ids = [user_id for user_id in user_ids if user_id not in activity_map]

    # Last fallback: member_xp timestamp only (channel/message unknown).
    if missing_ids:
        for batch in batched_ids(missing_ids):
            placeholders = ", ".join("?" for _ in batch)
            query = """
                SELECT user_id, last_message_at
                FROM member_xp
                WHERE guild_id = ? AND user_id IN ({}) AND last_message_at IS NOT NULL
                """.format(placeholders)
            rows = conn.execute(query, [guild_id, *batch]).fetchall()
            for row in rows:
                user_id = int(row["user_id"])
                if user_id in activity_map:
                    continue
                activity_map[user_id] = MemberActivity(
                    user_id=user_id,
                    channel_id=0,
                    message_id=0,
                    created_at=float(row["last_message_at"]),
                )

    return activity_map


def update_pair_state(state: PairState | None, author_id: int) -> tuple[PairState, int]:
    """Advance the pair-streak state machine and return (new_state, current_streak).

    A streak increments each time the same two authors alternate messages.
    It resets to 1 whenever a different pair posts, and to 0 when the same
    author posts consecutively.
    """
    if state is None or state.last_author_id is None:
        return PairState(last_author_id=author_id), 0

    if author_id == state.last_author_id:
        return PairState(last_author_id=author_id), 0

    first_id, second_id = sorted((author_id, state.last_author_id))
    pair = (first_id, second_id)
    if state.active_pair == pair:
        streak = state.alternating_streak + 1
    else:
        streak = 1

    return PairState(
        last_author_id=author_id, active_pair=pair, alternating_streak=streak
    ), streak


def get_voice_session(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> VoiceSession | None:
    row = conn.execute(
        """
        SELECT guild_id, user_id, channel_id, session_started_at, qualified_since, awarded_intervals
        FROM voice_sessions
        WHERE guild_id = ? AND user_id = ?
        """,
        (guild_id, user_id),
    ).fetchone()
    if not row:
        return None

    return VoiceSession(
        guild_id=int(row["guild_id"]),
        user_id=int(row["user_id"]),
        channel_id=int(row["channel_id"]),
        session_started_at=float(row["session_started_at"]),
        qualified_since=row["qualified_since"],
        awarded_intervals=int(row["awarded_intervals"]),
    )


def set_voice_session(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    channel_id: int,
    *,
    session_started_at: float | None = None,
    qualified_since: float | None = None,
    awarded_intervals: int = 0,
) -> None:
    started_at = time.time() if session_started_at is None else session_started_at
    conn.execute(
        """
        INSERT INTO voice_sessions (
            guild_id,
            user_id,
            channel_id,
            session_started_at,
            qualified_since,
            awarded_intervals
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            channel_id = excluded.channel_id,
            session_started_at = excluded.session_started_at,
            qualified_since = excluded.qualified_since,
            awarded_intervals = excluded.awarded_intervals
        """,
        (guild_id, user_id, channel_id, started_at, qualified_since, awarded_intervals),
    )


def delete_voice_session(conn: sqlite3.Connection, guild_id: int, user_id: int) -> None:
    conn.execute(
        "DELETE FROM voice_sessions WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )


def list_voice_sessions(conn: sqlite3.Connection) -> list[VoiceSession]:
    rows = conn.execute(
        """
        SELECT guild_id, user_id, channel_id, session_started_at, qualified_since, awarded_intervals
        FROM voice_sessions
        """
    ).fetchall()
    return [
        VoiceSession(
            guild_id=int(row["guild_id"]),
            user_id=int(row["user_id"]),
            channel_id=int(row["channel_id"]),
            session_started_at=float(row["session_started_at"]),
            qualified_since=row["qualified_since"],
            awarded_intervals=int(row["awarded_intervals"]),
        )
        for row in rows
    ]


def completed_voice_intervals(
    session: VoiceSession,
    now_ts: float,
    settings: XpSettings = DEFAULT_XP_SETTINGS,
) -> int:
    """Return the number of full voice XP intervals earned since the last award.

    Returns 0 if the session has not yet qualified (e.g. not enough humans in
    the channel) or no new complete intervals have elapsed.
    """
    if session.qualified_since is None:
        return 0

    elapsed = max(0.0, now_ts - session.qualified_since)
    completed = int(elapsed // settings.voice_interval_seconds)
    return max(0, completed - session.awarded_intervals)


def get_xp_leaderboard(
    conn: sqlite3.Connection,
    guild_id: int,
    source: str,
    *,
    since_ts: float | None = None,
    limit: int = 5,
) -> list[LeaderboardEntry]:
    params: list[object] = [guild_id, source]
    where_clause = "guild_id = ? AND source = ?"
    if since_ts is not None:
        where_clause += " AND created_at >= ?"
        params.append(since_ts)
    params.append(limit)

    query = """
        SELECT user_id, ROUND(SUM(amount), 2) AS xp
        FROM xp_events
        WHERE {}
        GROUP BY user_id
        ORDER BY xp DESC, user_id ASC
        LIMIT ?
        """.format(where_clause)
    rows = conn.execute(query, params).fetchall()

    return [
        LeaderboardEntry(
            user_id=int(row["user_id"]),
            xp=float(row["xp"]),
        )
        for row in rows
    ]


def get_xp_distribution_stats(
    conn: sqlite3.Connection,
    guild_id: int,
    source: str,
    *,
    since_ts: float | None = None,
) -> XpDistributionStats:
    params: list[object] = [guild_id, source]
    where_clause = "guild_id = ? AND source = ?"
    if since_ts is not None:
        where_clause += " AND created_at >= ?"
        params.append(since_ts)

    query = """
        SELECT ROUND(SUM(amount), 2) AS xp
        FROM xp_events
        WHERE {}
        GROUP BY user_id
        """.format(where_clause)
    rows = conn.execute(query, params).fetchall()
    values = [float(row["xp"]) for row in rows]
    if not values:
        return XpDistributionStats(member_count=0, median_xp=0.0, stddev_xp=0.0)

    return XpDistributionStats(
        member_count=len(values),
        median_xp=round(float(statistics.median(values)), 2),
        stddev_xp=round(float(statistics.pstdev(values)), 2),
    )


def get_user_xp_standing(
    conn: sqlite3.Connection,
    guild_id: int,
    source: str,
    user_id: int,
    *,
    since_ts: float | None = None,
) -> UserXpStanding:
    params: list[object] = [guild_id, source]
    where_clause = "guild_id = ? AND source = ?"
    if since_ts is not None:
        where_clause += " AND created_at >= ?"
        params.append(since_ts)

    query = """
        SELECT user_id, ROUND(SUM(amount), 2) AS xp
        FROM xp_events
        WHERE {}
        GROUP BY user_id
        ORDER BY xp DESC, user_id ASC
        """.format(where_clause)
    rows = conn.execute(query, params).fetchall()

    for idx, row in enumerate(rows, start=1):
        if int(row["user_id"]) == user_id:
            return UserXpStanding(
                user_id=user_id,
                xp=float(row["xp"]),
                rank=idx,
            )

    return UserXpStanding(
        user_id=user_id,
        xp=0.0,
        rank=None,
    )


def has_any_xp_events(conn: sqlite3.Connection, guild_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM xp_events
        WHERE guild_id = ?
        LIMIT 1
        """,
        (guild_id,),
    ).fetchone()
    return row is not None


def has_any_member_xp(conn: sqlite3.Connection, guild_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM member_xp
        WHERE guild_id = ?
        LIMIT 1
        """,
        (guild_id,),
    ).fetchone()
    return row is not None


def count_xp_events(conn: sqlite3.Connection, guild_id: int) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM xp_events
        WHERE guild_id = ?
        """,
        (guild_id,),
    ).fetchone()
    return int(row[0]) if row else 0


def get_time_to_level_seconds(
    conn: sqlite3.Connection,
    guild_id: int,
    target_level: int,
    settings: XpSettings = DEFAULT_XP_SETTINGS,
    *,
    since_ts: float | None = None,
) -> list[float]:
    """Return a list of durations (seconds) each user took to first reach target_level.

    Uses a running-sum window query to find the exact event that pushed each user
    over the XP threshold, then measures from that user's first-ever XP event.

    If *since_ts* is given, only include users who reached the level at or after
    that unix timestamp.
    """
    xp_threshold = xp_required_for_level(target_level, settings)

    since_clause = ""
    params: list[object] = [guild_id, guild_id, xp_threshold]
    if since_ts is not None:
        since_clause = " AND lr.reached_at >= ?"
        params.append(since_ts)

    rows = conn.execute(
        f"""
        WITH running AS (
            SELECT
                user_id,
                created_at,
                SUM(amount) OVER (
                    PARTITION BY user_id ORDER BY created_at
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ) AS cumulative_xp
            FROM xp_events
            WHERE guild_id = ?
        ),
        first_event AS (
            SELECT user_id, MIN(created_at) AS first_at
            FROM xp_events
            WHERE guild_id = ?
            GROUP BY user_id
        ),
        level_reached AS (
            SELECT user_id, MIN(created_at) AS reached_at
            FROM running
            WHERE cumulative_xp >= ?
            GROUP BY user_id
        )
        SELECT lr.reached_at - fe.first_at AS seconds_to_level
        FROM level_reached lr
        JOIN first_event fe ON fe.user_id = lr.user_id
        WHERE seconds_to_level >= 0{since_clause}
        """,
        params,
    ).fetchall()

    return [float(row[0]) for row in rows]


def get_time_to_level_details(
    conn: sqlite3.Connection,
    guild_id: int,
    target_level: int,
    settings: XpSettings = DEFAULT_XP_SETTINGS,
    *,
    since_ts: float | None = None,
) -> list[dict]:
    """Return per-user details for reaching target_level.

    Each dict has: user_id, first_at (unix ts), reached_at (unix ts), seconds.
    """
    xp_threshold = xp_required_for_level(target_level, settings)

    since_clause = ""
    params: list[object] = [guild_id, guild_id, xp_threshold]
    if since_ts is not None:
        since_clause = " AND lr.reached_at >= ?"
        params.append(since_ts)

    rows = conn.execute(
        f"""
        WITH running AS (
            SELECT
                user_id,
                created_at,
                SUM(amount) OVER (
                    PARTITION BY user_id ORDER BY created_at
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ) AS cumulative_xp
            FROM xp_events
            WHERE guild_id = ?
        ),
        first_event AS (
            SELECT user_id, MIN(created_at) AS first_at
            FROM xp_events
            WHERE guild_id = ?
            GROUP BY user_id
        ),
        level_reached AS (
            SELECT user_id, MIN(created_at) AS reached_at
            FROM running
            WHERE cumulative_xp >= ?
            GROUP BY user_id
        )
        SELECT lr.user_id, fe.first_at, lr.reached_at,
               lr.reached_at - fe.first_at AS seconds_to_level
        FROM level_reached lr
        JOIN first_event fe ON fe.user_id = lr.user_id
        WHERE seconds_to_level >= 0{since_clause}
        ORDER BY seconds_to_level ASC
        """,
        params,
    ).fetchall()

    return [
        {
            "user_id": int(row[0]),
            "first_at": float(row[1]),
            "reached_at": float(row[2]),
            "seconds": float(row[3]),
        }
        for row in rows
    ]


def get_oldest_xp_event_timestamp(
    conn: sqlite3.Connection,
    guild_id: int,
    sources: tuple[str, ...] | None = None,
) -> float | None:
    params: list[object] = [guild_id]
    where_clause = "guild_id = ?"

    if sources:
        placeholders = ", ".join("?" for _ in sources)
        where_clause += " AND source IN ({})".format(placeholders)
        params.extend(sources)

    query = """
        SELECT MIN(created_at)
        FROM xp_events
        WHERE {}
        """.format(where_clause)
    row = conn.execute(query, params).fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0])
