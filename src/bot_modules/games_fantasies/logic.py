"""Pure decision logic for the Fantasies & Dealbreakers cog.

All functions here take and return plain Python values so they're unit-
testable without spinning up Discord. The cog calls these from inside
its modal handlers, button callbacks, and the round-advance coroutine;
the Discord glue (sending messages, persisting via ``modify_payload``)
stays in the cog.

The shape of this game is closest to Hot Takes — anonymous submission
of free-form text entries, then per-entry binary voting (✅ Same vs
❌ Not for me). Three extracted pieces:

* :func:`add_entry` — append an anonymous entry to the round's
  ``entries`` list, lazily creating the ``rounds``/round-key scaffolding
  in the payload. Mirrors the modal's ``_add_entry`` closure.
* :func:`tally_entry_votes` and :func:`build_result_entry` — collapse
  the two vote lists into the per-entry result dict the recap consumes.
* :func:`roster_from_results` — the room the host's End Game pays: entry
  authors plus every voter, the same two fields the 24h sweep reads.
* :func:`compute_recap_summary` — picks the headline entries (most
  shared, most polarizing, biggest outlier) for the final recap, plus
  the de-duplicated voter set.
* :func:`active_voters` / :func:`everyone_has_voted` — the vote-complete
  auto-advance (2026-09-04, anon-tail-71): an entry's vote closes on the
  **Seconds per Entry** timer (:data:`DEFAULT_ENTRY_SECONDS`; 0 = host-paced,
  the timer itself is ``games/utils/round_pacing.RoundPacing``) or as soon
  as everyone the round is waiting on has voted.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# Seconds an entry stays open for votes when the dashboard dial is unset;
# 0 on the dial means host-paced (Next only), as for WYR/NHIE/MLT.
DEFAULT_ENTRY_SECONDS = 45

# The one ephemeral line for a self-vote — shared with Hot Takes.
SELF_VOTE_REFUSAL = "❌ You can't vote on your own entry!"

# The two categories. One submit button carries each, so these are the
# only values ``add_entry`` ever stores — no parsing stands between a
# member's choice and the payload. Kept here so the cog's buttons, the
# tests and the embed builders don't redeclare the strings.
CATEGORY_FANTASY = "Fantasy"
CATEGORY_DEALBREAKER = "Dealbreaker"


def add_entry(
    payload: dict[str, Any],
    round_num: int,
    user_id: int,
    text: str,
    category: str,
) -> None:
    """Append an anonymous entry to a round's ``entries`` list.

    Mutates ``payload`` in place: ensures ``payload['rounds']`` exists,
    ensures the round-key sub-dict (with an ``entries`` list) exists,
    then appends ``{user_id, text, category}``. Round keys are stringi-
    fied to match the JSON-serialised payload convention used elsewhere
    in the games stack.
    """
    rounds: dict[str, Any] = payload.setdefault("rounds", {})
    round_key = str(round_num)
    rnd = rounds.setdefault(round_key, {"entries": []})
    rnd.setdefault("entries", []).append(
        {"user_id": user_id, "text": text, "category": category}
    )


def tally_entry_votes(
    same_votes: list[int], nope_votes: list[int]
) -> tuple[int, int, float]:
    """Aggregate one entry's vote lists into ``(same, nope, same_pct)``.

    ``same_pct`` is the share of "Same" votes out of all votes cast; it
    is ``0.0`` when no one voted. The cog stores this value in the
    per-entry result dict so the recap can render percentages without
    re-computing.
    """
    same = len(same_votes)
    nope = len(nope_votes)
    total = same + nope
    same_pct = (same / total) if total > 0 else 0.0
    return same, nope, same_pct


def apply_vote(
    same_votes: list[int],
    nope_votes: list[int],
    uid: int,
    vote_kind: str,
) -> bool:
    """Toggle ``uid``'s vote on the appropriate list.

    Mutates ``same_votes`` and ``nope_votes`` in place. ``vote_kind`` is
    ``"same"`` or ``"nope"``. Behavior mirrors the cog's button
    callbacks:

    * If the user is on the opposite list, they're moved off it
      (the "changed sides" path) and the returned flag is ``True``.
    * If the user is already on the chosen list, this is a no-op
      idempotent re-press; ``False`` is returned.

    Returns ``True`` if the user switched sides, ``False`` otherwise
    (the cog uses this to add a ``" (changed)"`` suffix to the
    ephemeral confirmation).
    """
    if vote_kind == "same":
        chosen, other = same_votes, nope_votes
    elif vote_kind == "nope":
        chosen, other = nope_votes, same_votes
    else:
        raise ValueError(f"unknown vote_kind: {vote_kind!r}")

    changed = uid in other
    if changed:
        other.remove(uid)
    if uid not in chosen:
        chosen.append(uid)
    return changed


def build_result_entry(
    *,
    text: str,
    category: str,
    author: int,
    same_votes: list[int],
    nope_votes: list[int],
) -> dict[str, Any]:
    """Build the per-entry result dict the recap consumes.

    Combines :func:`tally_entry_votes` with the entry metadata
    (text/category/author) and the de-duplicated voter list. The cog
    persists this dict into ``payload['results']`` so the recap can
    re-render after a mid-game close without re-tallying.
    """
    same, nope, same_pct = tally_entry_votes(same_votes, nope_votes)
    return {
        "text": text,
        "category": category,
        "same": same,
        "nope": nope,
        "same_pct": same_pct,
        "author": author,
        "voters": list(same_votes) + list(nope_votes),
    }


def compute_recap_summary(
    results: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Compute the headline picks for the game-over recap.

    Returns a dict with:

    * ``most_shared`` — the entry with the highest ``same_pct``
      (ties broken by list order, matching the cog's old ``max``).
    * ``most_polar`` — the entry whose ``same_pct`` is closest to
      ``0.5`` (computed via ``abs(same_pct - 0.5)``).
    * ``biggest_outlier`` — the entry with the lowest ``same_pct``
      (the take fewest people shared).
    * ``total_voters`` — the de-duplicated set of voter user IDs
      across every entry.
    * ``total_results`` — the count of entries (used as
      "Total Submissions" in the recap).

    Returns ``None`` when ``results`` is empty so the cog can early-
    return without sending an empty recap embed.
    """
    if not results:
        return None

    most_shared = max(results, key=lambda x: x.get("same_pct", 0))
    most_polar = min(results, key=lambda x: abs(x.get("same_pct", 0) - 0.5))
    biggest_outlier = min(results, key=lambda x: x.get("same_pct", 1))

    total_voters: set[int] = set()
    for r in results:
        total_voters.update(r.get("voters", []))

    return {
        "most_shared": most_shared,
        "most_polar": most_polar,
        "biggest_outlier": biggest_outlier,
        "total_voters": total_voters,
        "total_results": len(results),
    }


def get_round_entries(
    payload: dict[str, Any], round_num: int
) -> list[dict[str, Any]]:
    """Fetch a round's submitted entries from the payload.

    Returns an empty list when the round (or the ``rounds`` scaffold)
    is missing — the cog uses that to short-circuit a round with no
    submissions to its "No entries submitted" notice.
    """
    return (
        payload.get("rounds", {}).get(str(round_num), {}).get("entries", [])
    )


def roster_from_results(results: list[dict[str, Any]]) -> list[int]:
    """Everyone the game pays when the host ends it: entry authors plus
    everyone who voted either way on an entry, de-duplicated and sorted.

    The author of the most-shared entry may never have voted, so a voters-only
    roster would drop their participation credit. This is the completion
    site's roster; ``game_roster._fantasies`` (the sweep's and ``/games end``'s
    view of the same payload) reads the same two fields, so every end path
    pays the same room.
    """
    roster: set[int] = set()
    for r in results:
        if not isinstance(r, dict):
            continue
        if r.get("author") is not None:
            roster.add(int(r["author"]))
        roster.update(int(v) for v in r.get("voters") or [])
    return sorted(roster)


def active_voters(
    entries: list[dict[str, Any]],
    results: list[dict[str, Any]],
    *,
    exclude: int | None = None,
) -> set[int]:
    """The room an entry is waiting on: everyone who submitted this round
    plus everyone who has voted on an earlier entry this game, minus the
    entry's own author (who may not vote on it).
    """
    room: set[int] = set()
    for e in entries:
        if isinstance(e, dict) and e.get("user_id") is not None:
            room.add(int(e["user_id"]))
    for r in results:
        if isinstance(r, dict):
            room.update(int(v) for v in r.get("voters") or [])
    if exclude is not None:
        room.discard(int(exclude))
    return room


def everyone_has_voted(expected: Iterable[int], voted: Iterable[int]) -> bool:
    """True when every expected voter has voted — the vote-complete auto-advance.

    An empty expected set never advances; the timer or Next handles that
    entry.
    """
    expected_set = {int(u) for u in expected}
    if not expected_set:
        return False
    return expected_set <= {int(u) for u in voted}


def round_in_progress(active_submit: Any, active_vote: Any, *, running: bool = False) -> bool:
    """True while a round is running — the Start Round guard.

    A live submit or vote view means a phase is open; ``running`` covers the
    gaps between phases, when neither view is set but the round loop is still
    awaiting. A second press used to start a concurrent round and overwrite
    the main view's live submit view.
    """
    return running or active_submit is not None or active_vote is not None
