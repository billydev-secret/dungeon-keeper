"""Casino result-card colors — the win/loss pair must stay the sanctioned
semantic set. A big win is still a win: it gets ``COLOR_GREEN`` like any other,
with the celebration carried by the copy, not by a third color tier.

Also home to the name-resolution contract (todo #90): no casino card may leave
a player as a raw ``<@id>``, since that is resolved by the *reading* client and
degrades to a bare id for anyone who hasn't cached that user.
"""
from __future__ import annotations

import re

import discord
import pytest

from bot_modules.cogs.casino import embeds as casino_embeds
from bot_modules.cogs.casino.embeds import (
    build_derby_race_embed,
    build_derby_result_embed,
    build_derby_round_embed,
    build_help_embed,
    build_pools_panel_embed,
    build_pools_result_embed,
    build_pools_void_embed,
    build_roulette_round_embed,
    build_slots_embed,
    build_slots_spin_embed,
)
from bot_modules.services import casino_logic as logic
from bot_modules.services import pools_logic, pools_metrics
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
    value = _bets_value(
        build_roulette_round_embed(_ECON, 0.0, bets, None, name_fn=_named)
    )
    assert value.index("Player2") < value.index("Player1")


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
        name_fn=_named,
    )
    field = next(f for f in embed.fields if "On the floor" in (f.name or ""))
    assert field.value is not None
    assert field.value.index("Player2") < field.value.index("Player1")
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
        standings=((7, 340), (9, -120)), name_fn=_named,
    )
    value = _standings_field(embed)
    assert value is not None
    assert "Player7" in value and "**+340**" in value  # winner, signed +
    assert "Player9" in value and "**−120**" in value  # loser, magnitude with −
    assert value.index("Player7") < value.index("Player9")  # up-most first


def test_hub_embed_shows_only_the_winner_when_nobody_is_down():
    from bot_modules.cogs.casino.embeds import build_hub_embed

    embed = build_hub_embed(
        _ECON, CasinoSettings(channel_id=1), None,
        standings=((7, 340), None), name_fn=_named,
    )
    value = _standings_field(embed)
    assert value is not None
    assert "Player7" in value and "Down most" not in value


def test_hub_embed_omits_standings_when_the_board_is_empty():
    from bot_modules.cogs.casino.embeds import build_hub_embed

    for standings in (None, (None, None)):
        embed = build_hub_embed(
            _ECON, CasinoSettings(channel_id=1), None, standings=standings,
        )
        assert _standings_field(embed) is None


# ── pools cards name their metric, and state their cap ─────────────────
#
# One wiring assertion each, not a re-test of pools_metrics: the roster
# already pins that every metric HAS a question and a cap note. What is
# only checkable here is that the card actually prints them — a cap that
# exists in the registry but never reaches the panel is a promise members
# cannot see, and the promise is the whole reason the metric is bettable.


def _panel(key: str) -> discord.Embed:
    return build_pools_panel_embed(
        _ECON, 1186.5, pools_logic.PoolSplit(400, 250), 0.0, "2026-08-03",
        None, spec=pools_metrics.SPECS[key],
    )


def test_panel_asks_the_drawn_metrics_question():
    body = _panel("messages").description or ""
    assert "1,186.5" in body
    assert "messages today" in body
    assert "2026-08-03" in body


def test_panel_states_the_cap_where_members_bet():
    fields = " ".join(f.value or "" for f in _panel("messages").fields)
    assert "at most 30 messages per person" in fields


def test_panel_names_the_metric_in_its_title():
    """The market rotates daily; a card that just says "today's market"
    tells a member nothing about what they are betting on."""
    assert "Messages sent" in (_panel("messages").title or "")
    assert "Economy net change" in (_panel("economy_net").title or "")


def test_the_currency_placeholder_is_filled_in_not_printed():
    body = _panel("economy_net").description or ""
    assert "gems" in body
    assert "{currency}" not in body


def _result(key: str, value: int) -> discord.Embed:
    return build_pools_result_embed(
        _ECON, "2026-08-03", value, 1186.5, pools_logic.OVER, [], 19, None,
        spec=pools_metrics.SPECS[key],
    )


def test_count_results_read_unsigned_and_economy_results_keep_their_sign():
    """A count of "+1,204 messages" reads like a delta it is not; a net
    change of "4,275" loses the direction that is its whole point."""
    counted = _result("messages", 1204).description or ""
    assert "**1,204** messages" in counted
    assert "+1,204" not in counted
    assert "**+4,275**" in (_result("economy_net", 4275).description or "")


def test_void_card_distinguishes_the_two_refund_reasons():
    """A member whose round was called off because the bot could no longer
    measure it must not be told they all backed the same side."""
    one_sided = build_pools_void_embed("2026-08-03", 250, None).description or ""
    unmeasurable = build_pools_void_embed(
        "2026-08-03", 250, None, unmeasurable=True
    ).description or ""
    assert "same side" in one_sided
    assert "no longer measure" in unmeasurable


# ── #90: a player is named, never left as a raw Discord reference ──────
#
# A `<@id>` inside an embed is resolved client-side, from the reader's own
# cache — Discord's servers do nothing to it. So on a casino card it renders
# as a bare id for any viewer who hasn't seen that player, which is the
# common case for the hub's ticker (past betters) and for result cards read
# by everyone else in the channel. Every builder that names a player takes a
# `name_fn` and emits plain text; this table is the enforcement, and a new
# builder adds one row rather than its own test.

_MENTION = re.compile(r"<@!?\d+>")
_BETS = [(7, "Red", 10)]
_PAID = [(7, "Red", 10, 20)]


def _seen(embed: discord.Embed) -> str:
    """Every string a reader actually sees on a card."""
    parts = [embed.title or "", embed.description or ""]
    if embed.footer is not None:
        parts.append(embed.footer.text or "")
    if embed.author is not None:
        parts.append(embed.author.name or "")
    for f in embed.fields:
        parts += [f.name or "", f.value or ""]
    return "\n".join(parts)


def _named(uid: int) -> str:
    return f"Player{uid}"


_HUB = (_ECON, CasinoSettings(channel_id=1), None)
_NAME_CASES: list[tuple[str, object]] = [
    ("hub_ticker", lambda n: casino_embeds.build_hub_embed(
        *_HUB, ticker=[(7, "slots", 50, 500)], name_fn=n)),
    ("hub_standings", lambda n: casino_embeds.build_hub_embed(
        *_HUB, standings=((7, 340), (9, -120)), name_fn=n)),
    ("coinflip", lambda n: casino_embeds.build_coinflip_embed(
        _ECON, 7, "heads", "heads", 10, 20, name_fn=n)),
    ("coinflip_spin", lambda n: casino_embeds.build_coinflip_spin_embed(
        _ECON, 7, "heads", 10, None, name_fn=n)),
    ("slots_win", lambda n: casino_embeds.build_slots_embed(
        _ECON, 7, _REELS, 10, 20, "Three of a kind!", name_fn=n)),
    ("slots_loss", lambda n: casino_embeds.build_slots_embed(
        _ECON, 7, _REELS, 10, 0, None, name_fn=n)),
    ("slots_spin", lambda n: casino_embeds.build_slots_spin_embed(
        _ECON, 7, 10, ("🍯", None, None), None, name_fn=n)),
    ("jackpot", lambda n: casino_embeds.build_jackpot_celebration(
        _ECON, 7, 5000, name_fn=n)),
    ("blackjack_reveal", lambda n: casino_embeds.build_blackjack_reveal_embed(
        _ECON, 7, ["9♠", "5♦"], ["K♥"], 10, None, name_fn=n)),
    ("blackjack", lambda n: casino_embeds.build_blackjack_embed(
        _ECON, 7, ["9♠", "5♦"], ["K♥", "7♣"], 10, None,
        outcome="win", payout=20, name_fn=n)),
    ("war", lambda n: casino_embeds.build_war_embed(
        _ECON, 7, "K♥", "7♣", 10, None, outcome="win", payout=20, name_fn=n)),
    ("roulette_round", lambda n: casino_embeds.build_roulette_round_embed(
        _ECON, 1.0, _BETS, None, name_fn=n)),
    ("roulette_result", lambda n: casino_embeds.build_roulette_result_embed(
        _ECON, 7, _PAID, name_fn=n)),
    ("derby_round", lambda n: casino_embeds.build_derby_round_embed(
        _ECON, 1.0, _BETS, None, name_fn=n)),
    ("derby_result", lambda n: casino_embeds.build_derby_result_embed(
        _ECON, 0, [12, 9, 8, 7, 6, 5], _PAID, name_fn=n)),
    ("baccarat_round", lambda n: casino_embeds.build_baccarat_round_embed(
        _ECON, 1.0, _BETS, None, name_fn=n)),
    ("baccarat_result", lambda n: casino_embeds.build_baccarat_result_embed(
        _ECON, ["9♠", "5♦"], ["K♥", "7♣"], _PAID, name_fn=n)),
    ("dice_round", lambda n: casino_embeds.build_dice_round_embed(
        _ECON, 1.0, _BETS, None, name_fn=n)),
    ("dice_result", lambda n: casino_embeds.build_dice_result_embed(
        _ECON, (3, 4, 5), _PAID, name_fn=n)),
    ("keno_round", lambda n: casino_embeds.build_keno_round_embed(
        _ECON, 1.0, _BETS, None, name_fn=n)),
    ("keno_result", lambda n: casino_embeds.build_keno_result_embed(
        _ECON, list(range(1, 21)), _PAID, name_fn=n)),
    ("pools_result", lambda n: casino_embeds.build_pools_result_embed(
        _ECON, "2026-08-03", 1204, 1186.5, pools_logic.OVER,
        [(7, 10, 20)], 19, None, spec=pools_metrics.SPECS["messages"],
        chart=False, name_fn=n)),
]


@pytest.mark.parametrize(
    ("label", "build"), _NAME_CASES, ids=[c[0] for c in _NAME_CASES]
)
def test_no_casino_card_leaves_a_raw_discord_reference(label, build):
    text = _seen(build(_named))
    assert not _MENTION.search(text), f"{label} left a raw <@id> reference"
    assert "Player7" in text, f"{label} never rendered the resolved name"


def test_ticker_line_names_the_player():
    assert "Player7" in casino_embeds.ticker_line(
        7, "slots", 50, 500, name_fn=_named
    )
    assert not _MENTION.search(
        casino_embeds.ticker_line(7, "slots", 50, 500, name_fn=_named)
    )


def test_every_casino_render_site_passes_a_resolver():
    """The other half of the contract above.

    ``name_fn`` defaults to ``mention`` so an un-wired caller keeps its old
    output rather than crashing — which means a render site that forgets to
    pass one silently reintroduces todo #90 and no builder test would notice.
    This walks the two modules that actually render and requires every call
    to a name-taking builder to hand one over. ``round_embed``/``build_show``
    are the ``_WindowUI`` indirections onto those same builders.
    """
    import ast
    import inspect
    import pathlib

    from bot_modules.cogs.casino import cog as casino_cog
    from bot_modules.cogs.casino import pools_panel

    needs = {"round_embed", "build_show"} | {
        name
        for name, fn in inspect.getmembers(casino_embeds, inspect.isfunction)
        if "name_fn" in inspect.signature(fn).parameters
    }
    missed = []
    for module in (casino_cog, pools_panel):
        # Explicit utf-8: these sources are full of em-dashes and the CI
        # runner is Windows, where the default encoding is cp1252.
        source = pathlib.Path(inspect.getfile(module)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = (
                func.attr if isinstance(func, ast.Attribute)
                else getattr(func, "id", None)
            )
            if called in needs and not any(
                kw.arg == "name_fn" for kw in node.keywords
            ):
                missed.append(
                    f"{module.__name__.rsplit('.', 1)[-1]}.py:"
                    f"{node.lineno} {called}()"
                )
    assert not missed, "render sites with no name_fn: " + ", ".join(missed)


# ── big-win broadcast (the public recap, separate from the player's card) ─


def _result_card() -> discord.Embed:
    """Stand-in for whatever the player already holds, with the two things
    the broadcast must carry over: the copy and a result field."""
    embed = discord.Embed(
        title="🎡 Roulette — no more bets!",
        description="The ball lands on 🔴 **7**.",
        color=COLOR_GREEN,
    )
    embed.add_field(name="Winners", value="Nelli — 💎 1,200 gems", inline=False)
    embed.set_footer(text="Play for fun, not for rent.")
    return embed


def _broadcast(payout: int, threshold: int = 500, stake: int = 10, **kw):
    return casino_embeds.build_big_win_broadcast(
        _result_card(), payout=payout, threshold=threshold, stake=stake,
        game_label="Roulette", **kw,
    )


@pytest.mark.parametrize(
    ("payout", "threshold"),
    [
        pytest.param(499, 500, id="under-the-bar"),
        pytest.param(50_000, 0, id="bar-switched-off"),
    ],
)
def test_no_broadcast_is_built_below_the_bar(payout, threshold):
    """None is the whole decision — the cog has no second threshold check, so
    a builder that returned an embed here would post one."""
    assert _broadcast(payout, threshold) is None


def test_broadcast_leads_with_the_tier_header_not_the_game_card_title():
    """The point of the change: in-channel this has to read as an event, not
    as a replay of the receipt the player already got."""
    built = _broadcast(1200)
    assert built is not None
    assert built.embed.title == "💰 Big Win — Roulette"
    assert built.embed.title != _result_card().title
    assert not built.ping


def test_broadcast_escalates_its_header_with_the_payout():
    assert _broadcast(1499).embed.title.startswith("💰 Big Win")
    assert _broadcast(1500).embed.title.startswith("🔥 Huge Win")


def test_broadcast_carries_over_the_result_copy_and_fields():
    """A new embed, but not a poorer one — the reason a bystander cares (what
    landed, who won what) has to survive the retitle."""
    built = _broadcast(1200)
    assert built is not None
    assert built.embed.description == "The ball lands on 🔴 **7**."
    assert [(f.name, f.value) for f in built.embed.fields] == [
        ("Winners", "Nelli — 💎 1,200 gems")
    ]
    assert built.embed.color is not None
    assert built.embed.color.value == COLOR_GREEN


def test_broadcast_never_mutates_the_players_own_card():
    """These were one object before this change. If the builder retitled in
    place, the card already on the player's screen would be rewritten — and
    which text they ended up with would depend on send ordering."""
    card = _result_card()
    before = (card.title, card.description, len(card.fields), card.color)
    built = casino_embeds.build_big_win_broadcast(
        card, payout=9_999, threshold=500, stake=10, game_label="Roulette",
        top_pct_payout=2500,
    )
    assert built is not None
    assert built.embed is not card
    assert (card.title, card.description, len(card.fields), card.color) == before


def test_top_three_percent_pings_and_says_why():
    """The loudest rung: @here plus a lead line above the result copy, so the
    ping is explained by the card rather than just louder than the rest."""
    built = _broadcast(3000, top_pct_payout=2500)
    assert built is not None
    assert built.ping
    assert built.embed.title == f"{logic.LEGENDARY_HEADER} — Roulette"
    assert built.embed.description is not None
    assert built.embed.description.startswith(logic.LEGENDARY_LEAD)
    # The result copy still follows the lead, not replaced by it.
    assert "The ball lands on 🔴 **7**." in built.embed.description


def test_a_push_over_the_bar_builds_nothing():
    """The card being copied would be a push card — neither a win nor green.
    This gate is what keeps the accent-contract exemption honest: every card
    this builder ever copies is a winning one."""
    assert _broadcast(2000, stake=2000) is None
    assert _broadcast(2000, stake=4000) is None  # a war retreat


def test_broadcast_names_the_winner_in_the_author_slot():
    """Style guide: title = the event, author = the person it's about."""
    built = _broadcast(1200, winner_name="Nelli", winner_icon="http://x/a.png")
    assert built is not None
    assert built.embed.author.name == "Nelli"
    assert built.embed.author.icon_url == "http://x/a.png"
    # Optional — the windowed games resolve a Member that can be uncached.
    assert _broadcast(1200).embed.author.name is None
