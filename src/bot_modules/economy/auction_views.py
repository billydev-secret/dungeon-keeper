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
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import discord

from bot_modules.core.branding import DEFAULT_ACCENT_COLOR, safe_resolve_accent
from bot_modules.core.sticky import PanelContent
from bot_modules.core.utils import jump_url
from bot_modules.economy.quest_views import can_manage_economy
from bot_modules.economy.view_helpers import coins as _coins
from bot_modules.economy.view_helpers import safe_ephemeral as _safe_ephemeral
from bot_modules.services.economy_auction_service import (
    SettledAuction,
    attach_card,
    bid_count,
    cancel_auction,
    end_auction_now,
    get_auction,
    get_open_auction,
    latest_auction,
    min_next_bid,
    open_auction,
    place_bid_now,
    settle_due_auctions,
)
from bot_modules.services.sticky_registry import (
    StickyResident,
    sticky_panel_channels,
)
from bot_modules.services.economy_service import (
    EconSettings,
    load_econ_settings,
    notify_member,
)
from bot_modules.core.branding import apply_section_spacing

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.economy")

__all__ = [
    "AuctionBidButton",
    "AuctionBidView",
    "build_auction_panel",
    "render_auction_card",
    "start_auction",
    "cancel_open_auction",
    "end_open_auction",
    "settle_and_announce",
]


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
            name="🎁 Up for Auction",
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
                name="🔨 Winning Bid",
                value=_coins(settings, int(auction["winning_bid"])),
                inline=True,
            )
            embed.add_field(
                name="Next Step",
                value="The host will hand over the prize.",
                inline=False,
            )
        else:
            embed.add_field(
                name="No Bids",
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
                name="🔨 Current Bid", value=_coins(settings, int(high)), inline=True
            )
            embed.add_field(
                name="🙋 High Bidder", value=f"<@{int(high_bidder)}>", inline=True
            )
        else:
            embed.add_field(
                name="🔨 Opening Bid",
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
            name="How It Works",
            value=(
                f"Tap **Bid** to bid at least {_coins(settings, min_next_bid(settings, auction))}. "
                "Outbid someone and they get their coins back instantly; the "
                "winning bid is spent. A late bid nudges the clock so it can't "
                "be sniped."
            ),
            inline=False,
        )
    apply_section_spacing(embed)
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


async def _load_settings(bot: Bot, guild_id: int) -> EconSettings:
    def _read() -> EconSettings:
        with bot.ctx.open_db() as conn:
            return load_econ_settings(conn, guild_id)

    return await asyncio.to_thread(_read)


async def _render(
    bot: Bot,
    guild: discord.Guild,
    auction_id: int,
    *,
    settings: EconSettings | None = None,
) -> tuple[discord.Embed, discord.ui.View | None, str] | None:
    """Build the card embed + view for an auction, plus its title (for reuse).

    Pass ``settings`` to skip the settings read when the caller already has it.
    Returns None if the auction row is gone."""
    if settings is None:
        settings = await _load_settings(bot, guild.id)
    accent = await safe_resolve_accent(bot, guild, log_label="auction", default=DEFAULT_ACCENT_COLOR)

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
    return embed, view, str(row["title"])


def _release_panel(bot: Bot, guild_id: int) -> None:
    """Drop the auction panel's cached ids for a guild.

    ``StickyPanel.on_message`` reads ids through a 300s TTL cache that is
    populated by *any* member message in the guild — and it caches "no panel"
    exactly as readily as a real id. Only ``place`` refreshes it (via
    ``_remember``), and this feature posts its card itself at both ends of the
    lifecycle, so both ends have to invalidate by hand:

    * **at start** — chat before the auction existed has almost certainly
      cached ``(0, 0)``. Without this the brand-new card does not re-stick
      until the entry lapses: the feature silently not working for up to five
      minutes.
    * **at close** — the cache still holds the pre-close message, so a restick
      would wake up, find it buried and try to place. ``build_auction_panel``
      refuses a finished auction so nothing would be posted, but clearing the
      entry means the wake-up (and its error log) never happens.
    """
    panel = getattr(bot.get_cog("EconomyCog"), "auction_panel", None)
    if panel is not None:
        panel.forget(guild_id)


async def build_auction_panel(
    bot: Bot, guild: discord.Guild
) -> PanelContent | None:
    """The guild's **open** auction as sticky-panel content, or None.

    ``StickyPanel.build`` for the auction card. It renders through the same
    ``_render`` the card itself uses, so a re-sticked card and a repainted one
    can never disagree.

    Returning None for anything but an open auction is the resurrection
    guard, and it is load-bearing rather than defensive. ``card_ids`` going to
    (0, 0) at close stops the restick *arming*, but a restick armed in the
    seconds before settlement can still be in flight, and ``_place_locked``
    treats a (0, 0) stored id as "not at the bottom" and would happily post a
    fresh card for the finished auction. Refusing to build is what makes that
    interleaving a no-op: ``build`` runs before ``send``, so the placement
    aborts having posted nothing.

    No signature is supplied: the card shows ``<t:…:R>`` countdowns and a
    live high bid, so "unchanged" is never true for long enough to be worth
    the comparison.
    """
    def _open_id() -> int:
        with bot.ctx.open_db() as conn:
            row = latest_auction(conn, guild.id)
            if row is None or str(row["state"]) != "open":
                return 0
            return int(row["id"])

    auction_id = await asyncio.to_thread(_open_id)
    if not auction_id:
        return None
    rendered = await _render(bot, guild, auction_id)
    if rendered is None:
        return None
    embed, view, _title = rendered
    return PanelContent(embed=embed, view=view or discord.utils.MISSING)


async def _refresh_card(
    bot: Bot,
    card: discord.Message | None,
    guild: discord.Guild,
    auction_id: int,
    *,
    settings: EconSettings | None = None,
) -> str | None:
    """Re-render the card from the current row; returns the auction title (or None).

    The title lets the caller (e.g. the outbid DM) reuse this read instead of a
    second one."""
    rendered = await _render(bot, guild, auction_id, settings=settings)
    if rendered is None:
        return None
    embed, view, title = rendered
    if card is not None:
        try:
            await card.edit(embed=embed, view=view)
        except discord.HTTPException:
            log.debug("econ auction: failed to edit card", exc_info=True)
    return title


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

    # One refresh, reusing the settings we already loaded; its returned title
    # feeds the outbid DM so we don't read the row a second time.
    title = await _refresh_card(bot, card, guild, auction_id, settings=settings)
    # Tell the member we just displaced that they're out — and refunded.
    if result.outbid_user_id is not None and result.outbid_user_id != member.id:
        # The degraded form (style guide: Pointing at things) — a channel
        # mention, not a permalink. The auction is still open, so its card is a
        # live sticky that gets deleted and reposted as chat moves; an id
        # captured now would be dead by the time they read this. The room is
        # the stable pointer, and the card is sitting at the bottom of it.
        where = f" It's in <#{card.channel.id}>." if card is not None else ""
        try:
            await notify_member(
                bot, bot.ctx.db_path, guild.id, result.outbid_user_id,
                content=(
                    f"You were outbid on **{title or 'an auction'}** "
                    f"— your {result.outbid_amount:,} coins are back. "
                    f"Bid again to reclaim it!{where}"
                ),
            )
        except Exception:
            log.debug("econ auction: failed to DM outbid member", exc_info=True)
    tail = " The clock was nudged to keep it fair." if result.extended else ""
    await _safe_ephemeral(
        interaction,
        f"🔨 You're the high bidder at {result.amount:,}.{tail}",
    )


# ── command-backed flows (start / cancel / end) ──────────────────────────────


@dataclass(frozen=True)
class _StickyCheck:
    """What is wrong with running an auction in this channel."""

    message: str
    #: True → refuse to open the auction at all. Reserved for the case where
    #: the card would be buried *reliably and silently*; a merely-degraded
    #: card still runs, with the message attached as a warning.
    blocking: bool


async def _sticky_check(
    bot: Bot, guild: discord.Guild, channel: discord.abc.Messageable
) -> _StickyCheck | None:
    """Why this channel won't give the card the behaviour the manual promises.

    Three ways to lose it. The first two warn — the auction still runs, and the
    mod is told the card will sit still — and the third blocks:

    * **a thread** — ``StickyPanel._channel`` resolves ids with
      ``guild.get_channel``, which never returns a thread, and ``_freeze_card``
      wants a ``TextChannel`` too. The card posts and works; it just never
      moves. (Auctions in threads predate stickiness, so this warns rather
      than blocks — the capability isn't ours to take away.)
    * **a resident sticky panel that only moves under human messages** (the
      economy and shop panels, pen pals, DM perms, Voice Control, the Guess Who
      prompt, the todo boards) — one bottom slot, two claimants, so the two
      trade places as people chat. Intermittent and visible; the mod can judge
      it.
    * **a resident sticky panel that re-sticks under bot messages** (the casino
      hub, the bounty board hub, the Survivor panel) — this one blocks. Those panels re-take the
      bottom after every card render, so the card loses the slot every single
      time and there is nothing the mod can do in the channel to keep it in
      view. Warning about a card that is guaranteed to vanish just documents a
      broken auction; refusing sends the mod somewhere it will work.

    Called *before* the auction is opened, so a block costs no escrow, no
    rollback and no burned single-live-auction slot.
    """
    if not isinstance(channel, discord.TextChannel):
        return _StickyCheck(
            message=(
                "The card can't stay at the bottom here — sticky panels only "
                "work in ordinary text channels, not threads or forum posts. "
                "The auction runs fine, but the card will stay where it was "
                "posted and won't move down as people chat."
            ),
            blocking=False,
        )

    def _resident() -> StickyResident | None:
        with bot.ctx.open_db() as conn:
            return sticky_panel_channels(conn, guild.id).get(channel.id)

    resident = await asyncio.to_thread(_resident)
    if resident is None:
        return None
    if resident.restick_on_bot:
        return _StickyCheck(
            message=(
                f"This channel is {resident.name}'s, and that panel follows "
                "the bot's own posts to stay at the bottom — so it would push "
                "the auction card out of view every time the card updated, and "
                "it would never come back. Run the auction in another channel."
            ),
            blocking=True,
        )
    return _StickyCheck(
        message=(
            f"This channel already has {resident.name} stuck to the bottom. "
            "Both can't be last, so the auction card will keep getting pushed "
            "above it. Run the auction somewhere else if you want the card to "
            "stay in view."
        ),
        blocking=False,
    )


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

    # Checked before _open(): a channel whose resident panel chases bot posts
    # can never show this card, and refusing now costs no escrow, no rollback
    # and no consumed single-live-auction slot.
    sticky = await _sticky_check(bot, guild, channel)
    if sticky is not None and sticky.blocking:
        await _safe_ephemeral(interaction, f"❌ {sticky.message}")
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

    # If we can't get a card in front of members, roll the auction back — a
    # committed-but-cardless auction blocks the single-live-auction slot and
    # can't be bid on. Cancelling is a clean no-op here (no bids yet).
    def _rollback() -> None:
        with bot.ctx.open_db() as conn:
            cancel_auction(conn, guild.id, auction_id, resolver_id=member.id)

    rendered = await _render(bot, guild, auction_id, settings=settings)
    if rendered is None:
        await asyncio.to_thread(_rollback)
        await _safe_ephemeral(interaction, "❌ Couldn't render the auction card.")
        return
    embed, _view, _title = rendered
    # A freshly-opened auction is always live, so it always gets a Bid button.
    try:
        message = await channel.send(embed=embed, view=AuctionBidView(auction_id))
    except discord.HTTPException:
        await asyncio.to_thread(_rollback)
        await _safe_ephemeral(
            interaction,
            "❌ I couldn't post the auction card here — check I can send "
            "messages and embeds in this channel, then try again.",
        )
        return

    def _attach() -> None:
        with bot.ctx.open_db() as conn:
            attach_card(conn, auction_id, channel.id, message.id)

    await asyncio.to_thread(_attach)
    # The card was posted here rather than through place(), so nothing has
    # told the panel it exists — see _release_panel. Without this the auction
    # does not stick at all for up to 300s.
    _release_panel(bot, guild.id)

    if sticky is not None:
        # Non-blocking by now (the blocking case returned before _open). The
        # card is posted and the auction is live either way — this only tells
        # the mod, who is standing right here choosing a channel, that the card
        # will not behave the way the manual promises.
        await _safe_ephemeral(
            interaction,
            f"🔨 Auction started — the card is live.\n\n⚠️ {sticky.message} "
            "`/bank auction cancel` refunds and clears this one if you'd "
            "rather move it.",
        )
        return
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
    _release_panel(bot, guild.id)  # closed in the DB already — see _announce_settlement
    card = await _card_message(bot, cancelled) if cancelled is not None else None
    await _refresh_card(bot, card, guild, auction_id)
    if cancelled is not None:
        # Cancelling ends the auction too, so the card freezes here the same
        # way it does at close — anyone who had bid should see the refund
        # notice at the bottom of the channel, not wherever chat left it.
        await _freeze_card(bot, guild, auction_id, int(cancelled["channel_id"] or 0))
    refunded = cancelled["high_bidder_id"] if cancelled is not None else None
    if refunded is not None:
        where = await _frozen_card_link(bot, guild.id, auction_id)
        try:
            await notify_member(
                bot, bot.ctx.db_path, guild.id, int(refunded),
                content=(
                    "An auction you were leading was cancelled — your bid is "
                    f"back.{where}"
                ),
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


async def _freeze_card(
    bot: Bot, guild: discord.Guild, auction_id: int, channel_id: int
) -> None:
    """Move the finished card to the bottom one last time, then stop forever.

    The card stops being re-sticked the moment the state leaves ``open``
    (``card_ids`` goes to (0, 0)), so whatever has buried it stays on top of
    it — and on the settle path that includes our own "Sold!" ping, which
    would leave the result buried under the very announcement of it. One
    repost puts the outcome where everyone is looking, and then it never moves
    again.

    Deliberately NOT routed through ``StickyPanel.place``: that path builds
    via ``build_auction_panel``, which refuses a finished auction precisely so
    a late restick can't resurrect one. Post-before-delete is kept by hand for
    the same reason it exists in ``core.sticky`` — a failed send must leave
    the working card alone.

    Runs exactly once per auction: every caller reaches it through the state
    claim in ``settle_due_auctions`` / ``end_auction_now`` / ``cancel_auction``,
    which only one caller can win.
    """
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return

    # Re-read the card id rather than trusting the caller's snapshot, which
    # was taken when the state claim won. A restick that placed between that
    # claim and now has already deleted the snapshotted message and recorded
    # a NEW one; deleting the stale id would then leave the re-sticked card —
    # rendered while the auction was still open, Bid button and all — sitting
    # above the frozen result forever.
    def _current() -> int:
        with bot.ctx.open_db() as conn:
            row = get_auction(conn, auction_id)
            return int(row["message_id"] or 0) if row is not None else 0

    message_id = await asyncio.to_thread(_current)
    if not message_id:
        return  # the auction never got a card (rolled back at start)

    rendered = await _render(bot, guild, auction_id)
    if rendered is None:
        return
    embed, view, _title = rendered
    try:
        fresh = await channel.send(embed=embed, view=view or discord.utils.MISSING)
    except discord.HTTPException:
        log.debug("econ auction: failed to repost the finished card", exc_info=True)
        return

    # attach_card, not attach_card_to_latest: we know the concrete auction.
    # The state-blind variant is only right on the save_ids path, where a
    # guild id is all there is — here it would write to whatever row is
    # newest, and a mod who starts the next auction while this send is in
    # flight would get their fresh card's ids overwritten by this dead one.
    def _attach() -> None:
        with bot.ctx.open_db() as conn:
            attach_card(conn, auction_id, channel.id, fresh.id)

    await asyncio.to_thread(_attach)
    try:
        await channel.get_partial_message(message_id).delete()
    except discord.HTTPException:
        pass


async def _frozen_card_link(bot: Bot, guild_id: int, auction_id: int) -> str:
    """A link line for the auction's card, for a DM sent AFTER the freeze.

    Only safe once ``_freeze_card`` has run. While an auction is open its card
    is a ``StickyPanel`` that gets deleted and reposted as chat moves, so any
    id snapshotted mid-auction is dead within minutes — the freeze reposts one
    last time, writes the new id, and then it never moves again. Hence the
    re-read: the caller's snapshot is the id the freeze just deleted.
    """
    def _read() -> tuple[int, int]:
        with bot.ctx.open_db() as conn:
            row = get_auction(conn, auction_id)
            if row is None:
                return 0, 0
            return int(row["channel_id"] or 0), int(row["message_id"] or 0)

    try:
        channel_id, message_id = await asyncio.to_thread(_read)
    except Exception:
        log.debug("econ auction: couldn't read the frozen card ids", exc_info=True)
        return ""
    if not channel_id or not message_id:
        return ""
    return f"\n{jump_url(guild_id, channel_id, message_id)}"


async def _announce_settlement(
    bot: Bot, guild: discord.Guild, settled: SettledAuction
) -> None:
    """Repaint the card as closed, post/ping the result, then freeze the card."""
    # First thing, before any awaiting on Discord: the auction is already
    # closed in the DB, so drop the panel's cached ids. A restick firing from
    # here on then reads a fresh (0, 0) and returns early instead of reaching
    # build_auction_panel's refusal — which is a correct stop, but core logs
    # it as an ERROR with a traceback, and an expected close is not an error.
    _release_panel(bot, guild.id)
    card = await _card_message(bot, {
        "channel_id": settled.channel_id, "message_id": settled.message_id
    })
    # Repaint in place first: if the freeze repost below can't post, the card
    # that stays on screen still reads "closed" rather than "bidding open".
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
    await _freeze_card(bot, guild, settled.auction_id, settled.channel_id)
    if settled.winner_id is not None:
        where = await _frozen_card_link(bot, guild.id, settled.auction_id)
        try:
            await notify_member(
                bot, bot.ctx.db_path, guild.id, settled.winner_id,
                content=(
                    f"🏆 You won the auction for **{settled.title}** at "
                    f"{settled.winning_bid:,} coins! The host will sort out "
                    f"your prize.{where}"
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
