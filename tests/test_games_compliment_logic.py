"""Tests for the extracted Spin-the-Compliment pure-logic modules.

Covers ``bot_modules/games_compliment/logic.py`` (participant toggle,
pairing generation wrapper, payload serialisation) and
``bot_modules/games_compliment/embeds.py`` (lobby and pairings embeds,
line formatter). Mirrors the games_traditional template: the cog stays
thin; this module proves the extracted pieces work without spinning
up Discord.
"""

from __future__ import annotations

import random

import pytest

from bot_modules.games_compliment.embeds import (
    build_lobby_embed,
    build_pairings_embed,
    format_pairing_line,
)
from bot_modules.games_compliment.logic import (
    generate_pairings,
    pairing_ids,
    serialize_pairings,
    toggle_participant,
)


# ── toggle_participant ───────────────────────────────────────────────


def test_toggle_participant_adds_new_user_and_returns_added():
    payload: dict = {}
    action = toggle_participant(payload, user_id=42)
    assert action == "added to"
    assert payload["participants"] == [42]


def test_toggle_participant_removes_existing_user_and_returns_removed():
    payload = {"participants": [42]}
    action = toggle_participant(payload, user_id=42)
    assert action == "removed from"
    assert payload["participants"] == []


def test_toggle_participant_creates_list_when_missing():
    """First call on an empty payload should set up the list."""
    payload: dict = {}
    toggle_participant(payload, 1)
    assert "participants" in payload


def test_toggle_participant_preserves_other_users():
    payload = {"participants": [1, 2, 3]}
    toggle_participant(payload, 2)
    assert payload["participants"] == [1, 3]


def test_toggle_participant_appends_to_existing_list():
    payload = {"participants": [1]}
    action = toggle_participant(payload, 2)
    assert action == "added to"
    assert payload["participants"] == [1, 2]


def test_toggle_participant_is_idempotent_pair():
    """Add then remove returns the payload to its starting state."""
    payload: dict = {}
    toggle_participant(payload, 42)
    toggle_participant(payload, 42)
    assert payload["participants"] == []


# ── generate_pairings ────────────────────────────────────────────────


def test_generate_pairings_empty_for_fewer_than_two():
    """One-player game can't be deranged — the shared helper returns {}."""
    assert generate_pairings([]) == {}
    assert generate_pairings([1]) == {}


def test_generate_pairings_every_player_is_giver_and_receiver():
    """Each id appears exactly once on each side of the mapping."""
    participants = [1, 2, 3, 4, 5]
    pairings = generate_pairings(participants)
    assert set(pairings.keys()) == set(participants)
    assert set(pairings.values()) == set(participants)


def test_generate_pairings_no_self_pairing():
    """Sattolo guarantees ``giver != receiver`` for every entry."""
    participants = list(range(2, 12))
    # Run multiple times because shuffle is random
    for _ in range(20):
        pairings = generate_pairings(participants)
        for giver, receiver in pairings.items():
            assert giver != receiver


def test_generate_pairings_two_player_swap():
    """Two players can only swap — every call returns ``{a: b, b: a}``."""
    pairings = generate_pairings([1, 2])
    assert pairings == {1: 2, 2: 1} or pairings == {2: 1, 1: 2}


# ── serialize_pairings ───────────────────────────────────────────────


def test_serialize_pairings_keys_become_strings():
    assert serialize_pairings({1: 2, 3: 4}) == {"1": 2, "3": 4}


def test_serialize_pairings_empty():
    assert serialize_pairings({}) == {}


def test_serialize_pairings_preserves_receiver_values():
    """Only the giver key is stringified; the receiver int is untouched."""
    result = serialize_pairings({100: 200})
    assert result["100"] == 200
    assert isinstance(result["100"], int)


# ── pairing_ids ──────────────────────────────────────────────────────


def test_pairing_ids_returns_each_user_once():
    pairings = {1: 2, 2: 3, 3: 1}
    ids = pairing_ids(pairings)
    assert sorted(ids) == [1, 2, 3]


def test_pairing_ids_dedupes_when_user_is_both_giver_and_receiver():
    """A circular triangle has every user listed as both giver and
    receiver — they should each appear once."""
    pairings = {1: 2, 2: 3, 3: 1}
    ids = pairing_ids(pairings)
    # exactly 3 ids, no duplicates
    assert len(ids) == 3
    assert len(set(ids)) == 3


def test_pairing_ids_empty():
    assert pairing_ids({}) == []


def test_pairing_ids_preserves_iteration_order():
    """Ordering follows giver-then-receiver iteration of the dict."""
    pairings = {10: 20, 20: 30, 30: 10}
    ids = pairing_ids(pairings)
    # First giver is 10, its receiver is 20; then 30 from the third entry.
    assert ids[0] == 10
    assert 20 in ids
    assert 30 in ids


# ── build_lobby_embed ────────────────────────────────────────────────


def test_build_lobby_embed_empty_pool_shows_dash():
    embed = build_lobby_embed("Alice", [])
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Host"] == "Alice"
    assert by_name["Pool (0)"] == "—"


def test_build_lobby_embed_lists_participants():
    embed = build_lobby_embed("Alice", ["Bob", "Carol"])
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Pool (2)"] == "Bob, Carol"


def test_build_lobby_embed_title_uses_compliment_label():
    embed = build_lobby_embed("Alice", [])
    assert embed.title is not None
    assert "Spin the Compliment" in embed.title


def test_build_lobby_embed_has_footer():
    embed = build_lobby_embed("Alice", [])
    assert embed.footer.text is not None
    assert "Spin the Compliment" in embed.footer.text


# ── format_pairing_line ──────────────────────────────────────────────


def test_format_pairing_line_uses_arrow():
    assert format_pairing_line("<@1>", "<@2>") == "<@1> → <@2>"


def test_format_pairing_line_preserves_raw_strings():
    """Plain-string ids (no member resolved) still render with the arrow."""
    line = format_pairing_line("Alice", "Bob")
    assert "Alice" in line
    assert "Bob" in line
    assert "→" in line


# ── build_pairings_embed ─────────────────────────────────────────────


def test_build_pairings_embed_title_and_color():
    embed = build_pairings_embed(["<@1> → <@2>"])
    assert embed.title is not None
    assert "Compliment Pairings" in embed.title


def test_build_pairings_embed_joins_lines_with_newlines():
    lines = ["A → B", "B → C", "C → A"]
    embed = build_pairings_embed(lines)
    assert embed.description is not None
    for line in lines:
        assert line in embed.description


def test_build_pairings_embed_appends_call_to_action():
    embed = build_pairings_embed(["A → B"])
    assert embed.description is not None
    assert "deliver your compliment" in embed.description.lower()


def test_build_pairings_embed_handles_single_pair():
    embed = build_pairings_embed(["A → B"])
    assert embed.description is not None
    assert "A → B" in embed.description


def test_build_pairings_embed_has_footer():
    embed = build_pairings_embed(["A → B"])
    assert embed.footer.text is not None
    assert "Spin the Compliment" in embed.footer.text


# ── integration ──────────────────────────────────────────────────────


def test_full_lobby_flow_pair_of_users():
    """Two players join the pool, then pairings are generated."""
    payload: dict = {}
    toggle_participant(payload, 1)
    toggle_participant(payload, 2)
    assert payload["participants"] == [1, 2]
    pairings = generate_pairings(payload["participants"])
    # Two-player game must always swap
    assert pairings[1] == 2 and pairings[2] == 1


def test_serialize_then_pairing_ids_consistent():
    """Whatever ids appear in the pairings show up in pairing_ids."""
    random.seed(0)
    pairings = generate_pairings([10, 20, 30, 40])
    ids = pairing_ids(pairings)
    serialized = serialize_pairings(pairings)
    # Each serialized key (as int) is in ids
    for key in serialized:
        assert int(key) in ids


@pytest.mark.parametrize("pool_size", [2, 3, 4, 5, 8])
def test_generate_pairings_size_matches_pool(pool_size):
    pairings = generate_pairings(list(range(pool_size)))
    assert len(pairings) == pool_size


# ── economy roster enrichment (Stage 2 faucet) ──────────────────────

from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

import bot_modules.cogs.games_compliment_cog as compliment_cog  # noqa: E402
from bot_modules.games.utils.game_manager import create_game  # noqa: E402
from bot_modules.services.games_db import GamesDb  # noqa: E402
from tests.fakes import FakeUser, fake_interaction  # noqa: E402


class _SpyBot:
    def __init__(self, db_path) -> None:
        self.games_db = GamesDb(db_path)
        self.active_views: dict = {}
        self.ctx = SimpleNamespace(db_path=db_path)

    def get_cog(self, name):
        return None


async def test_close_generate_pays_participants(monkeypatch, sync_db_path):
    """Generating pairings pays everyone who joined the pool."""
    spy = AsyncMock()
    monkeypatch.setattr(compliment_cog, "end_game", spy)
    bot = _SpyBot(sync_db_path)
    gid = await create_game(bot.games_db, 100, 1, "compliment", payload={"participants": [1, 2, 3]})
    view = compliment_cog.ComplimentView(gid, 1, bot.games_db, bot)  # type: ignore[arg-type]
    interaction = fake_interaction(user=FakeUser(id=1))
    interaction.guild = None
    interaction.followup.send = AsyncMock(return_value=SimpleNamespace(delete=AsyncMock()))
    await view.close_generate.callback(interaction)
    call = spy.await_args
    assert call is not None and spy.await_count == 1
    assert call.kwargs["player_ids"] == [1, 2, 3]
    assert call.kwargs["bot"] is bot
