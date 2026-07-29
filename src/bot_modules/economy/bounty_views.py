"""Discord views for Community Bounty — the board card and its buttons.

One **persistent** card per bounty carries three ``discord.ui.DynamicItem``
buttons whose ``custom_id`` embeds the bounty id (``econ_bounty:chip:<id>`` /
``:award:<id>`` / ``:cancel:<id>``), so clicks still route after a restart once
the cog re-registers the classes:

* 💰 **Chip in** — any member; opens an amount modal and escrows into the pot.
* 🏆 **Award** — mod only; opens a ``UserSelect`` and pays the winner minus rake.
* **Cancel** — mod only; refunds every contributor.

Above the cards sits one **hub panel** per guild, stuck to the bottom of the
board channel by ``core.sticky`` (see ``EconomyCog.bounty_panel``). It explains
the mechanic and lists the open bounties, and carries the board's only two
entry points — 🎯 **Post a bounty** and 💰 **Chip in** (which picks a bounty from
a select, since the hub is not attached to any one card). It replaced the
``/bounty`` command on 2026-07-29.

Every handler is fail-safe — a service error becomes an ephemeral note, never a
dead button. Two mods resolving at once is settled in the service (the state
guard); the loser gets the card refreshed.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, cast

import discord

from bot_modules.core.branding import resolve_accent_color
from bot_modules.economy.quest_views import can_manage_economy
from bot_modules.economy.view_helpers import coins as _coins
from bot_modules.economy.view_helpers import safe_ephemeral as _safe_ephemeral
from bot_modules.services.economy_bounty_service import (
    HUB_LIST_LIMIT,
    BountyBoardEntry,
    award_bounty,
    board_entries,
    cancel_bounty,
    contribute,
    contributor_count,
    get_bounty,
    open_board_count,
    pot_of,
    set_bounty_card,
)
from bot_modules.services.economy_service import (
    EconSettings,
    load_econ_settings,
    notify_member,
)
from bot_modules.services.embeds import COLOR_GREEN, COLOR_RED

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.economy")

MANAGE_DENIED_MSG = "❌ You don't have permission to award or cancel bounties."


def render_bounty_card(
    accent: discord.Color,
    settings: EconSettings,
    bounty,
    *,
    pot: int,
    contributors: int,
) -> discord.Embed:
    """The board card for a bounty in its current state."""
    state = str(bounty["state"])
    title = str(bounty["title"])
    if state == "awarded":
        embed = discord.Embed(
            title=f"🏆 Bounty Awarded — {title}", color=discord.Color(COLOR_GREEN)
        )
    elif state in ("cancelled", "expired"):
        verb = "Cancelled" if state == "cancelled" else "Expired"
        embed = discord.Embed(
            title=f"✖️ Bounty {verb} — {title}", color=discord.Color(COLOR_RED)
        )
    else:
        embed = discord.Embed(title=f"🎯 Bounty — {title}", color=accent)

    if bounty["description"]:
        embed.add_field(name="Task", value=str(bounty["description"])[:1024], inline=False)
    embed.add_field(name="👤 Posted by", value=f"<@{int(bounty['poster_id'])}>", inline=True)

    if state == "awarded":
        embed.add_field(name="🏆 Winner", value=f"<@{int(bounty['winner_id'])}>", inline=True)
        embed.add_field(name="💰 Paid out", value=_coins(settings, int(bounty["payout"])), inline=True)
        if int(bounty["rake_amount"]) > 0:
            embed.add_field(
                name="🏦 House cut", value=_coins(settings, int(bounty["rake_amount"])), inline=True
            )
    elif state in ("cancelled", "expired"):
        embed.add_field(
            name="↩️ Refunded",
            value="Everyone who chipped in got their coins back.",
            inline=False,
        )
    else:
        embed.add_field(name="💰 Pot", value=_coins(settings, pot), inline=True)
        embed.add_field(
            name="🙌 Contributors",
            value=str(contributors),
            inline=True,
        )
        rake = max(0, min(100, int(settings.bounty_rake_pct)))
        note = "Chip in to grow the pot. A mod awards it to whoever gets it done"
        if rake > 0:
            note += f" (the house keeps {rake}% on award)"
        embed.add_field(name="How it works", value=note + ".", inline=False)
    if state != "open":
        embed.timestamp = discord.utils.utcnow()
    return embed


# ── hub panel ────────────────────────────────────────────────────────────────


def _jump_url(guild_id: int, entry: BountyBoardEntry) -> str | None:
    """The card's jump link, or None when the card never posted (ids are 0)."""
    if not entry.card_channel_id or not entry.card_message_id:
        return None
    return (
        f"https://discord.com/channels/{guild_id}"
        f"/{entry.card_channel_id}/{entry.card_message_id}"
    )


def build_bounty_hub_embed(
    accent: discord.Color,
    settings: EconSettings,
    guild_id: int,
    entries: list[BountyBoardEntry],
    *,
    open_total: int,
) -> discord.Embed:
    """The board's sticky hub: what a bounty is, plus every open one.

    Deliberately does NOT repeat the per-card "How it works" line — the blurb
    here is the *first* thing a member reads in the channel, so it explains the
    mechanic end to end (including the refund promise, which the open-state card
    never mentions); the card's version is the reminder next to a live pot.
    """
    embed = discord.Embed(title="🎯 The Bounty Board", color=accent)
    rake = max(0, min(100, int(settings.bounty_rake_pct)))
    days = max(0, int(settings.bounty_expire_days))
    unit = settings.currency_plural or "coins"

    blurb = [
        f"**Post a task** and seed it with {unit} — anyone can chip in to grow "
        "the pot.",
        "**A mod awards the pot** to whoever gets it done.",
    ]
    if rake > 0:
        blurb.append(f"The house keeps **{rake}%** on award.")
    if days > 0:
        blurb.append(
            f"Nobody awarded it within **{days} days**? Everyone who chipped in "
            "is refunded in full."
        )
    embed.add_field(name="How it works", value="\n".join(blurb), inline=False)

    if not entries:
        embed.add_field(
            name="📋 Open bounties",
            value="Nothing on the board yet — post the first one.",
            inline=False,
        )
        return embed

    lines: list[str] = []
    for entry in entries:
        backers = (
            "no backers yet"
            if entry.contributors == 0
            else f"{entry.contributors} backer{'' if entry.contributors == 1 else 's'}"
        )
        head = f"**{entry.title}** — {_coins(settings, entry.pot)} · {backers}"
        url = _jump_url(guild_id, entry)
        lines.append(f"• {head} · [jump]({url})" if url else f"• {head}")
    # board_entries() caps the list; say so rather than silently showing a
    # partial board (the count comes from a separate COUNT over all open rows).
    hidden = open_total - len(entries)
    if hidden > 0:
        lines.append(f"…and **{hidden}** more further up the channel.")
    embed.add_field(name="📋 Open bounties", value="\n".join(lines), inline=False)
    return embed


# ── modals / selects ───────────────────────────────────────────────────────


class _ChipInModal(discord.ui.Modal, title="Chip in to this bounty"):
    amount: discord.ui.TextInput = discord.ui.TextInput(
        label="How much?",
        placeholder="A whole number of coins",
        max_length=12,
    )

    def __init__(
        self,
        bounty_id: int,
        card: discord.Message | None,
        *,
        refresh_by_ids: bool = False,
    ) -> None:
        super().__init__()
        self.bounty_id = bounty_id
        self.card = card
        #: Set when the chip-in came from the hub panel, which has no card
        #: message to hand us — refetch the card by its stored ids instead.
        self.refresh_by_ids = refresh_by_ids

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await _handle_chip(
            interaction,
            self.bounty_id,
            str(self.amount.value),
            self.card,
            refresh_by_ids=self.refresh_by_ids,
        )


class _AwardSelect(discord.ui.UserSelect):
    def __init__(self, bounty_id: int, card: discord.Message | None) -> None:
        super().__init__(placeholder="Who gets the bounty?", min_values=1, max_values=1)
        self.bounty_id = bounty_id
        self.card = card

    async def callback(self, interaction: discord.Interaction) -> None:
        await _handle_award(interaction, self.bounty_id, self.values[0], self.card)


class _AwardSelectView(discord.ui.View):
    """Ephemeral member picker shown to a mod after they click Award."""

    def __init__(self, bounty_id: int, card: discord.Message | None) -> None:
        super().__init__(timeout=300)
        self.add_item(_AwardSelect(bounty_id, card))


# ── persistent card buttons ──────────────────────────────────────────────────


class BountyChipInButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_bounty:chip:(?P<bid>\d+)"),
):
    def __init__(self, bounty_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Chip in", emoji="💰",
                style=discord.ButtonStyle.success,
                custom_id=f"econ_bounty:chip:{bounty_id}",
            )
        )
        self.bounty_id = bounty_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match: re.Match[str]
    ) -> BountyChipInButton:
        return cls(int(match["bid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            _ChipInModal(self.bounty_id, interaction.message)
        )


class BountyAwardButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_bounty:award:(?P<bid>\d+)"),
):
    def __init__(self, bounty_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Award", emoji="🏆",
                style=discord.ButtonStyle.primary,
                custom_id=f"econ_bounty:award:{bounty_id}",
            )
        )
        self.bounty_id = bounty_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match: re.Match[str]
    ) -> BountyAwardButton:
        return cls(int(match["bid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _gate_manage(interaction):
            return
        await interaction.response.send_message(
            "Pick who earned this bounty:",
            view=_AwardSelectView(self.bounty_id, interaction.message),
            ephemeral=True,
        )


class BountyCancelButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_bounty:cancel:(?P<bid>\d+)"),
):
    def __init__(self, bounty_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Cancel",
                style=discord.ButtonStyle.danger,
                custom_id=f"econ_bounty:cancel:{bounty_id}",
            )
        )
        self.bounty_id = bounty_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match: re.Match[str]
    ) -> BountyCancelButton:
        return cls(int(match["bid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _handle_cancel(interaction, self.bounty_id)


class BountyBoardView(discord.ui.View):
    """Persistent (timeout=None) Chip-in / Award / Cancel trio for one bounty."""

    def __init__(self, bounty_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(BountyChipInButton(bounty_id))
        self.add_item(BountyAwardButton(bounty_id))
        self.add_item(BountyCancelButton(bounty_id))


# ── persistent hub buttons ───────────────────────────────────────────────────
#
# The hub carries no per-bounty state, so both custom_ids are constant — but
# they are still DynamicItems so the whole file registers through the one
# ``add_dynamic_items`` call the cog already makes for the card buttons.

#: Discord's hard cap on options in a single select.
_SELECT_MAX = 25


class _HubChipSelect(discord.ui.Select):
    """Pick which open bounty to chip into, then the amount modal."""

    def __init__(self, entries: list[BountyBoardEntry], settings: EconSettings) -> None:
        super().__init__(
            placeholder="Which bounty?",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=entry.title[:100],
                    value=str(entry.bounty_id),
                    description=f"Pot: {_coins(settings, entry.pot)}"[:100],
                )
                for entry in entries
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        # card=None: the chip-in came from the hub, not from a card, so this
        # interaction has no card message to edit. _handle_chip escrows either
        # way; refresh_card_by_id below repaints the real card from its ids.
        await interaction.response.send_modal(
            _ChipInModal(int(self.values[0]), None, refresh_by_ids=True)
        )


class _HubChipView(discord.ui.View):
    """Ephemeral bounty picker shown after a member clicks Chip in on the hub."""

    def __init__(self, entries: list[BountyBoardEntry], settings: EconSettings) -> None:
        super().__init__(timeout=300)
        self.add_item(_HubChipSelect(entries, settings))


class BountyHubPostButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_bounty:hub_post"),
):
    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                label="Post a bounty", emoji="🎯",
                style=discord.ButtonStyle.primary,
                custom_id="econ_bounty:hub_post",
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match: re.Match[str]
    ) -> BountyHubPostButton:
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        # The modal lives on the cog (it calls back into do_bounty_post), and a
        # modal must be the interaction's FIRST response — so the cog opens it
        # rather than handing one back here.
        cog = cast("Bot", interaction.client).get_cog("EconomyCog")
        # getattr, not a direct call: importing EconomyCog here to type it would
        # be circular (the cog imports this module for its buttons).
        opener = getattr(cog, "open_bounty_post_modal", None)
        if opener is None:  # pragma: no cover — cog is always loaded in practice
            await _safe_ephemeral(interaction, "❌ The economy is unavailable.")
            return
        await opener(interaction)


class BountyHubChipButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_bounty:hub_chip"),
):
    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                label="Chip in", emoji="💰",
                style=discord.ButtonStyle.success,
                custom_id="econ_bounty:hub_chip",
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match: re.Match[str]
    ) -> BountyHubChipButton:
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await _safe_ephemeral(interaction, "❌ This only works in a server.")
            return
        bot = cast("Bot", interaction.client)

        def _read() -> tuple[EconSettings, list[BountyBoardEntry]]:
            with bot.ctx.open_db() as conn:
                return (
                    load_econ_settings(conn, guild.id),
                    board_entries(conn, guild.id, limit=_SELECT_MAX),
                )

        settings, entries = await asyncio.to_thread(_read)
        if not entries:
            await interaction.response.send_message(
                "There are no open bounties to chip into yet — post one!",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Pick the bounty you want to add to:",
            view=_HubChipView(entries, settings),
            ephemeral=True,
        )


class BountyHubView(discord.ui.View):
    """Persistent (timeout=None) Post / Chip-in pair on the board's hub panel."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(BountyHubPostButton())
        self.add_item(BountyHubChipButton())


async def build_bounty_hub_panel(
    bot: Bot, guild: discord.Guild
) -> tuple[discord.Embed, BountyHubView]:
    """Render the hub for ``guild`` — the cog's StickyPanel ``build`` callback."""
    accent = await resolve_accent_color(bot.ctx.db_path, guild)

    def _read() -> tuple[EconSettings, list[BountyBoardEntry], int]:
        # One connection for settings, list and count: this runs on every
        # sticky repost, so a connect+PRAGMA per read would be the hot waste.
        with bot.ctx.open_db() as conn:
            return (
                load_econ_settings(conn, guild.id),
                board_entries(conn, guild.id, limit=HUB_LIST_LIMIT),
                open_board_count(conn, guild.id),
            )

    settings, entries, open_total = await asyncio.to_thread(_read)
    return (
        build_bounty_hub_embed(
            accent, settings, guild.id, entries, open_total=open_total
        ),
        BountyHubView(),
    )


# ── helpers ──────────────────────────────────────────────────────────────────


async def _gate_manage(interaction: discord.Interaction) -> bool:
    """True if the clicker may award/cancel; otherwise reply and return False."""
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        await _safe_ephemeral(interaction, "❌ This only works in a server.")
        return False
    bot = cast("Bot", interaction.client)

    def _load() -> EconSettings:
        with bot.ctx.open_db() as conn:
            return load_econ_settings(conn, guild.id)

    settings = await asyncio.to_thread(_load)
    if not can_manage_economy(member, settings):
        await _safe_ephemeral(interaction, MANAGE_DENIED_MSG)
        return False
    return True


async def _load_settings(bot: Bot, guild_id: int) -> EconSettings:
    def _read() -> EconSettings:
        with bot.ctx.open_db() as conn:
            return load_econ_settings(conn, guild_id)

    return await asyncio.to_thread(_read)


async def _refresh_card(
    bot: Bot, card: discord.Message | None, guild: discord.Guild, bounty_id: int
) -> None:
    """Re-render a card from the current row (after any state move)."""
    if card is None:
        return
    settings = await _load_settings(bot, guild.id)
    accent = await resolve_accent_color(bot.ctx.db_path, guild)

    def _read():
        with bot.ctx.open_db() as conn:
            row = get_bounty(conn, bounty_id)
            if row is None:
                return None
            return row, pot_of(conn, bounty_id), contributor_count(conn, bounty_id)

    data = await asyncio.to_thread(_read)
    if data is None:
        return
    row, pot, contributors = data
    view = BountyBoardView(bounty_id) if str(row["state"]) == "open" else None
    try:
        await card.edit(
            embed=render_bounty_card(accent, settings, row, pot=pot, contributors=contributors),
            view=view,
        )
    except discord.HTTPException:
        log.debug("econ bounty: failed to edit card", exc_info=True)


# ── handlers ─────────────────────────────────────────────────────────────────


async def _refresh_card_by_stored_ids(
    bot: Bot, guild: discord.Guild, bounty_id: int
) -> None:
    """Repaint a bounty's card when the caller has no message to edit.

    The hub's Chip in button is one interaction removed from the card, so the
    card's ids are the only handle on it. Best-effort like every other refresh:
    the coins are already escrowed by the time this runs.
    """

    def _ids() -> tuple[int, int] | None:
        with bot.ctx.open_db() as conn:
            row = get_bounty(conn, bounty_id)
            if row is None:
                return None
            return int(row["card_channel_id"]), int(row["card_message_id"])

    ids = await asyncio.to_thread(_ids)
    if ids is None:
        return
    await refresh_card_by_id(bot, guild, ids[0], ids[1], bounty_id)


async def refresh_bounty_hub(bot: discord.Client, guild: discord.Guild) -> None:
    """Nudge the board's hub panel to repaint after a pot changed.

    Takes a bare ``Client`` like :func:`refresh_card_by_id` so the economy loop
    can call it with the bot it holds. Best-effort and never raises: a stale pot
    on the hub is cosmetic (the card is authoritative), so this must not turn a
    successful chip-in into an error.
    """
    cog = cast("Bot", bot).get_cog("EconomyCog")
    refresh = getattr(cog, "refresh_bounty_hub_panel", None)
    if refresh is None:
        return
    try:
        await refresh(guild)
    except Exception:
        log.debug("econ bounty: hub refresh failed", exc_info=True)


async def _handle_chip(
    interaction: discord.Interaction,
    bounty_id: int,
    raw_amount: str,
    card: discord.Message | None,
    *,
    refresh_by_ids: bool = False,
) -> None:
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        await _safe_ephemeral(interaction, "❌ This only works in a server.")
        return
    bot = cast("Bot", interaction.client)
    await interaction.response.defer(ephemeral=True)

    try:
        amount = int(raw_amount.strip())
    except ValueError:
        await _safe_ephemeral(interaction, "❌ Enter a whole number of coins.")
        return
    if amount <= 0:
        await _safe_ephemeral(interaction, "❌ Enter a positive amount.")
        return

    settings = await _load_settings(bot, guild.id)

    def _contribute() -> int:
        with bot.ctx.open_db() as conn:
            return contribute(conn, settings, guild.id, bounty_id, member.id, amount)

    try:
        pot = await asyncio.to_thread(_contribute)
    except ValueError as exc:
        await _safe_ephemeral(interaction, f"❌ {exc}")
        return
    except Exception:
        log.exception("econ bounty: chip-in failed for %s", bounty_id)
        await _safe_ephemeral(interaction, "❌ Couldn't add that — try again.")
        return

    if refresh_by_ids:
        await _refresh_card_by_stored_ids(bot, guild, bounty_id)
    else:
        await _refresh_card(bot, card, guild, bounty_id)
    await refresh_bounty_hub(bot, guild)
    await _safe_ephemeral(
        interaction, f"💰 Chipped in {amount:,} — the pot is now {pot:,}."
    )


async def _handle_award(
    interaction: discord.Interaction,
    bounty_id: int,
    winner: discord.User | discord.Member,
    card: discord.Message | None,
) -> None:
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        await _safe_ephemeral(interaction, "❌ This only works in a server.")
        return
    bot = cast("Bot", interaction.client)
    await interaction.response.defer(ephemeral=True)

    settings = await _load_settings(bot, guild.id)
    if not can_manage_economy(member, settings):
        await _safe_ephemeral(interaction, MANAGE_DENIED_MSG)
        return

    def _award():
        with bot.ctx.open_db() as conn:
            return award_bounty(
                conn, settings, guild.id, bounty_id,
                winner_id=winner.id, resolver_id=member.id,
            )

    try:
        result = await asyncio.to_thread(_award)
    except ValueError as exc:
        await _refresh_card(bot, card, guild, bounty_id)
        await _safe_ephemeral(interaction, f"❌ {exc}")
        return
    except Exception:
        log.exception("econ bounty: award failed for %s", bounty_id)
        await _safe_ephemeral(interaction, "❌ Couldn't award that — try again.")
        return

    await _refresh_card(bot, card, guild, bounty_id)
    # The bounty just left the open list the hub renders.
    await refresh_bounty_hub(bot, guild)
    try:
        await notify_member(
            bot, bot.ctx.db_path, guild.id, winner.id,
            content=(
                f"🏆 You were awarded the bounty **{result.bounty['title']}** — "
                f"{result.payout:,} coins are in your wallet!"
            ),
        )
    except Exception:
        log.debug("econ bounty: failed to DM winner", exc_info=True)
    tail = f" (house kept {result.rake:,})" if result.rake else ""
    await _safe_ephemeral(
        interaction,
        f"🏆 Awarded to {winner.mention} — {result.payout:,} paid out{tail}.",
    )


async def _handle_cancel(interaction: discord.Interaction, bounty_id: int) -> None:
    if not await _gate_manage(interaction):
        return
    guild = interaction.guild
    member = interaction.user
    assert guild is not None and isinstance(member, discord.Member)
    bot = cast("Bot", interaction.client)
    card = interaction.message
    await interaction.response.defer(ephemeral=True)

    def _cancel():
        with bot.ctx.open_db() as conn:
            return cancel_bounty(conn, guild.id, bounty_id, resolver_id=member.id)

    try:
        _row, refunded = await asyncio.to_thread(_cancel)
    except ValueError as exc:
        await _refresh_card(bot, card, guild, bounty_id)
        await _safe_ephemeral(interaction, f"❌ {exc}")
        return
    except Exception:
        log.exception("econ bounty: cancel failed for %s", bounty_id)
        await _safe_ephemeral(interaction, "❌ Couldn't cancel that — try again.")
        return

    await _refresh_card(bot, card, guild, bounty_id)
    # The bounty just left the open list the hub renders.
    await refresh_bounty_hub(bot, guild)
    for uid in refunded:
        try:
            await notify_member(
                bot, bot.ctx.db_path, guild.id, uid,
                content="A bounty you chipped into was cancelled — your coins are back.",
            )
        except Exception:
            log.debug("econ bounty: failed to DM refunded contributor", exc_info=True)
    await _safe_ephemeral(
        interaction, f"✖️ Cancelled — refunded {len(refunded)} contributor(s)."
    )


async def refresh_card_by_id(
    bot: discord.Client,
    guild: discord.Guild,
    channel_id: int,
    message_id: int,
    bounty_id: int,
) -> None:
    """Fetch a board card by ids and re-render it — used by the expiry sweep."""
    if not channel_id or not message_id:
        return
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        return
    try:
        card = await channel.fetch_message(message_id)
    except discord.HTTPException:
        return
    await _refresh_card(cast("Bot", bot), card, guild, bounty_id)


async def post_bounty_card(
    bot: Bot,
    ctx: AppContext,
    guild: discord.Guild,
    settings: EconSettings,
    accent: discord.Color,
    bounty_id: int,
) -> None:
    """Best-effort: post the board card to the bounty channel and record its ids."""
    channel = guild.get_channel(int(settings.bounty_channel_id))
    if not isinstance(channel, discord.abc.Messageable):
        return

    def _read():
        with ctx.open_db() as conn:
            row = get_bounty(conn, bounty_id)
            if row is None:
                return None
            return row, pot_of(conn, bounty_id), contributor_count(conn, bounty_id)

    try:
        data = await asyncio.to_thread(_read)
        if data is None:
            return
        row, pot, contributors = data
        embed = render_bounty_card(accent, settings, row, pot=pot, contributors=contributors)
        message = await channel.send(embed=embed, view=BountyBoardView(bounty_id))
    except discord.HTTPException:
        log.warning("econ bounty: failed to post card for %s", bounty_id)
        return
    except Exception:
        log.exception("econ bounty: unexpected error posting card %s", bounty_id)
        return

    def _record() -> None:
        with ctx.open_db() as conn:
            set_bounty_card(conn, bounty_id, channel.id, message.id)

    try:
        await asyncio.to_thread(_record)
    except Exception:
        log.debug("econ bounty: failed to record card ids", exc_info=True)
