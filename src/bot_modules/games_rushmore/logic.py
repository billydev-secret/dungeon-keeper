"""Pure decision logic for the Mt. Rushmore Draft cog.

All functions here take and return plain Python values so they're unit-
testable without spinning up Discord. The cog calls these from inside
its button callbacks, modal handlers, and the draft loop; the Discord
glue (sending the message, persisting via ``update_game_payload``)
stays in the cog.

High-leverage pieces:

* :func:`generate_snake_order` — builds the ``[round, player_id]`` pair
  list for a snake draft; round 1 forward, round 2 reverse, etc.
* :func:`is_duplicate` / :func:`find_who_picked` — case-insensitive
  helpers used by the pick-modal duplicate check.
* :func:`eligible_voters` — filters the player list down to those with
  at least one real (non-skipped, non-empty) pick.
* :func:`tally_votes` — turns a ``{voter: target}`` map into the winner
  uids, max-vote count, and the sorted ``[(uid, votes)]`` list the
  winner embed and recap both consume.
* :func:`compute_recap_stats` — derives the first-pick, skipped, fast/
  slow, and unanimous/split fields the recap embed renders. Pulled out
  so the parsing of ``f"{uid}_{rnd}"`` keys lives in one place.
* :func:`clamp_settings` — applies the slash-command bounds so the
  cog and any future API entrypoint share one rule set.
"""

from __future__ import annotations

from typing import Any

# How many picks each player makes — surfaced here so the cog, embed
# builders, and tests all share the same constant.
DRAFT_ROUNDS: int = 4

# Sentinel value stored in ``boards[uid][i]`` when a player ran out the
# clock for that pick. Compared as a string so the embed builders and
# stats logic agree on what counts as "real pick" vs "skipped".
SKIPPED_MARKER: str = "⏭️ Skipped"

# Length of the post-draft window in which players may fill their own
# skipped slots before the final boards are shown.
BACKFILL_SECONDS: int = 60


def generate_snake_order(
    players: list[int], rounds: int = DRAFT_ROUNDS,
) -> list[list[int]]:
    """Return ``[round_num, player_id]`` pairs in snake draft order.

    Round 1 goes forward in ``players`` order, round 2 reverses, round
    3 forwards again, and so on. ``round_num`` is 1-indexed because the
    cog displays it directly in embeds.
    """
    order: list[list[int]] = []
    for r in range(rounds):
        if r % 2 == 0:
            order.extend([[r + 1, pid] for pid in players])
        else:
            order.extend([[r + 1, pid] for pid in reversed(players)])
    return order


def is_duplicate(pick: str, all_picks: list[str]) -> bool:
    """Return ``True`` when ``pick`` collides with any prior pick.

    Comparison is case-insensitive and whitespace-trimmed, matching the
    duplicate-check the modal uses to reject a player's input.
    """
    normalized = pick.strip().lower()
    return normalized in [p.strip().lower() for p in all_picks]


def find_who_picked(
    pick: str, boards: dict[str, list[Any]],
) -> str | None:
    """Return the uid (as str) of whichever board already contains ``pick``.

    Skips empty slots and the :data:`SKIPPED_MARKER` sentinel. Returns
    ``None`` when nobody has it. Used by the pick modal to point the
    player at who beat them to it.
    """
    norm = pick.strip().lower()
    for uid_str, board in boards.items():
        for p in board:
            if p and p != SKIPPED_MARKER and p.strip().lower() == norm:
                return uid_str
    return None


def first_skipped_slot(board: list[Any]) -> int | None:
    """Index of the first :data:`SKIPPED_MARKER` slot, or ``None``.

    Backfill fills slots in board order, so "which slot am I fixing" is
    always the first skipped one.
    """
    for i, pick in enumerate(board):
        if pick == SKIPPED_MARKER:
            return i
    return None


def players_with_skips(boards: dict[str, list[Any]]) -> list[str]:
    """Uids (as str, board order) that still have at least one skipped slot."""
    return [
        uid for uid, board in boards.items()
        if any(p == SKIPPED_MARKER for p in board)
    ]


def apply_backfill(
    boards: dict[str, list[Any]],
    skipped: list[str],
    uid: int | str,
    pick_text: str,
) -> int | None:
    """Fill ``uid``'s first skipped slot with ``pick_text``.

    Mutates ``boards`` and ``skipped`` in place: the slot gets the pick and
    the matching ``f"{uid}_{round}"`` entry leaves the skipped list (so the
    recap's skip stats reflect what actually stayed empty). ``pick_times``
    is deliberately untouched — backfills aren't turn-timed and must not
    compete for fastest/slowest pick.

    Returns the 0-indexed slot that was filled, or ``None`` when the player
    has no board or no skipped slots (nothing changes).
    """
    board = boards.get(str(uid))
    if not board:
        return None
    slot = first_skipped_slot(board)
    if slot is None:
        return None
    board[slot] = pick_text
    key = f"{uid}_{slot + 1}"
    if key in skipped:
        skipped.remove(key)
    return slot


def eligible_voters(
    players: list[int], boards: dict[str, list[Any]],
) -> list[int]:
    """Return the subset of ``players`` who made at least one real pick.

    A player whose board is all-empty or all-skipped is excluded so the
    vote dropdown doesn't show people with nothing to judge. Order is
    preserved from ``players``.
    """
    eligible: list[int] = []
    for uid in players:
        board = boards.get(str(uid), [])
        has_pick = any(p and p != SKIPPED_MARKER for p in board)
        if has_pick:
            eligible.append(uid)
    return eligible


def tally_votes(
    votes: dict[int, int], eligible: list[int],
) -> tuple[list[int], int, list[tuple[int, int]]]:
    """Tally ``{voter: target}`` votes into winners + sorted results.

    Returns ``(winner_uids, max_votes, all_results)``:

    * ``winner_uids`` is every uid tied for the most votes (often a
      single id, possibly several on a tie). Empty when no votes were
      cast.
    * ``max_votes`` is the top vote count, or 0 when no votes.
    * ``all_results`` is ``[(uid, votes)]`` covering every player in
      ``eligible`` (zero-vote players included), sorted highest-first.

    Iteration follows ``eligible`` order for the zero-vote tail; ties
    fall back to that order.
    """
    tally: dict[int, int] = {}
    for _voter, target in votes.items():
        tally[target] = tally.get(target, 0) + 1

    if tally:
        max_votes = max(tally.values())
        winner_uids = [uid for uid, v in tally.items() if v == max_votes]
    else:
        winner_uids = []
        max_votes = 0

    all_results: list[tuple[int, int]] = [
        (uid, tally.get(uid, 0)) for uid in eligible
    ]
    all_results.sort(key=lambda x: x[1], reverse=True)

    return winner_uids, max_votes, all_results


def compute_recap_stats(
    draft_order: list[list[int]],
    boards: dict[str, list[Any]],
    all_picks: list[str],
    pick_times: dict[str, float | None],
    skipped: list[str],
    votes: dict[int, int],
    name_resolver: Any = None,
) -> dict[str, Any]:
    """Derive the stat fields the recap embed renders.

    ``name_resolver`` is a callable ``int -> str`` that maps a uid to a
    display name; if ``None`` the string form of the uid is used (so
    tests can omit it). All other inputs come straight off the
    ``RushmoreDraftView``.

    Returns a dict with keys:

    * ``first_pick`` — ``{"pick": str, "player": str}`` for the round-1
      pick-1 board entry, or absent when that slot is empty/skipped.
    * ``skipped_count`` — total skips this game (always present).
    * ``skipped_names`` — list of unique player names who skipped at
      least once (omitted when nobody skipped).
    * ``fastest`` / ``slowest`` — ``{"pick": str, "player": str,
      "time": float}`` for the fastest and slowest completed picks
      (omitted when no timed picks exist).
    * ``unanimous`` — ``True`` when one and only one player received
      votes (and at least one vote was cast).
    * ``vote_split`` — number of distinct players who received votes,
      present only when more than one did.
    """
    def _resolve(uid: int) -> str:
        if name_resolver is None:
            return str(uid)
        return name_resolver(uid)

    stats: dict[str, Any] = {}

    # First overall pick — round 1, first slot in the draft order.
    if all_picks and draft_order:
        first_pick_uid = draft_order[0][1]
        first_board = boards.get(str(first_pick_uid), [])
        if first_board and first_board[0] and first_board[0] != SKIPPED_MARKER:
            stats["first_pick"] = {
                "pick": first_board[0],
                "player": _resolve(first_pick_uid),
            }

    stats["skipped_count"] = len(skipped)
    if skipped:
        skipped_uids: list[int] = []
        seen: set[int] = set()
        for key in skipped:
            uid_str = key.rsplit("_", 1)[0]
            uid = int(uid_str)
            if uid not in seen:
                seen.add(uid)
                skipped_uids.append(uid)
        stats["skipped_names"] = [_resolve(uid) for uid in skipped_uids]

    # Fastest / slowest among picks that finished (skips store None).
    valid_times = {k: v for k, v in pick_times.items() if v is not None}
    if valid_times:
        # mypy/pyright dislikes ``v is not None`` flow-narrowing through
        # the dict comp into ``min/max``; the cast via local typing keeps
        # the lambda explicit.
        def _t(key: str) -> float:
            val = valid_times[key]
            assert val is not None  # narrowed by filter above
            return val

        fastest_key = min(valid_times, key=_t)
        slowest_key = max(valid_times, key=_t)

        def _pick_info(key: str) -> dict[str, Any]:
            uid_str, rnd_str = key.rsplit("_", 1)
            uid = int(uid_str)
            rnd = int(rnd_str)
            board = boards.get(uid_str, [])
            pick_text = board[rnd - 1] if 0 <= rnd - 1 < len(board) else "?"
            return {
                "pick": pick_text or "?",
                "player": _resolve(uid),
                "time": _t(key),
            }

        stats["fastest"] = _pick_info(fastest_key)
        stats["slowest"] = _pick_info(slowest_key)

    # Unanimous / split flags for the vote outcome.
    tally: dict[int, int] = {}
    for _voter, target in votes.items():
        tally[target] = tally.get(target, 0) + 1
    unique_targets = set(tally.keys())
    if len(unique_targets) == 1 and tally:
        stats["unanimous"] = True
    elif len(unique_targets) > 1:
        stats["vote_split"] = len(unique_targets)

    return stats


def clamp_settings(timer: int, vote_timer: int) -> tuple[int, int]:
    """Clamp the slash-command timer inputs into the allowed ranges.

    Returns ``(timer, vote_timer)`` with ``timer`` in ``[10, 120]`` and
    ``vote_timer`` in ``[10, 60]``. Mirrors the cog's pre-extraction
    clamp block so the slash command and any future API entrypoint
    share one rule set.
    """
    timer = max(10, min(timer, 120))
    vote_timer = max(10, min(vote_timer, 60))
    return timer, vote_timer
