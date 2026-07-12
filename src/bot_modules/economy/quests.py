"""Pure quest math — no discord, no database (spec §4).

The claim ``period`` model, the library slot rule, the rotate-pool cursor,
and the reward bands. Everything is deterministic on its inputs so the ISO
week boundaries, slot matrix, and rotation cycling stay table-testable.
"""

from __future__ import annotations

from datetime import date

# Library slot limits per guild: 1 active daily, up to 5 active weeklies,
# community goals are uncapped.
MAX_ACTIVE_DAILY = 1
MAX_ACTIVE_WEEKLY = 5

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
    raise ValueError(f"unknown quest type: {qtype!r}")


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
