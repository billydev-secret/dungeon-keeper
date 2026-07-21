"""Tests for games_external.parser — Gamebot CAH parsing (#70).

Fixtures mirror the real /games track sample: standings (`<@id>: N`),
submission status (`✅ <@id> Submitted!`), round wins, and the terminal
`Game over!` embed (`<@id> is the winner!`).
"""

from __future__ import annotations

from bot_modules.games_external import parser

ALICE, BOB, CAROL = 111, 222, 333


def _standings(scores: dict[int, int]) -> dict:
    desc = "\n".join(f"<@{uid}>: {n}" for uid, n in scores.items())
    return {"embeds": [{"title": "Current Standings", "description": desc}]}


def _submissions(uids: list[int]) -> dict:
    desc = "\n".join(f"✅ <@{uid}> Submitted!" for uid in uids)
    return {"embeds": [{"title": "Submission status", "description": desc}]}


def _round_win(uid: int) -> dict:
    return {"embeds": [{"description": (
        f"The winning card is **Judge Judy.** which belonged to <@{uid}>!\n\n"
        f"<@{uid}> has earned a point."
    )}]}


def _game_over(winner: int) -> dict:
    return {"embeds": [{"title": "Game over!", "description": (
        f"<@{winner}> is the winner!\nVote for Gamebot on top.gg!"
    )}]}


def test_players_from_standings():
    embeds = _standings({ALICE: 5, BOB: 1, CAROL: 0})["embeds"]
    assert parser.players_from_standings(embeds) == {ALICE, BOB, CAROL}


def test_players_from_submissions():
    embeds = _submissions([ALICE, BOB])["embeds"]
    assert parser.players_from_submissions(embeds) == {ALICE, BOB}


def test_winner_and_is_game_over():
    embeds = _game_over(ALICE)["embeds"]
    assert parser.winner_from_game_over(embeds) == ALICE
    assert parser.is_game_over(embeds) is True


def test_round_win_is_not_game_over():
    # A per-round point ("has earned a point") must not end/settle the game.
    embeds = _round_win(ALICE)["embeds"]
    assert parser.is_game_over(embeds) is False
    assert parser.winner_from_game_over(embeds) is None


def test_extract_cah_game_unions_roster_and_finds_winner():
    window = [
        _submissions([ALICE, BOB, CAROL]),
        _round_win(BOB),
        _standings({ALICE: 5, BOB: 1, CAROL: 1}),
        _game_over(ALICE),
    ]
    roster, winner = parser.extract_cah_game(window)
    assert roster == {ALICE, BOB, CAROL}
    assert winner == ALICE


def test_current_game_window_bounds_on_previous_game_over():
    # Two back-to-back games in one channel; the second must not inherit the
    # first's roster.
    DAVE = 444
    parsed = [
        _standings({ALICE: 5, BOB: 3}),   # 0: game A
        _game_over(ALICE),                # 1: game A ends
        _standings({CAROL: 5, DAVE: 2}),  # 2: game B
        _game_over(CAROL),                # 3: game B ends
    ]
    window = parser.current_game_window(parsed, over_index=3)
    roster, winner = parser.extract_cah_game(window)
    assert roster == {CAROL, DAVE}
    assert winner == CAROL


def test_extract_handles_no_winner():
    roster, winner = parser.extract_cah_game([_standings({ALICE: 2, BOB: 2})])
    assert roster == {ALICE, BOB}
    assert winner is None
