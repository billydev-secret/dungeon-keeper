"""Pure embed builders for the Golden Meadow casino.

Style-guide rules in force: accent color for neutral states, semantic
green/red only for genuine win/loss (COLOR_GOLD for the jackpot moment),
currency always rendered through the guild's own emoji/name, section
spacing via the trailing zero-width space, no custom emoji in footers.
Builders are pure — color and settings arrive as parameters.
"""

from __future__ import annotations

import sqlite3

import discord

from bot_modules.services import casino_logic as logic
from bot_modules.services.casino_service import CasinoSettings
from bot_modules.services.economy_service import EconSettings
from bot_modules.services.embeds import COLOR_GOLD, COLOR_GREEN, COLOR_RED

CASINO_TITLE = "🌻 The Golden Meadow Casino"
_FOOTER = "The meadow always wins — eventually. Play for fun, not for rent."

_GAME_LINES = {
    "coinflip": "🪙 **Coinflip** — call it in the air; a win pays 1.9× your bet",
    "slots": "🎰 **Slots** — three meadow reels; pairs pay back, sevens pay big",
    "blackjack": "🃏 **Blackjack** — beat the dealer to 21; naturals pay 3:2",
    "roulette": "🎡 **Roulette** — one wheel, one window, everyone bets together",
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
            f"🧊 {abs(streak)} losses in a row — the meadow is merciless."
        )
    return None


def _with_streak(desc: str, econ: EconSettings, streak: int) -> str:
    line = _streak_line(econ, streak)
    return f"{desc}\n{line}" if line else desc


def _pot_line(pot_after: int) -> str:
    """Every loss is a tiny ad for the jackpot."""
    return f"🍯 The loss waters the honeypot — now **{pot_after:,}**."


def build_hub_embed(
    econ: EconSettings,
    settings: CasinoSettings,
    accent: discord.Color | None,
    *,
    jackpot: int | None = None,
) -> discord.Embed:
    open_lines = [
        line
        for game, line in _GAME_LINES.items()
        if getattr(settings, f"{game}_enabled")
    ]
    embed = discord.Embed(
        title=CASINO_TITLE,
        description=(
            "Sunshine, clover, and questionable financial decisions. "
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
            name="🍯 Progressive jackpot",
            value=(
                f"Currently {_coins(econ, jackpot)} — every lost bet feeds "
                "it, and triple 7️⃣ on the slots takes it ALL.\n​"
            ),
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
) -> discord.Embed:
    embed = discord.Embed(
        title="How the Golden Meadow pays",
        description=(
            "Payouts below are **total return** on your bet — a 2× win on a "
            "10-bet hands back 20. The house keeps a small edge on every "
            "table; that's what makes it a casino.\n​"
        ),
        color=_accent(accent),
    )
    embed.add_field(
        name="🪙 Coinflip",
        value="Call heads or tails. Win: **1.9×** (95% return).\n​",
        inline=False,
    )
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
    embed.add_field(
        name="🃏 Blackjack",
        value=(
            "Dealer stands on all 17s. Blackjack pays **3:2**, wins pay "
            "**2×**, pushes return your bet. Double down on your first two "
            "cards. Idle hands stand automatically.\n​"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎡 Roulette",
        value=(
            "European wheel, one zero. Red/black **2×** · dozens **3×** · "
            "straight numbers **36×** (~97% return). A betting window opens "
            f"for {settings.roulette_window_seconds}s, then the wheel decides "
            "for everyone at once."
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
            else "The coin does not care. The meadow keeps the bet."
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
) -> discord.Embed:
    reel_line = f"▶ {reels[0]} │ {reels[1]} │ {reels[2]} ◀"
    title = "🎰 Meadow Slots"
    if payout > 0:
        big = jackpot_won > 0 or logic.is_big_win(stake, payout)
        desc = (
            f"{reel_line}\n\n{label} <@{user_id}> bet {_coins(econ, stake)} "
            f"and collects {_coins(econ, payout)}."
        )
        if jackpot_won:
            title = "💥 🎰 THE HONEYPOT SPILLS"
            desc += "\nThe whole progressive pot. The bees weep."
        color = COLOR_GOLD if big else COLOR_GREEN
    else:
        desc = (
            f"{reel_line}\n\n<@{user_id}>'s {_coins(econ, stake)} scatters "
            "into the wildflowers."
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
    econ: EconSettings, user_id: int, amount: int
) -> discord.Embed:
    """The standalone fanfare posted beside a jackpot result."""
    embed = discord.Embed(
        title="🏆 JACKPOT AT THE GOLDEN MEADOW 🏆",
        description=(
            f"🍯 7️⃣ 7️⃣ 7️⃣ 🍯\n\n<@{user_id}> just hit the progressive "
            f"jackpot for {_coins(econ, amount)}!\n"
            "The pot reseeds — every lost bet grows the next one. 🌱"
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
            "The coin spins high over the meadow. 🌾"
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
) -> discord.Embed:
    cells = " │ ".join(sym if sym is not None else "🌀" for sym in revealed)
    embed = discord.Embed(
        title="🎰 Meadow Slots",
        description=(
            f"▶ {cells} ◀\n\n<@{user_id}> bet {_coins(econ, stake)} — "
            "the reels are spinning…"
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
    if bets:
        lines = [
            f"<@{uid}> — {desc} · {_coins(econ, amount)}"
            for uid, desc, amount in bets[-15:]
        ]
        if len(bets) > 15:
            lines.insert(0, f"*…and {len(bets) - 15} earlier bet(s)*")
        embed.add_field(name=f"Bets ({len(bets)})", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Bets", value="*No bets yet — be first.*", inline=False)
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
            "The wheel spins for the bees alone."
        )
    embed = discord.Embed(
        title="🎡 Roulette — no more bets!",
        description=description,
        color=COLOR_GREEN if winners else COLOR_RED,
    )
    if winners:
        embed.add_field(
            name="Winners",
            value="\n".join(
                f"{'💥 ' if logic.is_big_win(amount, payout) else ''}"
                f"<@{uid}> — {d} · {_coins(econ, amount)} → {_coins(econ, payout)}"
                for uid, d, amount, payout in winners
            )
            + "\n​",
            inline=False,
        )
    if losers_total:
        kept = _coins(econ, losers_total)
        if pot_after > 0:
            kept += f"\n{_pot_line(pot_after)}"
        embed.add_field(name="The meadow keeps", value=kept, inline=False)
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
