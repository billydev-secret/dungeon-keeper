"""Economy — the ``/bank`` command surface (wallet view + mod grants).

Thin cog over ``bot_modules.services.economy_service``: it loads per-guild
``econ_`` settings on each interaction (cheap KV reads, no cache for stage 0),
resolves the branded currency naming, and renders the accent-colored embeds.
See docs/economy_spec.md for the feature design.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.branding import resolve_accent_color
from bot_modules.core.db_utils import get_tz_offset_hours
from bot_modules.economy.guide import build_guide_embed, should_restick_guide
from bot_modules.economy.leaderboard import (
    _pad,
    build_leaderboard_embed,
    collect_leaderboard_data,
    progress_bar,
)
from bot_modules.economy.logic import local_day_for
from bot_modules.economy.perk_actions import (
    apply_role_perks,
    feature_gate_ok,
    find_color_clash,
    parse_hex_color,
    revoke_role_perks,
)
from bot_modules.economy.quest_views import (
    QuestApproveButton,
    QuestClaimView,
    QuestDenyButton,
    can_manage_economy,
    post_signoff_card,
)
from bot_modules.economy.quests import (
    compile_trigger_pattern,
    has_board,
    iso_week_for,
    message_matches_trigger,
    parse_trigger_words,
    quest_period,
)
from bot_modules.services.economy_quests_service import (
    assigned_board_ids,
    claim_quest,
    fire_trigger_inline,
    fire_trigger_quests,
    get_progress,
    list_trigger_quests,
    reroll_quote,
    resolve_member_target,
    source_enabled,
    spotlight_kind,
)
from bot_modules.services.economy_icon_catalog_service import (
    catalog_price_range,
    get_catalog_icon,
    list_catalog,
)
from bot_modules.services.economy_rentals_service import (
    cancel_all_for_member,
    entitlements,
    get_live_role_icon_rental,
    list_member_rentals,
    rent_perk,
    set_rental_catalog_icon,
    upsert_personal_role,
)
from bot_modules.services.economy_service import (
    EconSettings,
    apply_credit,
    create_qotd,
    get_balance,
    get_ledger,
    get_notify_muted,
    load_econ_settings,
    notify_member,
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
_QOTD_CARD_FILENAME = "qotd.png"

# Transfers above this need an explicit confirm step (spec §5, "over 100").
_PAY_CONFIRM_THRESHOLD = 100
_MAX_ROLE_NAME_LEN = 32
_MAX_MEMO_LEN = 100
# Memos are shortened further in the one-line wallet render, and the joined
# field is bounded — Discord rejects an embed field over 1024 chars.
_WALLET_MEMO_LEN = 40
_EMBED_FIELD_LIMIT = 1024
_MAX_ICON_BYTES = 256 * 1024

# Human labels for the rentable perks (shop rows, wallet field, DMs).
_PERK_LABELS = {
    "role_color": "Custom role color",
    "role_name": "Custom role name",
    "role_icon": "Role icon",
    "role_gradient": "Gradient role",
    "gift_color": "Gift-a-color",
}
# The perks a member rents for themselves, in shop display order.
_SELF_PERKS = ("role_color", "role_name", "role_gradient", "role_icon")
# Feature-gated perks and the friendly reason shown when the gate is closed.
_FEATURE_GATED = ("role_gradient", "role_icon")

# Shop-table furniture. The full `_PERK_LABELS` names are too wide for an
# aligned two-cell row, so the shop uses a short cell label plus a one-line
# blurb — most members have never seen a gradient role and can't price what
# they can't picture. Blurbs stay under ~27 chars so a row survives mobile.
_PERK_SHORT = {
    "role_color": "Color",
    "role_name": "Name",
    "role_gradient": "Gradient",
    "role_icon": "Icon",
    "gift_color": "Gift",
}
_PERK_BLURBS = {
    "role_color": "one solid color, your pick",
    "role_name": "call yourself anything",
    "role_gradient": "two-color fade on your name",
    "role_icon": "a badge beside your name",
    "gift_color": "buy someone a role color",
}
_PERK_EMOJI = {
    "role_color": "🎨",
    "role_name": "✨",
    "role_gradient": "🌈",
    "role_icon": "🖼️",
    "gift_color": "🎁",
}
# Self-perks grouped into a price ladder — cheap everyday tweaks first, the
# showy ones second — so the shop reads as tiers to climb rather than a flat
# spreadsheet. Rows sort by price inside each tier at render time, since
# prices are guild-configurable and can reorder.
_PERK_TIERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Essentials", ("role_name", "role_color")),
    ("Signature", ("role_gradient", "role_icon")),
)


def _perk_price(settings: EconSettings, perk: str) -> int:
    return int(getattr(settings, f"price_{perk}"))


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


def _rental_lines(settings: EconSettings, rentals: list, user_id: int) -> list[str]:
    """One line per live rental for the wallet's 'Active rentals' field."""
    emoji = settings.currency_emoji
    lines: list[str] = []
    for r in rentals:
        perk = str(r["perk"])
        label = _PERK_LABELS.get(perk, perk)
        price = int(r["price"])
        next_bill = int(r["next_bill_at"])
        owner_id = int(r["user_id"])
        beneficiary_id = int(r["beneficiary_id"])
        attribution = ""
        if perk == "gift_color":
            if beneficiary_id == user_id and owner_id != user_id:
                attribution = " (gift received)"
            elif owner_id == user_id and beneficiary_id != user_id:
                attribution = f" (gift to <@{beneficiary_id}>)"
        grace = " · ⏳ in grace" if str(r["state"]) == "grace" else ""
        lines.append(
            f"**{label}**{attribution} — {emoji} {price:,}/wk · "
            f"renews <t:{next_bill}:R>{grace}"
        )
    return lines


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


def _unit(settings: EconSettings, amount: int) -> str:
    """Currency name matching ``amount``'s grammatical number."""
    return settings.currency_name if abs(amount) == 1 else settings.currency_plural


# Group order + headings for the /bank quests table. The long per-state
# explainer text lives in quest_views.QUEST_STATE_LABEL, shown by the
# details select — the list itself stays one line per quest.
_QUEST_GROUPS = (
    ("daily", "Daily"),
    ("weekly", "Weekly"),
    ("monthly", "Monthly"),
    ("event", "Anytime"),
    ("community", "Community goals"),
)


def _quest_line_status(q: dict) -> str:
    """The status column: one short glyph phrase, or n/target progress."""
    state = str(q.get("state") or "")
    if state == "community":
        return f"▸ {int(q['current']):,}/{int(q['target']):,}"
    if state == "done":
        return "✅ done"
    if state == "pending":
        return "⏳ sign-off"
    if state == "claimable":
        return "🔶 claim below"
    if q.get("progress_target"):
        return f"▸ {int(q['progress_current']):,}/{int(q['progress_target']):,}"
    return "☐ to do"


def _quest_line_reward(q: dict, settings: EconSettings) -> str:
    """The payment column: coins, optional XP, optional spotlight bolt."""
    reward = f"{settings.currency_emoji} {int(q['reward']):,}"
    if q.get("reward_xp"):
        reward += f" +⭐{int(q['reward_xp']):,}xp"
    if q.get("spotlight"):
        reward += " ⚡"
    return reward

# Trigger-quest cache staleness bound: a dashboard edit takes effect on the
# next message after at most this many seconds.
_TRIGGER_CACHE_TTL = 60.0

# Guide-panel sticky: how long the cached (channel_id, message_id) of the guide
# panel is trusted before re-reading it from config on the next message, and how
# long to wait for the channel to fall quiet before reposting the panel at the
# bottom. The delay coalesces bursts into a single repost — a busy channel keeps
# resetting the timer, so the panel only re-sticks once activity pauses.
_GUIDE_STICKY_CACHE_TTL = 300.0
_GUIDE_STICKY_DELAY = 6.0


@dataclass(frozen=True)
class _TriggerQuest:
    """One active trigger-word quest, pre-compiled for the message listener."""

    quest_id: int
    qtype: str
    title: str
    signoff: bool
    channel_id: int | None  # None = any channel counts
    pattern: re.Pattern[str]
    reward_xp: int


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


# A text meter for a community quest's running total — shared with the
# leaderboard panel so the two surfaces render one way.
_progress_bar = progress_bar


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


def _ellipsis(text: str, limit: int = _WALLET_MEMO_LEN) -> str:
    """Shorten a memo for the cramped one-line wallet render."""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _fit_lines(lines: list[str], limit: int = _EMBED_FIELD_LIMIT) -> str:
    """Join as many leading lines as fit an embed field.

    Memos make each activity row variable-length, so ten of them can overrun
    the 1024-char field cap and make Discord reject the whole wallet embed.
    Dropping the oldest rows keeps the newest visible rather than 400-ing.
    """
    out: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + (1 if out else 0)
        if used + cost > limit:
            break
        out.append(line)
        used += cost
    return "\n".join(out)


def _memo_of(row_meta: str | None) -> str | None:
    """Pull the memo out of a ledger row's meta JSON, tolerating junk."""
    if not row_meta:
        return None
    try:
        memo = json.loads(row_meta).get("memo")
    except (ValueError, TypeError, AttributeError):
        return None
    return memo if isinstance(memo, str) and memo else None


class _PayConfirmView(discord.ui.View):
    """Ephemeral Confirm/Cancel gate for a transfer over the threshold."""

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
        super().__init__(timeout=60)
        self.cog = cog
        self.settings = settings
        self.guild = guild
        self.sender = sender
        self.recipient = recipient
        self.amount = amount
        self.memo = memo

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.sender.id:
            await interaction.response.send_message(
                "This confirmation isn't yours.", ephemeral=True
            )
            return False
        return True

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


class _RoleNameModal(discord.ui.Modal, title="Custom role name"):
    text = discord.ui.TextInput(
        label="Role name",
        min_length=1,
        max_length=_MAX_ROLE_NAME_LEN,
        placeholder="Shown in your profile and the member list",
    )

    def __init__(self, cog: EconomyCog) -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.set_role_name(interaction, str(self.text.value))


class _RoleColorModal(discord.ui.Modal, title="Custom role color"):
    hex_value = discord.ui.TextInput(
        label="Hex color", min_length=3, max_length=9, placeholder="#7B2FF7"
    )

    def __init__(self, cog: EconomyCog) -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.set_role_color(interaction, str(self.hex_value.value))


class _RoleGradientModal(discord.ui.Modal, title="Gradient role"):
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


class _RoleIconModal(discord.ui.Modal, title="Role icon"):
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


# Which modal customises which perk; gift_color shares the color modal.
_CFG_MODALS = {
    "role_name": _RoleNameModal,
    "role_color": _RoleColorModal,
    "role_gradient": _RoleGradientModal,
    "role_icon": _RoleIconModal,
}

# Short button labels for the customise flows (the perk label is on the row).
_CUSTOMISE_LABELS = {
    "role_color": "Set color",
    "role_name": "Set name",
    "role_gradient": "Set gradient",
    "role_icon": "Set icon",
}


# Discord caps a select at 25 options; a larger catalog shows its first 25
# (by sort order) and tells the member the list was trimmed.
_ICON_SELECT_LIMIT = 25


class _IconCatalogSelect(discord.ui.Select):
    """A picker of curated role icons; choosing one rents or switches to it."""

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
        super().__init__(
            placeholder="Choose a role icon…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.cog = cog
        self.guild = guild

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.pick_catalog_icon(
            interaction, self.guild, int(self.values[0])
        )


class _IconCatalogView(discord.ui.View):
    """Ephemeral catalog picker, scoped to the member who opened the shop."""

    def __init__(
        self,
        cog: EconomyCog,
        settings: EconSettings,
        guild: discord.Guild,
        user_id: int,
        icons: list[dict],
    ) -> None:
        super().__init__(timeout=120)
        self.user_id = user_id
        self.add_item(_IconCatalogSelect(cog, settings, guild, icons))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Open your own shop with /bank shop.", ephemeral=True
            )
            return False
        return True


class _ShopView(discord.ui.View):
    """One button per self-perk: Rent when unowned, a customise modal when owned.

    Feature-gated rows are disabled either way. A member holding only a
    *gifted* color gets an extra "Set gifted color" button, since the
    role_color row still shows Rent for them.
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
    ) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.settings = settings
        self.guild = guild
        self.user_id = user_id
        for perk in _SELF_PERKS:
            if perk == "role_icon" and has_catalog:
                # A curated catalog replaces the rent/customise buttons with a
                # single picker — renting and restyling both happen by choosing
                # an icon (each carries its own price).
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
            if perk in owned:
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
                    label=f"{_PERK_EMOJI[perk]} {_PERK_SHORT[perk]}",
                    style=discord.ButtonStyle.primary,
                    disabled=perk in gated,
                    custom_id=f"econ_shop_rent:{perk}",
                )
                button.callback = self._make_rent_callback(perk)
            self.add_item(button)
        if "gift_color" in owned and "role_color" not in owned:
            button = discord.ui.Button(
                label="Set gifted color",
                style=discord.ButtonStyle.success,
                custom_id="econ_shop_cfg:gift_color",
            )
            button.callback = self._make_cfg_callback("role_color")
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Open your own shop with /bank shop.", ephemeral=True
            )
            return False
        return True

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

    Shared by the ephemeral /bank shop view and the persistent channel panel —
    every reply is ephemeral to the clicker, and a successful rent carries the
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

            def _bal() -> int:
                with ctx.open_db() as conn:
                    return get_balance(conn, guild.id, user_id)

            bal = await asyncio.to_thread(_bal)
            text = (
                f"You need {settings.currency_emoji} "
                f"{_perk_price(settings, perk):,} but only have {bal:,}."
            )
        elif "already rented" in msg:
            text = "You're already renting that perk."
        else:
            text = "That perk isn't available."
        await interaction.response.send_message(text, ephemeral=True)
        return

    await apply_role_perks(cog.bot, ctx.db_path, guild.id, user_id)
    note = (
        " (For an image icon, upload one with `/bank role icon`.)"
        if perk == "role_icon"
        else ""
    )
    await interaction.response.send_message(
        f"Rented **{_PERK_LABELS[perk]}**! Set it up right here:{note}",
        view=_PostRentView(cog, perk),
        ephemeral=True,
    )


class ShopRentButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_shop_panel:(?P<perk>[a-z_]+)"),
):
    """Persistent shop-panel rent button; ``custom_id`` carries the perk.

    Unlike the ephemeral /bank shop view, settings and the feature gate are
    re-read on every click — the panel can sit in a channel for months, so
    nothing rendered at post time is trusted at click time. Labels carry no
    price (the embed's table does), so re-pricing only needs the embed
    refreshed, not the buttons re-labelled.
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
                label=label or f"Rent {_PERK_LABELS.get(perk, perk)}",
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
        guild = interaction.guild
        if guild is None or self.perk not in _SELF_PERKS:
            await interaction.response.send_message(
                "That perk isn't available.", ephemeral=True
            )
            return
        bot = cast("Bot", interaction.client)
        ctx = bot.ctx

        def _load() -> EconSettings:
            with ctx.open_db() as conn:
                return load_econ_settings(conn, guild.id)

        settings = await asyncio.to_thread(_load)
        if not settings.enabled:
            await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
            return
        if self.perk in _FEATURE_GATED and not await feature_gate_ok(
            bot, guild.id, self.perk
        ):
            await interaction.response.send_message(
                "That perk needs a server feature that isn't enabled here.",
                ephemeral=True,
            )
            return
        cog = cast("EconomyCog | None", bot.get_cog("EconomyCog"))
        if cog is None:  # cog unloaded mid-flight; the panel button outlives it
            await interaction.response.send_message(
                "That perk isn't available right now.", ephemeral=True
            )
            return
        if self.perk == "role_icon" and await asyncio.to_thread(
            cog._has_catalog, guild.id
        ):
            await cog.open_icon_catalog(interaction, settings, guild)
            return
        await _rent_perk_flow(interaction, cog, settings, guild, self.perk)


def _shop_row_price(
    settings: EconSettings,
    perk: str,
    icon_catalog: tuple[int, int, int] | None,
) -> tuple[int, str]:
    """(sort key, display string) for a shop row's price.

    A curated icon catalog prices per icon, so the role-icon row shows the
    catalog's span and sorts on its floor.
    """
    if perk == "role_icon" and icon_catalog is not None:
        lo, hi, _count = icon_catalog
        return lo, f"{lo:,}" if lo == hi else f"{lo:,}–{hi:,}"
    price = _perk_price(settings, perk)
    return price, f"{price:,}"


def _build_shop_embed(
    settings: EconSettings,
    gated: set[str],
    accent: discord.Color | None,
    *,
    panel: bool = False,
    owned: set[str] | frozenset[str] = frozenset(),
    icon_catalog: tuple[int, int, int] | None = None,
    balance: int | None = None,
) -> discord.Embed:
    """The shop listing, shared by /bank shop and the channel panel.

    Rendered as the aligned code-cell table the leaderboard, guide and quest
    panels use: ``label`` | ``blurb`` | price, grouped into price tiers. Five
    ``inline=False`` fields carrying four words each read as an airy list;
    a table reads as a storefront.

    ``owned`` marks the viewer's rented rows and ``balance`` puts their wallet
    in the footer — both only meaningful for the ephemeral per-member view;
    the channel panel is member-agnostic and passes neither.
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
            "Tap a button to rent — the reply is private to you."
            if panel
            else "Green buttons customise what you've already rented."
        )
        + "\n​"
    )
    embed = discord.Embed(
        title="🛍️ Perk shop", description=description, color=accent
    )

    # One width per table, not per tier, so cells line up across the whole
    # embed rather than jumping at each heading.
    rows = [*_SELF_PERKS, "gift_color"]
    label_width = max(len(_PERK_SHORT[p]) for p in rows)
    blurb_width = max(len(_PERK_BLURBS[p]) for p in rows)

    def _line(perk: str) -> str:
        _sort, price_str = _shop_row_price(settings, perk, icon_catalog)
        note = ""
        if perk in gated:
            note = " · _needs a server feature not enabled here_"
        elif perk in owned:
            note = " · ✅"
        elif perk == "role_icon" and icon_catalog is not None:
            note = f" · {icon_catalog[2]} to pick from"
        return (
            f"`{_pad(_PERK_SHORT[perk], label_width)}` "
            f"`{_pad(_PERK_BLURBS[perk], blurb_width)}` "
            f"{settings.currency_emoji} **{price_str}**{note}"
        )

    for tier_name, perks in _PERK_TIERS:
        ordered = sorted(
            perks, key=lambda p: _shop_row_price(settings, p, icon_catalog)[0]
        )
        embed.add_field(
            name=tier_name,
            value="\n".join(_line(p) for p in ordered) + "\n​",
            inline=False,
        )
    embed.add_field(
        name="For a friend",
        value=f"{_line('gift_color')}\nSend it with `/bank gift`.",
        inline=False,
    )

    embed.set_footer(
        text=(
            "Prices are per week, billed every 7 days. A short grace period "
            "covers a missed renewal."
        )
    )
    return embed


def _shop_panel_view(
    settings: EconSettings,
    gated: set[str],
    *,
    has_catalog: bool = False,
) -> discord.ui.View:
    """A never-expiring view of ShopRentButtons, priced at post time.

    With a curated icon catalog, the role-icon button becomes a catalog opener
    (its click routes to the picker); its custom_id is unchanged so existing
    panels keep working across a restart.
    """
    view = discord.ui.View(timeout=None)
    for perk in _SELF_PERKS:
        if perk == "role_icon" and has_catalog:
            view.add_item(
                ShopRentButton(
                    perk,
                    label="🖼️ Browse icons",
                    style=discord.ButtonStyle.secondary,
                    disabled=perk in gated,
                )
            )
            continue
        view.add_item(
            ShopRentButton(
                perk,
                label=f"{_PERK_EMOJI[perk]} {_PERK_SHORT[perk]}",
                disabled=perk in gated,
            )
        )
    return view


class EconomyCog(commands.Cog):
    bank = app_commands.Group(
        name="bank",
        description="Wallet and currency commands.",
        guild_only=True,
    )
    role = app_commands.Group(
        name="role",
        description="Personal role extras (customise your perks in /bank shop).",
        parent=bank,
    )

    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        # guild_id → (monotonic expiry, trigger quests). TTL-refreshed in the
        # message listener; empty lists are cached too so guilds without
        # trigger quests cost one dict lookup per message.
        self._trigger_cache: dict[int, tuple[float, list[_TriggerQuest]]] = {}
        # Guide-panel sticky. `_guide_ref` caches guild_id → (monotonic expiry,
        # channel_id, message_id) of the posted panel (0/0 when none), so the
        # message listener costs a dict lookup, not a DB read, per message.
        # `_restick_tasks` holds the pending debounced repost per guild;
        # `_guide_locks` serialises a repost against a concurrent /bank
        # post-guide so the panel can't double-post.
        self._guide_ref: dict[int, tuple[float, int, int]] = {}
        self._restick_tasks: dict[int, asyncio.Task[None]] = {}
        self._guide_locks: dict[int, asyncio.Lock] = {}
        # Photo Challenge game options, TTL-cached so the reaction/auto-react
        # listeners cost a dict lookup, not a DB read, per event:
        # guild_id → (monotonic expiry, (channel_id, threshold, auto_react)).
        self._photo_opts: dict[int, tuple[float, tuple[int, int, str]]] = {}
        # message_ids we've already paid a photo_react for this process — bounds
        # the (Discord-API-heavy) distinct-reactor recount once a post has paid.
        # The DB claim collision is the durable guard; this just avoids re-work.
        self._photo_paid: set[int] = set()
        super().__init__()

    async def cog_unload(self) -> None:
        for task in self._restick_tasks.values():
            task.cancel()
        self._restick_tasks.clear()

    @bank.command(name="wallet", description="Check your balance and recent activity.")
    async def bank_wallet(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        guild_id = guild.id
        user_id = interaction.user.id

        def _load() -> tuple[EconSettings, int, list, list]:
            with self.ctx.open_db() as conn:
                settings = load_econ_settings(conn, guild_id)
                balance = get_balance(conn, guild_id, user_id)
                ledger = get_ledger(conn, guild_id, user_id, limit=10)
                rentals = list_member_rentals(conn, guild_id, user_id)
            return settings, balance, ledger, rentals

        settings, balance, ledger, rentals = await asyncio.to_thread(_load)

        if not settings.enabled:
            await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
            return

        accent = await resolve_accent_color(self.ctx.db_path, guild)
        embed = discord.Embed(
            title=settings.wallet_name,
            description=(
                f"{settings.currency_emoji} **{balance:,}** {_unit(settings, balance)}"
            ),
            color=accent,
        )
        if settings.currency_icon_url:
            embed.set_thumbnail(url=settings.currency_icon_url)

        if ledger:
            lines = []
            for row in ledger:
                amount = int(row["amount"])
                sign = "+" if amount >= 0 else "-"
                ts = int(row["created_at"])
                line = (
                    f"{sign}{abs(amount):,} {settings.currency_emoji} · "
                    f"{row['kind']} · <t:{ts}:R>"
                )
                memo = _memo_of(row["meta"])
                if memo:
                    line += f" — *{discord.utils.escape_markdown(_ellipsis(memo))}*"
                lines.append(line)
            embed.add_field(
                name="Recent activity", value=_fit_lines(lines), inline=False
            )
        else:
            embed.add_field(
                name="Recent activity", value="_No activity yet._", inline=False
            )

        rental_lines = _rental_lines(settings, rentals, user_id)
        if rental_lines:
            embed.add_field(
                name="Active rentals", value="\n".join(rental_lines), inline=False
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

        settings = await asyncio.to_thread(self._load_settings, guild_id)

        if not settings.enabled:
            await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
            return

        if not _can_grant(actor, settings):
            await interaction.response.send_message(
                "You don't have permission to grant currency.", ephemeral=True
            )
            return

        if member.bot:
            await interaction.response.send_message(
                "Bots don't have wallets.", ephemeral=True
            )
            return

        if amount < 1:
            await interaction.response.send_message(
                "The amount must be at least 1.", ephemeral=True
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
            title="Currency granted",
            description=(
                f"{settings.currency_emoji} **{credited:,}** {_unit(settings, credited)} "
                f"→ {member.mention}"
            ),
            color=accent,
        )
        if booster and credited != amount:
            embed.add_field(
                name="Booster bonus",
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

        settings = await asyncio.to_thread(self._load_settings, guild_id)
        if not settings.enabled:
            await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
            return

        def _toggle() -> bool:
            with self.ctx.open_db() as conn:
                new_muted = not get_notify_muted(conn, guild_id, user_id)
                set_notify_muted(conn, guild_id, user_id, new_muted)
                return new_muted

        muted = await asyncio.to_thread(_toggle)

        accent = await resolve_accent_color(self.ctx.db_path, guild)
        embed = discord.Embed(
            title="Notifications muted" if muted else "Notifications on",
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

        settings = await asyncio.to_thread(self._load_settings, guild.id)
        if not settings.enabled:
            await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
            return
        if not settings.transfers_enabled:
            await interaction.response.send_message(
                "Transfers are turned off on this server.", ephemeral=True
            )
            return
        if member.bot:
            await interaction.response.send_message(
                "Bots don't have wallets.", ephemeral=True
            )
            return
        if member.id == sender.id:
            await interaction.response.send_message(
                "You can't pay yourself.", ephemeral=True
            )
            return
        if amount < 1:
            await interaction.response.send_message(
                "The amount must be at least 1.", ephemeral=True
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
                title="Confirm payment", description=desc, color=accent
            )
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
        embed = discord.Embed(title="Payment sent", description=desc, color=accent)
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
        assert interaction.guild is not None
        guild = interaction.guild
        user_id = interaction.user.id

        settings, owned, balance = await asyncio.to_thread(
            self._load_role_ctx, guild.id, user_id
        )
        if not settings.enabled:
            await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
            return

        gated: set[str] = set()
        for perk in _FEATURE_GATED:
            if not await feature_gate_ok(self.bot, guild.id, perk):
                gated.add(perk)

        icon_range = await asyncio.to_thread(self._icon_price_range, guild.id)
        has_catalog = icon_range is not None
        accent = await resolve_accent_color(self.ctx.db_path, guild)
        embed = _build_shop_embed(
            settings,
            gated,
            accent,
            owned=owned,
            icon_catalog=icon_range,
            balance=balance,
        )
        view = _ShopView(self, settings, guild, user_id, gated, owned, has_catalog)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def do_rent(
        self,
        interaction: discord.Interaction,
        settings: EconSettings,
        guild: discord.Guild,
        perk: str,
    ) -> None:
        """Rent a self-perk from the ephemeral shop view."""
        await _rent_perk_flow(interaction, self, settings, guild, perk)

    # ── gift ─────────────────────────────────────────────────────────────

    @bank.command(name="gift", description="Gift a friend a custom color.")
    @app_commands.describe(member="Who to gift a color to")
    async def bank_gift(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        gifter = interaction.user
        assert isinstance(gifter, discord.Member)

        settings = await asyncio.to_thread(self._load_settings, guild.id)
        if not settings.enabled:
            await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
            return
        if member.bot:
            await interaction.response.send_message(
                "Bots can't wear a color.", ephemeral=True
            )
            return
        if member.id == gifter.id:
            await interaction.response.send_message(
                "Rent your own color with /bank shop.", ephemeral=True
            )
            return

        def _rent() -> None:
            with self.ctx.open_db() as conn:
                rent_perk(
                    conn, settings, guild.id, gifter.id, "gift_color",
                    beneficiary_id=member.id, now=time.time(),
                )

        try:
            await asyncio.to_thread(_rent)
        except ValueError as exc:
            msg = str(exc)
            if "insufficient" in msg:
                bal = await asyncio.to_thread(self._balance, guild.id, gifter.id)
                text = (
                    f"You need {settings.currency_emoji} "
                    f"{_perk_price(settings, 'gift_color'):,} but only have {bal:,}."
                )
            elif "already rented" in msg:
                text = "You're already gifting them a color."
            else:
                text = "That gift isn't available."
            await interaction.response.send_message(text, ephemeral=True)
            return

        await apply_role_perks(self.bot, self.ctx.db_path, guild.id, member.id)
        await notify_member(
            self.bot, self.ctx.db_path, guild.id, member.id,
            content=(
                f"{gifter.display_name} gifted you a custom color! "
                "Pick one from /bank shop."
            ),
        )
        await interaction.response.send_message(
            f"Gifted a custom color to {member.mention}. They can set it from "
            "`/bank shop`.",
            ephemeral=True,
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
        if not await feature_gate_ok(self.bot, guild.id, "role_icon"):
            await interaction.response.send_message(
                "This server doesn't support role icons right now.", ephemeral=True
            )
            return
        icons = await asyncio.to_thread(self._load_catalog, guild.id)
        if not icons:
            await interaction.response.send_message(
                "No rentable icons are set up here yet.", ephemeral=True
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
        if not settings.enabled:
            await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
            return
        if icon is None:
            await interaction.response.send_message(
                "That icon isn't available anymore — open the shop again.",
                ephemeral=True,
            )
            return
        if not await feature_gate_ok(self.bot, guild.id, "role_icon"):
            await interaction.response.send_message(
                "This server doesn't support role icons right now.", ephemeral=True
            )
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
                    bal = await asyncio.to_thread(self._balance, guild.id, user_id)
                    text = (
                        f"You need {settings.currency_emoji} {icon['price']:,} but "
                        f"only have {bal:,}."
                    )
                elif "already rented" in msg:
                    text = "You're already renting a role icon."
                else:
                    text = "That icon isn't available."
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

        ok = await apply_role_perks(self.bot, self.ctx.db_path, guild.id, user_id)
        tail = (
            "" if ok else " (I couldn't update your role right now — try again shortly.)"
        )
        await interaction.response.send_message(
            f"{verb} the **{icon['name']}** icon "
            f"({settings.currency_emoji} {icon['price']:,}/week).{tail}",
            ephemeral=True,
        )

    async def set_role_name(self, interaction: discord.Interaction, text: str) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        user_id = interaction.user.id
        settings, ent, _bal = await asyncio.to_thread(
            self._load_role_ctx, guild.id, user_id
        )
        if not settings.enabled:
            await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
            return
        if "role_name" not in ent:
            await interaction.response.send_message(
                "Rent the **Custom role name** perk first (/bank shop).", ephemeral=True
            )
            return
        text = text.strip()
        if not text or len(text) > _MAX_ROLE_NAME_LEN:
            await interaction.response.send_message(
                f"Role names must be 1–{_MAX_ROLE_NAME_LEN} characters.", ephemeral=True
            )
            return
        patterns = await asyncio.to_thread(self._name_blocklist, guild.id)
        if name_is_blocked(text, patterns):
            await interaction.response.send_message(
                "That name isn't allowed here.", ephemeral=True
            )
            return
        await asyncio.to_thread(
            self._upsert_role, guild.id, user_id, {"name": text}
        )
        await self._apply_and_confirm(
            interaction, guild.id, user_id, f"Your role name is now **{text}**."
        )

    async def set_role_color(self, interaction: discord.Interaction, hex: str) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        user_id = interaction.user.id
        settings, ent, _bal = await asyncio.to_thread(
            self._load_role_ctx, guild.id, user_id
        )
        if not settings.enabled:
            await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
            return
        if "role_color" not in ent and "gift_color" not in ent:
            await interaction.response.send_message(
                "Rent the **Custom role color** perk or get one gifted (/bank shop).",
                ephemeral=True,
            )
            return
        value = parse_hex_color(hex)
        if value is None:
            await interaction.response.send_message(
                "Give a color as a hex code like `#7B2FF7`.", ephemeral=True
            )
            return
        clash = find_color_clash(guild, value)
        if clash is not None:
            await interaction.response.send_message(
                f"That color is too close to **{clash.name}** — pick another.",
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
        settings, ent, _bal = await asyncio.to_thread(
            self._load_role_ctx, guild.id, user_id
        )
        if not settings.enabled:
            await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
            return
        if "role_gradient" not in ent:
            await interaction.response.send_message(
                "Rent the **Gradient role** perk first (/bank shop).", ephemeral=True
            )
            return
        if not await feature_gate_ok(self.bot, guild.id, "role_gradient"):
            await interaction.response.send_message(
                "This server doesn't support gradient roles right now.", ephemeral=True
            )
            return
        v1, v2 = parse_hex_color(hex1), parse_hex_color(hex2)
        if v1 is None or v2 is None:
            await interaction.response.send_message(
                "Give both colors as hex codes like `#7B2FF7`.", ephemeral=True
            )
            return
        clash = find_color_clash(guild, v1) or find_color_clash(guild, v2)
        if clash is not None:
            await interaction.response.send_message(
                f"That color is too close to **{clash.name}** — pick another.",
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
        settings, ent, _bal = await asyncio.to_thread(
            self._load_role_ctx, guild.id, user_id
        )
        if not settings.enabled:
            await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
            return
        if "role_icon" not in ent:
            await interaction.response.send_message(
                "Rent the **Role icon** perk first (/bank shop).", ephemeral=True
            )
            return
        if not await feature_gate_ok(self.bot, guild.id, "role_icon"):
            await interaction.response.send_message(
                "This server doesn't support role icons right now.", ephemeral=True
            )
            return
        if await asyncio.to_thread(self._has_catalog, guild.id):
            await interaction.response.send_message(
                "This server uses a curated icon catalog — pick one from /bank shop.",
                ephemeral=True,
            )
            return
        emoji = _resolve_guild_emoji(guild, raw)
        if emoji is None:
            await interaction.response.send_message(
                "That doesn't match a custom emoji on this server — type its "
                "name like `:party_parrot:`. For an image icon, upload one "
                "with `/bank role icon`.",
                ephemeral=True,
            )
            return
        if emoji.animated:
            await interaction.response.send_message(
                "Animated emojis can't be role icons — pick a static one.",
                ephemeral=True,
            )
            return
        try:
            data = await emoji.read()
        except discord.HTTPException:
            await interaction.response.send_message(
                "I couldn't fetch that emoji's image — try again shortly.",
                ephemeral=True,
            )
            return
        if len(data) > _MAX_ICON_BYTES:
            await interaction.response.send_message(
                "That emoji's image is too big — 256KB max.", ephemeral=True
            )
            return
        path = _icon_store_path(self.ctx.db_path, guild.id, user_id)

        def _write() -> None:
            path.write_bytes(data)
            self._upsert_role(guild.id, user_id, {"icon_path": str(path)})

        await asyncio.to_thread(_write)
        await self._apply_and_confirm(
            interaction, guild.id, user_id, "Your role icon is set."
        )

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
        settings, ent, _bal = await asyncio.to_thread(
            self._load_role_ctx, guild.id, user_id
        )
        if not settings.enabled:
            await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
            return
        if "role_icon" not in ent:
            await interaction.response.send_message(
                "Rent the **Role icon** perk first (/bank shop).", ephemeral=True
            )
            return
        if not await feature_gate_ok(self.bot, guild.id, "role_icon"):
            await interaction.response.send_message(
                "This server doesn't support role icons right now.", ephemeral=True
            )
            return
        if await asyncio.to_thread(self._has_catalog, guild.id):
            await interaction.response.send_message(
                "This server uses a curated icon catalog — pick one from /bank shop.",
                ephemeral=True,
            )
            return
        if image.size > _MAX_ICON_BYTES:
            await interaction.response.send_message(
                "That image is too big — 256KB max.", ephemeral=True
            )
            return
        data = await image.read()
        path = _icon_store_path(self.ctx.db_path, guild.id, user_id)

        def _write() -> None:
            path.write_bytes(data)
            self._upsert_role(guild.id, user_id, {"icon_path": str(path)})

        await asyncio.to_thread(_write)
        await self._apply_and_confirm(
            interaction, guild.id, user_id, "Your role icon is set."
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Cancel a leaver's rentals and re-project every affected role."""
        guild = member.guild
        settings = await asyncio.to_thread(self._load_settings, guild.id)
        if not settings.enabled:
            return

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
        ok = await apply_role_perks(self.bot, self.ctx.db_path, guild_id, user_id)
        if ok:
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.response.send_message(
                "Saved — but I couldn't update your role right now. Try again shortly.",
                ephemeral=True,
            )

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

    def _load_role_ctx(
        self, guild_id: int, user_id: int
    ) -> tuple[EconSettings, set[str], int]:
        """Settings, the member's entitlements, and their balance in one trip.

        The shop header shows the balance next to the prices, so it rides
        along on the connection the perk rows already need.
        """
        with self.ctx.open_db() as conn:
            settings = load_econ_settings(conn, guild_id)
            ent = entitlements(conn, guild_id, user_id)
            balance = get_balance(conn, guild_id, user_id)
        return settings, ent, balance

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

    def _has_catalog(self, guild_id: int) -> bool:
        """Whether the guild has at least one enabled catalog icon."""
        return self._icon_price_range(guild_id) is not None

    @bank.command(name="quests", description="View and claim the server's active quests.")
    async def bank_quests(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        guild = interaction.guild

        settings, quests_state, board_meta = await asyncio.to_thread(
            self._load_quests_state, guild.id, interaction.user.id
        )
        if not settings.enabled:
            await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
            return

        accent = await resolve_accent_color(self.ctx.db_path, guild)
        embed = discord.Embed(title=f"{settings.currency_emoji} Quests", color=accent)

        if not quests_state:
            embed.description = "_No active quests right now — check back soon!_"
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # One line per quest — title | status | payment, grouped by cadence.
        # Descriptions and the how-it-completes explainers live behind the
        # details select, so the list stays scannable.
        desc_bits = []
        if any(q.get("spotlight") for q in quests_state):
            desc_bits.append("⚡ Spotlight quests pay **double** this week!")
        desc_bits.append(
            "Pick a quest from the menu below for its full story."
        )
        embed.description = "\n".join(desc_bits) + "\n\u200b"

        groups: dict[str, list[dict]] = {}
        for q in quests_state:
            groups.setdefault(str(q["qtype"]), []).append(q)
        width = min(max(len(str(q["title"])) for q in quests_state), 22)
        sections = [
            (heading, groups[qtype])
            for qtype, heading in _QUEST_GROUPS
            if groups.get(qtype)
        ]
        for i, (heading, batch) in enumerate(sections):
            lines = [
                f"`{_pad(str(q['title']), width)}` {_quest_line_status(q)} · "
                f"{_quest_line_reward(q, settings)}"
                for q in batch
            ]
            if i < len(sections) - 1:  # breathing room above the next heading
                lines.append("\u200b")
            embed.add_field(name=heading, value="\n".join(lines), inline=False)

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
            rows = conn.execute(
                """
                SELECT * FROM econ_quests
                WHERE guild_id = ? AND active = 1
                ORDER BY qtype, id
                """,
                (guild_id,),
            ).fetchall()
            # daily/weekly/monthly quests are shown per member — only the ones
            # on this member's board for the period. Compute each cadence once.
            spot = spotlight_kind(conn, guild_id, iso_week_for(day))
            boards: dict[str, set[int]] = {}
            out: list[dict] = []
            for row in rows:
                qtype = str(row["qtype"])
                quest_id = int(row["id"])
                if has_board(qtype):
                    if qtype not in boards:
                        boards[qtype] = assigned_board_ids(
                            conn, guild_id, user_id, qtype, day, settings
                        )
                    if quest_id not in boards[qtype]:
                        continue  # not on this member's board this period
                entry: dict = {
                    "id": quest_id,
                    "title": row["title"],
                    "description": row["description"],
                    "qtype": qtype,
                    "reward": int(row["reward"]),
                    "reward_xp": int(row["reward_xp"]),
                    "signoff": bool(row["signoff"]),
                    "criteria": row["criteria"],
                    "spotlight": bool(
                        spot and str(row["trigger_kind"] or "") == spot
                    ),
                }
                if qtype == "community":
                    prog = conn.execute(
                        "SELECT current FROM econ_community_progress WHERE quest_id = ?",
                        (quest_id,),
                    ).fetchone()
                    target = row["community_target"]
                    entry["state"] = "community"
                    entry["current"] = int(prog["current"]) if prog else 0
                    entry["target"] = int(target) if target is not None else 0
                elif qtype == "event":
                    # No calendar period — the trigger listener pays per
                    # occurrence (e.g. per photo card), so the list shows the
                    # standing how-to instead of a per-period claim state.
                    entry["state"] = str(row["trigger_kind"]) or "trigger"
                else:
                    period = quest_period(qtype, day)
                    claim = conn.execute(
                        """
                        SELECT state FROM econ_quest_claims
                        WHERE quest_id = ? AND user_id = ? AND period = ?
                          AND state IN ('paid', 'pending')
                        ORDER BY CASE state WHEN 'paid' THEN 0 ELSE 1 END
                        LIMIT 1
                        """,
                        (quest_id, user_id, period),
                    ).fetchone()
                    kind = str(row["trigger_kind"] or "")
                    has_trigger = bool(str(row["trigger_words"] or "").strip())
                    # Resolves (and stores) the member's dynamic target on
                    # first sight, so the wallet shows the same number the
                    # fire path will enforce all period.
                    target = resolve_member_target(
                        conn, guild_id, user_id, row,
                        period=period, local_day=day,
                    )
                    if kind and target > 1:
                        entry["progress_current"] = get_progress(
                            conn, quest_id, user_id, period
                        )
                        entry["progress_target"] = target
                    if claim is None:
                        # Trigger quests never enter the claim select — the
                        # phrase/game event IS the verification, so a manual
                        # claim would bypass it.
                        entry["state"] = kind or (
                            "trigger" if has_trigger else "claimable"
                        )
                    elif claim["state"] == "paid":
                        entry["state"] = "done"
                    else:
                        entry["state"] = "pending"
                out.append(entry)
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
        ``claim_quest``'s per-period collision ValueError.
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
            try:
                triggers = await asyncio.to_thread(
                    self._load_trigger_quests, guild_id
                )
            except Exception:
                log.exception("econ trigger: failed to load quests for %s", guild_id)
                return
            self._trigger_cache[guild_id] = (now + _TRIGGER_CACHE_TTL, triggers)
        else:
            triggers = cached[1]
        if not triggers:
            return

        channel = message.channel
        parent_id = getattr(channel, "parent_id", None)  # threads count as parent
        for trig in triggers:
            if trig.channel_id is not None and trig.channel_id not in (
                channel.id,
                parent_id,
            ):
                continue
            if message_matches_trigger(content, trig.pattern):
                await self._complete_trigger_quest(message, member, trig)

    def _load_trigger_quests(self, guild_id: int) -> list[_TriggerQuest]:
        """Active trigger quests with compiled patterns ([] when econ is off)."""
        with self.ctx.open_db() as conn:
            settings = load_econ_settings(conn, guild_id)
            if not settings.enabled:
                return []
            rows = list_trigger_quests(conn, guild_id)
        out: list[_TriggerQuest] = []
        for row in rows:
            pattern = compile_trigger_pattern(
                parse_trigger_words(str(row["trigger_words"]))
            )
            if pattern is None:
                continue
            channel_id = row["trigger_channel_id"]
            out.append(
                _TriggerQuest(
                    quest_id=int(row["id"]),
                    qtype=str(row["qtype"]),
                    title=str(row["title"]),
                    signoff=bool(row["signoff"]),
                    channel_id=int(channel_id) if channel_id is not None else None,
                    pattern=pattern,
                    reward_xp=int(row["reward_xp"]),
                )
            )
        return out

    async def _complete_trigger_quest(
        self, message: discord.Message, member: discord.Member, trig: _TriggerQuest
    ) -> None:
        """Claim a matched trigger quest for the message author, best-effort."""
        guild = message.guild
        assert guild is not None
        booster = member.premium_since is not None

        def _claim():
            with self.ctx.open_db() as conn:
                settings = load_econ_settings(conn, guild.id)
                offset = get_tz_offset_hours(conn, guild.id)
                day = local_day_for(time.time(), offset)
                period = quest_period(trig.qtype, day)
                # A trigger-word quest still only pays when it's on the
                # member's personal board this period (parity with kind
                # triggers). Off-board → treat like an unclaimable repeat.
                if has_board(trig.qtype) and trig.quest_id not in (
                    assigned_board_ids(
                        conn, guild.id, member.id, trig.qtype, day, settings
                    )
                ):
                    raise ValueError("quest not on member's board this period")
                outcome = claim_quest(
                    conn,
                    settings,
                    guild.id,
                    trig.quest_id,
                    member.id,
                    period=period,
                    booster=booster,
                )
            return settings, outcome

        try:
            settings, outcome = await asyncio.to_thread(_claim)
        except ValueError:
            # Already claimed this period, quest window closed, or deactivated
            # since the cache load — every repeat message would hit this, so
            # stay quiet rather than spam the channel.
            return
        except Exception:
            log.exception(
                "econ trigger: claim failed for quest %s", trig.quest_id
            )
            return

        await self._announce_quest_claim(
            message, member, trig.title, settings, outcome,
            reward_xp=trig.reward_xp,
        )

    async def _announce_quest_claim(
        self,
        message: discord.Message,
        member: discord.Member,
        title: str,
        settings: EconSettings,
        outcome,
        reward_xp: int = 0,
    ) -> None:
        """React + reply for an auto-claimed quest (trigger phrase or photo)."""
        guild = message.guild
        assert guild is not None
        accent = await resolve_accent_color(self.ctx.db_path, guild)

        if outcome.state == "paid":
            paid = int(outcome.paid)
            xp_note = f" (+⭐ {reward_xp:,} XP)" if reward_xp > 0 else ""
            embed = discord.Embed(
                title="Quest complete!",
                description=(
                    f"{member.mention} completed **{title}** — "
                    f"{settings.currency_emoji} {paid:,} {_unit(settings, paid)} "
                    f"added to their wallet{xp_note}."
                ),
                color=accent,
            )
            reaction, note = "✅", embed
        else:
            # Sign-off trigger quest: the phrase files the claim; a manager
            # still approves the payout from the bank-channel card.
            await post_signoff_card(
                self.bot, self.ctx, guild, settings, accent,
                int(outcome.claim_id), member,
            )
            embed = discord.Embed(
                title="Quest submitted",
                description=(
                    f"{member.mention} triggered **{title}** — "
                    "sent for manager sign-off."
                ),
                color=accent,
            )
            reaction, note = "📝", embed

        # When a "game role" is configured, the completion card is a DM to
        # opted-in players (keeps the trigger channel clean); members without
        # the role are paid silently. With no role set the feature is off and
        # everyone gets the legacy in-channel reaction + reply.
        role_id = settings.game_role_id
        if role_id:
            if not any(r.id == role_id for r in member.roles):
                return
            try:
                await message.add_reaction(reaction)
            except discord.HTTPException:
                log.debug("econ trigger: failed to react", exc_info=True)
            await notify_member(
                self.bot, self.ctx.db_path, guild.id, member.id, embed=note
            )
            return

        try:
            await message.add_reaction(reaction)
        except discord.HTTPException:
            log.debug("econ trigger: failed to react", exc_info=True)
        try:
            await message.reply(embed=note, mention_author=False)
        except discord.HTTPException:
            log.debug("econ trigger: failed to reply", exc_info=True)

    # ── photo-react event quest (a Photo Challenge post that earns reactions) ──

    _PHOTO_OPTS_TTL = 60.0  # game-option cache staleness bound (seconds)
    _PHOTO_THRESHOLD_DEFAULT = 5  # distinct human reactors, when unconfigured

    def _read_photo_opts(self, guild_id: int) -> tuple[int, int, str]:
        """(channel_id, react_threshold, auto_react_emoji) from config.

        ``channel_id`` is 0 when the admin hasn't picked a Photo Challenge
        channel — both listeners no-op then, so the mechanic is dormant until
        one is set. The threshold clamps to a floor of 1; ``auto_react`` is ''
        (off) unless set. Read from ``games_game_config`` (game_type 'photo'),
        the same options blob the standalone Photo Challenge Setup panel owns:
        ``channel_id`` is its dedicated channel; ``react_threshold`` and
        ``auto_react`` are this feature's additions to that panel.
        """
        with self.ctx.open_db() as conn:
            row = conn.execute(
                "SELECT options FROM games_game_config"
                " WHERE guild_id = ? AND game_type = 'photo'",
                (guild_id,),
            ).fetchone()
        opts: dict = {}
        if row and row[0]:
            try:
                opts = json.loads(row[0])
            except (ValueError, TypeError):
                opts = {}

        def _as_int(val: object) -> int:
            try:
                return int(str(val).strip() or 0)
            except (ValueError, TypeError):
                return 0

        channel_id = _as_int(opts.get("channel_id"))
        threshold = max(
            1, _as_int(opts.get("react_threshold")) or self._PHOTO_THRESHOLD_DEFAULT
        )
        emoji = str(opts.get("auto_react") or "").strip()
        return channel_id, threshold, emoji

    async def _photo_options(self, guild_id: int) -> tuple[int, int, str]:
        """TTL-cached ``_read_photo_opts`` — one DB read per guild per TTL.

        Keeps the reaction listener (which fires for every reaction anywhere)
        and the auto-react listener from touching the DB on each event.
        """
        now = time.monotonic()
        cached = self._photo_opts.get(guild_id)
        if cached is not None and cached[0] > now:
            return cached[1]
        try:
            opts = await asyncio.to_thread(self._read_photo_opts, guild_id)
        except Exception:
            log.exception("econ photo: options read failed in guild %s", guild_id)
            return 0, self._PHOTO_THRESHOLD_DEFAULT, ""
        self._photo_opts[guild_id] = (now + self._PHOTO_OPTS_TTL, opts)
        return opts

    @commands.Cog.listener("on_message")
    async def _on_photo_autoreact(self, message: discord.Message) -> None:
        """Seed the configured reaction on image posts in the photo channel.

        A one-tap target so members can pile on toward the react threshold.
        The bot's own reaction never counts (the distinct-reactor tally
        excludes bots), so this only lowers friction — it can't inflate a
        member's own count.
        """
        if message.guild is None or message.author.bot:
            return
        if not _has_image_attachment(message):
            return
        channel_id, _threshold, emoji = await self._photo_options(message.guild.id)
        if not emoji or message.channel.id != channel_id:
            return
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            log.debug("econ photo: auto-react failed", exc_info=True)

    @commands.Cog.listener("on_raw_reaction_add")
    async def _on_photo_react(self, payload: discord.RawReactionActionEvent) -> None:
        """Pay the photo-react event quest when a post earns enough reactors.

        Eligibility: an image post by a real member in the configured Photo
        Challenge channel that has drawn ``react_threshold`` distinct human
        reactors (the author and bots never count). Fires once per member per
        guild-local day (occurrence ``photo_react:<local_day>``, like
        voice_session/boost); the claim collision dedups. The distinct count
        is Discord-API-heavy, so it runs only behind a cheap cached channel
        gate, a DB eligibility pre-check, a raw-total prune, and a per-process
        ``_photo_paid`` skip once the post has crossed and paid.
        """
        if payload.guild_id is None:
            return
        channel_id, threshold, _emoji = await self._photo_options(payload.guild_id)
        if channel_id == 0 or payload.channel_id != channel_id:
            return
        if payload.message_id in self._photo_paid:
            return

        # Cheap DB gate before the expensive reactor fetch: economy on, source
        # on, and at least one active photo_react quest to pay.
        try:
            eligible = await asyncio.to_thread(self._photo_eligible, payload.guild_id)
        except Exception:
            log.exception(
                "econ photo: eligibility check failed in guild %s", payload.guild_id
            )
            return
        if not eligible:
            return

        channel = self.bot.get_channel(payload.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            return
        author = message.author
        if author.bot or not _has_image_attachment(message):
            return
        # Distinct reactors can't exceed the raw total — prune before fetching
        # per-emoji reactor lists.
        if sum(r.count for r in message.reactions) < threshold:
            return
        if await self._distinct_reactors(message, exclude_id=author.id) < threshold:
            return

        guild = self.bot.get_guild(payload.guild_id)
        member = guild.get_member(author.id) if guild is not None else None
        if member is None:
            return
        guild_id = payload.guild_id
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
                    "photo_react",
                    member.id,
                    local_day=day,
                    occurrence=day,
                    booster=booster,
                    channel_ids=(channel_id,),
                )
                return settings, fired

        try:
            result = await asyncio.to_thread(_claim)
        except Exception:
            log.exception("econ photo: claim failed in guild %s", guild_id)
            return
        # Crossed the threshold with a quest live — no more reactions can change
        # the outcome for this post today, so stop recounting it this process.
        self._photo_paid.add(payload.message_id)
        if result is None:
            return
        settings, fired = result
        for quest, outcome in fired:
            await self._announce_quest_claim(
                message, member, str(quest["title"]), settings, outcome,
                reward_xp=int(quest["reward_xp"]),
            )

    def _photo_eligible(self, guild_id: int) -> bool:
        """True when a photo-react payout is possible in this guild right now.

        Economy enabled, the photo_react income source on, and ≥1 active
        photo_react quest. Gates the expensive distinct-reactor fetch so a
        channel with no quest configured never triggers reactor lookups.
        """
        with self.ctx.open_db() as conn:
            settings = load_econ_settings(conn, guild_id)
            if not settings.enabled:
                return False
            if not source_enabled(conn, guild_id, "photo_react"):
                return False
            row = conn.execute(
                "SELECT 1 FROM econ_quests WHERE guild_id = ? AND active = 1"
                " AND trigger_kind = 'photo_react' LIMIT 1",
                (guild_id,),
            ).fetchone()
            return row is not None

    async def _distinct_reactors(
        self, message: discord.Message, *, exclude_id: int
    ) -> int:
        """Count distinct non-bot reactors on a message, minus ``exclude_id``.

        Unions the reactor sets across every emoji, so five different people
        with five different emoji count as five (and one person spamming five
        emoji counts as one). ``exclude_id`` drops the author's own reaction.
        """
        seen: set[int] = set()
        for reaction in message.reactions:
            try:
                async for user in reaction.users():
                    if user.bot or user.id == exclude_id:
                        continue
                    seen.add(user.id)
            except discord.HTTPException:
                log.debug("econ photo: reactor fetch failed", exc_info=True)
        return len(seen)

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
        for quest, outcome in fired:
            await self._announce_quest_claim(
                message, member, str(quest["title"]), settings, outcome,
                reward_xp=int(quest["reward_xp"]),
            )

    qotd = app_commands.Group(
        name="qotd",
        description="Question of the day.",
        guild_only=True,
    )

    @qotd.command(
        name="post", description="Post today's question of the day (staff only)."
    )
    @app_commands.describe(question="The question to ask the server")
    async def qotd_post(
        self, interaction: discord.Interaction, question: str
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        guild_id = guild.id
        actor = interaction.user
        assert isinstance(actor, discord.Member)

        settings = await asyncio.to_thread(self._load_settings, guild_id)
        if not settings.enabled:
            await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
            return
        if not _can_grant(actor, settings):
            await interaction.response.send_message(
                "You don't have permission to post a question of the day.",
                ephemeral=True,
            )
            return

        channel = interaction.channel
        if not isinstance(channel, discord.abc.Messageable):
            await interaction.response.send_message(
                "I can't post a question here.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        accent = await resolve_accent_color(self.ctx.db_path, guild)

        # Prefer the rendered quote card; fall back to a plain branded embed if
        # there's no usable background image or the renderer raises.
        card_file: discord.File | None = None
        image_bytes = await _resolve_qotd_image(guild, self.bot)
        if image_bytes is not None:
            try:
                card_bytes = await asyncio.to_thread(
                    render_quote_card,
                    question,
                    author_name="Question of the Day",
                    avatar_bytes=image_bytes,
                    theme=THEMES["midnight"],
                    pfp_shape="none",
                )
                card_file = discord.File(
                    io.BytesIO(card_bytes), filename=_QOTD_CARD_FILENAME
                )
            except Exception:
                log.exception("qotd: failed to render card in guild %s", guild_id)

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
                message = await channel.send(
                    content=content, embed=embed, allowed_mentions=mentions
                )
        except discord.Forbidden:
            await interaction.followup.send(
                "I don't have permission to post in this channel.", ephemeral=True
            )
            return

        def _record() -> None:
            with self.ctx.open_db() as conn:
                offset = get_tz_offset_hours(conn, guild_id)
                today = local_day_for(time.time(), offset)
                create_qotd(
                    conn, guild_id, channel.id, message.id, question, actor.id, today
                )

        await asyncio.to_thread(_record)
        await interaction.followup.send(
            "Posted the question of the day.", ephemeral=True
        )

    # ── how-to guide panel ───────────────────────────────────────────────

    @bank.command(
        name="post-guide",
        description="Post (or refresh) the economy how-to panel (staff only).",
    )
    @app_commands.describe(channel="Where the panel lives — defaults to this channel")
    async def bank_post_guide(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        actor = interaction.user
        assert isinstance(actor, discord.Member)

        settings = await asyncio.to_thread(self._load_settings, guild.id)
        if not settings.enabled:
            await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
            return
        if not _can_grant(actor, settings):
            await interaction.response.send_message(
                "You don't have permission to post the guide panel.",
                ephemeral=True,
            )
            return

        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message(
                "Pick a regular text channel for the guide panel.", ephemeral=True
            )
            return

        accent = await resolve_accent_color(self.ctx.db_path, guild)
        embed = build_guide_embed(settings, color=accent)

        # Same channel and the old panel is still there → edit in place, so a
        # refresh after re-branding/re-pricing doesn't hop the panel to the
        # bottom of the channel.
        if settings.guide_message_id and settings.guide_channel_id == target.id:
            try:
                old = await target.fetch_message(settings.guide_message_id)
                await old.edit(embed=embed)
            except discord.HTTPException:
                pass  # gone or unreachable — fall through to a fresh post
            else:
                await interaction.response.send_message(
                    f"Refreshed the guide panel in {target.mention}.",
                    ephemeral=True,
                )
                return

        # Moving or reposting → drop the stale panel and post a fresh one at
        # the bottom (shared with the sticky repost path).
        message = await self._place_guide_panel(
            guild,
            target,
            settings,
            accent,
            old_channel_id=settings.guide_channel_id,
            old_message_id=settings.guide_message_id,
        )
        if message is None:
            await interaction.response.send_message(
                f"I don't have permission to post in {target.mention}.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Posted the guide panel in {target.mention}.", ephemeral=True
        )

    # ── guide-panel placement + sticky repost ────────────────────────────

    async def _place_guide_panel(
        self,
        guild: discord.Guild,
        target: discord.TextChannel,
        settings: EconSettings,
        accent: discord.Color | None,
        *,
        old_channel_id: int,
        old_message_id: int,
    ) -> discord.Message | None:
        """Delete the old guide panel (if any) and post a fresh one at the
        bottom of ``target``, persisting the new ids. Returns the new message,
        or ``None`` when posting is forbidden. Serialised per guild so a manual
        ``/bank post-guide`` and a sticky repost can't race into two panels.
        """
        lock = self._guide_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            if old_message_id and old_channel_id:
                old_channel = guild.get_channel(old_channel_id)
                if isinstance(old_channel, discord.TextChannel):
                    try:
                        old = await old_channel.fetch_message(old_message_id)
                        await old.delete()
                    except discord.HTTPException:
                        pass

            try:
                message = await target.send(
                    embed=build_guide_embed(settings, color=accent)
                )
            except discord.Forbidden:
                return None

            # Record the new id *before* the DB-save await so the gateway event
            # for our own repost is recognized (and skipped) by the sticky
            # listener rather than triggering yet another repost.
            self._guide_ref[guild.id] = (
                time.monotonic() + _GUIDE_STICKY_CACHE_TTL,
                target.id,
                message.id,
            )

            def _save() -> None:
                with self.ctx.open_db() as conn:
                    save_econ_settings(
                        conn,
                        guild.id,
                        {
                            "guide_channel_id": target.id,
                            "guide_message_id": message.id,
                        },
                    )

            await asyncio.to_thread(_save)
            return message

    @commands.Cog.listener("on_message")
    async def _restick_guide_panel(self, message: discord.Message) -> None:
        """Keep the guide panel as the last message in its channel.

        A **member** message in the panel's channel means the panel is no
        longer at the bottom, so we arm a debounced repost. Bot messages are
        ignored outright: re-sticking under our own repost is a self-loop
        (the repost's ``on_message`` can arrive before the new id is cached,
        so the id skip alone can't be relied on), and chasing our own economy
        notices adds churn for no member-visible benefit.
        """
        if message.guild is None or message.author.bot:
            return
        guild_id = message.guild.id
        panel_channel_id, panel_message_id = await self._guide_panel_ref(guild_id)
        if not should_restick_guide(
            message_channel_id=message.channel.id,
            message_id=message.id,
            panel_channel_id=panel_channel_id,
            panel_message_id=panel_message_id,
        ):
            return
        self._schedule_guide_restick(guild_id)

    async def _guide_panel_ref(self, guild_id: int) -> tuple[int, int]:
        """Cached ``(channel_id, message_id)`` of the guild's guide panel, or
        ``(0, 0)`` when the economy is off or no panel is posted. Re-read from
        config at most once per ``_GUIDE_STICKY_CACHE_TTL`` so the listener is a
        dict lookup per message, not a DB read.
        """
        entry = self._guide_ref.get(guild_id)
        now = time.monotonic()
        if entry is not None and entry[0] > now:
            return entry[1], entry[2]

        def _load() -> tuple[int, int]:
            with self.ctx.open_db() as conn:
                s = load_econ_settings(conn, guild_id)
            if not s.enabled:
                return 0, 0
            return s.guide_channel_id, s.guide_message_id

        channel_id, message_id = await asyncio.to_thread(_load)
        self._guide_ref[guild_id] = (
            now + _GUIDE_STICKY_CACHE_TTL,
            channel_id,
            message_id,
        )
        return channel_id, message_id

    def _schedule_guide_restick(self, guild_id: int) -> None:
        """(Re)arm the debounced repost — a burst of messages collapses to a
        single repost once the channel falls quiet."""
        existing = self._restick_tasks.get(guild_id)
        if existing is not None and not existing.done():
            existing.cancel()
        self._restick_tasks[guild_id] = asyncio.create_task(
            self._delayed_restick(guild_id)
        )

    async def _delayed_restick(self, guild_id: int) -> None:
        try:
            await asyncio.sleep(_GUIDE_STICKY_DELAY)
        except asyncio.CancelledError:
            return
        try:
            await self._restick_now(guild_id)
        except Exception:
            log.exception(
                "econ guide sticky: repost failed in guild %s", guild_id
            )

    async def _restick_now(self, guild_id: int) -> None:
        """Repost the existing guide panel at the bottom of its channel."""

        def _load() -> EconSettings:
            with self.ctx.open_db() as conn:
                return load_econ_settings(conn, guild_id)

        settings = await asyncio.to_thread(_load)
        # Only maintain an already-posted panel; never create one here.
        if (
            not settings.enabled
            or not settings.guide_channel_id
            or not settings.guide_message_id
        ):
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        channel = guild.get_channel(settings.guide_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        accent = await resolve_accent_color(self.ctx.db_path, guild)
        await self._place_guide_panel(
            guild,
            channel,
            settings,
            accent,
            old_channel_id=settings.guide_channel_id,
            old_message_id=settings.guide_message_id,
        )

    # ── auto-updating leaderboard panel ──────────────────────────────────

    @bank.command(
        name="post-leaderboard",
        description="Post (or refresh) the auto-updating leaderboard panel (staff only).",
    )
    @app_commands.describe(channel="Where the panel lives — defaults to this channel")
    async def bank_post_leaderboard(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        actor = interaction.user
        assert isinstance(actor, discord.Member)

        settings = await asyncio.to_thread(self._load_settings, guild.id)
        if not settings.enabled:
            await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
            return
        if not _can_grant(actor, settings):
            await interaction.response.send_message(
                "You don't have permission to post the leaderboard panel.",
                ephemeral=True,
            )
            return

        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message(
                "Pick a regular text channel for the leaderboard panel.",
                ephemeral=True,
            )
            return

        now_ts = time.time()

        def _collect():
            with self.ctx.open_db() as conn:
                data = collect_leaderboard_data(conn, guild.id, now_ts)
                known = get_known_users_bulk(
                    conn, guild.id, [uid for uid, _ in data.top_earners]
                )
            return data, known

        data, known = await asyncio.to_thread(_collect)

        def _name(uid: int) -> str:
            member = guild.get_member(uid)
            if member:
                return member.display_name
            return known.get(uid) or f"User {uid}"

        accent = await resolve_accent_color(self.ctx.db_path, guild)
        embed = build_leaderboard_embed(
            settings, data, _name, now_ts=now_ts, color=accent
        )

        # Same channel and the old panel is still there → edit in place.
        if (
            settings.leaderboard_message_id
            and settings.leaderboard_channel_id == target.id
        ):
            try:
                old = await target.fetch_message(settings.leaderboard_message_id)
                await old.edit(embed=embed)
            except discord.HTTPException:
                pass  # gone or unreachable — fall through to a fresh post
            else:
                await interaction.response.send_message(
                    f"Refreshed the leaderboard panel in {target.mention}.",
                    ephemeral=True,
                )
                return

        # Moving or reposting: drop the stale panel if we can still find it.
        if settings.leaderboard_message_id and settings.leaderboard_channel_id:
            old_channel = guild.get_channel(settings.leaderboard_channel_id)
            if isinstance(old_channel, discord.TextChannel):
                try:
                    old = await old_channel.fetch_message(
                        settings.leaderboard_message_id
                    )
                    await old.delete()
                except discord.HTTPException:
                    pass

        try:
            message = await target.send(embed=embed)
        except discord.Forbidden:
            await interaction.response.send_message(
                f"I don't have permission to post in {target.mention}.",
                ephemeral=True,
            )
            return

        def _save() -> None:
            with self.ctx.open_db() as conn:
                save_econ_settings(
                    conn,
                    guild.id,
                    {
                        "leaderboard_channel_id": target.id,
                        "leaderboard_message_id": message.id,
                    },
                )

        await asyncio.to_thread(_save)
        await interaction.response.send_message(
            f"Posted the leaderboard panel in {target.mention} — it refreshes "
            "itself live, within a couple of minutes of economy activity.",
            ephemeral=True,
        )

    # ── persistent shop panel ────────────────────────────────────────────

    @bank.command(
        name="post-shop",
        description="Post (or refresh) the perk-shop panel (staff only).",
    )
    @app_commands.describe(channel="Where the panel lives — defaults to this channel")
    async def bank_post_shop(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        actor = interaction.user
        assert isinstance(actor, discord.Member)

        settings = await asyncio.to_thread(self._load_settings, guild.id)
        if not settings.enabled:
            await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
            return
        if not _can_grant(actor, settings):
            await interaction.response.send_message(
                "You don't have permission to post the shop panel.",
                ephemeral=True,
            )
            return

        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message(
                "Pick a regular text channel for the shop panel.", ephemeral=True
            )
            return

        gated: set[str] = set()
        for perk in _FEATURE_GATED:
            if not await feature_gate_ok(self.bot, guild.id, perk):
                gated.add(perk)

        icon_range = await asyncio.to_thread(self._icon_price_range, guild.id)
        has_catalog = icon_range is not None
        accent = await resolve_accent_color(self.ctx.db_path, guild)
        embed = _build_shop_embed(
            settings, gated, accent, panel=True, icon_catalog=icon_range
        )
        view = _shop_panel_view(settings, gated, has_catalog=has_catalog)

        # Same channel and the old panel is still there → edit in place (the
        # view is re-sent too, so re-pricing refreshes the button labels).
        if settings.shop_message_id and settings.shop_channel_id == target.id:
            try:
                old = await target.fetch_message(settings.shop_message_id)
                await old.edit(embed=embed, view=view)
            except discord.HTTPException:
                pass  # gone or unreachable — fall through to a fresh post
            else:
                await interaction.response.send_message(
                    f"Refreshed the shop panel in {target.mention}.",
                    ephemeral=True,
                )
                return

        # Moving or reposting: drop the stale panel if we can still find it.
        if settings.shop_message_id and settings.shop_channel_id:
            old_channel = guild.get_channel(settings.shop_channel_id)
            if isinstance(old_channel, discord.TextChannel):
                try:
                    old = await old_channel.fetch_message(settings.shop_message_id)
                    await old.delete()
                except discord.HTTPException:
                    pass

        try:
            message = await target.send(embed=embed, view=view)
        except discord.Forbidden:
            await interaction.response.send_message(
                f"I don't have permission to post in {target.mention}.",
                ephemeral=True,
            )
            return

        def _save() -> None:
            with self.ctx.open_db() as conn:
                save_econ_settings(
                    conn,
                    guild.id,
                    {"shop_channel_id": target.id, "shop_message_id": message.id},
                )

        await asyncio.to_thread(_save)
        await interaction.response.send_message(
            f"Posted the shop panel in {target.mention}. Re-run this after "
            "re-pricing to refresh it.",
            ephemeral=True,
        )

    async def cog_load(self) -> None:
        # Re-register the persistent buttons so clicks on existing messages
        # still route after a restart — the custom_ids carry the state
        # (econ_claim:{approve,deny}:<id>, econ_shop_panel:<perk>).
        self.bot.add_dynamic_items(
            QuestApproveButton, QuestDenyButton, ShopRentButton
        )

    def _load_settings(self, guild_id: int) -> EconSettings:
        with self.ctx.open_db() as conn:
            return load_econ_settings(conn, guild_id)


async def setup(bot: Bot) -> None:
    await bot.add_cog(EconomyCog(bot, bot.ctx))
