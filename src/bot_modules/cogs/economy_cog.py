"""Economy — the ``/bank`` command surface (wallet view + mod grants).

Thin cog over ``bot_modules.services.economy_service``: it loads per-guild
``econ_`` settings on each interaction (cheap KV reads, no cache for stage 0),
resolves the branded currency naming, and renders the accent-coloured embeds.
See docs/economy_spec.md for the feature design.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.branding import resolve_accent_color
from bot_modules.core.db_utils import get_tz_offset_hours
from bot_modules.economy.guide import build_guide_embed
from bot_modules.economy.logic import local_day_for
from bot_modules.economy.perk_actions import (
    apply_role_perks,
    feature_gate_ok,
    find_color_clash,
    parse_hex_colour,
    revoke_role_perks,
)
from bot_modules.economy.quest_views import (
    QuestApproveButton,
    QuestClaimView,
    QuestDenyButton,
    can_manage_economy,
)
from bot_modules.economy.quests import quest_period
from bot_modules.services.economy_rentals_service import (
    cancel_all_for_member,
    entitlements,
    list_member_rentals,
    rent_perk,
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
_MAX_ICON_BYTES = 256 * 1024

# Human labels for the rentable perks (shop rows, wallet field, DMs).
_PERK_LABELS = {
    "role_color": "Custom role colour",
    "role_name": "Custom role name",
    "role_icon": "Role icon",
    "role_gradient": "Gradient role",
    "gift_color": "Gift-a-colour",
}
# The perks a member rents for themselves, in shop display order.
_SELF_PERKS = ("role_color", "role_name", "role_gradient", "role_icon")
# Feature-gated perks and the friendly reason shown when the gate is closed.
_FEATURE_GATED = ("role_gradient", "role_icon")


def _perk_price(settings: EconSettings, perk: str) -> int:
    return int(getattr(settings, f"price_{perk}"))


def _icon_store_path(db_path, guild_id: int, user_id: int):
    """Managed on-disk path for an uploaded personal-role icon (per guild/member)."""
    directory = db_path.parent / "econ_role_icons" / str(guild_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{user_id}.png"


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


_QUEST_STATE_LABEL = {
    "claimable": "✅ Ready to claim",
    "pending": "⏳ Awaiting sign-off",
    "done": "☑️ Completed this period",
}


def _progress_bar(current: int, target: int, width: int = 10) -> str:
    """A text meter for a community quest's running total."""
    if target <= 0:
        return f"{current:,}"
    filled = max(0, min(width, round(width * current / target)))
    return f"{'▰' * filled}{'▱' * (width - filled)} {current:,}/{target:,}"


def _can_grant(user: discord.Member, settings: EconSettings) -> bool:
    """True for server admins or holders of the configured manager role.

    Delegates to the canonical gate in ``quest_views`` so the grant command
    and the sign-off buttons enforce one rule.
    """
    return can_manage_economy(user, settings)


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
    ) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.settings = settings
        self.guild = guild
        self.sender = sender
        self.recipient = recipient
        self.amount = amount

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
            self.amount, via_confirm=True,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def _cancel(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="Payment cancelled.", embed=None, view=None
        )


class _ShopView(discord.ui.View):
    """Rent buttons for the self-service role perks (feature-gated rows disabled)."""

    def __init__(
        self,
        cog: EconomyCog,
        settings: EconSettings,
        guild: discord.Guild,
        user_id: int,
        gated: set[str],
    ) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.settings = settings
        self.guild = guild
        self.user_id = user_id
        for perk in _SELF_PERKS:
            price = _perk_price(settings, perk)
            button = discord.ui.Button(
                label=f"Rent {_PERK_LABELS[perk]} · {price}",
                style=discord.ButtonStyle.primary,
                disabled=perk in gated,
                custom_id=f"econ_shop_rent:{perk}",
            )
            button.callback = self._make_callback(perk)
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Open your own shop with /bank shop.", ephemeral=True
            )
            return False
        return True

    def _make_callback(self, perk: str):
        async def _cb(interaction: discord.Interaction) -> None:
            await self.cog.do_rent(interaction, self.settings, self.guild, perk)

        return _cb


class EconomyCog(commands.Cog):
    bank = app_commands.Group(
        name="bank",
        description="Wallet and currency commands.",
        guild_only=True,
    )
    role = app_commands.Group(
        name="role",
        description="Customise your personal role (rent perks with /bank shop).",
        parent=bank,
    )

    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

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
            colour=accent,
        )
        if settings.currency_icon_url:
            embed.set_thumbnail(url=settings.currency_icon_url)

        if ledger:
            lines = []
            for row in ledger:
                amount = int(row["amount"])
                sign = "+" if amount >= 0 else "-"
                ts = int(row["created_at"])
                lines.append(
                    f"{sign}{abs(amount):,} {settings.currency_emoji} · "
                    f"{row['kind']} · <t:{ts}:R>"
                )
            embed.add_field(name="Recent activity", value="\n".join(lines), inline=False)
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
            colour=accent,
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
            colour=accent,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── transfers ────────────────────────────────────────────────────────

    @bank.command(name="pay", description="Send currency to another member.")
    @app_commands.describe(member="Who to pay", amount="How much (whole number)")
    async def bank_pay(
        self, interaction: discord.Interaction, member: discord.Member, amount: int
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        sender = interaction.user
        assert isinstance(sender, discord.Member)

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
            confirm = discord.Embed(
                title="Confirm payment",
                description=(
                    f"Send {settings.currency_emoji} **{amount:,}** "
                    f"{_unit(settings, amount)} to {member.mention}?"
                ),
                colour=accent,
            )
            view = _PayConfirmView(self, settings, guild, sender, member, amount)
            await interaction.response.send_message(
                embed=confirm, view=view, ephemeral=True
            )
            return

        await self.finalize_pay(
            interaction, settings, guild, sender, member, amount, via_confirm=False
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
        via_confirm: bool,
    ) -> None:
        """Execute the transfer and report — shared by the direct and confirm paths."""

        def _tx() -> int:
            with self.ctx.open_db() as conn:
                transfer_currency(conn, guild.id, sender.id, recipient.id, amount)
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
        embed = discord.Embed(
            title="Payment sent",
            description=(
                f"{settings.currency_emoji} **{amount:,}** {_unit(settings, amount)} "
                f"→ {recipient.mention}"
            ),
            colour=accent,
        )
        embed.set_footer(text=f"Your balance: {new_balance:,}")
        await self._reply_embed(interaction, embed, via_confirm=via_confirm)

        await notify_member(
            self.bot, self.ctx.db_path, guild.id, recipient.id,
            content=(
                f"{sender.display_name} sent you {settings.currency_emoji} "
                f"{amount:,} {_unit(settings, amount)}."
            ),
        )

    # ── shop ─────────────────────────────────────────────────────────────

    @bank.command(name="shop", description="Browse and rent personal-role perks.")
    async def bank_shop(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        user_id = interaction.user.id

        settings = await asyncio.to_thread(self._load_settings, guild.id)
        if not settings.enabled:
            await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
            return

        gated: set[str] = set()
        for perk in _FEATURE_GATED:
            if not await feature_gate_ok(self.bot, guild.id, perk):
                gated.add(perk)

        accent = await resolve_accent_color(self.ctx.db_path, guild)
        embed = discord.Embed(
            title="🛍️ Perk shop",
            description="Weekly rentals — billed every 7 days, cancel any time.",
            colour=accent,
        )
        for perk in _SELF_PERKS:
            price = _perk_price(settings, perk)
            note = (
                " · _requires a server feature not enabled here_"
                if perk in gated
                else ""
            )
            embed.add_field(
                name=_PERK_LABELS[perk],
                value=f"{settings.currency_emoji} **{price:,}** / week{note}",
                inline=False,
            )
        gift_price = _perk_price(settings, "gift_color")
        embed.add_field(
            name=_PERK_LABELS["gift_color"],
            value=(
                f"{settings.currency_emoji} **{gift_price:,}** / week · "
                "gift a friend a colour with /bank gift"
            ),
            inline=False,
        )
        view = _ShopView(self, settings, guild, user_id, gated)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def do_rent(
        self,
        interaction: discord.Interaction,
        settings: EconSettings,
        guild: discord.Guild,
        perk: str,
    ) -> None:
        """Rent a self-perk from a shop button, then project the role."""
        user_id = interaction.user.id

        def _rent() -> None:
            with self.ctx.open_db() as conn:
                rent_perk(conn, settings, guild.id, user_id, perk, now=time.time())

        try:
            await asyncio.to_thread(_rent)
        except ValueError as exc:
            msg = str(exc)
            if "insufficient" in msg:
                bal = await asyncio.to_thread(self._balance, guild.id, user_id)
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

        await apply_role_perks(self.bot, self.ctx.db_path, guild.id, user_id)
        await interaction.response.send_message(
            f"Rented **{_PERK_LABELS[perk]}**! Customise it with `/bank role`.",
            ephemeral=True,
        )

    # ── gift ─────────────────────────────────────────────────────────────

    @bank.command(name="gift", description="Gift a friend a custom colour.")
    @app_commands.describe(member="Who to gift a colour to")
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
                "Bots can't wear a colour.", ephemeral=True
            )
            return
        if member.id == gifter.id:
            await interaction.response.send_message(
                "Rent your own colour with /bank shop.", ephemeral=True
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
                text = "You're already gifting them a colour."
            else:
                text = "That gift isn't available."
            await interaction.response.send_message(text, ephemeral=True)
            return

        await apply_role_perks(self.bot, self.ctx.db_path, guild.id, member.id)
        await notify_member(
            self.bot, self.ctx.db_path, guild.id, member.id,
            content=(
                f"{gifter.display_name} gifted you a custom colour! "
                "Pick one with /bank role color."
            ),
        )
        await interaction.response.send_message(
            f"Gifted a custom colour to {member.mention}. They can set it with "
            "`/bank role color`.",
            ephemeral=True,
        )

    # ── role studio ──────────────────────────────────────────────────────

    @role.command(name="name", description="Set your personal role's name.")
    @app_commands.describe(text="The name (up to 32 characters)")
    async def role_name(self, interaction: discord.Interaction, text: str) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        user_id = interaction.user.id
        settings, ent = await asyncio.to_thread(self._load_role_ctx, guild.id, user_id)
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

    @role.command(name="color", description="Set your personal role's colour.")
    @app_commands.describe(hex="A hex colour like #7B2FF7")
    async def role_color(self, interaction: discord.Interaction, hex: str) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        user_id = interaction.user.id
        settings, ent = await asyncio.to_thread(self._load_role_ctx, guild.id, user_id)
        if not settings.enabled:
            await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
            return
        if "role_color" not in ent and "gift_color" not in ent:
            await interaction.response.send_message(
                "Rent the **Custom role colour** perk or get one gifted (/bank shop).",
                ephemeral=True,
            )
            return
        value = parse_hex_colour(hex)
        if value is None:
            await interaction.response.send_message(
                "Give a colour as a hex code like `#7B2FF7`.", ephemeral=True
            )
            return
        clash = find_color_clash(guild, value)
        if clash is not None:
            await interaction.response.send_message(
                f"That colour is too close to **{clash.name}** — pick another.",
                ephemeral=True,
            )
            return
        await asyncio.to_thread(
            self._upsert_role, guild.id, user_id, {"color": value}
        )
        await self._apply_and_confirm(
            interaction, guild.id, user_id, f"Your role colour is now `#{value:06X}`."
        )

    @role.command(name="gradient", description="Set a two-colour gradient role.")
    @app_commands.describe(hex1="First hex colour", hex2="Second hex colour")
    async def role_gradient(
        self, interaction: discord.Interaction, hex1: str, hex2: str
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        user_id = interaction.user.id
        settings, ent = await asyncio.to_thread(self._load_role_ctx, guild.id, user_id)
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
        v1, v2 = parse_hex_colour(hex1), parse_hex_colour(hex2)
        if v1 is None or v2 is None:
            await interaction.response.send_message(
                "Give both colours as hex codes like `#7B2FF7`.", ephemeral=True
            )
            return
        clash = find_color_clash(guild, v1) or find_color_clash(guild, v2)
        if clash is not None:
            await interaction.response.send_message(
                f"That colour is too close to **{clash.name}** — pick another.",
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

    @role.command(name="icon", description="Set your personal role's icon.")
    @app_commands.describe(
        emoji="A unicode emoji to use as the icon",
        image="Or upload an image (256KB max)",
    )
    async def role_icon(
        self,
        interaction: discord.Interaction,
        emoji: str | None = None,
        image: discord.Attachment | None = None,
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        user_id = interaction.user.id
        settings, ent = await asyncio.to_thread(self._load_role_ctx, guild.id, user_id)
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
        if not emoji and image is None:
            await interaction.response.send_message(
                "Give an emoji or upload an image.", ephemeral=True
            )
            return

        if image is not None:
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
        else:
            assert emoji is not None
            await asyncio.to_thread(
                self._upsert_role, guild.id, user_id, {"icon_path": emoji.strip()}
            )
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
        # gifted colour the leaver was funding.
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
    ) -> tuple[EconSettings, set[str]]:
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

    @bank.command(name="quests", description="View and claim the server's active quests.")
    async def bank_quests(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        guild = interaction.guild

        settings, quests_state = await asyncio.to_thread(
            self._load_quests_state, guild.id, interaction.user.id
        )
        if not settings.enabled:
            await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
            return

        accent = await resolve_accent_color(self.ctx.db_path, guild)
        embed = discord.Embed(title=f"{settings.currency_emoji} Quests", colour=accent)

        if not quests_state:
            embed.description = "_No active quests right now — check back soon!_"
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        for q in quests_state:
            reward = int(q["reward"])
            unit = _unit(settings, reward)
            header = f"{settings.currency_emoji} {q['title']}"
            lines = [f"**{reward:,}** {unit} · {q['qtype']}"]
            if q.get("description"):
                lines.append(str(q["description"]))
            if q["state"] == "community":
                lines.append(_progress_bar(q["current"], q["target"]))
            else:
                lines.append(_QUEST_STATE_LABEL.get(q["state"], ""))
            embed.add_field(name=header, value="\n".join(lines), inline=False)

        claimable = [q for q in quests_state if q["state"] == "claimable"]
        kwargs: dict = {"embed": embed, "ephemeral": True}
        if claimable:
            kwargs["view"] = QuestClaimView(self.ctx, settings, guild, claimable)
        await interaction.response.send_message(**kwargs)

    def _load_quests_state(
        self, guild_id: int, user_id: int
    ) -> tuple[EconSettings, list[dict]]:
        """Load active quests with the caller's per-period claim state.

        Community quests carry their running total (no self-claim); daily/weekly
        carry ``claimable``/``pending``/``done`` for this period's key.
        """
        with self.ctx.open_db() as conn:
            settings = load_econ_settings(conn, guild_id)
            if not settings.enabled:
                return settings, []
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
            out: list[dict] = []
            for row in rows:
                qtype = str(row["qtype"])
                quest_id = int(row["id"])
                entry: dict = {
                    "id": quest_id,
                    "title": row["title"],
                    "description": row["description"],
                    "qtype": qtype,
                    "reward": int(row["reward"]),
                    "signoff": bool(row["signoff"]),
                    "criteria": row["criteria"],
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
                    if claim is None:
                        entry["state"] = "claimable"
                    elif claim["state"] == "paid":
                        entry["state"] = "done"
                    else:
                        entry["state"] = "pending"
                out.append(entry)
        return settings, out

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

        try:
            if card_file is not None:
                message = await channel.send(file=card_file)
            else:
                embed = discord.Embed(
                    title="📣 Question of the Day",
                    description=question,
                    colour=accent,
                )
                message = await channel.send(embed=embed)
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
        embed = build_guide_embed(settings, colour=accent)

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

        # Moving or reposting: drop the stale panel if we can still find it.
        if settings.guide_message_id and settings.guide_channel_id:
            old_channel = guild.get_channel(settings.guide_channel_id)
            if isinstance(old_channel, discord.TextChannel):
                try:
                    old = await old_channel.fetch_message(settings.guide_message_id)
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
                    {"guide_channel_id": target.id, "guide_message_id": message.id},
                )

        await asyncio.to_thread(_save)
        await interaction.response.send_message(
            f"Posted the guide panel in {target.mention}.", ephemeral=True
        )

    async def cog_load(self) -> None:
        # Re-register the persistent sign-off buttons so Approve/Deny clicks on
        # existing bank-channel cards still route after a restart — the
        # custom_ids embed the claim id (econ_claim:{approve,deny}:<id>).
        self.bot.add_dynamic_items(QuestApproveButton, QuestDenyButton)

    def _load_settings(self, guild_id: int) -> EconSettings:
        with self.ctx.open_db() as conn:
            return load_econ_settings(conn, guild_id)


async def setup(bot: Bot) -> None:
    await bot.add_cog(EconomyCog(bot, bot.ctx))
