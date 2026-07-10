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


# ── Mention-list capping ──────────────────────────────────────────────


def cap_mentions(
    ids: list[int] | set[int],
    *,
    max_count: int = 25,
) -> tuple[list[int], int]:
    """Cap an ID list to ``max_count``, returning ``(shown, overflow_count)``.

    Discord embed fields max out at 1024 characters. A field full of
    ``<@123456789012345678>`` mentions hits that ceiling around 25 entries,
    so the policy-vote embed has to truncate when the eligible roster grows
    past that cap. The caller then renders ``"+N more"`` for the overflow.

    Sorted so the output is deterministic regardless of input ordering — the
    same set always produces the same shown roster across embed refreshes.
    """
    sorted_ids = sorted(ids)
    if len(sorted_ids) <= max_count:
        return sorted_ids, 0
    return sorted_ids[:max_count], len(sorted_ids) - max_count


# ── Setup wizard metadata ─────────────────────────────────────────────


# Step → (title, description, config_key, select_kind, placeholder).
# ``select_kind`` is one of ``"role"``, ``"channel"``, ``"category"`` — the
# cog/commands layer picks the right UI element based on this. Decoupling
# the data from the View construction lets tests pin the wording without
# instantiating a discord.ui.View.
_SETUP_STEPS: dict[int, dict[str, str]] = {
    1: {
        "title": "Setup — Step 1/6",
        "description": "Which roles should have **moderator** access?",
        "config_key": "mod_role_ids",
        "select_kind": "role",
        "placeholder": "Select mod roles…",
    },
    2: {
        "title": "Setup — Step 2/6",
        "description": (
            "Which roles are **admin/senior staff**? "
            "(for escalations and warning alerts)"
        ),
        "config_key": "admin_role_ids",
        "select_kind": "role",
        "placeholder": "Select admin roles…",
    },
    3: {
        "title": "Setup — Step 3/6",
        "description": "Where should **jail channels** be created?",
        "config_key": "jail_category_id",
        "select_kind": "category",
        "placeholder": "Select jail category…",
    },
    4: {
        "title": "Setup — Step 4/6",
        "description": "Where should **ticket channels** be created?",
        "config_key": "ticket_category_id",
        "select_kind": "category",
        "placeholder": "Select ticket category…",
    },
    5: {
        "title": "Setup — Step 5/6",
        "description": "Where should **audit logs** be posted?",
        "config_key": "log_channel_id",
        "select_kind": "channel",
        "placeholder": "Select log channel…",
    },
    6: {
        "title": "Setup — Step 6/6",
        "description": (
            "Where should **transcripts** be posted? "
            "(can be the same as log channel)"
        ),
        "config_key": "transcript_channel_id",
        "select_kind": "channel",
        "placeholder": "Select transcript channel…",
    },
}

SETUP_FINAL_STEP = 6


def setup_step_meta(step: int) -> dict[str, str] | None:
    """Return the per-step metadata for the ``/setup`` wizard, or ``None`` when done.

    Returning ``None`` past the final step signals "no more wizard pages" —
    the caller then renders the "Setup Complete" embed. Returning ``None``
    (rather than raising) makes the loop in the cog straightforward:

        meta = setup_step_meta(step)
        if meta is None:
            ... render completion ...
    """
    if step < 1 or step > SETUP_FINAL_STEP:
        return None
    # Return a copy so callers can't mutate the module-level table.
    return dict(_SETUP_STEPS[step])


def setup_button_label(step: int) -> str:
    """Return the next-button label for the given step ("Next →" or "Finish")."""
    return "Finish" if step >= SETUP_FINAL_STEP else "Next →"


# ── Setup wizard: DM-delivered dropdown pagination ────────────────────

# Discord's native role/channel/category select menus auto-populate from the
# guild the interaction happens in — so they render empty in a DM. The DM
# wizard instead builds a plain StringSelect whose options we assemble by hand
# from the guild's roles/channels. Discord caps a select at 25 options, so a
# server with more than 25 roles (or channels) needs paging; these two pure
# helpers own that paging + cross-page accumulation so the View stays thin.

SETUP_PAGE_SIZE = 25  # Discord's hard cap on options in a single select menu
SETUP_MAX_ROLE_PICKS = 10  # cap on accumulated multi-select picks per role step


def paginate_setup_options(
    options: list[tuple[int, str]],
    page: int,
    *,
    per_page: int = SETUP_PAGE_SIZE,
) -> tuple[list[tuple[int, str]], int, int]:
    """Slice ``(id, label)`` *options* into one page for a DM setup dropdown.

    Returns ``(page_slice, clamped_page, total_pages)``. ``clamped_page`` is the
    input clamped into ``0..total_pages-1`` so paging past either end (pressing
    ▶ on the last page) is a no-op rather than an empty menu. An empty option
    list yields a single empty page (``total_pages == 1``) so callers can render
    a disabled "nothing to pick" select without special-casing.
    """
    total_pages = max(1, (len(options) + per_page - 1) // per_page)
    clamped = max(0, min(page, total_pages - 1))
    start = clamped * per_page
    return options[start : start + per_page], clamped, total_pages


def merge_setup_selection(
    accumulated: list[int],
    page_option_ids: list[int],
    selected_ids: list[int],
    *,
    max_picks: int = SETUP_MAX_ROLE_PICKS,
) -> list[int]:
    """Fold one page's multi-select result into the running selection.

    A StringSelect only reports the picks on the *current* page, so to let an
    admin choose roles spread across several pages the View carries the
    accumulated selection between page flips. Each time a page's selection
    changes we drop that page's option IDs from the accumulator and re-add
    whatever is now selected there — so deselecting on a page removes the pick
    while choices made on other pages survive.

    Insertion order is preserved (previously-kept first, then this page's picks)
    with duplicates collapsed, and the result is capped at ``max_picks`` to keep
    the stored config sane.
    """
    page_set = set(page_option_ids)
    kept = [i for i in accumulated if i not in page_set]
    result: list[int] = []
    seen: set[int] = set()
    for i in [*kept, *selected_ids]:
        if i not in seen:
            seen.add(i)
            result.append(i)
    return result[:max_picks]


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
