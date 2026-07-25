"""Tests for games_external.parser — Gamebot CAH + Connect 4 parsing (#70).

Fixtures mirror the real /games track sample: standings (`<@id>: N`),
submission status (`✅ <@id> Submitted!`), round wins, and the terminal
`Game over!` embed (`<@id> is the winner!`) for CAH; the join-phase start
embed, the `Time's up!` recap, and the terminal `Game over!` embed
(`<@id> has won!`) for Connect 4.
"""

from __future__ import annotations

import pytest

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


# ── Connect 4 fixtures ─────────────────────────────────────────────────────────

def _c4_start(host: int, joined: list[int]) -> dict:
    return {"embeds": [{
        "title": f"host{host} is starting a Connect 4 game!",
        "description": 'Click "Join" below to join in the next **120 seconds**.',
        "fields": [{
            "name": "Joined Players",
            "value": ", ".join(f"<@{u}>" for u in joined),
            "inline": False,
        }],
    }]}


def _c4_times_up(joined: list[int]) -> dict:
    desc = ", ".join(f"<@{u}>" for u in joined) + " joined the game!"
    return {"embeds": [{"title": "Time's up!", "description": desc}]}


def _c4_move() -> dict:
    return {"embeds": [{"description": (
        "First to 4 in a row wins!\n```board```\n<@111> 🔴, select a column between 1-7!"
    )}]}


def _c4_game_over(winner: int) -> dict:
    return {"embeds": [{"title": "Game over!", "description": f"<@{winner}> has won! ```board```"}]}


def test_players_from_standings():
    embeds = _standings({ALICE: 5, BOB: 1, CAROL: 0})["embeds"]
    assert parser.players_from_standings(embeds) == {ALICE, BOB, CAROL}


def test_scores_from_standings():
    embeds = _standings({ALICE: 5, BOB: 1, CAROL: 0})["embeds"]
    assert parser.scores_from_standings(embeds) == {ALICE: 5, BOB: 1, CAROL: 0}


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


def test_is_terminal_true_for_either_games_over_embed():
    # Both games share the "Game over!" title — is_terminal is title-only,
    # unlike is_game_over which is CAH's "is the winner" phrasing specifically.
    assert parser.is_terminal(_game_over(ALICE)["embeds"]) is True
    assert parser.is_terminal(_c4_game_over(ALICE)["embeds"]) is True
    assert parser.is_terminal(_round_win(ALICE)["embeds"]) is False


def test_is_game_over_does_not_match_connect4s_game_over():
    # Connect 4's "<@id> has won!" must not be mistaken for CAH's finish.
    assert parser.is_game_over(_c4_game_over(ALICE)["embeds"]) is False


def test_extract_cah_game_unions_roster_and_finds_winner():
    window = [
        _submissions([ALICE, BOB, CAROL]),
        _round_win(BOB),
        _standings({ALICE: 5, BOB: 1, CAROL: 1}),
        _game_over(ALICE),
    ]
    scores, winner = parser.extract_cah_game(window)
    assert scores == {ALICE: 5, BOB: 1, CAROL: 1}
    assert winner == ALICE


def test_extract_cah_game_last_standings_supersedes_earlier_ones():
    # Current Standings is a cumulative snapshot each time — later posts
    # replace earlier scores for the same player rather than merging with them.
    window = [
        _standings({ALICE: 2, BOB: 0}),
        _standings({ALICE: 5, BOB: 1}),
        _game_over(ALICE),
    ]
    scores, winner = parser.extract_cah_game(window)
    assert scores == {ALICE: 5, BOB: 1}
    assert winner == ALICE


def test_extract_cah_game_submission_only_player_folded_in_at_zero():
    # A player who submitted before the first standings post (or left before
    # any standings) still counts as having played, at score 0.
    window = [
        _submissions([ALICE, BOB, CAROL]),
        _standings({ALICE: 3, BOB: 1}),
        _game_over(ALICE),
    ]
    scores, winner = parser.extract_cah_game(window)
    assert scores == {ALICE: 3, BOB: 1, CAROL: 0}
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
    scores, winner = parser.extract_cah_game(window)
    assert scores == {CAROL: 5, DAVE: 2}
    assert winner == CAROL


def test_extract_handles_no_winner():
    scores, winner = parser.extract_cah_game([_standings({ALICE: 2, BOB: 2})])
    assert scores == {ALICE: 2, BOB: 2}
    assert winner is None


# ── Connect 4 (#70 follow-up) ──────────────────────────────────────────────────

def test_players_from_connect4_start_reads_joined_players_field():
    embeds = _c4_start(999, [ALICE, BOB])["embeds"]
    assert parser.players_from_connect4_start(embeds) == {ALICE, BOB}


def test_players_from_connect4_start_reads_times_up_recap():
    embeds = _c4_times_up([ALICE, BOB])["embeds"]
    assert parser.players_from_connect4_start(embeds) == {ALICE, BOB}


def test_players_from_connect4_ignores_unrelated_embeds():
    # A move embed mentions the current player, but that's not a join signal.
    assert parser.players_from_connect4_start(_c4_move()["embeds"]) == set()


def test_winner_from_connect4_over():
    embeds = _c4_game_over(ALICE)["embeds"]
    assert parser.winner_from_connect4_over(embeds) == ALICE


def test_extract_connect4_game_unions_roster_and_finds_winner():
    window = [
        _c4_start(999, [ALICE, BOB]),
        _c4_times_up([ALICE, BOB]),
        _c4_move(),
        _c4_game_over(ALICE),
    ]
    roster, winner = parser.extract_connect4_game(window)
    assert roster == {ALICE, BOB}
    assert winner == ALICE


def test_extract_connect4_game_folds_in_absent_winner():
    roster, winner = parser.extract_connect4_game([_c4_game_over(ALICE)])
    assert roster == {ALICE}
    assert winner == ALICE


def test_extract_connect4_game_unrecognised_finish_pays_participation_only():
    # A draw's exact wording isn't confirmed yet — an unmatched "Game over!"
    # must still credit the roster, just with no winner.
    window = [
        _c4_start(999, [ALICE, BOB]),
        {"embeds": [{"title": "Game over!", "description": "It's a draw! ```board```"}]},
    ]
    roster, winner = parser.extract_connect4_game(window)
    assert roster == {ALICE, BOB}
    assert winner is None


def test_current_game_window_bounds_across_mixed_game_types():
    # A CAH game ends, then a Connect 4 game starts and ends in the same
    # channel (same Gamebot account) — the Connect 4 window must not reach
    # back into the CAH game's roster.
    parsed = [
        _standings({ALICE: 5, BOB: 3}),  # 0: CAH game A
        _game_over(ALICE),               # 1: CAH game A ends
        _c4_start(999, [BOB, CAROL]),    # 2: Connect 4 game B
        _c4_game_over(BOB),              # 3: Connect 4 game B ends
    ]
    window = parser.current_game_window(parsed, over_index=3)
    roster, winner = parser.extract_connect4_game(window)
    assert roster == {BOB, CAROL}
    assert winner == BOB


# ── Cat Bot (#65) ─────────────────────────────────────────────────────────────

_CATCH = (
    "ceilruxdealta cought <:nicecat:1279106518423441478> Nice cat!!!!1!\n"
    "You now have 208 cats of dat type!!!\n"
    "this fella was cought in 2 minutes 7.00 seconds!!!!"
)
_CATCH_DOUBLED = (
    "efficientpanic cought <:wildcat:1279106513129967750> Wild cat!!!!1!\n"
    "You now have 138 cats of dat type!!!\n"
    "this fella was cought in 6 minutes 33.05 seconds!!!!\n"
    "💫 rjoy_26 blessed your catch and it got doubled!"
)
_CATCH_REVERSE = "!1!!!!cat Reverse <:reversecat:1279106519581069313> cought ceilruxdealta"
# Cat Bot markdown-escapes underscores in usernames, in both word orders.
_CATCH_ESCAPED = (
    "tryingnewthingz\\_0504 cought <:nicecat:1279106518423441478> Nice cat!!!!1!\n"
    "You now have 2,505 cats of dat type!!!"
)
_CATCH_ESCAPED_REVERSE = (
    "!1!!!!cat Reverse <:reversecat:1279106519581069313> cought tryingnewthingz\\_0504"
)
_SPAWN = "** A <:finecat:1279106515894141019> @Cats! has appeared**\nCatch Fine for cuddles!!"
_BONUS = (
    "🎁 **BONUS <:reversecat:1279106519581069313> REVERSE CAT!**\n"
    "Anyone who cought this cat can play a minigame and potentially **get +3 more!**"
)


def test_rarity_coins_tiers():
    assert parser.rarity_coins("fine") == 1       # common
    assert parser.rarity_coins("wild") == 3       # uncommon (the *Rare* cat lives here too)
    assert parser.rarity_coins("rare") == 3
    assert parser.rarity_coins("reverse") == 11   # rare tier
    assert parser.rarity_coins("legendary") == 35
    assert parser.rarity_coins("mythic") == 102
    assert parser.rarity_coins("egirl") == 300
    assert parser.rarity_coins("frobnicate") == 1  # unknown -> common


def test_parse_cat_catch_normal():
    catch = parser.parse_cat_catch(_CATCH)
    assert catch is not None
    assert catch.username == "ceilruxdealta"
    assert catch.rarity == "nice"
    assert catch.doubled is False
    assert catch.coins == 1


def test_parse_cat_catch_blessed_doubles_coins():
    catch = parser.parse_cat_catch(_CATCH_DOUBLED)
    assert catch is not None
    assert catch.username == "efficientpanic"
    assert catch.rarity == "wild"
    assert catch.doubled is True
    assert catch.coins == 6  # 3 (uncommon) x2


def test_parse_cat_catch_reverse_cat():
    catch = parser.parse_cat_catch(_CATCH_REVERSE)
    assert catch is not None
    assert catch.username == "ceilruxdealta"   # the non-emoji token by "cought"
    assert catch.rarity == "reverse"
    assert catch.coins == 11


@pytest.mark.parametrize(
    "content", [_CATCH_ESCAPED, _CATCH_ESCAPED_REVERSE], ids=["normal", "reverse"]
)
def test_parse_cat_catch_unescapes_markdown_in_username(content):
    """Cat Bot prints ``tryingnewthingz\\_0504``; the payout resolves the *real*
    username by name, so the escapes have to come off or the catch pays nobody.
    """
    catch = parser.parse_cat_catch(content)
    assert catch is not None
    assert catch.username == "tryingnewthingz_0504"


def test_spawn_and_bonus_are_not_catches():
    assert parser.parse_cat_catch(_SPAWN) is None
    assert parser.parse_cat_catch(_BONUS) is None
    assert parser.parse_cat_catch("") is None
