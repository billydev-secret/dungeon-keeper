"""Pure jail business logic — no Discord API calls, no database access.

All functions here take and return plain Python primitives so they are
trivially unit-testable without fakes or mocks.
"""

from __future__ import annotations

import time
from typing import Any

# Re-export duration helpers from moderation so tests import from one place
from services.moderation import fmt_duration, parse_duration  # noqa: F401


# ── Role snapshots ────────────────────────────────────────────────────

def snapshot_roles(role_ids: list[int]) -> list[int]:
    """Return a copy of a member's role IDs suitable for storage."""
    return list(role_ids)


def restore_roles(stored: list[int], available_role_ids: set[int]) -> list[int]:
    """Filter stored role IDs to those still present in the guild."""
    return [rid for rid in stored if rid in available_role_ids]


# ── Expiry checks ─────────────────────────────────────────────────────

def is_jail_expired(jail_row: dict[str, Any], now_ts: float | None = None) -> bool:
    """Return True if a jail row has passed its expiry time.

    A jail without an expires_at is indefinite and never expires.
    """
    expires_at = jail_row.get("expires_at")
    if expires_at is None:
        return False
    if now_ts is None:
        now_ts = time.time()
    return now_ts >= expires_at


def jail_duration_seconds(jail_row: dict[str, Any], now_ts: float | None = None) -> float:
    """Return elapsed seconds since the jail was created."""
    if now_ts is None:
        now_ts = time.time()
    return now_ts - jail_row["created_at"]


# ── Policy vote logic ─────────────────────────────────────────────────

def eligible_voters(
    members: list[dict[str, Any]],
    mod_role_ids: set[int],
    admin_role_ids: set[int],
) -> set[int]:
    """Return the set of user IDs eligible to vote on a policy.

    A member is eligible if they are:
      - an admin (has administrator permission), OR
      - have any mod or admin role

    members: list of dicts with keys 'user_id', 'is_bot', 'role_ids',
             'is_administrator'  (all plain ints/bools, no discord objects)
    """
    all_role_ids = mod_role_ids | admin_role_ids
    eligible: set[int] = set()
    for m in members:
        if m.get("is_bot"):
            continue
        if m.get("is_administrator"):
            eligible.add(m["user_id"])
            continue
        if all_role_ids & set(m.get("role_ids", [])):
            eligible.add(m["user_id"])
    return eligible


def tally_votes(
    vote_map: dict[int, str],
    eligible: set[int],
) -> dict[str, list[int]]:
    """Tally votes from eligible members.

    Returns dict with keys 'yes', 'no', 'abstain', 'awaiting'.
    """
    voted = set(vote_map.keys()) & eligible
    return {
        "yes": [uid for uid in voted if vote_map[uid] == "yes"],
        "no": [uid for uid in voted if vote_map[uid] == "no"],
        "abstain": [uid for uid in voted if vote_map[uid] == "abstain"],
        "awaiting": list(eligible - voted),
    }


def resolve_policy_vote(
    tally: dict[str, list[int]],
    eligible: set[int],
) -> str:
    """Return the vote outcome given a tally and eligible voter set.

    Rules:
    - 'adopted'  — all eligible voters voted yes (unanimous)
    - 'rejected' — any eligible voter voted no
    - 'pending'  — some eligible voters haven't voted yet and no 'no' votes

    Returns one of: 'adopted', 'rejected', 'pending'
    """
    if tally["no"]:
        return "rejected"
    # All eligible must have voted yes for adoption
    if not tally["awaiting"] and not tally["no"] and len(tally["yes"]) == len(eligible):
        return "adopted"
    return "pending"
