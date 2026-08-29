"""Discord views for Flash Themes — the paid themed day's approval card.

Same shape as ``pin_views``: a **persistent** Approve/Decline pair built from
``discord.ui.DynamicItem`` subclasses whose ``custom_id`` embeds the submission
id (``econ_theme_sub:approve:<id>`` / ``econ_theme_sub:deny:<id>``), so a click
still routes after a restart once the cog re-registers the classes.

The one real difference from the pin's card is that **Approve posts nothing**.
A pin goes up the moment a mod says yes; a theme joins a queue and runs when
the channel is next free, so approving is a pure database move and the
announcement is the loop's job (:func:`announce_theme`). That keeps the
heaviest failure mode — a Discord post that doesn't land — off the mod's
button and onto a sweep that simply tries again next hour.

Every handler is fail-safe: a service error becomes an ephemeral note, never a
dead button.
"""

from __future__ import annotations

import asyncio
import logging
import re
from functools import partial
from typing import TYPE_CHECKING, cast

import discord

from bot_modules.core.branding import safe_resolve_accent
from bot_modules.core.db_utils import open_db
from bot_modules.core.utils import safe_ephemeral as _core_safe_ephemeral
from bot_modules.economy.quest_views import can_manage_economy
from bot_modules.economy.view_helpers import coins as _reward_text
from bot_modules.economy.view_helpers import edit_review_card, refresh_review_card
from bot_modules.services.economy_service import (
    EconSettings,
    load_econ_settings,
    notify_member,
)
from bot_modules.services.economy_theme_service import (
    approve as approve_theme,
    deny,
    get_submission,
    go_live,
    queue_depth,
    set_submission_card,
    theme_window_seconds,
)
from bot_modules.services.embeds import COLOR_GREEN, COLOR_RED

if TYPE_CHECKING:
    from pathlib import Path

    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.economy")

MANAGE_DENIED_MSG = "❌ You don't have permission to review themed days."


def _hours_text(settings: EconSettings) -> str:
    hours = int(theme_window_seconds(settings) // 3600)
    return "24 hours" if hours == 24 else f"{hours} hours"


def render_theme_review_embed(
    accent: discord.Color,
    settings: EconSettings,
    *,
    sponsor_mention: str,
    title: str,
    blurb: str,
    price: int,
    state: str,
    resolver_id: int | None = None,
    deny_reason: str | None = None,
    refunded: bool = False,
) -> discord.Embed:
    """The bank-channel approval card for a submission in the given state.

    ``expired`` is three different endings — a request nobody reviewed in time
    (refunded), a theme that ran its whole window, and one a mod ended early
    (neither refunded) — so the state alone cannot say whether money went back.
    ``refunded`` carries that, read from the row's ``refunded_at``, which is the
    only thing that actually knows. Rendering the refund line off the state
    would tell a mod a themed day was refunded when it ran.
    """
    ended_unrefunded = state == "expired" and not refunded
    if state in ("approved", "live"):
        embed = discord.Embed(title="🎨 Theme Approved", color=discord.Color(COLOR_GREEN))
    elif ended_unrefunded:
        embed = discord.Embed(title="🎨 Theme Ended", color=accent)
    elif state in ("denied", "expired"):
        embed = discord.Embed(title="❌ Theme Declined", color=discord.Color(COLOR_RED))
    else:
        embed = discord.Embed(title="📋 Theme Requested", color=accent)

    embed.add_field(name="👤 From", value=sponsor_mention, inline=True)
    embed.add_field(name="💰 Paid", value=_reward_text(settings, price), inline=True)
    embed.add_field(name="🎨 Theme", value=title[:256], inline=False)
    embed.add_field(name="📝 The idea", value=blurb[:1024], inline=False)
    if state == "approved":
        # Deliberately not a queue position: the card is not re-rendered as the
        # queue drains, so a number here would be wrong within the hour. The
        # mod gets the live count in their ephemeral reply instead.
        embed.add_field(
            name="Queued",
            value="It runs the next time the channel is free.",
            inline=False,
        )
        if resolver_id:
            embed.add_field(name="Approved by", value=f"<@{resolver_id}>", inline=True)
    if state == "live":
        embed.add_field(
            name="Now",
            value=f"Running — announced and pinned for {_hours_text(settings)}.",
            inline=False,
        )
        if resolver_id:
            embed.add_field(name="Approved by", value=f"<@{resolver_id}>", inline=True)
    if ended_unrefunded:
        embed.add_field(
            name="Done",
            value="It had its day — no refund.",
            inline=False,
        )
        if resolver_id:
            embed.add_field(name="Ended by", value=f"<@{resolver_id}>", inline=True)
    elif state in ("denied", "expired"):
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


def render_theme_live_embed(
    accent: discord.Color,
    settings: EconSettings,
    *,
    sponsor_mention: str,
    title: str,
    blurb: str,
) -> discord.Embed:
    """The announcement — which is also the message that gets pinned.

    One message, not two: the announcement and the pin are the same thing, so
    the channel gets a single card that stays at the top for the window rather
    than an announcement scrolling away from a separate pinned copy.
    """
    embed = discord.Embed(
        title=f"🎨 Today's theme: {title[:200]}",
        description=blurb[:2048],
        color=accent,
    )
    embed.add_field(name="Themed by", value=sponsor_mention, inline=False)
    embed.set_footer(text=f"Flash Theme · running for {_hours_text(settings)}")
    embed.timestamp = discord.utils.utcnow()
    return embed


async def unpin_and_delete(
    bot: discord.Client, channel_id: int, message_id: int
) -> None:
    """Best-effort: unpin and delete a finished theme's announcement.

    Shared with the loop's expiry sweep. A missing channel/message (already
    gone, or the bot lost access) is not an error — the DB row is already
    retired. Deleting rather than merely unpinning matches Pin of the Day, and
    is what keeps an erased member's name from surviving in a stale
    announcement long after their data went.
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
        await message.unpin(reason="Flash theme ended")
    except discord.HTTPException:
        log.debug("econ theme: failed to unpin %s", message_id, exc_info=True)
    try:
        await message.delete()
    except discord.HTTPException:
        log.debug("econ theme: failed to delete %s", message_id, exc_info=True)


# ── persistent approval buttons ───────────────────────────────────────────────


class ThemeApproveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_theme_sub:approve:(?P<sid>\d+)"),
):
    """Persistent Approve button; ``custom_id`` carries the submission id."""

    def __init__(self, submission_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Approve",
                emoji="✅",
                style=discord.ButtonStyle.success,
                custom_id=f"econ_theme_sub:approve:{submission_id}",
            )
        )
        self.submission_id = submission_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> ThemeApproveButton:
        return cls(int(match["sid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _handle_resolution(
            interaction, self.submission_id, approve=True, deny_reason=None
        )


class _DenyReasonModal(discord.ui.Modal, title="Decline This Theme"):
    """Reason is optional but encouraged — the member gets it in a DM."""

    reason: discord.ui.TextInput = discord.ui.TextInput(
        label="Why? (shown to the member)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=400,
        placeholder="Too close to last week's, needs rewording…",
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


class ThemeDenyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_theme_sub:deny:(?P<sid>\d+)"),
):
    """Persistent Decline button; opens the reason modal before resolving."""

    def __init__(self, submission_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Decline",
                emoji="🚫",
                style=discord.ButtonStyle.danger,
                custom_id=f"econ_theme_sub:deny:{submission_id}",
            )
        )
        self.submission_id = submission_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> ThemeDenyButton:
        return cls(int(match["sid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            _DenyReasonModal(self.submission_id, interaction.message)
        )


class ThemeReviewView(discord.ui.View):
    """Persistent (timeout=None) Approve/Decline pair for one submission."""

    def __init__(self, submission_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(ThemeApproveButton(submission_id))
        self.add_item(ThemeDenyButton(submission_id))


_safe_ephemeral = partial(_core_safe_ephemeral, log_label="econ theme")


async def _handle_resolution(
    interaction: discord.Interaction,
    submission_id: int,
    *,
    approve: bool,
    deny_reason: str | None,
    card_message: discord.Message | None = None,
) -> None:
    """Gate, resolve, edit the card, DM. Never raises."""
    guild = interaction.guild
    member = interaction.user
    bot = cast("Bot", interaction.client)
    ctx = bot.ctx
    card = card_message if card_message is not None else interaction.message

    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException:
        log.debug("econ theme: failed to defer resolution", exc_info=True)

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
        log.exception("econ theme: failed to load submission %s", submission_id)
        await _safe_ephemeral(interaction, "❌ Couldn't load that — try again.")
        return
    if loaded is None:
        await _safe_ephemeral(interaction, "❌ That theme no longer exists.")
        return
    settings, row = loaded

    if not can_manage_economy(member, settings):
        await _safe_ephemeral(interaction, MANAGE_DENIED_MSG)
        return
    accent = await safe_resolve_accent(ctx, guild, log_label="theme")
    if str(row["state"]) != "pending":  # type: ignore[index]
        await _refresh_card(card, ctx, accent, settings, submission_id)
        await _safe_ephemeral(interaction, f"Already {row['state']}.")  # type: ignore[index]
        return

    def _resolve():
        with ctx.open_db() as conn:
            if approve:
                fresh = approve_theme(conn, submission_id, resolver_id=member.id)
                return fresh, queue_depth(conn, guild.id)
            fresh = deny(
                conn, submission_id, resolver_id=member.id,
                deny_reason=deny_reason or "",
            )
            return fresh, 0

    try:
        fresh, depth = await asyncio.to_thread(_resolve)
    except ValueError as exc:
        await _refresh_card(card, ctx, accent, settings, submission_id)
        await _safe_ephemeral(interaction, str(exc))
        return
    except Exception:
        log.exception("econ theme: failed to resolve %s", submission_id)
        await _safe_ephemeral(interaction, "❌ Couldn't resolve that — try again.")
        return

    await _edit_card(card, accent, settings, fresh)
    await _dm_sponsor(bot, ctx.db_path, guild, settings, fresh)
    if approve:
        ahead = max(0, depth - 1)
        when = "It runs next time the channel is free." if not ahead else (
            f"{ahead} ahead of it in the queue."
        )
        await _safe_ephemeral(interaction, f"Approved and queued. {when}")
    else:
        await _safe_ephemeral(interaction, "Declined and refunded.")



def _card_embed(accent, settings: EconSettings, row):
    return render_theme_review_embed(
        accent,
        settings,
        sponsor_mention=f"<@{int(row['user_id'])}>",
        title=str(row["title"]),
        blurb=str(row["blurb"]),
        price=int(row["price"]),
        state=str(row["state"]),
        resolver_id=int(row["resolver_id"]) if row["resolver_id"] else None,
        deny_reason=str(row["deny_reason"] or ""),
        refunded=row["refunded_at"] is not None,
    )


_edit_card = partial(edit_review_card, build_embed=_card_embed, log_label="econ theme")

_refresh_card = partial(
    refresh_review_card,
    read_row=get_submission,
    build_embed=_card_embed,
    log_label="econ theme",
)


def theme_resolution_dm_text(settings: EconSettings, row) -> str:
    """The member-facing receipt for a resolved submission."""
    unit = settings.currency_plural or "coins"
    state = str(row["state"])
    if state == "approved":
        return (
            f"🎨 Your theme **{row['title']}** was approved — it runs the next "
            "time the channel is free. You'll hear from me when it goes up."
        )
    if state == "live":
        return (
            f"🎨 Your theme **{row['title']}** is live now, announced and pinned "
            f"for {_hours_text(settings)}. Enjoy the day!"
        )
    # Only `refunded_at` knows whether money actually went back: `expired` is
    # reached by a request nobody reviewed (refunded) AND by a theme that ran
    # or was ended early (not refunded). Branching on the state alone told a
    # member their coins were returned when a mod ended their running theme.
    if row["refunded_at"] is None:
        return (
            f"🎨 Your themed day **{row['title']}** has ended. Thanks for "
            "setting the tone — no refund, you got your day."
        )
    reason = str(row["deny_reason"] or "")
    tail = f"\n**Why:** {reason}" if reason else ""
    return (
        f"🚫 Your themed day wasn't accepted, and your {int(row['price'])} {unit} "
        f"have been refunded.\n> {row['title']}{tail}"
    )


async def _dm_sponsor(
    bot: discord.Client,
    db_path: Path,
    guild: discord.Guild,
    settings: EconSettings,
    row,
) -> None:
    """Tell the member what happened. Best-effort; a closed DM is not an error.

    Takes a ``db_path`` rather than an AppContext because the loop announcing a
    queued theme has only the former.
    """
    if not int(row["user_id"]):
        return  # detached by an erasure — there is nobody left to tell
    try:
        await notify_member(
            bot, db_path, guild.id, int(row["user_id"]),
            content=theme_resolution_dm_text(settings, row),
        )
    except Exception:
        log.debug("econ theme: failed to DM member", exc_info=True)


async def announce_theme(
    bot: discord.Client,
    db_path: Path,
    guild: discord.Guild,
    settings: EconSettings,
    accent: discord.Color,
    row,
) -> bool:
    """Post + pin one queued theme's announcement, then flip it live.

    Called by the hourly loop when the theme channel is free. Returns True if
    the theme actually went live.

    The Discord post happens BEFORE the state move, matching Pin of the Day: a
    post that fails leaves the row ``approved``, so the sweep simply tries
    again next hour and nobody is charged for a day that never ran. If the
    state move then fails the row was resolved out from under us (withdrawn,
    and already refunded), so the orphan announcement is taken straight back
    down.
    """
    channel = guild.get_channel(int(settings.theme_channel_id))
    if not isinstance(channel, discord.abc.Messageable):
        log.warning("econ theme: no usable theme channel in guild %d", guild.id)
        return False

    embed = render_theme_live_embed(
        accent, settings,
        sponsor_mention=(f"<@{int(row['user_id'])}>" if int(row["user_id"]) else "a member"),
        title=str(row["title"]),
        blurb=str(row["blurb"]),
    )
    try:
        posted = await channel.send(embed=embed)
    except discord.HTTPException:
        log.warning(
            "econ theme: couldn't post in the theme channel for guild %d", guild.id
        )
        return False
    try:
        await posted.pin(reason="Flash theme")
    except discord.HTTPException:
        # The card is already in the channel but could not be pinned (no Manage
        # Messages, or the channel is at Discord's 50-pin limit). Take it back
        # down: the row stays `approved`, so leaving it would post a fresh
        # unpinned copy every hour for as long as the condition lasts.
        log.warning(
            "econ theme: couldn't pin in the theme channel for guild %d", guild.id
        )
        try:
            await posted.delete()
        except discord.HTTPException:
            log.debug("econ theme: failed to clean up unpinned card", exc_info=True)
        return False

    def _golive():
        with open_db(db_path) as conn:
            return go_live(
                conn, int(row["id"]),
                theme_channel_id=channel.id,
                theme_message_id=posted.id,
                window_seconds=theme_window_seconds(settings),
            )

    try:
        fresh = await asyncio.to_thread(_golive)
    except ValueError:
        await unpin_and_delete(bot, channel.id, posted.id)
        return False
    except Exception:
        log.exception("econ theme: go_live failed for %s", row["id"])
        await unpin_and_delete(bot, channel.id, posted.id)
        return False

    await _restamp_card(bot, accent, settings, fresh)
    await _dm_sponsor(bot, db_path, guild, settings, fresh)
    return True


async def _restamp_card(
    bot: discord.Client, accent: discord.Color, settings: EconSettings, row
) -> None:
    """Re-render the bank-channel card once a queued theme actually goes live.

    The loop holds no card object — only the ids recorded at submit — so this
    fetches before editing. Entirely best-effort: the theme is already running
    and the member already told, so a lost card is cosmetic.
    """
    channel_id, message_id = int(row["card_channel_id"]), int(row["card_message_id"])
    if not channel_id or not message_id:
        return
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        return
    try:
        card = await channel.fetch_message(message_id)
        await card.edit(embed=_card_embed(accent, settings, row), view=None)
    except discord.HTTPException:
        log.debug("econ theme: failed to restamp card %s", message_id, exc_info=True)


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

    The pending row already exists and the member has already paid, so a
    missing or forbidden bank channel must never raise back to them.
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
        embed = render_theme_review_embed(
            accent,
            settings,
            sponsor_mention=sponsor.mention,
            title=str(row["title"]),
            blurb=str(row["blurb"]),
            price=int(row["price"]),
            state="pending",
        )
        message = await channel.send(embed=embed, view=ThemeReviewView(submission_id))
    except discord.HTTPException:
        log.warning("econ theme: failed to post review card for %s", submission_id)
        return
    except Exception:
        log.exception("econ theme: unexpected error posting card %s", submission_id)
        return

    def _record() -> None:
        with ctx.open_db() as conn:
            set_submission_card(conn, submission_id, channel.id, message.id)

    try:
        await asyncio.to_thread(_record)
    except Exception:
        log.debug("econ theme: failed to record card ids", exc_info=True)
