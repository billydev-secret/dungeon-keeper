"""Economy — the ``/bank`` command surface (wallet view + mod grants).

Thin cog over ``bot_modules.services.economy_service``: it loads per-guild
``econ_`` settings on each interaction (cheap KV reads, no cache for stage 0),
resolves the branded currency naming, and renders the accent-colored embeds.
See docs/economy_spec.md for the feature design.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import sqlite3
import time
from typing import TYPE_CHECKING, NamedTuple, cast

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot_modules.core.branding import resolve_accent_color
from bot_modules.core.db_utils import get_tz_offset_hours
from bot_modules.core.sticky import PanelContent, StickyPanel
from bot_modules.economy.guide import (
    GuideView,
    build_guide_embed,
)
from bot_modules.economy.leaderboard import (
    build_leaderboard_embed,
    collect_leaderboard_data,
)
from bot_modules.economy import quests as quest_rules
from bot_modules.economy.logic import local_day_for
from bot_modules.economy.view_helpers import (
    unit as _unit,
)
from bot_modules.economy.wallet import build_wallet_embed
from bot_modules.economy.perks import (
    CUSTOMISE_LABELS as _CUSTOMISE_LABELS,
    FEATURE_GATED,
    GIFTABLE_PERKS,
    NO_CONFIG_PERKS,
    PERK_EMOJI,
    PERK_LABELS,
    PERK_REFUSAL as _PERK_REFUSAL,
    PERK_REFUSAL_FALLBACK as _PERK_REFUSAL_FALLBACK,
    PERK_SHORT,
    SELF_PERKS,
    perk_price,
)
from bot_modules.economy.shop import build_shop_embed
from bot_modules.economy.perk_actions import (
    apply_role_perks,
    feature_gate_ok,
    find_color_clash,
    parse_hex_color,
    revoke_role_perks,
)
from bot_modules.economy.quest_views import (
    build_quest_board_embed,
    QuestApproveButton,
    QuestBoardView,
    QuestClaimView,
    QuestDenyButton,
    can_manage_economy,
    post_signoff_card,
)
from bot_modules.economy.bounty_views import (
    BountyAwardButton,
    BountyCancelButton,
    BountyChipInButton,
    BountyHubChipButton,
    BountyHubPostButton,
    build_bounty_hub_panel,
    post_bounty_card,
)
from bot_modules.economy.auction_views import (
    AuctionBidButton,
    build_auction_panel,
    cancel_open_auction,
    end_open_auction,
    settle_and_announce,
    start_auction,
)
from bot_modules.services.economy_auction_service import (
    attach_card_to_latest,
    card_ids,
    open_auction_guild_ids,
)
from bot_modules.economy.pin_views import (
    PinApproveButton,
    PinDenyButton,
)
from bot_modules.economy.pin_views import (
    post_review_card as post_pin_review_card,
)
from bot_modules.economy.sponsor_views import (
    SponsorApproveButton,
    SponsorDenyButton,
    post_review_card,
)
from bot_modules.services.economy_bounty_service import (
    MAX_DESC_LEN,
    MAX_TITLE_LEN,
    bounty_enabled,
    create_bounty,
)
from bot_modules.services.economy_pin_service import (
    MAX_PIN_LEN,
    MIN_PIN_LEN,
    pin_enabled,
    submit_pin,
)
from bot_modules.services.economy_qotd_sponsor_service import (
    attach_qotd,
    claim_next_approved,
    release_claim,
    sponsor_enabled,
    submit_sponsor,
)
from bot_modules.services.economy_quests_service import (
    fire_trigger_inline,
    fire_trigger_quests,
    load_member_quest_board,
    reroll_quote,
)
from bot_modules.services.economy_icon_catalog_service import (
    catalog_price_range,
    get_catalog_icon,
    list_catalog,
)
from bot_modules.services.economy_rentals_service import (
    RentalRefund,
    cancel_all_for_member,
    entitlements,
    get_live_role_icon_rental,
    get_refundable_rental,
    list_member_rentals,
    list_refundable_rentals,
    refund_rental,
    rent_perk,
    set_rental_catalog_icon,
    upsert_personal_role,
)
from bot_modules.services.economy_loop import revoke_perk_effect
from bot_modules.economy.rentals import prorated_refund
from bot_modules.services import economy_emoji_service as emoji_svc
from bot_modules.services import economy_wager_service as wager_svc
from bot_modules.services import economy_photo_service as photo_svc
from bot_modules.services import economy_quests_service as quests_svc
from bot_modules.services import economy_raffle_service as raffle_svc
from bot_modules.services.economy_service import (
    EconSettings,
    apply_credit,
    create_qotd,
    get_balance,
    get_ledger,
    get_notify_muted,
    get_streak_shield_price,
    get_streak_shield_status,
    get_streak_shields,
    load_econ_settings,
    notify_member,
    purchase_streak_shield,
    refund_streak_shield,
    save_econ_settings,
    set_notify_muted,
    transfer_currency,
)
from bot_modules.services.message_store import get_known_users_bulk
from bot_modules.services.quote_renderer import THEMES, render_quote_card
from bot_modules.services.voice_master_service import (
    list_name_blocklist,
    name_is_blocked,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.economy")

_DISABLED_MSG = "The economy isn't enabled on this server yet."
# Shared by both bring-your-own upload paths (emoji modal, /bank role icon)
# when the member's rental is a curated catalog icon rather than a custom one.
_CATALOG_LOCKED_MSG = (
    "❌ You're renting a curated catalog icon — to upload your own instead, "
    "pick **Custom** from the shop's icon picker first (/bank shop)."
)
_QOTD_CARD_FILENAME = "qotd.png"
_NO_ROLE_ICONS_MSG = "❌ This server doesn't support role icons right now."

# Transfers above this need an explicit confirm step (spec §5, "over 100").
_PAY_CONFIRM_THRESHOLD = 100
_MAX_ROLE_NAME_LEN = 32
_MAX_MEMO_LEN = 100
_MAX_ICON_BYTES = 256 * 1024
# Emoji upload types Discord accepts, mapped to whether they're animated.
_EMOJI_CONTENT_TYPES = {
    "image/png": False, "image/jpeg": False,
    "image/webp": False, "image/gif": True,
}

# The custom-name perk renames the member's personal role AND sets their
# server nickname to match — "call yourself anything" has to actually change
# the name people see, not just a cosmetic role label. The nickname is
# best-effort (a bot can't outrank the hierarchy or rename the guild owner),
# so the confirmation spells out which parts landed.
_NICK_FORBIDDEN = (
    "I couldn't set your server nickname to match — my role needs to sit "
    "above yours with the Manage Nicknames permission (and Discord never "
    "lets a bot rename the server owner)."
)
_NICK_FAILED = "Your server nickname didn't take, though — try again in a moment."


def _custom_name_confirmation(text: str, *, nick_ok: bool, nick_reason: str = "") -> str:
    """The ephemeral reply after a member sets their custom name."""
    if nick_ok:
        return (
            f"Your name is now **{text}** — it's your server nickname and your "
            "personal role name."
        )
    base = f"Your role name is now **{text}**."
    return f"{base} {nick_reason}" if nick_reason else base


def _icon_store_path(db_path, guild_id: int, user_id: int):
    """Managed on-disk path for an uploaded personal-role icon (per guild/member)."""
    directory = db_path.parent / "econ_role_icons" / str(guild_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{user_id}.png"


# The pasted form of a custom emoji: <:name:id> (or <a:name:id> when animated).
_CUSTOM_EMOJI_RE = re.compile(r"<(a?):([A-Za-z0-9_~]+):(\d+)>$")


def _resolve_guild_emoji(guild: discord.Guild, raw: str) -> discord.Emoji | None:
    """Resolve member input to one of *this guild's* custom emojis.

    Accepts the pasted form ``<:name:id>`` (matched by id) or a typed
    ``:name:`` / bare name (matched by name). Unicode emojis and custom
    emojis from other servers resolve to ``None`` — role icons only take
    images this server has already approved as emojis.
    """
    raw = raw.strip()
    m = _CUSTOM_EMOJI_RE.match(raw)
    if m:
        return discord.utils.get(guild.emojis, id=int(m.group(3)))
    name = raw.strip(":").strip()
    if not name:
        return None
    return discord.utils.get(guild.emojis, name=name)


async def _resolve_qotd_image(guild: discord.Guild, bot: Bot) -> bytes | None:
    """Bytes for the QOTD card background — the server icon, bot avatar fallback."""
    if guild.icon is not None:
        try:
            return await guild.icon.replace(size=512).read()
        except discord.HTTPException:
            log.warning("qotd: failed to read guild icon for %s", guild.id)
    user = bot.user
    if user is not None:
        try:
            return await user.display_avatar.with_size(512).read()
        except discord.HTTPException:
            log.warning("qotd: failed to read bot avatar")
    return None


# Trigger-quest cache staleness bound: a dashboard edit takes effect on the
# next message after at most this many seconds.
_TRIGGER_CACHE_TTL = 60.0


class _ShopContext(NamedTuple):
    """Everything /bank shop renders from, read on a single connection."""

    settings: EconSettings
    owned: set[str]
    balance: int
    icon_range: tuple[int, int, int] | None
    refundable: list[dict]
    shields_held: int
    shield_price: int


_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".avif")


def _has_image_attachment(message: discord.Message) -> bool:
    """True when any attachment is an image.

    Content-type first; filename extension as the fallback for the uploads
    Discord serves without one (some mobile clients).
    """
    for att in message.attachments:
        ctype = att.content_type or ""
        if ctype.startswith("image/"):
            return True
        if not ctype and att.filename.lower().endswith(_IMAGE_EXTENSIONS):
            return True
    return False


def _can_grant(user: discord.Member, settings: EconSettings) -> bool:
    """True for server admins or holders of the configured manager role.

    Delegates to the canonical gate in ``quest_views`` so the grant command
    and the sign-off buttons enforce one rule.
    """
    return can_manage_economy(user, settings)


def _clean_memo(memo: str | None) -> str | None:
    """Collapse a pay memo to a single trimmed line, or None if it's empty.

    Newlines would break the one-line-per-row wallet and ledger renders, so
    they collapse to spaces. The Range on the command caps length client-side;
    the truncation here is the server-side guard.
    """
    if not memo:
        return None
    cleaned = " ".join(memo.split())
    if not cleaned:
        return None
    return cleaned[:_MAX_MEMO_LEN]


class _MemberScopedView(discord.ui.View):
    """A view usable only by the member it was opened for.

    Shared base for the shop-adjacent pickers (``_ShopView``,
    ``_IconCatalogView``, ``_RefundPickerView``) and for the confirm gates,
    which were each carrying an identical ``interaction_check``. Subclasses
    differ only in what the refusal says and how long they live, so those are
    a class attribute and a constructor argument rather than a re-implementation.
    """

    SCOPE_DENIAL = "❌ Open your own shop with /bank shop."

    def __init__(self, user_id: int, *, timeout: float | None = 120) -> None:
        super().__init__(timeout=timeout)
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                self.SCOPE_DENIAL, ephemeral=True
            )
            return False
        return True


class _PayConfirmView(_MemberScopedView):
    """Ephemeral Confirm/Cancel gate for a transfer over the threshold."""

    SCOPE_DENIAL = "❌ This confirmation isn't yours."

    def __init__(
        self,
        cog: EconomyCog,
        settings: EconSettings,
        guild: discord.Guild,
        sender: discord.Member,
        recipient: discord.Member,
        amount: int,
        memo: str | None = None,
    ) -> None:
        super().__init__(sender.id, timeout=60)
        self.cog = cog
        self.settings = settings
        self.guild = guild
        self.sender = sender
        self.recipient = recipient
        self.amount = amount
        self.memo = memo

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def _confirm(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        self.stop()
        await self.cog.finalize_pay(
            interaction, self.settings, self.guild, self.sender, self.recipient,
            self.amount, memo=self.memo, via_confirm=True,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def _cancel(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="Payment cancelled.", embed=None, view=None
        )


class _GiftConfirmView(_MemberScopedView):
    """Ephemeral Confirm/Cancel gate for gifting a perk the friend already has.

    The rental would stack silently (their role already shows the perk), so
    the double-spend has to be an explicit choice, mirroring _PayConfirmView.
    """

    SCOPE_DENIAL = "❌ This confirmation isn't yours."

    def __init__(
        self,
        cog: EconomyCog,
        settings: EconSettings,
        guild: discord.Guild,
        gifter: discord.Member,
        member: discord.Member,
        perk: str,
    ) -> None:
        super().__init__(gifter.id, timeout=60)
        self.cog = cog
        self.settings = settings
        self.guild = guild
        self.gifter = gifter
        self.member = member
        self.perk = perk

    @discord.ui.button(label="Gift Anyway", style=discord.ButtonStyle.success)
    async def _confirm(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        self.stop()
        await self.cog.finalize_gift(
            interaction, self.settings, self.guild, self.gifter, self.member,
            self.perk, via_confirm=True,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def _cancel(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="Gift cancelled.", view=None
        )


class _RaffleBuyModal(discord.ui.Modal, title="Weekly Raffle Tickets"):
    quantity = discord.ui.TextInput(
        label="How many tickets?", min_length=1, max_length=3, placeholder="1"
    )

    def __init__(self, cog: EconomyCog, settings: EconSettings) -> None:
        super().__init__()
        self.cog = cog
        self.settings = settings

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.do_buy_raffle_tickets(
            interaction, self.settings, str(self.quantity.value)
        )


class _EmojiCancelView(_MemberScopedView):
    """Cancel button on the bare /bank emoji status reply (pending only)."""

    SCOPE_DENIAL = "❌ This isn't your submission."

    def __init__(self, cog: EconomyCog, submission_id: int, user_id: int) -> None:
        super().__init__(user_id, timeout=60)
        self.cog = cog
        self.submission_id = submission_id

    @discord.ui.button(label="Cancel & Refund", style=discord.ButtonStyle.danger)
    async def _cancel(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        self.stop()
        await self.cog.do_cancel_emoji(interaction, self.submission_id)


class _RoleNameModal(discord.ui.Modal, title="Set Your Custom Name"):
    text = discord.ui.TextInput(
        label="Name",
        min_length=1,
        max_length=_MAX_ROLE_NAME_LEN,
        placeholder="Becomes your server nickname + your role name",
    )

    def __init__(self, cog: EconomyCog) -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.set_role_name(interaction, str(self.text.value))


class _RoleColorModal(discord.ui.Modal, title="Custom Role Color"):
    hex_value = discord.ui.TextInput(
        label="Hex color", min_length=3, max_length=9, placeholder="#7B2FF7"
    )

    def __init__(self, cog: EconomyCog) -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.set_role_color(interaction, str(self.hex_value.value))


class _RoleGradientModal(discord.ui.Modal, title="Gradient Role"):
    hex1 = discord.ui.TextInput(
        label="First hex color", min_length=3, max_length=9, placeholder="#7B2FF7"
    )
    hex2 = discord.ui.TextInput(
        label="Second hex color", min_length=3, max_length=9, placeholder="#2FF7B2"
    )

    def __init__(self, cog: EconomyCog) -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.set_role_gradient(
            interaction, str(self.hex1.value), str(self.hex2.value)
        )


class _RoleIconModal(discord.ui.Modal, title="Role Icon"):
    emoji = discord.ui.TextInput(
        label="Server emoji",
        min_length=1,
        max_length=100,
        placeholder=":emoji_name: — a custom emoji from this server",
    )

    def __init__(self, cog: EconomyCog) -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.set_role_icon_emoji(interaction, str(self.emoji.value))


class _PinSubmitModal(discord.ui.Modal, title="Pin a Message"):
    """The paragraph a member pays to pin; a mod reviews it before it goes up."""

    text: discord.ui.TextInput = discord.ui.TextInput(
        label="Your message",
        style=discord.TextStyle.paragraph,
        min_length=MIN_PIN_LEN,
        max_length=MAX_PIN_LEN,
        placeholder="Keep it short and fun — a mod approves it before it pins.",
    )

    def __init__(self, cog: EconomyCog) -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.do_pin_submit(interaction, str(self.text.value))


class _BountyPostModal(discord.ui.Modal, title="Post a Bounty"):
    """A freeform task plus the poster's opening stake into the pot."""

    b_title: discord.ui.TextInput = discord.ui.TextInput(
        label="Bounty",
        max_length=MAX_TITLE_LEN,
        placeholder="What needs doing? e.g. 'Draw our mascot as a knight'",
    )
    description: discord.ui.TextInput = discord.ui.TextInput(
        label="Details (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=MAX_DESC_LEN,
        placeholder="Any rules, deadline, or what 'done' looks like.",
    )
    stake: discord.ui.TextInput = discord.ui.TextInput(
        label="Your opening stake",
        max_length=12,
        placeholder="Coins you put in to start the pot",
    )

    def __init__(self, cog: EconomyCog) -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.do_bounty_post(
            interaction,
            str(self.b_title.value),
            str(self.description.value or ""),
            str(self.stake.value),
        )


# Which modal customises which perk; a gifted perk uses the same modal as a
# self-rented one (entitlements are beneficiary-based, so the rows match).
_CFG_MODALS = {
    "role_name": _RoleNameModal,
    "role_color": _RoleColorModal,
    "role_gradient": _RoleGradientModal,
    "role_icon": _RoleIconModal,
}



# Discord caps a select at 25 options; the last slot is the bring-your-own
# Custom entry, so a larger catalog shows its first 24 (by sort order) and
# tells the member the list was trimmed.
_ICON_SELECT_LIMIT = 24


class _IconCatalogSelect(discord.ui.Select):
    """A picker of curated role icons; choosing one rents or switches to it.

    The final option is always **Custom** — the bring-your-own icon at the
    flat ``price_role_icon`` — so stocking a catalog never takes the classic
    upload-your-own rental off the shelf.
    """

    def __init__(
        self,
        cog: EconomyCog,
        settings: EconSettings,
        guild: discord.Guild,
        icons: list[dict],
    ) -> None:
        options = [
            discord.SelectOption(
                label=str(icon["name"])[:100],
                value=str(icon["id"]),
                description=(
                    f"{settings.currency_emoji} {int(icon['price']):,} / week"
                )[:100],
            )
            for icon in icons[:_ICON_SELECT_LIMIT]
        ]
        options.append(
            discord.SelectOption(
                label="Custom — upload your own",
                value="custom",
                emoji="🎨",
                description=(
                    f"{settings.currency_emoji} "
                    f"{settings.price_role_icon:,} / week — any image or emoji"
                )[:100],
            )
        )
        super().__init__(
            placeholder="Choose a role icon…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.cog = cog
        self.settings = settings
        self.guild = guild

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.values[0] == "custom":
            await self.cog.pick_custom_icon(interaction, self.settings, self.guild)
            return
        await self.cog.pick_catalog_icon(
            interaction, self.guild, int(self.values[0])
        )


class _IconCatalogView(_MemberScopedView):
    """Ephemeral catalog picker, scoped to the member who opened the shop."""

    def __init__(
        self,
        cog: EconomyCog,
        settings: EconSettings,
        guild: discord.Guild,
        user_id: int,
        icons: list[dict],
    ) -> None:
        super().__init__(user_id)
        self.add_item(_IconCatalogSelect(cog, settings, guild, icons))


class _RefundSelect(discord.ui.Select):
    """Picker of the member's cancellable rentals + held shield.

    Option values are ``"rental:{id}"`` or the literal ``"shield"``; choosing
    one moves to a confirm step rather than acting immediately (a refund ends
    the perk right away, so it gets the same Confirm/Back gate as a pay/gift
    double-spend).
    """

    def __init__(
        self,
        cog: EconomyCog,
        settings: EconSettings,
        guild: discord.Guild,
        rentals: list[dict],
        shield_price: int,
    ) -> None:
        now = time.time()
        options: list[discord.SelectOption] = []
        for r in rentals:
            label = PERK_LABELS.get(str(r["perk"]), str(r["perk"]))
            if r["state"] == "active":
                preview = prorated_refund(
                    int(r["price"]), float(r["next_bill_at"]), now
                )
                desc = f"{settings.currency_emoji} {preview:,} back — ends now"
            else:
                desc = "Already in grace — no refund, ends now"
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=f"rental:{int(r['id'])}",
                    description=desc[:100],
                )
            )
        if shield_price > 0:
            options.append(
                discord.SelectOption(
                    label="Streak Shield",
                    value="shield",
                    description=f"{settings.currency_emoji} {shield_price:,} back"[:100],
                )
            )
        super().__init__(
            placeholder="Choose what to cancel…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.cog = cog
        self.settings = settings
        self.guild = guild

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.show_refund_confirm(
            interaction, self.settings, self.guild, self.values[0]
        )


class _RefundPickerView(_MemberScopedView):
    """Ephemeral picker of the member's cancellable rentals + held shield."""

    def __init__(
        self,
        cog: EconomyCog,
        settings: EconSettings,
        guild: discord.Guild,
        user_id: int,
        rentals: list[dict],
        shield_price: int,
    ) -> None:
        super().__init__(user_id)
        self.add_item(_RefundSelect(cog, settings, guild, rentals, shield_price))


class _RefundConfirmView(_MemberScopedView):
    """Ephemeral Confirm/Back gate before a refund actually runs.

    Danger-styled (unlike _PayConfirmView's plain success Confirm) since this
    ends a live perk immediately, not just moves money between two members.
    """

    SCOPE_DENIAL = "❌ This confirmation isn't yours."

    def __init__(
        self,
        cog: EconomyCog,
        settings: EconSettings,
        guild: discord.Guild,
        user_id: int,
        target: str,
    ) -> None:
        super().__init__(user_id, timeout=60)
        self.cog = cog
        self.settings = settings
        self.guild = guild
        self.target = target

    @discord.ui.button(label="Yes, Cancel & Refund", style=discord.ButtonStyle.danger)
    async def _confirm(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        self.stop()
        await self.cog.finalize_refund(
            interaction, self.settings, self.guild, self.target
        )

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary)
    async def _back(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="Nothing changed.", embed=None, view=None
        )


class _ShopView(_MemberScopedView):
    """One button per self-perk: Rent when unowned, a customise modal when owned.

    Feature-gated rows are disabled either way. ``owned`` is the viewer's
    beneficiary-based entitlements, so a gifted perk shows its customise
    button exactly like a self-rented one.
    """

    def __init__(
        self,
        cog: EconomyCog,
        settings: EconSettings,
        guild: discord.Guild,
        user_id: int,
        gated: set[str],
        owned: set[str],
        has_catalog: bool = False,
        shields_held: int = 0,
        refundable: list[dict] | None = None,
        shield_price: int = 0,
    ) -> None:
        super().__init__(user_id)
        self.cog = cog
        self.settings = settings
        self.guild = guild
        self.refundable = refundable or []
        self.shield_price = shield_price
        for perk in SELF_PERKS:
            if perk == "role_icon" and has_catalog:
                # A curated catalog replaces the rent/customise buttons with a
                # single picker — renting and restyling both happen by choosing
                # an icon (each carries its own price; the bring-your-own
                # custom icon rides the picker's last slot at the flat price).
                button = discord.ui.Button(
                    label=(
                        "Change icon" if perk in owned else "🖼️ Browse icons"
                    ),
                    # The catalog opener is the odd one out — it browses rather
                    # than rents — so it takes the only secondary style in the
                    # row instead of blending into the blurple slabs.
                    style=(
                        discord.ButtonStyle.success
                        if perk in owned
                        else discord.ButtonStyle.secondary
                    ),
                    disabled=perk in gated,
                    custom_id="econ_shop_icons",
                )
                button.callback = self._make_icons_callback()
                self.add_item(button)
                continue
            if perk in owned and perk in NO_CONFIG_PERKS:
                # Nothing to customise (a fixed preset) — show it as active,
                # like the voice lease's "Leased" chip, rather than a dead
                # "Set …" button that opens an empty modal.
                button = discord.ui.Button(
                    label=f"{PERK_EMOJI[perk]} Active",
                    style=discord.ButtonStyle.success,
                    disabled=True,
                    custom_id=f"econ_shop_active:{perk}",
                )
            elif perk in owned:
                button = discord.ui.Button(
                    label=_CUSTOMISE_LABELS[perk],
                    style=discord.ButtonStyle.success,
                    disabled=perk in gated,
                    custom_id=f"econ_shop_cfg:{perk}",
                )
                button.callback = self._make_cfg_callback(perk)
            else:
                # No price in the label — the table above carries it, and a
                # short emoji-led label keeps the row scannable.
                button = discord.ui.Button(
                    label=f"{PERK_EMOJI[perk]} {PERK_SHORT[perk]}",
                    style=discord.ButtonStyle.primary,
                    disabled=perk in gated,
                    custom_id=f"econ_shop_rent:{perk}",
                )
                button.callback = self._make_rent_callback(perk)
            self.add_item(button)
        if settings.price_voice_style > 0:
            if "voice_style" in owned:
                button = discord.ui.Button(
                    label="🎙️ Leased",
                    style=discord.ButtonStyle.success,
                    disabled=True,  # customization lives on the VM panel
                    custom_id="econ_shop_rent:voice_style",
                )
            else:
                button = discord.ui.Button(
                    label="🎙️ Voice",
                    style=discord.ButtonStyle.primary,
                    custom_id="econ_shop_rent:voice_style",
                )
                button.callback = self._make_rent_callback("voice_style")
            self.add_item(button)
        if raffle_svc.raffle_enabled(settings):
            button = discord.ui.Button(
                label="🎟️ Tickets",
                style=discord.ButtonStyle.secondary,
                custom_id="econ_shop_raffle",
            )
            button.callback = self._make_raffle_callback()
            self.add_item(button)
        if settings.price_streak_shield > 0:
            # A held shield stays visible (green, disabled) so the cap reads
            # as "you have one", not as the button being broken.
            held = shields_held > 0
            button = discord.ui.Button(
                label="🛡️ Shield Held" if held else "🛡️ Shield",
                style=(
                    discord.ButtonStyle.success
                    if held
                    else discord.ButtonStyle.secondary
                ),
                disabled=held,
                custom_id="econ_shop_shield",
            )
            button.callback = self._make_shield_callback()
            self.add_item(button)
        if self.refundable or self.shield_price > 0:
            # One entry point for everything cancellable, rather than a
            # second button per owned row — the picker underneath handles
            # however many the member happens to hold.
            button = discord.ui.Button(
                label="↩️ Cancel & Refund",
                style=discord.ButtonStyle.secondary,
                custom_id="econ_shop_refund",
            )
            button.callback = self._make_refund_callback()
            self.add_item(button)

    def _make_rent_callback(self, perk: str):
        async def _cb(interaction: discord.Interaction) -> None:
            await self.cog.do_rent(interaction, self.settings, self.guild, perk)

        return _cb

    def _make_cfg_callback(self, perk: str):
        async def _cb(interaction: discord.Interaction) -> None:
            await self.cog.open_customise_modal(interaction, perk)

        return _cb

    def _make_icons_callback(self):
        async def _cb(interaction: discord.Interaction) -> None:
            await self.cog.open_icon_catalog(interaction, self.settings, self.guild)

        return _cb

    def _make_shield_callback(self):
        async def _cb(interaction: discord.Interaction) -> None:
            await self.cog.do_buy_shield(interaction, self.settings, self.guild)

        return _cb

    def _make_raffle_callback(self):
        async def _cb(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(
                _RaffleBuyModal(self.cog, self.settings)
            )

        return _cb

    def _make_refund_callback(self):
        async def _cb(interaction: discord.Interaction) -> None:
            await self.cog.open_refund_picker(
                interaction, self.settings, self.guild, self.refundable,
                self.shield_price,
            )

        return _cb


class _PostRentView(discord.ui.View):
    """Single customise button attached to a fresh rental's confirmation."""

    def __init__(self, cog: EconomyCog, perk: str) -> None:
        super().__init__(timeout=300)
        button = discord.ui.Button(
            label=_CUSTOMISE_LABELS[perk],
            style=discord.ButtonStyle.success,
            custom_id=f"econ_rent_cfg:{perk}",
        )

        async def _cb(interaction: discord.Interaction) -> None:
            await cog.open_customise_modal(interaction, perk)

        button.callback = _cb
        self.add_item(button)


async def _rent_perk_flow(
    interaction: discord.Interaction,
    cog: EconomyCog,
    settings: EconSettings,
    guild: discord.Guild,
    perk: str,
) -> None:
    """Rent a self-perk from a shop button, then project the role.

    Every reply is ephemeral to the clicker, and a successful rent carries the
    perk's customise button so styling happens without leaving the message.
    """
    ctx = cog.ctx
    user_id = interaction.user.id

    def _rent() -> None:
        with ctx.open_db() as conn:
            rent_perk(conn, settings, guild.id, user_id, perk, now=time.time())

    try:
        await asyncio.to_thread(_rent)
    except ValueError as exc:
        msg = str(exc)
        if "insufficient" in msg:
            text = await cog._short_funds_text(
                settings, guild.id, user_id, perk_price(settings, perk)
            )
        elif "already rented" in msg:
            text = "❌ You're already renting that perk."
        else:
            text = "❌ That perk isn't available."
        await interaction.response.send_message(text, ephemeral=True)
        return

    if perk == "voice_style":
        # No personal role to project and no customise modal — the perk's
        # controls ARE Voice Control's rename/limit, live again from now on.
        await interaction.response.send_message(
            "Rented **Voice Style**! Renaming and sizing your voice channel "
            "are unlocked — your saved name and limit apply the next time "
            "you spin one up.",
            ephemeral=True,
        )
        return
    await apply_role_perks(cog.bot, ctx.db_path, guild.id, user_id)
    if perk in NO_CONFIG_PERKS:
        # A fixed preset — there's nothing to set, so it's live the moment the
        # role projects. No customise button (an empty modal would confuse).
        await interaction.response.send_message(
            f"Rented **{PERK_LABELS[perk]}**! Your personal role now wears "
            "Discord's holographic shimmer — no setup needed.",
            ephemeral=True,
        )
        return
    note = (
        " (For an image icon, upload one with `/bank role icon`.)"
        if perk == "role_icon"
        else ""
    )
    await interaction.response.send_message(
        f"Rented **{PERK_LABELS[perk]}**! Set it up right here:{note}",
        view=_PostRentView(cog, perk),
        ephemeral=True,
    )


async def _open_shop_from_panel(interaction: discord.Interaction) -> None:
    """Route a persistent panel click to the clicker's personal shop menu.

    Shared by the panel's Open Shop button and legacy per-perk rent buttons.
    The cog is resolved at click time — the panel outlives reloads, so
    nothing rendered at post time is trusted at click time.
    """
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ The shop only works in a server.", ephemeral=True
        )
        return
    bot = cast("Bot", interaction.client)
    cog = cast("EconomyCog | None", bot.get_cog("EconomyCog"))
    if cog is None:  # cog unloaded mid-flight; the panel button outlives it
        await interaction.response.send_message(
            "❌ The shop isn't available right now.", ephemeral=True
        )
        return
    await cog.open_personal_shop(interaction)


class ShopPanelView(discord.ui.View):
    """The channel shop panel's single persistent Open Shop button.

    Carries no per-message state, so it's a static-custom_id view (the
    GuideView pattern) re-registered in ``cog_load`` rather than a
    DynamicItem. The button serves the clicker's exact `/bank shop` menu as
    an ephemeral reply — one shop menu, so the panel can't drift from it.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🛍️ Open Shop",
        style=discord.ButtonStyle.primary,
        custom_id="econ_shop_open",
    )
    async def _open(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await _open_shop_from_panel(interaction)


class ShopRentButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_shop_panel:(?P<perk>[a-z_]+)"),
):
    """Legacy per-perk panel button; now a launcher for the personal shop.

    Panels posted before the single Open Shop button carried one of these
    per perk. They stay registered so stale panels keep working across
    restarts, but every click now opens the clicker's personal shop menu
    (where renting that perk is one more click) instead of renting directly
    — one menu, no drift. A `/bank post-shop` refresh replaces them.
    """

    def __init__(
        self,
        perk: str,
        *,
        label: str | None = None,
        style: discord.ButtonStyle = discord.ButtonStyle.primary,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            discord.ui.Button(
                label=label or f"Rent {PERK_LABELS.get(perk, perk)}",
                style=style,
                disabled=disabled,
                custom_id=f"econ_shop_panel:{perk}",
            )
        )
        self.perk = perk

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> ShopRentButton:
        return cls(str(match["perk"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _open_shop_from_panel(interaction)


class EconomyCog(commands.Cog):
    bank = app_commands.Group(
        name="bank",
        description="Wallet and currency commands.",
        guild_only=True,
    )
    role = app_commands.Group(
        name="role",
        description="Personal role extras (customize your perks in /bank shop).",
        parent=bank,
    )
    auction = app_commands.Group(
        name="auction",
        description="Run a live auction (staff only).",
        parent=bank,
    )

    @auction.command(name="start", description="Start a live auction (staff only).")
    @app_commands.describe(
        title="What's being auctioned (short name)",
        prize="What the winner gets — you hand it over yourself",
        hours="How long the auction runs, in hours",
    )
    async def auction_start(
        self,
        interaction: discord.Interaction,
        title: str,
        prize: str,
        hours: app_commands.Range[float, 1, 168],
    ) -> None:
        await start_auction(
            interaction, title=title, prize=prize, duration_hours=float(hours)
        )

    @auction.command(name="cancel", description="Cancel the live auction and refund (staff only).")
    async def auction_cancel(self, interaction: discord.Interaction) -> None:
        await cancel_open_auction(interaction)

    @auction.command(name="end", description="Close the live auction now (staff only).")
    async def auction_end(self, interaction: discord.Interaction) -> None:
        await end_open_auction(interaction)

    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        # guild_id → (monotonic expiry, trigger quests). TTL-refreshed in the
        # message listener; empty lists are cached too so guilds without
        # trigger quests cost one dict lookup per message.
        self._trigger_cache: dict[
            int, tuple[float, list[quests_svc.TriggerQuest]]
        ] = {}
        # Four channel-bottom panels, all on the shared machinery in
        # core.sticky (locks, debounce, id cache, post-before-delete). This cog
        # only says where each panel's ids live and what it should look like.
        self.guide_panel = StickyPanel(
            "econ guide", bot,
            load_ids=lambda gid: self._panel_ids(gid, "guide"),
            save_ids=lambda gid, cid, mid: self._save_panel_ids(gid, "guide", cid, mid),
            build=self._build_guide_panel,
        )
        self.leaderboard_panel = StickyPanel(
            "econ leaderboard", bot,
            load_ids=lambda gid: self._panel_ids(gid, "leaderboard"),
            save_ids=lambda gid, cid, mid: self._save_panel_ids(
                gid, "leaderboard", cid, mid
            ),
            build=self._build_leaderboard_panel,
        )
        self.shop_panel = StickyPanel(
            "econ shop", bot,
            load_ids=lambda gid: self._panel_ids(gid, "shop"),
            save_ids=lambda gid, cid, mid: self._save_panel_ids(gid, "shop", cid, mid),
            build=self._build_shop_panel,
        )
        # The fourth panel is the odd one out: every other StickyPanel in the
        # bot is permanent and one-per-guild, but an auction ENDS. The
        # lifecycle lives entirely in these callbacks (see card_ids /
        # attach_card_to_latest) — card_ids goes (0, 0) at close and the
        # restick, which never creates a panel it didn't already find, simply
        # stops. No change to core.sticky was needed for that.
        #
        # restick_on_bot stays off (the default) and must stay off: while an
        # auction is open the bot posts NOTHING into the channel — bid
        # confirmations are ephemeral and outbid notices are DMs — so there is
        # no bot noise to chase, and chasing our own repost is what turned the
        # casino hub into a 275-message flood.
        self.auction_panel = StickyPanel(
            "econ auction", bot,
            load_ids=self._auction_card_ids,
            save_ids=self._save_auction_card_ids,
            build=self._build_auction_panel,
        )
        # The Bounty Board hub — the board's only entry point since /bounty was
        # deleted (2026-07-29). Unlike the three panels above it does not get
        # to pick its channel: it belongs in `bounty_channel_id`, the board it
        # fronts, so its ids come from there (see _bounty_panel_ids).
        #
        # restick_on_bot stays off, and here that matters more than anywhere
        # else in the cog: this is the one sticky panel whose OWN channel is
        # where the bot posts (every new bounty card, plus a re-render on every
        # chip-in and every award). Turning it on would have the hub chase its
        # own card posts — exactly the loop that produced the casino's
        # 275-message flood. The cost is that the hub sits above the newest
        # card until a human speaks, which is the right trade.
        self.bounty_panel = StickyPanel(
            "econ bounty hub", bot,
            load_ids=self._bounty_panel_ids,
            save_ids=self._save_bounty_panel_ids,
            build=self._build_bounty_panel,
        )
        # Photo Challenge channel id, TTL-cached so the on_message listener
        # costs a dict lookup, not a DB read, for every message in the guild:
        # guild_id → (monotonic expiry, channel_id).
        self._photo_opts: dict[int, tuple[float, int]] = {}
        super().__init__()

    async def cog_unload(self) -> None:
        self._auction_settle_loop.cancel()
        for panel in (
            self.guide_panel,
            self.leaderboard_panel,
            self.shop_panel,
            self.auction_panel,
            self.bounty_panel,
        ):
            panel.cancel_all()

    @tasks.loop(seconds=30)
    async def _auction_settle_loop(self) -> None:
        """Close auctions past their end and announce each — the timed-close path.

        One indexed read finds the (usually zero) guilds with a live auction, so
        idle guilds cost nothing; only those are then swept. Best-effort — a
        per-guild failure is logged, never fatal to the loop."""
        def _live() -> set[int]:
            with self.ctx.open_db() as conn:
                return open_auction_guild_ids(conn)

        try:
            live = await asyncio.to_thread(_live)
        except Exception:
            log.exception("auction settle loop: failed to list live auctions")
            return
        for guild_id in live:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            try:
                await settle_and_announce(self.bot, guild)
            except Exception:
                log.exception("auction settle loop failed for guild %s", guild_id)

    @_auction_settle_loop.before_loop
    async def _before_auction_settle(self) -> None:
        await self.bot.wait_until_ready()

    @bank.command(name="wallet", description="Check your balance and recent activity.")
    async def bank_wallet(self, interaction: discord.Interaction) -> None:
        await self.send_wallet_panel(interaction)

    async def send_wallet_panel(self, interaction: discord.Interaction) -> None:
        """The member's private balance + activity view.

        Shared by the ``/bank wallet`` command and the leaderboard panel's
        "Wallet" button, so both open the exact same ephemeral embed (mirrors
        ``send_quests_panel``).
        """
        assert interaction.guild is not None
        guild = interaction.guild
        guild_id = guild.id
        user_id = interaction.user.id

        def _load() -> tuple[
            EconSettings, int, list, list, int, sqlite3.Row | None
        ]:
            from bot_modules.services.casino_service import (  # noqa: PLC0415
                member_casino_stats,
            )

            with self.ctx.open_db() as conn:
                settings = load_econ_settings(conn, guild_id)
                if not settings.enabled:
                    # The caller refuses before rendering, so the other five
                    # reads would be thrown away (mirrors _shop_context).
                    return settings, 0, [], [], 0, None
                balance = get_balance(conn, guild_id, user_id)
                ledger = get_ledger(conn, guild_id, user_id, limit=10)
                rentals = list_member_rentals(conn, guild_id, user_id)
                shields = get_streak_shields(conn, guild_id, user_id)
                casino = member_casino_stats(conn, guild_id, user_id)
            return settings, balance, ledger, rentals, shields, casino

        settings, balance, ledger, rentals, shields, casino = (
            await asyncio.to_thread(_load)
        )

        if await self._refuse_disabled(interaction, settings):
            return

        accent = await resolve_accent_color(self.ctx.db_path, guild)
        embed = build_wallet_embed(
            settings,
            balance=balance,
            ledger=ledger,
            rentals=rentals,
            shields=shields,
            casino=casino,
            viewer_id=user_id,
            color=accent,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bank.command(name="grant", description="Award currency to a member (staff only).")
    @app_commands.describe(
        member="Who to award",
        amount="How much to award (whole number)",
        reason="Why — recorded in the ledger",
    )
    async def bank_grant(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: int,
        reason: str,
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        guild_id = guild.id
        actor = interaction.user
        assert isinstance(actor, discord.Member)

        settings = await self._settings_or_refuse(interaction, guild_id)
        if settings is None:
            return

        if not _can_grant(actor, settings):
            await interaction.response.send_message(
                "❌ You don't have permission to grant currency.", ephemeral=True
            )
            return

        if member.bot:
            await interaction.response.send_message(
                "❌ Bots don't have wallets.", ephemeral=True
            )
            return

        if amount < 1:
            await interaction.response.send_message(
                "❌ The amount must be at least 1.", ephemeral=True
            )
            return

        booster = member.premium_since is not None
        meta = {"reason": reason, "granted_by": actor.display_name}

        def _grant() -> int:
            with self.ctx.open_db() as conn:
                return apply_credit(
                    conn,
                    guild_id,
                    member.id,
                    amount,
                    "grant",
                    actor_id=actor.id,
                    meta=meta,
                    booster=booster,
                    multiplier=settings.booster_multiplier,
                )

        credited = await asyncio.to_thread(_grant)

        accent = await resolve_accent_color(self.ctx.db_path, guild)
        embed = discord.Embed(
            title=f"{settings.currency_emoji} Currency Granted",
            description=(
                f"{settings.currency_emoji} **{credited:,}** {_unit(settings, credited)} "
                f"→ {member.mention}"
            ),
            color=accent,
        )
        if settings.currency_icon_url:
            embed.set_thumbnail(url=settings.currency_icon_url)
        if booster and credited != amount:
            embed.add_field(
                name="Booster Bonus",
                value=f"Base {amount:,} × {settings.booster_multiplier:g}",
                inline=False,
            )
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_footer(text=f"Granted by {actor.display_name}")

        await interaction.response.send_message(embed=embed)

    @bank.command(
        name="mute", description="Toggle economy DM notifications for yourself."
    )
    async def bank_mute(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        guild_id = guild.id
        user_id = interaction.user.id

        settings = await self._settings_or_refuse(interaction, guild_id)
        if settings is None:
            return

        def _toggle() -> bool:
            with self.ctx.open_db() as conn:
                new_muted = not get_notify_muted(conn, guild_id, user_id)
                set_notify_muted(conn, guild_id, user_id, new_muted)
                return new_muted

        muted = await asyncio.to_thread(_toggle)

        accent = await resolve_accent_color(self.ctx.db_path, guild)
        embed = discord.Embed(
            title="🔔 Notifications Muted" if muted else "🔔 Notifications On",
            description=(
                "You won't get economy DMs anymore. Run this again to turn them back on."
                if muted
                else "You'll get economy DMs again — milestones, streak saves, and more."
            ),
            color=accent,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── transfers ────────────────────────────────────────────────────────

    @bank.command(name="pay", description="Send currency to another member.")
    @app_commands.describe(
        member="Who to pay",
        amount="How much (whole number)",
        memo="Optional note — what's it for? (shown to them)",
    )
    async def bank_pay(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: int,
        memo: app_commands.Range[str, None, _MAX_MEMO_LEN] | None = None,
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        sender = interaction.user
        assert isinstance(sender, discord.Member)

        # Normalise once, here — every downstream site (ledger meta, embeds,
        # DM) takes the cleaned value and escapes only at render.
        memo = _clean_memo(memo)

        settings = await self._settings_or_refuse(interaction, guild.id)
        if settings is None:
            return
        if not settings.transfers_enabled:
            await interaction.response.send_message(
                "❌ Transfers are turned off on this server.", ephemeral=True
            )
            return
        if member.bot:
            await interaction.response.send_message(
                "❌ Bots don't have wallets.", ephemeral=True
            )
            return
        if member.id == sender.id:
            await interaction.response.send_message(
                "❌ You can't pay yourself.", ephemeral=True
            )
            return
        if amount < 1:
            await interaction.response.send_message(
                "❌ The amount must be at least 1.", ephemeral=True
            )
            return

        if amount > _PAY_CONFIRM_THRESHOLD:
            accent = await resolve_accent_color(self.ctx.db_path, guild)
            desc = (
                f"Send {settings.currency_emoji} **{amount:,}** "
                f"{_unit(settings, amount)} to {member.mention}?"
            )
            if memo:
                desc += f"\n\n*{discord.utils.escape_markdown(memo)}*"
            confirm = discord.Embed(
                title=f"{settings.currency_emoji} Confirm Payment",
                description=desc,
                color=accent,
            )
            if settings.currency_icon_url:
                confirm.set_thumbnail(url=settings.currency_icon_url)
            view = _PayConfirmView(self, settings, guild, sender, member, amount, memo)
            await interaction.response.send_message(
                embed=confirm, view=view, ephemeral=True
            )
            return

        await self.finalize_pay(
            interaction, settings, guild, sender, member, amount,
            memo=memo, via_confirm=False,
        )

    async def finalize_pay(
        self,
        interaction: discord.Interaction,
        settings: EconSettings,
        guild: discord.Guild,
        sender: discord.Member,
        recipient: discord.Member,
        amount: int,
        *,
        memo: str | None = None,
        via_confirm: bool,
    ) -> None:
        """Execute the transfer and report — shared by the direct and confirm paths."""

        def _tx() -> int:
            with self.ctx.open_db() as conn:
                transfer_currency(
                    conn, guild.id, sender.id, recipient.id, amount, memo=memo
                )
                return get_balance(conn, guild.id, sender.id)

        try:
            new_balance = await asyncio.to_thread(_tx)
        except ValueError as exc:
            if "insufficient" in str(exc):
                bal = await asyncio.to_thread(self._balance, guild.id, sender.id)
                text = (
                    f"You don't have enough — your balance is "
                    f"{settings.currency_emoji} {bal:,}."
                )
            else:
                text = "That payment isn't allowed."
            await self._reply(interaction, text, via_confirm=via_confirm)
            return

        accent = await resolve_accent_color(self.ctx.db_path, guild)
        safe_memo = discord.utils.escape_markdown(memo) if memo else None
        desc = (
            f"{settings.currency_emoji} **{amount:,}** {_unit(settings, amount)} "
            f"→ {recipient.mention}"
        )
        if safe_memo:
            desc += f"\n\n*{safe_memo}*"
        embed = discord.Embed(
            title=f"{settings.currency_emoji} Payment Sent", description=desc, color=accent
        )
        if settings.currency_icon_url:
            embed.set_thumbnail(url=settings.currency_icon_url)
        embed.set_footer(text=f"Your balance: {new_balance:,}")
        await self._reply_embed(interaction, embed, via_confirm=via_confirm)

        # notify_member sends `content` with no allowed_mentions, and its
        # fallback posts into the public bank channel — so a memo has to be
        # mention-escaped here or it could ping the server.
        note = (
            f"{sender.display_name} sent you {settings.currency_emoji} "
            f"{amount:,} {_unit(settings, amount)}."
        )
        if safe_memo:
            note += f' — "{discord.utils.escape_mentions(safe_memo)}"'
        await notify_member(
            self.bot, self.ctx.db_path, guild.id, recipient.id, content=note,
        )

    # ── shop ─────────────────────────────────────────────────────────────

    @bank.command(name="shop", description="Browse and rent personal-role perks.")
    async def bank_shop(self, interaction: discord.Interaction) -> None:
        await self.open_personal_shop(interaction)

    async def open_personal_shop(self, interaction: discord.Interaction) -> None:
        """Serve the member's personal shop menu, ephemeral to them.

        The one real shop: `/bank shop` and the channel panel's Open Shop
        button both land here, so every shop feature (rent, customise,
        refunds) exists on both surfaces by construction.
        """
        assert interaction.guild is not None
        guild = interaction.guild
        user_id = interaction.user.id

        shop = await asyncio.to_thread(self._shop_context, guild.id, user_id)
        settings = shop.settings
        if await self._refuse_disabled(interaction, settings):
            return

        gated = await self._gated_perks(guild.id)

        accent = await resolve_accent_color(self.ctx.db_path, guild)
        embed = build_shop_embed(
            settings,
            gated,
            accent,
            owned=shop.owned,
            icon_catalog=shop.icon_range,
            balance=shop.balance,
            shields_held=shop.shields_held,
        )
        view = _ShopView(
            self, settings, guild, user_id, gated, shop.owned,
            shop.icon_range is not None,
            shields_held=shop.shields_held, refundable=shop.refundable,
            shield_price=shop.shield_price,
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def do_buy_shield(
        self,
        interaction: discord.Interaction,
        settings: EconSettings,
        guild: discord.Guild,
    ) -> None:
        """Buy the one-shot streak shield (shared by the shop view and panel)."""
        user_id = interaction.user.id

        def _buy() -> int:
            with self.ctx.open_db() as conn:
                return purchase_streak_shield(conn, settings, guild.id, user_id)

        try:
            price = await asyncio.to_thread(_buy)
        except ValueError as exc:
            msg = str(exc)
            if "insufficient" in msg:
                text = await self._short_funds_text(
                    settings, guild.id, user_id, settings.price_streak_shield
                )
            elif "already holding" in msg:
                text = (
                    "❌ You're already holding a 🛡️ shield — it burns "
                    "automatically if a missed day would break your streak."
                )
            else:
                text = "❌ That isn't available right now."
            await interaction.response.send_message(text, ephemeral=True)
            return

        await interaction.response.send_message(
            f"🛡️ Streak shield ready ({settings.currency_emoji} {price:,}). "
            "If a gap would break your login streak, it burns automatically "
            "and the streak lives on.",
            ephemeral=True,
        )

    async def do_buy_raffle_tickets(
        self,
        interaction: discord.Interaction,
        settings: EconSettings,
        raw_quantity: str,
    ) -> None:
        """Buy tickets for the current guild-local ISO week (modal submit)."""
        assert interaction.guild is not None
        guild = interaction.guild
        user_id = interaction.user.id
        try:
            quantity = int(raw_quantity.strip())
        except ValueError:
            await interaction.response.send_message(
                "❌ Give a whole number of tickets.", ephemeral=True
            )
            return

        def _buy() -> raffle_svc.TicketPurchase:
            with self.ctx.open_db() as conn:
                offset = get_tz_offset_hours(conn, guild.id)
                week = quest_rules.iso_week_for(local_day_for(time.time(), offset))
                return raffle_svc.buy_tickets(
                    conn, settings, guild.id, user_id, week, quantity
                )

        try:
            purchase = await asyncio.to_thread(_buy)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"🎟️ {purchase.quantity} ticket(s) bought "
            f"({settings.currency_emoji} {purchase.price:,}) — you hold "
            f"{purchase.week_total} this week. Winner drawn at the week "
            "roll and announced by name; the prize is a free weekly perk "
            "payment.",
            ephemeral=True,
        )

    async def do_rent(
        self,
        interaction: discord.Interaction,
        settings: EconSettings,
        guild: discord.Guild,
        perk: str,
    ) -> None:
        """Rent a self-perk from the ephemeral shop view."""
        await _rent_perk_flow(interaction, self, settings, guild, perk)

    # ── self-service refunds ─────────────────────────────────────────────

    async def open_refund_picker(
        self,
        interaction: discord.Interaction,
        settings: EconSettings,
        guild: discord.Guild,
        refundable: list[dict],
        shield_price: int,
    ) -> None:
        """Show the picker of what the member can cancel & refund."""
        view = _RefundPickerView(
            self, settings, guild, interaction.user.id, refundable, shield_price
        )
        await interaction.response.send_message(
            "Pick what to cancel — this credits you back right away and ends "
            "the perk immediately:",
            view=view,
            ephemeral=True,
        )

    async def show_refund_confirm(
        self,
        interaction: discord.Interaction,
        settings: EconSettings,
        guild: discord.Guild,
        target: str,
    ) -> None:
        """Re-check the pick is still live and show its Confirm/Back gate."""
        user_id = interaction.user.id

        def _load() -> tuple[dict | None, int]:
            with self.ctx.open_db() as conn:
                if target == "shield":
                    return None, get_streak_shield_price(
                        conn, guild.id, user_id, settings
                    )
                rental_id = int(target.split(":", 1)[1])
                row = get_refundable_rental(conn, guild.id, user_id, rental_id)
                return (dict(row) if row is not None else None), 0

        rental, shield_price = await asyncio.to_thread(_load)
        if target == "shield":
            if shield_price <= 0:
                await interaction.response.edit_message(
                    content="❌ You're not holding a shield anymore.",
                    embed=None, view=None,
                )
                return
            text = (
                "Cancel your 🛡️ **Streak Shield**? You'll get back "
                f"{settings.currency_emoji} **{shield_price:,}**."
            )
        else:
            if rental is None:
                await interaction.response.edit_message(
                    content="❌ That rental isn't yours to refund anymore.",
                    embed=None, view=None,
                )
                return
            label = PERK_LABELS.get(str(rental["perk"]), str(rental["perk"]))
            if rental["state"] == "active":
                preview = prorated_refund(
                    int(rental["price"]), float(rental["next_bill_at"]), time.time()
                )
                text = (
                    f"Cancel **{label}**? You'll get back "
                    f"{settings.currency_emoji} **{preview:,}** — the perk ends "
                    "right now."
                )
            else:
                text = (
                    f"Cancel **{label}**? You're already in the grace period, "
                    "so there's no refund — the perk ends right now."
                )
        view = _RefundConfirmView(self, settings, guild, user_id, target)
        await interaction.response.edit_message(content=text, embed=None, view=view)

    async def finalize_refund(
        self,
        interaction: discord.Interaction,
        settings: EconSettings,
        guild: discord.Guild,
        target: str,
    ) -> None:
        """Run the refund for real and strip the perk immediately."""
        user_id = interaction.user.id

        if target == "shield":

            def _refund_shield() -> int:
                with self.ctx.open_db() as conn:
                    return refund_streak_shield(conn, guild.id, user_id, settings)

            try:
                amount = await asyncio.to_thread(_refund_shield)
            except ValueError:
                await interaction.response.edit_message(
                    content="❌ You're not holding a shield anymore.", view=None
                )
                return
            await interaction.response.edit_message(
                content=(
                    "✅ Streak shield cancelled — "
                    f"{settings.currency_emoji} **{amount:,}** credited back."
                ),
                view=None,
            )
            return

        rental_id = int(target.split(":", 1)[1])

        def _refund_rental() -> RentalRefund:
            with self.ctx.open_db() as conn:
                return refund_rental(
                    conn, guild.id, rental_id, requester_id=user_id
                )

        try:
            result = await asyncio.to_thread(_refund_rental)
        except ValueError:
            await interaction.response.edit_message(
                content="❌ That rental isn't yours to refund anymore.", view=None
            )
            return

        # Best-effort past this point: the refund already committed (money
        # moved, rental terminal), so a Discord-side hiccup here must not
        # blow up the interaction — same guard the billing loop's own call to
        # this dispatcher uses for the same reason (economy_loop.py).
        try:
            await revoke_perk_effect(
                self.bot, self.ctx.db_path, guild.id, result.perk, result.rental_id,
                result.beneficiary_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "Economy shop refund: failed to revoke perk for beneficiary %s.",
                result.beneficiary_id,
            )
        label = PERK_LABELS.get(result.perk, result.perk)
        if result.refund > 0:
            text = (
                f"✅ **{label}** cancelled — {settings.currency_emoji} "
                f"**{result.refund:,}** credited back."
            )
        else:
            text = f"✅ **{label}** cancelled — no refund (already in the grace period)."
        await interaction.response.edit_message(content=text, view=None)

    # ── sponsor a QOTD ───────────────────────────────────────────────────

    @bank.command(
        name="sponsor",
        description="Pay to put your question forward as a question of the day.",
    )
    @app_commands.describe(question="Your question — a mod reviews it before it runs")
    async def bank_sponsor(
        self, interaction: discord.Interaction, question: str
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        member = interaction.user
        assert isinstance(member, discord.Member)

        settings = await self._settings_or_refuse(interaction, guild.id)
        if settings is None:
            return
        if not sponsor_enabled(settings):
            await interaction.response.send_message(
                "❌ Sponsoring a question of the day isn't enabled here.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        def _submit():
            with self.ctx.open_db() as conn:
                return submit_sponsor(
                    conn, settings, guild.id, member.id, question
                )

        try:
            outcome = await asyncio.to_thread(_submit)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        # The money is already taken and the row exists; a card failure must
        # never surface as an error to the member (it's still resolvable from
        # the dashboard), so this is best-effort inside post_review_card.
        accent = await resolve_accent_color(self.ctx.db_path, guild)
        await post_review_card(
            self.bot,
            self.ctx,
            guild,
            settings,
            accent,
            outcome.submission_id,
            member,
        )
        unit = _unit(settings, outcome.price)
        await interaction.followup.send(
            f"📨 Sent your question to the mods for review — {outcome.price} "
            f"{unit} held. If it's turned down you'll get a full refund, and "
            "you'll hear either way.",
            ephemeral=True,
        )

    # ── pin of the day ───────────────────────────────────────────────────

    @bank.command(
        name="pin",
        description="Pay to pin a short message for a day — a mod approves it first.",
    )
    async def bank_pin(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        member = interaction.user
        assert isinstance(member, discord.Member)

        settings = await self._settings_or_refuse(interaction, guild.id)
        if settings is None:
            return
        if not pin_enabled(settings):
            await interaction.response.send_message(
                "❌ Pinning a message isn't enabled here.", ephemeral=True
            )
            return
        # A modal must be the FIRST response to the interaction (can't defer
        # first), so the enable check above runs before we open it.
        await interaction.response.send_modal(_PinSubmitModal(self))

    async def do_pin_submit(
        self, interaction: discord.Interaction, message: str
    ) -> None:
        """Escrow the price, queue the pin, and post the mod-approval card."""
        assert interaction.guild is not None
        guild = interaction.guild
        member = interaction.user
        assert isinstance(member, discord.Member)

        settings = await self._settings_or_refuse(interaction, guild.id)
        if settings is None:
            return
        if not pin_enabled(settings):
            await interaction.response.send_message(
                "❌ Pinning a message isn't enabled here.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        def _submit():
            with self.ctx.open_db() as conn:
                return submit_pin(conn, settings, guild.id, member.id, message)

        try:
            outcome = await asyncio.to_thread(_submit)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        # Money's taken and the row exists; a card failure must never surface as
        # an error to the member (it's still resolvable), so it's best-effort.
        accent = await resolve_accent_color(self.ctx.db_path, guild)
        await post_pin_review_card(
            self.bot, self.ctx, guild, settings, accent, outcome.submission_id, member
        )
        unit = _unit(settings, outcome.price)
        await interaction.followup.send(
            f"📨 Sent your message to the mods for review — {outcome.price} "
            f"{unit} held. If it's turned down you'll get a full refund; if it's "
            "approved it's pinned for 24 hours.",
            ephemeral=True,
        )

    # ── community bounty ─────────────────────────────────────────────────

    async def open_bounty_post_modal(self, interaction: discord.Interaction) -> None:
        """Open the post-a-bounty modal for the hub panel's 🎯 button.

        Lives on the cog rather than in ``bounty_views`` because the modal
        submits back into :meth:`do_bounty_post`; the button just routes here.
        Replaced the ``/bounty`` command on 2026-07-29 — the hub is now the
        board's only entry point, per CLAUDE.md's "one panel over a sprawl of
        subcommands".
        """
        assert interaction.guild is not None
        guild = interaction.guild

        settings = await self._settings_or_refuse(interaction, guild.id)
        if settings is None:
            return
        if not bounty_enabled(settings):
            await interaction.response.send_message(
                "❌ Bounties aren't enabled here.", ephemeral=True
            )
            return
        # A modal must be the first response — the enable check runs before it.
        await interaction.response.send_modal(_BountyPostModal(self))

    async def do_bounty_post(
        self,
        interaction: discord.Interaction,
        title: str,
        description: str,
        raw_stake: str,
    ) -> None:
        """Open a bounty from the modal: escrow the stake and post the board card."""
        assert interaction.guild is not None
        guild = interaction.guild
        member = interaction.user
        assert isinstance(member, discord.Member)

        settings = await asyncio.to_thread(self._load_settings, guild.id)
        if not settings.enabled or not bounty_enabled(settings):
            await interaction.response.send_message(
                "❌ Bounties aren't enabled here.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            stake = int(raw_stake.strip())
        except ValueError:
            await interaction.followup.send(
                "❌ Your stake has to be a whole number of coins.", ephemeral=True
            )
            return

        def _create():
            with self.ctx.open_db() as conn:
                return create_bounty(
                    conn, settings, guild.id, member.id,
                    title=title, description=description, stake=stake,
                )

        try:
            outcome = await asyncio.to_thread(_create)
        except ValueError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return

        accent = await resolve_accent_color(self.ctx.db_path, guild)
        await post_bounty_card(
            self.bot, self.ctx, guild, settings, accent, outcome.bounty_id
        )
        # The new bounty belongs on the hub's open list straight away.
        await self.refresh_bounty_hub_panel(guild)
        await interaction.followup.send(
            f"🎯 Bounty posted with {outcome.stake:,} in the pot! Others can chip "
            "in from its card or the board panel, and a mod awards it when it's "
            "done.",
            ephemeral=True,
        )

    # ── emoji sponsorship ────────────────────────────────────────────────

    @bank.command(
        name="emoji",
        description="Sponsor a custom emoji — you pay weekly rent to keep it.",
    )
    @app_commands.describe(
        image="The emoji image (PNG/JPEG/WEBP, GIF for animated — max 256KB)",
        name="Its :name: — 2–32 letters, numbers, underscores",
    )
    async def bank_emoji(
        self,
        interaction: discord.Interaction,
        image: discord.Attachment | None = None,
        name: str | None = None,
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        member = interaction.user
        assert isinstance(member, discord.Member)

        settings = await self._settings_or_refuse(interaction, guild.id)
        if settings is None:
            return
        if not emoji_svc.sponsoring_enabled(settings):
            await interaction.response.send_message(
                "❌ Emoji sponsorship isn't enabled here.", ephemeral=True
            )
            return

        if image is None or name is None:
            await self._emoji_status(interaction, settings, guild, member)
            return

        ctype = (image.content_type or "").split(";")[0].strip()
        if ctype not in _EMOJI_CONTENT_TYPES:
            await interaction.response.send_message(
                "❌ Emoji images are PNG, JPEG, WEBP, or GIF.", ephemeral=True
            )
            return
        if image.size > emoji_svc.MAX_IMAGE_BYTES:
            await interaction.response.send_message(
                "❌ Discord caps emoji images at 256KB — that one's too big.",
                ephemeral=True,
            )
            return
        animated = _EMOJI_CONTENT_TYPES[ctype]

        await interaction.response.defer(ephemeral=True)
        data = await image.read()

        # Live Discord slot math the service layer can't see: sponsors may
        # use the cap's worth of slots, but never the guild's LAST free slot
        # of that kind (static and animated count separately).
        same_kind = [e for e in guild.emojis if e.animated == animated]
        guild_has_room = len(same_kind) < max(0, guild.emoji_limit - 1)

        ext = "gif" if animated else "png"
        directory = self.ctx.db_path.parent / "econ_emoji" / str(guild.id)
        path = directory / f"{int(time.time())}_{member.id}.{ext}"

        taken = {e.name for e in guild.emojis}

        def _submit():
            # The mkdir and the up-to-256KB write ride the worker thread with
            # the DB work rather than stalling the event loop — same order as
            # before (image on disk before the row that points at it), so the
            # unlink below still undoes a rejected submission.
            directory.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            with self.ctx.open_db() as conn:
                for row in emoji_svc.list_submissions(conn, guild.id):
                    if row["state"] in ("pending", "approved", "live"):
                        taken.add(str(row["name"]))
                under_cap = (
                    emoji_svc.open_submission_count(conn, guild.id)
                    < max(0, settings.emoji_sponsor_slots)
                )
                return emoji_svc.submit_sponsorship(
                    conn, settings, guild.id, member.id,
                    name=name, image_path=str(path), animated=animated,
                    blocklist_patterns=list_name_blocklist(conn, guild.id),
                    taken_names=taken,
                    guild_slots_free=guild_has_room and under_cap,
                )

        try:
            outcome = await asyncio.to_thread(_submit)
        except ValueError as exc:
            path.unlink(missing_ok=True)
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        unit = _unit(settings, outcome.price)
        await interaction.followup.send(
            f"📨 Sent :{name.strip(':')}: to the mods for review — "
            f"{outcome.price} {unit} held (that covers week one). If it's "
            "turned down you get a full refund; once it's up it renews "
            "weekly from your wallet, and lapsing takes it down.",
            ephemeral=True,
        )

    async def _emoji_status(
        self,
        interaction: discord.Interaction,
        settings: EconSettings,
        guild: discord.Guild,
        member: discord.Member,
    ) -> None:
        """Bare /bank emoji: show the member's in-flight sponsorship, if any."""

        def _load():
            with self.ctx.open_db() as conn:
                return emoji_svc.open_submission(conn, guild.id, member.id)

        row = await asyncio.to_thread(_load)
        if row is None:
            await interaction.response.send_message(
                "Sponsor a custom emoji with `/bank emoji image: name:` — "
                f"{settings.currency_emoji} {settings.price_emoji:,}/week "
                f"(animated {settings.price_emoji_animated:,}), first week "
                "held at submit, mod-approved.",
                ephemeral=True,
            )
            return
        state = str(row["state"])
        if state == "pending":
            view = _EmojiCancelView(self, int(row["id"]), member.id)
            await interaction.response.send_message(
                f"Your :{row['name']}: is waiting for a mod. Cancel to get "
                "your escrow back:",
                view=view,
                ephemeral=True,
            )
            return
        blurb = (
            "being set up" if state == "approved"
            else "live — it renews weekly from your wallet"
        )
        await interaction.response.send_message(
            f"Your sponsored :{row['name']}: is {blurb}.", ephemeral=True
        )

    async def do_cancel_emoji(
        self, interaction: discord.Interaction, submission_id: int
    ) -> None:
        def _cancel():
            with self.ctx.open_db() as conn:
                return emoji_svc.cancel_submission(
                    conn, submission_id, user_id=interaction.user.id
                )

        try:
            row = await asyncio.to_thread(_cancel)
        except ValueError as exc:
            await interaction.response.edit_message(content=str(exc), view=None)
            return
        await interaction.response.edit_message(
            content=(
                f"Cancelled :{row['name']}: — your "
                f"{int(row['price']):,} escrow is back in your wallet."
            ),
            view=None,
        )

    # ── gift ─────────────────────────────────────────────────────────────

    @bank.command(
        name="gift",
        description="Gift a friend a perk — you pay its weekly price.",
    )
    @app_commands.describe(member="Who to gift it to", perk="Which perk to gift")
    @app_commands.choices(
        perk=[
            app_commands.Choice(name=PERK_LABELS[p], value=p)
            for p in GIFTABLE_PERKS
        ]
    )
    async def bank_gift(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        perk: app_commands.Choice[str],
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        gifter = interaction.user
        assert isinstance(gifter, discord.Member)
        perk_key = perk.value

        settings = await self._settings_or_refuse(interaction, guild.id)
        if settings is None:
            return
        if member.bot:
            await interaction.response.send_message(
                "❌ Bots can't wear perks.", ephemeral=True
            )
            return
        if member.id == gifter.id:
            await interaction.response.send_message(
                "❌ Rent your own perks with /bank shop.", ephemeral=True
            )
            return
        if perk_key in FEATURE_GATED and not await feature_gate_ok(
            self.bot, guild.id, perk_key
        ):
            await interaction.response.send_message(
                "❌ That perk needs a server feature that isn't enabled here.",
                ephemeral=True,
            )
            return
        if perk_key == "voice_style" and settings.price_voice_style <= 0:
            await interaction.response.send_message(
                "❌ The voice-style lease isn't active here right now.",
                ephemeral=True,
            )
            return

        def _recipient_ent() -> set[str]:
            with self.ctx.open_db() as conn:
                return entitlements(conn, guild.id, member.id)

        if perk_key in await asyncio.to_thread(_recipient_ent):
            # Probably a mistake — the perk stacks silently (their role
            # already shows it), so make the double-spend an explicit choice.
            await interaction.response.send_message(
                f"{member.display_name} already has **{PERK_LABELS[perk_key]}**. "
                "Gift it anyway?",
                view=_GiftConfirmView(self, settings, guild, gifter, member, perk_key),
                ephemeral=True,
            )
            return

        await self.finalize_gift(
            interaction, settings, guild, gifter, member, perk_key,
            via_confirm=False,
        )

    async def finalize_gift(
        self,
        interaction: discord.Interaction,
        settings: EconSettings,
        guild: discord.Guild,
        gifter: discord.Member,
        member: discord.Member,
        perk: str,
        *,
        via_confirm: bool,
    ) -> None:
        """Charge the gifter and open the rental with the friend as beneficiary."""

        def _rent() -> None:
            with self.ctx.open_db() as conn:
                rent_perk(
                    conn, settings, guild.id, gifter.id, perk,
                    beneficiary_id=member.id, now=time.time(),
                )

        label = PERK_LABELS[perk]
        try:
            await asyncio.to_thread(_rent)
        except ValueError as exc:
            msg = str(exc)
            if "insufficient" in msg:
                text = await self._short_funds_text(
                    settings, guild.id, gifter.id, perk_price(settings, perk)
                )
            elif "already rented" in msg:
                text = f"❌ You're already gifting them **{label}**."
            else:
                text = "❌ That gift isn't available."
            await self._reply(interaction, text, via_confirm=via_confirm)
            return

        # Defer BEFORE the slow role-apply + notify REST calls (apply_role_perks
        # makes several requests incl. the rate-limited edit_role_positions) so a
        # single 429 can't push the first response past the 3s budget and leave
        # the charged gifter staring at "This interaction failed."
        await self._defer(interaction, via_confirm=via_confirm)
        if perk in SELF_PERKS:
            await apply_role_perks(self.bot, self.ctx.db_path, guild.id, member.id)
        # `/bank role icon` (upload) hard-refuses catalog servers, so only hint it
        # off-catalog; on a catalog server the recipient picks from /bank shop.
        note = ""
        if perk == "role_icon" and (
            await asyncio.to_thread(self._icon_price_range, guild.id) is None
        ):
            note = " They can upload one with `/bank role icon`."
        if perk == "voice_style":
            gift_hint = "Renaming and sizing your voice channel are unlocked."
        elif perk in NO_CONFIG_PERKS:
            gift_hint = "It's already live on your personal role — no setup needed."
        else:
            gift_hint = "Set it up from /bank shop."
        await notify_member(
            self.bot, self.ctx.db_path, guild.id, member.id,
            content=(
                f"{gifter.display_name} gifted you **{label}**! {gift_hint}"
            ),
        )
        await interaction.edit_original_response(
            content=(
                f"Gifted **{label}** to {member.mention}. They can set it from "
                f"`/bank shop`.{note}"
            ),
            embed=None,
            view=None,
        )

    # ── role studio ──────────────────────────────────────────────────────
    # Customisation is button + modal driven from /bank shop; the setters
    # below are shared by the modals (and re-check entitlements on submit,
    # since a rental can lapse between opening the shop and submitting).

    async def open_customise_modal(
        self, interaction: discord.Interaction, perk: str
    ) -> None:
        await interaction.response.send_modal(_CFG_MODALS[perk](self))

    async def open_icon_catalog(
        self,
        interaction: discord.Interaction,
        settings: EconSettings,
        guild: discord.Guild,
    ) -> None:
        """Show the curated icon picker (rent a new icon or switch the rented one)."""
        if not await self._role_icon_gate_ok(interaction, guild.id):
            return
        icons = await asyncio.to_thread(self._load_catalog, guild.id)
        if not icons:
            await interaction.response.send_message(
                "❌ No rentable icons are set up here yet.", ephemeral=True
            )
            return
        note = ""
        if len(icons) > _ICON_SELECT_LIMIT:
            note = f"\n_Showing the first {_ICON_SELECT_LIMIT} icons._"
        view = _IconCatalogView(self, settings, guild, interaction.user.id, icons)
        await interaction.response.send_message(
            f"Pick a role icon to rent — each is billed weekly:{note}",
            view=view,
            ephemeral=True,
        )

    async def pick_catalog_icon(
        self, interaction: discord.Interaction, guild: discord.Guild, icon_id: int
    ) -> None:
        """Rent the chosen catalog icon, or switch a live rental to it.

        A fresh rental charges the icon's price upfront; switching an existing
        rental only re-tags it (no charge) so the newly chosen icon's price
        takes effect at the next weekly renewal.
        """
        user_id = interaction.user.id

        def _load() -> tuple[EconSettings, dict | None, int | None]:
            with self.ctx.open_db() as conn:
                settings = load_econ_settings(conn, guild.id)
                row = get_catalog_icon(conn, guild.id, icon_id)
                icon = (
                    {
                        "name": row["name"],
                        "price": int(row["price"]),
                        "image_path": str(row["image_path"]),
                    }
                    if row is not None and int(row["enabled"])
                    else None
                )
                existing = get_live_role_icon_rental(conn, guild.id, user_id)
                existing_id = int(existing["id"]) if existing is not None else None
            return settings, icon, existing_id

        settings, icon, existing_id = await asyncio.to_thread(_load)
        if await self._refuse_disabled(interaction, settings):
            return
        if icon is None:
            await interaction.response.send_message(
                "❌ That icon isn't available anymore — open the shop again.",
                ephemeral=True,
            )
            return
        if not await self._role_icon_gate_ok(interaction, guild.id):
            return

        if existing_id is None:
            # New rental: rent_perk + icon set ride ONE transaction, so a failed
            # upfront debit rolls the whole thing back (the ValueError must
            # propagate out of the `with` block — never caught inside it).
            def _rent() -> None:
                with self.ctx.open_db() as conn:
                    rent_perk(
                        conn, settings, guild.id, user_id, "role_icon",
                        catalog_icon_id=icon_id, now=time.time(),
                    )
                    upsert_personal_role(
                        conn, guild.id, user_id, {"icon_path": icon["image_path"]}
                    )

            try:
                await asyncio.to_thread(_rent)
            except ValueError as exc:
                msg = str(exc)
                if "insufficient" in msg:
                    text = await self._short_funds_text(
                        settings, guild.id, user_id, int(icon["price"])
                    )
                elif "already rented" in msg:
                    text = "❌ You're already renting a role icon."
                else:
                    text = "❌ That icon isn't available."
                await interaction.response.send_message(text, ephemeral=True)
                return
            verb = "Rented"
        else:
            def _switch() -> None:
                with self.ctx.open_db() as conn:
                    set_rental_catalog_icon(conn, guild.id, existing_id, icon_id)
                    upsert_personal_role(
                        conn, guild.id, user_id, {"icon_path": icon["image_path"]}
                    )

            await asyncio.to_thread(_switch)
            verb = "Switched to"

        # Defer before apply_role_perks — its rate-limited role edits can exceed
        # the 3s budget, and the rent/switch is already committed.
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok = await apply_role_perks(self.bot, self.ctx.db_path, guild.id, user_id)
        tail = (
            "" if ok else " (I couldn't update your role right now — try again shortly.)"
        )
        await interaction.edit_original_response(
            content=(
                f"{verb} the **{icon['name']}** icon "
                f"({settings.currency_emoji} {icon['price']:,}/week).{tail}"
            ),
        )

    async def pick_custom_icon(
        self,
        interaction: discord.Interaction,
        settings: EconSettings,
        guild: discord.Guild,
    ) -> None:
        """The picker's Custom entry: rent the flat-price bring-your-own icon,
        or switch a live catalog rental back to it.

        Switching re-tags the rental (no charge; the flat price bills from the
        next renewal, mirroring an icon-to-icon switch) and clears the
        projected image — the catalog art belongs to the catalog price, so the
        member starts from a blank icon and uploads their own.
        """
        user_id = interaction.user.id

        def _load() -> tuple[EconSettings, dict | None]:
            with self.ctx.open_db() as conn:
                fresh = load_econ_settings(conn, guild.id)
                row = get_live_role_icon_rental(conn, guild.id, user_id)
                existing = (
                    {
                        "id": int(row["id"]),
                        "catalog_icon_id": row["catalog_icon_id"],
                    }
                    if row is not None
                    else None
                )
            return fresh, existing

        settings, existing = await asyncio.to_thread(_load)
        if await self._refuse_disabled(interaction, settings):
            return
        if not await self._role_icon_gate_ok(interaction, guild.id):
            return

        if existing is None:
            # Fresh rental — the shared rent flow already handles the charge,
            # the error copy, and the upload note + customise button.
            await _rent_perk_flow(interaction, self, settings, guild, "role_icon")
            return
        if existing["catalog_icon_id"] is None:
            await interaction.response.send_message(
                "You already have the custom icon — set an emoji below, or "
                "upload an image with `/bank role icon`.",
                view=_PostRentView(self, "role_icon"),
                ephemeral=True,
            )
            return

        def _switch() -> None:
            with self.ctx.open_db() as conn:
                set_rental_catalog_icon(conn, guild.id, existing["id"], None)
                upsert_personal_role(conn, guild.id, user_id, {"icon_path": ""})

        await asyncio.to_thread(_switch)
        await apply_role_perks(self.bot, self.ctx.db_path, guild.id, user_id)
        await interaction.response.send_message(
            f"Switched to a **custom icon** ({settings.currency_emoji} "
            f"{settings.price_role_icon:,}/week from your next renewal). "
            "Set an emoji below, or upload an image with `/bank role icon`.",
            view=_PostRentView(self, "role_icon"),
            ephemeral=True,
        )

    async def set_role_name(self, interaction: discord.Interaction, text: str) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        user_id = interaction.user.id
        if not await self._require_perk(interaction, guild.id, "role_name"):
            return
        text = text.strip()
        if not text or len(text) > _MAX_ROLE_NAME_LEN:
            await interaction.response.send_message(
                f"❌ Role names must be 1–{_MAX_ROLE_NAME_LEN} characters.", ephemeral=True
            )
            return
        patterns = await asyncio.to_thread(self._name_blocklist, guild.id)
        if name_is_blocked(text, patterns):
            await interaction.response.send_message(
                "❌ That name isn't allowed here.", ephemeral=True
            )
            return
        await asyncio.to_thread(
            self._upsert_role, guild.id, user_id, {"name": text}
        )
        nick_ok, nick_reason = await self._apply_custom_nick(interaction.user, text)
        await self._apply_and_confirm(
            interaction,
            guild.id,
            user_id,
            _custom_name_confirmation(text, nick_ok=nick_ok, nick_reason=nick_reason),
        )

    async def _apply_custom_nick(
        self, member: discord.User | discord.Member, text: str
    ) -> tuple[bool, str]:
        """Set ``member``'s server nickname to their custom name (best-effort).

        Returns ``(ok, reason)``. The role rename stands regardless; the
        nickname can fail when the bot lacks Manage Nicknames, the member
        outranks the bot, or the member is the guild owner (a Discord limit).
        """
        if not isinstance(member, discord.Member):
            return False, ""
        try:
            await member.edit(nick=text, reason="Economy custom-name perk")
        except discord.Forbidden:
            return False, _NICK_FORBIDDEN
        except discord.HTTPException:
            return False, _NICK_FAILED
        return True, ""

    async def set_role_color(self, interaction: discord.Interaction, hex: str) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        user_id = interaction.user.id
        if not await self._require_perk(interaction, guild.id, "role_color"):
            return
        value = parse_hex_color(hex)
        if value is None:
            await interaction.response.send_message(
                "❌ Give a color as a hex code like `#7B2FF7`.", ephemeral=True
            )
            return
        clash = find_color_clash(guild, value)
        if clash is not None:
            await interaction.response.send_message(
                f"❌ That color is too close to **{clash.name}** — pick another.",
                ephemeral=True,
            )
            return
        await asyncio.to_thread(
            self._upsert_role, guild.id, user_id, {"color": value}
        )
        await self._apply_and_confirm(
            interaction, guild.id, user_id, f"Your role color is now `#{value:06X}`."
        )

    async def set_role_gradient(
        self, interaction: discord.Interaction, hex1: str, hex2: str
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        user_id = interaction.user.id
        if not await self._require_perk(interaction, guild.id, "role_gradient"):
            return
        if not await feature_gate_ok(self.bot, guild.id, "role_gradient"):
            await interaction.response.send_message(
                "❌ This server doesn't support gradient roles right now.", ephemeral=True
            )
            return
        v1, v2 = parse_hex_color(hex1), parse_hex_color(hex2)
        if v1 is None or v2 is None:
            await interaction.response.send_message(
                "❌ Give both colors as hex codes like `#7B2FF7`.", ephemeral=True
            )
            return
        clash = find_color_clash(guild, v1) or find_color_clash(guild, v2)
        if clash is not None:
            await interaction.response.send_message(
                f"❌ That color is too close to **{clash.name}** — pick another.",
                ephemeral=True,
            )
            return
        await asyncio.to_thread(
            self._upsert_role, guild.id, user_id, {"color": v1, "color2": v2}
        )
        await self._apply_and_confirm(
            interaction, guild.id, user_id,
            f"Your gradient is now `#{v1:06X}` → `#{v2:06X}`.",
        )

    async def set_role_icon_emoji(
        self, interaction: discord.Interaction, raw: str
    ) -> None:
        """Set the role icon from one of the server's custom emojis (modal path)."""
        assert interaction.guild is not None
        guild = interaction.guild
        user_id = interaction.user.id
        if not await self._require_perk(interaction, guild.id, "role_icon"):
            return
        if not await self._role_icon_gate_ok(interaction, guild.id):
            return
        if await asyncio.to_thread(self._catalog_locked, guild.id, user_id):
            await interaction.response.send_message(_CATALOG_LOCKED_MSG, ephemeral=True)
            return
        emoji = _resolve_guild_emoji(guild, raw)
        if emoji is None:
            await interaction.response.send_message(
                "❌ That doesn't match a custom emoji on this server — type its "
                "name like `:party_parrot:`. For an image icon, upload one "
                "with `/bank role icon`.",
                ephemeral=True,
            )
            return
        if emoji.animated:
            await interaction.response.send_message(
                "❌ Animated emojis can't be role icons — pick a static one.",
                ephemeral=True,
            )
            return
        try:
            data = await emoji.read()
        except discord.HTTPException:
            await interaction.response.send_message(
                "❌ I couldn't fetch that emoji's image — try again shortly.",
                ephemeral=True,
            )
            return
        if len(data) > _MAX_ICON_BYTES:
            await interaction.response.send_message(
                "❌ That emoji's image is too big — 256KB max.", ephemeral=True
            )
            return
        await self._store_role_icon(interaction, guild.id, user_id, data)

    @role.command(
        name="icon", description="Upload an image for your personal role's icon."
    )
    @app_commands.describe(
        image="An image up to 256KB (emoji icons: use /bank shop)"
    )
    async def role_icon(
        self, interaction: discord.Interaction, image: discord.Attachment
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        user_id = interaction.user.id
        if not await self._require_perk(interaction, guild.id, "role_icon"):
            return
        if not await self._role_icon_gate_ok(interaction, guild.id):
            return
        if await asyncio.to_thread(self._catalog_locked, guild.id, user_id):
            await interaction.response.send_message(_CATALOG_LOCKED_MSG, ephemeral=True)
            return
        if image.size > _MAX_ICON_BYTES:
            await interaction.response.send_message(
                "❌ That image is too big — 256KB max.", ephemeral=True
            )
            return
        data = await image.read()
        await self._store_role_icon(interaction, guild.id, user_id, data)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Cancel a leaver's rentals, refund live wagers, re-project roles."""
        guild = member.guild
        settings = await asyncio.to_thread(self._load_settings, guild.id)
        if not settings.enabled:
            return

        # Escrowed game stakes come back: the plan flagged a leaver's stake
        # stranding forever (their id stays in the roster but they can never
        # win). Refunding only their row leaves the rest of the pot intact
        # for the players still in the game.
        def _refund_wagers() -> int:
            with self.ctx.open_db() as conn:
                total = 0
                for row in wager_svc.live_stakes_for_member(
                    conn, guild.id, member.id
                ):
                    total += wager_svc.refund_player(
                        conn, str(row["game_type"]), int(row["game_id"]),
                        member.id,
                    )
                return total

        try:
            refunded = await asyncio.to_thread(_refund_wagers)
            if refunded:
                log.info(
                    "econ: refunded %d staked to leaver %s in %s",
                    refunded, member.id, guild.id,
                )
        except Exception:
            log.exception(
                "econ: wager refund failed for leaver %s in %s",
                member.id, guild.id,
            )

        def _cancel() -> list:
            with self.ctx.open_db() as conn:
                return cancel_all_for_member(
                    conn, guild.id, member.id, now=time.time()
                )

        rows = await asyncio.to_thread(_cancel)
        # Re-project every distinct beneficiary whose entitlements just changed —
        # the leaver themselves (self-perks / received gifts) AND any friend whose
        # gifted color the leaver was funding.
        affected = {int(r["beneficiary_id"]) for r in rows}
        affected.add(member.id)
        for beneficiary_id in affected:
            try:
                await revoke_role_perks(
                    self.bot, self.ctx.db_path, guild.id, beneficiary_id
                )
            except Exception:
                log.exception(
                    "econ: role cleanup failed for %s in %s", beneficiary_id, guild.id
                )

    # ── shared helpers ───────────────────────────────────────────────────

    async def _apply_and_confirm(
        self, interaction: discord.Interaction, guild_id: int, user_id: int, msg: str
    ) -> None:
        # Defer first: apply_role_perks makes several REST calls (incl. the
        # rate-limited edit_role_positions), which a 429 can push past the 3s
        # interaction budget — leaving the member with "This interaction failed"
        # even though the perk saved.
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok = await apply_role_perks(self.bot, self.ctx.db_path, guild_id, user_id)
        if ok:
            await interaction.edit_original_response(content=msg)
        else:
            await interaction.edit_original_response(
                content=(
                    "Saved — but I couldn't update your role right now. "
                    "Try again shortly."
                ),
            )

    async def _refuse_disabled(
        self, interaction: discord.Interaction, settings: EconSettings
    ) -> bool:
        """Send the economy-off refusal; True when it fired and the caller stops.

        Every member-facing economy surface opens with this gate. Commands that
        already hold ``settings`` from a compound load (the shop's role context,
        the quest board, the wallet) call this directly; the ones that would
        otherwise load settings for the gate alone go through
        ``_settings_or_refuse``.
        """
        if settings.enabled:
            return False
        await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
        return True

    async def _settings_or_refuse(
        self, interaction: discord.Interaction, guild_id: int
    ) -> EconSettings | None:
        """Settings for an enabled economy, else send the refusal and give None."""
        settings = await asyncio.to_thread(self._load_settings, guild_id)
        return None if await self._refuse_disabled(interaction, settings) else settings

    async def _require_perk(
        self, interaction: discord.Interaction, guild_id: int, perk: str
    ) -> bool:
        """Whether the member may drive ``perk``'s role-studio setter.

        The economy-off gate and the not-rented refusal in one place: every
        setter opened with the same pair, differing only in the perk name and
        its refusal line.
        """
        settings, ent = await asyncio.to_thread(
            self._load_role_ctx, guild_id, interaction.user.id
        )
        if await self._refuse_disabled(interaction, settings):
            return False
        if perk not in ent:
            # .get, not [perk]: the table covers four of the five SELF_PERKS,
            # so a setter added for the fifth must degrade to a polite refusal,
            # not a KeyError and "This interaction failed."
            await interaction.response.send_message(
                _PERK_REFUSAL.get(perk, _PERK_REFUSAL_FALLBACK), ephemeral=True
            )
            return False
        return True

    async def _role_icon_gate_ok(
        self, interaction: discord.Interaction, guild_id: int
    ) -> bool:
        """Whether role icons are available here; sends the refusal if not."""
        if await feature_gate_ok(self.bot, guild_id, "role_icon"):
            return True
        await interaction.response.send_message(_NO_ROLE_ICONS_MSG, ephemeral=True)
        return False

    async def _gated_perks(self, guild_id: int) -> set[str]:
        """The feature-gated perks this guild currently can't offer."""
        return {
            perk
            for perk in FEATURE_GATED
            if not await feature_gate_ok(self.bot, guild_id, perk)
        }

    async def _store_role_icon(
        self, interaction: discord.Interaction, guild_id: int, user_id: int, data: bytes
    ) -> None:
        """Persist role-icon bytes and re-apply the role.

        Shared tail of the two acquisition paths — the emoji modal and the
        `/bank role icon` upload — which differ only in where ``data`` and its
        size refusal come from.
        """
        def _write() -> None:
            # _icon_store_path mkdirs, so it belongs on the worker thread too.
            path = _icon_store_path(self.ctx.db_path, guild_id, user_id)
            path.write_bytes(data)
            self._upsert_role(guild_id, user_id, {"icon_path": str(path)})

        await asyncio.to_thread(_write)
        await self._apply_and_confirm(
            interaction, guild_id, user_id, "Your role icon is set."
        )

    async def _defer(
        self, interaction: discord.Interaction, *, via_confirm: bool
    ) -> None:
        """Ack the interaction before slow role-apply REST calls.

        A component press (``via_confirm``) defers as a message update — no
        loading spinner, so the confirm view's own message is edited in place by
        the follow-up ``edit_original_response``. A fresh slash/modal submit
        defers with the thinking indicator.
        """
        if via_confirm:
            await interaction.response.defer()
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)

    async def _reply(
        self, interaction: discord.Interaction, text: str, *, via_confirm: bool
    ) -> None:
        if via_confirm:
            await interaction.response.edit_message(content=text, embed=None, view=None)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    async def _reply_embed(
        self, interaction: discord.Interaction, embed: discord.Embed, *, via_confirm: bool
    ) -> None:
        if via_confirm:
            await interaction.response.edit_message(
                content=None, embed=embed, view=None
            )
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    def _balance(self, guild_id: int, user_id: int) -> int:
        with self.ctx.open_db() as conn:
            return get_balance(conn, guild_id, user_id)

    async def _short_funds_text(
        self, settings: EconSettings, guild_id: int, user_id: int, price: int
    ) -> str:
        """The shared refusal for a purchase the member can't afford.

        Reads the balance back so the member sees the gap, not just the price.
        Every "insufficient" handler in the cog renders this one sentence — a
        reword belongs here, not in four places.
        """
        bal = await asyncio.to_thread(self._balance, guild_id, user_id)
        return (
            f"❌ You need {settings.currency_emoji} {price:,} "
            f"but only have {bal:,}."
        )

    def _shop_context(self, guild_id: int, user_id: int) -> _ShopContext:
        """Read the whole shop in one go.

        The header (settings, entitlements, balance), the catalog row's price
        span, and the refund/shield controls' state were three separate thread
        hops, each opening — and PRAGMA-ing — its own connection for a page
        that renders once. ``list_refundable_rentals`` already excludes
        sponsored-emoji rentals (a different self-service surface, ``/bank
        emoji``) and admin-force-cancelled ones.

        A disabled economy stops after the settings row: the caller refuses
        before rendering anything, so reading the rest would be paying five
        queries to say "the economy is off".
        """
        with self.ctx.open_db() as conn:
            settings = load_econ_settings(conn, guild_id)
            if not settings.enabled:
                return _ShopContext(settings, set(), 0, None, [], 0, 0)
            ent = entitlements(conn, guild_id, user_id)
            balance = get_balance(conn, guild_id, user_id)
            icon_range = catalog_price_range(conn, guild_id)
            rentals = [dict(r) for r in list_refundable_rentals(conn, guild_id, user_id)]
            shields_held, shield_price = get_streak_shield_status(
                conn, guild_id, user_id, settings
            )
        return _ShopContext(
            settings, ent, balance, icon_range, rentals, shields_held, shield_price
        )

    def _load_role_ctx(
        self, guild_id: int, user_id: int
    ) -> tuple[EconSettings, set[str]]:
        """Settings and the member's entitlements, for the role-studio gates.

        No balance: the shop header is the only surface that shows one, and it
        reads the whole page through ``_shop_context``.
        """
        with self.ctx.open_db() as conn:
            settings = load_econ_settings(conn, guild_id)
            ent = entitlements(conn, guild_id, user_id)
        return settings, ent

    def _name_blocklist(self, guild_id: int) -> list[str]:
        with self.ctx.open_db() as conn:
            return list_name_blocklist(conn, guild_id)

    def _upsert_role(
        self, guild_id: int, user_id: int, values: dict[str, object]
    ) -> None:
        with self.ctx.open_db() as conn:
            upsert_personal_role(conn, guild_id, user_id, values)

    def _load_catalog(self, guild_id: int) -> list[dict]:
        """Enabled catalog icons a member may rent, as plain dicts for the view."""
        with self.ctx.open_db() as conn:
            return [
                {"id": int(r["id"]), "name": r["name"], "price": int(r["price"])}
                for r in list_catalog(conn, guild_id, enabled_only=True)
            ]

    def _icon_price_range(self, guild_id: int) -> tuple[int, int, int] | None:
        """(min, max, count) over enabled icons, or None with no catalog set up."""
        with self.ctx.open_db() as conn:
            return catalog_price_range(conn, guild_id)

    def _catalog_locked(self, guild_id: int, user_id: int) -> bool:
        """Whether this member's live icon rental is tied to a catalog icon.

        A catalog rental's image IS the icon being paid for, so the
        bring-your-own upload paths are blocked until the member switches to
        the flat-price Custom entry in the shop picker. A custom rental (or no
        rental — the entitlement check upstream handles that) uploads freely,
        catalog or not.
        """
        with self.ctx.open_db() as conn:
            row = get_live_role_icon_rental(conn, guild_id, user_id)
        return row is not None and row["catalog_icon_id"] is not None

    @bank.command(name="quests", description="View and claim the server's active quests.")
    async def bank_quests(self, interaction: discord.Interaction) -> None:
        await self.send_quests_panel(interaction)

    async def send_quests_panel(self, interaction: discord.Interaction) -> None:
        """The member's private quest list + claim UI.

        Shared by the ``/bank quests`` command and the leaderboard panel's
        "Show my quests" button, so both open the exact same ephemeral view.
        """
        assert interaction.guild is not None
        guild = interaction.guild

        settings, quests_state, board_meta = await asyncio.to_thread(
            self._load_quests_state, guild.id, interaction.user.id
        )
        if await self._refuse_disabled(interaction, settings):
            return

        accent = await resolve_accent_color(self.ctx.db_path, guild)
        embed = build_quest_board_embed(settings, quests_state, color=accent)
        if not quests_state:
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        claimable = [q for q in quests_state if q["state"] == "claimable"]
        rerollable = board_meta.get("rerollable") or []
        show_reroll = bool(board_meta.get("reroll_ok") and rerollable)
        kwargs: dict = {
            "embed": embed,
            "ephemeral": True,
            "view": QuestClaimView(
                self.ctx, settings, guild, claimable,
                rerollable=rerollable if show_reroll else None,
                reroll_cost=board_meta.get("reroll_cost"),
                local_day=str(board_meta.get("local_day") or ""),
                detailable=quests_state,
                accent=accent,
            ),
        }
        await interaction.response.send_message(**kwargs)

    def _load_quests_state(
        self, guild_id: int, user_id: int
    ) -> tuple[EconSettings, list[dict], dict]:
        """Load active quests with the caller's per-period claim state.

        Community quests carry their running total (no self-claim); daily/weekly
        carry ``claimable``/``pending``/``done`` for this period's key.
        """
        with self.ctx.open_db() as conn:
            settings = load_econ_settings(conn, guild_id)
            if not settings.enabled:
                return settings, [], {}
            offset = get_tz_offset_hours(conn, guild_id)
            day = local_day_for(time.time(), offset)
            out = load_member_quest_board(conn, settings, guild_id, user_id, day)
            # Reroll offer: board quests untouched this period (no claim, no
            # counted progress). One free swap per guild-local day, then paid
            # ones up to the daily cap — `reroll_cost` is 0/price/None, and
            # None is the only state that hides the select.
            cost = reroll_quote(conn, settings, guild_id, user_id, day)
            meta = {
                "local_day": day,
                "reroll_cost": cost,
                "reroll_ok": cost is not None,
                "rerollable": [
                    {"id": q["id"], "title": q["title"], "qtype": q["qtype"]}
                    for q in out
                    if q["qtype"] in ("daily", "weekly", "monthly")
                    and q["state"] not in ("done", "pending")
                    and not q.get("progress_current")
                ],
            }
        return settings, out, meta

    # ── trigger-word quest verification (spec §4.4) ───────────────────────

    @commands.Cog.listener("on_message")
    async def _on_trigger_message(self, message: discord.Message) -> None:
        """Auto-claim trigger-word quests when a member says the phrase.

        The message is the verification: an instant quest pays on the spot
        (reply + ✅), a sign-off quest files the pending claim and posts the
        bank-channel card. Repeats inside the period fall out silently via
        ``claim_trigger_word``'s ValueError.

        The compiled-pattern index is TTL-cached here because this fires for
        every message in the guild; the matching and the claim rules live in
        ``economy_quests_service`` beside the trigger-*kind* mechanic they
        share a board gate with.
        """
        if message.guild is None or message.author.bot:
            return
        member = message.author
        if not isinstance(member, discord.Member):
            return
        content = message.content or ""
        if not content:
            return

        guild_id = message.guild.id
        now = time.monotonic()
        cached = self._trigger_cache.get(guild_id)
        if cached is None or cached[0] <= now:

            def _load() -> list[quests_svc.TriggerQuest]:
                with self.ctx.open_db() as conn:
                    return quests_svc.load_trigger_index(conn, guild_id)

            try:
                triggers = await asyncio.to_thread(_load)
            except Exception:
                log.exception("econ trigger: failed to load quests for %s", guild_id)
                return
            self._trigger_cache[guild_id] = (now + _TRIGGER_CACHE_TTL, triggers)
        else:
            triggers = cached[1]
        if not triggers:
            return

        channel = message.channel
        # Threads count as their parent channel.
        scope = (channel.id, getattr(channel, "parent_id", None))
        for trig in quests_svc.matching_triggers(triggers, content, scope):
            await self._complete_trigger_quest(message, member, trig)

    async def _complete_trigger_quest(
        self,
        message: discord.Message,
        member: discord.Member,
        trig: quests_svc.TriggerQuest,
    ) -> None:
        """Claim a matched trigger quest for the message author, best-effort."""
        guild = message.guild
        assert guild is not None
        booster = member.premium_since is not None

        def _claim():
            with self.ctx.open_db() as conn:
                return quests_svc.claim_trigger_word(
                    conn, guild.id, member.id, trig,
                    booster=booster, now=time.time(),
                )

        try:
            settings, outcome = await asyncio.to_thread(_claim)
        except ValueError:
            # Already claimed this period, quest window closed, deactivated
            # since the cache load, or off-board — every repeat message would
            # hit this, so stay quiet rather than spam the channel.
            return
        except Exception:
            log.exception(
                "econ trigger: claim failed for quest %s", trig.quest_id
            )
            return

        await self._announce_quest_claim(message, member, settings, outcome)

    async def _announce_quest_claim(
        self,
        message: discord.Message,
        member: discord.Member,
        settings: EconSettings,
        outcome,
    ) -> None:
        """React for an auto-claimed quest (trigger phrase or photo).

        Silent otherwise — no channel reply, no DM. Wallet/quest log carries
        the news, same as every other trigger kind.
        """
        guild = message.guild
        assert guild is not None

        if outcome.state == "paid":
            reaction = "✅"
        else:
            # Sign-off trigger quest: the phrase files the claim; a manager
            # still approves the payout from the bank-channel card.
            accent = await resolve_accent_color(self.ctx.db_path, guild)
            await post_signoff_card(
                self.bot, self.ctx, guild, settings, accent,
                int(outcome.claim_id), member,
            )
            reaction = "📝"

        try:
            await message.add_reaction(reaction)
        except discord.HTTPException:
            log.debug("econ trigger: failed to react", exc_info=True)

    # ── photo-post event quest (posting a photo in the Photo Challenge channel) ──

    _PHOTO_OPTS_TTL = 60.0  # channel-id cache staleness bound (seconds)

    async def _photo_channel(self, guild_id: int) -> int:
        """TTL-cached ``read_photo_channel`` — one DB read per guild per TTL.

        The cache stays here rather than in the service: it exists because
        ``on_message`` fires for every message in the guild, which is a
        listener concern, not a rule about how photo posts pay. Only image
        posts in the configured channel go past this to the service.
        """
        now = time.monotonic()
        cached = self._photo_opts.get(guild_id)
        if cached is not None and cached[0] > now:
            return cached[1]

        def _read() -> int:
            with self.ctx.open_db() as conn:
                return photo_svc.read_photo_channel(conn, guild_id)

        try:
            channel_id = await asyncio.to_thread(_read)
        except Exception:
            log.exception("econ photo: channel read failed in guild %s", guild_id)
            return 0
        self._photo_opts[guild_id] = (now + self._PHOTO_OPTS_TTL, channel_id)
        return channel_id

    @commands.Cog.listener("on_message")
    async def _on_photo_post(self, message: discord.Message) -> None:
        """Pay for an image posted in the Photo Challenge channel.

        Guards cheapest-first: guild/bot check, image check, TTL-cached
        channel gate, then a DB eligibility pre-check so a channel with
        nothing to pay never opens a write transaction. The payout split
        (flat participation award + stacked photo_post quest bonus, each
        once per guild-local day) lives in ``economy_photo_service``.
        """
        if message.guild is None or message.author.bot:
            return
        member = message.author
        if not isinstance(member, discord.Member):
            return
        if not _has_image_attachment(message):
            return
        channel_id = await self._photo_channel(message.guild.id)
        if channel_id == 0:
            return
        # A photo posted in a thread of the Photo Challenge channel should earn
        # too — match on the parent like the trigger-quest / games siblings.
        parent_id = getattr(message.channel, "parent_id", None)
        if channel_id not in (message.channel.id, parent_id):
            return

        guild_id = message.guild.id

        def _possible() -> bool:
            with self.ctx.open_db() as conn:
                return photo_svc.payout_possible(conn, guild_id)

        try:
            eligible = await asyncio.to_thread(_possible)
        except Exception:
            log.exception(
                "econ photo: eligibility check failed in guild %s", guild_id
            )
            return
        if not eligible:
            return

        booster = member.premium_since is not None

        def _claim():
            with self.ctx.open_db() as conn:
                return photo_svc.award_photo_post(
                    conn, guild_id, member.id,
                    channel_id=channel_id, booster=booster, now=time.time(),
                )

        try:
            result = await asyncio.to_thread(_claim)
        except Exception:
            log.exception("econ photo: claim failed in guild %s", guild_id)
            return
        if result is None:
            return
        settings, participation, fired = result
        if fired:
            # A quest outcome carries its own ✅ (paid) / 📝 (sign-off) react.
            for _quest, outcome in fired:
                await self._announce_quest_claim(message, member, settings, outcome)
        elif participation:
            # Flat award only (no quest fired) — acknowledge the post.
            try:
                await message.add_reaction("✅")
            except discord.HTTPException:
                log.debug("econ photo: participation react failed", exc_info=True)

    @commands.Cog.listener("on_member_update")
    async def _on_boost_started(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        """Fire the boost trigger when a member starts boosting.

        Occurrence is the boost start timestamp, so one boost pays an event
        quest once even across gateway replays; boosting again later is a new
        occurrence. Nothing in the codebase watched premium_since transitions
        before this.
        """
        if before.premium_since is not None or after.premium_since is None:
            return
        guild_id = after.guild.id
        occurrence = str(int(after.premium_since.timestamp()))

        def _fire():
            with self.ctx.open_db() as conn:
                return fire_trigger_inline(
                    conn,
                    guild_id,
                    "boost",
                    after.id,
                    occurrence=occurrence,
                    booster=True,
                )

        await asyncio.to_thread(_fire)

    @commands.Cog.listener("on_message")
    async def _on_media_post(self, message: discord.Message) -> None:
        """Fire the media-post trigger for any image a member posts.

        Channel scoping happens per quest (``trigger_channel_id``) inside
        ``fire_trigger_quests``, so an unscoped media quest counts every
        channel while a scoped one (e.g. #art) only counts there. Occurrence
        is the message, so event quests pay per image message — the sane
        cadence for this kind is daily/weekly, as the dashboard hint says.
        """
        if message.guild is None or message.author.bot:
            return
        member = message.author
        if not isinstance(member, discord.Member):
            return
        if not _has_image_attachment(message):
            return

        guild_id = message.guild.id
        channel = message.channel
        parent_id = getattr(channel, "parent_id", None)
        channel_ids = tuple(
            c for c in (channel.id, parent_id) if c is not None
        )
        booster = member.premium_since is not None

        def _claim():
            with self.ctx.open_db() as conn:
                settings = load_econ_settings(conn, guild_id)
                if not settings.enabled:
                    return None
                offset = get_tz_offset_hours(conn, guild_id)
                day = local_day_for(time.time(), offset)
                fired = fire_trigger_quests(
                    conn,
                    settings,
                    guild_id,
                    "media_post",
                    member.id,
                    local_day=day,
                    occurrence=str(message.id),
                    booster=booster,
                    channel_ids=channel_ids,
                )
                return settings, fired

        try:
            result = await asyncio.to_thread(_claim)
        except Exception:
            log.exception("econ media: claim failed in guild %s", guild_id)
            return
        if result is None:
            return
        settings, fired = result
        for _quest, outcome in fired:
            await self._announce_quest_claim(message, member, settings, outcome)

    qotd = app_commands.Group(
        name="qotd",
        description="Question of the day.",
        guild_only=True,
    )

    @qotd.command(
        name="post", description="Post today's question of the day (staff only)."
    )
    @app_commands.describe(
        question="The question to ask (leave blank to post the next sponsored one)"
    )
    async def qotd_post(
        self, interaction: discord.Interaction, question: str | None = None
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        guild_id = guild.id
        actor = interaction.user
        assert isinstance(actor, discord.Member)

        settings = await self._settings_or_refuse(interaction, guild_id)
        if settings is None:
            return
        if not _can_grant(actor, settings):
            await interaction.response.send_message(
                "❌ You don't have permission to post a question of the day.",
                ephemeral=True,
            )
            return

        channel = interaction.channel
        if not isinstance(channel, discord.abc.Messageable):
            await interaction.response.send_message(
                "❌ I can't post a question here.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        # No question typed → take the oldest approved sponsored one. The claim
        # is atomic and happens BEFORE the send, so two mods racing this can't
        # both post the same question; if the send then fails we release it
        # back to the queue rather than eating a member's paid slot.
        submission_id = 0
        sponsor_id = 0
        if question is None:
            queued = await asyncio.to_thread(self._claim_sponsored, guild_id)
            if queued is None:
                await interaction.followup.send(
                    "❌ No sponsored questions are waiting. Type a question to post "
                    "your own.",
                    ephemeral=True,
                )
                return
            submission_id = int(queued["id"])
            sponsor_id = int(queued["user_id"])
            question = str(queued["question"])

        # Prefer the rendered quote card; fall back to a plain branded embed if
        # there's no usable background image or the renderer raises.
        card_file: discord.File | None = None
        image_bytes = await _resolve_qotd_image(guild, self.bot)
        sponsor_name = ""
        if sponsor_id:
            sponsor = guild.get_member(sponsor_id)
            sponsor_name = sponsor.display_name if sponsor else ""
        byline = (
            f"Question of the Day · sponsored by {sponsor_name}"
            if sponsor_name
            else "Question of the Day"
        )

        if image_bytes is not None:
            try:
                card_bytes = await asyncio.to_thread(
                    render_quote_card,
                    question,
                    author_name=byline,
                    avatar_bytes=image_bytes,
                    theme=THEMES["midnight"],
                    pfp_shape="none",
                )
                card_file = discord.File(
                    io.BytesIO(card_bytes), filename=_QOTD_CARD_FILENAME
                )
            except Exception:
                log.exception("qotd: failed to render card in guild %s", guild_id)

        # Only the fallback embed is accented — the rendered card carries its
        # own palette — so the card path skips the lookup entirely. Resolved
        # before the try, as it was, so a failure here still can't be mistaken
        # for a failed send and release the claimed sponsored question.
        accent = (
            None
            if card_file is not None
            else await resolve_accent_color(self.ctx.db_path, guild)
        )

        content: str | None = None
        mentions = discord.AllowedMentions.none()
        if settings.qotd_ping_role_id:
            content = f"<@&{settings.qotd_ping_role_id}>"
            mentions = discord.AllowedMentions(roles=True)

        try:
            if card_file is not None:
                message = await channel.send(
                    content=content, file=card_file, allowed_mentions=mentions
                )
            else:
                embed = discord.Embed(
                    title="📣 Question of the Day",
                    description=question,
                    color=accent,
                )
                if sponsor_name:
                    embed.set_footer(text=f"Sponsored by {sponsor_name}")
                message = await channel.send(
                    content=content, embed=embed, allowed_mentions=mentions
                )
        except discord.Forbidden:
            # The send failed, so put a claimed sponsored question back on the
            # queue — the member paid for it and it hasn't run.
            if submission_id:
                await asyncio.to_thread(self._release_sponsored, submission_id)
            await interaction.followup.send(
                "❌ I don't have permission to post in this channel.", ephemeral=True
            )
            return
        except Exception:
            if submission_id:
                await asyncio.to_thread(self._release_sponsored, submission_id)
            raise

        posted_question = question

        def _record() -> None:
            with self.ctx.open_db() as conn:
                offset = get_tz_offset_hours(conn, guild_id)
                today = local_day_for(time.time(), offset)
                qotd_id = create_qotd(
                    conn,
                    guild_id,
                    channel.id,
                    message.id,
                    posted_question,
                    actor.id,
                    today,
                    sponsor_user_id=sponsor_id,
                )
                if submission_id:
                    attach_qotd(conn, submission_id, qotd_id)

        await asyncio.to_thread(_record)
        await interaction.followup.send(
            f"Posted {sponsor_name}'s sponsored question." if sponsor_name
            else "Posted the question of the day.",
            ephemeral=True,
        )

    # ── how-to guide panel ───────────────────────────────────────────────

    async def _require_economy_enabled(self, guild_id: int) -> None:
        """Raise with a reason the admin can act on when the economy is off.

        The panel route maps ValueError to a 400 carrying the message; returning
        None instead would surface as "Discord rejected the post", pointing at
        the wrong thing entirely.
        """
        settings = await asyncio.to_thread(self._load_settings, guild_id)
        if not settings.enabled:
            raise ValueError(
                "The economy is disabled for this server — turn it on under "
                "Economy → Settings before posting its panels."
            )

    async def post_guide_panel(self, guild, channel):
        """Place the how-to panel. Entry point for the dashboard's panel poster
        (``panel_registry``); replaced /bank post-guide on 2026-07-28.

        Edits in place when the panel is already this channel's, so a re-brand
        refresh doesn't hop it to the bottom. Returns None only when Discord
        refused the post.

        The disabled-economy check lives here rather than in the route because
        it is a domain rule, not an access rule: posting a currency guide for a
        currency that doesn't exist would be wrong however you got here. It
        *raises* rather than returning None so the admin is told to turn the
        economy on — a bare None reads as "Discord rejected it" and sends them
        to check bot permissions instead.
        """
        await self._require_economy_enabled(guild.id)
        return await self.guide_panel.place_or_refresh(guild, channel)

    # ── channel-bottom panels ────────────────────────────────────────────
    #
    # All four run on core.sticky.StickyPanel. Each supplies only: where its
    # ids live, and what it should look like. A panel is treated as unposted
    # while the economy is disabled, so a disabled guild never re-sticks.
    #
    # _PANEL_FIELDS covers the three permanent panels; the auction card keeps
    # its ids elsewhere and supplies its own callbacks (see below).

    _PANEL_FIELDS = {
        "guide": ("guide_channel_id", "guide_message_id"),
        "leaderboard": ("leaderboard_channel_id", "leaderboard_message_id"),
        "shop": ("shop_channel_id", "shop_message_id"),
    }

    def _panel_ids(self, guild_id: int, kind: str) -> tuple[int, int]:
        chan_field, msg_field = self._PANEL_FIELDS[kind]
        with self.ctx.open_db() as conn:
            s = load_econ_settings(conn, guild_id)
        if not s.enabled:
            return 0, 0
        return getattr(s, chan_field), getattr(s, msg_field)

    def _save_panel_ids(
        self, guild_id: int, kind: str, channel_id: int, message_id: int
    ) -> None:
        chan_field, msg_field = self._PANEL_FIELDS[kind]
        with self.ctx.open_db() as conn:
            save_econ_settings(
                conn, guild_id, {chan_field: channel_id, msg_field: message_id}
            )

    def _bounty_panel_ids(self, guild_id: int) -> tuple[int, int]:
        """Where the hub is, or (0, 0) for "unposted" (the restick no-ops).

        (0, 0) in three cases: the economy is off; no board channel is set (the
        same condition ``bounty_enabled`` gates the whole feature on); or the
        hub was posted in a channel that is no longer the board.

        That last one is why the channel is stored rather than assumed. An
        admin can repoint ``bounty_channel_id`` at any time from the dashboard,
        and that save doesn't touch the panel ids. Pairing the old message id
        with the new channel would have the restick edit-404, post a *second*
        hub, and fail to delete the first — leaving two live hubs, the stale
        one listing bounties whose cards now post elsewhere.
        """
        with self.ctx.open_db() as conn:
            s = load_econ_settings(conn, guild_id)
        if not s.enabled or not bounty_enabled(s):
            return 0, 0
        if int(s.bounty_panel_channel_id) != int(s.bounty_channel_id):
            return 0, 0
        return int(s.bounty_panel_channel_id), int(s.bounty_panel_message_id)

    def _save_bounty_panel_ids(
        self, guild_id: int, channel_id: int, message_id: int
    ) -> None:
        """Persist where the hub landed — its own fields, never the board's.

        Writing ``bounty_channel_id`` here would let a mis-posted hub silently
        *redefine* which channel the board is, moving every future bounty card
        with it; ``post_bounty_panel`` refuses any channel but the board, so
        these two agree on every normal path and diverge only after a repoint.
        """
        with self.ctx.open_db() as conn:
            save_econ_settings(
                conn,
                guild_id,
                {
                    "bounty_panel_channel_id": channel_id,
                    "bounty_panel_message_id": message_id,
                },
            )

    async def _drop_stale_bounty_hub(self, guild: discord.Guild) -> None:
        """Delete a hub left behind in a channel that is no longer the board.

        Its buttons are static custom_ids, so an orphaned hub keeps working —
        Post a bounty on it would file a card into the *new* board channel.
        Called when the panel is (re)posted, which is the moment an admin who
        just repointed the board is here to see it cleaned up. Best-effort:
        the ids are cleared either way so this can't be retried forever.
        """
        def _read() -> tuple[int, int, int]:
            with self.ctx.open_db() as conn:
                s = load_econ_settings(conn, guild.id)
                return (
                    int(s.bounty_panel_channel_id),
                    int(s.bounty_panel_message_id),
                    int(s.bounty_channel_id),
                )

        old_channel, old_message, board = await asyncio.to_thread(_read)
        if not old_message or old_channel == board:
            return
        channel = guild.get_channel(old_channel)
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.get_partial_message(old_message).delete()
            except discord.HTTPException:
                log.debug("econ bounty: stale hub already gone", exc_info=True)
        await asyncio.to_thread(self._save_bounty_panel_ids, guild.id, 0, 0)

    async def _build_bounty_panel(self, guild: discord.Guild) -> PanelContent:
        embed, view = await build_bounty_hub_panel(self.bot, guild)
        return PanelContent(embed=embed, view=view)

    async def post_bounty_panel(self, guild, channel):
        """Place the Bounty Board hub. Entry point for the dashboard's panel
        poster (``panel_registry``); the board's only entry point since
        ``/bounty`` was deleted.

        Refuses any channel but the configured board, because the hub's ids are
        looked up *through* ``bounty_channel_id`` — a hub somewhere else would
        be adopted by the restick as though it were in the board channel and
        then edited into the wrong place. Raising sends the admin to set the
        board channel first, which is the step that enables bounties at all.
        """
        await self._require_economy_enabled(guild.id)
        settings = await asyncio.to_thread(self._load_settings, guild.id)
        if not bounty_enabled(settings):
            raise ValueError(
                "No bounty board channel is set — pick one under Economy → "
                "Settings before posting the board panel."
            )
        if int(channel.id) != int(settings.bounty_channel_id):
            raise ValueError(
                "The Bounty Board panel has to go in the bounty board channel "
                f"(<#{int(settings.bounty_channel_id)}>) — that's the channel "
                "its cards post to."
            )
        # If the board was repointed, the hub still sitting in the old channel
        # has live buttons. Clear it out before placing the new one.
        await self._drop_stale_bounty_hub(guild)
        return await self.bounty_panel.place_or_refresh(guild, channel)

    async def refresh_bounty_hub_panel(self, guild: discord.Guild) -> None:
        """Repaint the hub after a pot or the open list changed.

        A no-op until the hub has actually been posted: ``place_or_refresh``
        would otherwise *create* one, so a guild that never set the board up
        would sprout a panel the first time somebody chipped in.
        """
        channel_id, message_id = await asyncio.to_thread(
            self._bounty_panel_ids, guild.id
        )
        if not channel_id or not message_id:
            return
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        await self.bounty_panel.place_or_refresh(guild, channel)

    async def _build_guide_panel(self, guild: discord.Guild) -> PanelContent:
        settings = await asyncio.to_thread(self._load_settings, guild.id)
        accent = await resolve_accent_color(self.ctx.db_path, guild)
        return PanelContent(
            embed=build_guide_embed(settings, color=accent), view=GuideView()
        )

    async def _build_leaderboard_panel(self, guild: discord.Guild) -> PanelContent:
        now_ts = time.time()

        def _collect():
            # Settings ride the connection the board data already needs; this
            # runs on every sticky repost, so a second connect+PRAGMA for one
            # settings row was the most-repeated waste in the cog.
            with self.ctx.open_db() as conn:
                settings = load_econ_settings(conn, guild.id)
                data = collect_leaderboard_data(conn, guild.id, now_ts)
                known = get_known_users_bulk(
                    conn, guild.id, [uid for uid, _ in data.top_earners]
                )
            return settings, data, known

        settings, data, known = await asyncio.to_thread(_collect)

        def _name(uid: int) -> str:
            member = guild.get_member(uid)
            if member:
                return member.display_name
            return known.get(uid) or f"User {uid}"

        accent = await resolve_accent_color(self.ctx.db_path, guild)
        return PanelContent(
            embed=build_leaderboard_embed(
                settings, data, _name, now_ts=now_ts, color=accent
            ),
            view=QuestBoardView(),
        )

    async def _build_shop_panel_embed(
        self,
        guild: discord.Guild,
        settings: EconSettings,
        icon_range: tuple[int, int, int] | None,
    ) -> discord.Embed:
        """The channel shop panel's embed with current gating, icon prices and
        accent. Every route to the panel — ``/bank post-shop`` and the sticky
        repost alike — arrives through ``_build_shop_panel``, so the two can't
        render different panels.

        ``icon_range`` is read by the caller on the connection it already
        holds; ``None`` means this guild has no icon catalog.
        """
        gated = await self._gated_perks(guild.id)
        accent = await resolve_accent_color(self.ctx.db_path, guild)
        return build_shop_embed(
            settings, gated, accent, panel=True, icon_catalog=icon_range
        )

    async def _build_shop_panel(self, guild: discord.Guild) -> PanelContent:
        def _read() -> tuple[EconSettings, tuple[int, int, int] | None]:
            # One connection for both, like _build_leaderboard_panel — this
            # runs on every debounced sticky repost.
            with self.ctx.open_db() as conn:
                return (
                    load_econ_settings(conn, guild.id),
                    catalog_price_range(conn, guild.id),
                )

        settings, icon_range = await asyncio.to_thread(_read)
        return PanelContent(
            embed=await self._build_shop_panel_embed(guild, settings, icon_range),
            view=ShopPanelView(),
        )

    # ── the auction card's sticky callbacks ──────────────────────────────
    #
    # Unlike the three panels above, these ids live on the auction row rather
    # than in econ settings — the card belongs to one auction, not to the
    # guild — so they go through the service instead of _PANEL_FIELDS.

    def _auction_card_ids(self, guild_id: int) -> tuple[int, int]:
        with self.ctx.open_db() as conn:
            if not load_econ_settings(conn, guild_id).enabled:
                return 0, 0
            return card_ids(conn, guild_id)

    def _save_auction_card_ids(
        self, guild_id: int, channel_id: int, message_id: int
    ) -> None:
        with self.ctx.open_db() as conn:
            attach_card_to_latest(conn, guild_id, channel_id, message_id)

    async def _build_auction_panel(self, guild: discord.Guild) -> PanelContent:
        content = await build_auction_panel(self.bot, guild)
        if content is None:
            # The auction finished between this restick being armed and the
            # placement reaching build. Raising is the hard stop that keeps
            # place() from posting a card for an auction that is over — build
            # runs before send, so nothing is posted. The close paths call
            # _release_panel the moment the state flips, which normally makes
            # the restick return earlier than this; getting here is the narrow
            # residual race, and core logs it at error level.
            raise RuntimeError(f"auction finished before its card was rebuilt "
                               f"(guild {guild.id})")
        return content

    @commands.Cog.listener("on_message")
    async def _restick_panels(self, message: discord.Message) -> None:
        """Keep all five panels at the bottom of their channels.

        One listener for five panels: each ignores activity outside its own
        channel, so a message usually arms at most one repost. The auction
        card is the exception — it can share a channel with one of the other
        three, in which case both re-stick and one ends up second. That is
        why ``/bank auction start`` warns the mod before it happens (see
        ``sticky_panel_channels``); it is a bad configuration, not a bug.

        The bounty hub is bot-message-blind like the rest (``restick_on_bot``
        is off), so a burst of new bounty cards leaves it stranded up-channel
        until a human posts. Deliberate — see the panel's declaration.
        """
        for panel in (
            self.guide_panel,
            self.leaderboard_panel,
            self.shop_panel,
            self.auction_panel,
            self.bounty_panel,
        ):
            await panel.on_message(message)

    # ── auto-updating leaderboard panel ──────────────────────────────────

    async def post_leaderboard_panel(self, guild, channel):
        """Place the auto-updating leaderboard panel. See post_guide_panel."""
        await self._require_economy_enabled(guild.id)
        return await self.leaderboard_panel.place_or_refresh(guild, channel)

    # ── persistent shop panel ────────────────────────────────────────────

    async def post_shop_panel(self, guild, channel):
        """Place the perk-shop panel. See post_guide_panel."""
        await self._require_economy_enabled(guild.id)
        return await self.shop_panel.place_or_refresh(guild, channel)

    async def cog_load(self) -> None:
        # Re-register the persistent buttons so clicks on existing messages
        # still route after a restart — the custom_ids carry the state
        # (econ_claim:{approve,deny}:<id>, econ_shop_panel:<perk> on
        # pre-Open-Shop panels, econ_qotd_sub:{approve,deny}:<id>).
        self.bot.add_dynamic_items(
            QuestApproveButton,
            QuestDenyButton,
            ShopRentButton,
            SponsorApproveButton,
            SponsorDenyButton,
            PinApproveButton,
            PinDenyButton,
            BountyChipInButton,
            BountyAwardButton,
            BountyCancelButton,
            # The hub's two buttons carry no per-message state, but they are
            # dynamic items too so the whole bounty surface registers here.
            BountyHubPostButton,
            BountyHubChipButton,
            AuctionBidButton,
        )
        # The guide panel's 🔔 toggle, the quest board's "Show my quests"
        # button, and the shop panel's Open Shop button carry no per-message
        # state, so they are plain static-custom_id views, not dynamic items.
        self.bot.add_view(GuideView())
        self.bot.add_view(QuestBoardView())
        self.bot.add_view(ShopPanelView())
        self._auction_settle_loop.start()

    def _load_settings(self, guild_id: int) -> EconSettings:
        with self.ctx.open_db() as conn:
            return load_econ_settings(conn, guild_id)

    def _claim_sponsored(self, guild_id: int):
        with self.ctx.open_db() as conn:
            return claim_next_approved(conn, guild_id)

    def _release_sponsored(self, submission_id: int) -> None:
        with self.ctx.open_db() as conn:
            release_claim(conn, submission_id)


async def setup(bot: Bot) -> None:
    await bot.add_cog(EconomyCog(bot, bot.ctx))
