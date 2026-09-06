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
from collections.abc import Iterable
from typing import Any

CATEGORIES: tuple[str, ...] = ("sfw_truth", "sfw_dare", "nsfw_truth", "nsfw_dare")
CAT_LABELS: dict[str, str] = {
    "sfw_truth": "SFW Truth",
    "sfw_dare": "SFW Dare",
    "nsfw_truth": "NSFW Truth",
    "nsfw_dare": "NSFW Dare",
}


def toggle_pref(
    payload: dict[str, Any], user_id: int, category: str, single_choice: bool = False
) -> str:
    """Toggle ``category`` in ``user_id``'s preference list inside ``payload``.

    Mutates ``payload`` in place: ensures ``participants`` and ``prefs``
    keys exist, adds the user to ``participants`` on first preference,
    appends or removes the category on the user's prefs list, and drops
    the user from ``participants`` entirely when their last preference
    is removed (so an opted-out player isn't still shown in the lobby).

    When ``single_choice`` is true the four categories behave like radio
    buttons: picking a new category while another is already selected
    replaces the old one, so each player holds exactly one preference.
    Tapping the already-selected category still deselects it (leaving the
    player with none), and a player's first pick is an ordinary add.

    Returns ``"added"``, ``"removed"``, or — only in single-choice mode
    when an existing pick was replaced — ``"switched"``, so the caller
    can echo back the action in an ephemeral reply.
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
    if single_choice and user_prefs:
        user_prefs.clear()
        user_prefs.append(category)
        return "switched"
    user_prefs.append(category)
    return "added"


# ── Passes ──────────────────────────────────────────────────────────
# A pass is one sweep through every (player, category) pair. The game used to
# be one-shot: once each pair had been asked, ``select_next_question_target``
# returned None and the host was told everything had been asked with nothing
# to do next (trivia-tail-89). Now the host's next Ask rolls the game into a
# second pass and the pairs open up again. Pass 1 keys stay ``"<uid>:<cat>"``
# so every payload written before passes existed still reads; pass ``n > 1``
# keys carry the pass as a third segment, ``"<uid>:<cat>:<n>"``.

FIRST_PASS = 1


def current_pass(payload: dict[str, Any]) -> int:
    """The pass the game is on — 1 for every payload that never rolled over."""
    try:
        return max(FIRST_PASS, int(payload.get("pass", FIRST_PASS)))
    except (TypeError, ValueError):
        return FIRST_PASS


def asked_key(target_id: str, category: str, pass_no: int = FIRST_PASS) -> str:
    """The ``asked`` key for a (player, category) pair on ``pass_no``."""
    if pass_no <= FIRST_PASS:
        return f"{target_id}:{category}"
    return f"{target_id}:{category}:{pass_no}"


def parse_asked_key(key: str) -> tuple[str, str, int]:
    """Split an ``asked`` key back into ``(user_id, category, pass_no)``.

    Categories never contain a colon, so the last segment is the pass number
    when there are three segments and the category when there are two.
    """
    parts = key.split(":")
    if len(parts) >= 3 and parts[-1].isdigit():
        return ":".join(parts[:-2]), parts[-2], int(parts[-1])
    user_id, _, cat = key.rpartition(":")
    return user_id, cat, FIRST_PASS


def record_asked(
    payload: dict[str, Any],
    target_id: str,
    category: str,
    question: str,
    pass_no: int | None = None,
) -> None:
    """Record that ``question`` was asked to ``target_id`` in ``category``.

    Mutates ``payload`` in place. The key is :func:`asked_key` for the
    game's current pass (or ``pass_no`` when given), so each (player,
    category) pair is recorded at most once *per pass* — matching the
    cog's "no duplicate (user, category) questions" rule within a pass.
    """
    asked: dict[str, str] = payload.setdefault("asked", {})
    if pass_no is None:
        pass_no = current_pass(payload)
    asked[asked_key(target_id, category, pass_no)] = question


def pass_complete(
    prefs: dict[str, list[str]], asked: dict[str, str], pass_no: int = FIRST_PASS
) -> bool:
    """Has every declared (player, category) pair been asked on ``pass_no``?

    False for an empty room — nothing to complete — so the "pass complete"
    moment only ever fires after a real question closed the pass.
    """
    if not any(cats for cats in prefs.values()):
        return False
    return not available_targets(prefs, asked, pass_no)


def start_next_pass(payload: dict[str, Any]) -> int:
    """Roll the game onto its next pass; returns the new pass number."""
    payload["pass"] = current_pass(payload) + 1
    return payload["pass"]


def available_targets(
    prefs: dict[str, list[str]], asked: dict[str, str], pass_no: int = FIRST_PASS
) -> list[tuple[str, str]]:
    """Return ``(user_id, category)`` pairs not yet asked on ``pass_no``.

    For each participant's declared preferences, filter out any
    ``(user, category)`` combinations already recorded in ``asked`` for
    this pass. Returned in iteration order of ``prefs`` (stable for
    Python 3.7+).
    """
    out: list[tuple[str, str]] = []
    for user_id, user_cats in prefs.items():
        for cat in user_cats:
            if asked_key(user_id, cat, pass_no) not in asked:
                out.append((user_id, cat))
    return out


def asked_counts_by_user(asked: dict[str, str]) -> dict[str, int]:
    """Return how many questions each user has been asked.

    Used to weight selection toward the player who's been asked the
    least so often, so one chatty target doesn't soak up every turn.
    """
    counts: dict[str, int] = {}
    for key in asked:
        user_id, _, _ = parse_asked_key(key)
        counts[user_id] = counts.get(user_id, 0) + 1
    return counts


def select_next_question_target(
    prefs: dict[str, list[str]],
    asked: dict[str, str],
    rng: random.Random | None = None,
    *,
    excluded: Iterable[int | str] | None = None,
    pass_no: int = FIRST_PASS,
) -> tuple[str, str] | None:
    """Pick the next ``(user_id, category)`` to ask on ``pass_no``.

    Implements the cog's selection rule:

    1. Build the list of available (player, category) pairs for the pass.
    2. Drop every player in ``excluded``.
    3. Look up each candidate's total asked-count.
    4. Keep only candidates whose player has the minimum asked-count.
    5. Choose one of the remainder uniformly at random.

    ``excluded`` is the no-contact gate (``docs/no_contact_spec.md``): the
    cog passes the asker's no-contact partners, so the bot never seats a
    blocked pair for a directed question. It is applied *before* the
    least-asked weighting, so an excluded player's low count can never
    pull them back in. Int or str ids both work — ``no_contact_partners``
    hands back ints, the payload keys are strs.

    Returns ``None`` when no eligible pair exists (either no prefs or
    every combination has already been asked on this pass). The
    least-asked weighting counts every pass, so a player who joined late
    is still preferred on pass two. ``rng`` is injected so tests can pin
    the tiebreak; defaults to the module ``random``.
    """
    dropped = {str(uid) for uid in (excluded or ())}
    available = [
        (uid, cat)
        for uid, cat in available_targets(prefs, asked, pass_no)
        if uid not in dropped
    ]
    if not available:
        return None

    counts = asked_counts_by_user(asked)
    candidate_counts = {uid: counts.get(uid, 0) for uid, _ in available}
    min_count = min(candidate_counts.values())
    least_asked = [(uid, cat) for uid, cat in available if candidate_counts[uid] == min_count]

    chooser = rng if rng is not None else random
    return chooser.choice(least_asked)


def select_bank_categories_for_all(
    prefs: dict[str, list[str]],
    asked: dict[str, str],
    rng: random.Random | None = None,
    pass_no: int = FIRST_PASS,
) -> dict[str, str]:
    """Pick one opted-in category per participant for a bank round.

    Returns ``{user_id: category}`` — for each participant, a single category
    chosen uniformly at random from the preferences they have *not yet been
    asked in* (bank questions are recorded in the same ``asked`` history as
    written ones). Players with no preferences, or whose every preference has
    already been asked, are omitted — so re-running the bank round after new
    people join only serves the newcomers instead of double-asking the
    original group.
    """
    chooser = rng if rng is not None else random
    out: dict[str, str] = {}
    for uid, cats in prefs.items():
        open_cats = [cat for cat in cats if asked_key(uid, cat, pass_no) not in asked]
        if open_cats:
            out[uid] = chooser.choice(open_cats)
    return out


def summarize_asked_by_category(asked: dict[str, str]) -> dict[str, int]:
    """Count questions asked per known category.

    Returns a dict keyed by every category in :data:`CATEGORIES` (zero
    when none asked) plus any unknown categories observed in ``asked``.
    Unknowns are tracked so a stale payload — e.g. produced before a
    category was renamed — still surfaces in the game-over recap.
    """
    by_cat: dict[str, int] = {cat: 0 for cat in CATEGORIES}
    for key in asked:
        _, cat, _ = parse_asked_key(key)
        by_cat[cat] = by_cat.get(cat, 0) + 1
    return by_cat


def question_pool_size(
    prefs: dict[str, list[str]], asked: dict[str, str], pass_no: int = FIRST_PASS
) -> int:
    """Total number of distinct ``(player, category)`` questions on a pass.

    This is the denominator for the "X / Y asked" progress report: every
    preference combo currently declared, unioned with anything already
    asked on ``pass_no``. The union keeps the total ``>= asked_on_pass``
    even if a player drops a preference after being asked that category,
    so the progress never reads as more-asked-than-possible.
    """
    pool = {asked_key(uid, cat, pass_no) for uid, cats in prefs.items() for cat in cats}
    pool |= {key for key in asked if parse_asked_key(key)[2] == pass_no}
    return len(pool)


def asked_on_pass(asked: dict[str, str], pass_no: int = FIRST_PASS) -> int:
    """How many questions were asked on ``pass_no`` (the progress numerator)."""
    return sum(1 for key in asked if parse_asked_key(key)[2] == pass_no)


# ── NSFW gating ─────────────────────────────────────────────────────
# NSFW prompts ride Discord's own age gate (``channel.is_nsfw()``), never a
# bot-side toggle. Both helpers take the already-resolved channel verdict so
# they stay pure and testable; the cog supplies it via ``channel_allows_nsfw``.

NSFW_PREFIX = "nsfw_"


def category_allowed(category: str, allow_nsfw: bool) -> bool:
    """May this preference category be selected in this channel?"""
    return allow_nsfw or not category.startswith(NSFW_PREFIX)


def filter_nsfw_prefs(
    prefs: dict[str, list[str]], allow_nsfw: bool
) -> dict[str, list[str]]:
    """Drop every NSFW category from every player's prefs in a SFW channel.

    Filtering the *preferences* rather than the drawn questions is what makes
    the gate hold: every serve path picks from opted-in categories, so a
    channel that lost its age-restriction mid-game stops serving NSFW at once,
    and the round's "already asked" accounting stays consistent.
    """
    if allow_nsfw:
        return prefs
    return {
        uid: [c for c in cats if category_allowed(c, allow_nsfw)]
        for uid, cats in prefs.items()
    }


# ── Idle close ──────────────────────────────────────────────────────
# Truth or Dare is the one game whose lobby and play are the same phase — the
# row sits in ``joining`` from open to end, so the lobby idle sweep can't tell
# a room that never filled from one mid-game, and 18 of 19 prod games were
# left to the 24-hour sweep (trivia-tail-84). The room ends itself instead:
# after the dashboard's quiet window with nothing pressed, the host view
# posts the recap and pays the room the same way End Game does. 0 turns it
# off. The window is stored on the payload at launch and re-armed from
# ``last_activity`` after a restart, so a restart neither resets nor loses it.

IDLE_MINUTES_DEFAULT = 20
IDLE_MINUTES_MAX = 24 * 60
IDLE_MINUTES_KEY = "idle_minutes"
LAST_ACTIVITY_KEY = "last_activity"


def clamp_idle_minutes(raw: Any, default: int = IDLE_MINUTES_DEFAULT) -> int:
    """The dial's stored value as whole minutes, ``0`` meaning off; junk
    reads as the default, and anything over a day clamps to a day."""
    try:
        minutes = int(float(raw))
    except (TypeError, ValueError):
        return default
    return max(0, min(minutes, IDLE_MINUTES_MAX))


def touch_activity(payload: dict[str, Any], now: float) -> None:
    """Stamp the moment something happened — a toggle, a question, a bank round."""
    payload[LAST_ACTIVITY_KEY] = int(now)


def idle_seconds_left(payload: dict[str, Any], now: float) -> float | None:
    """Seconds until the room has been quiet for its idle window, or None when
    the window is off (``idle_minutes`` 0 / absent) — never negative."""
    minutes = clamp_idle_minutes(payload.get(IDLE_MINUTES_KEY), default=0)
    if minutes <= 0:
        return None
    try:
        last = float(payload.get(LAST_ACTIVITY_KEY) or now)
    except (TypeError, ValueError):
        last = now
    return max(0.0, last + minutes * 60 - now)


def idle_close_notice(minutes: int) -> str:
    """The one line that says why the recap just appeared."""
    unit = "minute" if minutes == 1 else "minutes"
    return f"⌛ Truth or Dare wrapped up on its own after {minutes} quiet {unit}."


def pass_complete_notice(pass_no: int) -> str:
    """The loud moment a pass closes: everyone's been asked, and what's next."""
    return (
        f"🎉 Pass {pass_no} complete — everyone has been asked in every category "
        "they picked. The host can press **Ask Question** to go round again, or "
        "**End Game** for the recap and payout."
    )
