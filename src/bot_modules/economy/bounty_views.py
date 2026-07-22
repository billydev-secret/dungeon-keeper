"""Discord views for Community Bounty — the board card and its buttons.

One **persistent** card per bounty carries three ``discord.ui.DynamicItem``
buttons whose ``custom_id`` embeds the bounty id (``econ_bounty:chip:<id>`` /
``:award:<id>`` / ``:cancel:<id>``), so clicks still route after a restart once
the cog re-registers the classes:

* 💰 **Chip in** — any member; opens an amount modal and escrows into the pot.
* 🏆 **Award** — mod only; opens a ``UserSelect`` and pays the winner minus rake.
* ✖️ **Cancel** — mod only; refunds every contributor.

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
from bot_modules.services.economy_bounty_service import (
    award_bounty,
    cancel_bounty,
    contribute,
    contributor_count,
    get_bounty,
    pot_of,
    set_bounty_card,
)
from bot_modules.services.economy_service import (
    EconSettings,
    load_econ_settings,
    notify_member,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.economy")

MANAGE_DENIED_MSG = "❌ You don't have permission to award or cancel bounties."


def _coins(settings: EconSettings, amount: int) -> str:
    """``🪙 **250** coins`` — the shared currency vocabulary."""
    unit = (
        settings.currency_name
        if abs(amount) == 1
        else (settings.currency_plural or "coins")
    )
    return f"{settings.currency_emoji} **{amount:,}** {unit}"


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
            title=f"🏆 Bounty Awarded — {title}", color=discord.Color.green()
        )
    elif state in ("cancelled", "expired"):
        verb = "Cancelled" if state == "cancelled" else "Expired"
        embed = discord.Embed(
            title=f"✖️ Bounty {verb} — {title}", color=discord.Color.red()
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


# ── modals / selects ───────────────────────────────────────────────────────


class _ChipInModal(discord.ui.Modal, title="Chip in to this bounty"):
    amount: discord.ui.TextInput = discord.ui.TextInput(
        label="How much?",
        placeholder="A whole number of coins",
        max_length=12,
    )

    def __init__(self, bounty_id: int, card: discord.Message | None) -> None:
        super().__init__()
        self.bounty_id = bounty_id
        self.card = card

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await _handle_chip(interaction, self.bounty_id, str(self.amount.value), self.card)


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
                label="Cancel", emoji="✖️",
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


# ── helpers ──────────────────────────────────────────────────────────────────


async def _safe_ephemeral(interaction: discord.Interaction, text: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        log.debug("econ bounty: failed to send ephemeral", exc_info=True)


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


async def _handle_chip(
    interaction: discord.Interaction,
    bounty_id: int,
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

    await _refresh_card(bot, card, guild, bounty_id)
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
