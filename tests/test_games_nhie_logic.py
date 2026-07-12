"""Tests for the extracted Never Have I Ever pure-logic modules.

Covers ``bot_modules/games_nhie/logic.py`` (vote toggle, round-end
lives accounting, winner detection, payload codecs) and
``bot_modules/games_nhie/embeds.py`` (round, closed, recap embeds).
Mirrors the games_traditional template: the cog stays thin; this
module proves the extracted pieces work without spinning up Discord.
"""

from __future__ import annotations

import pytest

from bot_modules.games_nhie.embeds import (
    build_closed_embed,
    build_recap_embed,
    build_round_embed,
)
from bot_modules.games_nhie.logic import (
    DEFAULT_LIVES,
    apply_round_lives,
    apply_vote,
    bump_guilt_scores,
    encode_round_state,
    find_winner,
    payload_to_round_state,
)


# ── apply_vote ───────────────────────────────────────────────────────


def test_apply_vote_adds_new_guilty_voter():
    guilty: list[int] = []
    innocent: list[int] = []
    lives: dict[int, int] = {}
    changed = apply_vote(guilty, innocent, lives, 42, "guilty", max_lives=3)
    assert changed is False
    assert guilty == [42]
    assert innocent == []
    # Lazy registration in lives tracker
    assert lives == {42: 3}


def test_apply_vote_adds_new_innocent_voter():
    guilty: list[int] = []
    innocent: list[int] = []
    lives: dict[int, int] = {}
    changed = apply_vote(guilty, innocent, lives, 42, "innocent", max_lives=3)
    assert changed is False
    assert innocent == [42]
    assert guilty == []
    assert lives == {42: 3}


def test_apply_vote_switching_sides_returns_changed_true():
    """User on the innocent list votes guilty — they move and the flag fires."""
    guilty: list[int] = []
    innocent: list[int] = [42]
    lives: dict[int, int] = {42: 3}
    changed = apply_vote(guilty, innocent, lives, 42, "guilty", max_lives=3)
    assert changed is True
    assert guilty == [42]
    assert innocent == []


def test_apply_vote_idempotent_re_press_is_noop():
    """Pressing the same vote twice doesn't duplicate the entry."""
    guilty: list[int] = [42]
    innocent: list[int] = []
    lives: dict[int, int] = {42: 3}
    changed = apply_vote(guilty, innocent, lives, 42, "guilty", max_lives=3)
    assert changed is False
    assert guilty == [42]
    assert innocent == []
    # Lives untouched — still 3
    assert lives == {42: 3}


def test_apply_vote_does_not_overwrite_existing_lives():
    """A vote from an already-tracked user must NOT reset their HP."""
    guilty: list[int] = []
    innocent: list[int] = []
    lives: dict[int, int] = {42: 1}
    apply_vote(guilty, innocent, lives, 42, "guilty", max_lives=3)
    assert lives[42] == 1


def test_apply_vote_skips_lives_registration_when_max_lives_zero():
    """max_lives=0 means no elimination mode — don't track lives."""
    guilty: list[int] = []
    innocent: list[int] = []
    lives: dict[int, int] = {}
    apply_vote(guilty, innocent, lives, 42, "guilty", max_lives=0)
    assert lives == {}
    assert guilty == [42]


# ── apply_round_lives ────────────────────────────────────────────────


def test_apply_round_lives_decrements_guilty_voters():
    lives = {1: 3, 2: 3}
    eliminated: set[int] = set()
    newly = apply_round_lives(
        lives, eliminated, guilty=[1], innocent=[2], max_lives=3
    )
    assert lives == {1: 2, 2: 3}
    assert eliminated == set()
    assert newly == []


def test_apply_round_lives_eliminates_at_zero():
    lives = {1: 1}
    eliminated: set[int] = set()
    newly = apply_round_lives(
        lives, eliminated, guilty=[1], innocent=[], max_lives=3
    )
    assert lives == {1: 0}
    assert eliminated == {1}
    assert newly == [1]


def test_apply_round_lives_skips_already_eliminated():
    """A player still in `eliminated` doesn't lose another heart."""
    lives = {1: 0}
    eliminated: set[int] = {1}
    newly = apply_round_lives(
        lives, eliminated, guilty=[1], innocent=[], max_lives=3
    )
    assert lives == {1: 0}
    assert newly == []


def test_apply_round_lives_registers_untracked_guilty_with_full_bar_first():
    """First-time guilty voter is registered at max_lives, THEN decremented."""
    lives: dict[int, int] = {}
    eliminated: set[int] = set()
    newly = apply_round_lives(
        lives, eliminated, guilty=[42], innocent=[], max_lives=3
    )
    assert lives == {42: 2}
    assert newly == []


def test_apply_round_lives_registers_untracked_innocent_voters():
    """The easy-to-miss case: innocent voters who never voted before get
    added to the lives tracker so they render in "Still Standing"."""
    lives: dict[int, int] = {}
    eliminated: set[int] = set()
    apply_round_lives(
        lives, eliminated, guilty=[], innocent=[99], max_lives=3
    )
    assert lives == {99: 3}


def test_apply_round_lives_does_not_register_innocent_already_eliminated():
    lives = {99: 0}
    eliminated: set[int] = {99}
    apply_round_lives(
        lives, eliminated, guilty=[], innocent=[99], max_lives=3
    )
    assert lives == {99: 0}


def test_apply_round_lives_noop_when_max_lives_zero():
    lives: dict[int, int] = {}
    eliminated: set[int] = set()
    newly = apply_round_lives(
        lives, eliminated, guilty=[1, 2], innocent=[3], max_lives=0
    )
    assert lives == {}
    assert eliminated == set()
    assert newly == []


def test_apply_round_lives_multiple_eliminations_one_round():
    lives = {1: 1, 2: 1, 3: 3}
    eliminated: set[int] = set()
    newly = apply_round_lives(
        lives, eliminated, guilty=[1, 2], innocent=[3], max_lives=3
    )
    assert eliminated == {1, 2}
    assert set(newly) == {1, 2}
    assert lives[3] == 3


# ── find_winner ──────────────────────────────────────────────────────


def test_find_winner_continue_when_empty_lives():
    """Empty lives dict — game just started, no one's been tracked yet."""
    status, uid = find_winner({}, set())
    assert status == "continue"
    assert uid is None


def test_find_winner_continue_when_two_alive():
    status, uid = find_winner({1: 2, 2: 1}, set())
    assert status == "continue"
    assert uid is None


def test_find_winner_picks_last_alive():
    status, uid = find_winner({1: 0, 2: 2}, {1})
    assert status == "winner"
    assert uid == 2


def test_find_winner_picks_last_alive_without_elim_set():
    """A player with hp=0 but not yet in `eliminated` is still dead for
    winner-detection (the `hp > 0` test holds them out)."""
    status, uid = find_winner({1: 0, 2: 2}, set())
    assert status == "winner"
    assert uid == 2


def test_find_winner_all_eliminated():
    status, uid = find_winner({1: 0, 2: 0}, {1, 2})
    assert status == "all_eliminated"
    assert uid is None


# ── bump_guilt_scores ────────────────────────────────────────────────


def test_bump_guilt_scores_increments_per_user():
    scores: dict[str, int] = {}
    bump_guilt_scores(scores, [1, 2, 1])
    assert scores == {"1": 2, "2": 1}


def test_bump_guilt_scores_preserves_existing():
    scores = {"1": 5}
    bump_guilt_scores(scores, [1])
    assert scores == {"1": 6}


def test_bump_guilt_scores_empty_guilty_list_noop():
    scores = {"1": 5}
    bump_guilt_scores(scores, [])
    assert scores == {"1": 5}


# ── payload_to_round_state / encode_round_state ──────────────────────


def test_payload_to_round_state_decodes_string_keys():
    payload = {
        "lives": {"1": 3, "2": 1},
        "eliminated": ["3"],
        "max_lives": 5,
    }
    lives, elim, max_lives = payload_to_round_state(payload)
    assert lives == {1: 3, 2: 1}
    assert elim == {3}
    assert max_lives == 5


def test_payload_to_round_state_defaults_when_missing():
    lives, elim, max_lives = payload_to_round_state({})
    assert lives == {}
    assert elim == set()
    assert max_lives == DEFAULT_LIVES


def test_encode_round_state_round_trip():
    lives, elim = encode_round_state({1: 3, 2: 1}, {3})
    assert lives == {"1": 3, "2": 1}
    assert sorted(elim) == ["3"]


def test_codec_round_trip_preserves_state():
    """Encode then decode returns the same typed state."""
    original_lives = {1: 3, 7: 2}
    original_elim = {99}
    enc_lives, enc_elim = encode_round_state(original_lives, original_elim)
    payload = {"lives": enc_lives, "eliminated": enc_elim, "max_lives": 3}
    decoded_lives, decoded_elim, _ = payload_to_round_state(payload)
    assert decoded_lives == original_lives
    assert decoded_elim == original_elim


# ── build_round_embed ────────────────────────────────────────────────


def test_build_round_embed_active_title_and_color():
    embed = build_round_embed(
        statement="gone skydiving",
        guilty=[],
        innocent=[],
        round_num=1,
    )
    assert embed.title is not None
    assert "NEVER HAVE I EVER" in embed.title
    assert "ROUND OVER" not in embed.title


def test_build_round_embed_closed_title_changes():
    embed = build_round_embed(
        statement="x", guilty=[], innocent=[], round_num=1, closed=True
    )
    assert embed.title is not None
    assert "ROUND OVER" in embed.title


def test_build_round_embed_renders_round_and_statement_fields():
    embed = build_round_embed(
        statement="gone skydiving",
        guilty=[1, 2],
        innocent=[3],
        round_num=7,
    )
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Round"] == "7"
    assert by_name["Statement"] == "gone skydiving"
    votes = by_name["Votes"] or ""
    assert "😈" in votes
    assert "😇" in votes
    assert "(2)" in votes
    assert "(1)" in votes


def test_build_round_embed_escapes_markdown_in_statement():
    """Markdown characters in the statement are escaped to avoid
    breaking embed rendering."""
    embed = build_round_embed(
        statement="*sneaky* statement",
        guilty=[],
        innocent=[],
        round_num=1,
    )
    by_name = {f.name: f.value for f in embed.fields}
    statement = by_name["Statement"] or ""
    assert "\\*sneaky\\*" in statement


def test_build_round_embed_no_lives_section_when_lives_empty():
    """No `Still Standing` field when lives dict is empty (round 1
    before anyone has voted)."""
    embed = build_round_embed(
        statement="x", guilty=[], innocent=[], round_num=1, lives={}
    )
    names = {f.name or "" for f in embed.fields}
    assert not any("Still Standing" in n for n in names)
    assert "💀 Eliminated" not in names


def test_build_round_embed_shows_still_standing_when_lives_set():
    embed = build_round_embed(
        statement="x",
        guilty=[],
        innocent=[],
        round_num=2,
        lives={1: 3, 2: 1},
        max_lives=3,
    )
    standing_field = next(f for f in embed.fields if "Still Standing" in (f.name or ""))
    standing_name = standing_field.name or ""
    standing_value = standing_field.value or ""
    assert "(2)" in standing_name
    # Full HP shows three full hearts
    assert "❤️❤️❤️" in standing_value
    # User 2 has 1 of 3 hearts left
    assert "❤️🖤🖤" in standing_value


def test_build_round_embed_shows_eliminated_section():
    embed = build_round_embed(
        statement="x",
        guilty=[],
        innocent=[],
        round_num=2,
        lives={1: 3, 99: 0},
        eliminated={99},
        max_lives=3,
    )
    elim_field = next(f for f in embed.fields if "Eliminated" in (f.name or ""))
    elim_value = elim_field.value or ""
    assert "99" in elim_value
    # The eliminated player is NOT in the still-standing section
    standing_field = next(f for f in embed.fields if "Still Standing" in (f.name or ""))
    standing_value = standing_field.value or ""
    assert "99" not in standing_value


def test_build_round_embed_has_footer():
    embed = build_round_embed(
        statement="x", guilty=[], innocent=[], round_num=3
    )
    assert embed.footer.text is not None
    assert "Round 3" in embed.footer.text


# ── build_closed_embed ───────────────────────────────────────────────


def test_build_closed_embed_title_says_closed():
    embed = build_closed_embed(
        statement="x", guilty=[], innocent=[], round_num=1
    )
    assert embed.title is not None
    assert "CLOSED" in embed.title


def test_build_closed_embed_uses_recap_color():
    """Closed flips to the recap colour (dark gold), distinct from the
    round-over green."""
    closed = build_closed_embed(
        statement="x", guilty=[], innocent=[], round_num=1
    )
    round_over = build_round_embed(
        statement="x", guilty=[], innocent=[], round_num=1, closed=True
    )
    assert closed.colour != round_over.colour


# ── build_recap_embed ────────────────────────────────────────────────


def test_build_recap_embed_winner_in_description():
    embed = build_recap_embed(winner_id=42, guilt_scores={"42": 1})
    assert embed.description is not None
    assert "42" in embed.description
    assert "last one standing" in embed.description


def test_build_recap_embed_all_eliminated_message():
    embed = build_recap_embed(winner_id=None, guilt_scores={"1": 2, "2": 1})
    assert embed.description is not None
    assert "Everyone" in embed.description
    assert "eliminated" in embed.description.lower()


def test_build_recap_embed_lists_scores_sorted_descending():
    embed = build_recap_embed(
        winner_id=1, guilt_scores={"1": 1, "2": 5, "3": 3}
    )
    score_field = next(f for f in embed.fields if "Guilt" in (f.name or ""))
    score_value = score_field.value or ""
    # User 2 (highest) appears before user 3 (middle) appears before user 1
    lines = score_value.split("\n")
    assert "2" in lines[0]
    assert "3" in lines[1]
    assert "1" in lines[2]


def test_build_recap_embed_renders_dash_when_no_scores():
    embed = build_recap_embed(winner_id=None, guilt_scores={})
    score_field = next(f for f in embed.fields if "Guilt" in (f.name or ""))
    assert score_field.value == "—"


def test_build_recap_embed_title_says_game_over():
    embed = build_recap_embed(winner_id=1, guilt_scores={})
    assert embed.title is not None
    assert "GAME OVER" in embed.title


# ── sanity / integration ─────────────────────────────────────────────


@pytest.mark.parametrize("vote_kind", ["guilty", "innocent"])
def test_apply_vote_two_users_independent(vote_kind):
    guilty: list[int] = []
    innocent: list[int] = []
    lives: dict[int, int] = {}
    apply_vote(guilty, innocent, lives, 1, vote_kind, max_lives=3)
    apply_vote(guilty, innocent, lives, 2, vote_kind, max_lives=3)
    if vote_kind == "guilty":
        assert sorted(guilty) == [1, 2]
    else:
        assert sorted(innocent) == [1, 2]
    assert lives == {1: 3, 2: 3}


def test_full_round_flow_winner_emerges():
    """Simulate two rounds: both users guilty each round at 2 lives.
    After round 2, user 1's flagged guilty twice should die first
    while user 2 still has at least one heart — except both are
    guilty in lockstep here, so both die at the same round."""
    lives: dict[int, int] = {1: 2, 2: 2}
    eliminated: set[int] = set()
    apply_round_lives(lives, eliminated, guilty=[1, 2], innocent=[], max_lives=2)
    # Both at 1 hp, still alive
    status, _ = find_winner(lives, eliminated)
    assert status == "continue"
    apply_round_lives(lives, eliminated, guilty=[1, 2], innocent=[], max_lives=2)
    status, uid = find_winner(lives, eliminated)
    assert status == "all_eliminated"
    assert uid is None


def test_full_round_flow_single_winner():
    """User 1 votes innocent each round; user 2 votes guilty each round
    until eliminated."""
    lives: dict[int, int] = {}
    eliminated: set[int] = set()
    # Round 1
    apply_round_lives(lives, eliminated, guilty=[2], innocent=[1], max_lives=2)
    # User 1 now registered at 2, user 2 at 1
    assert lives[1] == 2
    assert lives[2] == 1
    # Round 2
    apply_round_lives(lives, eliminated, guilty=[2], innocent=[1], max_lives=2)
    status, uid = find_winner(lives, eliminated)
    assert status == "winner"
    assert uid == 1


# ── economy roster enrichment (Stage 2 faucet) ──────────────────────

from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

import bot_modules.cogs.games_nhie_cog as nhie_cog  # noqa: E402
from bot_modules.games.utils.game_manager import create_game  # noqa: E402
from bot_modules.services.games_db import GamesDb  # noqa: E402
from tests.fakes import FakeGuild  # noqa: E402


class _SpyBot:
    def __init__(self, db_path) -> None:
        self.games_db = GamesDb(db_path)
        self.active_views: dict = {}
        self.ctx = SimpleNamespace(db_path=db_path)

    def get_cog(self, name):
        return None


async def test_advance_winner_pays_survivors_and_eliminated(monkeypatch, sync_db_path):
    """The guiltiest winner may be an eliminated player, so the roster must be
    everyone who played (survivors + eliminated), not just survivors."""
    spy = AsyncMock()
    monkeypatch.setattr(nhie_cog, "end_game", spy)
    bot = _SpyBot(sync_db_path)
    payload = {
        "rounds": {"1": {}},
        "lives": {"1": 1, "2": 1},
        "eliminated": [],
        "guilt_scores": {},
        "max_lives": 1,
    }
    gid = await create_game(bot.games_db, 100, 1, "nhie", payload=payload)
    bot.active_views[gid] = object()
    cog = nhie_cog.NHIECog(bot)  # type: ignore[arg-type]
    guild = FakeGuild(id=9001)
    channel = SimpleNamespace(id=100, guild=guild, send=AsyncMock())
    view = cog._build_round_view(
        game_id=gid, host_id=1, host_name="Host", round_num=1, channel=channel,
        guild=guild, statement="X", lives={1: 1, 2: 1}, eliminated=set(),
        max_lives=1, interaction=None,
    )
    view.guilty = [1]     # player 1 admits guilt -> loses last life -> eliminated
    view.innocent = [2]   # player 2 survives -> winner
    await view.advance_callback(SimpleNamespace(edit=AsyncMock()))
    call = spy.await_args
    assert call is not None and spy.await_count == 1
    # Eliminated guiltiest (1) is included alongside survivor (2).
    assert sorted(call.kwargs["player_ids"]) == [1, 2]
    assert call.kwargs["bot"] is bot
