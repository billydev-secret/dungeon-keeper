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


# ── Assist block on the rack panel (plans/mahjong-assist.md stage 3) ─────────


def _assist_state():
    from tests.test_mahjong_game_logic import play_state
    return play_state(2, {0: "flower*4 2d*4 6b*2 9d wn", 1: "9c*13"})


def _readout(mode: str):
    state = _assist_state()
    return state, G.assist_readout(state, 0, CARD, mode)


def _rack_field(embed, name):
    return next((f.value for f in embed.fields if f.name == name), None)


def test_rack_panel_without_assist_has_no_block():
    state = _assist_state()
    embed = mj_embeds.build_rack_panel(state, 0, ACCENT, None, assist=None)
    assert _rack_field(embed, "Closest Hands") is None


def test_assist_target_names_lines_and_distance_only():
    state, r = _readout("target")
    embed = mj_embeds.build_rack_panel(state, 0, ACCENT, None, assist=r)
    block = _rack_field(embed, "Closest Hands")
    assert block is not None
    top = r.prospects[0]
    assert top.hand.name in block
    assert "away" in block
    assert "need" not in block
    assert "discard" not in block.lower()


def test_assist_gap_adds_the_needed_tiles():
    state, r = _readout("gap")
    embed = mj_embeds.build_rack_panel(state, 0, ACCENT, None, assist=r)
    block = _rack_field(embed, "Closest Hands")
    assert block is not None and "need" in block
    assert "discard" not in block.lower()


def test_assist_coach_adds_dead_weight_and_suggestion():
    state, r = _readout("coach")
    assert r is not None and r.suggestion is not None
    embed = mj_embeds.build_rack_panel(state, 0, ACCENT, None, assist=r)
    block = _rack_field(embed, "Closest Hands")
    assert block is not None
    assert "Dead weight" in block
    assert "Consider discarding" in block


def test_assist_coach_silence_is_explicit():
    # Rail-silenced suggestion still shows dead weight, with the your-call note.
    state, r = _readout("coach")
    silenced = G.AssistReadout(
        mode="coach", prospects=r.prospects, live_count=r.live_count,
        suggestion=None,
    )
    embed = mj_embeds.build_rack_panel(state, 0, ACCENT, None, assist=silenced)
    block = _rack_field(embed, "Closest Hands")
    assert block is not None
    assert "No clearly safe discard" in block
    assert "Consider discarding" not in block


def test_assist_all_dead_says_play_for_the_wall():
    state = _assist_state()
    empty = G.AssistReadout(mode="gap", prospects=(), live_count=0, suggestion=None)
    embed = mj_embeds.build_rack_panel(state, 0, ACCENT, None, assist=empty)
    block = _rack_field(embed, "Closest Hands")
    assert block is not None and "play for the wall" in block


def test_assist_block_fits_a_discord_field():
    for mode in ("target", "gap", "coach"):
        state, r = _readout(mode)
        embed = mj_embeds.build_rack_panel(state, 0, ACCENT, None, assist=r)
        block = _rack_field(embed, "Closest Hands")
        assert block is not None and len(block) <= 1024


# ── Stage-4 review findings: honest dead weight + overflow-proof field ───────


def _fake_prospect(hand, distance, needed, dead_weight):
    from bot_modules.games.mahjong.match_logic import Prospect
    return Prospect(hand=hand, distance=distance, needed=tuple(needed),
                    dead_weight=tuple(dead_weight))


def test_dead_weight_is_the_intersection_of_shown_hands():
    # A tile hand #2 still NEEDS must never print as dead weight: the copy
    # promises "tiles none of your closest hands can use".
    h1, h2 = CARD.hands[0], CARD.hands[1]
    p1 = _fake_prospect(h1, 4, [(Tile("8c"), 2)],
                        [(Tile("9d"), 1), (Tile("wn"), 1)])
    p2 = _fake_prospect(h2, 5, [(Tile("wn"), 1)], [(Tile("9d"), 1)])
    r = G.AssistReadout(mode="coach", prospects=(p1, p2), live_count=2,
                        suggestion=None)
    state = _assist_state()
    embed = mj_embeds.build_rack_panel(state, 0, ACCENT, None, assist=r)
    block = _rack_field(embed, "Closest Hands")
    assert block is not None
    dead_line = next(ln for ln in block.split("\n") if ln.startswith("Dead weight"))
    assert "9D" in dead_line
    assert "N" not in dead_line  # hand #2 needs the wind — not dead


def test_assist_field_survives_the_registered_emoji_map(monkeypatch):
    # Once prod emoji registration runs, every chip becomes ~28 chars of
    # <:mm_xx:snowflake> markup. The field must still fit 1024 WITHOUT a
    # blind slice: degrade to text chips rather than cut mid-token and
    # silently drop the coach suggestion (review finding, stage 4).
    from bot_modules.games.mahjong import tile_render

    fake_map = {t.code: 1400000000000000001 for t in Tile}
    fake_map["back"] = 1400000000000000001
    monkeypatch.setattr(tile_render, "_map_cache", fake_map)

    # Adversarial worst case: three far lines with maximal need lists.
    hands = CARD.hands[:3]
    wide = [(t, 2) for t in list(Tile)[:10]]
    prospects = tuple(
        _fake_prospect(h, 14, wide, [(Tile("9d"), 2), (Tile("wn"), 1)])
        for h in hands
    )
    r = G.AssistReadout(mode="coach", prospects=prospects, live_count=22,
                        suggestion=Tile("9d"))
    state = _assist_state()
    embed = mj_embeds.build_rack_panel(state, 0, ACCENT, None, assist=r)
    block = _rack_field(embed, "Closest Hands")
    assert block is not None
    assert len(block) <= 1024
    # the advice coach exists to deliver is never the part that gets cut
    assert "Consider discarding" in block
    # and no half-sliced emoji token renders as garbage
    assert block.count("<:") == block.count(">") or "<:" not in block


def test_card_viewer_shows_notes_and_parity():
    # Grammar-verify G1: the odd/even locks and "these #s only" annotations
    # must be visible in Discord, not just on the web viewer — and long
    # sections split across fields instead of slicing mid-hand.
    from bot_modules.games.mahjong.card_logic import load_card_file
    from bot_modules.games.mahjong.card_logic import FIRST_LIGHT_PATH

    card = load_card_file(FIRST_LIGHT_PATH.parent / "meadow_harvest.json")
    embeds = mj_embeds.build_card_viewer(card, ACCENT)
    text = "\n".join(f.value for e in embeds for f in e.fields)
    assert "odd like numbers" in text
    assert "any 2 dragons" in text
    total_lines = sum(
        f.value.count("`") // 2 for e in embeds for f in e.fields)
    assert total_lines == len(card.hands)   # nothing sliced away
    for e in embeds:
        for f in e.fields:
            assert len(f.value) <= 1024
