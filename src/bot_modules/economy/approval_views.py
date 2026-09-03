"""Paid-request review: the approvals-channel card and the board's button.

A themed day, a sponsored question and a pin are three products a member pays
for and a moderator then approves. Each used to post its own Approve/Decline
card into the economy's ``bank_channel_id``, which in the main guild is
"how-it-works" — a member-facing explainer. That published an unreviewed
request, naming the member and quoting what they wrote, to the whole server
before anyone had looked at it. Pin of the Day was the worst of the three: it
has no dashboard queue, so that public card was the *only* place it could be
reviewed at all.

There are now two review surfaces, and both are always live:

* **A card in ``approvals_channel_id``** (:func:`post_approval_card`) — a
  *dedicated, staff-only* dial, not the bank channel, and dark until it is
  set. This is where mods actually work.
* **The mods' todo board**, exactly as the quest sign-offs do
  (``quest_views.open_signoff_picker``). The board is one sticky message and
  Discord caps the components on a message, so Approve/Decline cannot hang off
  it once per request: the Approvals button opens an ephemeral pick-one select
  across all three queues, and picking a request edits that ephemeral into the
  product's own review card. The backstop, and the only surface when the
  channel dial is unset.

**Two surfaces, one ledger.** ``card_channel_id``/``card_message_id`` on each
submissions table are what stop them disagreeing: a resolution anywhere closes
both — the board because its section reads the queue live, the card because
``view_helpers.edit_stored_card`` repaints it from those ids. The hourly
expiry sweep closes cards through :func:`close_expired_card` for the same
reason.

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
from bot_modules.core.utils import role_ping_kwargs
from bot_modules.services.name_resolver import NameFn, mention
from bot_modules.core.utils import safe_ephemeral as _core_safe_ephemeral
from bot_modules.economy import pin_views, sponsor_views, theme_views
from bot_modules.economy.quest_views import can_manage_economy
from bot_modules.economy.view_helpers import card_name_fn, refresh_todo_board
from bot_modules.services.economy_approvals_service import (
    QUEUES_BY_KEY,
    card_location,
    get_approval_row,
    pending_approvals,
    set_approval_card,
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


def _renderers(
    name_fn: NameFn = mention,
) -> dict[str, tuple[Callable[..., discord.Embed], Callable[[int], discord.ui.View]]]:
    """Queue key → (build this row's embed, build its buttons).

    A registry rather than a method on ``ApprovalQueue``: the descriptor lives
    in the service layer, which must not import Discord views, and what a
    request *looks* like is the product's business either way. Everything here
    is a thin adapter onto a builder that already existed for the card.

    ``name_fn`` resolves the requester (and the resolving mod) to a display
    name. It is threaded in rather than looked up inside because resolution
    needs an async prefetch and these adapters are called synchronously; the
    default keeps a caller that has none rendering exactly as before. A card
    in a *channel* must always pass a real one — see
    :func:`post_approval_card`.
    """

    def theme_embed(accent, settings, row) -> discord.Embed:
        return theme_views.render_theme_review_embed(
            accent,
            settings,
            sponsor_id=int(row["user_id"]),
            name_fn=name_fn,
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
            sponsor_id=int(row["user_id"]),
            name_fn=name_fn,
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
            sponsor_id=int(row["user_id"]),
            name_fn=name_fn,
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


#: Queue keys this build can draw a card for. Read off the registry so the two
#: can't drift, and checked before any work: a select value naming a product
#: this build doesn't know is a shrug, not a crash.
RENDERABLE_KINDS = frozenset(_renderers())


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
        if kind not in QUEUES_BY_KEY or kind not in RENDERABLE_KINDS:
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
        build_embed, review_view = _renderers(
            await card_name_fn(bot.ctx, guild, row)
        )[kind]
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


# ---------------------------------------------------------------------------
# Posting a card into the review channel
# ---------------------------------------------------------------------------


async def post_approval_card(
    bot: Bot,
    guild: discord.Guild,
    settings: EconSettings,
    kind: str,
    submission_id: int,
) -> None:
    """Post one paid request's review card to the approvals channel, best effort.

    The channel surface the board replaced, done properly: a **dedicated,
    staff-only** ``approvals_channel_id`` rather than the member-facing bank
    channel these cards used to land in. Unset ⇒ this returns immediately and
    the board stays the only surface, which is how the dial ships dark.

    One poster for all three products. The embed and the buttons come from
    the same ``_renderers()`` registry the board's picker uses, so a mod reads
    an identical card whichever surface they are on, and neither surface owns
    a copy of a product's review.

    The member has already paid and the pending row already exists by the time
    this runs, so **nothing here may raise back at them** — a missing channel,
    a forbidden send or a Discord hiccup leaves the request on the board and
    is logged, not surfaced. The card's location is recorded last: the ledger
    is what lets a resolution on one surface repaint the other, and a card
    with no recorded location simply behaves like one posted before the dial
    was set.
    """
    channel_id = int(getattr(settings, "approvals_channel_id", 0) or 0)
    if not channel_id:
        return
    if kind not in RENDERABLE_KINDS:
        return
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        log.warning(
            "econ approvals: review channel %s missing in guild %s",
            channel_id, guild.id,
        )
        return

    def _read():
        with bot.ctx.open_db() as conn:
            return get_approval_row(conn, kind, submission_id)

    try:
        row = await asyncio.to_thread(_read)
    except Exception:
        log.exception("econ approvals: failed to read %s %s", kind, submission_id)
        return
    if row is None:
        return

    accent = await safe_resolve_accent(
        bot.ctx, guild, log_label="economy", default=DEFAULT_ACCENT_COLOR
    )
    build_embed, review_view = _renderers(
        await card_name_fn(bot.ctx, guild, row)
    )[kind]
    # The people who can actually press Approve — ``can_manage_economy`` is
    # the gate on both surfaces — rather than a second role to keep in sync.
    ping = role_ping_kwargs([settings.manager_role_id])
    try:
        posted = await channel.send(
            embed=build_embed(accent, settings, row),
            view=review_view(submission_id),
            **ping,
        )
    except discord.HTTPException:
        log.warning(
            "econ approvals: failed to post the %s card for %s", kind, submission_id
        )
        return
    except Exception:
        log.exception("econ approvals: unexpected error posting %s %s", kind, submission_id)
        return

    def _record() -> None:
        with bot.ctx.open_db() as conn:
            set_approval_card(conn, kind, submission_id, channel.id, posted.id)

    try:
        await asyncio.to_thread(_record)
    except Exception:
        log.debug("econ approvals: failed to record card ids", exc_info=True)


async def close_expired_card(
    bot: Bot,
    guild: discord.Guild,
    settings: EconSettings,
    kind: str,
    submission_id: int,
) -> None:
    """Repaint an expired request's card so it stops offering a decision.

    The hourly sweep refunds requests nobody reviewed in time, and until now
    nothing told their card. That was invisible while the cards lived only on
    the board — the board reads the queue live, so an expired row simply
    vanishes from it — but a *channel* card is a message: left alone it sits
    there indefinitely showing Approve and Decline for a request that has
    already been refunded. The buttons are safe (the ``state = from_state``
    guard rejects them) but they read as broken, and a mod pressing one has
    reasonably concluded the queue is lying to them.

    Best effort, and deliberately silent about a card that was never posted:
    every request submitted while the channel dial was unset has no location
    recorded, which is not an error.
    """
    if kind not in RENDERABLE_KINDS or not submission_id:
        return

    def _read():
        with bot.ctx.open_db() as conn:
            return get_approval_row(conn, kind, submission_id)

    try:
        row = await asyncio.to_thread(_read)
    except Exception:
        log.debug("econ approvals: failed to read expired %s", submission_id, exc_info=True)
        return
    if row is None:
        return
    channel_id, message_id = card_location(row)
    if not channel_id or not message_id:
        return

    accent = await safe_resolve_accent(
        bot.ctx, guild, log_label="economy", default=DEFAULT_ACCENT_COLOR
    )
    build_embed, _ = _renderers(await card_name_fn(bot.ctx, guild, row))[kind]
    try:
        channel = bot.get_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return
        message = await channel.fetch_message(message_id)
        await message.edit(embed=build_embed(accent, settings, row), view=None)
    except (discord.HTTPException, discord.NotFound, discord.Forbidden):
        log.debug("econ approvals: failed to close expired card", exc_info=True)
    except Exception:  # pragma: no cover - defensive
        log.warning("econ approvals: unexpected error closing expired card")
