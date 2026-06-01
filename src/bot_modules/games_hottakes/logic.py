"""Pure decision logic for the Hot Takes cog.

All functions here take and return plain Python values so they're unit-
testable without spinning up Discord. The cog calls these from inside
its button callbacks and modal handlers; the Discord glue (sending the
message, persisting via ``modify_payload``) stays in the cog.

The high-leverage piece is :func:`tally_votes` — it implements the
cog's per-round vote aggregation (counts, weighted average, standard
deviation) in one place so the same shape can be reused at every
collection point. Before the extraction the cog had this logic
duplicated in both the ``advance`` closure and ``close_game._confirmed``;
the extraction centralizes it.

Helpers (:func:`add_take`, :func:`shuffle_takes`,
:func:`compute_recap_summary`) are pure dict/list transforms the cog's
``modify_payload`` closures and recap builders delegate to.
"""

from __future__ import annotations

import random
import statistics
from typing import Any

# Vote scale constants — kept here so tests and recap builders can use
# them without dragging in Discord-bound modules.
VOTE_LABELS: list[str] = [
    "🧊 Strongly Disagree",
    "👎 Disagree",
    "😐 Meh",
    "👍 Agree",
    "🔥 Strongly Agree",
]
VOTE_VALUES: list[int] = [1, 2, 3, 4, 5]  # temperature values
VOTE_KEYS: list[str] = ["cold", "disagree", "meh", "agree", "hot"]


def add_take(payload: dict[str, Any], user_id: int, text: str) -> int:
    """Append a hot-take entry to ``payload['takes']``.

    Mutates ``payload`` in place: ensures the ``takes`` list exists,
    appends a new dict with ``user_id``, ``text``, and the current
    insertion index recorded as ``display_order`` (overwritten later
    when :func:`shuffle_takes` runs at the start of voting).

    Returns the new total count of takes — the caller echoes it back
    to the submitter in their ephemeral confirmation.
    """
    takes: list[dict[str, Any]] = payload.setdefault("takes", [])
    takes.append(
        {
            "user_id": user_id,
            "text": text,
            "display_order": len(takes),
        }
    )
    return len(takes)


def shuffle_takes(
    takes: list[dict[str, Any]], rng: random.Random | None = None
) -> list[dict[str, Any]]:
    """Return a new list of takes shuffled and re-indexed by ``display_order``.

    Does NOT mutate the input list. Rewrites each take's
    ``display_order`` to match its new position so downstream code can
    iterate by index without re-reading the list. ``rng`` is injected
    so tests can pin the order; defaults to the module ``random``.
    """
    shuffled = list(takes)
    chooser = rng if rng is not None else random
    chooser.shuffle(shuffled)
    for i, t in enumerate(shuffled):
        t["display_order"] = i
    return shuffled


def tally_votes(
    votes_by_user: dict[int, int],
    vote_values: list[int] | None = None,
    num_options: int | None = None,
) -> tuple[list[int], float, float]:
    """Aggregate per-user vote indexes into counts, weighted average, and stddev.

    ``votes_by_user`` maps ``user_id -> option_index`` (0-based index
    into :data:`VOTE_LABELS`). Returns ``(counts, avg, std)``:

    - ``counts``: a fixed-length list with one entry per option,
      counting how many users picked each option.
    - ``avg``: the weighted mean using ``vote_values`` as weights
      (defaults to :data:`VOTE_VALUES`). 0.0 when nobody voted.
    - ``std``: the sample standard deviation of the chosen weights
      (``statistics.stdev``); 0.0 when fewer than two votes.

    ``num_options`` overrides the length of the counts list — useful
    when the caller wants a different scale; defaults to
    ``len(vote_values)``.
    """
    values = vote_values if vote_values is not None else VOTE_VALUES
    n_opts = num_options if num_options is not None else len(values)

    counts = [0] * n_opts
    for v in votes_by_user.values():
        if 0 <= v < n_opts:
            counts[v] += 1

    total = sum(counts)
    if total == 0:
        return counts, 0.0, 0.0

    weighted_sum = sum(values[idx] * c for idx, c in enumerate(counts))
    avg = weighted_sum / total

    chosen_values = [values[v] for v in votes_by_user.values() if 0 <= v < n_opts]
    std = statistics.stdev(chosen_values) if len(chosen_values) > 1 else 0.0

    return counts, avg, std


def compute_recap_summary(
    results: list[dict[str, Any]],
    vote_values: list[int] | None = None,
) -> dict[str, Any] | None:
    """Compute the headline picks for the game-over recap.

    Returns a dict with ``hottest`` (highest avg), ``coldest`` (lowest
    avg), ``most_divisive`` (highest stddev, tiebroken by distance to
    the scale midpoint — only present when there are 2+ results), the
    de-duplicated ``total_voters`` set, and the total take count.
    Returns ``None`` when ``results`` is empty so the cog can early-
    return without sending an empty recap.
    """
    if not results:
        return None

    values = vote_values if vote_values is not None else VOTE_VALUES

    hottest = max(results, key=lambda x: x.get("avg", 0))
    coldest = min(results, key=lambda x: x.get("avg", 0))

    most_divisive: dict[str, Any] | None = None
    if len(results) > 1:
        midpoint = (values[0] + values[-1]) / 2
        most_divisive = max(
            results,
            key=lambda x: (x.get("std", 0), abs(x.get("avg", 0) - midpoint)),
        )

    total_voters: set[int] = set()
    for r in results:
        total_voters.update(r.get("voters", []))

    return {
        "hottest": hottest,
        "coldest": coldest,
        "most_divisive": most_divisive,
        "total_voters": total_voters,
        "total_takes": len(results),
    }
