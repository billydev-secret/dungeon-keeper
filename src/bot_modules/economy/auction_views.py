"""Discord views for live auctions — the sticky card, its Bid button, and the
handlers that drive it. Discord glue only; the money lives in
``economy_auction_service`` (escrow, refund, burn, the BEGIN IMMEDIATE bid path).

One **persistent** card per auction carries a single ``discord.ui.DynamicItem``
Bid button whose ``custom_id`` embeds the auction id (``econ_auction:bid:<id>``),
so clicks still route after a restart once the cog re-registers the class. Every
handler is fail-safe — a service error becomes an ephemeral note, never a dead
button. Bids serialize in the service (BEGIN IMMEDIATE); an outbid or busy bid
comes back as a friendly ephemeral and the card refreshes.

Start / cancel / end are mod commands (``/bank auction …`` on EconomyCog); this
module owns the card, the Bid flow, and the settle→announce that closes an
auction and pings the host and winner.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, cast

import discord

from bot_modules.core.branding import resolve_accent_color
from bot_modules.economy.quest_views import can_manage_economy
from bot_modules.services.economy_auction_service import (
    SettledAuction,
    attach_card,
    bid_count,
    cancel_auction,
    end_auction_now,
    get_auction,
    get_open_auction,
    min_next_bid,
    open_auction,
    place_bid_now,
    settle_due_auctions,
)
from bot_modules.services.economy_service import (
    EconSettings,
    load_econ_settings,
    notify_member,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.economy")

__all__ = [
    "AuctionBidButton",
    "AuctionBidView",
    "render_auction_card",
    "start_auction",
    "cancel_open_auction",
    "end_open_auction",
    "settle_and_announce",
]


def _coins(settings: EconSettings, amount: int) -> str:
    """``🪙 **250** coins`` — the shared currency vocabulary."""
    unit = (
        settings.currency_name
        if abs(amount) == 1
        else (settings.currency_plural or "coins")
    )
    return f"{settings.currency_emoji} **{amount:,}** {unit}"


def render_auction_card(
    accent: discord.Color,
    settings: EconSettings,
    auction,
    *,
    bids: int,
) -> discord.Embed:
    """The sticky card for an auction in its current state."""
    state = str(auction["state"])
    title = str(auction["title"])
    high = auction["high_bid"]
    high_bidder = auction["high_bidder_id"]

    if state == "closed":
        winner = auction["winner_id"]
        if winner is not None:
            embed = discord.Embed(
                title=f"🔨 Sold — {title}", color=discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title=f"🔨 Auction closed — {title}", color=accent
            )
    elif state == "cancelled":
        embed = discord.Embed(
            title=f"✖️ Auction cancelled — {title}", color=discord.Color.red()
        )
    else:
        embed = discord.Embed(title=f"🔨 Auction — {title}", color=accent)

    if auction["description"]:
        embed.add_field(
            name="🎁 Up for auction",
            value=str(auction["description"])[:1024],
            inline=False,
        )
    embed.add_field(
        name="🎙️ Hosted by", value=f"<@{int(auction['created_by'])}>", inline=True
    )

    if state == "closed":
        winner = auction["winner_id"]
        if winner is not None:
            embed.add_field(name="🏆 Winner", value=f"<@{int(winner)}>", inline=True)
            embed.add_field(
                name="🔨 Winning bid",
                value=_coins(settings, int(auction["winning_bid"])),
                inline=True,
            )
            embed.add_field(
                name="Next step",
                value="The host will hand over the prize.",
                inline=False,
            )
        else:
            embed.add_field(
                name="No bids",
                value="Nobody bid — nothing changes hands.",
                inline=False,
            )
        embed.timestamp = discord.utils.utcnow()
    elif state == "cancelled":
        embed.add_field(
            name="↩️ Refunded",
            value="The standing high bid was returned in full.",
            inline=False,
        )
        embed.timestamp = discord.utils.utcnow()
    else:
        if high is not None:
            embed.add_field(
                name="🔨 Current bid", value=_coins(settings, int(high)), inline=True
            )
            embed.add_field(
                name="🙋 High bidder", value=f"<@{int(high_bidder)}>", inline=True
            )
        else:
            embed.add_field(
                name="🔨 Opening bid",
                value=_coins(settings, min_next_bid(settings, auction)),
                inline=True,
            )
        embed.add_field(
            name="⏳ Ends",
            value=f"<t:{int(float(auction['ends_at']))}:R>",
            inline=True,
        )
        embed.add_field(name="🙌 Bids", value=str(bids), inline=True)
        embed.add_field(
            name="How it works",
            value=(
                f"Tap **Bid** to bid at least {_coins(settings, min_next_bid(settings, auction))}. "
                "Outbid someone and they get their coins back instantly; the "
                "winning bid is spent. A late bid nudges the clock so it can't "
                "be sniped."
            ),
            inline=False,
        )
    return embed


# ── modal + persistent button ────────────────────────────────────────────────


class _BidModal(discord.ui.Modal, title="Place your bid"):
    amount: discord.ui.TextInput = discord.ui.TextInput(
        label="How much?",
        placeholder="A whole number of coins",
        max_length=12,
    )

    def __init__(self, auction_id: int, card: discord.Message | None) -> None:
        super().__init__()
        self.auction_id = auction_id
        self.card = card

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await _handle_bid(interaction, self.auction_id, str(self.amount.value), self.card)


class AuctionBidButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_auction:bid:(?P<aid>\d+)"),
):
    def __init__(self, auction_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Bid", emoji="🔨",
                style=discord.ButtonStyle.success,
                custom_id=f"econ_auction:bid:{auction_id}",
            )
        )
        self.auction_id = auction_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match: re.Match[str]
    ) -> AuctionBidButton:
        return cls(int(match["aid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            _BidModal(self.auction_id, interaction.message)
        )


class AuctionBidView(discord.ui.View):
    """Persistent (timeout=None) single Bid button for one auction."""

    def __init__(self, auction_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(AuctionBidButton(auction_id))


# ── helpers ──────────────────────────────────────────────────────────────────


async def _safe_ephemeral(interaction: discord.Interaction, text: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        log.debug("econ auction: failed to send ephemeral", exc_info=True)


async def _load_settings(bot: Bot, guild_id: int) -> EconSettings:
    def _read() -> EconSettings:
        with bot.ctx.open_db() as conn:
            return load_econ_settings(conn, guild_id)

    return await asyncio.to_thread(_read)


async def _render(
    bot: Bot, guild: discord.Guild, auction_id: int
) -> tuple[discord.Embed, discord.ui.View | None] | None:
    settings = await _load_settings(bot, guild.id)
    accent = await resolve_accent_color(bot.ctx.db_path, guild)

    def _read():
        with bot.ctx.open_db() as conn:
            row = get_auction(conn, auction_id)
            if row is None:
                return None
            return row, bid_count(conn, auction_id)

    data = await asyncio.to_thread(_read)
    if data is None:
        return None
    row, bids = data
    embed = render_auction_card(accent, settings, row, bids=bids)
    view = AuctionBidView(auction_id) if str(row["state"]) == "open" else None
    return embed, view


async def _refresh_card(
    bot: Bot, card: discord.Message | None, guild: discord.Guild, auction_id: int
) -> None:
    if card is None:
        return
    rendered = await _render(bot, guild, auction_id)
    if rendered is None:
        return
    embed, view = rendered
    try:
        await card.edit(embed=embed, view=view)
    except discord.HTTPException:
        log.debug("econ auction: failed to edit card", exc_info=True)


async def _card_message(
    bot: discord.Client, auction
) -> discord.Message | None:
    channel_id = int(auction["channel_id"] or 0)
    message_id = int(auction["message_id"] or 0)
    if not channel_id or not message_id:
        return None
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        return None
    try:
        return await channel.fetch_message(message_id)
    except discord.HTTPException:
        return None


# ── the Bid handler ──────────────────────────────────────────────────────────


async def _handle_bid(
    interaction: discord.Interaction,
    auction_id: int,
    raw_amount: str,
    card: discord.Message | None,
) -> None:
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        await _safe_ephemeral(interaction, "❌ This only works in a server.")
        return
    bot = cast("Bot", interaction.client)
    await interaction.response.defer(ephemeral=True)

    try:
        amount = int(raw_amount.strip().replace(",", ""))
    except ValueError:
        await _safe_ephemeral(interaction, "❌ Enter a whole number of coins.")
        return
    if amount <= 0:
        await _safe_ephemeral(interaction, "❌ Enter a positive amount.")
        return

    settings = await _load_settings(bot, guild.id)

    def _bid():
        return place_bid_now(
            bot.ctx.db_path, settings, guild.id, auction_id, member.id, amount
        )

    try:
        result = await asyncio.to_thread(_bid)
    except ValueError as exc:
        await _refresh_card(bot, card, guild, auction_id)
        await _safe_ephemeral(interaction, f"❌ {exc}")
        return
    except Exception:
        log.exception("econ auction: bid failed for %s", auction_id)
        await _safe_ephemeral(interaction, "❌ Couldn't place that bid — try again.")
        return

    await _refresh_card(bot, card, guild, auction_id)
    # Tell the member we just displaced that they're out — and refunded.
    if result.outbid_user_id is not None and result.outbid_user_id != member.id:
        try:
            await notify_member(
                bot, bot.ctx.db_path, guild.id, result.outbid_user_id,
                content=(
                    f"You were outbid on **{await _auction_title(bot, auction_id)}** "
                    f"— your {result.outbid_amount:,} coins are back. Bid again to reclaim it!"
                ),
            )
        except Exception:
            log.debug("econ auction: failed to DM outbid member", exc_info=True)
    tail = " The clock was nudged to keep it fair." if result.extended else ""
    await _safe_ephemeral(
        interaction,
        f"🔨 You're the high bidder at {result.amount:,}.{tail}",
    )


async def _auction_title(bot: Bot, auction_id: int) -> str:
    def _read():
        with bot.ctx.open_db() as conn:
            row = get_auction(conn, auction_id)
            return str(row["title"]) if row else "an auction"

    return await asyncio.to_thread(_read)


# ── command-backed flows (start / cancel / end) ──────────────────────────────


async def start_auction(
    interaction: discord.Interaction,
    *,
    title: str,
    prize: str,
    duration_hours: float,
) -> None:
    """`/bank auction start` — open an auction and post its card here."""
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        await _safe_ephemeral(interaction, "❌ This only works in a server.")
        return
    bot = cast("Bot", interaction.client)
    await interaction.response.defer(ephemeral=True)
    settings = await _load_settings(bot, guild.id)
    if not can_manage_economy(member, settings):
        await _safe_ephemeral(interaction, "❌ You don't have permission to run auctions.")
        return
    if not settings.enabled:
        await _safe_ephemeral(interaction, "❌ The economy is turned off here.")
        return

    channel = interaction.channel
    if not isinstance(channel, discord.abc.Messageable):
        await _safe_ephemeral(interaction, "❌ Run this in a text channel.")
        return

    def _open() -> int:
        with bot.ctx.open_db() as conn:
            return open_auction(
                conn, settings, guild.id,
                created_by=member.id, title=title, description=prize,
                duration_hours=duration_hours, channel_id=channel.id,
            )

    try:
        auction_id = await asyncio.to_thread(_open)
    except ValueError as exc:
        await _safe_ephemeral(interaction, f"❌ {exc}")
        return
    except Exception:
        log.exception("econ auction: open failed in guild %s", guild.id)
        await _safe_ephemeral(interaction, "❌ Couldn't start the auction — try again.")
        return

    rendered = await _render(bot, guild, auction_id)
    if rendered is None:
        await _safe_ephemeral(interaction, "❌ Couldn't render the auction card.")
        return
    embed, _ = rendered
    # A freshly-opened auction is always live, so it always gets a Bid button.
    try:
        message = await channel.send(embed=embed, view=AuctionBidView(auction_id))
    except discord.HTTPException:
        await _safe_ephemeral(interaction, "❌ I couldn't post the auction card here.")
        return

    def _attach() -> None:
        with bot.ctx.open_db() as conn:
            attach_card(conn, auction_id, channel.id, message.id)

    await asyncio.to_thread(_attach)
    await _safe_ephemeral(interaction, "🔨 Auction started — the card is live.")


async def cancel_open_auction(interaction: discord.Interaction) -> None:
    """`/bank auction cancel` — cancel the live auction and refund the bid."""
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        await _safe_ephemeral(interaction, "❌ This only works in a server.")
        return
    bot = cast("Bot", interaction.client)
    await interaction.response.defer(ephemeral=True)
    settings = await _load_settings(bot, guild.id)
    if not can_manage_economy(member, settings):
        await _safe_ephemeral(interaction, "❌ You don't have permission to run auctions.")
        return

    def _cancel():
        with bot.ctx.open_db() as conn:
            row = get_open_auction(conn, guild.id)
            if row is None:
                return None
            aid = int(row["id"])
            cancelled = cancel_auction(conn, guild.id, aid, resolver_id=member.id)
            return aid, cancelled

    result = await asyncio.to_thread(_cancel)
    if result is None:
        await _safe_ephemeral(interaction, "❌ There's no live auction to cancel.")
        return
    auction_id, cancelled = result
    card = await _card_message(bot, cancelled) if cancelled is not None else None
    await _refresh_card(bot, card, guild, auction_id)
    refunded = cancelled["high_bidder_id"] if cancelled is not None else None
    if refunded is not None:
        try:
            await notify_member(
                bot, bot.ctx.db_path, guild.id, int(refunded),
                content="An auction you were leading was cancelled — your bid is back.",
            )
        except Exception:
            log.debug("econ auction: failed to DM refunded bidder", exc_info=True)
    await _safe_ephemeral(interaction, "✖️ Auction cancelled and any bid refunded.")


async def end_open_auction(interaction: discord.Interaction) -> None:
    """`/bank auction end` — close the live auction now and settle it."""
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        await _safe_ephemeral(interaction, "❌ This only works in a server.")
        return
    bot = cast("Bot", interaction.client)
    await interaction.response.defer(ephemeral=True)
    settings = await _load_settings(bot, guild.id)
    if not can_manage_economy(member, settings):
        await _safe_ephemeral(interaction, "❌ You don't have permission to run auctions.")
        return

    def _end() -> SettledAuction | None:
        with bot.ctx.open_db() as conn:
            row = get_open_auction(conn, guild.id)
            if row is None:
                return None
            return end_auction_now(conn, guild.id, int(row["id"]))

    settled = await asyncio.to_thread(_end)
    if settled is None:
        await _safe_ephemeral(interaction, "❌ There's no live auction to end.")
        return
    await _announce_settlement(bot, guild, settled)
    await _safe_ephemeral(interaction, "🔨 Auction closed.")


# ── settle → announce (background loop + /bank auction end) ───────────────────


async def _announce_settlement(
    bot: Bot, guild: discord.Guild, settled: SettledAuction
) -> None:
    """Repaint the card as closed and post/ping the result."""
    card = await _card_message(bot, {
        "channel_id": settled.channel_id, "message_id": settled.message_id
    })
    await _refresh_card(bot, card, guild, settled.auction_id)

    channel = bot.get_channel(settled.channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        return
    if settled.winner_id is not None:
        text = (
            f"🔨 **Sold!** <@{settled.winner_id}> won **{settled.title}** for "
            f"{settled.winning_bid:,}. <@{settled.created_by}>, time to hand over the prize."
        )
        allowed = discord.AllowedMentions(
            users=[discord.Object(settled.winner_id), discord.Object(settled.created_by)]
        )
    else:
        text = f"🔨 **{settled.title}** closed with no bids."
        allowed = discord.AllowedMentions.none()
    try:
        await channel.send(text, allowed_mentions=allowed)
    except discord.HTTPException:
        log.debug("econ auction: failed to post settlement", exc_info=True)
    if settled.winner_id is not None:
        try:
            await notify_member(
                bot, bot.ctx.db_path, guild.id, settled.winner_id,
                content=(
                    f"🏆 You won the auction for **{settled.title}** at "
                    f"{settled.winning_bid:,} coins! The host will sort out your prize."
                ),
            )
        except Exception:
            log.debug("econ auction: failed to DM winner", exc_info=True)


async def settle_and_announce(bot: Bot, guild: discord.Guild) -> None:
    """Close every auction past its end for a guild and announce each. Idempotent."""
    def _settle() -> list[SettledAuction]:
        with bot.ctx.open_db() as conn:
            return settle_due_auctions(conn, guild.id)

    try:
        settled = await asyncio.to_thread(_settle)
    except Exception:
        log.exception("econ auction: settle sweep failed for %s", guild.id)
        return
    for auction in settled:
        await _announce_settlement(bot, guild, auction)
