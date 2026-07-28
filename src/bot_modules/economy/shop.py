"""Perk shop — the shop listing embed.

The vocabulary it renders lives in ``perks.py``; this module is the storefront,
not the dictionary.

Two surfaces render from here and must not diverge: the ephemeral per-member
shop (``/bank shop`` and the panel's Open Shop button), which knows the
viewer's balance, entitlements and shields, and the member-agnostic channel
panel, which passes none of them.

Pricing is not purely presentational — ``shop_row_price`` folds the flat
``price_role_icon`` into a curated catalog's span and decides the sort floor,
and ``build_shop_embed`` decides row visibility from the voice-lease, streak
shield and raffle prices. That policy lives here with the table it drives.
"""

from __future__ import annotations

import discord

from bot_modules.economy.perks import (
    PERK_BLURBS,
    PERK_SHORT,
    PERK_TIERS,
    SELF_PERKS,
    perk_price,
)
from bot_modules.services.embeds import pad_cell as _pad
from bot_modules.services import economy_raffle_service as raffle_svc
from bot_modules.services.economy_service import EconSettings

def shop_row_price(
    settings: EconSettings,
    perk: str,
    icon_catalog: tuple[int, int, int] | None,
) -> tuple[int, str]:
    """(sort key, display string) for a shop row's price.

    A curated icon catalog prices per icon, so the role-icon row shows a span
    and sorts on its floor. The flat ``price_role_icon`` folds into that span —
    the picker's bring-your-own Custom entry sells at it, so it's a price the
    row genuinely offers.
    """
    if perk == "role_icon" and icon_catalog is not None:
        lo, hi, _count = icon_catalog
        lo = min(lo, settings.price_role_icon)
        hi = max(hi, settings.price_role_icon)
        return lo, f"{lo:,}" if lo == hi else f"{lo:,}–{hi:,}"
    price = perk_price(settings, perk)
    return price, f"{price:,}"


def build_shop_embed(
    settings: EconSettings,
    gated: set[str],
    accent: discord.Color | None,
    *,
    panel: bool = False,
    owned: set[str] | frozenset[str] = frozenset(),
    icon_catalog: tuple[int, int, int] | None = None,
    balance: int | None = None,
    shields_held: int = 0,
) -> discord.Embed:
    """The shop listing, shared by /bank shop and the channel panel.

    Rendered as the aligned code-cell table the leaderboard, guide and quest
    panels use: one ``label  blurb`` cell then the price, grouped into price
    tiers (the quest-board row shape — a single cell keeps the whole row
    inside a phone-width line). Five ``inline=False`` fields carrying four
    words each read as an airy list; a table reads as a storefront.

    ``owned`` marks the viewer's rented rows, ``balance`` puts their wallet
    in the description, and ``shields_held`` marks the shield row — all only
    meaningful for the ephemeral per-member view; the channel panel is
    member-agnostic and passes none of them.
    ``icon_catalog`` is (min price, max price, icon count) across the guild's
    curated catalog; when set, the role-icon row shows that span and its size
    instead of a single flat price.
    """
    # The balance lives in the description, not the footer: footers render
    # plain text, so a custom currency emoji would show as raw <:name:id>.
    header = "Weekly rentals · cancel any time"
    if balance is not None:
        header += f" · you have {settings.currency_emoji} **{balance:,}**"
    description = (
        header
        + "\n"
        + (
            "Tap **Open Shop** for your personal menu — rent, customize, "
            "and refund, all private to you."
            if panel
            else "Green buttons customize what you've already rented."
        )
        + "\n​"
    )
    embed = discord.Embed(
        title="🛍️ Perk Shop", description=description, color=accent
    )
    if settings.currency_icon_url:
        embed.set_thumbnail(url=settings.currency_icon_url)

    # The Voice tier exists only while the lease is priced (> 0 = the paywall
    # is armed); at the price-0 dark default the shop shows no trace of it.
    tiers = list(PERK_TIERS)
    table_perks: list[str] = list(SELF_PERKS)
    if settings.price_voice_style > 0:
        tiers.append(("Voice", ("voice_style",)))
        table_perks.append("voice_style")

    # One width per table, not per tier, so cells line up across the whole
    # embed rather than jumping at each heading.
    label_width = max(len(PERK_SHORT[p]) for p in table_perks)
    blurb_width = max(len(PERK_BLURBS[p]) for p in table_perks)

    def _line(perk: str) -> str:
        _sort, price_str = shop_row_price(settings, perk, icon_catalog)
        note = ""
        if perk in gated:
            note = " · _needs a server feature not enabled here_"
        elif perk in owned:
            note = " · ✅"
        elif perk == "role_icon" and icon_catalog is not None:
            note = f" · {icon_catalog[2]} + your own"
        return (
            f"`{_pad(PERK_SHORT[perk], label_width)}  "
            f"{_pad(PERK_BLURBS[perk], blurb_width)}` "
            f"{settings.currency_emoji} **{price_str}**{note}"
        )

    for tier_name, perks in tiers:
        ordered = sorted(
            perks, key=lambda p: shop_row_price(settings, p, icon_catalog)[0]
        )
        embed.add_field(
            name=tier_name,
            value="\n".join(_line(p) for p in ordered) + "\n​",
            inline=False,
        )
    if settings.price_streak_shield > 0:
        # One-shot, not a rental — the only non-weekly row, so it carries its
        # own field with the "once" spelled out instead of joining the table.
        held = " · 🛡️ **held**" if shields_held > 0 else ""
        embed.add_field(
            name="One-shot",
            value=(
                f"🛡️ Streak shield — {settings.currency_emoji} "
                f"**{settings.price_streak_shield:,}** once{held}\n"
                "Auto-burns to save your login streak from a missed day the "
                "free grace can't cover. Hold one at a time."
            ),
            inline=False,
        )
    if raffle_svc.raffle_enabled(settings):
        embed.add_field(
            name="Weekly Raffle",
            value=(
                f"🎟️ Tickets — {settings.currency_emoji} "
                f"**{settings.price_raffle_ticket:,}** each, up to "
                f"{settings.raffle_max_tickets}/week. Drawn at the week "
                "roll; the winner's next weekly perk payment is free "
                "(and they're announced by name)."
            ),
            inline=False,
        )
    embed.add_field(
        name="For a Friend",
        value=(
            "🎁 Any perk above can be gifted at its listed price — "
            "you pay the weekly rent, they wear it. Send one with `/bank gift`."
        ),
        inline=False,
    )

    embed.set_footer(
        text=(
            "Prices are per week, billed every 7 days. A short grace period "
            "covers a missed renewal."
        )
    )
    return embed
