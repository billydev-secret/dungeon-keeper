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


# ── sub-game identification (2026-07-26) ─────────────────────────────────────
#
# Gamebot hosts CAH, Connect 4 and Anagrams in the same channel. CAH and
# Anagrams end with the *identical* "<@id> is the winner!" wording, so the
# terminal message can never tell them apart — the lobby embed is what names
# the game, and everything dispatches off that.

def _lobby(game: str, joined: list[int]) -> dict:
    return {"embeds": [{
        "title": f"host is starting a {game} game!",
        "description": 'Click "Join" below to join in the next **120 seconds**.',
        "fields": [{
            "name": "Joined Players",
            "value": ", ".join(f"<@{u}>" for u in joined),
            "inline": False,
        }],
    }]}


def _scoreboard(points: dict[str, int]) -> dict:
    return {"embeds": [{
        "title": "Scoreboard",
        "fields": [
            {"name": f"{name} - {n} POINTS", "value": "WORDS"}
            for name, n in points.items()
        ] + [{"name": "Pangram", "value": "The pangram was CLEANUP."}],
    }]}


def _not_enough_players() -> dict:
    return {"embeds": [
        {"title": "Time's up!", "description": "Not enough players joined the game!"}
    ]}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Cards Against Humanity", parser.GAME_CAH),
        ("Cards Against Humanity: Family Edition", parser.GAME_CAH),
        ("Connect 4", parser.GAME_CONNECT4),
        ("Anagrams", parser.GAME_ANAGRAMS),
        ("Chess", None),          # a real Gamebot game we have no parser for
        ("Survey Says", None),
    ],
)
def test_game_from_start_reads_the_lobby_embed(name, expected):
    embeds = _lobby(name, [ALICE])["embeds"]
    assert parser.is_game_start(embeds) is True
    assert parser.game_from_start(embeds) == expected


def test_is_game_start_ignores_non_lobby_embeds():
    assert parser.is_game_start(_standings({ALICE: 1})["embeds"]) is False
    assert parser.game_from_start(_game_over(ALICE)["embeds"]) is None


def test_identify_game_tells_anagrams_from_cah():
    # The regression that mattered: both terminals say "is the winner!", so
    # without the lobby every Anagrams game was credited as CAH.
    cah = [_lobby("Cards Against Humanity", [ALICE]), _game_over(ALICE)]
    ana = [_lobby("Anagrams", [ALICE]), _scoreboard({"alice": 900}), _game_over(ALICE)]
    assert parser.identify_game(cah) == parser.GAME_CAH
    assert parser.identify_game(ana) == parser.GAME_ANAGRAMS


def test_identify_game_returns_none_for_an_unparsed_game():
    window = [_lobby("Chess", [ALICE, BOB]),
              {"embeds": [{"title": "Game over!", "description": "Good game!"}]}]
    assert parser.identify_game(window) is None


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        ([_standings({ALICE: 1}), _game_over(ALICE)], parser.GAME_CAH),
        ([_scoreboard({"alice": 9}), _game_over(ALICE)], parser.GAME_ANAGRAMS),
        ([_c4_game_over(ALICE)], parser.GAME_CONNECT4),
        ([_game_over(ALICE)], parser.GAME_CAH),   # bare winner line: assume CAH
        ([_round_win(ALICE)], None),              # nothing terminal at all
    ],
)
def test_identify_game_falls_back_to_window_shape_without_a_lobby(window, expected):
    # Only reachable when the banked slice starts after the game began.
    assert parser.identify_game(window) == expected


def test_current_game_window_stops_at_its_own_lobby():
    # The previous game's standings sit in the same channel with no terminal
    # between them (an abandoned run). Bounding on the lobby keeps them out.
    parsed = [
        _standings({CAROL: 9}),                        # 0: stale, previous run
        _lobby("Cards Against Humanity", [ALICE, BOB]),  # 1: this game starts
        _standings({ALICE: 5, BOB: 1}),                # 2
        _game_over(ALICE),                             # 3
    ]
    window = parser.current_game_window(parsed, over_index=3)
    assert parser.identify_game(window) == parser.GAME_CAH
    scores, winner = parser.extract_cah_game(window)
    assert scores == {ALICE: 5, BOB: 1}   # CAROL's 9 is not in this game
    assert winner == ALICE


def test_is_abandoned_flags_a_lobby_that_never_filled():
    window = [
        _lobby("Cards Against Humanity", [ALICE, BOB]),
        _not_enough_players(),
        {"embeds": [{"title": "Game over!", "description": "To play fun games…"}]},
    ]
    assert parser.is_abandoned(window) is True
    # …and a real game isn't flagged.
    assert parser.is_abandoned([_standings({ALICE: 1}), _game_over(ALICE)]) is False


def test_join_phase_roster_is_gated_on_the_lobby_embed():
    # The Joined Players field used to be read off *any* embed, so an abandoned
    # CAH lobby fed its roster straight into the Connect 4 payout. A CAH lobby
    # is still a lobby (same join phase), but an unrelated embed carrying that
    # field name is not.
    assert parser.players_from_join_phase(
        _lobby("Cards Against Humanity", [ALICE, BOB])["embeds"]
    ) == {ALICE, BOB}
    stray = [{"title": "Scoreboard",
              "fields": [{"name": "Joined Players", "value": f"<@{CAROL}>"}]}]
    assert parser.players_from_join_phase(stray) == set()


def test_times_up_recap_only_counts_when_players_actually_joined():
    assert parser.players_from_join_phase(_c4_times_up([ALICE, BOB])["embeds"]) == {ALICE, BOB}
    assert parser.players_from_join_phase(_not_enough_players()["embeds"]) == set()


def test_standings_are_gated_on_the_embed_title():
    # A stray "<@id>: N" in ordinary game chatter must not invent a score.
    stray = [{"title": "This round's black card", "description": f"<@{ALICE}>: 99"}]
    assert parser.scores_from_standings(stray) == {}


# ── Anagrams ─────────────────────────────────────────────────────────────────

def test_scores_from_scoreboard_reads_points_out_of_field_names():
    embeds = _scoreboard({"efficientpanic": 900, "ceilruxdealta": 500, "jay": 0})["embeds"]
    assert parser.scores_from_scoreboard(embeds) == {
        "efficientpanic": 900, "ceilruxdealta": 500, "jay": 0,
    }  # the trailing Pangram field carries no score and is skipped


def test_extract_anagrams_game_pairs_usernames_with_the_mentioned_winner():
    window = [
        _lobby("Anagrams", [ALICE, BOB]),
        _scoreboard({"alice": 900, "bob": 500}),
        _game_over(ALICE),
    ]
    scores, winner = parser.extract_anagrams_game(window)
    assert scores == {"alice": 900, "bob": 500}
    assert winner == ALICE  # by mention, while the scores are by username


def test_scoreboard_usernames_are_markdown_unescaped():
    embeds = _scoreboard({r"dozer\_nation": 100})["embeds"]
    assert parser.scores_from_scoreboard(embeds) == {"dozer_nation": 100}


# ── Wordle (kind='wordle') ───────────────────────────────────────────────────
#
# One self-contained daily digest per group: no embeds, no lobby, nothing to
# scan back for. Scoring is inverted (1/6 is best) and ties on the 👑 line are
# normal, so there are usually several winners.

def _digest(*lines: str) -> str:
    return (
        "**Your group is on a 9 day streak!** 🔥 Here are yesterday's results:\n"
        + "\n".join(lines)
    )


def test_parse_wordle_results_inverts_the_guess_count():
    r = parser.parse_wordle_results(
        _digest(f"👑 1/6: <@{ALICE}>", f"4/6: <@{BOB}>", f"6/6: <@{CAROL}>")
    )
    assert r is not None
    # Fewer guesses must pay more, so 1/6 is the top score and 6/6 the floor.
    assert r.scores == {ALICE: 6, BOB: 3, CAROL: 1}
    assert r.winners == frozenset({ALICE})


def test_parse_wordle_results_failed_player_scores_zero_but_still_played():
    r = parser.parse_wordle_results(_digest(f"👑 3/6: <@{ALICE}>", f"X/6: <@{BOB}>"))
    assert r is not None
    assert r.scores == {ALICE: 4, BOB: 0}  # BOB is in the roster at 0, not absent


def test_parse_wordle_results_supports_tied_winners():
    # "👑 3/6: <@a> <@b> <@c>" is a real and common shape — everyone on the
    # crowned line won.
    r = parser.parse_wordle_results(
        _digest(f"👑 3/6: <@{ALICE}> <@{BOB}> <@{CAROL}>", "4/6: <@444>")
    )
    assert r is not None
    assert r.winners == frozenset({ALICE, BOB, CAROL})
    assert r.scores[444] == 3


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("👑 3/6: @Ciccio", {"Ciccio": 4}),
        ("6/6: @Olivia @UnfeelingFreedom", {"Olivia": 1, "UnfeelingFreedom": 1}),
        # A display name with a space would be shredded by whitespace splitting.
        ("4/6: @communal potato", {"communal potato": 3}),
    ],
)
def test_parse_wordle_results_splits_plain_names_on_the_at_sign(line, expected):
    # Wordle prints some players as bare "@Name" rather than mentioning them;
    # the caller resolves those by name.
    r = parser.parse_wordle_results(_digest(line))
    assert r is not None
    assert r.named_scores == expected


def test_parse_wordle_results_mixes_mentions_and_plain_names_on_one_line():
    r = parser.parse_wordle_results(_digest(f"👑 4/6: @Ciccio <@{ALICE}> <@{BOB}>"))
    assert r is not None
    assert r.scores == {ALICE: 3, BOB: 3}
    assert r.named_scores == {"Ciccio": 3}
    assert r.winners == frozenset({ALICE, BOB})
    assert r.named_winners == frozenset({"Ciccio"})


def test_wordle_chatter_is_not_a_digest():
    assert parser.parse_wordle_results("bigprop03 is playing") is None
    assert parser.parse_wordle_results("") is None
    # A digest header with no result lines pays nobody rather than half-parsing.
    assert parser.parse_wordle_results("Here are yesterday's results:") is None


# ── Co-ordle (kind='coordle') ────────────────────────────────────────────────

_COORDLE_EMPTY_ROW = "<:white_square:946958839192891402>" * 6


def _coordle_cells(colours: list[str]) -> str:
    return "".join(
        f"<:{c}_{w}:94647073314244610{i}>"
        for i, (c, w) in enumerate(zip(colours, "spirit"))
    )


def _coordle_row(n, colours, uid=None, pts=None, bonus=None) -> str:
    row = f"**`{n}.`** {_coordle_cells(colours)}"
    if uid is not None:
        score = f"**+{pts}" + (f" (+{bonus})" if bonus else "") + "**"
        row += f" <@!{uid}> {score}"
    return row


def _coordle_board(rows: list[str], ts: int = 1785103200) -> list[dict]:
    return [{"title": f"Co-ordle for <t:{ts}:f>", "description": "\n".join(rows)}]


_MISS = ["gray"] * 6
_HIT = ["green"] * 6


def test_coordle_board_is_recognised_and_keyed_on_its_round():
    embeds = _coordle_board([_coordle_row(1, _MISS, ALICE, 4)], ts=1785103200)
    assert parser.is_coordle_board(embeds) is True
    # Keyed on the round's scheduled time, not a message id: Co-ordle posts a
    # fresh board message for *every* guess, so a message-keyed payout would
    # pay the same round once per guess.
    assert parser.coordle_game_key(embeds) == 1785103200
    assert parser.is_coordle_board(
        [{"title": "Co-ordle Leaderboard for `The Golden Meadow`"}]
    ) is False


def test_extract_coordle_sums_the_bonus_into_each_players_points():
    # "**+1 (+2)**" means the player earned 1+2 — verified against the bot's own
    # cumulative leaderboard across consecutive snapshots.
    embeds = _coordle_board([
        _coordle_row(1, _MISS, ALICE, 1, bonus=2),
        _coordle_row(2, _MISS, ALICE, 1),
        _coordle_row(3, _MISS, BOB, 4),
        _coordle_row(4, _HIT, BOB, 5),
    ])
    scores, winner, state = parser.extract_coordle_game(embeds)
    assert scores == {ALICE: 4, BOB: 9}
    assert winner == BOB              # whoever played the all-green solving row
    assert state == parser.COORDLE_SOLVED


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([_coordle_row(1, _HIT, ALICE, 5)], parser.COORDLE_SOLVED),
        # Every row used and never solved.
        ([_coordle_row(i, _MISS, ALICE, 1) for i in range(1, 8)],
         parser.COORDLE_EXHAUSTED),
        # Guesses so far but rows to spare — still running, must not pay.
        ([_coordle_row(1, _MISS, ALICE, 1), f"**`2.`** {_COORDLE_EMPTY_ROW}"],
         parser.COORDLE_OPEN),
        # Nobody has guessed at all.
        ([f"**`{i}.`** {_COORDLE_EMPTY_ROW}" for i in range(1, 8)],
         parser.COORDLE_EMPTY),
    ],
    ids=["solved", "exhausted", "open", "empty"],
)
def test_extract_coordle_reads_finality_off_the_board(rows, expected):
    # Co-ordle never posts a terminal message, so the board itself is the only
    # signal that a round is over.
    _scores, _winner, state = parser.extract_coordle_game(_coordle_board(rows))
    assert state == expected


def test_extract_coordle_unsolved_round_has_no_winner():
    embeds = _coordle_board([_coordle_row(i, _MISS, ALICE, 1) for i in range(1, 8)])
    scores, winner, state = parser.extract_coordle_game(embeds)
    assert winner is None
    assert state == parser.COORDLE_EXHAUSTED
    assert scores == {ALICE: 7}


# ── host attribution (2026-07-26) ────────────────────────────────────────────
#
# The lobby title is the only place an external game names who started it, and
# it names them by username. External games paid no host bounty at all until
# this was read out.

@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("efficientpanic is starting a Cards Against Humanity game!", "efficientpanic"),
        ("regalruffian is starting a Connect 4 game!", "regalruffian"),
        ("efficientpanic is starting a Anagrams game!", "efficientpanic"),
        # Real usernames carry dots, underscores and spaces.
        ("secretagentman. is starting a Cards Against Humanity game!", "secretagentman."),
        ("displaced_alaskan is starting a Cards Against Humanity game!", "displaced_alaskan"),
        ("some person is starting a Chess game!", "some person"),
    ],
)
def test_host_from_lobby_reads_the_username_off_the_title(title, expected):
    assert parser.host_from_lobby([{"title": title}]) == expected


def test_host_from_lobby_ignores_non_lobby_embeds():
    assert parser.host_from_lobby(_game_over(ALICE)["embeds"]) is None
    assert parser.host_from_lobby(_standings({ALICE: 1})["embeds"]) is None


def test_host_from_window_finds_the_lobby_anywhere_in_the_game():
    window = [
        _lobby("Cards Against Humanity", [ALICE, BOB]),
        _standings({ALICE: 5, BOB: 1}),
        _game_over(ALICE),
    ]
    assert parser.host_from_window(window) == "host"
    # A window whose lobby aged out has no host to attribute.
    assert parser.host_from_window([_standings({ALICE: 1}), _game_over(ALICE)]) is None
