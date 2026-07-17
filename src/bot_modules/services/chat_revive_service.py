"""Chat Revive persistence — config, question bank, events, rhythm cache.

Everything here is synchronous SQLite over a caller-supplied connection
(callers on the event loop wrap calls in ``asyncio.to_thread``), and every
function takes ``now_ts`` explicitly so time-dependent behavior stays
deterministic in tests. The frequency gates all read from ``revive_events``
alone — there is no separate counter state to drift out of sync.
"""

from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import dataclass

from bot_modules.chat_revive.logic import (
    ANTI_REPEAT_DAYS,
    FOLLOW_WINDOW_SECONDS,
    PROFILE_WINDOW_DAYS,
    BandProfile,
    GateInputs,
    Verdict,
    compute_band_profiles,
    decide,
    pick_weighted,
    question_weight,
    revive_succeeded,
)
from bot_modules.chat_revive.starter_pack import STARTER_QUESTIONS
from bot_modules.core.db_utils import get_tz_offset_hours
from bot_modules.economy.logic import local_day_for

RHYTHM_MAX_AGE_SECONDS = 6 * 3600.0

KNOWN_CATEGORIES = (
    "general",
    "deep",
    "silly",
    "spicy",
    "photo",
    "music",
    "food",
    "gaming",
)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GuildConfig:
    guild_id: int
    enabled: bool = False
    role_id: int | None = None
    quiet_start: int = 0
    quiet_end: int = 8
    daily_budget: int = 3
    guild_gap_minutes: int = 90
    flourish_enabled: bool = True
    ping_max_per_day: int = 3
    ping_cooldown_minutes: int = 60


@dataclass(frozen=True)
class ChannelConfig:
    guild_id: int
    channel_id: int
    enabled: bool = True
    categories: tuple[str, ...] = ()
    ping_enabled: bool = False
    role_id_override: int | None = None
    rest_hours: float = 8.0
    fire_multiplier: float = 4.0


def get_guild_config(conn: sqlite3.Connection, guild_id: int) -> GuildConfig:
    row = conn.execute(
        "SELECT * FROM revive_guild_config WHERE guild_id = ?", (guild_id,)
    ).fetchone()
    if row is None:
        return GuildConfig(guild_id=guild_id)
    return GuildConfig(
        guild_id=guild_id,
        enabled=bool(row["enabled"]),
        role_id=row["role_id"],
        quiet_start=row["quiet_start"],
        quiet_end=row["quiet_end"],
        daily_budget=row["daily_budget"],
        guild_gap_minutes=row["guild_gap_minutes"],
        flourish_enabled=bool(row["flourish_enabled"]),
        ping_max_per_day=row["ping_max_per_day"],
        ping_cooldown_minutes=row["ping_cooldown_minutes"],
    )


def save_guild_config(conn: sqlite3.Connection, cfg: GuildConfig) -> None:
    conn.execute(
        """
        INSERT INTO revive_guild_config
            (guild_id, enabled, role_id, quiet_start, quiet_end,
             daily_budget, guild_gap_minutes, flourish_enabled,
             ping_max_per_day, ping_cooldown_minutes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
            enabled=excluded.enabled, role_id=excluded.role_id,
            quiet_start=excluded.quiet_start, quiet_end=excluded.quiet_end,
            daily_budget=excluded.daily_budget,
            guild_gap_minutes=excluded.guild_gap_minutes,
            flourish_enabled=excluded.flourish_enabled,
            ping_max_per_day=excluded.ping_max_per_day,
            ping_cooldown_minutes=excluded.ping_cooldown_minutes
        """,
        (
            cfg.guild_id,
            int(cfg.enabled),
            cfg.role_id,
            cfg.quiet_start,
            cfg.quiet_end,
            cfg.daily_budget,
            cfg.guild_gap_minutes,
            int(cfg.flourish_enabled),
            cfg.ping_max_per_day,
            cfg.ping_cooldown_minutes,
        ),
    )


def _channel_from_row(row: sqlite3.Row) -> ChannelConfig:
    return ChannelConfig(
        guild_id=row["guild_id"],
        channel_id=row["channel_id"],
        enabled=bool(row["enabled"]),
        categories=tuple(json.loads(row["categories"] or "[]")),
        ping_enabled=bool(row["ping_enabled"]),
        role_id_override=row["role_id_override"],
        rest_hours=row["rest_hours"],
        fire_multiplier=row["fire_multiplier"],
    )


def get_channel_config(
    conn: sqlite3.Connection, guild_id: int, channel_id: int
) -> ChannelConfig | None:
    row = conn.execute(
        "SELECT * FROM revive_channel_config WHERE guild_id = ? AND channel_id = ?",
        (guild_id, channel_id),
    ).fetchone()
    return _channel_from_row(row) if row else None


def save_channel_config(conn: sqlite3.Connection, cfg: ChannelConfig) -> None:
    conn.execute(
        """
        INSERT INTO revive_channel_config
            (guild_id, channel_id, enabled, categories, ping_enabled,
             role_id_override, rest_hours, fire_multiplier)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, channel_id) DO UPDATE SET
            enabled=excluded.enabled, categories=excluded.categories,
            ping_enabled=excluded.ping_enabled,
            role_id_override=excluded.role_id_override,
            rest_hours=excluded.rest_hours,
            fire_multiplier=excluded.fire_multiplier
        """,
        (
            cfg.guild_id,
            cfg.channel_id,
            int(cfg.enabled),
            json.dumps(list(cfg.categories)),
            int(cfg.ping_enabled),
            cfg.role_id_override,
            cfg.rest_hours,
            cfg.fire_multiplier,
        ),
    )


def list_enabled_channels(
    conn: sqlite3.Connection, guild_id: int | None = None
) -> list[ChannelConfig]:
    """Channels the bot was explicitly invited into (across guilds for the loop)."""
    if guild_id is None:
        rows = conn.execute(
            "SELECT * FROM revive_channel_config WHERE enabled = 1"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM revive_channel_config WHERE guild_id = ? AND enabled = 1",
            (guild_id,),
        ).fetchall()
    return [_channel_from_row(r) for r in rows]


def delete_channel_config(
    conn: sqlite3.Connection, guild_id: int, channel_id: int
) -> bool:
    """Un-invite a channel entirely (vs. enabled=0 which keeps its dials)."""
    cur = conn.execute(
        "DELETE FROM revive_channel_config WHERE guild_id = ? AND channel_id = ?",
        (guild_id, channel_id),
    )
    return (cur.rowcount or 0) > 0


def list_channel_configs(
    conn: sqlite3.Connection, guild_id: int
) -> list[ChannelConfig]:
    rows = conn.execute(
        "SELECT * FROM revive_channel_config WHERE guild_id = ? ORDER BY channel_id",
        (guild_id,),
    ).fetchall()
    return [_channel_from_row(r) for r in rows]


# --------------------------------------------------------------------------
# Question bank
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Question:
    id: int
    guild_id: int
    text: str
    category: str
    nsfw: bool
    active: bool
    created_by: int | None
    created_at: float
    use_count: int
    last_used_at: float | None


def _question_from_row(row: sqlite3.Row) -> Question:
    return Question(
        id=row["id"],
        guild_id=row["guild_id"],
        text=row["text"],
        category=row["category"],
        nsfw=bool(row["nsfw"]),
        active=bool(row["active"]),
        created_by=row["created_by"],
        created_at=row["created_at"],
        use_count=row["use_count"],
        last_used_at=row["last_used_at"],
    )


def add_question(
    conn: sqlite3.Connection,
    guild_id: int,
    text: str,
    *,
    category: str = "general",
    nsfw: bool = False,
    created_by: int | None,
    now_ts: float,
) -> int | None:
    """Insert one question; returns its id, or None for a duplicate/blank."""
    text = " ".join(text.split())
    if not text:
        return None
    dup = conn.execute(
        "SELECT 1 FROM revive_questions WHERE guild_id = ? AND lower(text) = lower(?)",
        (guild_id, text),
    ).fetchone()
    if dup:
        return None
    cur = conn.execute(
        """
        INSERT INTO revive_questions
            (guild_id, text, category, nsfw, active, created_by, created_at)
        VALUES (?, ?, ?, ?, 1, ?, ?)
        """,
        (guild_id, text, category, int(nsfw), created_by, now_ts),
    )
    return cur.lastrowid


def parse_bulk_line(line: str) -> tuple[str, bool, str] | None:
    """Parse one bulk-add line into (category, nsfw, text).

    ``deep: What ...`` tags a category; ``spicy,nsfw: What ...`` also flags
    adult-only. A prefix only counts when it looks like tags (all-lowercase
    words, no spaces) so questions containing a colon pass through untouched.
    """
    line = line.strip()
    if not line:
        return None
    category, nsfw = "general", False
    head, sep, rest = line.partition(":")
    head = head.strip()
    if sep and rest.strip() and head == head.lower() and " " not in head:
        tags = [t.strip() for t in head.split(",")]
        if all(t.isalpha() for t in tags):
            nsfw = "nsfw" in tags
            named = [t for t in tags if t != "nsfw"]
            if named:
                category = named[0]
            line = rest.strip()
    return category, nsfw, line


def bulk_add_questions(
    conn: sqlite3.Connection,
    guild_id: int,
    lines: list[str],
    *,
    created_by: int | None,
    now_ts: float,
) -> tuple[int, int]:
    """Add many questions (one per line); returns (added, skipped)."""
    added = skipped = 0
    for raw in lines:
        parsed = parse_bulk_line(raw)
        if parsed is None:
            continue
        category, nsfw, text = parsed
        if add_question(
            conn,
            guild_id,
            text,
            category=category,
            nsfw=nsfw,
            created_by=created_by,
            now_ts=now_ts,
        ):
            added += 1
        else:
            skipped += 1
    return added, skipped


def retire_question(conn: sqlite3.Connection, guild_id: int, question_id: int) -> bool:
    cur = conn.execute(
        "UPDATE revive_questions SET active = 0 WHERE guild_id = ? AND id = ?",
        (guild_id, question_id),
    )
    return (cur.rowcount or 0) > 0


def list_questions(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    category: str | None = None,
    include_retired: bool = False,
) -> list[Question]:
    sql = "SELECT * FROM revive_questions WHERE guild_id = ?"
    params: list[object] = [guild_id]
    if not include_retired:
        sql += " AND active = 1"
    if category:
        sql += " AND category = ?"
        params.append(category)
    sql += " ORDER BY category, id"
    return [_question_from_row(r) for r in conn.execute(sql, params).fetchall()]


def seed_starter_pack(conn: sqlite3.Connection, guild_id: int, now_ts: float) -> int:
    """Seed the shipped questions into an empty guild bank; returns count added."""
    existing = conn.execute(
        "SELECT 1 FROM revive_questions WHERE guild_id = ? LIMIT 1", (guild_id,)
    ).fetchone()
    if existing:
        return 0
    added = 0
    for category, nsfw, text in STARTER_QUESTIONS:
        if add_question(
            conn,
            guild_id,
            text,
            category=category,
            nsfw=bool(nsfw),
            created_by=None,
            now_ts=now_ts,
        ):
            added += 1
    return added


def _success_counts(conn: sqlite3.Connection, guild_id: int) -> dict[int, int]:
    rows = conn.execute(
        """
        SELECT question_id, COUNT(*) AS n FROM revive_events
        WHERE guild_id = ? AND success = 1 AND question_id IS NOT NULL
        GROUP BY question_id
        """,
        (guild_id,),
    ).fetchall()
    return {row["question_id"]: row["n"] for row in rows}


def pick_question(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    categories: tuple[str, ...],
    allow_nsfw: bool,
    now_ts: float,
    rng: random.Random | None = None,
) -> Question | None:
    """Weighted pick: proven sparkers favored, month-long anti-repeat honored.

    If every eligible question was used within the anti-repeat window, the
    picker refuses (returns None) rather than repeat — rare is powerful.
    """
    sql = "SELECT * FROM revive_questions WHERE guild_id = ? AND active = 1"
    params: list[object] = [guild_id]
    if not allow_nsfw:
        sql += " AND nsfw = 0"
    if categories:
        sql += f" AND category IN ({','.join('?' * len(categories))})"
        params.extend(categories)
    cutoff = now_ts - ANTI_REPEAT_DAYS * 86400.0
    sql += " AND (last_used_at IS NULL OR last_used_at < ?)"
    params.append(cutoff)

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return None
    questions = [_question_from_row(r) for r in rows]
    successes = _success_counts(conn, guild_id)
    weights = [question_weight(q.use_count, successes.get(q.id, 0)) for q in questions]
    ids = [q.id for q in questions]
    chosen = pick_weighted(ids, weights, rng or random.Random())
    return next(q for q in questions if q.id == chosen)


# --------------------------------------------------------------------------
# Events (the frequency-gate ledger) + measurement
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FrequencyState:
    revives_today: int
    last_guild_revive_ts: float | None
    last_channel_revive_ts: float | None
    last_ping_ts: float | None
    pings_today: int


def frequency_state(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    *,
    now_ts: float,
    offset_hours: float,
) -> FrequencyState:
    today = local_day_for(now_ts, offset_hours)
    revives_today = conn.execute(
        "SELECT COUNT(*) AS n FROM revive_events WHERE guild_id = ? AND local_day = ?",
        (guild_id, today),
    ).fetchone()["n"]
    last_guild = conn.execute(
        "SELECT MAX(created_at) AS ts FROM revive_events WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()["ts"]
    last_channel = conn.execute(
        "SELECT MAX(created_at) AS ts FROM revive_events "
        "WHERE guild_id = ? AND channel_id = ?",
        (guild_id, channel_id),
    ).fetchone()["ts"]
    last_ping = conn.execute(
        "SELECT MAX(created_at) AS ts FROM revive_events "
        "WHERE guild_id = ? AND channel_id = ? AND pinged = 1",
        (guild_id, channel_id),
    ).fetchone()["ts"]
    pings_today = conn.execute(
        "SELECT COUNT(*) AS n FROM revive_events "
        "WHERE guild_id = ? AND channel_id = ? AND local_day = ? AND pinged = 1",
        (guild_id, channel_id, today),
    ).fetchone()["n"]
    return FrequencyState(
        revives_today=revives_today,
        last_guild_revive_ts=last_guild,
        last_channel_revive_ts=last_channel,
        last_ping_ts=last_ping,
        pings_today=pings_today,
    )


def record_event(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    *,
    question_id: int | None,
    message_id: int | None,
    trigger_kind: str,
    pinged: bool,
    now_ts: float,
    offset_hours: float,
) -> int | None:
    """Record a successfully sent revive and bump the question's stats."""
    cur = conn.execute(
        """
        INSERT INTO revive_events
            (guild_id, channel_id, question_id, message_id, trigger_kind,
             pinged, local_day, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            guild_id,
            channel_id,
            question_id,
            message_id,
            trigger_kind,
            int(pinged),
            local_day_for(now_ts, offset_hours),
            now_ts,
        ),
    )
    if question_id is not None:
        conn.execute(
            "UPDATE revive_questions SET use_count = use_count + 1, "
            "last_used_at = ? WHERE id = ?",
            (now_ts, question_id),
        )
    return cur.lastrowid


def measure_due_events(conn: sqlite3.Connection, now_ts: float) -> int:
    """Fill follow-up stats for revives whose 30-minute window has closed.

    Counts human messages (processed_messages holds nothing else) in the
    window after each unmeasured revive; returns how many events were measured.
    """
    due = conn.execute(
        "SELECT id, guild_id, channel_id, created_at FROM revive_events "
        "WHERE measured_at IS NULL AND created_at + ? < ?",
        (FOLLOW_WINDOW_SECONDS, now_ts),
    ).fetchall()
    for ev in due:
        row = conn.execute(
            "SELECT COUNT(*) AS msgs, COUNT(DISTINCT user_id) AS authors "
            "FROM processed_messages WHERE guild_id = ? AND channel_id = ? "
            "AND created_at > ? AND created_at <= ?",
            (
                ev["guild_id"],
                ev["channel_id"],
                ev["created_at"],
                ev["created_at"] + FOLLOW_WINDOW_SECONDS,
            ),
        ).fetchone()
        success = revive_succeeded(row["msgs"], row["authors"])
        conn.execute(
            "UPDATE revive_events SET measured_at = ?, follow_msgs = ?, "
            "follow_authors = ?, success = ? WHERE id = ?",
            (now_ts, row["msgs"], row["authors"], int(success), ev["id"]),
        )
    return len(due)


# --------------------------------------------------------------------------
# Channel activity + rhythm cache
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelActivity:
    last_human_ts: float | None
    first_seen_ts: float | None
    history_days: float


def channel_activity(
    conn: sqlite3.Connection, guild_id: int, channel_id: int, *, now_ts: float
) -> ChannelActivity:
    row = conn.execute(
        "SELECT MIN(created_at) AS first_ts, MAX(created_at) AS last_ts "
        "FROM processed_messages WHERE guild_id = ? AND channel_id = ?",
        (guild_id, channel_id),
    ).fetchone()
    first, last = row["first_ts"], row["last_ts"]
    history_days = (now_ts - first) / 86400.0 if first is not None else 0.0
    return ChannelActivity(
        last_human_ts=last, first_seen_ts=first, history_days=history_days
    )


def refresh_rhythm(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    *,
    now_ts: float,
    offset_hours: float,
) -> dict[int, BandProfile]:
    """Recompute a channel's band profiles from raw history and cache them."""
    cutoff = now_ts - PROFILE_WINDOW_DAYS * 86400.0
    stamps = [
        row["created_at"]
        for row in conn.execute(
            "SELECT created_at FROM processed_messages "
            "WHERE guild_id = ? AND channel_id = ? AND created_at >= ? "
            "ORDER BY created_at",
            (guild_id, channel_id, cutoff),
        )
    ]
    profiles = compute_band_profiles(
        stamps, now_ts=now_ts, offset_hours=offset_hours
    )
    conn.execute(
        "DELETE FROM revive_channel_rhythm WHERE guild_id = ? AND channel_id = ?",
        (guild_id, channel_id),
    )
    conn.executemany(
        """
        INSERT INTO revive_channel_rhythm
            (guild_id, channel_id, band, median_gap, p90_gap,
             msgs_per_day, gap_count, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                guild_id,
                channel_id,
                p.band,
                p.median_gap,
                p.p90_gap,
                p.msgs_per_day,
                p.gap_count,
                now_ts,
            )
            for p in profiles.values()
        ],
    )
    return profiles


def load_rhythm(
    conn: sqlite3.Connection, guild_id: int, channel_id: int
) -> tuple[dict[int, BandProfile], float | None]:
    """Read the cached band profiles; returns ({}, None) when never computed."""
    rows = conn.execute(
        "SELECT * FROM revive_channel_rhythm WHERE guild_id = ? AND channel_id = ?",
        (guild_id, channel_id),
    ).fetchall()
    if not rows:
        return {}, None
    profiles = {
        row["band"]: BandProfile(
            band=row["band"],
            median_gap=row["median_gap"],
            p90_gap=row["p90_gap"],
            msgs_per_day=row["msgs_per_day"],
            gap_count=row["gap_count"],
        )
        for row in rows
    }
    return profiles, rows[0]["computed_at"]


def get_rhythm(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    *,
    now_ts: float,
    offset_hours: float,
    max_age_seconds: float = RHYTHM_MAX_AGE_SECONDS,
) -> dict[int, BandProfile]:
    """Cached profiles, recomputed from raw history when stale or missing."""
    profiles, computed_at = load_rhythm(conn, guild_id, channel_id)
    if computed_at is not None and now_ts - computed_at < max_age_seconds:
        return profiles
    return refresh_rhythm(
        conn, guild_id, channel_id, now_ts=now_ts, offset_hours=offset_hours
    )


# --------------------------------------------------------------------------
# Scoreboard
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class QuestionScore:
    question_id: int
    text: str
    uses: int
    successes: int


@dataclass(frozen=True)
class ChannelScore:
    channel_id: int
    revives: int
    successes: int
    measured: int


@dataclass(frozen=True)
class ReviveStats:
    total: int
    measured: int
    successes: int
    week_revives: int
    channels: tuple[ChannelScore, ...]  # last 30 days, busiest first
    top_questions: tuple[QuestionScore, ...]
    dud_questions: tuple[QuestionScore, ...]


def revive_stats(
    conn: sqlite3.Connection, guild_id: int, *, now_ts: float
) -> ReviveStats:
    """The scoreboard: how often we revive, how often it works, what carries."""
    totals = conn.execute(
        "SELECT COUNT(*) AS total, COUNT(measured_at) AS measured, "
        "COALESCE(SUM(success), 0) AS ok FROM revive_events WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()
    week = conn.execute(
        "SELECT COUNT(*) AS n FROM revive_events "
        "WHERE guild_id = ? AND created_at > ?",
        (guild_id, now_ts - 7 * 86400.0),
    ).fetchone()["n"]
    channels = tuple(
        ChannelScore(
            channel_id=r["channel_id"],
            revives=r["n"],
            successes=r["ok"],
            measured=r["m"],
        )
        for r in conn.execute(
            "SELECT channel_id, COUNT(*) AS n, COALESCE(SUM(success), 0) AS ok, "
            "COUNT(measured_at) AS m FROM revive_events "
            "WHERE guild_id = ? AND created_at > ? "
            "GROUP BY channel_id ORDER BY n DESC LIMIT 8",
            (guild_id, now_ts - 30 * 86400.0),
        )
    )
    ranked = conn.execute(
        "SELECT q.id, q.text, q.use_count, "
        "COALESCE(SUM(e.success), 0) AS ok "
        "FROM revive_questions q "
        "LEFT JOIN revive_events e "
        "  ON e.question_id = q.id AND e.guild_id = q.guild_id "
        "WHERE q.guild_id = ? AND q.use_count > 0 "
        "GROUP BY q.id "
        "ORDER BY (COALESCE(SUM(e.success), 0) + 1.0) / (q.use_count + 2.0) DESC",
        (guild_id,),
    ).fetchall()
    scores = [
        QuestionScore(
            question_id=r["id"], text=r["text"], uses=r["use_count"], successes=r["ok"]
        )
        for r in ranked
    ]
    return ReviveStats(
        total=totals["total"],
        measured=totals["measured"],
        successes=totals["ok"],
        week_revives=week,
        channels=channels,
        top_questions=tuple(scores[:5]),
        dud_questions=tuple(s for s in reversed(scores[-5:]) if s.successes == 0),
    )


# --------------------------------------------------------------------------
# The shared brain entry — one evaluation path for the loop and /revive check
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Evaluation:
    inputs: GateInputs
    verdict: Verdict
    guild_cfg: GuildConfig
    channel_cfg: ChannelConfig | None
    freq: FrequencyState
    offset_hours: float


def evaluate(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    *,
    now_ts: float,
    busy: bool,
    slowmode_delay: int,
) -> Evaluation:
    """Assemble every gate input from the DB and run ``decide()``.

    The discord-side facts (busy games, slowmode) are passed in by the caller;
    everything else — config, frequency ledger, activity, cached rhythm — is
    read here so the monitor loop and the ``/revive check`` preview can never
    disagree.
    """
    guild_cfg = get_guild_config(conn, guild_id)
    channel_cfg = get_channel_config(conn, guild_id, channel_id)
    offset = get_tz_offset_hours(conn, guild_id)
    act = channel_activity(conn, guild_id, channel_id, now_ts=now_ts)
    freq = frequency_state(
        conn, guild_id, channel_id, now_ts=now_ts, offset_hours=offset
    )
    profiles = get_rhythm(
        conn, guild_id, channel_id, now_ts=now_ts, offset_hours=offset
    )
    human_spoke = (
        freq.last_channel_revive_ts is None
        or (
            act.last_human_ts is not None
            and act.last_human_ts > freq.last_channel_revive_ts
        )
    )
    inputs = GateInputs(
        now_ts=now_ts,
        offset_hours=offset,
        guild_enabled=guild_cfg.enabled,
        channel_enabled=channel_cfg is not None and channel_cfg.enabled,
        busy=busy,
        slowmode_delay=slowmode_delay,
        quiet_start=guild_cfg.quiet_start,
        quiet_end=guild_cfg.quiet_end,
        revives_today=freq.revives_today,
        daily_budget=guild_cfg.daily_budget,
        last_guild_revive_ts=freq.last_guild_revive_ts,
        guild_gap_minutes=float(guild_cfg.guild_gap_minutes),
        last_channel_revive_ts=freq.last_channel_revive_ts,
        rest_hours=channel_cfg.rest_hours if channel_cfg else 8.0,
        human_spoke_since_revive=human_spoke,
        last_human_ts=act.last_human_ts,
        history_days=act.history_days,
        fire_multiplier=channel_cfg.fire_multiplier if channel_cfg else 4.0,
        profiles=profiles,
    )
    return Evaluation(
        inputs=inputs,
        verdict=decide(inputs),
        guild_cfg=guild_cfg,
        channel_cfg=channel_cfg,
        freq=freq,
        offset_hours=offset,
    )


__all__ = [
    "ChannelActivity",
    "ChannelConfig",
    "ChannelScore",
    "Evaluation",
    "FrequencyState",
    "GuildConfig",
    "KNOWN_CATEGORIES",
    "Question",
    "QuestionScore",
    "ReviveStats",
    "evaluate",
    "revive_stats",
    "add_question",
    "bulk_add_questions",
    "channel_activity",
    "frequency_state",
    "get_channel_config",
    "get_guild_config",
    "get_rhythm",
    "list_channel_configs",
    "list_enabled_channels",
    "list_questions",
    "load_rhythm",
    "measure_due_events",
    "parse_bulk_line",
    "pick_question",
    "record_event",
    "refresh_rhythm",
    "retire_question",
    "save_channel_config",
    "save_guild_config",
    "seed_starter_pack",
]
