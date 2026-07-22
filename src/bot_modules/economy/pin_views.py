"""Discord views for Pin of the Day — the paid pin's mod-approval card.

Same shape as ``sponsor_views``: a **persistent** Approve/Decline pair built from
``discord.ui.DynamicItem`` subclasses whose ``custom_id`` embeds the submission
id (``econ_pin_sub:approve:<id>`` / ``econ_pin_sub:deny:<id>``), so a click still
routes after a restart once the cog re-registers the classes.

Approve is heavier than the sponsor's: it posts the live "📌 Pinned by @X" card
to the pin channel and pins it, then flips the row to ``live`` (superseding any
prior live pin, which it unpins). The Discord post happens BEFORE the DB move, so
a failed post leaves the row pending and refundable — the member is never charged
for a pin nobody saw. Decline opens a reason modal and refunds.

Every handler is fail-safe — a service error becomes an ephemeral note, never a
dead button.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, cast

import discord

from bot_modules.core.branding import resolve_accent_color
from bot_modules.economy.quest_views import can_manage_economy
from bot_modules.services.economy_pin_service import (
    deny,
    get_submission,
    go_live,
    refund_failed_golive,
    set_submission_card,
)
from bot_modules.services.economy_service import (
    EconSettings,
    load_econ_settings,
    notify_member,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.economy")

MANAGE_DENIED_MSG = "❌ You don't have permission to review pinned messages."


def _reward_text(settings: EconSettings, amount: int) -> str:
    """``🪙 **300** coins`` — the currency vocabulary every economy card uses."""
    unit = (
        settings.currency_name
        if abs(amount) == 1
        else (settings.currency_plural or "coins")
    )
    return f"{settings.currency_emoji} **{amount:,}** {unit}"


def render_pin_review_embed(
    accent: discord.Color,
    settings: EconSettings,
    *,
    sponsor_mention: str,
    message: str,
    price: int,
    state: str,
    resolver_id: int | None = None,
    deny_reason: str | None = None,
) -> discord.Embed:
    """The bank-channel approval card for a submission in the given state."""
    if state == "live":
        embed = discord.Embed(title="📌 Pin Approved", color=discord.Color.green())
    elif state in ("denied", "expired", "superseded"):
        embed = discord.Embed(title="❌ Pin Declined", color=discord.Color.red())
    else:
        embed = discord.Embed(title="📋 Pin Requested", color=accent)

    embed.add_field(name="👤 From", value=sponsor_mention, inline=True)
    embed.add_field(name="💰 Paid", value=_reward_text(settings, price), inline=True)
    embed.add_field(name="✏️ Message", value=message[:1024], inline=False)
    if state == "live":
        embed.add_field(
            name="Now",
            value="Pinned for 24 hours — it auto-unpins after that.",
            inline=False,
        )
        if resolver_id:
            embed.add_field(name="Approved by", value=f"<@{resolver_id}>", inline=True)
    if state in ("denied", "expired", "superseded"):
        if resolver_id:
            embed.add_field(name="Declined by", value=f"<@{resolver_id}>", inline=True)
        if deny_reason:
            embed.add_field(name="Reason", value=deny_reason[:1024], inline=False)
        embed.add_field(
            name="↩️ Refund",
            value=f"{_reward_text(settings, price)} returned",
            inline=True,
        )
    if state != "pending":
        embed.timestamp = discord.utils.utcnow()
    return embed


def render_pin_live_embed(
    accent: discord.Color,
    *,
    sponsor_mention: str,
    message: str,
) -> discord.Embed:
    """The card that actually gets pinned in the pin channel."""
    embed = discord.Embed(
        title="📌 Pinned by a member",
        description=message[:2048],
        color=accent,
    )
    embed.add_field(name="Paid to pin this", value=sponsor_mention, inline=False)
    embed.set_footer(text="Pin of the Day · up for 24 hours")
    embed.timestamp = discord.utils.utcnow()
    return embed


async def unpin_and_delete(
    bot: discord.Client, channel_id: int, message_id: int
) -> None:
    """Best-effort: unpin and delete a live pin's Discord message.

    Shared with the loop's expiry sweep. A missing channel/message (already gone,
    or the bot lost access) is not an error — the DB row is already retired.
    """
    if not channel_id or not message_id:
        return
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        return
    try:
        message = await channel.fetch_message(message_id)
    except discord.HTTPException:
        return
    try:
        await message.unpin(reason="Pin of the Day expired")
    except discord.HTTPException:
        log.debug("econ pin: failed to unpin %s", message_id, exc_info=True)
    try:
        await message.delete()
    except discord.HTTPException:
        log.debug("econ pin: failed to delete %s", message_id, exc_info=True)


# ── persistent approval buttons ───────────────────────────────────────────────


class PinApproveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_pin_sub:approve:(?P<sid>\d+)"),
):
    """Persistent Approve button; ``custom_id`` carries the submission id."""

    def __init__(self, submission_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Approve",
                emoji="✅",
                style=discord.ButtonStyle.success,
                custom_id=f"econ_pin_sub:approve:{submission_id}",
            )
        )
        self.submission_id = submission_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> PinApproveButton:
        return cls(int(match["sid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _handle_resolution(
            interaction, self.submission_id, approve=True, deny_reason=None
        )


class _DenyReasonModal(discord.ui.Modal, title="Decline This Pin"):
    """Reason is optional but encouraged — the member gets it in a DM."""

    reason: discord.ui.TextInput = discord.ui.TextInput(
        label="Why? (shown to the member)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=400,
        placeholder="Off-topic, ad, needs rewording…",
    )

    def __init__(self, submission_id: int, card: discord.Message | None) -> None:
        super().__init__()
        self.submission_id = submission_id
        self.card = card

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await _handle_resolution(
            interaction,
            self.submission_id,
            approve=False,
            deny_reason=str(self.reason.value or ""),
            card_message=self.card,
        )


class PinDenyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_pin_sub:deny:(?P<sid>\d+)"),
):
    """Persistent Decline button; opens the reason modal before resolving."""

    def __init__(self, submission_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Decline",
                emoji="🚫",
                style=discord.ButtonStyle.danger,
                custom_id=f"econ_pin_sub:deny:{submission_id}",
            )
        )
        self.submission_id = submission_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> PinDenyButton:
        return cls(int(match["sid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            _DenyReasonModal(self.submission_id, interaction.message)
        )


class PinReviewView(discord.ui.View):
    """Persistent (timeout=None) Approve/Decline pair for one submission."""

    def __init__(self, submission_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(PinApproveButton(submission_id))
        self.add_item(PinDenyButton(submission_id))


async def _safe_ephemeral(interaction: discord.Interaction, text: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        log.debug("econ pin: failed to send ephemeral", exc_info=True)


async def _handle_resolution(
    interaction: discord.Interaction,
    submission_id: int,
    *,
    approve: bool,
    deny_reason: str | None,
    card_message: discord.Message | None = None,
) -> None:
    """Gate, resolve, (for approve: post+pin), edit the card, DM. Never raises."""
    guild = interaction.guild
    member = interaction.user
    bot = cast("Bot", interaction.client)
    ctx = bot.ctx
    card = card_message if card_message is not None else interaction.message

    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException:
        log.debug("econ pin: failed to defer resolution", exc_info=True)

    if guild is None or not isinstance(member, discord.Member):
        await _safe_ephemeral(interaction, "❌ This only works in a server.")
        return

    def _load() -> tuple[EconSettings, object] | None:
        with ctx.open_db() as conn:
            row = get_submission(conn, submission_id)
            if row is None:
                return None
            return load_econ_settings(conn, guild.id), row

    try:
        loaded = await asyncio.to_thread(_load)
    except Exception:
        log.exception("econ pin: failed to load submission %s", submission_id)
        await _safe_ephemeral(interaction, "❌ Couldn't load that — try again.")
        return
    if loaded is None:
        await _safe_ephemeral(interaction, "❌ That submission no longer exists.")
        return
    settings, row = loaded

    if not can_manage_economy(member, settings):
        await _safe_ephemeral(interaction, MANAGE_DENIED_MSG)
        return
    if str(row["state"]) != "pending":  # type: ignore[index]
        accent = await resolve_accent_color(ctx.db_path, guild)
        await _refresh_card(card, ctx, accent, settings, submission_id)
        await _safe_ephemeral(interaction, f"Already {row['state']}.")  # type: ignore[index]
        return

    accent = await resolve_accent_color(ctx.db_path, guild)

    if not approve:
        await _do_deny(interaction, ctx, guild, settings, accent, submission_id,
                       card, deny_reason or "", member.id)
        return
    await _do_approve(interaction, bot, ctx, guild, settings, accent,
                      submission_id, row, card, member.id)


async def _do_deny(
    interaction, ctx, guild: discord.Guild, settings, accent, submission_id, card,
    deny_reason: str, resolver_id: int,
) -> None:
    def _resolve():
        with ctx.open_db() as conn:
            return deny(
                conn, submission_id, resolver_id=resolver_id, deny_reason=deny_reason
            )

    try:
        fresh = await asyncio.to_thread(_resolve)
    except ValueError as exc:
        await _refresh_card(card, ctx, accent, settings, submission_id)
        await _safe_ephemeral(interaction, str(exc))
        return
    except Exception:
        log.exception("econ pin: failed to deny %s", submission_id)
        await _safe_ephemeral(interaction, "❌ Couldn't resolve that — try again.")
        return
    await _edit_card(card, accent, settings, fresh)
    await _dm_sponsor(interaction.client, ctx, guild, settings, fresh)
    await _safe_ephemeral(interaction, "Declined and refunded.")


async def _do_approve(
    interaction, bot, ctx, guild: discord.Guild, settings, accent, submission_id,
    row, card, resolver_id: int,
) -> None:
    """Post + pin the live card, then flip the row to live (superseding any prior).

    The Discord post is first: if it fails, the row stays pending and we refund,
    so the member is never charged for a pin that never showed.
    """
    channel = guild.get_channel(int(settings.pin_channel_id))
    if not isinstance(channel, discord.abc.Messageable):
        await _refund_and_report(
            interaction, ctx, guild, settings, accent, row, card,
            "❌ No pin channel is set here — refunded. Set one on the dashboard.",
        )
        return

    live_embed = render_pin_live_embed(
        accent, sponsor_mention=f"<@{int(row['user_id'])}>", message=str(row["message"])
    )
    try:
        posted = await channel.send(embed=live_embed)
        await posted.pin(reason="Pin of the Day")
    except discord.HTTPException:
        log.warning("econ pin: failed to post/pin for %s", submission_id)
        await _refund_and_report(
            interaction, ctx, guild, settings, accent, row, card,
            "❌ Couldn't post or pin in the pin channel (permissions?) — refunded.",
        )
        return

    def _golive():
        with ctx.open_db() as conn:
            return go_live(
                conn, submission_id, resolver_id=resolver_id,
                pin_channel_id=channel.id, pin_message_id=posted.id,
            )

    try:
        result = await asyncio.to_thread(_golive)
    except ValueError as exc:
        # Raced (declined/resolved between load and now) — undo the orphan pin.
        await unpin_and_delete(bot, channel.id, posted.id)
        await _refresh_card(card, ctx, accent, settings, submission_id)
        await _safe_ephemeral(interaction, str(exc))
        return
    except Exception:
        log.exception("econ pin: go_live failed for %s", submission_id)
        await unpin_and_delete(bot, channel.id, posted.id)
        await _safe_ephemeral(interaction, "❌ Couldn't finalise that — try again.")
        return

    # Retire the pin this one replaced (best-effort Discord cleanup).
    if result.superseded is not None:
        await unpin_and_delete(
            bot,
            int(result.superseded["pin_channel_id"]),
            int(result.superseded["pin_message_id"]),
        )

    await _edit_card(card, accent, settings, result.live)
    await _dm_sponsor(bot, ctx, guild, settings, result.live)
    await _safe_ephemeral(interaction, "Approved — pinned for 24 hours.")


async def _refund_and_report(
    interaction, ctx, guild: discord.Guild, settings, accent, row, card, msg: str,
) -> None:
    """Refund a pin that couldn't be posted and tell the mod."""
    def _refund():
        with ctx.open_db() as conn:
            refund_failed_golive(conn, row)
            return get_submission(conn, int(row["id"]))

    try:
        fresh = await asyncio.to_thread(_refund)
    except Exception:
        log.exception("econ pin: refund-on-post-failure errored for %s", row["id"])
        fresh = None
    if fresh is not None:
        await _edit_card(card, accent, settings, fresh)
        await _dm_sponsor(interaction.client, ctx, guild, settings, fresh)
    await _safe_ephemeral(interaction, msg)


def _card_embed(accent, settings: EconSettings, row) -> discord.Embed:
    return render_pin_review_embed(
        accent,
        settings,
        sponsor_mention=f"<@{int(row['user_id'])}>",
        message=str(row["message"]),
        price=int(row["price"]),
        state=str(row["state"]),
        resolver_id=int(row["resolver_id"]) if row["resolver_id"] else None,
        deny_reason=str(row["deny_reason"] or ""),
    )


async def _edit_card(
    card: discord.Message | None, accent, settings: EconSettings, row
) -> None:
    if card is None:
        return
    try:
        await card.edit(embed=_card_embed(accent, settings, row), view=None)
    except discord.HTTPException:
        log.debug("econ pin: failed to edit card", exc_info=True)


async def _refresh_card(
    card: discord.Message | None,
    ctx: AppContext,
    accent,
    settings: EconSettings,
    submission_id: int,
) -> None:
    """Re-render a card whose row moved underneath it (dashboard or race)."""
    if card is None:
        return

    def _read():
        with ctx.open_db() as conn:
            return get_submission(conn, submission_id)

    try:
        row = await asyncio.to_thread(_read)
    except Exception:
        log.debug("econ pin: failed to reload for refresh", exc_info=True)
        return
    if row is not None:
        await _edit_card(card, accent, settings, row)


def pin_resolution_dm_text(settings: EconSettings, row) -> str:
    """The member-facing receipt for a resolved submission."""
    unit = settings.currency_plural or "coins"
    if str(row["state"]) == "live":
        return (
            "📌 Your message is pinned for the next 24 hours — enjoy the spotlight!\n"
            f"> {row['message']}"
        )
    reason = str(row["deny_reason"] or "")
    tail = f"\n**Why:** {reason}" if reason else ""
    return (
        f"🚫 Your pin wasn't accepted, and your {int(row['price'])} {unit} have "
        f"been refunded.\n> {row['message']}{tail}"
    )


async def _dm_sponsor(
    bot: Bot, ctx: AppContext, guild: discord.Guild, settings: EconSettings, row
) -> None:
    """Tell the member what happened. Best-effort; a closed DM is not an error."""
    text = pin_resolution_dm_text(settings, row)
    try:
        await notify_member(bot, ctx.db_path, guild.id, int(row["user_id"]), content=text)
    except Exception:
        log.debug("econ pin: failed to DM member", exc_info=True)


async def post_review_card(
    bot: Bot,
    ctx: AppContext,
    guild: discord.Guild,
    settings: EconSettings,
    accent: discord.Color,
    submission_id: int,
    sponsor: discord.Member,
) -> None:
    """Best-effort: post the review card to the bank channel and record its ids.

    The pending row already exists and the member has already paid, so a missing
    or forbidden bank channel must never raise back to them.
    """
    if not settings.bank_channel_id:
        return
    channel = guild.get_channel(settings.bank_channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        return

    def _read():
        with ctx.open_db() as conn:
            return get_submission(conn, submission_id)

    try:
        row = await asyncio.to_thread(_read)
        if row is None:
            return
        embed = render_pin_review_embed(
            accent,
            settings,
            sponsor_mention=sponsor.mention,
            message=str(row["message"]),
            price=int(row["price"]),
            state="pending",
        )
        message = await channel.send(embed=embed, view=PinReviewView(submission_id))
    except discord.HTTPException:
        log.warning("econ pin: failed to post review card for %s", submission_id)
        return
    except Exception:
        log.exception("econ pin: unexpected error posting card %s", submission_id)
        return

    def _record() -> None:
        with ctx.open_db() as conn:
            set_submission_card(conn, submission_id, channel.id, message.id)

    try:
        await asyncio.to_thread(_record)
    except Exception:
        log.debug("econ pin: failed to record card ids", exc_info=True)
