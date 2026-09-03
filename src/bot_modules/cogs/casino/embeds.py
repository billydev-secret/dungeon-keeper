"""Pure embed builders for the casino.

Style-guide rules in force: accent color for neutral states, semantic
green/red only for genuine win/loss — a big win is green like any other win,
never a third tier — with COLOR_GOLD reserved for the casino's own panels,
currency always rendered through the guild's own emoji/name, section
spacing via the trailing zero-width space, no custom emoji in footers.
Builders are pure — color and settings arrive as parameters.

Flavor copy stays name-agnostic: every guild can rename its casino from
the Branding panel (``casino_name``/``DEFAULT_CASINO_NAME``), so nothing
here hardcodes "meadow"/"honeypot"/other Golden-Meadow-specific imagery —
generic gambling terms ("the house", "the jackpot", "the pot") read right
under any name.

Players are rendered through an injected ``name_fn`` (``services.name_resolver``)
rather than as ``<@id>``. An embed mention is resolved by the *reading* client
from its own cache — Discord's servers do nothing to it — so it degrades to a
bare numeric id for anyone who hasn't seen that player: routine for the hub's
ticker of past betters, and for a result card read by the rest of the channel.
The default is ``mention``, so a caller that hasn't been wired yet keeps its old
output; ``tests/test_casino_embeds.py`` holds the contract that none of them do.
"""

from __future__ import annotations

import sqlite3

from typing import NamedTuple

import discord

from bot_modules.core.meters import mono
from bot_modules.economy.leaderboard import bar_fill
from bot_modules.services import casino_logic as logic
from bot_modules.services import pools_charts, pools_logic
from bot_modules.services.casino_service import CasinoSettings, MinesStep
from bot_modules.services.economy_service import EconSettings
from bot_modules.services.branding_service import DEFAULT_CASINO_NAME
from bot_modules.services.embeds import COLOR_GOLD, COLOR_GREEN, COLOR_RED
from bot_modules.services.name_resolver import NameFn, mention
from bot_modules.core.branding import apply_section_spacing


def casino_title(casino_name: str = DEFAULT_CASINO_NAME) -> str:
    """Hub-panel title for a guild's casino ("🎲 The Golden Meadow Casino")."""
    return f"🎲 The {casino_name} Casino"


# Kept for callers with no guild handy; the per-guild name is the norm.
CASINO_TITLE = casino_title()

_GAME_LINES = {
    "coinflip": (
        "🪙 **Coinflip** — call it in the air; a win pays "
        f"{logic.mult_text(logic.COINFLIP_MULT_NUM, logic.COINFLIP_MULT_DEN)}× "
        "your bet"
    ),
    "slots": "🎰 **Slots** — three spinning reels; pairs pay back, sevens pay big",
    "blackjack": "🃏 **Blackjack** — beat the dealer to 21; naturals pay 3:2",
    "roulette": "🎡 **Roulette** — one wheel, one window, everyone bets together",
    "derby": "🏇 **Derby** — six critters, one finish line; back your favorite",
    "baccarat": "🎴 **Baccarat** — Player, Banker, or Tie; nearest to nine wins",
    "dice": "🎲 **Dice** — three dice, one roll; call Big, Small, Odd, or Even",
    "war": "⚔️ **War** — one card each, high card wins; on a tie, go to war",
    "keno": "🔢 **Keno** — grab a ticket, 20 numbers drop, catches pay big",
    "mines": (
        "💣 **Mines** — open tiles, climb the multiplier, stop before the bang"
    ),
}


def _coins(econ: EconSettings, n: int) -> str:
    unit = econ.currency_name if n == 1 else econ.currency_plural
    return f"{econ.currency_emoji} **{n:,}** {unit}"


def _accent(accent: discord.Color | None) -> discord.Color | int:
    return accent if accent is not None else COLOR_GOLD


def _streak_line(econ: EconSettings, streak: int) -> str | None:
    """The 🔥/🧊 callout once a run reaches the threshold, else None."""
    if streak >= logic.STREAK_CALLOUT_AT:
        return f"🔥 **{streak} wins in a row!**"
    if streak <= -logic.STREAK_CALLOUT_AT:
        return (
            f"🧊 {abs(streak)} losses in a row — the house is merciless."
        )
    return None


def _with_streak(desc: str, econ: EconSettings, streak: int) -> str:
    line = _streak_line(econ, streak)
    return f"{desc}\n{line}" if line else desc


def _pot_line(pot_after: int) -> str:
    """Every loss is a tiny ad for the jackpot."""
    return f"💰 The loss feeds the jackpot — now **{pot_after:,}**."


def _add_result_fields(
    embed: discord.Embed,
    econ: EconSettings,
    paid: list[tuple[int, str, int, int]],
    losers_total: int,
    pot_after: int,
    *,
    paid_name: str = "Winners",
    keep_name: str = "The house keeps",
    name_fn: NameFn = mention,
) -> None:
    """Every result card's shared tail: the paid board (💥 big-win prefix,
    char-capped under the 1024 field limit) and the house-keeps line with
    its pot ad. One implementation so a field-limit fix can never land in
    one game's recap and miss another's."""
    if paid:
        lines = [
            f"{'💥 ' if logic.is_big_win(amount, payout) else ''}"
            f"{name_fn(uid)} — {d} · {_coins(econ, amount)} → {_coins(econ, payout)}"
            for uid, d, amount, payout in paid
        ]
        embed.add_field(
            name=paid_name,
            value="\n".join(logic.cap_lines(lines, limit=1022)) + "\n​",
            inline=False,
        )
    if losers_total:
        kept = _coins(econ, losers_total)
        if pot_after > 0:
            kept += f"\n{_pot_line(pot_after)}"
        embed.add_field(name=keep_name, value=kept, inline=False)


def _running_note(
    lead: str, closes_at: float, url: str | None, jump: str, above: str
) -> str:
    """The already-running ephemeral pointer every windowed game shares."""
    note = f"{lead} <t:{int(closes_at)}:R>."
    if url:
        return f"{note} {jump}: {url}"
    return f"{note} {above}."


_TICKER_EMOJI = {"coinflip": "🪙", "slots": "🎰", "blackjack": "🃏", "war": "⚔️"}


def ticker_line(
    user_id: int, game: str, stake: int, payout: int, *, name_fn: NameFn = mention
) -> str:
    """One compact floor-ticker entry, naming the player as plain text."""
    if payout > stake:
        result = f"**{payout:,}**"
    elif payout == stake and payout > 0:
        result = "push"
    elif payout > 0:
        result = f"{payout:,} back"
    else:
        result = "the house"
    return f"{_TICKER_EMOJI.get(game, '🎲')} {name_fn(user_id)} · {stake:,} → {result}"


def build_hub_embed(
    econ: EconSettings,
    settings: CasinoSettings,
    accent: discord.Color | None,
    *,
    jackpot: int | None = None,
    ticker: list[tuple[int, str, int, int]] | None = None,
    standings: tuple[tuple[int, int] | None, tuple[int, int] | None]
    | None = None,
    casino_name: str = DEFAULT_CASINO_NAME,
    name_fn: NameFn = mention,
) -> discord.Embed:
    open_lines = [
        line
        for game, line in _GAME_LINES.items()
        if getattr(settings, f"{game}_enabled")
    ]
    embed = discord.Embed(
        title=casino_title(casino_name),
        description=(
            "Bright lights, long odds, and questionable financial decisions. "
            "Pick a table — every bet comes straight from your wallet.\n​"
        ),
        color=_accent(accent),
    )
    embed.add_field(
        name="Tables",
        value=("\n".join(open_lines) or "*Every table is closed right now.*")
        + "\n​",
        inline=False,
    )
    if jackpot is not None:
        embed.add_field(
            name="💰 Progressive Jackpot",
            value=(
                f"Currently {_coins(econ, jackpot)} — every lost bet feeds "
                "it, and triple 7️⃣ on the slots takes it ALL.\n​"
            ),
            inline=False,
        )
    if ticker:
        embed.add_field(
            name="📡 On the Floor",
            value="\n".join(
                ticker_line(uid, game, stake, payout, name_fn=name_fn)
                for uid, game, stake, payout in ticker
            ) + "\n​",
            inline=False,
        )
    if standings is not None:
        earner, loser = standings
        rows = []
        if earner is not None:
            rows.append(
                f"📈 Up most: {name_fn(earner[0])} · "
                f"**+{earner[1]:,}** {econ.currency_plural}"
            )
        if loser is not None:
            rows.append(
                f"📉 Down most: {name_fn(loser[0])} · "
                f"**−{abs(loser[1]):,}** {econ.currency_plural}"
            )
        if rows:
            embed.add_field(
                name="📊 Today at the Tables",
                value="\n".join(rows) + "\n​",
                inline=False,
            )
    limits = [f"Bets: **{settings.min_bet:,}**–**{settings.max_bet:,}**"
              if settings.max_bet else f"Bets: **{settings.min_bet:,}**+"]
    if settings.daily_wager_cap:
        limits.append(
            f"Daily table limit: **{settings.daily_wager_cap:,}** "
            f"{econ.currency_plural} staked per player"
        )
    embed.add_field(name="House Rules", value=" · ".join(limits), inline=False)
    apply_section_spacing(embed)
    return embed


def build_help_embed(
    econ: EconSettings,
    settings: CasinoSettings,
    accent: discord.Color | None,
    *,
    casino_name: str = DEFAULT_CASINO_NAME,
) -> discord.Embed:
    """One field per OPEN table — a closed table's odds are not on offer."""
    embed = discord.Embed(
        title=f"How the {casino_name} pays",
        description=(
            "Payouts below are **total return** on your bet — a 2× win on a "
            "10-bet hands back 20. The house keeps a small edge on every "
            "table; that's what makes it a casino.\n​"
        ),
        color=_accent(accent),
    )
    if settings.coinflip_enabled:
        embed.add_field(
            name="🪙 Coinflip",
            value=(
                "Call heads or tails. Win: "
                f"**{logic.mult_text(logic.COINFLIP_MULT_NUM, logic.COINFLIP_MULT_DEN)}×** "
                f"({logic.COINFLIP_RTP_PCT:g}% return).\n​"
            ),
            inline=False,
        )
    if settings.slots_enabled:
        triples = " · ".join(
            f"{sym}{sym}{sym} **{mult}×**"
            for sym, mult in logic.SLOT_TRIPLE_PAYOUT.items()
        )
        embed.add_field(
            name="🎰 Slots",
            value=(
                f"{triples}\n"
                f"Two 7️⃣ **{logic.SLOT_TWO_SEVENS_MULT}×** · any pair "
                f"**{logic.mult_text(logic.SLOT_PAIR_NUM, logic.SLOT_PAIR_DEN)}×** "
                f"(~{logic.SLOTS_RTP_PCT:g}% return)\n​"
            ),
            inline=False,
        )
    if settings.blackjack_enabled:
        embed.add_field(
            name="🃏 Blackjack",
            value=(
                "Dealer stands on all 17s. Blackjack pays **3:2**, wins pay "
                "**2×**, pushes return your bet. Double down on your first two "
                "cards. Idle hands stand automatically.\n​"
            ),
            inline=False,
        )
    if settings.roulette_enabled:
        embed.add_field(
            name="🎡 Roulette",
            value=(
                "European wheel, one zero. Red/black **2×** · dozens **3×** · "
                "straight numbers **36×** (~97% return). Your own private wheel: "
                "stack as many bets as you like, then press **Spin**.\n​"
            ),
            inline=False,
        )
    if settings.derby_enabled:
        odds = " · ".join(
            f"{r.emoji} **{logic.derby_odds_label(i)}**"
            for i, r in enumerate(logic.DERBY_FIELD)
        )
        embed.add_field(
            name="🏇 Derby",
            value=(
                f"{odds}\n"
                "Back a critter to win — the favorite pays least, the snail "
                "pays big (91–96% return by runner). Back as many runners as you "
                "like in your own private race, then press **Race**.\n​"
            ),
            inline=False,
        )
    if settings.baccarat_enabled:
        embed.add_field(
            name="🎴 Baccarat",
            value=(
                "Back the **Player** or **Banker** — both pay **2×** "
                "(~99% return, the best odds in the house; a Banker win on a "
                "three-card 7 pushes instead). **Tie** pays **9×** — the long "
                "shot (~86% return). Ties push the side bets. Your own private "
                "coup — take your positions, then press **Deal**.\n​"
            ),
            inline=False,
        )
    if settings.dice_enabled:
        embed.add_field(
            name="🎲 Dice",
            value=(
                "Three dice, one roll. **Big** (11–17), **Small** "
                "(4–10), **Odd**, **Even** — all pay **2×**, and any triple "
                "sweeps the table (~97% return). Your own private roll — make "
                "your calls, then press **Roll**.\n​"
            ),
            inline=False,
        )
    if settings.war_enabled:
        embed.add_field(
            name="⚔️ War",
            value=(
                "One card each, aces high — the higher card pays **2×** on "
                "the spot. A tie is a standoff: **go to war** (match your bet; "
                "win *or tie* the next card for **3×** your original) or "
                "**retreat** (half your bet back). ~97% return going to war — "
                "always the braver *and* the better play.\n​"
            ),
            inline=False,
        )
    if settings.keno_enabled:
        pays = " · ".join(
            f"Pick-{tier} up to **{max(table.values())}×**"
            for tier, table in logic.KENO_PAYTABLE.items()
        )
        embed.add_field(
            name="🔢 Keno",
            value=(
                "Grab a quick-pick ticket of 4, 6, 8, or 10 numbers; 20 of 80 "
                f"drop in your own private draw. {pays} — pays scale with how "
                "many of yours hit (~95% return, tuned far kinder than real "
                "keno). Buy your tickets, then press **Draw**.\n​"
            ),
            inline=False,
        )
    if settings.mines_enabled:
        ladders = "\n".join(
            f"· **{bombs} bomb{'' if bombs == 1 else 's'}** — "
            f"{logic.mines_top_rung(bombs)} tiles to "
            f"{logic.mines_mult_label(logic.mines_ladder(bombs)[-1])}"
            for bombs in logic.MINES_BOMB_CHOICES
        )
        embed.add_field(
            name="💣 Mines",
            value=(
                f"{logic.MINES_TILES} tiles, and you pick the danger:\n"
                f"{ladders}\n"
                "Every safe tile lifts your multiplier and **Cash Out** banks "
                "it — the button shows exactly what you would walk away with. "
                "Every ladder tops out around the same place, so the choice is "
                "how long the road is. Every stopping point returns the same "
                f"~{logic.MINES_RTP_PCT:.0f}%, so there is no wrong moment to "
                "stop, and if you wander off the house cashes you out where "
                "you stood.\n​"
            ),
            inline=False,
        )
    if settings.daily_wager_cap:
        embed.add_field(
            name="Daily Limit",
            value=(
                f"You can stake up to **{settings.daily_wager_cap:,}** "
                f"{econ.currency_plural} per day across all tables."
            ),
            inline=False,
        )
    apply_section_spacing(embed)
    return embed


def build_coinflip_embed(
    econ: EconSettings,
    user_id: int,
    call: str,
    landed: str,
    stake: int,
    payout: int,
    *,
    streak: int = 0,
    pot_after: int = 0,
    name_fn: NameFn = mention,
) -> discord.Embed:
    won = payout > 0
    face = "🌞" if landed == "heads" else "🌙"
    desc = (
        f"{name_fn(user_id)} called **{call}** for {_coins(econ, stake)}.\n"
        + (
            f"The coin agrees — they collect {_coins(econ, payout)}."
            if won
            else "The coin does not care. The house keeps the bet."
        )
    )
    if not won and pot_after > 0:
        desc += f"\n{_pot_line(pot_after)}"
    embed = discord.Embed(
        title=f"🪙 Coinflip — {landed}! {face}",
        description=_with_streak(desc, econ, streak),
        color=COLOR_GREEN if won else COLOR_RED,
    )
    return embed


def _reel_row(cells: tuple[str, ...]) -> str:
    """The slots reel row, deliberately unframed.

    A text-art cabinet used to box this row and was removed 2026-08-16.
    It could not be made to fit. The reel symbols are emoji whose display
    width varies by client, and ▶/◀ render in emoji presentation on
    Discord, so the row's rendered width isn't knowable at build time. The
    frame's own two lines had drifted apart as well — 16 display cells on
    top against 17 on the bottom — which is what finally showed up as a
    visibly crooked box.

    A code span would align all three lines, but Discord renders emoji
    inside code spans as monochrome glyphs, which costs the coloured reels
    that are the whole point. Better no box than one that never fit.
    """
    return f"▶ {cells[0]} │ {cells[1]} │ {cells[2]} ◀"


def build_slots_embed(
    econ: EconSettings,
    user_id: int,
    reels: tuple[str, str, str],
    stake: int,
    payout: int,
    label: str | None,
    *,
    jackpot_won: int = 0,
    streak: int = 0,
    pot_after: int = 0,
    casino_name: str = DEFAULT_CASINO_NAME,
    name_fn: NameFn = mention,
) -> discord.Embed:
    reel_line = _reel_row(reels)
    title = f"🎰 {casino_name} Slots"
    if payout > 0:
        desc = (
            f"{reel_line}\n\n{label} {name_fn(user_id)} bet {_coins(econ, stake)} "
            f"and collects {_coins(econ, payout)}."
        )
        if jackpot_won:
            title = "💥 🎰 The Jackpot Spills"
            desc += "\nThe whole progressive pot — gone in one spin."
        # A big win is still a win: the celebration lives in the copy, not in a
        # third color tier (style guide: green = win, red = loss, full stop).
        color = COLOR_GREEN
    else:
        desc = (
            f"{reel_line}\n\n{name_fn(user_id)}'s {_coins(econ, stake)} scatters "
            "into the house's take."
        )
        if pot_after > 0:
            desc += f"\n{_pot_line(pot_after)}"
        color = COLOR_RED
    embed = discord.Embed(
        title=title, description=_with_streak(desc, econ, streak), color=color
    )
    return embed


def build_jackpot_celebration(
    econ: EconSettings, user_id: int, amount: int,
    *, casino_name: str = DEFAULT_CASINO_NAME, name_fn: NameFn = mention,
) -> discord.Embed:
    """The standalone fanfare posted beside a jackpot result."""
    embed = discord.Embed(
        title=f"🏆 Jackpot at the {casino_name} 🏆",
        description=(
            f"💰 7️⃣ 7️⃣ 7️⃣ 💰\n\n{name_fn(user_id)} just hit the progressive "
            f"jackpot for {_coins(econ, amount)}!\n"
            "The pot reseeds — every lost bet grows the next one."
        ),
        color=COLOR_GOLD,
    )
    return embed


class BigWinBroadcast(NamedTuple):
    embed: discord.Embed
    ping: bool  # send with @here, allowed_mentions=everyone


def build_big_win_broadcast(
    result: discord.Embed,
    *,
    payout: int,
    threshold: int,
    stake: int,
    game_label: str,
    top_pct_payout: int | None = None,
    ping_enabled: bool = True,
    winner_name: str | None = None,
    winner_icon: str | None = None,
) -> BigWinBroadcast | None:
    """The public big-win card, or None when this win stays private.

    A SEPARATE embed from the one the player already holds — the result card
    is titled for the game ("🎡 Roulette — no more bets!") and reads as a
    receipt; in the channel it needs to read as an event. Copying rather than
    retitling in place matters because the player's message and this one are
    otherwise the same object: mutating it would rewrite the card already on
    their screen, and the outcome would depend on which send ran first.

    The color rides along from ``result``. ``big_win_tier`` refuses anything
    that is not a win (``payout > stake``), so the card being copied is always
    a winning one and its color is always the semantic green — never the guild
    accent. That is the premise this builder's accent-contract exemption rests
    on, so the stake gate is load-bearing for more than the copy.
    """
    tier = logic.big_win_tier(
        payout, threshold, stake=stake, top_pct_payout=top_pct_payout,
        ping_enabled=ping_enabled,
    )
    if tier is None:
        return None
    body = result.description or ""
    if tier.lead:
        body = f"{tier.lead}\n​\n{body}" if body else tier.lead
    embed = discord.Embed(
        title=f"{tier.header} — {game_label}",
        description=body or None,
        color=result.color,
    )
    if winner_name:
        embed.set_author(name=winner_name, icon_url=winner_icon or None)
    for field in result.fields:
        embed.add_field(
            name=field.name or "​",
            value=field.value or "​",
            inline=bool(field.inline),
        )
    return BigWinBroadcast(embed, tier.ping)


# ── animation frames (big bets get the show; money is already settled) ─


def build_coinflip_spin_embed(
    econ: EconSettings, user_id: int, call: str, stake: int,
    accent: discord.Color | None, *, name_fn: NameFn = mention,
) -> discord.Embed:
    embed = discord.Embed(
        title="🪙 Coinflip — It's in the Air!",
        description=(
            f"{name_fn(user_id)} calls **{call}** for {_coins(econ, stake)}…\n"
            "The coin spins high in the air. 🪙"
        ),
        color=_accent(accent),
    )
    return embed


def build_slots_spin_embed(
    econ: EconSettings,
    user_id: int,
    stake: int,
    revealed: tuple[str | None, str | None, str | None],
    accent: discord.Color | None,
    *,
    casino_name: str = DEFAULT_CASINO_NAME,
    name_fn: NameFn = mention,
) -> discord.Embed:
    cells = tuple(sym if sym is not None else "🌀" for sym in revealed)
    embed = discord.Embed(
        title=f"🎰 {casino_name} Slots",
        description=(
            f"{_reel_row(cells)}\n\n{name_fn(user_id)} bet "
            f"{_coins(econ, stake)} — the reels are spinning…"
        ),
        color=_accent(accent),
    )
    return embed


def build_blackjack_reveal_embed(
    econ: EconSettings,
    user_id: int,
    player: list[str],
    dealer_first_two: list[str],
    stake: int,
    accent: discord.Color | None,
    *,
    doubled: bool = False,
    name_fn: NameFn = mention,
) -> discord.Embed:
    stake_note = f" (doubled to {stake:,})" if doubled else ""
    embed = discord.Embed(
        title="🃏 Blackjack",
        description=(
            f"{name_fn(user_id)} is in for {_coins(econ, stake)}{stake_note}\n​"
        ),
        color=_accent(accent),
    )
    embed.add_field(
        name="Their Hand", value=_hand_line(player) + "\n​", inline=False
    )
    embed.add_field(
        name="Dealer",
        value=_hand_line(dealer_first_two) + "\n*The dealer turns the hole card…*",
        inline=False,
    )
    apply_section_spacing(embed)
    return embed


def build_roulette_bounce_embed(
    econ: EconSettings, bounce: tuple[int, int], accent: discord.Color | None
) -> discord.Embed:
    frames = " … ".join(
        f"{_COLOR_DOTS[logic.wheel_color(n)]} {n}" for n in bounce
    )
    embed = discord.Embed(
        title="🎡 Roulette — No More Bets!",
        description=f"The ball dances across the wheel… {frames} …",
        color=_accent(accent),
    )
    return embed


_OUTCOME_LINES = {
    "blackjack": "**Blackjack!** Paid 3:2 —",
    "win": "**They beat the dealer** —",
    "push": "**Push.** The bet comes home —",
    "lose": "The dealer takes it.",
    "bust": "**Bust.** The dealer takes it.",
    "refunded": "The table was reset — the bet came home.",
}


def _hand_line(cards: list[str], *, hide_hole: bool = False) -> str:
    if hide_hole and len(cards) >= 2:
        shown = [cards[0]] + ["🂠"] * (len(cards) - 1)
        return f"`{'  '.join(shown)}`"
    return f"`{'  '.join(cards)}`  ({logic.hand_value(cards)})"


def build_blackjack_embed(
    econ: EconSettings,
    user_id: int,
    player: list[str],
    dealer: list[str],
    stake: int,
    accent: discord.Color | None,
    *,
    doubled: bool = False,
    outcome: str | None = None,
    payout: int = 0,
    streak: int = 0,
    pot_after: int = 0,
    name_fn: NameFn = mention,
) -> discord.Embed:
    live = outcome is None
    if live:
        color: discord.Color | int = _accent(accent)
    elif outcome in ("blackjack", "win"):
        color = COLOR_GREEN
    elif outcome in ("push", "refunded"):
        color = _accent(accent)
    else:
        color = COLOR_RED
    stake_note = f" (doubled to {stake:,})" if doubled else ""
    embed = discord.Embed(
        title="🃏 Blackjack",
        description=(
            f"{name_fn(user_id)} is in for {_coins(econ, stake)}{stake_note}\n​"
        ),
        color=color,
    )
    embed.add_field(
        name="Their Hand", value=_hand_line(player) + "\n​", inline=False
    )
    embed.add_field(
        name="Dealer", value=_hand_line(dealer, hide_hole=live), inline=False
    )
    if not live:
        line = _OUTCOME_LINES.get(outcome or "", "")
        if payout > 0:
            line = f"{line} {_coins(econ, payout)}."
        if payout == 0 and pot_after > 0:
            line = f"{line}\n{_pot_line(pot_after)}"
        if outcome != "refunded":
            line = _with_streak(line, econ, streak)
        embed.add_field(name="Result", value=line, inline=False)
    apply_section_spacing(embed)
    return embed


def _add_bets_field(
    embed: discord.Embed,
    econ: EconSettings,
    bets: list[tuple[int, str, int]],
    *,
    name_fn: NameFn = mention,
) -> None:
    """The live bets board, newest first, char-capped under the 1024 field
    limit — a long currency name or big stakes must never 400 the repaint
    and silently freeze the board (the result embed's cap_lines rule)."""
    if not bets:
        embed.add_field(name="Bets", value="*No bets yet — be first.*", inline=False)
        return
    lines = [
        f"{name_fn(uid)} — {desc} · {_coins(econ, amount)}"
        for uid, desc, amount in reversed(bets)
    ]
    embed.add_field(
        name=f"Bets ({len(bets)})",
        value="\n".join(
            logic.cap_lines(lines, limit=1022, more_label="earlier bet(s)")
        ),
        inline=False,
    )


def build_roulette_round_embed(
    econ: EconSettings,
    closes_at: float,
    bets: list[tuple[int, str, int]],
    accent: discord.Color | None,
    *,
    name_fn: NameFn = mention,
) -> discord.Embed:
    """``bets`` = (user_id, bet description, amount), placement order."""
    embed = discord.Embed(
        title="🎡 Roulette — Bets Open!",
        description=(
            f"The wheel spins <t:{int(closes_at)}:R>. "
            "Pick a color, a dozen, or go all-in on a single number.\n​"
        ),
        color=_accent(accent),
    )
    _add_bets_field(embed, econ, bets, name_fn=name_fn)
    return embed


_COLOR_DOTS = {"red": "🔴", "black": "⚫", "green": "🟢"}


def build_roulette_result_embed(
    econ: EconSettings,
    result: int,
    bets: list[tuple[int, str, int, int]],
    *,
    pot_after: int = 0,
    name_fn: NameFn = mention,
) -> discord.Embed:
    """``bets`` = (user_id, bet description, amount, payout)."""
    color_name = logic.wheel_color(result)
    dot = _COLOR_DOTS[color_name]
    winners = [b for b in bets if b[3] > 0]
    losers_total = sum(b[2] for b in bets if b[3] == 0)
    if bets:
        description = f"The ball lands on {dot} **{result}**.\n​"
    else:
        description = (
            f"The ball lands on {dot} **{result}** — but nobody bet. "
            "The wheel spins for no one."
        )
    embed = discord.Embed(
        title="🎡 Roulette — No More Bets!",
        description=description,
        color=COLOR_GREEN if winners else COLOR_RED,
    )
    _add_result_fields(
        embed, econ, winners, losers_total, pot_after, name_fn=name_fn
    )
    return embed


def build_round_running_note(closes_at: float, url: str | None = None) -> str:
    """Ephemeral pointer when a member opens roulette mid-round."""
    return _running_note(
        "🎡 A roulette round is already running — the wheel spins",
        closes_at, url,
        "Jump to it and place your bet",
        "Place your bet on the round message above",
    )


# ── derby (docs/plans/casino-derby.md) ─────────────────────────────────


def _odds_board() -> str:
    return "\n".join(
        f"{logic.describe_runner(i)} — pays **{logic.derby_odds_label(i)}**"
        for i in range(len(logic.DERBY_FIELD))
    )


def build_derby_round_embed(
    econ: EconSettings,
    closes_at: float,
    bets: list[tuple[int, str, int]],
    accent: discord.Color | None,
    *,
    name_fn: NameFn = mention,
) -> discord.Embed:
    """``bets`` = (user_id, runner description, amount), placement order."""
    embed = discord.Embed(
        title="🏇 Meadow Derby — They're at the Gate!",
        description=(
            f"The race starts <t:{int(closes_at)}:R>. "
            "Back a critter — payouts are total return on your bet.\n​"
        ),
        color=_accent(accent),
    )
    embed.add_field(name="The Field", value=_odds_board() + "\n​", inline=False)
    _add_bets_field(embed, econ, bets, name_fn=name_fn)
    return embed


def _track_lines(positions: list[int]) -> str:
    """One line per runner, racing right-to-left toward the flag: the flag
    stays left-aligned and the shrinking gap IS the distance left, which
    reads cleanly in a proportional font (no column math to break)."""
    return "\n".join(
        f"🏁{'┄' * (logic.DERBY_TRACK_LEN - pos)}{runner.emoji}"
        for runner, pos in zip(logic.DERBY_FIELD, positions)
    )


def build_derby_race_embed(
    econ: EconSettings, positions: list[int], accent: discord.Color | None
) -> discord.Embed:
    embed = discord.Embed(
        title="🏇 Meadow Derby — And They're Off!",
        description=_track_lines(positions),
        color=_accent(accent),
    )
    return embed


def build_derby_result_embed(
    econ: EconSettings,
    winner: int,
    final_positions: list[int],
    bets: list[tuple[int, str, int, int]],
    *,
    pot_after: int = 0,
    name_fn: NameFn = mention,
) -> discord.Embed:
    """``bets`` = (user_id, runner description, amount, payout)."""
    winners = [b for b in bets if b[3] > 0]
    losers_total = sum(b[2] for b in bets if b[3] == 0)
    description = (
        f"{_track_lines(final_positions)}\n​\n"
        f"**{logic.describe_runner(winner)}** takes it!"
    )
    if not bets:
        description += " Nobody bet — the critters race for the glory alone."
    embed = discord.Embed(
        title="🏇 Meadow Derby — Photo Finish!",
        description=description + "\n​",
        color=COLOR_GREEN if winners else COLOR_RED,
    )
    _add_result_fields(
        embed, econ, winners, losers_total, pot_after,
        keep_name="The meadow keeps", name_fn=name_fn,
    )
    return embed


def build_race_running_note(closes_at: float, url: str | None = None) -> str:
    """Ephemeral pointer when a member opens the derby mid-race."""
    return _running_note(
        "🏇 A race is already forming — they're off",
        closes_at, url,
        "Jump in and back a critter",
        "Back a critter on the race message above",
    )


# ── baccarat (casino-classics Stage 1a) ────────────────────────────────


def _baccarat_hand_line(cards: list[str], *, reveal: int | None = None) -> str:
    """One hand as monospace cards + total; ``reveal`` shows only the first
    N cards (the rest as 🂠) for the dealing frame."""
    if reveal is not None and reveal < len(cards):
        shown = cards[:reveal] + ["🂠"] * (len(cards) - reveal)
        return f"`{'  '.join(shown)}`"
    return f"`{'  '.join(cards)}`  ({logic.baccarat_total(cards)})"


def build_baccarat_round_embed(
    econ: EconSettings,
    closes_at: float,
    bets: list[tuple[int, str, int]],
    accent: discord.Color | None,
    *,
    name_fn: NameFn = mention,
) -> discord.Embed:
    """``bets`` = (user_id, side description, amount), placement order."""
    embed = discord.Embed(
        title="🎴 Baccarat — Bets Open!",
        description=(
            f"The cards come down <t:{int(closes_at)}:R>. "
            "Back the Player, the Banker, or the long-shot Tie — "
            "nearest to nine wins.\n​"
        ),
        color=_accent(accent),
    )
    _add_bets_field(embed, econ, bets, name_fn=name_fn)
    return embed


def build_baccarat_deal_embed(
    econ: EconSettings,
    player: list[str],
    banker: list[str],
    accent: discord.Color | None,
) -> discord.Embed:
    """The dealing frame: both starting hands down, draws still to come."""
    embed = discord.Embed(
        title="🎴 Baccarat — No More Bets!",
        description=(
            f"🔵 Player  {_baccarat_hand_line(player, reveal=2)}\n"
            f"🔴 Banker  {_baccarat_hand_line(banker, reveal=2)}\n"
            "The cards hit the felt…"
        ),
        color=_accent(accent),
    )
    return embed


_BACCARAT_VERDICTS = {
    "player": "🔵 **Player wins.**",
    "banker": "🔴 **Banker wins.**",
    "tie": "🟡 **A tie!** Side bets come home; Tie pays 9×.",
}


def build_baccarat_result_embed(
    econ: EconSettings,
    player: list[str],
    banker: list[str],
    bets: list[tuple[int, str, int, int]],
    *,
    pot_after: int = 0,
    name_fn: NameFn = mention,
) -> discord.Embed:
    """``bets`` = (user_id, side description, amount, payout)."""
    winner = logic.baccarat_winner(player, banker)
    verdict = _BACCARAT_VERDICTS[winner]
    if (
        winner == "banker"
        and len(banker) == 3
        and logic.baccarat_total(banker) == 7
    ):
        verdict += " A three-card seven — Banker bets push."
    hands = (
        f"🔵 Player  {_baccarat_hand_line(player)}\n"
        f"🔴 Banker  {_baccarat_hand_line(banker)}"
    )
    if bets:
        description = f"{hands}\n{verdict}\n​"
    else:
        description = (
            f"{hands}\n{verdict} Nobody bet — the cards fall for no one."
        )
    # Green only for a genuine win (payout above the stake) — a coup of
    # pushed side bets came home, it didn't win.
    won = [b for b in bets if b[3] > b[2]]
    paid = [b for b in bets if b[3] > 0]
    losers_total = sum(b[2] for b in bets if b[3] == 0)
    embed = discord.Embed(
        title="🎴 Baccarat — Cards Down!",
        description=description,
        color=COLOR_GREEN if won else COLOR_RED,
    )
    _add_result_fields(
        embed, econ, paid, losers_total, pot_after,
        paid_name="Winners" if won else "Pushed", name_fn=name_fn,
    )
    return embed


def build_coup_running_note(closes_at: float, url: str | None = None) -> str:
    """Ephemeral pointer when a member opens baccarat mid-hand."""
    return _running_note(
        "🎴 A baccarat hand is already forming — cards down",
        closes_at, url,
        "Jump in and pick a side",
        "Pick a side on the hand message above",
    )


# ── dice / sic bo (casino-classics Stage 1b) ───────────────────────────


def build_dice_round_embed(
    econ: EconSettings,
    closes_at: float,
    bets: list[tuple[int, str, int]],
    accent: discord.Color | None,
    *,
    name_fn: NameFn = mention,
) -> discord.Embed:
    """``bets`` = (user_id, bet description, amount), placement order."""
    embed = discord.Embed(
        title="🎲 Dice — Bets Open!",
        description=(
            f"Three dice roll <t:{int(closes_at)}:R>. "
            "Call Big, Small, Odd, or Even — but any triple sweeps "
            "the table.\n​"
        ),
        color=_accent(accent),
    )
    _add_bets_field(embed, econ, bets, name_fn=name_fn)
    return embed


def build_dice_tumble_embed(
    econ: EconSettings, accent: discord.Color | None
) -> discord.Embed:
    """The rolling frame — dice still in the air."""
    embed = discord.Embed(
        title="🎲 Dice — No More Bets!",
        description="The dice tumble across the felt… 🎲 🎲 🎲 …",
        color=_accent(accent),
    )
    return embed


def build_dice_result_embed(
    econ: EconSettings,
    dice: tuple[int, int, int],
    bets: list[tuple[int, str, int, int]],
    *,
    pot_after: int = 0,
    name_fn: NameFn = mention,
) -> discord.Embed:
    """``bets`` = (user_id, bet description, amount, payout)."""
    total = sum(dice)
    is_triple = dice[0] == dice[1] == dice[2]
    call = "Big" if total >= 11 else "Small"
    parity = "odd" if total % 2 else "even"
    verdict = f"{logic.dice_faces(dice)} — **{total}**, {call} and {parity}."
    if is_triple:
        verdict = (
            f"{logic.dice_faces(dice)} — **a triple {dice[0]}!** "
            "The house sweeps every bet."
        )
    if bets:
        description = f"{verdict}\n​"
    else:
        description = f"{verdict} Nobody bet — the dice roll for no one."
    winners = [b for b in bets if b[3] > 0]
    losers_total = sum(b[2] for b in bets if b[3] == 0)
    embed = discord.Embed(
        title="🎲 Dice — No More Bets!",
        description=description,
        color=COLOR_GREEN if winners else COLOR_RED,
    )
    _add_result_fields(
        embed, econ, winners, losers_total, pot_after, name_fn=name_fn
    )
    return embed


def build_roll_running_note(closes_at: float, url: str | None = None) -> str:
    """Ephemeral pointer when a member opens dice mid-roll."""
    return _running_note(
        "🎲 A dice roll is already forming — they fly",
        closes_at, url,
        "Jump in and call it",
        "Call it on the roll message above",
    )


# ── keno (casino-classics Stage 1d) ────────────────────────────────────


def build_keno_round_embed(
    econ: EconSettings,
    closes_at: float,
    bets: list[tuple[int, str, int]],
    accent: discord.Color | None,
    *,
    name_fn: NameFn = mention,
) -> discord.Embed:
    """``bets`` = (user_id, ticket description, amount), placement order."""
    embed = discord.Embed(
        title="🔢 Keno — Tickets Open!",
        description=(
            f"The draw drops <t:{int(closes_at)}:R>. "
            "Pick a tier — the house quick-picks your numbers, fate does "
            "the rest.\n​"
        ),
        color=_accent(accent),
    )
    _add_bets_field(embed, econ, bets, name_fn=name_fn)
    return embed


def build_keno_tumble_embed(
    econ: EconSettings, accent: discord.Color | None
) -> discord.Embed:
    """The drawing frame — balls still in the hopper."""
    embed = discord.Embed(
        title="🔢 Keno — No More Tickets!",
        description="The hopper churns… numbers rattling into the chute…",
        color=_accent(accent),
    )
    return embed


def _keno_board(drawn: list[int]) -> str:
    """The 20 drawn numbers as two monospace rows of ten."""
    cells = [f"{n:>2}" for n in drawn]
    return (
        f"`{'  '.join(cells[:10])}`\n`{'  '.join(cells[10:])}`"
    )


def build_keno_result_embed(
    econ: EconSettings,
    drawn: list[int],
    bets: list[tuple[int, str, int, int]],
    *,
    pot_after: int = 0,
    name_fn: NameFn = mention,
) -> discord.Embed:
    """``bets`` = (user_id, ticket description, amount, payout)."""
    if bets:
        description = f"{_keno_board(drawn)}\n​"
    else:
        description = (
            f"{_keno_board(drawn)}\nNobody held a ticket — twenty numbers "
            "fall for no one."
        )
    winners = [b for b in bets if b[3] > 0]
    losers = [b for b in bets if b[3] == 0]
    losers_total = sum(b[2] for b in losers)
    embed = discord.Embed(
        title="🔢 Keno — The Draw Is In!",
        description=description,
        color=COLOR_GREEN if winners else COLOR_RED,
    )
    _add_result_fields(
        embed, econ, winners, losers_total, pot_after, name_fn=name_fn
    )
    # Keno itemises its losers — every other game collapses them into the
    # house total, but there a loss is self-evident (your number didn't
    # come up). Here a ticket that caught 3 of 10 looks identical to a
    # ticket the house forgot to pay, so the unpaid lines carry their own
    # catch counts. Inserted before the house-keeps field _add_result_fields
    # just appended, so the money line stays last.
    if losers:
        lines = [
            f"{name_fn(uid)} — {d} · {_coins(econ, amount)}"
            for uid, d, amount, _ in losers
        ]
        embed.insert_field_at(
            len(embed.fields) - 1 if losers_total else len(embed.fields),
            name="No payout",
            value="\n".join(logic.cap_lines(lines, limit=1022)) + "\n​",
            inline=False,
        )
    return embed


def build_draw_running_note(closes_at: float, url: str | None = None) -> str:
    """Ephemeral pointer when a member opens keno mid-draw."""
    return _running_note(
        "🔢 A keno draw is already forming — numbers drop",
        closes_at, url,
        "Jump in and grab a ticket",
        "Grab a ticket on the draw message above",
    )


# ── war (casino-classics Stage 1c) ─────────────────────────────────────

_WAR_OUTCOME_LINES = {
    "win": "**High card!**",
    "lose": "The dealer's card stands taller.",
    "war_win": "**Victory on the battlefield!**",
    "war_lose": "The war is lost — both stakes fall.",
    "retreat": "A tactical retreat — half the bet comes home:",
    "refunded": "The table was reset — the bet came home.",
}


def build_war_embed(
    econ: EconSettings,
    user_id: int,
    player: str,
    dealer: str,
    stake: int,
    accent: discord.Color | None,
    *,
    war_player: str | None = None,
    war_dealer: str | None = None,
    outcome: str | None = None,
    payout: int = 0,
    streak: int = 0,
    pot_after: int = 0,
    name_fn: NameFn = mention,
) -> discord.Embed:
    """One war play — instant verdict, the tie standoff, or its resolution.

    Green only for genuine wins; retreat (a partial return) and a refund
    stay on the accent; full losses go red.
    """
    live = outcome is None
    if live or outcome in ("retreat", "refunded"):
        color: discord.Color | int = _accent(accent)
    elif payout > stake:
        color = COLOR_GREEN
    else:
        color = COLOR_RED
    lines = [
        f"{name_fn(user_id)} is in for {_coins(econ, stake)}",
        f"Their card `{player}` · Dealer `{dealer}`",
    ]
    if war_player is not None and war_dealer is not None:
        lines.append(f"⚔️ War cards — theirs `{war_player}` · dealer `{war_dealer}`")
    embed = discord.Embed(
        title="⚔️ Casino War",
        description="\n".join(lines) + "\n​",
        color=color,
    )
    if live:
        embed.add_field(
            name="A Standoff!",
            value=(
                "Matched cards. **Go to War** doubles your stake — win *or "
                "tie* the next card and take 3× your original bet — or "
                "**Retreat** with half. Fortune favors the bold (and so do "
                "the odds)."
            ),
            inline=False,
        )
    else:
        line = _WAR_OUTCOME_LINES.get(outcome or "", "")
        if payout > 0:
            line = f"{line} {_coins(econ, payout)}."
        if payout == 0 and pot_after > 0:
            line = f"{line}\n{_pot_line(pot_after)}"
        if outcome != "refunded":
            line = _with_streak(line, econ, streak)
        embed.add_field(name="Result", value=line, inline=False)
    apply_section_spacing(embed)
    return embed


def build_my_stats_embed(
    econ: EconSettings,
    stats: sqlite3.Row | None,
    used: int,
    cap: int,
    reset_ts: float,
    accent: discord.Color | None,
) -> discord.Embed:
    """The hub's 📊 My Stats ephemeral — personal tally + cap headroom."""
    embed = discord.Embed(title="📊 Your Night at the Tables", color=_accent(accent))
    if stats is not None and int(stats["plays"]) > 0:
        wagered = int(stats["wagered"])
        returned = int(stats["returned"])
        net = returned - wagered
        streak = int(stats["streak"])
        lines = [
            f"Wagered {_coins(econ, wagered)} · returned "
            f"{_coins(econ, returned)}",
            f"Net: **{'+' if net >= 0 else '−'}{abs(net):,}** over "
            f"{int(stats['plays']):,} plays",
        ]
        if int(stats["biggest_win"]) > 0:
            lines.append(
                f"Biggest win: {_coins(econ, int(stats['biggest_win']))} "
                f"({stats['biggest_win_game']})"
            )
        streak_note = _streak_line(econ, streak)
        if streak_note:
            lines.append(streak_note)
        embed.description = "\n".join(lines) + "\n​"
    else:
        embed.description = "You haven't played yet — the tables are patient.\n​"
    if cap > 0:
        embed.add_field(
            name="Today",
            value=(
                f"**{used:,}** of **{cap:,}** {econ.currency_plural} wagered "
                f"· resets <t:{int(reset_ts)}:R>"
            ),
            inline=False,
        )
    return embed


# ── pools (casino-classics Stage 2 — the parimutuel market) ────────────


def _pool_bar(prob: float | None, width: int = 20) -> str:
    """Implied odds as a bar. None = nobody has staked yet.

    Uses the shared ``▰▱`` fill rather than a bare percentage, and takes it
    from ``bar_fill`` so the glyph vocabulary stays the one the style guide
    mandates for new bars. Only the bar is code-spanned — the bold percentage
    has to stay outside, since markdown does not render inside a code span.
    """
    if prob is None:
        return f"{mono(bar_fill(0, 1, width))}  no bets yet"
    bar = mono(bar_fill(round(prob * 1000), 1000, width))
    return f"{bar}  **{prob * 100:.0f}%** over"


def _pools_question(spec, econ: EconSettings, line: float, day: str) -> str:
    return spec.question.format(
        line=pools_logic.format_line(line),
        day=day,
        currency=econ.currency_plural,
    )


def build_pools_panel_embed(
    econ: EconSettings,
    line: float,
    split: pools_logic.PoolSplit,
    closes_at: float,
    day: str,
    accent: discord.Color | None,
    *,
    spec,
    closed: bool = False,
) -> discord.Embed:
    """The standing market panel, repainted as stakes land.

    Deliberately states what the number means and where it comes from: this
    is the only game in the casino whose outcome is not something members
    watch happen, so "the bot counts it at midnight" has to be on the card
    rather than in the manual.

    Under rotation that goes double for the cap. A capped metric is only
    safe to bet on *because* one member cannot run the number up, and a
    member deciding whether to bet has to be able to see that rule — so
    ``spec.cap_note`` prints next to the buttons, not in the manual.
    """
    prob = pools_logic.implied_probability(split)
    when = (
        "Betting is **closed** — settles when the day rolls over."
        if closed
        else f"Betting closes <t:{int(closes_at)}:R>."
    )
    embed = discord.Embed(
        title=f"📈 Pools — today's market · {spec.label}",
        description=(
            f"{_pools_question(spec, econ, line, day)}\n"
            f"{when}\n​"
        ),
        color=_accent(accent),
    )
    embed.add_field(name="Implied Odds", value=_pool_bar(prob), inline=False)
    embed.add_field(
        name="Over", value=_coins(econ, split.over), inline=True
    )
    embed.add_field(
        name="Under", value=_coins(econ, split.under), inline=True
    )
    embed.add_field(
        name="Pool", value=_coins(econ, split.total), inline=True
    )
    embed.add_field(
        name="How It Settles",
        value=(
            "Winners split the whole pool pro-rata — you're betting against "
            "the other side, not the house. The bot counts it up when the "
            "day rolls over and compares it to the line; there is nothing "
            "to dispute."
            + (f"\n{spec.cap_note}" if spec.cap_note else "")
            + "\n​"
        ),
        inline=False,
    )
    embed.add_field(
        name="Tomorrow",
        value=(
            "A different metric is drawn each day — never the same one two "
            "days running.\n​"
        ),
        inline=False,
    )
    embed.set_image(url=f"attachment://{pools_charts.LIVE_FILENAME}")
    apply_section_spacing(embed)
    return embed


def build_pools_result_embed(
    econ: EconSettings,
    day: str,
    result: int,
    line: float,
    winning_side: str,
    payouts: list[tuple[int, int, int]],
    takeout: int,
    accent: discord.Color | None,
    *,
    spec,
    chart: bool = True,
    name_fn: NameFn = mention,
) -> discord.Embed:
    """``payouts`` = (user_id, stake, payout), biggest return first.

    The metric is named in the title rather than assumed: a member reading
    this card hours later has seen other markets since, and "closed at
    1,204" means nothing without the thing that was counted.
    """
    unit = spec.unit.format(currency=econ.currency_plural)
    measured = f"{result:+,}" if spec.signed else f"{result:,}"
    embed = discord.Embed(
        title=f"📈 Pools — the day is in · {spec.label}",
        description=(
            f"**{day}** closed at **{measured}**"
            f"{f' {unit}' if unit else ''} against a line of "
            f"**{pools_logic.format_line(line)}** — "
            f"**{pools_logic.describe_side(winning_side)}** takes it.\n​"
        ),
        color=_accent(accent),
    )
    if payouts:
        rows = [
            f"{name_fn(user_id)} +{payout - stake:,} (staked {stake:,})"
            if payout else f"{name_fn(user_id)} −{stake:,}"
            for user_id, stake, payout in payouts
        ]
        embed.add_field(
            name="Positions",
            # A busy day can outrun Discord's 1024-char field limit, which
            # would 400 the result post; cap_lines trims with an "…and N
            # more" marker instead of silently dropping the tail.
            value="\n".join(
                logic.cap_lines(rows, limit=1022, more_label="more positions")
            ),
            inline=False,
        )
    if takeout:
        embed.add_field(
            name="Takeout",
            value=(
                f"{_coins(econ, takeout)} burned — taken out of circulation, "
                "not paid to the house."
            ),
            inline=False,
        )
    if chart:
        embed.set_image(url=f"attachment://{pools_charts.INSTRUMENT_FILENAME}")
    apply_section_spacing(embed)
    return embed


def build_pools_void_embed(
    day: str,
    refunded: int,
    accent: discord.Color | None,
    *,
    unmeasurable: bool = False,
) -> discord.Embed:
    """One-sided pools have no counterparty, so everyone gets their coins
    back rather than the house taking a cut of the only side that showed.

    ``unmeasurable`` covers the other refund case rotation introduced: a
    round whose metric the bot can no longer measure. Members are told the
    truth — the bot could not count it — rather than being shown the
    one-sided-pool wording for a thing that was not their doing.
    """
    reason = (
        "the bot can no longer measure what it was counting, so there is no "
        "honest way to settle it"
        if unmeasurable
        else "everyone who staked backed the same side, so there was nothing "
        "to play against"
    )
    return discord.Embed(
        title="📈 Pools — No Market Today",
        description=(
            f"The market for **{day}** has been called off: {reason}. All "
            f"**{refunded:,}** staked has been refunded in full.\n​"
        ),
        color=_accent(accent),
    )


# ── mines ──────────────────────────────────────────────────────────────

_MINES_HIDDEN = "⬛"
_MINES_SAFE = "💎"
_MINES_BOMB = "💣"

_MINES_OUTCOME_LINES = {
    "cashed": "**Banked it.**",
    "pushed": "**Broke even** — the stake comes home.",
    "bombed": "**Boom.** The grid takes it.",
    "refunded": "The table was reset — the bet came home.",
}


def _mines_board(
    revealed: tuple[int, ...] | list[int],
    bomb_tiles: tuple[int, ...] | list[int] | None,
) -> str:
    """The grid as it stands, mirroring the buttons above it.

    ``bomb_tiles`` is only ever passed once the round is OVER, and only on
    a bomb: revealing the safe tiles a player walked away from would
    manufacture a near miss out of a decision that was correct by
    construction. A cash-out card shows what was won, never what could
    have been.
    """
    opened, bombs = set(revealed), set(bomb_tiles or ())
    rows = []
    for start in range(0, logic.MINES_TILES, logic.MINES_GRID_WIDTH):
        row = []
        for tile in range(start, start + logic.MINES_GRID_WIDTH):
            if tile in bombs:
                row.append(_MINES_BOMB)
            elif tile in opened:
                row.append(_MINES_SAFE)
            else:
                row.append(_MINES_HIDDEN)
        rows.append("".join(row))
    return "\n".join(rows)


def build_mines_embed(
    econ: EconSettings,
    user_id: int,
    step: MinesStep,
    accent: discord.Color | None,
    *,
    name_fn: NameFn = mention,
) -> discord.Embed:
    """One grid, live or finished.

    The next rung is stated plainly while the grid is live — the decision
    genuinely needs it and hiding it would only make the player press
    blind — but it is never dressed up: no countdown, no "so close", and no
    escalating language as the ladder climbs.
    """
    live = step.outcome is None
    if live:
        color: discord.Color | int = _accent(accent)
    elif step.outcome == "cashed":
        color = COLOR_GREEN
    elif step.outcome in ("pushed", "refunded"):
        color = _accent(accent)
    else:
        color = COLOR_RED
    bomb_word = "bomb" if step.bombs == 1 else "bombs"
    embed = discord.Embed(
        title="💣 Mines",
        description=(
            f"{name_fn(user_id)} is in for {_coins(econ, step.stake)} "
            f"· {step.bombs} {bomb_word}\n​"
        ),
        color=color,
    )
    # A bomb reveals the board — that is what makes the loss legible rather
    # than a shrug. A cash-out deliberately does not.
    show_bombs = step.outcome == "bombed"
    embed.add_field(
        name="The Grid",
        value=(
            _mines_board(step.revealed, step.bomb_tiles if show_bombs else None)
            + "\n​"
        ),
        inline=False,
    )
    if live:
        standing = (
            f"**{logic.mines_mult_label(step.mult)}** · "
            f"{_coins(econ, logic.mines_payout(step.bombs, len(step.revealed), step.stake))}"
            if step.mult
            else "*Open a tile to start the ladder.*"
        )
        lines = [f"Banked if you stop now: {standing}"]
        if step.next_mult:
            lines.append(
                f"One more safe tile: {logic.mines_mult_label(step.next_mult)}"
            )
        embed.add_field(name="Standing", value="\n".join(lines), inline=False)
    else:
        line = _MINES_OUTCOME_LINES.get(step.outcome or "", "")
        if step.topped:
            line = f"**Cleared the board!** {line}"
        if step.payout > 0:
            line = (
                f"{line} {logic.mines_mult_label(step.mult)} — "
                f"{_coins(econ, step.payout)}."
            )
        if step.payout == 0 and step.pot_after > 0:
            line = f"{line}\n{_pot_line(step.pot_after)}"
        if step.outcome != "refunded":
            line = _with_streak(line, econ, step.streak)
        embed.add_field(name="Result", value=line, inline=False)
    apply_section_spacing(embed)
    return embed
