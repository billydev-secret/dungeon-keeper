"""Tests for the extracted Would-You-Rather pure-logic modules.

Covers ``bot_modules/games_wyr/logic.py`` (question parser, vote
toggle, next-button label) and ``bot_modules/games_wyr/embeds.py``
(round embed open/closed/revealed states + closed-game variant).
Mirrors the pressure_cooker pattern: the cog file stays thin; this
module proves the extracted pieces work without spinning up Discord.
"""

from __future__ import annotations

import pytest

from bot_modules.games.constants import (
    GAME_ICONS,
    PHASE_PLAYING,
    PHASE_RECAP,
    PHASE_RESULTS,
)
from bot_modules.games_wyr.embeds import build_closed_embed, build_wyr_embed
from bot_modules.games_wyr.logic import (
    next_button_label,
    parse_question_input,
    toggle_vote,
)

# ── parse_question_input ─────────────────────────────────────────────


def test_parse_question_input_splits_simple_pair():
    assert parse_question_input("fly | be invisible") == ("fly", "be invisible")


def test_parse_question_input_strips_whitespace_around_options():
    assert parse_question_input("  swim   |   fly  ") == ("swim", "fly")


def test_parse_question_input_returns_none_for_empty_string():
    assert parse_question_input("") is None


def test_parse_question_input_returns_none_for_whitespace_only():
    assert parse_question_input("   \n\t  ") is None


def test_parse_question_input_returns_none_when_no_pipe():
    assert parse_question_input("just one option") is None


def test_parse_question_input_returns_none_when_left_half_empty():
    assert parse_question_input("  | something") is None


def test_parse_question_input_returns_none_when_right_half_empty():
    assert parse_question_input("something |  ") is None


def test_parse_question_input_returns_none_when_both_halves_empty():
    assert parse_question_input(" | ") is None


def test_parse_question_input_uses_only_first_pipe():
    """Extra pipes in option B are preserved as-is — split(|, 1)."""
    assert parse_question_input("a | b | c") == ("a", "b | c")


# ── toggle_vote ──────────────────────────────────────────────────────


def test_toggle_vote_a_records_fresh_vote_returns_false():
    votes_a: list[int] = []
    votes_b: list[int] = []
    changed = toggle_vote(votes_a, votes_b, user_id=42, choice="a")
    assert changed is False
    assert votes_a == [42]
    assert votes_b == []


def test_toggle_vote_b_records_fresh_vote_returns_false():
    votes_a: list[int] = []
    votes_b: list[int] = []
    changed = toggle_vote(votes_a, votes_b, user_id=42, choice="b")
    assert changed is False
    assert votes_a == []
    assert votes_b == [42]


def test_toggle_vote_switching_from_b_to_a_returns_true():
    votes_a: list[int] = []
    votes_b: list[int] = [42]
    changed = toggle_vote(votes_a, votes_b, 42, "a")
    assert changed is True
    assert votes_a == [42]
    assert votes_b == []


def test_toggle_vote_switching_from_a_to_b_returns_true():
    votes_a: list[int] = [42]
    votes_b: list[int] = []
    changed = toggle_vote(votes_a, votes_b, 42, "b")
    assert changed is True
    assert votes_a == []
    assert votes_b == [42]


def test_toggle_vote_re_pressing_same_side_is_idempotent():
    """A user already on side A pressing A again is a no-op (no duplicate
    in the list and changed=False)."""
    votes_a: list[int] = [42]
    votes_b: list[int] = []
    changed = toggle_vote(votes_a, votes_b, 42, "a")
    assert changed is False
    assert votes_a == [42]
    assert votes_b == []


def test_toggle_vote_preserves_other_voters():
    """Switching one user must not disturb anyone else's vote."""
    votes_a: list[int] = [1, 2]
    votes_b: list[int] = [3, 4]
    toggle_vote(votes_a, votes_b, 3, "a")
    assert votes_a == [1, 2, 3]
    assert votes_b == [4]


def test_toggle_vote_raises_on_invalid_choice():
    with pytest.raises(ValueError):
        toggle_vote([], [], 1, "c")


# ── next_button_label ────────────────────────────────────────────────


def test_next_button_label_zero():
    assert next_button_label(0) == "⏭️ Next (0 queued)"


def test_next_button_label_one():
    assert next_button_label(1) == "⏭️ Next (1 queued)"


def test_next_button_label_many():
    assert next_button_label(17) == "⏭️ Next (17 queued)"


# ── build_wyr_embed ──────────────────────────────────────────────────


def _field_by_name(embed) -> dict[str, str]:
    return {f.name: f.value for f in embed.fields}


def test_build_wyr_embed_title_when_open():
    embed = build_wyr_embed("Alice", "fly", "swim", [], [], False, 1)
    assert embed.title is not None
    assert "WOULD YOU RATHER" in embed.title
    assert "ROUND OVER" not in embed.title
    assert embed.color is not None
    assert embed.color.value == PHASE_PLAYING


def test_build_wyr_embed_title_when_closed():
    embed = build_wyr_embed("Alice", "fly", "swim", [], [], False, 1, closed=True)
    assert embed.title is not None
    assert "ROUND OVER" in embed.title
    assert embed.color is not None
    assert embed.color.value == PHASE_RESULTS


def test_build_wyr_embed_shows_round_and_options():
    embed = build_wyr_embed("Alice", "fly", "swim", [], [], False, 3)
    by_name = _field_by_name(embed)
    assert by_name["Round"] == "3"
    assert by_name["🅰️"] == "fly"
    assert by_name["🅱️"] == "swim"


def test_build_wyr_embed_escapes_markdown_in_options():
    """Discord markdown in option text must be escaped so the embed
    can't be tricked into rendering bold or links from user input."""
    embed = build_wyr_embed("Alice", "**bold**", "_italic_", [], [], False, 1)
    by_name = _field_by_name(embed)
    # Asterisks/underscores get escaped (backslash-prefixed)
    assert "\\*" in by_name["🅰️"]
    assert "\\_" in by_name["🅱️"]


def test_build_wyr_embed_counts_votes_in_labels():
    embed = build_wyr_embed("Alice", "fly", "swim", [1, 2, 3], [4], False, 1)
    by_name = _field_by_name(embed)
    votes_field = by_name["Votes"]
    assert "(3)" in votes_field  # A count
    assert "(1)" in votes_field  # B count


def test_build_wyr_embed_revealed_lists_voter_mentions():
    embed = build_wyr_embed("Alice", "fly", "swim", [1, 2], [3], False, 1, revealed=True)
    votes_field = _field_by_name(embed)["Votes"]
    assert "<@1>" in votes_field
    assert "<@2>" in votes_field
    assert "<@3>" in votes_field


def test_build_wyr_embed_revealed_uses_dash_when_a_side_empty():
    """No voters on a side renders as an em-dash placeholder, not blank."""
    embed = build_wyr_embed("Alice", "fly", "swim", [], [5], False, 1, revealed=True)
    votes_field = _field_by_name(embed)["Votes"]
    # The A side has no voters -> dash placeholder
    assert "—" in votes_field
    assert "<@5>" in votes_field


def test_build_wyr_embed_anonymous_badge_in_footer():
    embed = build_wyr_embed("Alice", "fly", "swim", [], [], True, 1)
    assert embed.footer.text is not None
    assert "Anonymous" in embed.footer.text


def test_build_wyr_embed_no_anonymous_badge_when_off():
    embed = build_wyr_embed("Alice", "fly", "swim", [], [], False, 1)
    assert embed.footer.text is not None
    assert "Anonymous" not in embed.footer.text


def test_build_wyr_embed_footer_includes_round_number():
    embed = build_wyr_embed("Alice", "fly", "swim", [], [], False, 7)
    assert embed.footer.text is not None
    assert "Round 7" in embed.footer.text


def test_build_wyr_embed_renders_game_icon_in_title():
    embed = build_wyr_embed("Alice", "fly", "swim", [], [], False, 1)
    assert embed.title is not None
    assert GAME_ICONS["wyr"] in embed.title


# ── build_closed_embed ───────────────────────────────────────────────


def test_build_closed_embed_title_says_closed():
    embed = build_closed_embed("Alice", "fly", "swim", [1], [2], True, 1)
    assert embed.title is not None
    assert "CLOSED" in embed.title
    assert "ROUND OVER" not in embed.title  # CLOSED overrides the ROUND-OVER suffix


def test_build_closed_embed_uses_recap_color():
    embed = build_closed_embed("Alice", "fly", "swim", [1], [2], True, 1)
    assert embed.color is not None
    assert embed.color.value == PHASE_RECAP


def test_build_closed_embed_preserves_vote_counts():
    """The CLOSED embed still shows the final vote tallies."""
    embed = build_closed_embed("Alice", "fly", "swim", [1, 2], [3], True, 4)
    by_name = _field_by_name(embed)
    assert by_name["Round"] == "4"
    assert "(2)" in by_name["Votes"]
    assert "(1)" in by_name["Votes"]


def test_build_closed_embed_can_reveal_voters():
    embed = build_closed_embed("Alice", "fly", "swim", [1], [2], True, 1, revealed=True)
    votes_field = _field_by_name(embed)["Votes"]
    assert "<@1>" in votes_field
    assert "<@2>" in votes_field


# ── economy roster enrichment (Stage 2 faucet) ──────────────────────

from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

import bot_modules.cogs.games_wyr_cog as wyr_cog  # noqa: E402
from bot_modules.games.utils.game_manager import create_game  # noqa: E402
from bot_modules.services.games_db import GamesDb  # noqa: E402


class _SpyBot:
    def __init__(self, db_path) -> None:
        self.games_db = GamesDb(db_path)
        self.active_views: dict = {}
        self.ctx = SimpleNamespace(db_path=db_path)

    def get_cog(self, name):
        return None


async def test_run_round_empty_bank_pays_all_voters(monkeypatch, sync_db_path):
    """When the question bank runs dry, the game ends paying everyone who voted
    for either option across all completed rounds."""
    spy = AsyncMock()
    monkeypatch.setattr(wyr_cog, "end_game", spy)
    monkeypatch.setattr(wyr_cog, "get_wyr_question", AsyncMock(return_value=None))
    bot = _SpyBot(sync_db_path)
    payload = {"rounds": {
        "1": {"a": [1, 2], "b": [3], "q": "A OR B"},
        "2": {"a": [4], "b": [1], "q": "C OR D"},
    }}
    gid = await create_game(bot.games_db, 100, 1, "wyr", payload=payload)
    bot.active_views[gid] = object()
    cog = wyr_cog.WYRCog(bot)  # type: ignore[arg-type]
    channel = SimpleNamespace(id=100, guild=None, send=AsyncMock())
    await cog._run_round(None, gid, 1, "Host", 3, channel)
    call = spy.await_args
    assert call is not None and spy.await_count == 1
    assert call.kwargs["player_ids"] == [1, 2, 3, 4]
    assert call.kwargs["bot"] is bot
