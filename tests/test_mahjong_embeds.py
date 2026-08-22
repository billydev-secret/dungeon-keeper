"""Render tests for the mahjong embeds — stage 6 of docs/plans/meadow-mahjong.md.

Embeds are pure (state + names + color in, embed out), so every phase's
table panel is rendered here against real engine states — a render crash is
the classic cog bug, and none of these need a Discord mock. Rules stay
untested here; stage 3 owns them.
"""

from __future__ import annotations

import random

import discord
import pytest

from bot_modules.games.mahjong import embeds as mj_embeds
from bot_modules.games.mahjong import game_logic as G
from bot_modules.games.mahjong.card_logic import load_first_light
from bot_modules.games.mahjong.game_logic import Phase, TableConfig
from bot_modules.games.mahjong.tiles import Tile, shuffled_wall

CARD = load_first_light()
ACCENT = discord.Color(0xDAA520)
NAMES = {100: "Wren", 101: "Moss", 102: "Fern", 103: "Sage"}


def rng():
    return random.Random(5)


def state_seq():
    """One state per phase, driven through the real engine."""
    out = {}
    state = G.create_table(TableConfig(seat_count=2), 100)
    out["lobby_open"] = state
    state, _ = G.join_table(state, 101)
    out["lobby_full"] = state
    state, _ = G.deal(state, shuffled_wall(rng()))
    out["charleston"] = state
    r = rng()
    for _ in range(3):
        for s in range(2):
            picks = [t for t in state.seats[s].rack if t is not Tile.JOKER][:3]
            state, _ = G.charleston_pick(state, s, picks, 0, r)
    out["vote"] = state
    state, _ = G.vote_second_charleston(state, 0, False)
    out["courtesy_propose"] = state
    state, _ = G.courtesy_propose(state, 0, 1)
    state, _ = G.courtesy_propose(state, 1, 2)
    out["courtesy_pick"] = state
    give0 = [t for t in state.seats[0].rack if t is not Tile.JOKER][:1]
    give1 = [t for t in state.seats[1].rack if t is not Tile.JOKER][:1]
    state, _ = G.courtesy_pick(state, 0, give0)
    state, _ = G.courtesy_pick(state, 1, give1)
    out["await_discard"] = state
    tile = state.seats[0].rack[0]
    state, _ = G.discard(state, 0, tile)
    out["claim_window"] = state
    state, _ = G.timeout(state, CARD, r)
    out["mid_play"] = state
    return out


STATES = state_seq()


@pytest.mark.parametrize("phase_name", sorted(STATES))
def test_table_panel_renders_every_phase(phase_name):
    state = STATES[phase_name]
    embed = mj_embeds.build_table_panel(state, NAMES, stake=1, escrow=450,
                                  accent=ACCENT, deadline_at=None)
    assert embed.title and "Duel" in embed.title
    assert embed.footer.text and "Meadow Mahjong" in embed.footer.text


def test_lobby_panel_shows_open_seats_and_escrow():
    embed = mj_embeds.build_table_panel(STATES["lobby_open"], NAMES, stake=2,
                                  escrow=900, accent=ACCENT, deadline_at=None)
    assert "open seat" in (embed.description or "")
    assert any("900" in (f.value or "") for f in embed.fields)


def test_claim_window_panel_names_the_discarder():
    embed = mj_embeds.build_table_panel(STATES["claim_window"], NAMES, stake=1,
                                  escrow=450, accent=ACCENT, deadline_at=None)
    joined = " ".join(f"{f.name} {f.value}" for f in embed.fields)
    assert "Claim Window" in joined and "Wren" in joined


def test_settled_and_closed_panels_render():
    settled = G.GameState(
        config=TableConfig(seat_count=2), host=100,
        seats=[G.SeatState(member_id=100, rack=[Tile("1d")] * 13),
               G.SeatState(member_id=101, rack=[Tile("9c")] * 13)],
        phase=Phase.SETTLE, hand_no=1,
        outcome=G.Outcome(kind="mahjong", winner=1, line_id="gh-1",
                          line_name="Golden Hour", value=25, won_by="discard",
                          jokerless_double=True, discarder=0,
                          point_deltas={0: -100, 1: 100}),
    )
    embed = mj_embeds.build_table_panel(settled, NAMES, 1, 450, ACCENT, None)
    joined = " ".join(f"{f.name} {f.value}" for f in embed.fields)
    assert "Golden Hour" in joined and "Rematch" in joined
    closed, _ = G.close_table(settled, "finished")
    embed = mj_embeds.build_table_panel(closed, NAMES, 1, 450, ACCENT, None)
    assert embed.footer.text and "Closed" in embed.footer.text


def test_rack_panel_highlights_the_drawn_tile():
    state = STATES["mid_play"]
    seat = state.turn
    assert state.drawn is not None
    embed = mj_embeds.build_rack_panel(state, seat, ACCENT, None)
    assert "➜" in (embed.description or "")
    other = (seat + 1) % 2
    embed = mj_embeds.build_rack_panel(state, other, ACCENT, None)
    assert "➜" not in (embed.description or "")


def test_settlement_embed_monospace_table_and_notes():
    out = G.Outcome(kind="mahjong", winner=1, line_id="gh-1",
                    line_name="Golden Hour", value=25, won_by="discard",
                    jokerless_double=True, discarder=0,
                    point_deltas={0: -100, 1: 100})
    embed = mj_embeds.build_settlement(out, {0: "Wren", 1: "Moss"}, stake=2)
    desc = embed.description or ""
    assert "```" in desc and "+200" in desc and "-200" in desc
    assert "jokerless: doubled" in desc
    assert "discard win 2×" in desc  # §6.1: the exact line used, Duel form
    assert embed.color and embed.color.value == mj_embeds.COLOR_GREEN  # int constant

    wall = G.Outcome(kind="wall_game", winner=None, line_id=None,
                     line_name=None, value=0, won_by=None,
                     jokerless_double=False, discarder=None,
                     point_deltas={0: 0, 1: 0})
    assert "escrow" in (mj_embeds.build_settlement(wall, NAMES, 1).description or "")
    void = G.Outcome(kind="all_fallow", winner=None, line_id=None,
                     line_name=None, value=0, won_by=None,
                     jokerless_double=False, discarder=None,
                     point_deltas={0: 0, 1: 0})
    assert "voids" in (mj_embeds.build_settlement(void, NAMES, 1).description or "")


def test_reveal_and_redeem_and_stats_and_viewer():
    out = G.Outcome(kind="mahjong", winner=0, line_id="qp-1",
                    line_name="Quiet Pairs", value=50, won_by="self_pick",
                    jokerless_double=False, discarder=None,
                    point_deltas={0: 150, 1: -150})
    reveal = mj_embeds.build_mahjong_reveal(out, "Wren", "1D 1D 3D 3D")
    assert "Quiet Pairs" in (reveal.title or "") and "self-picked" in (reveal.description or "")

    redeem = mj_embeds.build_joker_redeemed("Wren", "Moss", Tile("5b"))
    assert "5B" in (redeem.description or "")

    stats = mj_embeds.build_my_stats(
        [{"mode": 2, "hands_played": 4, "wins": 2, "jokerless_wins": 1,
          "coins_won": 300, "coins_lost": 100, "biggest_win": 150}], ACCENT)
    assert "2/4" in stats.fields[0].value
    assert "chair" in (mj_embeds.build_my_stats([], ACCENT).description or "")

    pages = mj_embeds.build_card_viewer(CARD, ACCENT)
    text = " ".join(f.value for e in pages for f in e.fields)
    for section in CARD.sections():
        assert any(section in (f.name or "") for e in pages for f in e.fields)
    assert "Golden Hour" in text  # a hand name made it through
    for e in pages:  # discord field cap
        assert all(len(f.value or "") <= 1024 for f in e.fields)


def test_member_panel_variants():
    e = mj_embeds.build_member_panel(CARD, (1, 2, 5), 250, ACCENT)
    assert "First Light" in (e.description or "") and "250" in (e.description or "")
    e = mj_embeds.build_member_panel(None, (1,), 0, ACCENT)
    assert "No Meadow Card" in (e.description or "")
