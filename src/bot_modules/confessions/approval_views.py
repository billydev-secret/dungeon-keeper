"""The todo board's Confessions button — reviewing what mod-approve mode held.

With ``confession_config.require_approval`` on, a submission waits in
``confession_pending`` instead of posting. This is where a moderator works that
queue: a button on the mods' sticky todo board opens an ephemeral pick-one
select, and picking a confession edits that ephemeral into a review card with
Approve and Reject under it. Exactly the shape the paid requests took when they
moved off the bank channel (``economy.approval_views``), and for the same
structural reason — the board is one sticky message and Discord caps the
components on a message, so Approve/Reject cannot hang off the board once per
confession.

What is different here is what a moderator is *not* shown. The other two board
queues name the member, because a paid request and a quest claim are things a
member did openly. A confession is not, and the board's gate is the moderator
tier — a wider circle than the admin-only Confessions Audit Log, which is
admin-gated precisely because it puts a real name to an anonymous post. So no
author id reaches this module at all on the listing path
(``pending_confessions`` does not select one), and the single call that does
return one, ``resolve_pending_confession``, hands it straight to the poster or
the DM and renders nothing. De-anonymising a confession stays one thing, done
in one place, by admins.

The rejection DM likewise names no moderator. The mod team answers as a team,
and pointing a member at the individual who turned down their anonymous
confession would break the anonymity in the other direction.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING, Any, cast

import discord

from bot_modules.confessions.logic import (
    REJECTION_REASON_MAX,
    build_expiry_dm_text,
    build_rejection_dm_text,
    pending_option_text,
)
from bot_modules.core.branding import DEFAULT_ACCENT_COLOR, safe_resolve_accent
from bot_modules.core.utils import safe_ephemeral as _core_safe_ephemeral
# Generic despite its address: ``refresh_todo_board`` knows nothing about the
# economy, it just repaints TodoCog's board. It lives there because the paid
# queues needed it first.
from bot_modules.economy.view_helpers import refresh_todo_board
from bot_modules.services.confessions_service import (
    enqueue_confession,
    get_config,
    get_pending_confession,
    now_ts,
    pending_confessions,
    resolve_pending_confession,
)
from bot_modules.services.dm_branding import send_branded_dm

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger(__name__)

#: Seconds the ephemeral picker stays usable. Matches the other two pickers.
_PICKER_TIMEOUT = 180

MOD_DENIED_MSG = "❌ Only moderators can review confessions."
GONE_MSG = "❌ That confession has already been handled."
PANIC_MSG = (
    "❌ Confessions are paused, so nothing can be posted right now. "
    "It stays in the queue — approve it once panic mode is off."
)
BLOCKED_MSG = (
    "❌ Whoever sent this is on the confessions block list now, so it "
    "wasn't posted. It stays in the queue — reject it if it should go."
)
EMPTY_MSG = "No confessions are waiting for approval. ✨"

refresh_confessions_board = partial(refresh_todo_board, log_label="confessions")

_safe_ephemeral = partial(_core_safe_ephemeral, log_label="confession approvals")


def _is_mod(interaction: discord.Interaction) -> bool:
    """The board's own moderator gate.

    Not the economy's ``can_manage_economy`` the paid queues use — no currency
    moves here, so there is nothing to justify a narrower gate than the board
    every other button on it already uses. ``AppContext.is_mod`` is the same
    rule the dashboard resolves its ``moderator`` tier from, so a mod refused
    in one place is refused in both.
    """
    bot = cast("Bot", interaction.client)
    member = interaction.user
    return isinstance(member, discord.Member) and bot.ctx.is_mod(interaction)


def build_review_embed(content: str, created_at: int, accent) -> discord.Embed:
    """The card a moderator decides on: the confession, and how long it waited.

    No author field, and no name anywhere — see the module docstring. The
    footer says so out loud rather than leaving a mod to wonder whether the
    card failed to load one.
    """
    embed = discord.Embed(
        title="🕵️ Confession Awaiting Approval",
        description=content[:4000],
        color=accent,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Submitted", value=f"<t:{int(created_at)}:R>", inline=True)
    embed.set_footer(
        text="Anonymous — the author is shown only on the admin Confessions Audit Log."
    )
    return embed


def build_resolved_embed(text: str, accent) -> discord.Embed:
    return discord.Embed(title="🕵️ Confession", description=text, color=accent)


class RejectModal(discord.ui.Modal, title="Reject this confession"):
    """Reject, optionally saying why.

    The reason is quoted back to the member in their DM, so it is short by
    construction and required to be nothing: the common case is a confession
    that simply doesn't belong in the channel, and making a mod type a
    justification for that would just produce worse reasons.
    """

    reason = discord.ui.TextInput(
        label="Reason (optional — the member sees this)",
        style=discord.TextStyle.short,
        required=False,
        max_length=REJECTION_REASON_MAX,
        placeholder="Leave blank to say nothing",
    )

    def __init__(self, pending_id: int) -> None:
        super().__init__()
        self.pending_id = pending_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await _resolve(interaction, self.pending_id, approve=False, reason=str(self.reason.value or ""))


class ConfessionReviewView(discord.ui.View):
    """Approve / Reject for one queued confession.

    A short-lived view on an ephemeral message, not a persistent one: the card
    only ever exists inside a picker session, and the queue row it points at is
    gone the moment either button lands.
    """

    def __init__(self, pending_id: int) -> None:
        super().__init__(timeout=_PICKER_TIMEOUT)
        self.pending_id = pending_id

    @discord.ui.button(label="Approve", emoji="✅", style=discord.ButtonStyle.success)
    async def _approve(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await _resolve(interaction, self.pending_id, approve=True)

    @discord.ui.button(label="Reject", emoji="🚫", style=discord.ButtonStyle.danger)
    async def _reject(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        if not _is_mod(interaction):
            await _safe_ephemeral(interaction, MOD_DENIED_MSG)
            return
        await interaction.response.send_modal(RejectModal(self.pending_id))


async def _resolve(
    interaction: discord.Interaction,
    pending_id: int,
    *,
    approve: bool,
    reason: str = "",
) -> None:
    """Claim the row, then act on it. Both buttons land here.

    The gate is re-checked at click time rather than trusted from whoever
    opened the picker — an ephemeral card can outlive the roles of the person
    holding it.

    ``resolve_pending_confession`` deletes and returns in one immediate
    transaction, so the claim is exactly-once: two mods pressing Approve
    together cannot post the same confession twice, and the loser is told it
    has already been handled rather than shown a stale card. Everything after
    the claim is best-effort, because the row is already gone — this is the
    same "the decision has landed, the side effects must not undo it" shape the
    paid queues use.
    """
    bot = cast("Bot", interaction.client)
    guild = interaction.guild
    if guild is None:
        await _safe_ephemeral(interaction, "❌ This only works in a server.")
        return
    if not _is_mod(interaction):
        await _safe_ephemeral(interaction, MOD_DENIED_MSG)
        return

    try:
        await interaction.response.defer()
    except discord.HTTPException:
        return

    cfg = await asyncio.to_thread(get_config, bot.ctx.db_path, guild.id)
    accent = await safe_resolve_accent(
        bot.ctx, guild, log_label="confessions", default=DEFAULT_ACCENT_COLOR
    )

    # Panic is checked *before* the claim, so a refusal leaves the row exactly
    # where it was. It has to be checked at all because approving is the one
    # confession path that posts on a delay: an admin who hits the kill switch
    # during a raid means "nothing else appears in that channel", and a queue
    # that kept publishing under it would be a documented gate with a hole in
    # it. Rejecting stays available — clearing a backlog is not posting.
    if approve and cfg is not None and cfg.panic:
        await _repaint(interaction, build_resolved_embed(PANIC_MSG, accent))
        return

    row = await asyncio.to_thread(
        resolve_pending_confession, bot.ctx.db_path, guild.id, pending_id
    )
    if row is None:
        await _repaint(interaction, build_resolved_embed(GONE_MSG, accent))
        return

    if approve:
        # Blocked *after* submitting — usually because a mod blocked them over
        # this very confession. Needs the claim first, since the block list is
        # keyed on the author id and nothing before the claim has one. Put it
        # back rather than post it or bin it: rejecting is the mod's call.
        if cfg is not None and int(row["author_id"]) in cfg.blocked_set():
            await _requeue(bot, guild.id, row)
            await _repaint(interaction, build_resolved_embed(BLOCKED_MSG, accent))
            await refresh_confessions_board(bot, guild.id)
            return

        cog = bot.get_cog("ConfessionsCog")
        publish = getattr(cog, "publish_confession", None)
        ok, err = False, "❌ Confessions aren't available right now."
        if publish is not None and cfg is not None:
            try:
                ok, err = await publish(
                    guild,
                    cfg,
                    content=str(row["content"]),
                    author_id=int(row["author_id"]),
                    notify=bool(int(row["notify_original_author"]) == 1),
                )
            except Exception:
                # Not just the (ok, err) contract: the row is already deleted,
                # so anything that escapes here — a locked DB inside the audit
                # write, a Discord object misbehaving — would destroy a member's
                # text with nobody told. A raise is a failed post like any other.
                log.exception("confessions: publishing %s raised", pending_id)
                ok, err = False, "❌ Something went wrong posting that."
        if not ok:
            # The row is already claimed and cannot be un-deleted, so the
            # confession would otherwise vanish silently. Put it back rather
            # than lose a member's text to a missing permission.
            await _requeue(bot, guild.id, row)
            await _repaint(
                interaction,
                build_resolved_embed(
                    f"{err}\n\nPut back in the queue — nothing was lost.", accent
                ),
            )
            await refresh_confessions_board(bot, guild.id)
            return
        await _repaint(
            interaction,
            build_resolved_embed("✅ Approved and posted.", accent),
        )
    else:
        told = await _notify_rejected(bot, guild, int(row["author_id"]), reason)
        # Say which actually happened. A member with DMs closed is told nothing
        # at all, and a moderator assured otherwise has no reason to follow up.
        await _repaint(
            interaction,
            build_resolved_embed(
                "🚫 Rejected. The member has been told."
                if told
                else "🚫 Rejected. Couldn't DM them — their DMs are closed, so "
                     "they haven't been told.",
                accent,
            ),
        )

    await refresh_confessions_board(bot, guild.id)


async def _requeue(bot: "Bot", guild_id: int, row: dict[str, Any]) -> None:
    """Return a claimed confession to the queue after a failed post.

    Keeping ``created_at`` is the point: the seven-day sweep is a promise to the
    member, and a row that restamped itself every time a mod retried a failing
    post would outlive it for as long as the failure lasted.
    """
    try:
        await asyncio.to_thread(
            enqueue_confession,
            bot.ctx.db_path,
            guild_id=guild_id,
            author_id=int(row["author_id"]),
            content=str(row["content"]),
            notify_original_author=int(row["notify_original_author"]),
            created_at=int(row["created_at"]),
        )
    except Exception:  # pragma: no cover - defensive
        log.exception("confessions: failed to requeue %s after a failed post", row["id"])


async def _dm_author(
    bot: "Bot", guild: discord.Guild, author_id: int, text: str
) -> bool:
    """Tell the member their confession wasn't posted. Returns whether it landed.

    Never raises: the decision has already been written and must not be undone
    by a closed DM. But the caller is told, because "the member has been told"
    printed over a DM that bounced is a moderator given a false account of what
    their own click did. ``send_branded_dm`` swallows the failure itself and
    signals with ``None``. ``AllowedMentions.none()`` because the body can quote
    a moderator's free text.
    """
    user = guild.get_member(author_id)
    if user is None:
        try:
            user = await bot.fetch_user(author_id)
        except discord.HTTPException:
            return False
    if user is None:
        return False
    try:
        sent = await send_branded_dm(
            user,
            db_path=bot.ctx.db_path,
            guild=guild,
            embed=discord.Embed(description=text),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception:
        # ``send_branded_dm`` already swallows a closed DM and returns None, so
        # reaching here means something further in (accent resolution, say)
        # broke. Caught all the same: by the time this runs the queue row is
        # deleted, and letting it escape would lose the confession *and* leave
        # the moderator staring at "This interaction failed".
        log.exception("confessions: outcome DM failed")
        return False
    return sent is not None


async def _notify_rejected(
    bot: "Bot", guild: discord.Guild, author_id: int, reason: str
) -> bool:
    return await _dm_author(
        bot, guild, author_id,
        build_rejection_dm_text(guild_name=guild.name, reason=reason),
    )


async def notify_confession_expired(
    bot: "Bot", guild: discord.Guild, author_id: int
) -> None:
    """Tell a member their confession aged out of an unworked queue.

    Worded as its own outcome rather than a rejection: nobody judged it, and
    saying "the mods didn't approve it" when in truth nobody looked would be a
    verdict the mod team never reached.
    """
    await _dm_author(bot, guild, author_id, build_expiry_dm_text(guild_name=guild.name))


async def _repaint(interaction: discord.Interaction, embed: discord.Embed) -> None:
    """Rewrite the ephemeral card in place, buttons gone.

    Through ``edit_original_response``, not the message: the card is ephemeral
    and cannot be edited through the channel-message endpoint — the same
    constraint ``economy.view_helpers.EphemeralCard`` exists for. Here there is
    only ever the one surface, so the interaction is used directly.
    """
    try:
        await interaction.edit_original_response(embed=embed, view=None)
    except discord.HTTPException:
        log.debug("confessions: failed to repaint the review card", exc_info=True)


class ConfessionPickSelect(discord.ui.Select):
    """Ephemeral picker listing every confession waiting on a moderator."""

    def __init__(self, rows: list[dict[str, Any]], *, now: int) -> None:
        options = []
        for row in rows[:25]:  # Discord caps a select at 25 options
            label, desc = pending_option_text(row, now=now)
            options.append(
                discord.SelectOption(
                    label=label, value=str(row["id"]), description=desc or None
                )
            )
        super().__init__(
            placeholder="Pick a confession to review…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = cast("Bot", interaction.client)
        guild = interaction.guild
        if guild is None:
            await _safe_ephemeral(interaction, "❌ This only works in a server.")
            return
        if not _is_mod(interaction):
            await _safe_ephemeral(interaction, MOD_DENIED_MSG)
            return
        pending_id = int(self.values[0])

        def _load():
            with bot.ctx.open_db() as conn:
                return get_pending_confession(conn, guild.id, pending_id)

        try:
            row = await asyncio.to_thread(_load)
        except Exception:
            log.exception("confessions: failed to load pending %s", pending_id)
            await _safe_ephemeral(
                interaction, "❌ Couldn't load that confession — try again."
            )
            return

        accent = await safe_resolve_accent(
            bot.ctx, guild, log_label="confessions", default=DEFAULT_ACCENT_COLOR
        )
        # Resolved while the picker sat open: show that, with no buttons,
        # rather than offering a decision somebody already made.
        if row is None:
            embed, view = build_resolved_embed(GONE_MSG, accent), None
        else:
            embed = build_review_embed(
                str(row["content"]), int(row["created_at"]), accent
            )
            view = ConfessionReviewView(pending_id)
        try:
            await interaction.response.edit_message(embed=embed, view=view)
        except discord.HTTPException:
            log.debug("confessions: failed to open the review card", exc_info=True)


async def open_confessions_picker(interaction: discord.Interaction) -> None:
    """The todo board's Confessions button: gate, then offer what's waiting."""
    bot = cast("Bot", interaction.client)
    guild = interaction.guild
    if guild is None:
        await _safe_ephemeral(interaction, "❌ This only works in a server.")
        return
    if not _is_mod(interaction):
        await _safe_ephemeral(interaction, MOD_DENIED_MSG)
        return

    def _load() -> list[dict[str, Any]]:
        with bot.ctx.open_db() as conn:
            return pending_confessions(conn, guild.id, limit=25)

    try:
        rows = await asyncio.to_thread(_load)
    except Exception:
        log.exception("confessions: failed to load the approval queue")
        await _safe_ephemeral(
            interaction, "❌ Couldn't load the queue — try again."
        )
        return

    if not rows:
        await _safe_ephemeral(interaction, EMPTY_MSG)
        return

    view = discord.ui.View(timeout=_PICKER_TIMEOUT)
    view.add_item(ConfessionPickSelect(rows, now=now_ts()))
    await interaction.response.send_message(
        "Which confession do you want to review?", view=view, ephemeral=True
    )
