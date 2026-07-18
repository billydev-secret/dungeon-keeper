"""Pure Chat Revive ("Ember") math — no discord, no database.

Band bucketing, per-band gap statistics learned from raw message timestamps,
the full fire/refuse gate chain, and question-selection weighting. Everything
takes plain values (``now_ts`` injected) so the lull rules stay table-testable,
and the monitor loop and the ``/revive check`` preview share one ``decide()``
so the explanation can never disagree with what the loop would actually do.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

BAND_HOURS = 2
BANDS_PER_DAY = 24 // BAND_HOURS
DAY_BAND = -1  # whole-day fallback profile for sparse bands

PROFILE_WINDOW_DAYS = 60
COLD_START_DAYS = 14.0

# Session-gap ("conversation") model. Segment a band's message stream into
# conversations at a SESSION_GAP_SECONDS silence, then fire when the current
# silence exceeds a high quantile of the *between-conversation* gaps. This
# measures "a conversation ended and didn't restart", not "a message wasn't
# followed quickly" — the latter is dominated by tiny intra-burst gaps and so
# fires far too eagerly on chatty channels (a busy evening's median inter-
# message gap is ~1min, and 4x that is a 4-minute hair-trigger).
SESSION_GAP_SECONDS = 600.0  # a silence longer than this ends a conversation
INTERSESSION_QUANTILE = 0.90  # fire past the 90th-pct between-conversation lull
MIN_LULL_SECONDS = 900.0  # absolute floor; never revive a still-warm channel
MIN_BAND_SESSIONS = 8  # fewer sampled conversation gaps -> use the whole-day profile

FALLBACK_SILENCE_SECONDS = 6 * 3600.0
FALLBACK_START_HOUR = 10
FALLBACK_END_HOUR = 22

LIVENESS_FRACTION = 0.2  # of the channel's busiest band
LIVENESS_MIN_PER_DAY = 5.0

ANTI_REPEAT_DAYS = 30
FOLLOW_WINDOW_SECONDS = 1800.0
SUCCESS_MIN_MSGS = 3
SUCCESS_MIN_AUTHORS = 2
# Defaults for the per-guild ping dials (revive_guild_config). Kept in sync
# with migration 076 and the dashboard/route defaults.
DEFAULT_PING_MAX_PER_DAY = 3
DEFAULT_PING_COOLDOWN_MINUTES = 60

FLOURISHES = (
    "*stirring the coals…*",
    "*poking the fire…*",
    "*tossing a log on…*",
    "*fanning the embers…*",
    "*a spark drifts by…*",
)


def local_hour(ts: float, offset_hours: float) -> int:
    """Guild-local hour of day (0-23) for an epoch time."""
    tz = timezone(timedelta(hours=offset_hours))
    return datetime.fromtimestamp(ts, tz).hour


def band_of(ts: float, offset_hours: float) -> int:
    """Which 2-hour local band an epoch time falls in (0-11)."""
    return local_hour(ts, offset_hours) // BAND_HOURS


def band_label(band: int) -> str:
    """Human label for a band, e.g. ``18:00-20:00`` (``all day`` for DAY_BAND)."""
    if band == DAY_BAND:
        return "all day"
    return f"{band * BAND_HOURS:02d}:00–{(band + 1) * BAND_HOURS:02d}:00"


def _percentile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile over an ascending list (empty -> 0)."""
    if not sorted_values:
        return 0.0
    idx = round(q * (len(sorted_values) - 1))
    return sorted_values[idx]


@dataclass(frozen=True)
class BandProfile:
    band: int
    fire_threshold: float  # silence (s) that marks an unusual lull for this band
    sessions_per_day: float  # between-conversation gaps seen per calendar day
    msgs_per_day: float
    session_count: int  # number of between-conversation gaps the threshold used


def _session_threshold(gaps: list[float]) -> tuple[float, int]:
    """Fire threshold + sample size from a band's consecutive-message gaps.

    Intra-conversation gaps (``<= SESSION_GAP_SECONDS``) are discarded — what
    marks a lull is how long the channel stays quiet *between* conversations,
    not the seconds between replies inside one. The threshold is the high
    quantile of those between-conversation gaps, floored by ``MIN_LULL_SECONDS``
    so a still-warm channel is never revived. Returns ``(MIN_LULL_SECONDS, 0)``
    when the band has no between-conversation gaps yet.
    """
    inter = sorted(g for g in gaps if g > SESSION_GAP_SECONDS)
    if not inter:
        return MIN_LULL_SECONDS, 0
    threshold = max(_percentile(inter, INTERSESSION_QUANTILE), MIN_LULL_SECONDS)
    return threshold, len(inter)


def compute_band_profiles(
    timestamps: list[float],
    *,
    now_ts: float,
    offset_hours: float,
    window_days: int = PROFILE_WINDOW_DAYS,
) -> dict[int, BandProfile]:
    """Learn a channel's rhythm from ascending message timestamps.

    Each gap between consecutive messages is attributed to the band the gap
    *starts* in (the band whose lull it is), then segmented into conversations:
    a band's fire threshold is a high quantile of its between-conversation gaps
    (see ``_session_threshold``). Returns one profile per band that saw any
    messages, plus a DAY_BAND profile over everything — the fallback for bands
    with too few sampled conversation gaps to trust.
    """
    cutoff = now_ts - window_days * 86400.0
    ts = [t for t in timestamps if t >= cutoff]
    if not ts:
        return {}

    observed_days = max((now_ts - ts[0]) / 86400.0, 1.0)
    band_gaps: dict[int, list[float]] = {}
    band_counts: dict[int, int] = {}
    all_gaps: list[float] = []

    for i, t in enumerate(ts):
        band = band_of(t, offset_hours)
        band_counts[band] = band_counts.get(band, 0) + 1
        if i + 1 < len(ts):
            gap = ts[i + 1] - t
            band_gaps.setdefault(band, []).append(gap)
            all_gaps.append(gap)

    profiles: dict[int, BandProfile] = {}
    for band, count in band_counts.items():
        threshold, sessions = _session_threshold(band_gaps.get(band, []))
        profiles[band] = BandProfile(
            band=band,
            fire_threshold=threshold,
            sessions_per_day=sessions / observed_days,
            msgs_per_day=count / observed_days,
            session_count=sessions,
        )
    threshold, sessions = _session_threshold(all_gaps)
    profiles[DAY_BAND] = BandProfile(
        band=DAY_BAND,
        fire_threshold=threshold,
        sessions_per_day=sessions / observed_days,
        msgs_per_day=len(ts) / observed_days,
        session_count=sessions,
    )
    return profiles


def is_quiet_hours(hour: int, quiet_start: int, quiet_end: int) -> bool:
    """Whether a local hour falls in the [quiet_start, quiet_end) window.

    The window may wrap midnight (e.g. 22 -> 6). start == end disables it.
    """
    if quiet_start == quiet_end:
        return False
    if quiet_start < quiet_end:
        return quiet_start <= hour < quiet_end
    return hour >= quiet_start or hour < quiet_end


@dataclass(frozen=True)
class GateInputs:
    """Everything ``decide()`` needs, gathered by the caller."""

    now_ts: float
    offset_hours: float
    guild_enabled: bool
    channel_enabled: bool
    busy: bool
    slowmode_delay: int
    quiet_start: int
    quiet_end: int
    revives_today: int
    daily_budget: int
    last_guild_revive_ts: float | None
    guild_gap_minutes: float
    last_channel_revive_ts: float | None
    rest_hours: float
    human_spoke_since_revive: bool
    last_human_ts: float | None
    history_days: float
    fire_multiplier: float
    profiles: dict[int, BandProfile]


@dataclass(frozen=True)
class Verdict:
    """The fire decision plus the plain-language why (for /revive check)."""

    fire: bool
    reason: str
    mode: str = ""  # "rhythm" | "fallback" | ""
    band: int | None = None
    silence_s: float = 0.0
    threshold_s: float = 0.0


def _fmt_duration(seconds: float) -> str:
    if seconds >= 86400:
        return f"{seconds / 86400:.1f}d"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    return f"{max(seconds, 0) / 60:.0f}m"


def decide(g: GateInputs) -> Verdict:
    """Run the full gate chain; the first blocking protection wins.

    Ordering follows the spec's protections table: cheap config gates first,
    then frequency protections, then the rhythm judgment itself.
    """
    if not g.guild_enabled:
        return Verdict(False, "Chat Revive is not enabled for this server.")
    if not g.channel_enabled:
        return Verdict(False, "This channel is not enabled for revives.")
    if g.busy:
        return Verdict(False, "A game or event is active in this channel.")
    if g.slowmode_delay > 0:
        return Verdict(False, "The channel is in slowmode — mods slowed the room.")

    hour = local_hour(g.now_ts, g.offset_hours)
    if is_quiet_hours(hour, g.quiet_start, g.quiet_end):
        return Verdict(
            False,
            f"Quiet hours ({g.quiet_start:02d}:00–{g.quiet_end:02d}:00 local).",
        )
    if g.revives_today >= g.daily_budget:
        return Verdict(
            False,
            f"The server's daily budget ({g.daily_budget}) is spent for today.",
        )
    if g.last_guild_revive_ts is not None:
        since = g.now_ts - g.last_guild_revive_ts
        if since < g.guild_gap_minutes * 60:
            return Verdict(
                False,
                "Server breathing room: last revive anywhere was "
                f"{_fmt_duration(since)} ago (minimum {g.guild_gap_minutes:.0f}m).",
            )
    if g.last_channel_revive_ts is not None:
        since = g.now_ts - g.last_channel_revive_ts
        if since < g.rest_hours * 3600:
            return Verdict(
                False,
                f"Channel rest: revived {_fmt_duration(since)} ago "
                f"(rests {g.rest_hours:.0f}h).",
            )
        if not g.human_spoke_since_revive:
            return Verdict(
                False, "Nobody has spoken since the last revive — never chain."
            )
    if g.last_human_ts is None:
        return Verdict(False, "No message history for this channel yet.")

    silence = g.now_ts - g.last_human_ts

    if g.history_days < COLD_START_DAYS or not g.profiles:
        if not FALLBACK_START_HOUR <= hour < FALLBACK_END_HOUR:
            return Verdict(
                False,
                "Still learning this channel's rhythm — fallback mode only fires "
                f"{FALLBACK_START_HOUR}:00–{FALLBACK_END_HOUR}:00 local.",
                mode="fallback",
                silence_s=silence,
                threshold_s=FALLBACK_SILENCE_SECONDS,
            )
        if silence < FALLBACK_SILENCE_SECONDS:
            return Verdict(
                False,
                f"Fallback mode: quiet for {_fmt_duration(silence)}, needs "
                f"{_fmt_duration(FALLBACK_SILENCE_SECONDS)}.",
                mode="fallback",
                silence_s=silence,
                threshold_s=FALLBACK_SILENCE_SECONDS,
            )
        return Verdict(
            True,
            f"Fallback mode: quiet for {_fmt_duration(silence)} "
            f"(threshold {_fmt_duration(FALLBACK_SILENCE_SECONDS)}).",
            mode="fallback",
            silence_s=silence,
            threshold_s=FALLBACK_SILENCE_SECONDS,
        )

    band = band_of(g.now_ts, g.offset_hours)
    prof = g.profiles.get(band)
    if prof is None or prof.session_count < MIN_BAND_SESSIONS:
        prof = g.profiles.get(DAY_BAND)
    if prof is None or prof.session_count == 0:
        return Verdict(
            False,
            "Not enough activity history to judge a lull here.",
            mode="rhythm",
            band=band,
            silence_s=silence,
        )

    busiest = max(
        (p.msgs_per_day for p in g.profiles.values() if p.band != DAY_BAND),
        default=0.0,
    )
    floor = max(LIVENESS_MIN_PER_DAY, LIVENESS_FRACTION * busiest)
    band_rate = g.profiles[band].msgs_per_day if band in g.profiles else 0.0
    if band_rate < floor:
        return Verdict(
            False,
            f"This channel is normally quiet around now ({band_label(band)}: "
            f"~{band_rate:.1f} msgs/day vs a floor of {floor:.1f}).",
            mode="rhythm",
            band=band,
            silence_s=silence,
        )

    threshold = prof.fire_threshold * g.fire_multiplier
    if silence < threshold:
        return Verdict(
            False,
            f"Quiet for {_fmt_duration(silence)}, but a real lull here "
            f"({band_label(prof.band)}) starts at {_fmt_duration(threshold)}.",
            mode="rhythm",
            band=band,
            silence_s=silence,
            threshold_s=threshold,
        )
    return Verdict(
        True,
        f"Unusual lull: quiet for {_fmt_duration(silence)} — past the "
        f"{_fmt_duration(threshold)} that marks a real lull between "
        f"conversations here ({band_label(prof.band)}).",
        mode="rhythm",
        band=band,
        silence_s=silence,
        threshold_s=threshold,
    )


def should_ping(
    last_ping_ts: float | None,
    now_ts: float,
    pings_today: int,
    *,
    max_per_day: int,
    cooldown_seconds: float,
) -> bool:
    """Ping scarcity: at most `max_per_day` role-tags per channel per day, and
    no two pings within `cooldown_seconds` of each other. Both dials are
    per-guild (revive_guild_config); a cooldown of 0 means "cap only"."""
    if pings_today >= max_per_day:
        return False
    return last_ping_ts is None or now_ts - last_ping_ts >= cooldown_seconds


def question_weight(use_count: int, successes: int) -> float:
    """Beta-smoothed success rate — proven sparkers rise, duds fade."""
    return (successes + 1) / (use_count + 2)


def pick_weighted(ids: list[int], weights: list[float], rng: random.Random) -> int:
    """Weighted choice over question ids (weights already > 0)."""
    return rng.choices(ids, weights=weights, k=1)[0]


def revive_succeeded(follow_msgs: int, follow_authors: int) -> bool:
    """Did real conversation follow? (spec: within 30 minutes)."""
    return follow_msgs >= SUCCESS_MIN_MSGS and follow_authors >= SUCCESS_MIN_AUTHORS


def render_revive(
    question: str, *, role_id: int | None, flourish: str | None
) -> str:
    """The whole footprint: plain text, optional flourish, optional ping."""
    parts = ["\U0001f525"]
    if flourish:
        parts.append(flourish)
    if role_id is not None:
        parts.append(f"<@&{role_id}>")
    parts.append(question)
    return " ".join(parts)


def render_revive_caption(*, role_id: int | None, flourish: str | None) -> str:
    """Message text accompanying a rendered card — everything but the question.

    A role mention can't live inside an image, so the ping (and the flourish,
    which the dashboard toggle still governs) ride along as message content.
    Empty when there's neither: the card then posts bare. The 🔥 leads whenever
    there's a caption at all — it's the signature every revive post shares.
    """
    parts: list[str] = []
    if flourish:
        parts.append(flourish)
    if role_id is not None:
        parts.append(f"<@&{role_id}>")
    if not parts:
        return ""
    return " ".join(["\U0001f525", *parts])
