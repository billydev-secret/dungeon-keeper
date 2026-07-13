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
# community goals are uncapped. Event quests are capped at 1 active — the
# listener pays whichever event quest matches its trigger, so two active at
# once would double-pay; the cap becomes per-trigger-kind if more kinds land.
MAX_ACTIVE_DAILY = 1
MAX_ACTIVE_WEEKLY = 5
MAX_ACTIVE_EVENT = 1

# Trigger kinds an event quest can be paid by (v1: replying to a Photo
# Challenge card with an image attached).
EVENT_TRIGGER_KINDS = ("photo_reply",)

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
    # per-occurrence key (see photo_card_period), so a calendar lookup is a bug.
    raise ValueError(f"unknown quest type: {qtype!r}")


def photo_card_period(game_id: str) -> str:
    """The claim period key for one Photo Challenge card.

    Keyed to the card (not the calendar): each posted card pays each member
    at most once, forever — replies to old cards still count.
    """
    return f"photo:{game_id}"


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
        return existing_active.count("event") < MAX_ACTIVE_EVENT
    raise ValueError(f"unknown quest type: {qtype!r}")


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
