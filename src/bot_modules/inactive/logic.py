"""Pure inactive-sweep logic — no Discord API calls, no database access.

The auto-sweep is a destructive, hard-to-reverse mass role-strip, so the
decision of *who* gets swept lives here in a single, trivially unit-testable
function. The cog and the background loop both route through it, and tests pin
the exclusion rules (bots/mods/admins/owner/recent-joiners) and the safety cap.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SweepCandidate:
    """A member the sweep proposes to move to the inactive channel."""

    user_id: int
    last_seen: float  # epoch seconds; the more recent of last-message / join
    idle_seconds: float


def select_sweep_candidates(
    *,
    last_seen: dict[int, float],
    now: float,
    threshold_seconds: float,
    exclude_ids: set[int],
    cap: int,
) -> tuple[list[SweepCandidate], int]:
    """Decide which members should be swept into the inactive channel.

    ``last_seen`` maps ``user_id -> epoch seconds`` of the member's most recent
    activity signal (the caller should pass ``max(last_message_ts, joined_at)``
    so a brand-new member who hasn't posted isn't treated as ancient). Any
    member whose ID is in ``exclude_ids`` — bots, mods, admins, the owner, and
    anyone already inactive — is skipped entirely.

    A member is a candidate when ``now - last_seen >= threshold_seconds``.
    Results are sorted most-idle-first (so the cap keeps the stalest members)
    and truncated to ``cap``.

    Returns ``(candidates, overflow)`` where ``overflow`` is how many eligible
    members were dropped by the cap — the caller surfaces this so a silent
    truncation never reads as "swept everyone".
    """
    if threshold_seconds <= 0 or cap <= 0:
        return [], 0

    eligible: list[SweepCandidate] = []
    for user_id, seen in last_seen.items():
        if user_id in exclude_ids:
            continue
        idle = now - seen
        if idle >= threshold_seconds:
            eligible.append(
                SweepCandidate(user_id=user_id, last_seen=seen, idle_seconds=idle)
            )

    # Most-idle first, tie-break on user_id for a deterministic order.
    eligible.sort(key=lambda c: (-c.idle_seconds, c.user_id))

    if len(eligible) <= cap:
        return eligible, 0
    return eligible[:cap], len(eligible) - cap
