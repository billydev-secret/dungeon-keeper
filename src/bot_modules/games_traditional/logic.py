"""Pure decision logic for the Truth-or-Dare (traditional) cog.

All functions here take and return plain Python values so they're unit-
testable without spinning up Discord. The cog calls these from inside
its button callbacks and modal handlers; the Discord glue (sending the
message, persisting via ``modify_payload``) stays in the cog.

The high-leverage piece is :func:`select_next_question_target` — it
implements the cog's least-asked weighting and tiebreak in one place so
the same shape can be reused by sibling game cogs (nhie, wyr, hottakes,
ttl, clapback, ama) that all pick a target weighted by prior turns.

Toggle helpers (:func:`toggle_pref`, :func:`record_asked`) are pure dict
transforms that the cog's ``modify_payload`` closures delegate to. They
mutate the payload in place and return a small piece of metadata the
cog feeds back to the user (e.g. "added"/"removed"). This is the spine
the other 18 game cogs can copy: every one of them has the same
``_toggle`` closure shape.
"""

from __future__ import annotations

import random
from typing import Any

CATEGORIES: tuple[str, ...] = ("sfw_truth", "sfw_dare", "nsfw_truth", "nsfw_dare")
CAT_LABELS: dict[str, str] = {
    "sfw_truth": "SFW Truth",
    "sfw_dare": "SFW Dare",
    "nsfw_truth": "NSFW Truth",
    "nsfw_dare": "NSFW Dare",
}


def toggle_pref(
    payload: dict[str, Any], user_id: int, category: str
) -> str:
    """Toggle ``category`` in ``user_id``'s preference list inside ``payload``.

    Mutates ``payload`` in place: ensures ``participants`` and ``prefs``
    keys exist, adds the user to ``participants`` on first preference,
    appends or removes the category on the user's prefs list, and drops
    the user from ``participants`` entirely when their last preference
    is removed (so an opted-out player isn't still shown in the lobby).

    Returns ``"added"`` or ``"removed"`` so the caller can echo back the
    action in an ephemeral reply.
    """
    str_id = str(user_id)
    participants: list[int] = payload.setdefault("participants", [])
    prefs: dict[str, list[str]] = payload.setdefault("prefs", {})

    if user_id not in participants:
        participants.append(user_id)

    user_prefs = prefs.setdefault(str_id, [])
    if category in user_prefs:
        user_prefs.remove(category)
        if not user_prefs:
            participants.remove(user_id)
            del prefs[str_id]
        return "removed"
    user_prefs.append(category)
    return "added"


def record_asked(
    payload: dict[str, Any], target_id: str, category: str, question: str
) -> None:
    """Record that ``question`` was asked to ``target_id`` in ``category``.

    Mutates ``payload`` in place. The key is ``"<target_id>:<category>"``
    so each (player, category) pair is recorded at most once — matching
    the cog's "no duplicate (user, category) questions" rule.
    """
    asked: dict[str, str] = payload.setdefault("asked", {})
    asked[f"{target_id}:{category}"] = question


def available_targets(
    prefs: dict[str, list[str]], asked: dict[str, str]
) -> list[tuple[str, str]]:
    """Return ``(user_id, category)`` pairs that have not been asked yet.

    For each participant's declared preferences, filter out any
    ``(user, category)`` combinations already recorded in ``asked``.
    Returned in iteration order of ``prefs`` (stable for Python 3.7+).
    """
    out: list[tuple[str, str]] = []
    for user_id, user_cats in prefs.items():
        for cat in user_cats:
            key = f"{user_id}:{cat}"
            if key not in asked:
                out.append((user_id, cat))
    return out


def asked_counts_by_user(asked: dict[str, str]) -> dict[str, int]:
    """Return how many questions each user has been asked.

    Used to weight selection toward the player who's been asked the
    least so often, so one chatty target doesn't soak up every turn.
    """
    counts: dict[str, int] = {}
    for key in asked:
        user_id, _ = key.rsplit(":", 1)
        counts[user_id] = counts.get(user_id, 0) + 1
    return counts


def select_next_question_target(
    prefs: dict[str, list[str]],
    asked: dict[str, str],
    rng: random.Random | None = None,
) -> tuple[str, str] | None:
    """Pick the next ``(user_id, category)`` to ask.

    Implements the cog's selection rule:

    1. Build the list of available (player, category) pairs.
    2. Look up each candidate's total asked-count.
    3. Keep only candidates whose player has the minimum asked-count.
    4. Choose one of the remainder uniformly at random.

    Returns ``None`` when no eligible pair exists (either no prefs or
    every combination has already been asked). ``rng`` is injected so
    tests can pin the tiebreak; defaults to the module ``random``.
    """
    available = available_targets(prefs, asked)
    if not available:
        return None

    counts = asked_counts_by_user(asked)
    candidate_counts = {uid: counts.get(uid, 0) for uid, _ in available}
    min_count = min(candidate_counts.values())
    least_asked = [(uid, cat) for uid, cat in available if candidate_counts[uid] == min_count]

    chooser = rng if rng is not None else random
    return chooser.choice(least_asked)


def summarize_asked_by_category(asked: dict[str, str]) -> dict[str, int]:
    """Count questions asked per known category.

    Returns a dict keyed by every category in :data:`CATEGORIES` (zero
    when none asked) plus any unknown categories observed in ``asked``.
    Unknowns are tracked so a stale payload — e.g. produced before a
    category was renamed — still surfaces in the game-over recap.
    """
    by_cat: dict[str, int] = {cat: 0 for cat in CATEGORIES}
    for key in asked:
        _, cat = key.rsplit(":", 1)
        by_cat[cat] = by_cat.get(cat, 0) + 1
    return by_cat


def question_pool_size(prefs: dict[str, list[str]], asked: dict[str, str]) -> int:
    """Total number of distinct ``(player, category)`` questions in play.

    This is the denominator for the "X / Y asked" progress report: every
    preference combo currently declared, unioned with anything already
    asked. The union keeps the total ``>= len(asked)`` even if a player
    drops a preference after being asked that category, so the progress
    never reads as more-asked-than-possible.
    """
    pool = {f"{uid}:{cat}" for uid, cats in prefs.items() for cat in cats}
    pool |= set(asked.keys())
    return len(pool)
