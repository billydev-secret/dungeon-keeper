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
"""

from __future__ import annotations

import sqlite3

import discord

from bot_modules.services import casino_logic as logic
from bot_modules.services.casino_service import CasinoSettings
from bot_modules.services.economy_service import EconSettings
from bot_modules.services.branding_service import DEFAULT_CASINO_NAME
from bot_modules.services.embeds import COLOR_GOLD, COLOR_GREEN, COLOR_RED


def casino_title(casino_name: str = DEFAULT_CASINO_NAME) -> str:
    """Hub-panel title for a guild's casino ("🎲 The Golden Meadow Casino")."""
    return f"🎲 The {casino_name} Casino"


# Kept for callers with no guild handy; the per-guild name is the norm.
CASINO_TITLE = casino_title()
_FOOTER = "Play for fun, not for rent."

_GAME_LINES = {
    "coinflip": "🪙 **Coinflip** — call it in the air; a win pays 1.9× your bet",
    "slots": "🎰 **Slots** — three spinning reels; pairs pay back, sevens pay big",
    "blackjack": "🃏 **Blackjack** — beat the dealer to 21; naturals pay 3:2",
    "roulette": "🎡 **Roulette** — one wheel, one window, everyone bets together",
    "derby": "🏇 **Derby** — six critters, one finish line; back your favorite",
    "baccarat": "🎴 **Baccarat** — Player, Banker, or Tie; nearest to nine wins",
    "dice": "🎲 **Dice** — three dice, one roll; call Big, Small, Odd, or Even",
    "war": "⚔️ **War** — one card each, high card wins; on a tie, go to war",
    "keno": "🔢 **Keno** — grab a ticket, 20 numbers drop, catches pay big",
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


_TICKER_EMOJI = {"coinflip": "🪙", "slots": "🎰", "blackjack": "🃏", "war": "⚔️"}


def ticker_line(user_id: int, game: str, stake: int, payout: int) -> str:
    """One compact floor-ticker entry (embed mentions never ping)."""
    if payout > stake:
        result = f"**{payout:,}**"
    elif payout == stake and payout > 0:
        result = "push"
    elif payout > 0:
        result = f"{payout:,} back"
    else:
        result = "the house"
    return f"{_TICKER_EMOJI.get(game, '🎲')} <@{user_id}> · {stake:,} → {result}"


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
            name="💰 Progressive jackpot",
            value=(
                f"Currently {_coins(econ, jackpot)} — every lost bet feeds "
                "it, and triple 7️⃣ on the slots takes it ALL.\n​"
            ),
            inline=False,
        )
    if ticker:
        embed.add_field(
            name="📡 On the floor",
            value="\n".join(
                ticker_line(uid, game, stake, payout)
                for uid, game, stake, payout in ticker
            ) + "\n​",
            inline=False,
        )
    if standings is not None:
        earner, loser = standings
        rows = []
        if earner is not None:
            rows.append(
                f"📈 Up most: <@{earner[0]}> · "
                f"**+{earner[1]:,}** {econ.currency_plural}"
            )
        if loser is not None:
            rows.append(
                f"📉 Down most: <@{loser[0]}> · "
                f"**−{abs(loser[1]):,}** {econ.currency_plural}"
            )
        if rows:
            # Embed mentions never ping (the panel is sent/edited with
            # AllowedMentions.none()); names render, nobody's tagged.
            embed.add_field(
                name="📊 Today at the tables",
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
    embed.add_field(name="House rules", value=" · ".join(limits), inline=False)
    embed.set_footer(text=_FOOTER)
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
            value="Call heads or tails. Win: **1.9×** (95% return).\n​",
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
                f"Two 7️⃣ **{logic.SLOT_TWO_SEVENS_MULT}×** · any pair **1.5×** "
                "(~93% return)\n​"
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
                "straight numbers **36×** (~97% return). A betting window opens "
                f"for {settings.roulette_window_seconds}s, then the wheel decides "
                "for everyone at once.\n​"
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
                "pays big (91–96% return by runner). Betting stays open for "
                f"{settings.derby_window_seconds}s, then they're off.\n​"
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
                "shot (~86% return). Ties push the side bets. A betting window "
                f"opens for {settings.baccarat_window_seconds}s, then the cards "
                "decide for everyone at once.\n​"
            ),
            inline=False,
        )
    if settings.dice_enabled:
        embed.add_field(
            name="🎲 Dice",
            value=(
                "Three dice, one shared roll. **Big** (11–17), **Small** "
                "(4–10), **Odd**, **Even** — all pay **2×**, and any triple "
                "sweeps the table (~97% return). A betting window opens for "
                f"{settings.dice_window_seconds}s, then one roll settles "
                "everyone.\n​"
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
                f"drop in one shared draw. {pays} — pays scale with how many "
                "of yours hit (~95% return, tuned far kinder than real keno). "
                f"Tickets stay open for {settings.keno_window_seconds}s, then "
                "one draw settles everyone.\n​"
            ),
            inline=False,
        )
    if settings.daily_wager_cap:
        embed.add_field(
            name="Daily limit",
            value=(
                f"You can stake up to **{settings.daily_wager_cap:,}** "
                f"{econ.currency_plural} per day across all tables."
            ),
            inline=False,
        )
    embed.set_footer(text=_FOOTER)
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
) -> discord.Embed:
    won = payout > 0
    face = "🌞" if landed == "heads" else "🌙"
    desc = (
        f"<@{user_id}> called **{call}** for {_coins(econ, stake)}.\n"
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
    embed.set_footer(text=_FOOTER)
    return embed


def _slot_cabinet(cells: tuple[str, ...]) -> str:
    """Box the reel row in a small text-art slot machine. The reel row
    carries no side walls and no code span — emoji width varies per
    platform (and code spans swallow emoji rendering), so only the pure
    box-glyph lines are expected to align with each other."""
    row = f"▶ {cells[0]} │ {cells[1]} │ {cells[2]} ◀"
    return f"╭─────┤🎰├─────╮\n{row}\n╰──┬─────────┬──╯"


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
) -> discord.Embed:
    reel_line = _slot_cabinet(reels)
    title = f"🎰 {casino_name} Slots"
    if payout > 0:
        desc = (
            f"{reel_line}\n\n{label} <@{user_id}> bet {_coins(econ, stake)} "
            f"and collects {_coins(econ, payout)}."
        )
        if jackpot_won:
            title = "💥 🎰 THE JACKPOT SPILLS"
            desc += "\nThe whole progressive pot — gone in one spin."
        # A big win is still a win: the celebration lives in the copy, not in a
        # third color tier (style guide: green = win, red = loss, full stop).
        color = COLOR_GREEN
    else:
        desc = (
            f"{reel_line}\n\n<@{user_id}>'s {_coins(econ, stake)} scatters "
            "into the house's take."
        )
        if pot_after > 0:
            desc += f"\n{_pot_line(pot_after)}"
        color = COLOR_RED
    embed = discord.Embed(
        title=title, description=_with_streak(desc, econ, streak), color=color
    )
    embed.set_footer(text=_FOOTER)
    return embed


def build_jackpot_celebration(
    econ: EconSettings, user_id: int, amount: int,
    *, casino_name: str = DEFAULT_CASINO_NAME,
) -> discord.Embed:
    """The standalone fanfare posted beside a jackpot result."""
    embed = discord.Embed(
        title=f"🏆 JACKPOT AT THE {casino_name.upper()} 🏆",
        description=(
            f"💰 7️⃣ 7️⃣ 7️⃣ 💰\n\n<@{user_id}> just hit the progressive "
            f"jackpot for {_coins(econ, amount)}!\n"
            "The pot reseeds — every lost bet grows the next one."
        ),
        color=COLOR_GOLD,
    )
    embed.set_footer(text=_FOOTER)
    return embed


# ── animation frames (big bets get the show; money is already settled) ─


def build_coinflip_spin_embed(
    econ: EconSettings, user_id: int, call: str, stake: int,
    accent: discord.Color | None,
) -> discord.Embed:
    embed = discord.Embed(
        title="🪙 Coinflip — it's in the air!",
        description=(
            f"<@{user_id}> calls **{call}** for {_coins(econ, stake)}…\n"
            "The coin spins high in the air. 🪙"
        ),
        color=_accent(accent),
    )
    embed.set_footer(text=_FOOTER)
    return embed


def build_slots_spin_embed(
    econ: EconSettings,
    user_id: int,
    stake: int,
    revealed: tuple[str | None, str | None, str | None],
    accent: discord.Color | None,
    *,
    casino_name: str = DEFAULT_CASINO_NAME,
) -> discord.Embed:
    cells = tuple(sym if sym is not None else "🌀" for sym in revealed)
    embed = discord.Embed(
        title=f"🎰 {casino_name} Slots",
        description=(
            f"{_slot_cabinet(cells)}\n\n<@{user_id}> bet "
            f"{_coins(econ, stake)} — the reels are spinning…"
        ),
        color=_accent(accent),
    )
    embed.set_footer(text=_FOOTER)
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
) -> discord.Embed:
    stake_note = f" (doubled to {stake:,})" if doubled else ""
    embed = discord.Embed(
        title="🃏 Blackjack",
        description=f"<@{user_id}> is in for {_coins(econ, stake)}{stake_note}\n​",
        color=_accent(accent),
    )
    embed.add_field(
        name="Their hand", value=_hand_line(player) + "\n​", inline=False
    )
    embed.add_field(
        name="Dealer",
        value=_hand_line(dealer_first_two) + "\n*The dealer turns the hole card…*",
        inline=False,
    )
    embed.set_footer(text=_FOOTER)
    return embed


def build_roulette_bounce_embed(
    econ: EconSettings, bounce: tuple[int, int], accent: discord.Color | None
) -> discord.Embed:
    frames = " … ".join(
        f"{_COLOR_DOTS[logic.wheel_color(n)]} {n}" for n in bounce
    )
    embed = discord.Embed(
        title="🎡 Roulette — no more bets!",
        description=f"The ball dances across the wheel… {frames} …",
        color=_accent(accent),
    )
    embed.set_footer(text=_FOOTER)
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
        description=f"<@{user_id}> is in for {_coins(econ, stake)}{stake_note}\n​",
        color=color,
    )
    embed.add_field(
        name="Their hand", value=_hand_line(player) + "\n​", inline=False
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
    embed.set_footer(text=_FOOTER)
    return embed


def _add_bets_field(
    embed: discord.Embed, econ: EconSettings, bets: list[tuple[int, str, int]]
) -> None:
    """The live bets board, newest first, char-capped under the 1024 field
    limit — a long currency name or big stakes must never 400 the repaint
    and silently freeze the board (the result embed's cap_lines rule)."""
    if not bets:
        embed.add_field(name="Bets", value="*No bets yet — be first.*", inline=False)
        return
    lines = [
        f"<@{uid}> — {desc} · {_coins(econ, amount)}"
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
) -> discord.Embed:
    """``bets`` = (user_id, bet description, amount), placement order."""
    embed = discord.Embed(
        title="🎡 Roulette — bets open!",
        description=(
            f"The wheel spins <t:{int(closes_at)}:R>. "
            "Pick a color, a dozen, or go all-in on a single number.\n​"
        ),
        color=_accent(accent),
    )
    _add_bets_field(embed, econ, bets)
    embed.set_footer(text=_FOOTER)
    return embed


_COLOR_DOTS = {"red": "🔴", "black": "⚫", "green": "🟢"}


def build_roulette_result_embed(
    econ: EconSettings,
    result: int,
    bets: list[tuple[int, str, int, int]],
    *,
    pot_after: int = 0,
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
        title="🎡 Roulette — no more bets!",
        description=description,
        color=COLOR_GREEN if winners else COLOR_RED,
    )
    if winners:
        winner_lines = [
            f"{'💥 ' if logic.is_big_win(amount, payout) else ''}"
            f"<@{uid}> — {d} · {_coins(econ, amount)} → {_coins(econ, payout)}"
            for uid, d, amount, payout in winners
        ]
        # Cap under the 1024 field limit (reserve 2 for the trailing "\n​").
        embed.add_field(
            name="Winners",
            value="\n".join(logic.cap_lines(winner_lines, limit=1022)) + "\n​",
            inline=False,
        )
    if losers_total:
        kept = _coins(econ, losers_total)
        if pot_after > 0:
            kept += f"\n{_pot_line(pot_after)}"
        embed.add_field(name="The house keeps", value=kept, inline=False)
    embed.set_footer(text=_FOOTER)
    return embed


def build_round_running_note(closes_at: float, url: str | None = None) -> str:
    """Ephemeral pointer when a member opens roulette mid-round."""
    note = (
        f"🎡 A roulette round is already running — the wheel spins "
        f"<t:{int(closes_at)}:R>."
    )
    if url:
        return f"{note} Jump to it and place your bet: {url}"
    return f"{note} Place your bet on the round message above."


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
) -> discord.Embed:
    """``bets`` = (user_id, runner description, amount), placement order."""
    embed = discord.Embed(
        title="🏇 Meadow Derby — they're at the gate!",
        description=(
            f"The race starts <t:{int(closes_at)}:R>. "
            "Back a critter — payouts are total return on your bet.\n​"
        ),
        color=_accent(accent),
    )
    embed.add_field(name="The field", value=_odds_board() + "\n​", inline=False)
    _add_bets_field(embed, econ, bets)
    embed.set_footer(text=_FOOTER)
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
        title="🏇 Meadow Derby — and they're off!",
        description=_track_lines(positions),
        color=_accent(accent),
    )
    embed.set_footer(text=_FOOTER)
    return embed


def build_derby_result_embed(
    econ: EconSettings,
    winner: int,
    final_positions: list[int],
    bets: list[tuple[int, str, int, int]],
    *,
    pot_after: int = 0,
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
        title="🏇 Meadow Derby — photo finish!",
        description=description + "\n​",
        color=COLOR_GREEN if winners else COLOR_RED,
    )
    if winners:
        winner_lines = [
            f"{'💥 ' if logic.is_big_win(amount, payout) else ''}"
            f"<@{uid}> — {d} · {_coins(econ, amount)} → {_coins(econ, payout)}"
            for uid, d, amount, payout in winners
        ]
        embed.add_field(
            name="Winners",
            value="\n".join(logic.cap_lines(winner_lines, limit=1022)) + "\n​",
            inline=False,
        )
    if losers_total:
        kept = _coins(econ, losers_total)
        if pot_after > 0:
            kept += f"\n{_pot_line(pot_after)}"
        embed.add_field(name="The meadow keeps", value=kept, inline=False)
    embed.set_footer(text=_FOOTER)
    return embed


def build_race_running_note(closes_at: float, url: str | None = None) -> str:
    """Ephemeral pointer when a member opens the derby mid-race."""
    note = (
        f"🏇 A race is already forming — they're off <t:{int(closes_at)}:R>."
    )
    if url:
        return f"{note} Jump in and back a critter: {url}"
    return f"{note} Back a critter on the race message above."


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
) -> discord.Embed:
    """``bets`` = (user_id, side description, amount), placement order."""
    embed = discord.Embed(
        title="🎴 Baccarat — bets open!",
        description=(
            f"The cards come down <t:{int(closes_at)}:R>. "
            "Back the Player, the Banker, or the long-shot Tie — "
            "nearest to nine wins.\n​"
        ),
        color=_accent(accent),
    )
    _add_bets_field(embed, econ, bets)
    embed.set_footer(text=_FOOTER)
    return embed


def build_baccarat_deal_embed(
    econ: EconSettings,
    player: list[str],
    banker: list[str],
    accent: discord.Color | None,
) -> discord.Embed:
    """The dealing frame: both starting hands down, draws still to come."""
    embed = discord.Embed(
        title="🎴 Baccarat — no more bets!",
        description=(
            f"🔵 Player  {_baccarat_hand_line(player, reveal=2)}\n"
            f"🔴 Banker  {_baccarat_hand_line(banker, reveal=2)}\n"
            "The cards hit the felt…"
        ),
        color=_accent(accent),
    )
    embed.set_footer(text=_FOOTER)
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
        title="🎴 Baccarat — cards down!",
        description=description,
        color=COLOR_GREEN if won else COLOR_RED,
    )
    if paid:
        paid_lines = [
            f"{'💥 ' if logic.is_big_win(amount, payout) else ''}"
            f"<@{uid}> — {d} · {_coins(econ, amount)} → {_coins(econ, payout)}"
            for uid, d, amount, payout in paid
        ]
        embed.add_field(
            name="Winners" if won else "Pushed",
            value="\n".join(logic.cap_lines(paid_lines, limit=1022)) + "\n​",
            inline=False,
        )
    if losers_total:
        kept = _coins(econ, losers_total)
        if pot_after > 0:
            kept += f"\n{_pot_line(pot_after)}"
        embed.add_field(name="The house keeps", value=kept, inline=False)
    embed.set_footer(text=_FOOTER)
    return embed


def build_coup_running_note(closes_at: float, url: str | None = None) -> str:
    """Ephemeral pointer when a member opens baccarat mid-hand."""
    note = (
        f"🎴 A baccarat hand is already forming — cards down "
        f"<t:{int(closes_at)}:R>."
    )
    if url:
        return f"{note} Jump in and pick a side: {url}"
    return f"{note} Pick a side on the hand message above."


# ── dice / sic bo (casino-classics Stage 1b) ───────────────────────────


def build_dice_round_embed(
    econ: EconSettings,
    closes_at: float,
    bets: list[tuple[int, str, int]],
    accent: discord.Color | None,
) -> discord.Embed:
    """``bets`` = (user_id, bet description, amount), placement order."""
    embed = discord.Embed(
        title="🎲 Dice — bets open!",
        description=(
            f"Three dice roll <t:{int(closes_at)}:R>. "
            "Call Big, Small, Odd, or Even — but any triple sweeps "
            "the table.\n​"
        ),
        color=_accent(accent),
    )
    _add_bets_field(embed, econ, bets)
    embed.set_footer(text=_FOOTER)
    return embed


def build_dice_tumble_embed(
    econ: EconSettings, accent: discord.Color | None
) -> discord.Embed:
    """The rolling frame — dice still in the air."""
    embed = discord.Embed(
        title="🎲 Dice — no more bets!",
        description="The dice tumble across the felt… 🎲 🎲 🎲 …",
        color=_accent(accent),
    )
    embed.set_footer(text=_FOOTER)
    return embed


def build_dice_result_embed(
    econ: EconSettings,
    dice: tuple[int, int, int],
    bets: list[tuple[int, str, int, int]],
    *,
    pot_after: int = 0,
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
        title="🎲 Dice — no more bets!",
        description=description,
        color=COLOR_GREEN if winners else COLOR_RED,
    )
    if winners:
        winner_lines = [
            f"{'💥 ' if logic.is_big_win(amount, payout) else ''}"
            f"<@{uid}> — {d} · {_coins(econ, amount)} → {_coins(econ, payout)}"
            for uid, d, amount, payout in winners
        ]
        embed.add_field(
            name="Winners",
            value="\n".join(logic.cap_lines(winner_lines, limit=1022)) + "\n​",
            inline=False,
        )
    if losers_total:
        kept = _coins(econ, losers_total)
        if pot_after > 0:
            kept += f"\n{_pot_line(pot_after)}"
        embed.add_field(name="The house keeps", value=kept, inline=False)
    embed.set_footer(text=_FOOTER)
    return embed


def build_roll_running_note(closes_at: float, url: str | None = None) -> str:
    """Ephemeral pointer when a member opens dice mid-roll."""
    note = (
        f"🎲 A dice roll is already forming — they fly "
        f"<t:{int(closes_at)}:R>."
    )
    if url:
        return f"{note} Jump in and call it: {url}"
    return f"{note} Call it on the roll message above."


# ── keno (casino-classics Stage 1d) ────────────────────────────────────


def build_keno_round_embed(
    econ: EconSettings,
    closes_at: float,
    bets: list[tuple[int, str, int]],
    accent: discord.Color | None,
) -> discord.Embed:
    """``bets`` = (user_id, ticket description, amount), placement order."""
    embed = discord.Embed(
        title="🔢 Keno — tickets open!",
        description=(
            f"The draw drops <t:{int(closes_at)}:R>. "
            "Pick a tier — the house quick-picks your numbers, fate does "
            "the rest.\n​"
        ),
        color=_accent(accent),
    )
    _add_bets_field(embed, econ, bets)
    embed.set_footer(text=_FOOTER)
    return embed


def build_keno_tumble_embed(
    econ: EconSettings, accent: discord.Color | None
) -> discord.Embed:
    """The drawing frame — balls still in the hopper."""
    embed = discord.Embed(
        title="🔢 Keno — no more tickets!",
        description="The hopper churns… numbers rattling into the chute…",
        color=_accent(accent),
    )
    embed.set_footer(text=_FOOTER)
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
    losers_total = sum(b[2] for b in bets if b[3] == 0)
    embed = discord.Embed(
        title="🔢 Keno — the draw is in!",
        description=description,
        color=COLOR_GREEN if winners else COLOR_RED,
    )
    if winners:
        winner_lines = [
            f"{'💥 ' if logic.is_big_win(amount, payout) else ''}"
            f"<@{uid}> — {d} · {_coins(econ, amount)} → {_coins(econ, payout)}"
            for uid, d, amount, payout in winners
        ]
        embed.add_field(
            name="Winners",
            value="\n".join(logic.cap_lines(winner_lines, limit=1022)) + "\n​",
            inline=False,
        )
    if losers_total:
        kept = _coins(econ, losers_total)
        if pot_after > 0:
            kept += f"\n{_pot_line(pot_after)}"
        embed.add_field(name="The house keeps", value=kept, inline=False)
    embed.set_footer(text=_FOOTER)
    return embed


def build_draw_running_note(closes_at: float, url: str | None = None) -> str:
    """Ephemeral pointer when a member opens keno mid-draw."""
    note = (
        f"🔢 A keno draw is already forming — numbers drop "
        f"<t:{int(closes_at)}:R>."
    )
    if url:
        return f"{note} Jump in and grab a ticket: {url}"
    return f"{note} Grab a ticket on the draw message above."


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
        f"<@{user_id}> is in for {_coins(econ, stake)}",
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
            name="A standoff!",
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
    embed.set_footer(text=_FOOTER)
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
    embed = discord.Embed(title="📊 Your night at the tables", color=_accent(accent))
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
    embed.set_footer(text=_FOOTER)
    return embed
