"""Shared helpers for the economy card/view modules (bounty, pin, auction, …).

These are trivial but were copy-pasted into every view module; centralizing
keeps the currency vocabulary and the "reply without blowing up" behavior in one
place so a change lands everywhere at once.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING

import discord

from bot_modules.core.utils import safe_ephemeral as _core_safe_ephemeral
from bot_modules.services.name_resolver import NameFn, mention

if TYPE_CHECKING:
    from bot_modules.services.economy_service import EconSettings

log = logging.getLogger("dungeonkeeper.economy")


def unit(settings: EconSettings, amount: int) -> str:
    """Currency name matching ``amount``'s grammatical number.

    Note the deliberate difference from ``coins`` below: this returns the
    configured plural verbatim, including an empty one. Callers that render
    the bare unit (the wallet header, quest rewards) have always shown
    whatever the guild configured; ``coins`` substitutes a literal fallback.
    Don't "unify" the two without deciding which behaviour a guild with an
    empty ``currency_plural`` should get.
    """
    return settings.currency_name if abs(amount) == 1 else settings.currency_plural


def coins(settings: EconSettings, amount: int) -> str:
    """``🪙 **250** coins`` — the currency vocabulary every economy card uses."""
    unit = (
        settings.currency_name
        if abs(amount) == 1
        else (settings.currency_plural or "coins")
    )
    return f"{settings.currency_emoji} **{amount:,}** {unit}"


safe_ephemeral = partial(_core_safe_ephemeral, log_label="econ view")


class EphemeralCard:
    """Stands in for a review card when the review happened on the todo board.

    The board's Approve/Decline buttons live under an *ephemeral* detail
    message. That message is real and arrives at a resolver exactly like a
    channel card would, but an ephemeral message cannot be edited through the
    channel-message endpoint — only through the interaction that owns it.
    Wrapping the interaction in the one method every card path calls keeps the
    resolution code identical either way, instead of sprouting a
    "which surface am I on?" branch at each of its half-dozen repaints.
    """

    __slots__ = ("_interaction",)

    def __init__(self, interaction: discord.Interaction) -> None:
        self._interaction = interaction

    async def edit(self, **kwargs) -> None:
        await self._interaction.edit_original_response(**kwargs)


def review_surface(
    interaction: discord.Interaction, card: "discord.Message | None"
) -> "discord.Message | EphemeralCard | None":
    """The thing to repaint: the channel card, or the board's ephemeral detail.

    ``card`` is whatever the resolver was handed — ``interaction.message`` for
    a button, or the message the deny modal was opened from. An ephemeral one
    means the board flow; see :class:`EphemeralCard`.
    """
    if card is not None and getattr(card.flags, "ephemeral", False):
        return EphemeralCard(interaction)
    return card


async def refresh_todo_board(
    bot: "discord.Client", guild_id: int, *, log_label: str
) -> None:
    """Repaint the mods' todo board, where everything waiting on them is listed.

    Called on both edges — a request filed and a request resolved — because
    the board's own 60s loop only repaints guilds where a recurring chore
    spawned, so without this a paid request would sit invisible until
    something else happened to move the board. Best effort in the strongest
    sense: by the time this runs the money has already moved and the member
    has already been told, so a Discord hiccup must never surface as a failed
    submission or a failed refund.
    """
    # ``bot`` is annotated as the bare Client because an expiry sweep holds
    # one; only a commands.Bot carries cogs, which the runtime one always is.
    get_cog = getattr(bot, "get_cog", None)
    cog = get_cog("TodoCog") if get_cog is not None else None
    refresh = getattr(cog, "refresh_board", None)
    if refresh is None:
        return
    try:
        await refresh(guild_id)
    except Exception:  # pragma: no cover - defensive
        log.warning(
            "%s: failed to repaint the todo board for %s.", log_label, guild_id
        )


async def card_name_fn(
    ctx, guild, row, *extra_ids: int, guild_id: int | None = None
) -> NameFn:
    """A resolver covering everyone a paid-request card names.

    That is the requester (``row['user_id']``) plus whichever mod resolved it,
    prefetched in one batch. Embed mentions are resolved by the *reading*
    client from its own cache, so a card that emits ``<@id>`` shows a bare
    number to any mod who has never seen that member — and a paid request is
    often the first time they meet them. A member who has since left is still
    named, from ``known_users``.

    Cheap to call: ``build_name_fn`` queries only for ids the live member
    cache misses, so the usual case costs no I/O at all.

    ``guild`` may be None — the dashboard resolves a submission for a guild the
    bot may not currently see — in which case pass ``guild_id`` so the
    ``known_users`` lookup still has a guild to scope to. With neither, every
    id falls through to ``<@id>``, which is the old behaviour and no worse.
    """
    from bot_modules.services.name_resolver import build_name_fn  # noqa: PLC0415

    ids = [int(row["user_id"] or 0)] if row is not None else []
    resolver_id = None
    try:
        resolver_id = row["resolver_id"] if row is not None else None
    except (IndexError, KeyError, TypeError):
        resolver_id = None
    if resolver_id:
        ids.append(int(resolver_id))
    ids.extend(int(i) for i in extra_ids if i)
    gid = int(guild.id) if guild is not None else int(guild_id or 0)
    return await build_name_fn(
        guild=guild, db_path=ctx.db_path, guild_id=gid, user_ids=ids
    )


async def edit_review_card(
    card: "discord.Message | EphemeralCard | None",
    accent,
    settings: "EconSettings",
    row,
    *,
    build_embed,
    log_label: str,
    name_fn: NameFn = mention,
) -> None:
    """Re-render a paid-submission review card in place, best effort.

    ``build_embed`` stays a caller's argument on purpose. Pins and sponsored
    questions render different embeds from different columns, and that copy
    belongs with the product it speaks for — only the edit-and-swallow
    mechanics are shared. Losing the card is not worth raising over: the row
    is already resolved and the member has already been told.

    ``name_fn`` is handed to ``build_embed`` so the requester and the
    resolving mod render as names rather than ``<@id>``, which an embed
    leaves as a bare number for any reader whose client hasn't cached them.
    The default preserves the pre-resolver rendering for a caller that has no
    resolver to give.
    """
    if card is None:
        return
    try:
        await card.edit(embed=build_embed(accent, settings, row, name_fn), view=None)
    except discord.HTTPException:
        log.debug("%s: failed to edit card", log_label, exc_info=True)


async def edit_stored_card(
    bot: "discord.Client",
    card: "discord.Message | EphemeralCard | None",
    accent,
    settings: "EconSettings",
    row,
    *,
    build_embed,
    log_label: str,
    name_fn: NameFn = mention,
) -> None:
    """Repaint the request's card in the approvals channel, wherever it was resolved.

    The other half of "two surfaces, one ledger". A mod can resolve a paid
    request from the card itself, from the todo board's ephemeral Approvals
    detail, or from the dashboard — and the two they were *not* looking at
    must stop offering a decision that has already been made. The board is
    read live off the submission tables so it self-corrects; the channel card
    is a message, and only ``card_channel_id``/``card_message_id`` can find it
    again.

    Skipped when the caller already repainted that very message: ``card`` is
    the surface they were on, so a resolution from the channel card itself
    must not fetch and edit it a second time. An ephemeral surface never
    matches, which is exactly the case this exists for.

    Best effort throughout — the row is resolved and the member already told,
    so a deleted card or a lost permission is cosmetic, never an error.
    """
    # Deferred: the service imports the economy machinery, this module is
    # imported by it.
    from bot_modules.services.economy_approvals_service import (  # noqa: PLC0415
        card_location,
    )

    channel_id, message_id = card_location(row)
    if not channel_id or not message_id:
        return
    # Compared by id rather than by type: an EphemeralCard has no ``id`` and so
    # can never match, which is exactly the case this function exists for, and
    # a real card matches without depending on it being a ``discord.Message``
    # instance.
    if int(getattr(card, "id", 0) or 0) == message_id:
        return
    try:
        channel = bot.get_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return
        message = await channel.fetch_message(message_id)
        await message.edit(embed=build_embed(accent, settings, row, name_fn), view=None)
    except (discord.HTTPException, discord.NotFound, discord.Forbidden):
        log.debug("%s: failed to edit the stored card", log_label, exc_info=True)
    except Exception:  # pragma: no cover - defensive
        log.warning("%s: unexpected error editing the stored card", log_label)


async def refresh_review_card(
    card: "discord.Message | EphemeralCard | None",
    ctx,
    accent,
    settings: "EconSettings",
    submission_id: int,
    *,
    read_row,
    build_embed,
    log_label: str,
    name_fn: NameFn = mention,
) -> None:
    """Reload a row that moved underneath its card and re-render it.

    The row can change without the card knowing: a mod resolves it from the
    dashboard, or two mods press buttons at once. Re-reading before the edit
    is what stops a card claiming "approved" after the row went the other
    way. A failed read leaves the card as it is rather than blanking it —
    stale beats wrong-and-confident.
    """
    if card is None:
        return

    def _read():
        with ctx.open_db() as conn:
            return read_row(conn, submission_id)

    try:
        row = await asyncio.to_thread(_read)
    except Exception:
        log.debug("%s: failed to reload for refresh", log_label, exc_info=True)
        return
    if row is not None:
        await edit_review_card(
            card, accent, settings, row,
            build_embed=build_embed, log_label=log_label, name_fn=name_fn,
        )
