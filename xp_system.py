from __future__ import annotations

import re
import sqlite3
import time

from dataclasses import dataclass


URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
CUSTOM_EMOJI_RE = re.compile(r"<a?:[A-Za-z0-9_]+:\d+>$")
COLLAPSE_WHITESPACE_RE = re.compile(r"\s+")
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
STRIP_CHARS = " \t\r\n`*_~|<>[](){}\"'“”‘’.,!?;:;/\\"


@dataclass(frozen=True)
class XpSettings:
    message_word_xp: float = 0.25
    reply_bonus_xp: float = 1.0
    image_reaction_received_xp: float = 0.5
    cooldown_thresholds_seconds: tuple[int, int, int] = (10, 30, 60)
    cooldown_multipliers: tuple[float, float, float] = (0.35, 0.6, 0.85)
    duplicate_multiplier: float = 0.2
    pair_streak_threshold: int = 4
    pair_streak_multiplier: float = 0.5
    voice_award_xp: float = 20.0
    voice_interval_seconds: int = 600
    voice_min_humans: int = 3
    voice_poll_seconds: int = 30
    manual_grant_xp: float = 20.0
    level_step_xp: float = 100.0
    role_grant_level: int = 5


DEFAULT_XP_SETTINGS = XpSettings()
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


def is_channel_xp_eligible(channel_id: int, parent_id: int | None, excluded_channel_ids: set[int]) -> bool:
    if channel_id in excluded_channel_ids:
        return False
    if parent_id is not None and parent_id in excluded_channel_ids:
        return False
    return True


def qualified_words(text: str) -> list[str]:
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
    normalized_words = []
    for word in qualified_words(text):
        compact = NON_ALNUM_RE.sub("", word.lower())
        if compact:
            normalized_words.append(compact)

    if normalized_words:
        return " ".join(normalized_words)

    stripped = URL_RE.sub(" ", text).strip().lower()
    return COLLAPSE_WHITESPACE_RE.sub(" ", stripped)


def cooldown_multiplier(seconds_since_last_message: float | None, settings: XpSettings = DEFAULT_XP_SETTINGS) -> float:
    if seconds_since_last_message is None:
        return 1.0

    for threshold, multiplier in zip(settings.cooldown_thresholds_seconds, settings.cooldown_multipliers):
        if seconds_since_last_message < threshold:
            return multiplier

    return 1.0


def pair_multiplier(pair_streak: int, settings: XpSettings = DEFAULT_XP_SETTINGS) -> float:
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


def level_for_xp(total_xp: float, settings: XpSettings = DEFAULT_XP_SETTINGS) -> int:
    if total_xp <= 0:
        return 1
    return int(total_xp // settings.level_step_xp) + 1


def role_grant_due(previous_level: int, new_level: int, settings: XpSettings = DEFAULT_XP_SETTINGS) -> bool:
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
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_xp_events_lookup
        ON xp_events (guild_id, source, created_at, user_id)
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


def get_member_xp_state(conn: sqlite3.Connection, guild_id: int, user_id: int) -> MemberXpState:
    row = conn.execute(
        """
        SELECT total_xp, level, last_message_at, last_message_norm
        FROM member_xp
        WHERE guild_id = ? AND user_id = ?
        """,
        (guild_id, user_id),
    ).fetchone()
    if not row:
        return MemberXpState(total_xp=0.0, level=1, last_message_at=None, last_message_norm=None)

    return MemberXpState(
        total_xp=float(row["total_xp"]),
        level=int(row["level"]),
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
    settings: XpSettings = DEFAULT_XP_SETTINGS,
) -> AwardResult:
    state = get_member_xp_state(conn, guild_id, user_id)
    old_level = state.level
    new_total_xp = round(state.total_xp + max(0.0, xp_delta), 2)
    new_level = level_for_xp(new_total_xp, settings)
    last_message_at = state.last_message_at if message_timestamp is None else message_timestamp
    last_message_norm = state.last_message_norm if message_norm is None else message_norm

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
        (guild_id, user_id, new_total_xp, new_level, last_message_at, last_message_norm),
    )

    positive_xp = round(max(0.0, xp_delta), 2)
    if positive_xp > 0 and event_source:
        created_at = event_timestamp
        if created_at is None:
            created_at = message_timestamp if message_timestamp is not None else time.time()
        conn.execute(
            """
            INSERT INTO xp_events (guild_id, user_id, source, amount, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (guild_id, user_id, event_source, positive_xp, created_at),
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
) -> None:
    positive_amount = round(max(0.0, amount), 2)
    if positive_amount <= 0:
        return

    event_created_at = time.time() if created_at is None else created_at
    conn.execute(
        """
        INSERT INTO xp_events (guild_id, user_id, source, amount, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (guild_id, user_id, source, positive_amount, event_created_at),
    )


def is_message_processed(conn: sqlite3.Connection, guild_id: int, message_id: int) -> bool:
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


def update_pair_state(state: PairState | None, author_id: int) -> tuple[PairState, int]:
    if state is None or state.last_author_id is None:
        return PairState(last_author_id=author_id), 0

    if author_id == state.last_author_id:
        return PairState(last_author_id=author_id), 0

    pair = tuple(sorted((author_id, state.last_author_id)))
    if state.active_pair == pair:
        streak = state.alternating_streak + 1
    else:
        streak = 1

    return PairState(last_author_id=author_id, active_pair=pair, alternating_streak=streak), streak


def get_voice_session(conn: sqlite3.Connection, guild_id: int, user_id: int) -> VoiceSession | None:
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
    where = "guild_id = ? AND source = ?"
    if since_ts is not None:
        where += " AND created_at >= ?"
        params.append(since_ts)
    params.append(limit)

    rows = conn.execute(
        f"""
        SELECT user_id, ROUND(SUM(amount), 2) AS xp
        FROM xp_events
        WHERE {where}
        GROUP BY user_id
        ORDER BY xp DESC, user_id ASC
        LIMIT ?
        """,
        params,
    ).fetchall()

    return [
        LeaderboardEntry(
            user_id=int(row["user_id"]),
            xp=float(row["xp"]),
        )
        for row in rows
    ]


def get_user_xp_standing(
    conn: sqlite3.Connection,
    guild_id: int,
    source: str,
    user_id: int,
    *,
    since_ts: float | None = None,
) -> UserXpStanding:
    params: list[object] = [guild_id, source]
    where = "guild_id = ? AND source = ?"
    if since_ts is not None:
        where += " AND created_at >= ?"
        params.append(since_ts)

    rows = conn.execute(
        f"""
        SELECT user_id, ROUND(SUM(amount), 2) AS xp
        FROM xp_events
        WHERE {where}
        GROUP BY user_id
        ORDER BY xp DESC, user_id ASC
        """,
        params,
    ).fetchall()

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


def get_oldest_xp_event_timestamp(
    conn: sqlite3.Connection,
    guild_id: int,
    sources: tuple[str, ...] | None = None,
) -> float | None:
    params: list[object] = [guild_id]
    where = "guild_id = ?"

    if sources:
        placeholders = ", ".join("?" for _ in sources)
        where += f" AND source IN ({placeholders})"
        params.extend(sources)

    row = conn.execute(
        f"""
        SELECT MIN(created_at)
        FROM xp_events
        WHERE {where}
        """,
        params,
    ).fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0])
