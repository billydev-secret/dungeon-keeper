"""Discord views for sponsor-a-QOTD — the paid question's mod-approval card.

Same shape as the quest sign-off card in ``quest_views``: a **persistent**
Approve/Deny pair built from ``discord.ui.DynamicItem`` subclasses whose
``custom_id`` embeds the submission id (``econ_qotd_sub:approve:<id>`` /
``econ_qotd_sub:deny:<id>``), so a click still routes after a restart once the
cog re-registers the classes. Approve queues the question; Deny opens a reason
modal and refunds.

Every handler is fail-safe — a service error becomes an ephemeral note, never a
dead button. Two mods clicking at once is resolved in the service (the state
guard), and the loser gets the card refreshed rather than a second refund.

The card is reviewed in two places, and the buttons below serve both: a post in
the economy's staff-only ``approvals_channel_id`` (``approval_views.
post_approval_card``, dark until that dial is set), and the ephemeral detail
behind the todo board's 🧾 Approvals button. Resolving on either closes both —
the board reads the queue live, and the channel card is repainted through the
ids stored on the row (``view_helpers.edit_stored_card``). Between 2026-08-29
and 2026-09-02 the board was the only surface; before that, cards went to
``bank_channel_id``, which in the main guild members can read. Those legacy
cards stay clickable, since the DynamicItems never stopped being registered.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, cast
from functools import partial

import discord

from bot_modules.core.branding import safe_resolve_accent
from bot_modules.economy.quest_views import can_manage_economy
from bot_modules.services.economy_qotd_sponsor_service import (
    get_submission,
    resolve_submission,
)
from bot_modules.services.economy_service import (
    EconSettings,
    load_econ_settings,
    notify_member,
)
from bot_modules.services.embeds import COLOR_GREEN, COLOR_RED
from bot_modules.services.name_resolver import (
    NameFn,
    mention,
    named_or_anonymous,
)
from bot_modules.core.utils import safe_ephemeral as _core_safe_ephemeral
from bot_modules.economy.view_helpers import (
    card_name_fn,
    edit_stored_card,
    edit_review_card,
    refresh_review_card,
    refresh_todo_board,
    review_surface,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.economy")

MANAGE_DENIED_MSG = "❌ You don't have permission to review sponsored questions."


def _reward_text(settings: EconSettings, amount: int) -> str:
    """``🪙 **500** coins`` — the currency vocabulary every economy card uses.

    Mirrors ``quest_views._reward_text`` so a sponsored question's Paid/Refund
    fields read like the rest of the economy: emoji, bold amount with thousands
    separators, and a unit that goes singular at 1.
    """
    unit = settings.currency_name if abs(amount) == 1 else (settings.currency_plural or "coins")
    return f"{settings.currency_emoji} **{amount:,}** {unit}"


def render_sponsor_card_embed(
    accent: discord.Color,
    settings: EconSettings,
    *,
    sponsor_id: int,
    name_fn: NameFn = mention,
    question: str,
    price: int,
    state: str,
    resolver_id: int | None = None,
    deny_reason: str | None = None,
) -> discord.Embed:
    """Build the review card for a submission in the given state.

    Reused by the todo board's Approvals detail view and by the resolved
    edit, so the card always mirrors the row's true state. Colour is semantic on resolution
    (green approved, red denied/refunded) and accent while pending.
    """
    if state == "approved":
        embed = discord.Embed(
            title="✅ Sponsored Question Approved", color=discord.Color(COLOR_GREEN)
        )
    elif state in ("denied", "expired"):
        embed = discord.Embed(
            title="❌ Sponsored Question Declined", color=discord.Color(COLOR_RED)
        )
    elif state == "posted":
        embed = discord.Embed(
            title="📮 Sponsored Question Posted", color=discord.Color(COLOR_GREEN)
        )
    else:
        embed = discord.Embed(title="📋 Sponsored Question Submitted", color=accent)

    embed.add_field(name="👤 Sponsor", value=named_or_anonymous(sponsor_id, name_fn), inline=True)
    embed.add_field(name="💰 Paid", value=_reward_text(settings, price), inline=True)
    embed.add_field(name="❓ Question", value=question[:1024], inline=False)
    if state == "approved":
        embed.add_field(
            name="Next",
            value="Queued — it goes out with the next `/qotd post`.",
            inline=False,
        )
    if state == "approved" and resolver_id:
        embed.add_field(name="Approved by", value=name_fn(resolver_id), inline=True)
    if state in ("denied", "expired"):
        if resolver_id:
            embed.add_field(name="Declined by", value=name_fn(resolver_id), inline=True)
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


# ── persistent approval buttons ───────────────────────────────────────────────


class SponsorApproveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_qotd_sub:approve:(?P<sid>\d+)"),
):
    """Persistent Approve button; ``custom_id`` carries the submission id."""

    def __init__(self, submission_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Approve",
                emoji="✅",
                style=discord.ButtonStyle.success,
                custom_id=f"econ_qotd_sub:approve:{submission_id}",
            )
        )
        self.submission_id = submission_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> SponsorApproveButton:
        return cls(int(match["sid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _handle_resolution(
            interaction, self.submission_id, approve=True, deny_reason=None
        )


class _DenyReasonModal(discord.ui.Modal, title="Decline This Question"):
    """Reason is optional but strongly encouraged — the member gets it in a DM."""

    reason: discord.ui.TextInput = discord.ui.TextInput(
        label="Why? (shown to the member)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=400,
        placeholder="Off-topic, already asked, needs rewording…",
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


class SponsorDenyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_qotd_sub:deny:(?P<sid>\d+)"),
):
    """Persistent Decline button; opens the reason modal before resolving."""

    def __init__(self, submission_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Decline",
                emoji="🚫",
                style=discord.ButtonStyle.danger,
                custom_id=f"econ_qotd_sub:deny:{submission_id}",
            )
        )
        self.submission_id = submission_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> SponsorDenyButton:
        return cls(int(match["sid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        # The modal-submit interaction has no `.message`, so hand the card
        # along for the post-resolution edit.
        await interaction.response.send_modal(
            _DenyReasonModal(self.submission_id, interaction.message)
        )


class SponsorReviewView(discord.ui.View):
    """Persistent (timeout=None) Approve/Decline pair for one submission."""

    def __init__(self, submission_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(SponsorApproveButton(submission_id))
        self.add_item(SponsorDenyButton(submission_id))


_safe_ephemeral = partial(_core_safe_ephemeral, log_label="econ sponsor")
_refresh_board = partial(refresh_todo_board, log_label="econ sponsor")


async def _handle_resolution(
    interaction: discord.Interaction,
    submission_id: int,
    *,
    approve: bool,
    deny_reason: str | None,
    card_message: discord.Message | None = None,
) -> None:
    """Gate, resolve, edit the card, DM the sponsor. Never raises to the button."""
    guild = interaction.guild
    member = interaction.user
    bot = cast("Bot", interaction.client)
    ctx = bot.ctx
    # An ephemeral card means the todo board's Approvals flow; see
    # ``view_helpers.review_surface``. Everything downstream repaints the
    # same way either way.
    card = review_surface(
        interaction,
        card_message if card_message is not None else interaction.message,
    )

    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException:
        log.debug("econ sponsor: failed to defer resolution", exc_info=True)

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
        log.exception("econ sponsor: failed to load submission %s", submission_id)
        await _safe_ephemeral(
            interaction, "❌ Couldn't load that submission — try again."
        )
        return
    if loaded is None:
        await _safe_ephemeral(interaction, "❌ That submission no longer exists.")
        return
    settings, row = loaded

    if not can_manage_economy(member, settings):
        await _safe_ephemeral(interaction, MANAGE_DENIED_MSG)
        return

    accent = await safe_resolve_accent(ctx, guild, log_label="sponsor")
    # One resolver for every repaint below: the sponsor, and the mod acting now.
    name_fn = await card_name_fn(ctx, guild, row, member.id)

    def _resolve():
        with ctx.open_db() as conn:
            return resolve_submission(
                conn,
                submission_id,
                approve=approve,
                resolver_id=member.id,
                deny_reason=deny_reason or "",
            )

    try:
        fresh = await asyncio.to_thread(_resolve)
    except ValueError as exc:
        # Already resolved (dashboard, or another mod) — refresh the card to
        # the true state instead of pretending it worked.
        await _refresh_card(card, ctx, accent, settings, submission_id, name_fn=name_fn)
        await _safe_ephemeral(interaction, str(exc))
        return
    except Exception:
        log.exception("econ sponsor: failed to resolve %s", submission_id)
        await _safe_ephemeral(interaction, "❌ Couldn't resolve that — try again.")
        return

    await _edit_card(card, accent, settings, fresh, name_fn=name_fn)
    # If they resolved this from the board's ephemeral, the card sitting in
    # the approvals channel is still offering the decision — close it too.
    await _edit_stored(bot, card, accent, settings, fresh, name_fn=name_fn)
    await _dm_sponsor(bot, ctx, guild, settings, fresh)
    await _refresh_board(bot, guild.id)
    await _safe_ephemeral(
        interaction,
        "Queued for the next `/qotd post`." if approve
        else "Declined and refunded.",
    )


def _card_embed(accent, settings: EconSettings, row, name_fn: NameFn = mention) -> discord.Embed:
    return render_sponsor_card_embed(
        accent,
        settings,
        sponsor_id=int(row["user_id"]),
        name_fn=name_fn,
        question=str(row["question"]),
        price=int(row["price"]),
        state=str(row["state"]),
        resolver_id=int(row["resolver_id"]) if row["resolver_id"] else None,
        deny_reason=str(row["deny_reason"] or ""),
    )


_edit_card = partial(
    edit_review_card, build_embed=_card_embed, log_label="econ sponsor"
)
_edit_stored = partial(
    edit_stored_card, build_embed=_card_embed, log_label="econ sponsor"
)


_refresh_card = partial(
    refresh_review_card,
    read_row=get_submission,
    build_embed=_card_embed,
    log_label="econ sponsor",
)


def sponsor_resolution_dm_text(settings: EconSettings, row) -> str:
    """The member-facing receipt for a resolved submission.

    Shared with the dashboard's queue endpoints so a question resolved from the
    web reads exactly like one resolved from the card buttons.
    """
    unit = settings.currency_plural or "coins"
    if str(row["state"]) == "approved":
        return (
            f"✅ Your sponsored question was approved — it'll go out with the "
            f"next question of the day.\n> {row['question']}"
        )
    reason = str(row["deny_reason"] or "")
    tail = f"\n**Why:** {reason}" if reason else ""
    return (
        f"🚫 Your sponsored question wasn't accepted, and your "
        f"{int(row['price'])} {unit} have been refunded.\n"
        f"> {row['question']}{tail}"
    )


async def _dm_sponsor(
    bot: Bot, ctx: AppContext, guild: discord.Guild, settings: EconSettings, row
) -> None:
    """Tell the sponsor what happened. Best-effort; a closed DM is not an error.

    No ``require_game_role`` gate: this is the receipt for an action the member
    just took and paid for, not a recurring engagement nudge, so it goes to
    them whether or not they opted into the economy role.
    """
    text = sponsor_resolution_dm_text(settings, row)
    try:
        await notify_member(
            bot, ctx.db_path, guild.id, int(row["user_id"]), content=text
        )
    except Exception:
        log.debug("econ sponsor: failed to DM sponsor", exc_info=True)
