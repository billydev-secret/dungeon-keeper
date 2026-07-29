"""Tests for rebuilding a game's roster from its stored payload.

``game_roster`` is what lets the two outside-the-game end paths — the 24-hour
sweep and ``force_end_active_game`` (``/games end``) — pay the same room the
game's own completion site would have. Each case below mirrors the cited cog.
"""
from __future__ import annotations

import pytest

from bot_modules.games.utils.game_roster import NO_ROSTER_TYPES, roster_from_payload


@pytest.mark.parametrize(
    "game_type, payload, expected",
    [
        # traditional — opted-in categories; rounds = questions asked.
        ("traditional", {"participants": [1, 2, 3], "asked": {"1": "q"}}, ([1, 2, 3], 1)),
        # Ids round-trip as strings, junk is dropped, a double-join collapses.
        ("traditional", {"participants": ["4", None, "x", 4]}, ([4], 0)),
        # clapback — joined players; rounds = round_history entries.
        ("clapback", {"players": [1, 2], "round_history": [{}, {}]}, ([1, 2], 2)),
        # compliment / mfk — the join pool.
        ("compliment", {"participants": [7, 8]}, ([7, 8], 0)),
        ("mfk", {"participants": [1, 2, 3, 4]}, ([1, 2, 3, 4], 0)),
        # story — the writer list.
        ("story", {"players": [5, 6]}, ([5, 6], 0)),
        # legitlibs — players, one scored round.
        ("legitlibs", {"players": [9, 10]}, ([9, 10], 1)),
        # rushmore — the draft roster the view seeds from the payload.
        ("rushmore", {"players": [2, 3], "rounds": {"1": {}}}, ([2, 3], 1)),
        # ttl — explicit played list wins...
        ("ttl", {"played": ["11", "12"], "scores": {"99": {}}}, ([11, 12], 2)),
        # ...and legacy payloads fall back to scores keys.
        ("ttl", {"scores": {"11": {}, "12": {}}}, ([11, 12], 2)),
        # nhie — `lives` keeps eliminated players at 0 hp, so it is the full
        # roster; an eliminated player can still be the guiltiest winner.
        ("nhie", {"lives": {"1": 0, "2": 3}}, ([1, 2], 0)),
        # ama — askers plus hot seats; asker_id 0 is the AI sentinel.
        ("ama", {"questions": [
            {"asker_id": 1, "hot_seat_id": 2},
            {"asker_id": 0, "hot_seat_id": 2},
        ]}, ([1, 2], 2)),
        # hottakes — voters plus authors; a winning author may never have voted.
        ("hottakes", {"results": [{"voters": [1, 2], "author": 3}]}, ([1, 2, 3], 1)),
        ("hottakes", {"results": [{"voters": [1], "author": None}]}, ([1], 1)),
        # fantasies — entry authors plus both vote sides.
        ("fantasies", {"results": [
            {"author": 1, "same_votes": [2], "nope_votes": [3]},
        ]}, ([1, 2, 3], 1)),
        # wyr — everyone who voted either option in any round.
        ("wyr", {"rounds": {"1": {"a": [1, 2], "b": [3]}}}, ([1, 2, 3], 1)),
        # mlt — vote keys, not the survivors-only `players` list.
        ("mlt", {"rounds": {"1": {"votes": {"1": 9, "2": 9}}}, "players": [1]}, ([1, 2], 1)),
        # price — every uid that submitted a price in any round.
        ("price", {"rounds": {"1": {"prices": {"4": 10, "5": 20}}}}, ([4, 5], 1)),
        # Prompt-style types have no joined roster and must stay unpaid.
        ("ffa", {"prompt": "x", "seen": ["x"]}, ([], 0)),
        ("photo", {"submissions": {"1": "url"}}, ([], 0)),
        # Unknown type, empty and missing payloads.
        ("no_such_game", {"players": [1]}, ([], 0)),
        ("traditional", {}, ([], 0)),
        ("traditional", None, ([], 0)),
    ],
)
def test_roster_from_payload(game_type, payload, expected):
    assert roster_from_payload(game_type, payload) == expected


@pytest.mark.parametrize("game_type", sorted(NO_ROSTER_TYPES))
def test_no_roster_types_never_pay(game_type):
    """ffa and photo are posts, not games players sign into."""
    assert roster_from_payload(game_type, {"players": [1, 2], "participants": [3]}) == ([], 0)


@pytest.mark.parametrize(
    "game_type, payload",
    [
        ("wyr", {"rounds": "not-a-dict"}),
        ("hottakes", {"results": [None, "junk"]}),
        ("ama", {"questions": [{"asker_id": "nope"}]}),
        ("mlt", {"rounds": {"1": "junk"}}),
        ("nhie", {"lives": "junk"}),
        ("price", {"rounds": {"1": {"prices": "junk"}}}),
    ],
)
def test_malformed_payload_yields_no_roster_instead_of_raising(game_type, payload):
    """A bad payload costs its own game a roster, never the sweep it's found in."""
    players, _rounds = roster_from_payload(game_type, payload)
    assert players == []
