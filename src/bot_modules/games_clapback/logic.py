"""Pure decision logic for the Clapback cog.

All functions here take and return plain Python values so they're unit-
testable without spinning up Discord. The cog calls these from inside
its button callbacks and game-loop methods; the Discord glue (sending
the message, persisting via ``modify_payload``) stays in the cog.

High-leverage pieces:

* :func:`create_matchups` — pairs submitted answers head-to-head,
  handling 3-player round-robin, odd-count byes (fewest-byes-first
  rotation), and duplicate-answer avoidance. ``rng`` is injected so
  tests can pin the order.
* :func:`calculate_bye_award` — what sitting out a round is worth.
* :func:`calculate_matchup_score` — counts votes and returns scores,
  winner, and the clapback (unanimous, 2+ votes) flag for a single
  matchup.
* :func:`find_best_answer_record` / :func:`find_closest_matchup_record`
  — recap helpers that return the raw round-history record (plus
  round number) so embed builders can format names separately.
"""

from __future__ import annotations

import random
from itertools import combinations
from typing import Any

# Player count bounds — surfaced here so the start-game button and the
# tests can both read the same constants.
MIN_PLAYERS: int = 3
MAX_PLAYERS: int = 16

# AI prompt copy lives here so any test that wants to verify the
# generator wiring can pull the same strings the cog uses.
AI_SYSTEM_PROMPT: str = (
    "You are generating prompts for a Clapback-style comedy party game in the "
    "this Discord community. The prompt should be something players "
    "can write a short, funny answer to."
)
AI_USER_PROMPT: str = (
    "Generate a single comedy prompt. Good prompts are specific, unexpected, and "
    "leave room for creative answers. Examples: 'A terrible name for a pet store', "
    "'Something you'd never want to hear from your dentist', 'The worst superpower "
    "to have on a first date'. Avoid prompts that are too broad ('something funny') "
    "or too narrow (only one good answer). Return only the prompt text, no quotes, "
    "no numbering."
)


def admit_pending_players(
    players: list[Any],
    pending: list[Any] | None,
    max_players: int,
) -> tuple[list[Any], list[Any], list[Any]]:
    """Fold latecomers into the roster at a round boundary.

    Returns ``(roster, admitted, turned_away)``. A game night regularly wants
    one more body to even the numbers out and there was no way to add one
    mid-game — players resorted to typing another bot's ``&add`` syntax at it
    (2026-08-21). Admitting only at a round boundary is what keeps a live
    round's matchups and answer count stable.

    Anyone already in ``players`` is dropped silently (a double press), the
    order of ``pending`` is preserved, and anyone over ``max_players`` is
    turned away rather than quietly ignored so the caller can say so. They
    start on zero points, which is a real disadvantage — that is the honest
    consequence of joining late, not a bug.
    """
    roster = list(players)
    admitted: list[Any] = []
    turned_away: list[Any] = []
    seen = {str(p) for p in roster}
    for uid in pending or []:
        if str(uid) in seen:
            continue
        if len(roster) >= max_players:
            turned_away.append(uid)
            continue
        seen.add(str(uid))
        roster.append(uid)
        admitted.append(uid)
    return roster, admitted, turned_away


def drain_pending_players(payload: dict) -> list[Any]:
    """Empty the join queue and return whoever was still waiting in it.

    :func:`admit_pending_players` only runs at a round boundary, so anyone who
    pressed Join during the **final** round is queued for a round that never
    comes — no admission, no notice, no place in the payout. A finished game
    has to clear the queue and tell them, or they are left waiting on a game
    that already ended.
    """
    pending = list(payload.get("pending_players") or [])
    payload["pending_players"] = []
    return pending


#: Discord caps a button label at 80 characters; leave room for the side
#: marker and the ellipsis.
VOTE_LABEL_MAX = 55


def vote_button_label(side: str, answer: str, max_length: int = VOTE_LABEL_MAX) -> str:
    """Label for one of the two head-to-head vote buttons.

    The buttons used to be bare 🅰️/🅱️ emoji, which meant voters had to hold
    "left is the top answer" in their heads and several of them plainly
    didn't ("I can never remember if left is yes or no" — game night
    2026-08-21). Putting the answer itself on the button removes the mapping
    step entirely.

    Newlines are flattened (Discord renders labels on one line) and the text
    is truncated with an ellipsis. A blank or whitespace-only answer falls back
    to ``"Vote <side>"``. ``side`` is the embed's own marker for that answer
    (the 🅰️/🅱️ emoji in production), so button and embed stay legible read
    together.
    """
    flat = " ".join(str(answer or "").split())
    if not flat:
        return f"Vote {side}"
    if len(flat) > max_length:
        flat = flat[: max_length - 1].rstrip() + "…"
    return f"{side}: {flat}"


def pick_round_bye(
    player_ids: list[Any],
    bye_history: list[Any] | None = None,
    rng: random.Random | None = None,
) -> Any:
    """Who sits the coming round out, decided *before* the prompt goes out.

    The bye used to fall out of :func:`create_matchups`, i.e. after everyone
    had already written an answer — so the benched player spent the round
    composing something that was never used and only found out at the
    scoreboard ("It should really let you know when you're sitting out" —
    game night, 2026-08-21). Picking up front lets the round skip them
    entirely: no ping, no submit button, no wasted answer.

    Same rotation rule as :func:`create_matchups` — fewest byes so far wins,
    picked at random within the tied group — so the two stay consistent when
    a missing submitter forces a second, late bye on top of this one.

    Returns ``None`` when nobody needs to sit out: an even field pairs
    cleanly, and exactly 3 players run a round-robin instead.

    ``player_ids`` should be the same id type used as ``answers`` keys and in
    ``bye_history`` (strings in production) — ``history.count`` is what makes
    the rotation work, and it will not match across types.
    """
    ids = list(player_ids)
    if len(ids) == 3 or len(ids) % 2 == 0:
        return None
    chooser = rng if rng is not None else random
    chooser.shuffle(ids)
    history = list(bye_history or [])
    return min(ids, key=history.count)


def create_matchups(
    answers: dict[str, str],
    bye_history: list[Any] | None = None,
    rng: random.Random | None = None,
) -> tuple[list[dict[str, Any]], Any]:
    """Pair submitted answers for head-to-head voting.

    Returns ``(matchups, bye_player_id)``. Every submitter appears in
    the result exactly once — either in a pair or as the bye.

    Bye rotation: ``bye_history`` is every bye handed out this game, in
    order (the same id can appear more than once in a long game). The
    bye goes to whoever among *this round's* submitters has had the
    fewest so far, picked at random within that tied group. So nobody
    sits out twice until everyone has sat out once, and the rule keeps
    cycling correctly on the second lap. Counting — rather than
    remembering only the previous bye — is what makes it hold up when
    the set of submitters changes from round to round, which it does
    whenever someone misses the submit window.

    ID typing note: ids in ``bye_history`` and the returned bye id
    match the type of ``answers`` dict keys (strings in production,
    since the cog stores user ids as ``str(uid)``).

    Special case: with exactly 3 players the function returns the full
    round-robin (3 pairs) so the round has real action.

    Duplicate answers: up to 10 shuffles are tried and the pairing with
    the fewest same-answer pairs wins. Every attempt is a *complete*
    pairing, so an unavoidable duplicate costs a repeated answer in one
    matchup, never a player dropped from the round.

    ``rng`` is injected so tests can pin the shuffle order; defaults to
    the module-level :mod:`random` in production.
    """
    chooser = rng if rng is not None else random
    player_ids = list(answers.keys())
    chooser.shuffle(player_ids)

    bye_player: Any = None

    # Small games (3 players): round-robin every pair so each player
    # competes twice and the round has real action.
    if len(player_ids) == 3:
        pairs: list[dict[str, Any]] = []
        for a, b in combinations(player_ids, 2):
            pairs.append({"pair": [a, b], "votes": {}, "winner": None})
        chooser.shuffle(pairs)
        return pairs, None

    if len(player_ids) % 2 == 1:
        # Fewest byes so far wins the bye. player_ids is already
        # shuffled, so min() picking the first of a tied group is a
        # random choice among everyone equally overdue.
        history = list(bye_history or [])
        bye_player = min(player_ids, key=history.count)
        player_ids.remove(bye_player)

    # Same-answer pairings are ugly to vote on, so shuffle a few times
    # and keep the least-duplicated complete pairing we saw.
    best_matchups: list[dict[str, Any]] = []
    best_dupes: int | None = None
    for _ in range(10):
        chooser.shuffle(player_ids)
        pairs = []
        dupes = 0
        for i in range(0, len(player_ids), 2):
            a, b = player_ids[i], player_ids[i + 1]
            if answers[str(a)].strip().lower() == answers[str(b)].strip().lower():
                dupes += 1
            pairs.append({
                "pair": [a, b],
                "votes": {},
                "winner": None,
            })
        if dupes == 0:
            return pairs, bye_player
        if best_dupes is None or dupes < best_dupes:
            best_dupes = dupes
            best_matchups = pairs

    return best_matchups, bye_player


def calculate_bye_award(
    round_points: list[int] | None,
    default: int = 50,
) -> int:
    """Points for sitting out a round — the field's average that round.

    ``round_points`` is every point value the players who actually
    competed earned this round (one entry per contestant per matchup,
    clapback bonuses included). The bye player gets the mean, rounded,
    so sitting out neither punishes nor rewards them relative to the
    room. ``default`` covers the degenerate case where a round somehow
    resolved no matchups at all.

    Deliberately independent of the bye player's own history: a bye is
    a scheduling accident, not a performance, so it shouldn't compound
    a lead or a deficit.
    """
    values = list(round_points or [])
    if not values:
        return default
    return round(sum(values) / len(values))


def calculate_matchup_score(
    votes: dict[Any, Any],
    player_a_id: int,
    player_b_id: int,
) -> dict[str, Any]:
    """Tally votes for a single head-to-head matchup.

    Returns a dict with:

    - ``winner``: the player id of the side with more votes, or
      ``None`` for a tie (or no votes).
    - ``scores``: ``{player_a_id: pts, player_b_id: pts}`` based on
      vote percentage plus a +25 clapback bonus to the unanimous
      winner when applicable.
    - ``clapback``: ``True`` when one side has every vote and there are
      at least 2 votes — both halves of the rule.
    - ``vote_counts``: raw counts per player.

    The 50/50 fallback for the zero-votes case matches the cog's
    intentional "show up and play" behavior from before extraction.
    """
    total_votes = len(votes)

    if total_votes == 0:
        return {
            "winner": None,
            "scores": {player_a_id: 50, player_b_id: 50},
            "clapback": False,
            "vote_counts": {player_a_id: 0, player_b_id: 0},
        }

    votes_for_a = sum(1 for v in votes.values() if str(v) == str(player_a_id))
    votes_for_b = total_votes - votes_for_a

    pct_a = round((votes_for_a / total_votes) * 100)
    pct_b = 100 - pct_a

    clapback = (
        (votes_for_a == total_votes or votes_for_b == total_votes)
        and total_votes >= 2
    )
    bonus = 25 if clapback else 0

    if votes_for_a > votes_for_b:
        winner: int | None = player_a_id
    elif votes_for_b > votes_for_a:
        winner = player_b_id
    else:
        winner = None

    scores = {
        player_a_id: pct_a + (bonus if votes_for_a == total_votes else 0),
        player_b_id: pct_b + (bonus if votes_for_b == total_votes else 0),
    }
    return {
        "winner": winner,
        "scores": scores,
        "clapback": clapback,
        "vote_counts": {player_a_id: votes_for_a, player_b_id: votes_for_b},
    }


def find_best_answer_record(
    round_history: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Find the round-history matchup record with the highest vote share.

    Walks every matchup in ``round_history`` (requires at least 3 total
    votes per matchup, matching the cog's pre-extraction threshold) and
    returns a dict with the winning answer's text, author id, vote
    counts, and round number. Returns ``None`` when no matchup hits the
    3-vote floor.

    The embed builder takes this record and resolves the author name
    against the guild so this function stays Discord-free.
    """
    best_pct: float = -1.0
    best_votes = 0
    best_text: str | None = None
    best_author: int | None = None
    best_round = 0

    for rh in round_history:
        for m in rh.get("matchups", []):
            total = m["votes_a"] + m["votes_b"]
            if total < 3:
                continue
            pct_a = m["votes_a"] / total
            pct_b = m["votes_b"] / total

            if pct_a > best_pct or (pct_a == best_pct and m["votes_a"] > best_votes):
                best_pct = pct_a
                best_votes = m["votes_a"]
                best_text = m["answer_a"]
                best_author = m["player_a"]
                best_round = rh["round"]

            if pct_b > best_pct or (pct_b == best_pct and m["votes_b"] > best_votes):
                best_pct = pct_b
                best_votes = m["votes_b"]
                best_text = m["answer_b"]
                best_author = m["player_b"]
                best_round = rh["round"]

    if best_text is None:
        return None

    return {
        "text": best_text,
        "author": best_author,
        "pct": best_pct,
        "votes": best_votes,
        "round": best_round,
    }


def find_closest_matchup_record(
    round_history: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Find the matchup record with the smallest vote margin.

    Walks every matchup in ``round_history`` (skipping those with zero
    total votes) and returns the closest one, tiebroken by larger total
    vote count (so a 3–4 split beats a 1–2 split). Returns ``None``
    when nothing qualifies.

    The embed builder takes this record and renders the formatted
    matchup string so this function stays Discord-free.
    """
    best_margin: float = float("inf")
    best_total = 0
    best: dict[str, Any] | None = None

    for rh in round_history:
        for m in rh.get("matchups", []):
            total = m["votes_a"] + m["votes_b"]
            if total == 0:
                continue
            margin = abs(m["votes_a"] - m["votes_b"])
            if margin < best_margin or (
                margin == best_margin and total > best_total
            ):
                best_margin = margin
                best_total = total
                best = {"matchup": m, "round": rh["round"]}

    return best


def sort_scores(scores: dict[Any, int]) -> list[tuple[Any, int]]:
    """Sort a ``{pid: pts}`` map highest-first for scoreboards.

    Returns a list of ``(pid, pts)`` tuples. Pulled out so both the
    round-summary embed and the final recap embed call the same
    deterministic ordering.
    """
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def shuffled_replay_config(
    base_config: dict[str, Any],
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Return a new config with randomized rounds / timer / vote_timer.

    Mirrors the ``ClapbackRecapView`` "Play Again (Shuffled)" branch:
    picks ``rounds`` from 3-8, ``timer`` from ``{60, 90, 120, 150,
    180}``, and ``vote_timer`` from ``{30, 40, 50, 60}``. ``rng`` is
    injected so tests can pin the random picks.
    """
    chooser = rng if rng is not None else random
    new_cfg = dict(base_config)
    new_cfg["rounds"] = chooser.randint(3, 8)
    new_cfg["timer"] = chooser.choice([60, 90, 120, 150, 180])
    new_cfg["vote_timer"] = chooser.choice([30, 40, 50, 60])
    return new_cfg


def clamp_config_values(
    rounds: int, timer: int, vote_timer: int,
) -> tuple[int, int, int]:
    """Clamp the slash-command inputs into the allowed ranges.

    Returns ``(rounds, timer, vote_timer)`` with ``rounds`` in
    ``[1, 15]``, ``timer`` in ``[15, 180]``, ``vote_timer`` in
    ``[10, 60]``. Mirrors the cog's pre-extraction clamp block so the
    slash command and any future API entrypoint share one rule set.
    """
    rounds = min(max(rounds, 1), 15)
    timer = min(max(timer, 15), 180)
    vote_timer = min(max(vote_timer, 10), 60)
    return rounds, timer, vote_timer
