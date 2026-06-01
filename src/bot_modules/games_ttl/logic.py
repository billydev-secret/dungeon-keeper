"""Pure decision logic for the Two Truths and a Lie (TTL) cog.

All functions here take and return plain Python values so they're unit-
testable without spinning up Discord. The cog calls these from inside
its button callbacks and modal handlers; the Discord glue (sending the
message, persisting via ``modify_payload``) stays in the cog.

High-leverage pieces:

* :func:`parse_lie_index` — normalises the modal's "which is the lie?"
  free-text input ("1"/"a"/"first"/...) into a 0-indexed integer.
* :func:`add_submission` — applies a player's submission to the payload
  dict (statements, lie pointer, name, denormalised count).
* :func:`shuffle_statements` — re-orders the three statements and
  updates the lie index so display position doesn't give the lie away.
  ``rng`` is injected so tests can pin the shuffle.
* :func:`tally_votes` — splits voters into "got it right" vs "fooled"
  for a given lie index.
* :func:`update_scores` — applies a round's results to the running
  score dict (fooled count for the subject, correct-guess count for
  each successful guesser, total guessers seen).
* :func:`compute_recap_winners` — derives Best Liar / Most Honest /
  Best Guesser from the final scores dict, handling ties.

Note: unlike traditional Truth-or-Dare, TTL doesn't have a per-category
preference toggle — players opt in by submitting a modal, so there is
no ``toggle_pref`` equivalent here.
"""

from __future__ import annotations

import random
from typing import Any

# Module-level map for parsing the modal's "which is the lie?" answer.
_LIE_INPUT_MAP: dict[str, str] = {
    "1": "1", "2": "2", "3": "3",
    "a": "1", "b": "2", "c": "3",
    "first": "1", "second": "2", "third": "3",
    "one": "1", "two": "2", "three": "3",
}


def parse_lie_index(raw: str) -> int | None:
    """Parse the modal's "which statement is the lie?" answer.

    Accepts ``1``/``2``/``3``, ``a``/``b``/``c``, or
    ``first``/``second``/``third`` / ``one``/``two``/``three`` in any
    case, with surrounding whitespace. Returns the 0-indexed position
    of the lie, or ``None`` if the input doesn't match a known form.
    """
    if raw is None:
        return None
    key = raw.strip().lower()
    canonical = _LIE_INPUT_MAP.get(key)
    if canonical is None:
        return None
    return int(canonical) - 1


def add_submission(
    payload: dict[str, Any],
    user_id: int | str,
    display_name: str,
    statements: list[str],
    lie_index: int,
) -> None:
    """Record a player's three statements + lie pointer into the payload.

    Mutates ``payload`` in place: ensures the ``submissions`` and
    ``submitter_names`` sub-dicts exist, writes the player's entry,
    and refreshes the denormalised ``submission_count`` field that the
    lobby embed reads.
    """
    uid_str = str(user_id)
    submissions: dict[str, dict[str, Any]] = payload.setdefault("submissions", {})
    submissions[uid_str] = {
        "statements": list(statements),
        "lie": int(lie_index),
    }
    names: dict[str, str] = payload.setdefault("submitter_names", {})
    names[uid_str] = display_name
    payload["submission_count"] = len(submissions)


def shuffle_statements(
    statements: list[str],
    lie_index: int,
    rng: random.Random | None = None,
) -> tuple[list[str], int]:
    """Shuffle the three statements and return ``(new_statements, new_lie)``.

    The original ``lie_index`` is the position of the lie in the
    submitted order. After shuffling, the lie's new position is
    returned so callers can keep displaying the truth. ``rng`` is
    injected so tests can pin the order; defaults to the module
    ``random`` for production randomness.
    """
    chooser = rng if rng is not None else random
    indices = list(range(len(statements)))
    chooser.shuffle(indices)
    new_statements = [statements[i] for i in indices]
    new_lie = indices.index(lie_index)
    return new_statements, new_lie


def tally_votes(
    votes: dict[int, int], lie_index: int
) -> tuple[list[int], list[int]]:
    """Split voter ids into ``(correct, fooled)`` for a given ``lie_index``.

    Voters whose pick matches ``lie_index`` are "correct"; the rest are
    "fooled". Returns iteration order of the input dict, which lets the
    reveal embed list voters in the order they cast their first vote.
    """
    correct = [uid for uid, v in votes.items() if v == lie_index]
    fooled = [uid for uid, v in votes.items() if v != lie_index]
    return correct, fooled


def update_scores(
    scores: dict[str, dict[str, int]],
    subject_id: int | str,
    correct_voters: list[int],
    fooled_voters: list[int],
    total_voters: int,
) -> None:
    """Apply one round's outcome to the running ``scores`` dict.

    Mutates ``scores`` in place. The subject (the player whose
    statements were guessed at) gets ``fooled`` (number of wrong
    guesses) and ``total_guessers`` (everyone who voted) credited;
    each correct voter gets a point in their ``correct_guesses``.

    A fresh entry is created lazily for any uid not already in
    ``scores`` — handles both new subjects and first-time guessers.
    """
    subj_key = str(subject_id)
    subj_entry = scores.setdefault(
        subj_key,
        {"fooled": 0, "correct_guesses": 0, "total_guessers": 0},
    )
    subj_entry["fooled"] += len(fooled_voters)
    subj_entry["total_guessers"] += total_voters

    for uid in correct_voters:
        uid_str = str(uid)
        entry = scores.setdefault(
            uid_str,
            {"fooled": 0, "correct_guesses": 0, "total_guessers": 0},
        )
        entry["correct_guesses"] += 1


def compute_recap_winners(
    scores: dict[str, dict[str, int]],
    played_ids: set[str] | list[str],
) -> dict[str, Any]:
    """Compute Best Liar / Most Honest / Best Guesser from final scores.

    * **Best Liar** and **Most Honest** are derived only from players
      whose statements were actually played (``played_ids``); a player
      who only guessed shouldn't show up here.
    * **Best Guesser** considers everyone in ``scores`` — a non-subject
      can still win by guessing all the lies correctly.
    * Ties produce multiple winners (all uids matching the extremum).

    Returns a dict with keys ``best_liar`` (list[str]),
    ``most_fooled_count`` (int), ``most_honest`` (list[str]),
    ``least_fooled_count`` (int), ``best_guesser`` (list[str]),
    ``max_correct`` (int). Any category with no eligible candidates
    is returned as an empty list / 0.
    """
    played_set = set(played_ids)
    subject_scores = {uid: s for uid, s in scores.items() if uid in played_set}

    best_liar: list[str] = []
    most_fooled_count = 0
    most_honest: list[str] = []
    least_fooled_count = 0
    if subject_scores:
        most_fooled_count = max(s["fooled"] for s in subject_scores.values())
        best_liar = [uid for uid, s in subject_scores.items() if s["fooled"] == most_fooled_count]
        least_fooled_count = min(s["fooled"] for s in subject_scores.values())
        most_honest = [uid for uid, s in subject_scores.items() if s["fooled"] == least_fooled_count]

    best_guesser: list[str] = []
    max_correct = 0
    if scores:
        max_correct = max(s["correct_guesses"] for s in scores.values())
        best_guesser = [uid for uid, s in scores.items() if s["correct_guesses"] == max_correct]

    return {
        "best_liar": best_liar,
        "most_fooled_count": most_fooled_count,
        "most_honest": most_honest,
        "least_fooled_count": least_fooled_count,
        "best_guesser": best_guesser,
        "max_correct": max_correct,
    }
