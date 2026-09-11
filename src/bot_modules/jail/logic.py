"""Pure jail business logic — no Discord API calls, no database access.

All functions here take and return plain Python primitives so they are
trivially unit-testable without fakes or mocks.
"""

from __future__ import annotations

import re
import time
from typing import Any

# Re-export duration helpers from moderation so tests import from one place
from bot_modules.services.moderation import fmt_duration, parse_duration  # noqa: F401


# ── Channel name sanitization ─────────────────────────────────────────

# Discord accepts only [a-z0-9_-] in channel names. Anything else gets
# squashed to a hyphen (then collapsed at the edges).
_CHANNEL_NAME_INVALID_RE = re.compile(r"[^a-z0-9_-]+")


def sanitize_channel_name(part: str, *, fallback: str = "user") -> str:
    """Reduce *part* to a string Discord will accept as (a piece of) a channel name.

    The cog formats jail/ticket/policy channels as e.g. ``jail-{name}-{ts}`` —
    if ``name`` contains uppercase, spaces, or symbols, Discord rejects the
    creation. This helper lowercases, replaces every invalid run with a single
    hyphen, and strips edge hyphens so the result never starts or ends with one.

    An empty input (or one made entirely of invalid characters) returns
    ``fallback`` so the cog always has a non-empty piece to interpolate.
    """
    cleaned = _CHANNEL_NAME_INVALID_RE.sub("-", part.lower()).strip("-")
    return cleaned or fallback


# ── Role snapshot / restore ───────────────────────────────────────────


def snapshot_roles(role_ids: list[int]) -> list[int]:
    """Return a copy of a member's role IDs suitable for storage."""
    return list(role_ids)


def restore_roles(stored: list[int], available_role_ids: set[int]) -> list[int]:
    """Filter stored role IDs to those still present in the guild."""
    return [rid for rid in stored if rid in available_role_ids]


# ── Jailed-role channel visibility ────────────────────────────────────
#
# Jail is a deny-list: a jailed member keeps @everyone, so they can see any
# channel @everyone can see unless that channel carries an explicit
# ``@Jailed → view_channel=False`` overwrite. Those overwrites are stamped when
# the Jailed role is first created — but a channel (or category) created later
# has none, and leaks to jailed members. These helpers decide which channels
# still need the deny so the cog can stamp new ones (on_guild_channel_create)
# and backfill any that already leaked (startup sweep).


def channel_needs_jail_deny(jailed_view_overwrite: bool | None) -> bool:
    """Return True if a channel still needs a ``view_channel=False`` Jailed deny.

    ``jailed_view_overwrite`` is the Jailed role's current ``view_channel``
    overwrite on the channel:

    - ``False`` — the deny is already in place; leave it (returns False).
    - ``True``  — explicitly *allowed*; the channel is exposed and must be
      overridden (returns True).
    - ``None``  — no overwrite, so the member inherits @everyone's visibility;
      the channel is exposed and needs the stamp (returns True).
    """
    return jailed_view_overwrite is not False


def channels_needing_jail_deny(
    channel_states: list[tuple[int, bool | None]],
) -> list[int]:
    """Filter ``(channel_id, jailed_view_overwrite)`` pairs to the exposed ids.

    Preserves input order so a caller iterating the guild's channels stamps
    them top-to-bottom. See :func:`channel_needs_jail_deny` for the per-channel
    rule.
    """
    return [cid for cid, view in channel_states if channel_needs_jail_deny(view)]


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
    if not tally["awaiting"] and not tally["no"] and len(tally["yes"]) == len(eligible):
        return "adopted"
    return "pending"


def vote_outcome(
    tally: dict[str, list[int]],
    eligible: set[int],
    *,
    expired: bool,
) -> str:
    """Return the vote outcome, accounting for an optional timeout.

    Pre-timeout (``expired=False``): while anyone in ``awaiting`` hasn't
    voted, the result is 'pending' regardless of how others voted. Once
    everyone has voted, any 'no' rejects; otherwise adopted.

    Post-timeout (``expired=True``): absentees in ``awaiting`` are dropped
    from the tally. Any 'no' still rejects. If nobody in ``eligible`` voted
    at all, the outcome is 'rejected_no_quorum'. Otherwise the remaining
    voters (yes + abstain) carry the vote and it is 'adopted'.

    Returns one of: 'adopted', 'rejected', 'rejected_no_quorum', 'pending'.
    """
    if expired:
        if tally["no"]:
            return "rejected"
        if not tally["yes"] and not tally["abstain"]:
            return "rejected_no_quorum"
        return "adopted"
    if tally["awaiting"]:
        return "pending"
    if tally["no"]:
        return "rejected"
    if len(tally["yes"]) == len(eligible):
        return "adopted"
    return "pending"


# ── Policy channel visibility ──────────────────────────────────────────
#
# A policy proposal is discussed by mods **or** opened to the general public,
# proposal by proposal (Billy, 2026-09-09). `/policy open` always starts it
# private; a mod opens it from the proposal card when a proposal wants member
# eyes on it. Opening grants @everyone view + history + send, so members can
# read the discussion that led here and join it — the point of opening is
# member input, and `/policy ballot` already covers a silent public vote.
#
# Opening is therefore not reversible in the way a permission change usually
# is: what members have read, they have read. Hence the confirm step, and
# hence the message count that makes the exposure concrete before the press.

POLICY_VISIBILITY_MODS = "mods"
POLICY_VISIBILITY_PUBLIC = "public"

#: Stop counting a channel's backlog here. A policy channel holding more
#: messages than this is unusual, and the exact figure stops mattering once
#: it is this large — "500+ messages" lands the same warning as "512".
POLICY_EXPOSURE_COUNT_CAP = 500


def normalize_policy_visibility(value: object) -> str:
    """Coerce a stored visibility to one of the two known states.

    Anything unrecognised — NULL from a row written before migration 219, a
    typo, a future state this build doesn't know — reads as mods-only. The
    private reading is the safe one: it under-reports what members can see,
    so the worst case is a button offering to open a channel that is already
    open, never a card claiming privacy it doesn't have.
    """
    return (
        POLICY_VISIBILITY_PUBLIC
        if value == POLICY_VISIBILITY_PUBLIC
        else POLICY_VISIBILITY_MODS
    )


def is_policy_public(value: object) -> bool:
    """True when members can see the policy channel."""
    return normalize_policy_visibility(value) == POLICY_VISIBILITY_PUBLIC


def toggle_policy_visibility(value: object) -> str:
    """The state the visibility button moves to from ``value``."""
    return (
        POLICY_VISIBILITY_MODS
        if is_policy_public(value)
        else POLICY_VISIBILITY_PUBLIC
    )


def policy_visibility_label(value: object) -> str:
    """Label for the toggle button, naming what pressing it *does*.

    Buttons here are named for the action, not the state: a card reading
    "Mods Only" would leave a mod guessing whether that is the situation or
    the offer.
    """
    return (
        "Make Mods-Only"
        if is_policy_public(value)
        else "Open to Members"
    )


def policy_visibility_status(value: object) -> str:
    """The proposal card's visibility line — the state, for a reader."""
    return (
        "🌐 Open to members"
        if is_policy_public(value)
        else "🔒 Mods only"
    )


def format_exposure_count(count: int, *, capped: bool = False) -> str:
    """Render a backlog size for the confirm prompt.

    ``capped`` means counting stopped at ``POLICY_EXPOSURE_COUNT_CAP`` rather
    than reaching the end of the channel, so the figure is a floor.
    """
    if capped:
        return f"{count}+ messages"
    if count == 1:
        return "1 message"
    return f"{count} messages"


def policy_exposure_warning(count: int, *, capped: bool = False) -> str:
    """The ephemeral confirm text shown before a channel is opened.

    Names the backlog explicitly. The failure this guards against is a mod
    opening a channel while thinking only of the proposal, forgetting the
    candid discussion above it — so the count leads, and "including anything
    said before now" is spelled out rather than implied.
    """
    return (
        f"⚠️ Opening this channel makes "
        f"{format_exposure_count(count, capped=capped)} readable by every "
        "member — including anything said before now. Members will also be "
        "able to post here.\n\n"
        "Opening cannot un-read what they see. Continue?"
    )
