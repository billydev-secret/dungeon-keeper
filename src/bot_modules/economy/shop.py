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

#: Items per page of the dedicated storefront. Discord's select would hold 25,
#: but 25 monospace rows is a wall on a phone and a 25-deep scroll in the
#: picker — ten reads as a shelf, and a pair of arrows is cheaper than a
#: scroll. It is also why the store no longer has a ceiling: the old picker
#: showed the first 25 items and said so, which is a curation problem the
#: guild never asked for.
STORE_PAGE_SIZE = 10

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


def _item_rows(
    settings: EconSettings,
    items: list[ItemView],
    owned_item_ids: set[int] | frozenset[int],
) -> list[str]:
    """One aligned cell per item, sized to exactly the items handed in.

    Same two-cell shape as the perk table, so a store section and a perk tier
    read as one storefront rather than two lists that happen to share an embed.
    Width is measured over these rows only — the perk table sizes itself
    separately, and forcing one shared width would let a long item name push
    every perk row wide. It is also why the page, not the whole store, is what
    gets passed in: padding page 1 to the width of an item on page 3 would
    leave a gutter with nothing in it.
    """
    label_width = max(len(i.name) for i in items)
    blurb_width = max(len(i.blurb) for i in items) if any(i.blurb for i in items) else 0

    rows = []
    for item in items:
        cell = _pad(item.name, label_width)
        if blurb_width:
            cell += "  " + _pad(item.blurb, blurb_width)
        rent = "/wk" if item.is_rental else ""
        rows.append(
            f"`{cell}` {settings.currency_emoji} **{item.price:,}**{rent}"
            f"{_item_note(item, item.item_id in owned_item_ids)}"
        )
    return rows


def _items_block(
    settings: EconSettings,
    items: list[ItemView],
    owned_item_ids: set[int] | frozenset[int],
) -> str:
    """The Server Store teaser on the channel panel.

    The panel is a poster, not a menu: it is one static message that cannot
    page, so it advertises the first few rows and sends people to the shop,
    where the store has pages of its own. The ephemeral shop passes no
    ``items`` at all — its store pages *are* the store, and a preview there
    would be the same list twice in one book.
    """
    shown = items[:_MAX_ITEM_ROWS]
    lines = _item_rows(settings, shown, owned_item_ids)
    hidden = len(items) - len(shown)
    if hidden:
        lines.append(f"…and **{hidden}** more.")
    lines.append("\nTap **Open Shop** to browse and buy.")
    return "\n".join(lines)


#: The shop's sections, in the order a member walks them. What a guild stocks
#: itself leads — it is the part that changes and the part they came for; the
#: rest is the same ladder in every guild.
SECTION_SPECIALS = "specials"
SECTION_COSMETICS = "cosmetics"
SECTION_SERVER = "server"
SECTION_GAMES = "games"

#: What each section calls itself — the caption between the arrows, and the
#: title of its own embed, so the two can never disagree about where you are.
#: "Server features" is deliberately wider than the one product in it today
#: (the voice-room lease): personal channels land here when they ship, and a
#: section renamed under members is worse than one that reads slightly roomy.
SECTION_CAPTIONS = {
    SECTION_SPECIALS: "🎁 Specials",
    SECTION_COSMETICS: "🎨 Role cosmetics",
    SECTION_SERVER: "🏠 Server features",
    SECTION_GAMES: "🎲 Game features",
}


def stocked_perks(
    settings: EconSettings,
    perks: tuple[str, ...],
    *,
    owned: set[str] | frozenset[str] = frozenset(),
    has_palette: bool = True,
) -> tuple[str, ...]:
    """The perks of ``perks`` this guild is actually selling.

    Lifted out of ``build_shop_embed`` so section visibility and the table can
    ask the same question — a section whose rows the embed would all drop must
    not get a page of its own, and that decision now happens before either.

    A switched-off perk leaves the table entirely, including the width
    calculation, so a hidden row can never pad the visible ones. The carve-out
    is a member still renting it out to their anniversary: billing them for a
    perk with no row and no ✅ anywhere in the shop is how you get a support
    ticket instead of a clean wind-down. ``has_palette`` is the same rule for
    the colour palette, whose product disappears when the guild's swatches do.
    """
    return tuple(
        p
        for p in perks
        if (p != "role_preset" or has_palette)
        and (perk_on_sale(settings, p) or p in owned)
    )


def shop_sections(
    settings: EconSettings,
    *,
    items: list[ItemView] | None = None,
    owned: set[str] | frozenset[str] = frozenset(),
    has_palette: bool = True,
) -> list[str]:
    """The sections this guild has anything to put in.

    An empty section gets no page at all rather than a page saying it is empty
    — a guild that never turned the voice lease on should not have members
    stepping through a blank shelf to reach the next one. That means the shop's
    shape differs per guild, which is the point: TGM sells no custom items, so
    it has no Specials section and never sees one.
    """
    out: list[str] = []
    if items:
        out.append(SECTION_SPECIALS)
    if stocked_perks(settings, SELF_PERKS, owned=owned, has_palette=has_palette):
        out.append(SECTION_COSMETICS)
    if stocked_perks(settings, ("voice_style",), owned=owned):
        out.append(SECTION_SERVER)
    if settings.shop_streak_shield_enabled or raffle_svc.raffle_enabled(settings):
        out.append(SECTION_GAMES)
    return out


def shop_pages(
    settings: EconSettings,
    *,
    items: list[ItemView] | None = None,
    owned: set[str] | frozenset[str] = frozenset(),
    has_palette: bool = True,
) -> list[tuple[str, int]]:
    """The shop's pages in order, as ``(section, index within that section)``.

    Only Specials runs to more than one page: a guild can define unlimited
    items, and ten to a page is what survives a phone. Every other section is
    a fixed handful of rows.

    Never empty. A guild with every switch off and nothing stocked still gets
    one page — the cosmetics section, rendering its empty table and the "shop
    is switched off" story — because a shop with no pages has nothing to show
    a member who opened it, and the arrows have nothing to step between.
    """
    pages: list[tuple[str, int]] = []
    for section in shop_sections(
        settings, items=items, owned=owned, has_palette=has_palette
    ):
        if section == SECTION_SPECIALS:
            pages += [
                (SECTION_SPECIALS, i)
                for i in range(store_page_count(items or []))
            ]
        else:
            pages.append((section, 0))
    return pages or [(SECTION_COSMETICS, 0)]


def page_captions(pages: list[tuple[str, int]], total_items: int = 0) -> list[str]:
    """What each page calls itself, for the caption between the arrows.

    Specials carries its span when it runs to more than one page — "Specials"
    twice over tells a member nothing about which half they are looking at —
    and drops it when it doesn't, because "🎁 Specials · 1–7" is worse than
    "🎁 Specials" with nothing to tell it apart from.
    """
    spread = sum(1 for section, _ in pages if section == SECTION_SPECIALS) > 1
    out: list[str] = []
    for section, index in pages:
        caption = SECTION_CAPTIONS[section]
        if section == SECTION_SPECIALS and spread:
            start = index * STORE_PAGE_SIZE + 1
            end = min(total_items, (index + 1) * STORE_PAGE_SIZE)
            caption += f" · {start}–{end}"
        out.append(caption)
    return out


def page_note(pages: list[tuple[str, int]], page: int) -> str:
    """The "Page 2 of 4 · " prefix, or "" for a one-page shop.

    Numbered over the whole book, not within a section: the arrows cross every
    seam, so a counter that restarted at one would read as the shop losing its
    place. The section itself is named by the embed's title and by the caption
    between the arrows, so it is not repeated here.
    """
    if len(pages) < 2:
        return ""
    return f"Page {page + 1} of {len(pages)} · "


def store_page_count(items: list[ItemView]) -> int:
    """Pages the storefront needs. Always at least one, so page 1/1 is valid."""
    return max(1, -(-len(items) // STORE_PAGE_SIZE))


def store_page(items: list[ItemView], page: int) -> list[ItemView]:
    """The slice shown on ``page`` (0-based), clamped into range.

    Clamped rather than trusted: an admin can delete items while a member has
    the storefront open, and page 3 of a store that just shrank to one page
    must show that page rather than an empty picker.
    """
    page = max(0, min(page, store_page_count(items) - 1))
    return items[page * STORE_PAGE_SIZE : (page + 1) * STORE_PAGE_SIZE]


def build_store_embed(
    settings: EconSettings,
    items: list[ItemView],
    accent: discord.Color | None,
    *,
    owned_item_ids: set[int] | frozenset[int] = frozenset(),
    page: int = 0,
    balance: int | None = None,
    note: str = "",
) -> discord.Embed:
    """The Server Store on its own, one page at a time.

    Split out from the perk shop's teaser section because a stocked store
    outgrows a preview: the combined embed can spare eight rows, and a guild
    with twenty items was showing less than half of what it sells to anyone
    who did not click through. The rows live in the description rather than a
    field — one list needs no heading, and the description's cap is four times
    a field's, so a page of ten long names cannot truncate.

    ``page`` is 0-based and clamped by ``store_page``. It does **not** number
    the footer: the shop is one flat book whose arrows cross into the perk
    ladder, so the counter is computed over every page by ``page_note`` and
    handed in as ``note`` — a store-local count would restart at the seam.
    """
    shown = store_page(items, page)

    header = "Everything this server sells, beyond the built-in perks."
    if balance is not None:
        header += f" You have {settings.currency_emoji} **{balance:,}**."
    body = "\n".join(_item_rows(settings, shown, owned_item_ids)) if shown else (
        "_Nothing on the shelves yet._"
    )
    embed = discord.Embed(
        title=SECTION_CAPTIONS[SECTION_SPECIALS],
        description=f"{header}\n\u200b\n{body}",
        color=accent,
    )
    if settings.currency_icon_url:
        embed.set_thumbnail(url=settings.currency_icon_url)
    embed.set_footer(text=f"{note}Pick one below to buy it.")
    return embed


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
    note: str = "",
    section: str | None = None,
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
    # "Weekly rentals" was safe while the perk ladder came first. It is a lie
    # standing over a Server Store of mostly one-off items, so the claim is
    # made by the section that can keep it rather than by the whole embed.
    if section == SECTION_GAMES:
        header = "Bought once — nothing here renews"
    elif section is None and items:
        header = "The server store, plus weekly perk rentals"
    else:
        header = "Weekly rentals · cancel any time"
    if balance is not None:
        header += f" · you have {settings.currency_emoji} **{balance:,}**"
    if panel:
        second = (
            "Tap **Open Shop** for your personal menu — buy, rent, customize "
            "and refund, all private to you."
        )
    elif section == SECTION_COSMETICS:
        second = "Green buttons customize what you've already rented."
    else:
        # The green-button line is about the perk studio; on a section with no
        # customise buttons it explains a control that isn't there.
        second = "Step between sections with the arrows below."
    description = header + "\n" + second + "\n​"
    embed = discord.Embed(
        title=SECTION_CAPTIONS.get(section or "", "🛍️ Perk Shop"),
        description=description,
        color=accent,
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

    def _want(name: str) -> bool:
        """Whether this embed carries ``name``'s rows.

        ``section=None`` is the channel panel, which is one poster for a whole
        shop and so carries everything; a section renders only its own, which
        is what makes the ephemeral shop a book rather than a scroll.
        """
        return section is None or section == name

    def _stocked(perks: tuple[str, ...]) -> tuple[str, ...]:
        return stocked_perks(
            settings, perks, owned=owned, has_palette=show_palette
        )

    tiers = [(name, _stocked(perks)) for name, perks in PERK_TIERS]
    table_perks: list[str] = list(_stocked(SELF_PERKS))
    # The Voice tier exists only while the guild sells the lease. Its price is
    # no longer the switch — a price of 0 means a free lease, not a hidden one
    # (see SHOP_TOGGLE_PERKS).
    voice = _stocked(("voice_style",))
    if voice:
        table_perks.append("voice_style")
        # Its own section now, so it only joins the tier list on the panel,
        # where every section shares one embed.
        if section is None:
            tiers.append(("Voice", voice))

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

    # The store leads. What a guild stocks itself is the part a member came
    # for and the part that changes; the perk ladder below is the same six
    # rows in every guild, and it was pushing the server's own goods to the
    # fourth field of five.
    if items and section is None:
        # Panel only. In the shop the store is the Specials section, with pages
        # of its own from ``build_store_embed`` — a preview here as well would
        # be the same list twice in one book.
        embed.add_field(
            name="Specials",
            value=_items_block(settings, items, owned_item_ids),
            inline=False,
        )
    for tier_name, perks in (tiers if _want(SECTION_COSMETICS) else []):
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
    if _want(SECTION_SERVER) and section is not None and voice:
        embed.add_field(
            name="Voice",
            value="\n".join(_line(p) for p in voice) + "\n​",
            inline=False,
        )
    if _want(SECTION_GAMES) and settings.shop_streak_shield_enabled:
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
    if _want(SECTION_GAMES) and raffle_svc.raffle_enabled(settings):
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
    # Gifting needs something to gift: with every perk switched off, the only
    # rows left are the store's own items, which `/bank gift` doesn't sell.
    if table_perks and _want(SECTION_COSMETICS):
        embed.add_field(
            name="For a Friend",
            value=(
                "🎁 Any perk above can be gifted at its listed price — "
                "you pay the weekly rent, they wear it. Send one with `/bank gift`."
            ),
            inline=False,
        )

    if section == SECTION_GAMES:
        # Neither of these renews, so the weekly-billing footer would be a lie
        # standing under a shield and a raffle ticket.
        embed.set_footer(text=f"{note}Bought outright — no weekly rent.")
    else:
        embed.set_footer(
            text=(
                # With a store above, "prices" is no longer only perks.
                f"{note}{'Perk prices' if items and section is None else 'Prices'} "
                "are per week, billed every 7 days. A short grace period covers "
                "a missed renewal."
            )
        )
    return embed
