"""Perk shop — the shop listing embed.

The vocabulary it renders lives in ``perks.py``; this module is the storefront,
not the dictionary.

Two surfaces render from here and must not diverge: the ephemeral per-member
shop (``/bank shop`` and the panel's Open Shop button), which knows the
viewer's balance, entitlements and shields, and the member-agnostic channel
panel, which passes none of them.

Pricing is not purely presentational — ``shop_row_price`` folds the flat
``price_role_icon`` into a curated catalog's span and decides the sort floor,
and ``build_shop_embed`` decides row visibility from the guild's per-perk shop
switches, the palette's stock and the raffle. That policy lives here with the
table it drives.
"""

from __future__ import annotations

import discord

from bot_modules.economy.perks import (
    PERK_BLURBS,
    PERK_SHORT,
    PERK_TIERS,
    SELF_PERKS,
    perk_on_sale,
    perk_price,
)
from bot_modules.economy.shop_items import ItemView
from bot_modules.services.embeds import pad_cell as _pad
from bot_modules.services import economy_raffle_service as raffle_svc
from bot_modules.services.economy_service import EconSettings

#: Store rows shown inline before the section defers to the picker. The embed
#: already carries the perk table above, so this stays well short of the
#: description cap and of a wall on a phone.
_MAX_ITEM_ROWS = 8

#: "N left" appears only when N is small enough to be a reason to hurry.
_LOW_STOCK = 5

def shop_row_price(
    settings: EconSettings,
    perk: str,
    icon_catalog: tuple[int, int, int] | None,
    color_catalog: tuple[int, int, int] | None = None,
) -> tuple[int, str]:
    """(sort key, display string) for a shop row's price.

    A curated icon catalog prices per icon, so the role-icon row shows a span
    and sorts on its floor. The flat ``price_role_icon`` folds into that span —
    the picker's bring-your-own Custom entry sells at it, so it's a price the
    row genuinely offers.

    The palette spans the same way, but the flat ``price_role_preset`` is NOT
    folded in: there is no bring-your-own palette colour, so the only prices on
    offer are the palette's own (the catalog service has already substituted the
    flat price for colours left at 0).
    """
    if perk == "role_icon" and icon_catalog is not None:
        lo, hi, _count = icon_catalog
        lo = min(lo, settings.price_role_icon)
        hi = max(hi, settings.price_role_icon)
        return lo, f"{lo:,}" if lo == hi else f"{lo:,}–{hi:,}"
    if perk == "role_preset" and color_catalog is not None:
        lo, hi, _count = color_catalog
        return lo, f"{lo:,}" if lo == hi else f"{lo:,}–{hi:,}"
    price = perk_price(settings, perk)
    return price, f"{price:,}"


def _item_note(item: ItemView, owned: bool) -> str:
    """The trailing note on a store row: state first, then scarcity.

    At most one, and the most actionable one. "Sold out" beats "3 per person"
    because the second is advice about a purchase that can no longer happen.
    """
    if owned:
        return " · ✅"
    remaining = item.remaining
    if remaining == 0:
        return " · _sold out_"
    if remaining is not None and remaining <= _LOW_STOCK:
        return f" · _{remaining} left_"
    if item.per_member_limit == 1:
        return " · _one each_"
    return ""


def _items_block(
    settings: EconSettings,
    items: list[ItemView],
    owned_item_ids: set[int] | frozenset[int],
) -> str:
    """The Server Store section: one aligned cell per admin-defined item.

    Same two-cell shape as the perk table above, so the two sections read as
    one storefront rather than two lists that happen to share an embed. Width
    is measured over these rows only — the perk table sizes itself separately,
    and forcing one shared width would let a long item name push every perk
    row wide.
    """
    shown = items[:_MAX_ITEM_ROWS]
    label_width = max(len(i.name) for i in shown)
    blurb_width = max(len(i.blurb) for i in shown) if any(i.blurb for i in shown) else 0

    lines = []
    for item in shown:
        cell = _pad(item.name, label_width)
        if blurb_width:
            cell += "  " + _pad(item.blurb, blurb_width)
        rent = "/wk" if item.is_rental else ""
        lines.append(
            f"`{cell}` {settings.currency_emoji} **{item.price:,}**{rent}"
            f"{_item_note(item, item.item_id in owned_item_ids)}"
        )
    hidden = len(items) - len(shown)
    if hidden:
        lines.append(f"…and **{hidden}** more — tap 🎁 Store to see them all.")
    lines.append("\nTap **🎁 Store** to buy.")
    return "\n".join(lines)


def build_shop_embed(
    settings: EconSettings,
    gated: set[str],
    accent: discord.Color | None,
    *,
    panel: bool = False,
    owned: set[str] | frozenset[str] = frozenset(),
    comped: set[str] | frozenset[str] = frozenset(),
    icon_catalog: tuple[int, int, int] | None = None,
    color_catalog: tuple[int, int, int] | None = None,
    balance: int | None = None,
    shields_held: int = 0,
    items: list[ItemView] | None = None,
    owned_item_ids: set[int] | frozenset[int] = frozenset(),
) -> discord.Embed:
    """The shop listing, shared by /bank shop and the channel panel.

    Rendered as the aligned code-cell table the leaderboard, guide and quest
    panels use: one ``label  blurb`` cell then the price, grouped into price
    tiers (the quest-board row shape — a single cell keeps the whole row
    inside a phone-width line). Five ``inline=False`` fields carrying four
    words each read as an airy list; a table reads as a storefront.

    ``owned`` marks the viewer's rented rows, ``comped`` marks the ones staff
    get free (a subset of ``owned`` — the comp entitles, so a comped perk is
    owned), ``balance`` puts their wallet in the description, and
    ``shields_held`` marks the shield row — all only meaningful for the
    ephemeral per-member view; the channel panel is member-agnostic and passes
    none of them.
    ``items`` are the guild's admin-defined custom items (already filtered to
    what this viewer should see by ``shop_items_for``); they render as their own
    section below the perk table and the section is absent entirely in a guild
    that has defined none. ``owned_item_ids`` marks the viewer's live rentals
    and delivered one-offs among them.
    ``icon_catalog`` is (min price, max price, icon count) across the guild's
    curated catalog; when set, the role-icon row shows that span and its size
    instead of a single flat price. ``color_catalog`` is the same triple for the
    colour palette, and doubles as the palette row's visibility switch: None
    means this guild has no rentable colours, so the row is left out rather than
    advertising a product with nothing behind it.
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

    # A guild with no curated colours has no Palette product, so the row leaves
    # the table entirely — including the width calculation below, so a hidden row
    # can never pad the visible ones. ("Palette" is not the widest label today,
    # so this costs nothing now and stays correct if the labels change.)
    #
    # Unless the viewer is renting it: a palette can empty out under a live
    # rental (its last colour disabled, or its swatch deleted), and hiding the
    # row then would bill someone weekly for a perk with no row, no price and no
    # ✅ anywhere in the shop.
    show_palette = color_catalog is not None or "role_preset" in owned

    def _stocked(perks: tuple[str, ...]) -> tuple[str, ...]:
        """Drop rows this guild isn't selling, for the same reason as above.

        A switched-off perk leaves the table exactly like an empty palette
        does — including the width calculation, so a hidden row can never pad
        the visible ones. The one carve-out is identical too: a member still
        renting it out to their anniversary keeps their row, because billing
        them for a perk with no row and no ✅ anywhere in the shop is how you
        get a support ticket instead of a clean wind-down.
        """
        return tuple(
            p
            for p in perks
            if (p != "role_preset" or show_palette)
            and (perk_on_sale(settings, p) or p in owned)
        )

    tiers = [(name, _stocked(perks)) for name, perks in PERK_TIERS]
    table_perks: list[str] = list(_stocked(SELF_PERKS))
    # The Voice tier exists only while the guild sells the lease. Its price is
    # no longer the switch — a price of 0 means a free lease, not a hidden one
    # (see SHOP_TOGGLE_PERKS).
    if _stocked(("voice_style",)):
        tiers.append(("Voice", ("voice_style",)))
        table_perks.append("voice_style")

    # One width per table, not per tier, so cells line up across the whole
    # embed rather than jumping at each heading. Defaulted, not asserted: a
    # guild that switches every perk off has an empty table and still gets a
    # valid embed — the Server Store, the raffle and the gifting note can all
    # carry a shop on their own, and `max()` over nothing raises.
    label_width = max((len(PERK_SHORT[p]) for p in table_perks), default=0)
    blurb_width = max((len(PERK_BLURBS[p]) for p in table_perks), default=0)

    def _line(perk: str) -> str:
        _sort, price_str = shop_row_price(settings, perk, icon_catalog, color_catalog)
        note = ""
        if perk in gated:
            note = " · _needs a server feature not enabled here_"
        elif perk in comped:
            # Distinct from a plain ✅ so a mod can tell what they're paying
            # for from what the server is covering — the price still shows,
            # because it's what everyone else pays.
            note = " · ✅ _on the house_"
        elif perk in owned:
            note = " · ✅"
        elif perk == "role_icon" and icon_catalog is not None:
            note = f" · {icon_catalog[2]} + your own"
        elif perk == "role_preset" and color_catalog is not None:
            note = f" · {color_catalog[2]} to choose from"
        return (
            f"`{_pad(PERK_SHORT[perk], label_width)}  "
            f"{_pad(PERK_BLURBS[perk], blurb_width)}` "
            f"{settings.currency_emoji} **{price_str}**{note}"
        )

    for tier_name, perks in tiers:
        if not perks:
            continue
        ordered = sorted(
            perks,
            key=lambda p: shop_row_price(settings, p, icon_catalog, color_catalog)[0],
        )
        embed.add_field(
            name=tier_name,
            value="\n".join(_line(p) for p in ordered) + "\n​",
            inline=False,
        )
    if settings.shop_streak_shield_enabled:
        # One-shot, not a rental — the only non-weekly row, so it carries its
        # own field with the "once" spelled out instead of joining the table.
        # No ``or held`` carve-out: a held shield needs no shop row to work, it
        # burns itself when a streak is at risk, and re-advertising a line the
        # guild has stopped selling would invite a second purchase it refuses.
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
    if items:
        embed.add_field(
            name="Server Store",
            value=_items_block(settings, items, owned_item_ids),
            inline=False,
        )
    # Gifting needs something to gift: with every perk switched off, the only
    # rows left are the store's own items, which `/bank gift` doesn't sell.
    if table_perks:
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
