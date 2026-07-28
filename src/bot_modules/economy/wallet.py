"""Wallet embed — the member's private balance, activity and rentals view.

Rendered for ``/bank wallet`` and the leaderboard panel's Wallet button, which
must show the identical embed. The builder is pure: the cog reads the six
values on one connection and hands them over, so the decisions here — which
gift attribution a rental line carries, how many rows survive the embed field
cap, when the casino block calls a streak hot or cold — are testable without a
Discord mock.

The ``fit_lines`` guard this leans on (in ``services/embeds.py``, shared with
the quest board) is load-bearing rather than cosmetic: memos make each activity
row variable-length, and ten long ones overrun Discord's 1024-char field cap,
which rejects the *whole* embed. Dropping the oldest rows keeps the wallet
rendering instead of 400-ing.
"""

from __future__ import annotations

import json
import sqlite3

import discord

from bot_modules.economy.register import kind_display
from bot_modules.economy.perks import PERK_LABELS
from bot_modules.economy.view_helpers import unit
from bot_modules.services.embeds import fit_lines
from bot_modules.services.economy_service import EconSettings

# Memos are shortened in the one-line wallet render.
WALLET_MEMO_LEN = 40


def ellipsis(text: str, limit: int = WALLET_MEMO_LEN) -> str:
    """Shorten a memo for the cramped one-line wallet render."""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def memo_of(row_meta: str | None) -> str | None:
    """Pull the memo out of a ledger row's meta JSON, tolerating junk."""
    if not row_meta:
        return None
    try:
        memo = json.loads(row_meta).get("memo")
    except (ValueError, TypeError, AttributeError):
        return None
    return memo if isinstance(memo, str) and memo else None


def rental_lines(settings: EconSettings, rentals: list, user_id: int) -> list[str]:
    """One line per live rental for the wallet's 'Active rentals' field."""
    emoji = settings.currency_emoji
    lines: list[str] = []
    for r in rentals:
        perk = str(r["perk"])
        label = PERK_LABELS.get(perk, perk)
        price = int(r["price"])
        next_bill = int(r["next_bill_at"])
        owner_id = int(r["user_id"])
        beneficiary_id = int(r["beneficiary_id"])
        attribution = ""
        if beneficiary_id != owner_id:
            if beneficiary_id == user_id:
                attribution = " (gift received)"
            elif owner_id == user_id:
                attribution = f" (gift to <@{beneficiary_id}>)"
        grace = " · ⏳ in grace" if str(r["state"]) == "grace" else ""
        lines.append(
            f"**{label}**{attribution} — {emoji} {price:,}/wk · "
            f"renews <t:{next_bill}:R>{grace}"
        )
    return lines


def build_wallet_embed(
    settings: EconSettings,
    *,
    balance: int,
    ledger: list,
    rentals: list,
    shields: int,
    casino: sqlite3.Row | None,
    viewer_id: int,
    color: discord.Color | None,
) -> discord.Embed:
    """The member's wallet: balance, recent activity, rentals, casino record."""
    description = f"{settings.currency_emoji} **{balance:,}** {unit(settings, balance)}"
    if shields > 0:
        description += "\n🛡️ Streak shield held"
    embed = discord.Embed(
        title=f"{settings.currency_emoji} {settings.wallet_name}",
        description=description,
        color=color,
    )
    if settings.currency_icon_url:
        embed.set_thumbnail(url=settings.currency_icon_url)

    if ledger:
        lines = []
        for row in ledger:
            amount = int(row["amount"])
            sign = "+" if amount >= 0 else "−"
            ts = int(row["created_at"])
            glyph, label = kind_display(str(row["kind"]))
            line = (
                f"{sign}{abs(amount):,} {settings.currency_emoji} · "
                f"{glyph} {label} · <t:{ts}:R>"
            )
            memo = memo_of(row["meta"])
            if memo:
                line += f" — *{discord.utils.escape_markdown(ellipsis(memo))}*"
            lines.append(line)
        embed.add_field(name="Recent Activity", value=fit_lines(lines), inline=False)
    else:
        embed.add_field(
            name="Recent Activity", value="_No activity yet._", inline=False
        )

    lines = rental_lines(settings, rentals, viewer_id)
    if lines:
        # A dozen+ gifted perks can overrun the 1024-char field and 400 the
        # whole embed — trim to what fits (mirrors Recent Activity above).
        embed.add_field(name="Active Rentals", value=fit_lines(lines), inline=False)

    if casino is not None and int(casino["plays"]) > 0:
        wagered = int(casino["wagered"])
        returned = int(casino["returned"])
        net = returned - wagered
        streak = int(casino["streak"])
        lines = [
            f"Wagered **{wagered:,}** · returned **{returned:,}** · "
            f"net **{'+' if net >= 0 else '−'}{abs(net):,}**"
        ]
        if int(casino["biggest_win"]) > 0:
            lines.append(
                f"Biggest win: {settings.currency_emoji} "
                f"**{int(casino['biggest_win']):,}** "
                f"({str(casino['biggest_win_game'])})"
            )
        if streak >= 3:
            lines.append(f"🔥 {streak}-win streak going")
        elif streak <= -3:
            lines.append(f"🧊 {abs(streak)} losses running — walk away?")
        embed.add_field(name="🎰 At the Tables", value="\n".join(lines), inline=False)

    return embed
