"""The rentable-perk vocabulary — names, blurbs, glyphs, tiers and prices.

Every surface that says a perk's name reads it from here: the shop table,
``/bank gift``'s command choices, the wallet's rental lines, the refund
picker, the role-studio refusals. It lived inside the shop renderer for one
release, which made the wallet import the shop to learn a label — the tables
are the shop's *subject*, not its property.

Adding a perk means adding a row to each table here, and nowhere else in this
package — plus its ``shop_<perk>_enabled`` field and its ``SHOP_TOGGLE_PERKS``
entry, which live in ``economy_service`` because the settings dataclass is
there (this module imports it, not the other way round).

``economy/register.py`` keeps a separate label map on purpose: it carries
retired kinds (``gift_color``) so old ledger rows still render.
"""

from __future__ import annotations

from collections.abc import Iterable

from bot_modules.services.economy_service import SHOP_TOGGLE_PERKS, EconSettings

# Human labels for the rentable perks (shop rows, wallet field, DMs).
PERK_LABELS = {
    "role_color": "Custom Role Color",
    "role_name": "Custom Role Name",
    "role_icon": "Role Icon",
    "role_gradient": "Gradient Role",
    "role_holographic": "Holographic Role",
    "role_preset": "Palette Color",
    "voice_style": "Voice Style",
}
# The role perks a member rents for themselves, in shop display order. Every
# giftable perk (these + the voice-style lease) is gifted as the same perk
# kind rented with the friend as beneficiary (gift_color retired in 091).
SELF_PERKS = (
    "role_color", "role_name", "role_preset", "role_gradient",
    "role_holographic", "role_icon",
)
# Self-perks with no member-side customisation: renting IS the whole thing
# (holographic is a fixed Discord preset, not a colour the member picks), so
# these skip the "Set …" modal and post-rent button.
NO_CONFIG_PERKS = ("role_holographic",)
GIFTABLE_PERKS = (*SELF_PERKS, "voice_style")
# Feature-gated perks and the friendly reason shown when the gate is closed.
# ``role_preset`` joins them because a palette colour IS a two-colour fade — it
# needs the same ENHANCED_ROLE_COLORS support as the free-form gradient.
FEATURE_GATED = ("role_gradient", "role_holographic", "role_icon", "role_preset")

# Shop-table furniture. The full `PERK_LABELS` names are too wide for an
# aligned two-cell row, so the shop uses a short cell label plus a one-line
# blurb — most members have never seen a gradient role and can't price what
# they can't picture. Blurbs stay under ~27 chars so a row survives mobile.
PERK_SHORT = {
    "role_color": "Color",
    "role_name": "Name",
    "role_gradient": "Gradient",
    "role_holographic": "Holo",
    "role_icon": "Icon",
    "role_preset": "Palette",
    "voice_style": "Voice",
}
# Blurbs stay under ~15 chars: the shop row is one code cell of
# label + blurb, and anything wider pushes the price onto its own
# line on a phone-width embed.
PERK_BLURBS = {
    "role_color": "any solid color",
    "role_name": "nickname + role",
    "role_gradient": "two-color fade",
    "role_holographic": "shimmer preset",
    "role_icon": "badge by name",
    "role_preset": "curated fade",
    "voice_style": "your voice room",
}
PERK_EMOJI = {
    "role_color": "🎨",
    "role_name": "✨",
    "role_gradient": "🌈",
    "role_holographic": "🪩",
    "role_icon": "🖼️",
    "role_preset": "🖌️",
    "voice_style": "🎙️",
}
# Self-perks grouped into a price ladder — cheap everyday tweaks first, the
# showy ones second — so the shop reads as tiers to climb rather than a flat
# spreadsheet. Rows sort by price inside each tier at render time, since
# prices are guild-configurable and can reorder.
PERK_TIERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Essentials", ("role_name", "role_color")),
    ("Signature", ("role_preset", "role_gradient", "role_icon", "role_holographic")),
)


def perk_price(settings: EconSettings, perk: str) -> int:
    return int(getattr(settings, f"price_{perk}"))


def perk_on_sale(settings: EconSettings, perk: str) -> bool:
    """Whether this guild currently sells ``perk`` at all.

    The admin checkbox on the Shop & Perks page, and the only thing that
    answers this question — a price of 0 means free, not off (see
    ``SHOP_TOGGLE_PERKS``). Anything outside that tuple is not a switchable
    shop line (``custom_item`` carries its own per-item ``enabled`` column,
    ``emoji`` is priced dark) and is reported as on sale, so this can be
    called on any perk string without the caller pre-filtering.
    """
    if perk not in SHOP_TOGGLE_PERKS:
        return True
    return bool(getattr(settings, f"shop_{perk}_enabled"))


def perks_on_sale(settings: EconSettings, perks: Iterable[str]) -> tuple[str, ...]:
    """``perks`` filtered to the ones this guild sells, order preserved."""
    return tuple(p for p in perks if perk_on_sale(settings, p))


# What each role-studio setter says when the member hasn't rented its perk.
# The wording differs per perk (colour can also arrive as a gift) and is
# asserted verbatim by the setter-refusal tests — keep the strings as they are.
PERK_REFUSAL = {
    "role_name": "❌ Rent the **Custom Role Name** perk first (/bank shop).",
    "role_color": (
        "❌ Rent the **Custom Role Color** perk or get one gifted (/bank shop)."
    ),
    "role_gradient": "❌ Rent the **Gradient Role** perk first (/bank shop).",
    "role_icon": "❌ Rent the **Role Icon** perk first (/bank shop).",
    "role_preset": "❌ Rent the **Palette Color** perk first (/bank shop).",
}
# PERK_REFUSAL covers five of the six SELF_PERKS — role_holographic has no
# setter to refuse from, and voice_style isn't a self-perk at all. A setter
# added for either must degrade to a polite refusal, not a KeyError.
PERK_REFUSAL_FALLBACK = "❌ Rent that perk first (/bank shop)."

# Short button labels for the customise flows (the perk label is on the row).
CUSTOMISE_LABELS = {
    "role_color": "Set Color",
    "role_name": "Set Name",
    "role_gradient": "Set Gradient",
    "role_icon": "Set Icon",
}
