"""The todo board's Approvals button — one door onto three paid queues.

A themed day, a sponsored question and a pin are three products a member pays
for and a moderator then approves. Each used to post its own Approve/Decline
card into the economy's ``bank_channel_id``, which in the main guild is
"how-it-works" — a member-facing explainer. That published an unreviewed
request, naming the member and quoting what they wrote, to the whole server
before anyone had looked at it. Pin of the Day was the worst of the three: it
has no dashboard queue, so that public card was the *only* place it could be
reviewed at all.

They live on the mods' todo board now, exactly as the quest sign-offs do
(``quest_views.open_signoff_picker``). The board is one sticky message and
Discord caps the components on a message, so Approve/Decline cannot hang off
it once per request. The Approvals button opens an ephemeral pick-one select
across all three queues; picking a request edits that ephemeral into the
product's own review card — the same embed builder the bank-channel card used,
so nothing a moderator reads has changed — with the product's own buttons
underneath.

Nothing here re-implements a product's review. The registry below maps a queue
key onto the three things the picker needs from each product (read the row,
build its embed, build its buttons) and stops there; approving a pin still
posts and pins, approving a theme still only queues, and both still live with
their product.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING, Any, Callable, cast

import discord

from bot_modules.core.branding import DEFAULT_ACCENT_COLOR, safe_resolve_accent
from bot_modules.core.utils import safe_ephemeral as _core_safe_ephemeral
from bot_modules.economy import pin_views, sponsor_views, theme_views
from bot_modules.economy.quest_views import can_manage_economy
from bot_modules.economy.view_helpers import refresh_todo_board
from bot_modules.services.economy_approvals_service import (
    QUEUES_BY_KEY,
    get_approval_row,
    pending_approvals,
)
from bot_modules.services.economy_service import EconSettings, load_econ_settings
from bot_modules.todo.board_logic import approval_label

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.economy")

MANAGE_DENIED_MSG = "❌ You don't have permission to review paid requests."

#: Seconds the ephemeral picker stays usable. Matches the sign-off picker.
_PICKER_TIMEOUT = 180

_safe_ephemeral = partial(_core_safe_ephemeral, log_label="econ approvals")


def _renderers() -> dict[str, tuple[Callable[..., discord.Embed], Callable[[int], discord.ui.View]]]:
    """Queue key → (build this row's embed, build its buttons).

    A registry rather than a method on ``ApprovalQueue``: the descriptor lives
    in the service layer, which must not import Discord views, and what a
    request *looks* like is the product's business either way. Everything here
    is a thin adapter onto a builder that already existed for the card.
    """

    def theme_embed(accent, settings, row) -> discord.Embed:
        return theme_views.render_theme_review_embed(
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

    def sponsor_embed(accent, settings, row) -> discord.Embed:
        return sponsor_views.render_sponsor_card_embed(
            accent,
            settings,
            sponsor_mention=f"<@{int(row['user_id'])}>",
            question=str(row["question"]),
            price=int(row["price"]),
            state=str(row["state"]),
            resolver_id=int(row["resolver_id"]) if row["resolver_id"] else None,
            deny_reason=str(row["deny_reason"] or ""),
        )

    def pin_embed(accent, settings, row) -> discord.Embed:
        return pin_views.render_pin_review_embed(
            accent,
            settings,
            sponsor_mention=f"<@{int(row['user_id'])}>",
            message=str(row["message"]),
            price=int(row["price"]),
            state=str(row["state"]),
            resolver_id=int(row["resolver_id"]) if row["resolver_id"] else None,
            deny_reason=str(row["deny_reason"] or ""),
        )

    return {
        "theme": (theme_embed, theme_views.ThemeReviewView),
        "sponsor": (sponsor_embed, sponsor_views.SponsorReviewView),
        "pin": (pin_embed, pin_views.PinReviewView),
    }


def option_text(row: dict[str, Any], settings: EconSettings) -> tuple[str, str]:
    """``(label, description)`` for one request's row in the select.

    The label names the member and the queue, because one select covers three
    products and "Alex" alone doesn't say what they bought. The description
    carries what they actually wrote and what it cost — the two things a mod
    weighs before opening it. Both are clipped to Discord's 100.
    """
    who = str(row.get("requester_name") or "").strip() or "someone"
    label = f"{who} — {approval_label(str(row['kind']))}"
    if len(label) > 100:
        label = label[:99] + "…"
    price = int(row.get("price") or 0)
    unit = settings.currency_name if abs(price) == 1 else (settings.currency_plural or "coins")
    summary = " ".join(str(row.get("summary") or "").split())
    desc = f"{price:,} {unit} · {summary}" if summary else f"{price:,} {unit}"
    if len(desc) > 100:
        desc = desc[:99] + "…"
    return label, desc


class ApprovalPickSelect(discord.ui.Select):
    """Ephemeral picker listing every paid request waiting on a moderator."""

    def __init__(self, rows: list[dict[str, Any]], settings: EconSettings) -> None:
        options = []
        for row in rows[:25]:  # Discord caps a select at 25 options
            label, desc = option_text(row, settings)
            # The value carries the queue as well as the id: the ids are
            # per-table, so theme #3 and pin #3 are different requests and an
            # id alone would open whichever product happened to be asked.
            options.append(
                discord.SelectOption(
                    label=label,
                    value=f"{row['kind']}:{row['id']}",
                    description=desc or None,
                )
            )
        super().__init__(
            placeholder="Pick a request to review…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        kind, _, raw_id = str(self.values[0]).partition(":")
        bot = cast("Bot", interaction.client)
        guild = interaction.guild
        if guild is None:
            await _safe_ephemeral(interaction, "❌ This only works in a server.")
            return
        renderers = _renderers()
        if kind not in QUEUES_BY_KEY or kind not in renderers:
            await _safe_ephemeral(interaction, "❌ That request no longer exists.")
            return
        submission_id = int(raw_id)

        def _load():
            with bot.ctx.open_db() as conn:
                row = get_approval_row(conn, kind, submission_id)
                if row is None:
                    return None
                return load_econ_settings(conn, guild.id), row

        try:
            loaded = await asyncio.to_thread(_load)
        except Exception:
            log.exception("econ approvals: failed to load %s %s", kind, submission_id)
            await _safe_ephemeral(
                interaction, "❌ Couldn't load that request — try again."
            )
            return
        if loaded is None:
            await _safe_ephemeral(interaction, "❌ That request no longer exists.")
            return
        settings, row = loaded

        accent = await safe_resolve_accent(
            bot.ctx, guild, log_label="economy", default=DEFAULT_ACCENT_COLOR
        )
        build_embed, review_view = renderers[kind]
        # The product's own builder, in the row's true state — so a request
        # somebody resolved while this picker was open renders as resolved,
        # with no buttons, rather than offering a decision already made.
        view = review_view(submission_id) if str(row["state"]) == "pending" else None
        try:
            await interaction.response.edit_message(
                embed=build_embed(accent, settings, row), view=view
            )
        except discord.HTTPException:
            log.debug("econ approvals: failed to open request detail", exc_info=True)


async def open_approvals_picker(interaction: discord.Interaction) -> None:
    """The todo board's Approvals button: gate, then offer what's waiting.

    Gated on ``can_manage_economy`` rather than the board's own moderator
    check — every decision here moves real currency (a denial refunds it) — so
    it stays the economy's gate even though the button sits on the todo board.
    That is the same call the sign-off picker made, and in the main guild the
    two roles are the same one.
    """
    guild = interaction.guild
    member = interaction.user
    bot = cast("Bot", interaction.client)
    if guild is None or not isinstance(member, discord.Member):
        await _safe_ephemeral(interaction, "❌ This only works in a server.")
        return

    def _load() -> tuple[EconSettings, list[dict[str, Any]]]:
        with bot.ctx.open_db() as conn:
            settings = load_econ_settings(conn, guild.id)
            rows = pending_approvals(conn, guild.id, limit=25)
        return settings, rows

    try:
        settings, rows = await asyncio.to_thread(_load)
    except Exception:
        log.exception("econ approvals: failed to load the pending requests")
        await _safe_ephemeral(
            interaction, "❌ Couldn't load the requests — try again."
        )
        return

    if not can_manage_economy(member, settings):
        await _safe_ephemeral(interaction, MANAGE_DENIED_MSG)
        return
    if not rows:
        await _safe_ephemeral(interaction, "No paid requests are waiting. ✨")
        return

    for row in rows:
        requester = guild.get_member(int(row["user_id"]))
        row["requester_name"] = requester.display_name if requester else "someone"

    view = discord.ui.View(timeout=_PICKER_TIMEOUT)
    view.add_item(ApprovalPickSelect(rows, settings))
    await interaction.response.send_message(
        "Which request do you want to review?", view=view, ephemeral=True
    )


refresh_approvals_board = partial(refresh_todo_board, log_label="econ approvals")
