"""Pure quest math — no discord, no database (spec §4).

The claim ``period`` model, the library slot rule, the rotate-pool cursor,
the reward bands, and the trigger-phrase matcher. Everything is deterministic
on its inputs so the ISO week boundaries, slot matrix, rotation cycling, and
phrase-boundary rules stay table-testable.
"""

from __future__ import annotations

import re
from datetime import date

# Library slot limits per guild: 1 active daily, up to 5 active weeklies,
# community goals are uncapped. Event quests are capped at 1 active PER
# TRIGGER KIND — the listener pays every active quest matching its trigger,
# so two same-kind actives would double-pay one occurrence.
MAX_ACTIVE_DAILY = 1
MAX_ACTIVE_WEEKLY = 5
MAX_ACTIVE_EVENT_PER_KIND = 1

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
}


# Suggested reward bands per quest type (community is judged by the author).
_REWARD_BANDS: dict[str, tuple[int, int]] = {
    "daily": (10, 20),
    "weekly": (25, 75),
}


def iso_week_for(local_day: str) -> str:
    """Return the ISO week ("YYYY-Www") a guild-local calendar day falls in.

    Uses the ISO year from ``date.isocalendar()``, not the calendar year, so
    the year-rollover boundary is correct — 2026-12-31 is 2027-W01 and
    2027-01-01 can be 2026-W53.
    """
    iso = date.fromisoformat(local_day).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


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
