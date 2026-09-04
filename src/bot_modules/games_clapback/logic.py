"""Pure decision logic for the Clapback cog.

All functions here take and return plain Python values so they're unit-
testable without spinning up Discord. The cog calls these from inside
its button callbacks and game-loop methods; the Discord glue (sending
the message, persisting via ``modify_payload``) stays in the cog.

High-leverage pieces:

* :func:`create_matchups` — pairs submitted answers head-to-head,
  handling 3-player round-robin, odd-count byes (fewest-byes-first
  rotation), duplicate-answer avoidance, and the no-contact gate (a
  forbidden pair is never a matchup; see :func:`create_matchups`).
  ``rng`` is injected so tests can pin the order.
* :func:`playable_players` — the roster the Start button counts against
  ``MIN_PLAYERS``: anyone with at least one opponent the list allows.
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
from collections.abc import Iterable
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


def admit_player_now(payload: dict, uid: Any, max_players: int) -> str:
    """Seat a latecomer in the round that is *taking answers right now*.

    Returns one of ``joined`` / ``already-in`` / ``full`` / ``queued`` /
    ``already-queued``, and mutates ``payload`` to match.

    :func:`admit_pending_players` holds latecomers to the round boundary, which
    is the right rule once a round has matchups — those are fixed, and moving
    them under a live vote would be a mess. During the *submit* phase there is
    nothing fixed yet: :func:`create_matchups` is built from the answers dict
    after the window closes, so anyone who gets an answer in is simply paired
    like everyone else. Making them watch a round they could have played was a
    rule inherited from the harder case.

    Off the submit phase this falls back to the queue, so one button covers
    both and the caller can say which happened rather than promising "next
    round" when the player is already in this one.

    ``scores_checkpoint`` is seeded alongside ``scores``: the checkpoint is
    what a crash-resume rolls back to, and a joiner missing from it would be
    rolled off the scoreboard entirely.
    """
    players = payload.setdefault("players", [])
    if any(str(p) == str(uid) for p in players):
        return "already-in"

    if payload.get("phase") != "submitting":
        queued = payload.setdefault("pending_players", [])
        if any(str(q) == str(uid) for q in queued):
            return "already-queued"
        queued.append(uid)
        return "queued"

    if len(players) >= max_players:
        return "full"

    players.append(uid)
    payload.setdefault("scores", {}).setdefault(str(uid), 0)
    payload.setdefault("scores_checkpoint", {}).setdefault(str(uid), 0)
    payload.setdefault("clapbacks", {}).setdefault(str(uid), 0)
    return "joined"


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


# ── The no-contact gate ──────────────────────────────────────────────────────
#
# A vote card puts two answers side by side under two names, so a matchup is
# a contact surface (docs/no_contact_spec.md). The cog hands both bracket
# functions the pairs ``no_contact_pairs_among`` returns for the people in
# play, and neither ever seats such a pair. When the only pairing left would
# be forbidden, one of the two is the bye — paid like any bye, announced like
# any bye — so nothing on screen says why.

Pair = tuple[str, str]

#: Bound on the constrained pairing search in :func:`_safe_pairing`. With
#: sixteen players and a handful of forbidden pairs a full pairing falls out
#: of the first descent; the cap only matters for a contrived dense case,
#: where the best pairing found so far is used and the rest are byes.
MAX_PAIRING_NODES = 5_000


def _pair_key(a: Any, b: Any) -> Pair:
    """One canonical key per pair, in the str form answers are keyed by."""
    sa, sb = str(a), str(b)
    return (sa, sb) if sa < sb else (sb, sa)


def _forbidden_keys(forbidden_pairs: Iterable[tuple[Any, Any]] | None) -> set[Pair]:
    """Normalise ``forbidden_pairs`` to :func:`_pair_key` form.

    The no-contact service returns ``(low, high)`` int tuples while answers
    and ``bye_history`` are keyed by ``str(uid)``, so the pairs are re-keyed
    here and a caller may pass either type, either way round.
    """
    return {
        _pair_key(a, b) for a, b in (forbidden_pairs or ()) if str(a) != str(b)
    }


def _blocked(a: Any, b: Any, banned: set[Pair]) -> bool:
    return _pair_key(a, b) in banned


def _isolated(ids: list[Any], banned: set[Pair]) -> list[Any]:
    """Everyone with no opponent the list allows among ``ids``."""
    return [
        p for p in ids
        if all(_blocked(p, q, banned) for q in ids if str(q) != str(p))
    ]


def _has_forbidden_pair(ids: list[Any], banned: set[Pair]) -> bool:
    return any(_blocked(a, b, banned) for a, b in combinations(ids, 2))


def _safe_pairing(
    order: list[Any], banned: set[Pair], max_nodes: int = MAX_PAIRING_NODES,
) -> list[tuple[Any, Any]]:
    """The largest pairing of ``order`` that seats no forbidden pair.

    Backtracking over ``order`` — pair the first unseated player with the
    first allowed partner, recurse, and try the next partner if that dead-
    ends — so the result is a random full pairing when ``order`` is shuffled
    and one exists, which is what an unconstrained shuffle-and-pair-adjacent
    produces. A player is left out only when no partner leads anywhere,
    which is how the contrived case (a forbidden set that admits no full
    pairing) still yields the most matchups possible. The node cap keeps
    that case bounded; the best pairing seen by then is returned.
    """
    target = len(order) // 2
    best: list[tuple[Any, Any]] = []
    nodes = 0

    def walk(remaining: list[Any], acc: list[tuple[Any, Any]]) -> bool:
        nonlocal best, nodes
        if len(acc) > len(best):
            best = list(acc)
        if len(best) >= target or nodes >= max_nodes:
            return True
        if len(remaining) < 2:
            return False
        nodes += 1
        first, rest = remaining[0], remaining[1:]
        for i, partner in enumerate(rest):
            if _blocked(first, partner, banned):
                continue
            acc.append((first, partner))
            if walk(rest[:i] + rest[i + 1:], acc):
                return True
            acc.pop()
        # Nobody seats ``first`` — only reachable when no full pairing
        # exists from here, so the search keeps going for the biggest one.
        return walk(rest, acc)

    walk(list(order), [])
    return best


def _matchable(ids: list[Any], banned: set[Pair]) -> bool:
    """Whether ``ids`` admits a full pairing that seats no forbidden pair."""
    if len(ids) % 2:
        return False
    if not banned:
        return True
    return len(_safe_pairing(ids, banned)) * 2 == len(ids)


def _rotation_bye(ids: list[Any], history: list[Any], banned: set[Pair]) -> Any:
    """Fewest byes so far wins the bye — ``ids`` is already shuffled, so the
    first of a tied group is a random pick among everyone equally overdue.

    With forbidden pairs in play the bye must also leave a field that can
    still be paired: benching an overdue player whose absence strands two
    people who may only face each other would just force another bye. The
    first candidate in rotation order whose removal leaves a full safe
    pairing takes it; if none does (contrived), the rotation's own pick
    stands and :func:`create_matchups` benches whoever is left over.
    """
    ordered = sorted(ids, key=history.count)
    if not banned:
        return ordered[0]
    for candidate in ordered:
        if _matchable([p for p in ids if p != candidate], banned):
            return candidate
    return ordered[0]


def playable_players(
    players: list[Any], forbidden_pairs: Iterable[tuple[Any, Any]] | None,
) -> list[Any]:
    """The roster members who have at least one opponent the list allows.

    This is what the Start button holds against ``MIN_PLAYERS``: a member
    every other player is kept apart from can never be seated, so a lobby
    that only reaches the minimum by counting them has no game in it. The
    refusal it produces is the ordinary "Need at least N players" line — the
    count printed is the roster's, so the message is byte-identical to the
    one a genuinely short lobby gets.
    """
    banned = _forbidden_keys(forbidden_pairs)
    if not banned:
        return list(players)
    return [
        p for p in players
        if any(not _blocked(p, q, banned) for q in players if str(q) != str(p))
    ]


def pick_round_bye(
    player_ids: list[Any],
    bye_history: list[Any] | None = None,
    rng: random.Random | None = None,
    forbidden_pairs: Iterable[tuple[Any, Any]] | None = None,
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

    ``forbidden_pairs`` is the roster's no-contact set (as
    ``no_contact_pairs_among`` returns it; any id type, either way round).
    It changes three things, each the same rule :func:`create_matchups`
    applies after the window: a player the list keeps apart from *everyone*
    else on the roster is the bye (nobody can be seated opposite them, so
    they should not be asked to write); three players who include a pair
    bench one of the two rather than run the round-robin that would seat
    them; and an odd field's bye is picked so the remainder can still be
    paired safely. An even field that pairs safely has no bye, as before.
    """
    ids = list(player_ids)
    chooser = rng if rng is not None else random
    chooser.shuffle(ids)
    history = list(bye_history or [])
    banned = _forbidden_keys(forbidden_pairs)
    if banned:
        isolated = _isolated(ids, banned)
        if isolated:
            return min(isolated, key=history.count)
    if len(ids) == 3 and not _has_forbidden_pair(ids, banned):
        return None
    if len(ids) % 2 == 0:
        return None
    return _rotation_bye(ids, history, banned)


def create_matchups(
    answers: dict[str, str],
    bye_history: list[Any] | None = None,
    rng: random.Random | None = None,
    forbidden_pairs: Iterable[tuple[Any, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[Any]]:
    """Pair submitted answers for head-to-head voting.

    Returns ``(matchups, byes)``. Every submitter appears in the result
    exactly once — either in a pair or in ``byes``. In ordinary play
    ``byes`` holds at most one id (an odd count); only the no-contact gate
    below can add more, and an empty ``matchups`` means nothing could be
    voted on at all (``byes`` is then empty too — nobody is paid for a
    round that never ran; the cog skips it as a short round).

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

    No-contact gate: ``forbidden_pairs`` is the submitters' no-contact set
    (as ``no_contact_pairs_among`` returns it; any id type, either way
    round) and no such pair is ever a matchup. In order:

    * a submitter the list keeps apart from *every* other submitter is a
      bye — nobody can sit opposite them;
    * three submitters who include a pair do not round-robin (that would
      seat the pair): one of the two is the bye, fewest-byes-first between
      them, and the other two play one matchup;
    * an odd field's rotation bye is the most overdue player whose absence
      still leaves a fully pairable field;
    * the pairing itself is drawn by :func:`_safe_pairing` — still ten
      shuffles, still the fewest duplicate answers — and, in the contrived
      case where no full safe pairing exists, the largest safe one with
      the leftovers as byes. A round with no safe matchup at all returns
      ``([], [])``.

    Everything the gate does is a bye, and a bye is paid and announced the
    same way whatever caused it, so the card never says why. With nothing
    forbidden the gate is inert and the draw is byte-for-byte the one above.
    """
    chooser = rng if rng is not None else random
    player_ids = list(answers.keys())
    chooser.shuffle(player_ids)
    history = list(bye_history or [])
    banned = _forbidden_keys(forbidden_pairs)

    byes: list[Any] = []
    if banned:
        isolated = _isolated(player_ids, banned)
        for p in isolated:
            player_ids.remove(p)
        byes.extend(isolated)
        if len(player_ids) < 2:
            return [], []

    # Small games (3 players): round-robin every pair so each player
    # competes twice and the round has real action — unless one of those
    # pairs is forbidden, in which case the odd-count rule below benches
    # one of the two instead.
    if len(player_ids) == 3 and not _has_forbidden_pair(player_ids, banned):
        pairs: list[dict[str, Any]] = []
        for a, b in combinations(player_ids, 2):
            pairs.append({"pair": [a, b], "votes": {}, "winner": None})
        chooser.shuffle(pairs)
        return pairs, byes

    if len(player_ids) % 2 == 1:
        # Fewest byes so far wins the bye. player_ids is already
        # shuffled, so the first of a tied group is a random choice
        # among everyone equally overdue.
        bye_player = _rotation_bye(player_ids, history, banned)
        player_ids.remove(bye_player)
        byes.append(bye_player)

    # Same-answer pairings are ugly to vote on, so shuffle a few times
    # and keep the least-duplicated complete pairing we saw.
    best_matchups: list[dict[str, Any]] = []
    best_dupes: int | None = None
    if not banned:
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
                return pairs, byes
            if best_dupes is None or dupes < best_dupes:
                best_dupes = dupes
                best_matchups = pairs
        return best_matchups, byes

    # Constrained draw: the same ten shuffles, each turned into the largest
    # safe pairing that order allows. More matchups beat fewer duplicates.
    best_pairs: list[tuple[Any, Any]] = []
    for _ in range(10):
        chooser.shuffle(player_ids)
        safe = _safe_pairing(player_ids, banned)
        dupes = sum(
            1 for a, b in safe
            if answers[str(a)].strip().lower() == answers[str(b)].strip().lower()
        )
        if best_dupes is None or (len(safe), -dupes) > (len(best_pairs), -best_dupes):
            best_pairs, best_dupes = safe, dupes
        if len(safe) * 2 == len(player_ids) and dupes == 0:
            break
    seated = {str(p) for pair in best_pairs for p in pair}
    # Only the contrived no-full-pairing case leaves anyone here.
    byes.extend(p for p in player_ids if str(p) not in seated)
    if not best_pairs:
        return [], []
    return (
        [{"pair": [a, b], "votes": {}, "winner": None} for a, b in best_pairs],
        byes,
    )


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
