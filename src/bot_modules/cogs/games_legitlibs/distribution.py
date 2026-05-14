"""Pure round-robin assignment helpers for LegitLibs Classic mode.

These functions have no Discord or DB dependencies — they operate on plain
Python types so the logic is directly testable. Run this file as a script
(`python cogs/games_legitlibs/distribution.py`) to execute the built-in self-check.
"""


def assign_blanks_round_robin(blanks: list[dict], players: list[int]) -> dict[str, int]:
    """Assign each blank to a player via round-robin.

    Args:
        blanks:  Template blanks in template order; each dict must have an "id" key.
        players: Player user IDs in join order.

    Returns:
        Mapping of blank_id -> player_id. Empty dict if players is empty.
        Blanks beyond len(players) cycle back to the first player.
    """
    if not players:
        return {}
    return {
        blanks[i]["id"]: players[i % len(players)]
        for i in range(len(blanks))
    }


def compute_unfilled(blanks: list[dict], fills: dict) -> list[dict]:
    """Return blanks whose ids are not present in *fills*, preserving template order."""
    return [b for b in blanks if b["id"] not in fills]


def assign_rescue(unfilled: list[dict], volunteers: list[int]) -> dict[str, int]:
    """Round-robin the unfilled blanks across volunteers (in click order)."""
    if not volunteers or not unfilled:
        return {}
    return {
        unfilled[i]["id"]: volunteers[i % len(volunteers)]
        for i in range(len(unfilled))
    }


def players_done_count(
    assignments: dict[str, int],
    fills: dict,
    players: list[int],
) -> int:
    """How many players have all their assigned blanks filled.

    Players with zero assignments (possible when joiners > blanks) count as done.
    """
    done = 0
    for player_id in players:
        their_blanks = [bid for bid, pid in assignments.items() if pid == player_id]
        if all(bid in fills for bid in their_blanks):
            done += 1
    return done


def unique_contributors(fills: dict) -> list[int]:
    """Return the list of distinct player user_ids present in *fills*, stable order."""
    seen = []
    for data in fills.values():
        pid = data.get("by")
        if pid is not None and pid not in seen:
            seen.append(pid)
    return seen


def _self_check():
    """Sanity checks — invoked when this file is run directly."""
    # Round-robin, blanks > players
    blanks = [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"},
              {"id": "e"}, {"id": "f"}, {"id": "g"}]
    assignments = assign_blanks_round_robin(blanks, [1, 2, 3])
    assert assignments == {"a": 1, "b": 2, "c": 3, "d": 1, "e": 2, "f": 3, "g": 1}, \
        f"round-robin 7/3 wrong: {assignments}"

    # Blanks < players (extras get nothing)
    assignments_few = assign_blanks_round_robin(blanks[:2], [1, 2, 3, 4])
    assert assignments_few == {"a": 1, "b": 2}, f"few-blanks wrong: {assignments_few}"

    # Empty players
    assert assign_blanks_round_robin(blanks, []) == {}

    # Unfilled detection preserves template order
    fills = {"a": {"value": "x", "by": 1}, "c": {"value": "y", "by": 3}}
    unfilled = compute_unfilled(blanks, fills)
    assert [b["id"] for b in unfilled] == ["b", "d", "e", "f", "g"], \
        f"unfilled order wrong: {unfilled}"

    # Rescue distribution
    rescue = assign_rescue(unfilled, [2, 3])
    assert rescue == {"b": 2, "d": 3, "e": 2, "f": 3, "g": 2}, \
        f"rescue wrong: {rescue}"

    # Rescue with no volunteers → empty
    assert assign_rescue(unfilled, []) == {}

    # players_done_count: nobody has all their blanks filled
    full_assign = {"a": 1, "b": 2, "c": 3, "d": 1, "e": 2, "f": 3, "g": 1}
    done = players_done_count(full_assign, fills, [1, 2, 3])
    # P1 has a,d,g (only a filled); P2 has b,e (none filled); P3 has c,f (only c).
    assert done == 0, f"done count 0-case wrong: {done}"

    # Zero-assignment player counts as done
    done_zero = players_done_count(
        {"a": 1},                             # player 2 has no assignments
        {"a": {"value": "x", "by": 1}},       # player 1's blank is filled
        [1, 2],
    )
    assert done_zero == 2, f"zero-assignment done wrong: {done_zero}"

    # unique_contributors: stable order, deduplicated
    contribs = unique_contributors({
        "a": {"value": "x", "by": 1},
        "b": {"value": "y", "by": 2},
        "c": {"value": "z", "by": 1},
    })
    assert contribs == [1, 2], f"contribs wrong: {contribs}"

    print("distribution self-check passed")


if __name__ == "__main__":
    _self_check()
