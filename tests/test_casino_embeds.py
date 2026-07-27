"""Casino result-card colors — the win/loss pair must stay the sanctioned
semantic set. A big win is still a win: it gets ``COLOR_GREEN`` like any other,
with the celebration carried by the copy, not by a third color tier.
"""
from __future__ import annotations

import discord

from bot_modules.cogs.casino.embeds import (
    build_derby_race_embed,
    build_derby_result_embed,
    build_derby_round_embed,
    build_help_embed,
    build_roulette_round_embed,
    build_slots_embed,
    build_slots_spin_embed,
)
from bot_modules.services import casino_logic as logic
from bot_modules.services.casino_service import CasinoSettings
from bot_modules.services.economy_service import EconSettings
from bot_modules.services.embeds import COLOR_GREEN, COLOR_RED

_ECON = EconSettings(currency_emoji="💎", currency_name="gem", currency_plural="gems")
_REELS = ("🍯", "🍯", "🍯")


def _slots(stake: int, payout: int, *, jackpot: int = 0) -> discord.Embed:
    return build_slots_embed(
        _ECON, 42, _REELS, stake, payout, "Three of a kind!", jackpot_won=jackpot
    )


def test_small_win_is_green():
    assert _slots(10, 20).color == discord.Color(COLOR_GREEN)


def test_big_win_is_the_same_green_not_a_third_tier():
    assert _slots(10, 500).color == discord.Color(COLOR_GREEN)


def test_jackpot_win_is_green_but_keeps_its_copy():
    embed = _slots(10, 500, jackpot=5000)
    assert embed.color == discord.Color(COLOR_GREEN)
    assert embed.title is not None and "JACKPOT" in embed.title


def test_loss_is_red():
    assert _slots(10, 0).color == discord.Color(COLOR_RED)


def test_slots_reels_sit_inside_the_cabinet():
    """Result and spin frames both box the reel row in the text-art
    machine; unrevealed reels spin as 🌀."""
    final = _slots(10, 20)
    spin = build_slots_spin_embed(_ECON, 42, 10, ("🍯", None, None), None)
    for embed in (final, spin):
        assert embed.description is not None
        assert "┤🎰├" in embed.description
    assert "▶ 🍯 │ 🍯 │ 🍯 ◀" in (final.description or "")
    assert "▶ 🍯 │ 🌀 │ 🌀 ◀" in (spin.description or "")


# ── derby ──────────────────────────────────────────────────────────────

_FINAL = [logic.DERBY_TRACK_LEN, 8, 6, 9, 4, 3]


def test_derby_result_with_winners_is_green_and_names_the_winner():
    embed = build_derby_result_embed(
        _ECON, 0, _FINAL, [(42, "🐇 Hazel the Hare", 10, 25)]
    )
    assert embed.color == discord.Color(COLOR_GREEN)
    assert embed.description is not None
    assert "Hazel the Hare" in embed.description


def test_derby_result_all_losses_is_red():
    embed = build_derby_result_embed(
        _ECON, 0, _FINAL, [(42, "🐌 Turbo the Snail", 10, 0)]
    )
    assert embed.color == discord.Color(COLOR_RED)


def test_derby_race_frame_draws_every_runner():
    embed = build_derby_race_embed(_ECON, [0] * len(logic.DERBY_FIELD), None)
    assert embed.description is not None
    lines = embed.description.splitlines()
    assert len(lines) == len(logic.DERBY_FIELD)
    for line, runner in zip(lines, logic.DERBY_FIELD):
        assert line.startswith("🏁") and line.endswith(runner.emoji)


# ── the live bets board must never outgrow the 1024 field limit ────────

_LONG_ECON = EconSettings(
    currency_emoji="💎",
    currency_name="sunflower doubloon",
    currency_plural="sunflower doubloons of the golden meadow",
)


def _bets_value(embed: discord.Embed) -> str:
    field = next(f for f in embed.fields if (f.name or "").startswith("Bets"))
    assert field.value is not None
    return field.value


def test_round_embed_bets_fields_stay_under_the_field_limit():
    """Dozens of bets with a long currency name and 6-digit stakes — the
    exact shape that 400'd the repaint before the cap — must fit."""
    bets = [
        (10_000 + i, "🦋 Flutter the Butterfly", 250_000) for i in range(40)
    ]
    for embed in (
        build_derby_round_embed(_LONG_ECON, 1_800_000_000.0, bets, None),
        build_roulette_round_embed(_LONG_ECON, 1_800_000_000.0, bets, None),
    ):
        value = _bets_value(embed)
        assert len(value) <= 1024
        assert "earlier bet(s)" in value  # the tail is summarized, not lost


def test_round_embed_bets_show_newest_first():
    bets = [(1, "🔴 Red", 10), (2, "⚫ Black", 20)]
    value = _bets_value(build_roulette_round_embed(_ECON, 0.0, bets, None))
    assert value.index("<@2>") < value.index("<@1>")


# ── How It Works lists only the open tables ────────────────────────────


def test_help_embed_hides_closed_tables():
    settings = CasinoSettings(derby_enabled=False, slots_enabled=False)
    names = [f.name for f in build_help_embed(_ECON, settings, None).fields]
    assert "🏇 Derby" not in names and "🎰 Slots" not in names
    assert "🪙 Coinflip" in names and "🎡 Roulette" in names


# ── the hub panel's floor ticker ───────────────────────────────────────


def test_hub_embed_shows_ticker_lines_newest_first():
    from bot_modules.cogs.casino.embeds import build_hub_embed

    embed = build_hub_embed(
        _ECON, CasinoSettings(channel_id=1), None,
        ticker=[(2, "slots", 50, 500), (1, "coinflip", 10, 0)],
    )
    field = next(f for f in embed.fields if "On the floor" in (f.name or ""))
    assert field.value is not None
    assert field.value.index("<@2>") < field.value.index("<@1>")
    assert "**500**" in field.value  # the win shows its payout
    assert "the house" in field.value  # the loss names its destination


def test_hub_embed_omits_empty_ticker():
    from bot_modules.cogs.casino.embeds import build_hub_embed

    embed = build_hub_embed(_ECON, CasinoSettings(channel_id=1), None, ticker=[])
    assert all("On the floor" not in (f.name or "") for f in embed.fields)


def test_ticker_line_marks_push_and_partial_return():
    from bot_modules.cogs.casino.embeds import ticker_line

    assert "push" in ticker_line(1, "blackjack", 20, 20)
    assert "10 back" in ticker_line(1, "blackjack", 20, 10)


# ── baccarat result cards ──────────────────────────────────────────────

_BC_PWIN = (["A♠", "8♦"], ["K♠", "Q♦"])           # player 9 over banker 0
_BC_TIE = (["4♠", "3♦"], ["2♠", "5♦"])            # 7 all
_BC_DRAGON7 = (["2♠", "3♦"], ["A♠", "2♦", "4♣"])  # banker 3-card 7


def test_baccarat_result_win_is_green_and_shows_hands_with_totals():
    from bot_modules.cogs.casino.embeds import build_baccarat_result_embed

    embed = build_baccarat_result_embed(
        _ECON, *_BC_PWIN, [(42, "🔵 Player", 10, 20)]
    )
    assert embed.color == discord.Color(COLOR_GREEN)
    assert embed.description is not None
    assert "(9)" in embed.description and "(0)" in embed.description
    assert "Player wins" in embed.description


def test_baccarat_result_all_losses_is_red():
    from bot_modules.cogs.casino.embeds import build_baccarat_result_embed

    embed = build_baccarat_result_embed(
        _ECON, *_BC_PWIN, [(42, "🔴 Banker", 10, 0)]
    )
    assert embed.color == discord.Color(COLOR_RED)


def test_baccarat_result_pushes_alone_are_not_green():
    """A pushed side bet came home — it didn't win. The board says
    'Pushed' and the card stays red (nobody beat the house)."""
    from bot_modules.cogs.casino.embeds import build_baccarat_result_embed

    embed = build_baccarat_result_embed(
        _ECON, *_BC_TIE, [(42, "🔵 Player", 10, 10)]
    )
    assert embed.color == discord.Color(COLOR_RED)
    names = [f.name for f in embed.fields]
    assert "Pushed" in names and "Winners" not in names


def test_baccarat_result_dragon7_names_the_push():
    from bot_modules.cogs.casino.embeds import build_baccarat_result_embed

    embed = build_baccarat_result_embed(
        _ECON, *_BC_DRAGON7, [(42, "🔴 Banker", 10, 10)]
    )
    assert embed.description is not None
    assert "three-card seven" in embed.description
    assert "Banker bets push" in embed.description


def test_baccarat_deal_frame_hides_third_cards():
    from bot_modules.cogs.casino.embeds import build_baccarat_deal_embed

    embed = build_baccarat_deal_embed(
        _ECON, ["2♠", "3♦", "5♣"], ["4♠", "K♦"], None
    )
    assert embed.description is not None
    assert "🂠" in embed.description        # player's third card still down
    assert "5♣" not in embed.description
    assert "(0)" not in embed.description   # no totals until the reveal


# ── dice result cards ──────────────────────────────────────────────────


def test_dice_result_win_is_green_and_reads_the_roll():
    from bot_modules.cogs.casino.embeds import build_dice_result_embed

    embed = build_dice_result_embed(
        _ECON, (6, 5, 4), [(42, "⬆️ Big (11–17)", 10, 20)]
    )
    assert embed.color == discord.Color(COLOR_GREEN)
    assert embed.description is not None
    assert "⚅ ⚄ ⚃" in embed.description
    assert "**15**" in embed.description and "Big" in embed.description


def test_dice_result_all_losses_is_red():
    from bot_modules.cogs.casino.embeds import build_dice_result_embed

    embed = build_dice_result_embed(
        _ECON, (1, 2, 3), [(42, "⬆️ Big (11–17)", 10, 0)]
    )
    assert embed.color == discord.Color(COLOR_RED)


def test_dice_result_triple_names_the_sweep():
    from bot_modules.cogs.casino.embeds import build_dice_result_embed

    embed = build_dice_result_embed(
        _ECON, (4, 4, 4), [(42, "⬆️ Big (11–17)", 10, 0)]
    )
    assert embed.description is not None
    assert "a triple 4!" in embed.description
    assert "sweeps every bet" in embed.description


# ── keno result cards ──────────────────────────────────────────────────


def test_keno_result_win_is_green_and_shows_the_board():
    from bot_modules.cogs.casino.embeds import build_keno_result_embed

    drawn = list(range(1, 21))
    embed = build_keno_result_embed(
        _ECON, drawn, [(42, "Pick-4 · 1 2 3 4", 10, 600)]
    )
    assert embed.color == discord.Color(COLOR_GREEN)
    assert embed.description is not None
    # two monospace rows of ten
    assert embed.description.count("`") == 4
    assert " 1" in embed.description and "20" in embed.description


def test_keno_result_all_losses_is_red():
    from bot_modules.cogs.casino.embeds import build_keno_result_embed

    embed = build_keno_result_embed(
        _ECON, list(range(1, 21)), [(42, "Pick-4 · 61 62 63 64", 10, 0)]
    )
    assert embed.color == discord.Color(COLOR_RED)


def _fields(embed) -> list[tuple[str | None, str | None]]:
    return [(f.name, f.value) for f in embed.fields]


def test_keno_result_itemises_every_losing_ticket():
    """Round 7's shape: three unpaid tickets that previously vanished into
    "The house keeps 90 Coins" with nothing to explain them."""
    from bot_modules.cogs.casino.embeds import build_keno_result_embed

    embed = build_keno_result_embed(
        _ECON, list(range(1, 21)),
        [
            (42, "Pick-4 · 8 16 24 77 · caught 0 · 2 pays", 30, 0),
            (42, "Pick-10 · … · caught 3 · 4 returns your stake", 30, 0),
            (42, "Pick-8 · … · caught 1 · 3 returns your stake", 30, 0),
        ],
    )
    names = [n for n, _ in _fields(embed)]
    assert names == ["No payout", "The house keeps"]  # money line stays last
    unpaid = _fields(embed)[0][1]
    assert unpaid is not None
    assert unpaid.count("<@42>") == 3           # every loser, not a total
    assert "caught 3 · 4 returns your stake" in unpaid
    assert "90" in str(_fields(embed)[1][1])    # total still reconciles


def test_keno_result_keeps_winners_above_the_unpaid_lines():
    from bot_modules.cogs.casino.embeds import build_keno_result_embed

    embed = build_keno_result_embed(
        _ECON, list(range(1, 21)),
        [
            (7, "Pick-4 · **1** **2** **3** **4** · caught 4", 10, 600),
            (42, "Pick-4 · 61 62 63 64 · caught 0 · 2 pays", 10, 0),
        ],
    )
    assert [n for n, _ in _fields(embed)] == [
        "Winners", "No payout", "The house keeps",
    ]


def test_keno_result_has_no_unpaid_field_when_everyone_won():
    from bot_modules.cogs.casino.embeds import build_keno_result_embed

    embed = build_keno_result_embed(
        _ECON, list(range(1, 21)),
        [(7, "Pick-4 · **1** **2** **3** **4** · caught 4", 10, 600)],
    )
    assert [n for n, _ in _fields(embed)] == ["Winners"]


def test_only_keno_annotates_its_bets_with_the_draw():
    """The one wiring assertion: keno's result lines are built from the
    draw, and the four games that share _WindowUI opt out by leaving
    annotate_bet None — their bets board line is the whole story."""
    from bot_modules.cogs.casino import cog

    annotated = {u.key for u in cog._WINDOW_UIS if u.annotate_bet is not None}
    assert annotated == {"keno"}
    line = cog._KENO_UI.annotate_bet(
        {"spots": "[3, 4, 7, 18, 30, 40, 52, 62, 68, 72]", "payout": 0},
        [2, 4, 5, 10, 14, 18, 20, 27, 31, 33,
         35, 36, 38, 47, 50, 57, 63, 68, 70, 71],
    )
    assert line.endswith("· caught 3 · 4 returns your stake")


def test_keno_result_unpaid_field_stays_under_the_field_limit():
    """A busy round must truncate, never 400 the edit and freeze the
    board mid-show."""
    from bot_modules.cogs.casino.embeds import build_keno_result_embed

    embed = build_keno_result_embed(
        _ECON, list(range(1, 21)),
        [
            (10_000_000_000_000_000 + i,
             "Pick-10 · " + " ".join(f"**{n}**" for n in range(1, 11))
             + " · caught 3 · 4 returns your stake", 30, 0)
            for i in range(40)
        ],
    )
    value = _fields(embed)[0][1]
    assert value is not None and len(value) <= 1024


# ── war result cards ───────────────────────────────────────────────────


def _war(player="K♠", dealer="5♦", stake=10, **kw) -> discord.Embed:
    from bot_modules.cogs.casino.embeds import build_war_embed

    return build_war_embed(_ECON, 42, player, dealer, stake, None, **kw)


def test_war_win_is_green_and_loss_is_red():
    assert _war(outcome="win", payout=20).color == discord.Color(COLOR_GREEN)
    assert _war(outcome="lose", payout=0).color == discord.Color(COLOR_RED)
    assert _war(
        outcome="war_win", payout=30, stake=20,
        war_player="9♠", war_dealer="5♦",
    ).color == discord.Color(COLOR_GREEN)
    assert _war(
        outcome="war_lose", payout=0, stake=20,
        war_player="2♠", war_dealer="9♦",
    ).color == discord.Color(COLOR_RED)


def test_war_retreat_is_neutral_not_red():
    embed = _war(player="7♠", dealer="7♦", outcome="retreat", payout=5)
    assert embed.color != discord.Color(COLOR_RED)
    assert embed.color != discord.Color(COLOR_GREEN)


def test_war_standoff_shows_the_decision_not_a_verdict():
    embed = _war(player="7♠", dealer="7♦")
    names = [f.name for f in embed.fields]
    assert "A standoff!" in names and "Result" not in names
    assert embed.color != discord.Color(COLOR_GREEN)
    assert embed.color != discord.Color(COLOR_RED)


def test_war_result_shows_war_cards_when_drawn():
    embed = _war(
        player="7♠", dealer="7♦", outcome="war_win", payout=30, stake=20,
        war_player="9♠", war_dealer="5♦",
    )
    assert embed.description is not None
    assert "9♠" in embed.description and "5♦" in embed.description


# ── the hub panel's "Today at the tables" standings ────────────────────


def _standings_field(embed: discord.Embed) -> str | None:
    field = next(
        (f for f in embed.fields if "Today at the tables" in (f.name or "")),
        None,
    )
    return field.value if field is not None else None


def test_hub_embed_names_the_days_winner_and_loser_with_signed_amounts():
    from bot_modules.cogs.casino.embeds import build_hub_embed

    embed = build_hub_embed(
        _ECON, CasinoSettings(channel_id=1), None,
        standings=((7, 340), (9, -120)),
    )
    value = _standings_field(embed)
    assert value is not None
    assert "<@7>" in value and "**+340**" in value  # winner, signed +
    assert "<@9>" in value and "**−120**" in value   # loser, magnitude with −
    assert value.index("<@7>") < value.index("<@9>")  # up-most listed first


def test_hub_embed_shows_only_the_winner_when_nobody_is_down():
    from bot_modules.cogs.casino.embeds import build_hub_embed

    embed = build_hub_embed(
        _ECON, CasinoSettings(channel_id=1), None,
        standings=((7, 340), None),
    )
    value = _standings_field(embed)
    assert value is not None
    assert "<@7>" in value and "Down most" not in value


def test_hub_embed_omits_standings_when_the_board_is_empty():
    from bot_modules.cogs.casino.embeds import build_hub_embed

    for standings in (None, (None, None)):
        embed = build_hub_embed(
            _ECON, CasinoSettings(channel_id=1), None, standings=standings,
        )
        assert _standings_field(embed) is None
