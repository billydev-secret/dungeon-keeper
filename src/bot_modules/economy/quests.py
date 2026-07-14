"""Pure quest math — no discord, no database (spec §4).

The claim ``period`` model, the library slot rule, the rotate-pool cursor,
the reward bands, and the trigger-phrase matcher. Everything is deterministic
on its inputs so the ISO week boundaries, slot matrix, rotation cycling, and
phrase-boundary rules stay table-testable.
"""

from __future__ import annotations

import hashlib
import random
import re
from datetime import date

# Library slot limits per guild. Daily/weekly/monthly active quests form a
# per-cadence *pool*: each member is shown/paid a personal subset of N drawn
# from that pool per period (see assigned_quest_ids), so the caps are a
# sanity ceiling on pool size, not a hard "one active" rule. Community goals
# are uncapped. Event quests are capped at 1 active PER TRIGGER KIND — the
# listener pays every active quest matching its trigger, so two same-kind
# actives would double-pay one occurrence.
POOL_CAP = 25
MAX_ACTIVE_DAILY = POOL_CAP
MAX_ACTIVE_WEEKLY = POOL_CAP
MAX_ACTIVE_MONTHLY = POOL_CAP
MAX_ACTIVE_EVENT_PER_KIND = 1

# How many quests each member draws from each cadence's pool per period. The
# repeat gap for a member is ~floor(poolsize / N) periods, so a bigger pool
# (or a smaller N) spaces repeats further apart.
PERSONAL_BOARD_SIZE: dict[str, int] = {"daily": 2, "weekly": 2, "monthly": 2}

# Game/module triggers a quest can be auto-completed by (label = how the
# dashboard describes it). On an *event* quest the trigger pays per
# occurrence (period "<kind>:<occurrence>", no time gate); on a daily/weekly
# quest it auto-claims the ordinary calendar period — "do it once today/this
# week". The firing side lives with each module: the photo-reply listener in
# EconomyCog, the game-completion hooks in economy/game_rewards.py.
TRIGGER_KINDS: dict[str, str] = {
    "photo_reply": "Reply to a Photo Challenge card with a photo",
    "party_game": "Finish a party game",
    "duel": "Finish a duel / PvP challenge",
    "risky_roll": "Take a Risky Roll dare",
    "guess": "Play a Guess Who round",
    "voice_session": "Be active in voice chat",
    "qotd_reply": "Answer the Question of the Day",
    "starboard": "Get a message on the starboard",
    "invite": "Invite a new member",
    "boost": "Boost the server",
    "bio_set": "Set or update your bio",
    "media_post": "Post an image (optionally scoped to the trigger channel)",
    "pen_pal": "Get matched with a Pen Pal",
    "message_sent": "Send a message",
    "reply_sent": "Reply to someone's message",
    "reaction_given": "React to someone's message",
    "game_win": "Win a party game",
    "duel_win": "Win a duel / PvP challenge",
    "duel_lose": "Lose a duel / PvP challenge",
}

# Longer per-kind copy for the Income Sources page: what fires it and what
# the event-quest occurrence key means for repeat payouts.
TRIGGER_KIND_INFO: dict[str, str] = {
    "photo_reply": "A reply to a Photo Challenge card carrying an image. Event cadence: once per card.",
    "party_game": "Any party game completing with the member in the roster. Event cadence: once per game.",
    "duel": "A duel/PvP game resolving (chicken, hot potato, musical chairs, pressure cooker, quickdraw). Event cadence: once per match.",
    "risky_roll": "Pressing Roll in a Risky Rolls round. Event cadence: once per round.",
    "guess": "Submitting a scored guess in a Guess Who round. Event cadence: once per round.",
    "voice_session": "Earning voice-activity XP (being in VC, not idle-muted). Event cadence: once per guild-local day.",
    "qotd_reply": "Earning the QOTD reward (first message in the QOTD channel that day). Event cadence: once per question.",
    "starboard": "Having a message cross the starboard threshold. Event cadence: once per starred message.",
    "invite": "A member you invited joining the server. Event cadence: once per distinct invitee — alt-farmable, enable with care.",
    "boost": "Starting a server boost. Event cadence: once per day it is detected.",
    "bio_set": "Saving or updating your member bio. Event cadence: once ever.",
    "media_post": "Posting a message with an image attached; set a trigger channel to scope it (e.g. #art). Event cadence: once per message — use daily/weekly for this one.",
    "pen_pal": "Being paired into a Pen Pals session (both members fire). Event cadence: once per session.",
    "message_sent": "Any message in the server. Pair with a target count ('send 20 messages this week') — a target of 1 completes on the first message, and rewarding raw volume invites spam.",
    "reply_sent": "Using Discord's reply on someone ELSE's message (self-replies never count). Best with a target count.",
    "reaction_given": "Reacting to someone else's message — inherits the XP farm guard (one per message per reactor, ever; no self-reacts, no bots). Best with a target count.",
    "game_win": "Winning a party game (only types with a real winner resolve one: NHIE guiltiest, TTL best liar, Hot Takes hottest). Event cadence: once per game.",
    "duel_win": "Winning a duel/PvP match. Event cadence: once per match.",
    "duel_lose": "Not winning a duel/PvP match (every participant who wasn't the winner). Event cadence: once per match.",
}


# Suggested reward bands per quest type (community is judged by the author).
_REWARD_BANDS: dict[str, tuple[int, int]] = {
    "daily": (10, 20),
    "weekly": (25, 75),
    "monthly": (75, 200),
}


def iso_week_for(local_day: str) -> str:
    """Return the ISO week ("YYYY-Www") a guild-local calendar day falls in.

    Uses the ISO year from ``date.isocalendar()``, not the calendar year, so
    the year-rollover boundary is correct — 2026-12-31 is 2027-W01 and
    2027-01-01 can be 2026-W53.
    """
    iso = date.fromisoformat(local_day).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def month_for(local_day: str) -> str:
    """The calendar month ("YYYY-MM") a guild-local day falls in.

    Plain calendar months — a monthly quest's window opens on the 1st at
    guild-local midnight, no ISO-style shifting.
    """
    return local_day[:7]


def quest_period(qtype: str, local_day: str) -> str:
    """The claim period key for a quest type on a given guild-local day.

    Daily → the local day; weekly → its ISO week; community → the constant
    ``'once'`` (a community quest is claimed/settled once, not per period).
    Re-claimability falls straight out of this key — no reset sweeps.
    """
    if qtype == "daily":
        return local_day
    if qtype == "weekly":
        return iso_week_for(local_day)
    if qtype == "monthly":
        return month_for(local_day)
    if qtype == "community":
        return "once"
    # Event quests have no calendar period — the trigger listener supplies a
    # per-occurrence key (see occurrence_period), so a calendar lookup is a bug.
    raise ValueError(f"unknown quest type: {qtype!r}")


def occurrence_period(kind: str, occurrence: str) -> str:
    """The claim period key for one trigger occurrence on an *event* quest.

    Keyed to the occurrence (a photo card, one game, one duel …), not the
    calendar: each occurrence pays each member at most once, forever.
    """
    return f"{kind}:{occurrence}"


def can_activate(existing_active: list[str], qtype: str) -> bool:
    """True if activating one more ``qtype`` quest respects the slot rule.

    ``existing_active`` is the list of qtypes of the guild's currently-active
    quests (excluding the one under consideration). Community is uncapped.
    """
    if qtype == "daily":
        return existing_active.count("daily") < MAX_ACTIVE_DAILY
    if qtype == "weekly":
        return existing_active.count("weekly") < MAX_ACTIVE_WEEKLY
    if qtype == "monthly":
        return existing_active.count("monthly") < MAX_ACTIVE_MONTHLY
    if qtype == "community":
        return True
    if qtype == "event":
        # Callers gate event quests per trigger kind via can_activate_event;
        # type-level there is no cap (one photo + one duel event is fine).
        return True
    raise ValueError(f"unknown quest type: {qtype!r}")


def can_activate_event(existing_event_kinds: list[str], trigger_kind: str) -> bool:
    """True if activating one more event quest of this kind respects the cap.

    ``existing_event_kinds`` is the trigger kinds of the guild's currently
    active event quests (excluding the one under consideration). One active
    per kind — the listener pays every matching quest, so two same-kind
    actives would double-pay one occurrence.
    """
    return existing_event_kinds.count(trigger_kind) < MAX_ACTIVE_EVENT_PER_KIND


def pick_rotation(pool_ids: list[int], current_id: int | None) -> int | None:
    """The next quest id to activate when cycling a rotate-tag pool.

    Cycles by ascending id: the id after ``current_id`` wrapping around. A
    pool of one (or empty) has nowhere to rotate → None. When ``current_id``
    is not in the pool, start at the first id.
    """
    ordered = sorted(set(pool_ids))
    if len(ordered) <= 1:
        return None
    if current_id is None or current_id not in ordered:
        return ordered[0]
    idx = ordered.index(current_id)
    return ordered[(idx + 1) % len(ordered)]


def reward_band(qtype: str) -> tuple[int, int] | None:
    """The suggested (low, high) reward range for a quest type, or None.

    Advisory only — the dashboard warns out-of-band but saves anyway.
    Community has no band (author's call).
    """
    return _REWARD_BANDS.get(qtype)


# ── per-user quest board (spec §4.6) ──────────────────────────────────


def _seed(*parts: object) -> int:
    """A stable 64-bit seed from the parts — same across processes/versions.

    ``hash()`` is salted per-process and ``random.seed(str)`` isn't guaranteed
    stable, so we hash explicitly. Determinism is what makes the board a pure
    function of ``(user, period)`` with no stored assignment table.
    """
    digest = hashlib.sha256(":".join(str(p) for p in parts).encode()).hexdigest()
    return int(digest[:16], 16)


def period_index(qtype: str, local_day: str) -> int:
    """A monotonic integer index for the period ``local_day`` falls in.

    One integer per daily/weekly/monthly period, increasing over time — the
    board walks the per-user pool by this index, so a member's set advances
    exactly once per period and never mid-period (counted progress can't
    fragment). Community/event have no calendar period and raise.
    """
    d = date.fromisoformat(local_day)
    if qtype == "daily":
        return d.toordinal()
    if qtype == "weekly":
        iso = d.isocalendar()
        return iso.year * 53 + iso.week
    if qtype == "monthly":
        return d.year * 12 + (d.month - 1)
    raise ValueError(f"quest type has no board period: {qtype!r}")


def assigned_quest_ids(
    pool_ids: list[int], user_id: int, index: int, n: int
) -> list[int]:
    """The ``n`` quest ids a member draws from a cadence pool for a period.

    The pool is shuffled deterministically per member (so two members get
    different sets), then walked ``n``-at-a-time by ``index`` — a member can't
    see the same quest again until they've cycled the whole pool, so repeats
    are spaced ~``floor(len/n)`` periods apart. ``n >= len`` (or a tiny pool)
    degrades gracefully to "the whole pool". Returns sorted ids.
    """
    ordered = sorted(set(pool_ids))
    m = len(ordered)
    if m == 0 or n <= 0:
        return []
    if n >= m:
        return ordered
    # Per-member shuffle: order the pool by a per-(user, quest) hash.
    shuffled = sorted(ordered, key=lambda q: _seed(user_id, q))
    start = (index * n) % m
    picked = [shuffled[(start + i) % m] for i in range(n)]
    return sorted(picked)


def board_size(qtype: str) -> int:
    """How many quests a member draws from this cadence's pool per period."""
    return PERSONAL_BOARD_SIZE.get(qtype, 0)


def effective_target(
    target_count: int,
    target_min: int,
    target_max: int,
    *,
    user_id: int,
    quest_id: int,
    period: str,
) -> int:
    """A counted quest's target for one member+period.

    With a band (``0 < target_min < target_max``) the target is drawn from a
    Gaussian centred on the band, clamped to ``[min, max]`` — deterministic on
    ``(user, quest, period)`` so it's stable all period and varies run to run.
    Without a band it's the fixed ``target_count``. Never below 1.
    """
    if not (0 < target_min < target_max):
        return max(1, int(target_count))
    rng = random.Random(_seed(user_id, quest_id, period))
    mu = (target_min + target_max) / 2
    # ~95% of the mass lands inside the band before clamping; the tails clamp.
    sigma = (target_max - target_min) / 4 or 1
    draw = round(rng.gauss(mu, sigma))
    return max(target_min, min(target_max, draw))


# ── trigger-phrase verification (spec §4.4) ───────────────────────────


def parse_trigger_words(raw: str) -> list[str]:
    """Split a stored ``trigger_words`` value into clean phrases.

    Phrases are separated by commas or newlines; surrounding whitespace is
    stripped, internal runs of whitespace collapse to one space, and
    duplicates (case-insensitive) keep their first occurrence.
    """
    seen: set[str] = set()
    out: list[str] = []
    for chunk in re.split(r"[,\n]", raw or ""):
        phrase = " ".join(chunk.split())
        if not phrase:
            continue
        key = phrase.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(phrase)
    return out


def compile_trigger_pattern(words: list[str]) -> re.Pattern[str] | None:
    """One case-insensitive pattern matching any phrase as a whole word.

    ``(?<!\\w)…(?!\\w)`` instead of ``\\b`` so phrases that start or end with
    non-word characters (e.g. ``:wave:``) still anchor correctly, and "gm"
    never matches inside "dogma". Whitespace inside a phrase matches any
    whitespace run. None when there are no phrases.
    """
    if not words:
        return None
    alternatives = [
        r"\s+".join(re.escape(token) for token in phrase.split())
        for phrase in words
    ]
    return re.compile(
        r"(?<!\w)(?:" + "|".join(alternatives) + r")(?!\w)",
        re.IGNORECASE,
    )


def message_matches_trigger(content: str, pattern: re.Pattern[str] | None) -> bool:
    """True when a message body contains one of the quest's trigger phrases."""
    return bool(pattern is not None and content and pattern.search(content))
