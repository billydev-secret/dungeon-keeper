"""Discord surface for promotion-review cards.

Persistent (``timeout=None``) buttons rendered as
:class:`discord.ui.DynamicItem` so a card posted before a restart stays
actionable after it. Three button classes, all registered in ``__main__`` via
``bot.add_dynamic_items(...)``:

* :class:`GrantAccessButton` / :class:`DismissButton` — on the pruned-return and
  sleeper cards; ``custom_id`` carries the ``promotion_review_cards`` row id.
* :class:`Level5GrantButton` — retrofitted onto the existing Level 5 card;
  ``custom_id`` carries the target member id (that card is not ledger-backed).

Gating logic and the ledger live in
:mod:`bot_modules.services.promotion_review_service`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, cast

import discord

from bot_modules.core.branding import resolve_accent_color
from bot_modules.inactive.apply import reactivate_member
from bot_modules.services import promotion_review_service as svc

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger(__name__)

_GRANT_CID = re.compile(r"promo_review:grant:(?P<cid>\d+)")
_DISMISS_CID = re.compile(r"promo_review:dismiss:(?P<cid>\d+)")
_LEVEL5_CID = re.compile(r"promo_review:l5grant:(?P<uid>\d+)")


# ---------------------------------------------------------------------------
# Embed (pure)
# ---------------------------------------------------------------------------


def format_prune_lines(
    guild: discord.Guild, pruned_roles: list[tuple[int, float | None]]
) -> list[str]:
    """Human lines for what a sweep took, e.g. ``@NSFW — removed <t:…:D>``."""
    lines: list[str] = []
    for role_id, pruned_at in pruned_roles:
        role = guild.get_role(role_id)
        label = role.mention if role is not None else f"role `{role_id}`"
        when = f"removed <t:{int(pruned_at)}:D>" if pruned_at else "removed (date unknown)"
        lines.append(f"{label} — {when}")
    return lines


def build_review_embed(
    accent: discord.Color,
    *,
    kind: str,
    member_mention: str,
    member_display: str,
    level: int,
    prune_lines: list[str],
    action_hint: str,
    resolved: tuple[str, str] | None = None,
) -> discord.Embed:
    """Card body for a pruned-return / sleeper card.

    ``resolved`` = ``(resolution, resolver_mention)`` once actioned.
    """
    if kind == svc.KIND_SLEEPER:
        title = "😴 Sleeper is stirring"
        desc = f"{member_mention} is inactive-held and just posted in the sleeper channel."
    else:
        title = "🔙 Member returned"
        desc = f"{member_mention} lost access to a sweep and is active again."
    embed = discord.Embed(
        title=title, description=f"{desc}\n{action_hint}", color=accent
    )
    embed.add_field(name="Member", value=f"{member_mention} (`{member_display}`)", inline=True)
    embed.add_field(name="Level", value=str(level), inline=True)
    if prune_lines:
        embed.add_field(
            name="Access a sweep removed", value="\n".join(prune_lines), inline=False
        )
    if resolved is not None:
        resolution, resolver = resolved
        verb = {
            svc.RESOLUTION_GRANTED: "✅ Access granted",
            svc.RESOLUTION_REACTIVATED: "✅ Reactivated",
            svc.RESOLUTION_DISMISSED: "🚫 Dismissed",
        }.get(resolution, "Resolved")
        embed.add_field(name="Resolved", value=f"{verb} by {resolver}", inline=False)
    return embed


# ---------------------------------------------------------------------------
# Persistent buttons — ledger-backed (pruned-return + sleeper)
# ---------------------------------------------------------------------------


class GrantAccessButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=_GRANT_CID,
):
    """Pruned-return: re-add the configured role. Sleeper: full reactivate."""

    def __init__(self, card_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Grant access",
                emoji="✅",
                style=discord.ButtonStyle.success,
                custom_id=f"promo_review:grant:{card_id}",
            )
        )
        self.card_id = card_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ) -> GrantAccessButton:
        return cls(int(match["cid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _resolve_card(interaction, self.card_id, grant=True)


class DismissButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=_DISMISS_CID,
):
    """Close the card without granting — e.g. the return isn't approved."""

    def __init__(self, card_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Dismiss",
                emoji="🚫",
                style=discord.ButtonStyle.secondary,
                custom_id=f"promo_review:dismiss:{card_id}",
            )
        )
        self.card_id = card_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ) -> DismissButton:
        return cls(int(match["cid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _resolve_card(interaction, self.card_id, grant=False)


class ReviewCardView(discord.ui.View):
    """Persistent Grant/Dismiss pair for one ledger-backed card."""

    def __init__(self, card_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(GrantAccessButton(card_id))
        self.add_item(DismissButton(card_id))


# ---------------------------------------------------------------------------
# Persistent button — Level 5 card (not ledger-backed; keyed by member id)
# ---------------------------------------------------------------------------


class Level5GrantButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=_LEVEL5_CID,
):
    """Grant the configured access role to the Level 5 promotion candidate."""

    def __init__(self, user_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Grant access",
                emoji="✅",
                style=discord.ButtonStyle.success,
                custom_id=f"promo_review:l5grant:{user_id}",
            )
        )
        self.user_id = user_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ) -> Level5GrantButton:
        return cls(int(match["uid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _grant_role_only(interaction, self.user_id)


class Level5PromotionView(discord.ui.View):
    """Persistent single Grant button for a Level 5 promotion card."""

    def __init__(self, user_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(Level5GrantButton(user_id))


# ---------------------------------------------------------------------------
# Posting a card (called from the message hot-path hook)
# ---------------------------------------------------------------------------


async def _safe_ephemeral(interaction: discord.Interaction, text: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        log.debug("promo review: failed to send ephemeral", exc_info=True)


async def post_review_card(
    ctx: AppContext, member: discord.Member, posted_channel_id: int
) -> None:
    """Post a pruned-return / sleeper card for ``member`` if one is owed.

    Runs the authoritative DB gate off-thread, reserves the single open-card
    slot (racing callers lose and skip), posts to the promotion-reviews channel,
    then attaches the message location. Manages the in-memory watch set: drops
    the member once carded or no longer a candidate; keeps a sleeper who posted
    outside the sleeper channel for a later message.
    """
    guild = member.guild
    guild_id = guild.id
    user_id = member.id

    def _prepare():
        with ctx.open_db() as conn:
            kind = svc.evaluate_trigger(conn, guild_id, user_id, posted_channel_id)
            if kind is None:
                return ("skip", svc.still_candidate(conn, guild_id, user_id))
            card_id = svc.reserve_card(conn, guild_id, user_id, kind, time.time())
            if card_id is None:  # lost the race — already carded
                return ("skip", False)
            return (
                "post",
                kind,
                card_id,
                svc.review_channel_id(conn, guild_id),
                svc.pruned_roles_for(conn, guild_id, user_id),
                svc.member_level(conn, guild_id, user_id),
            )

    try:
        prepared = await asyncio.to_thread(_prepare)
    except Exception:
        log.exception("promo review: failed to prepare card for %s", user_id)
        return

    if prepared[0] == "skip":
        still = prepared[1]
        if not still:
            svc.discard(guild_id, user_id)
        return

    _, kind, card_id, review_ch_id, pruned_roles, level = prepared

    channel = guild.get_channel(review_ch_id)
    if not isinstance(channel, discord.abc.Messageable):
        await asyncio.to_thread(_delete_card, ctx, card_id)
        log.warning(
            "promo review: review channel %s missing in guild %s", review_ch_id, guild_id
        )
        return

    accent = await resolve_accent_color(ctx.db_path, guild)
    hint = (
        "Grant access re-adds the role and closes this out."
        if kind == svc.KIND_PRUNED_RETURN
        else "Grant access reactivates them (restores their roles)."
    )
    embed = build_review_embed(
        accent,
        kind=kind,
        member_mention=member.mention,
        member_display=str(member),
        level=level,
        prune_lines=format_prune_lines(guild, pruned_roles),
        action_hint=hint,
    )
    try:
        posted = await channel.send(
            embed=embed,
            view=ReviewCardView(card_id),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        log.warning("promo review: failed to post card in guild %s", guild_id)
        await asyncio.to_thread(_delete_card, ctx, card_id)
        return  # keep on watch set — retry on their next message

    await asyncio.to_thread(_attach_message, ctx, card_id, channel.id, posted.id)
    svc.discard(guild_id, user_id)


def _delete_card(ctx: AppContext, card_id: int) -> None:
    with ctx.open_db() as conn:
        svc.delete_card(conn, card_id)


def _attach_message(ctx: AppContext, card_id: int, channel_id: int, message_id: int) -> None:
    with ctx.open_db() as conn:
        svc.set_card_message(conn, card_id, channel_id, message_id)


# ---------------------------------------------------------------------------
# Resolving a card (button callbacks)
# ---------------------------------------------------------------------------


def _can_action(ctx: AppContext, member: discord.Member) -> bool:
    perms = member.guild_permissions
    return bool(
        perms.administrator
        or perms.manage_roles
        or ctx.guild_config(member.guild.id).member_is_mod(member)
    )


async def _resolve_card(interaction: discord.Interaction, card_id: int, *, grant: bool) -> None:
    """Gate, apply the action (grant/reactivate), close the card, edit. Never raises."""
    guild = interaction.guild
    actor = interaction.user
    bot = cast("Bot", interaction.client)
    ctx = bot.ctx

    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException:
        log.debug("promo review: failed to defer", exc_info=True)

    if guild is None or not isinstance(actor, discord.Member):
        await _safe_ephemeral(interaction, "❌ This only works in a server.")
        return
    if not _can_action(ctx, actor):
        await _safe_ephemeral(interaction, "❌ You can't action returns here.")
        return

    card = await asyncio.to_thread(lambda: _get_card(ctx, card_id))
    if card is None:
        await _safe_ephemeral(interaction, "❌ That card no longer exists.")
        return
    if card["resolved_at"] is not None:
        await _safe_ephemeral(interaction, "Already resolved.")
        return

    guild_id = guild.id
    member_id = int(card["user_id"])
    kind = str(card["kind"])
    target = guild.get_member(member_id)

    resolution = svc.RESOLUTION_DISMISSED
    if grant:
        if target is None:
            await _safe_ephemeral(interaction, "❌ That member isn't in the server anymore.")
            return
        if kind == svc.KIND_SLEEPER:
            ok, msg = await _do_reactivate(ctx, guild, target, actor)
            if not ok:
                await _safe_ephemeral(interaction, msg)
                return
            resolution = svc.RESOLUTION_REACTIVATED
        else:
            ok, msg = await _do_grant_role(ctx, guild, target, actor)
            if not ok:
                await _safe_ephemeral(interaction, msg)
                return
            resolution = svc.RESOLUTION_GRANTED

    now = time.time()

    def _commit():
        with ctx.open_db() as conn:
            closed = svc.resolve_card(conn, card_id, actor.id, now, resolution)
            if closed and resolution == svc.RESOLUTION_GRANTED:
                svc.mark_prunes_restored(conn, guild_id, member_id, now)
            return closed

    closed = await asyncio.to_thread(_commit)
    svc.discard(guild_id, member_id)
    if not closed:  # someone else resolved it between load and commit
        await _safe_ephemeral(interaction, "Already resolved.")
        return

    await _edit_resolved(interaction, ctx, guild, card, resolution, actor.mention)
    await _safe_ephemeral(interaction, _RESULT_MSG.get(resolution, "Done."))


_RESULT_MSG = {
    svc.RESOLUTION_GRANTED: "Access granted.",
    svc.RESOLUTION_REACTIVATED: "Reactivated.",
    svc.RESOLUTION_DISMISSED: "Dismissed.",
}


async def _grant_role_only(interaction: discord.Interaction, user_id: int) -> None:
    """Level 5 card Grant button: add the configured role, no ledger row."""
    guild = interaction.guild
    actor = interaction.user
    bot = cast("Bot", interaction.client)
    ctx = bot.ctx

    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException:
        log.debug("promo review: failed to defer l5 grant", exc_info=True)

    if guild is None or not isinstance(actor, discord.Member):
        await _safe_ephemeral(interaction, "❌ This only works in a server.")
        return
    if not _can_action(ctx, actor):
        await _safe_ephemeral(interaction, "❌ You can't action promotions here.")
        return
    target = guild.get_member(user_id)
    if target is None:
        await _safe_ephemeral(interaction, "❌ That member isn't in the server anymore.")
        return
    ok, msg = await _do_grant_role(ctx, guild, target, actor)
    await _safe_ephemeral(interaction, "Access granted." if ok else msg)


async def _do_grant_role(
    ctx: AppContext, guild: discord.Guild, target: discord.Member, actor: discord.Member
) -> tuple[bool, str]:
    role_id = await asyncio.to_thread(lambda: _grant_role_id(ctx, guild.id))
    role = guild.get_role(role_id) if role_id > 0 else None
    if role is None:
        return False, "❌ No grant role is configured — set one on the dashboard."
    try:
        await target.add_roles(role, reason=f"Promotion review: granted by {actor}")
    except discord.Forbidden:
        return False, "❌ I can't assign that role (check my role position)."
    except discord.HTTPException:
        return False, "❌ Discord hiccup assigning the role — try again."
    return True, "Access granted."


async def _do_reactivate(
    ctx: AppContext, guild: discord.Guild, target: discord.Member, actor: discord.Member
) -> tuple[bool, str]:
    try:
        result = await reactivate_member(
            ctx, guild, target, reason="Promotion review", actor=actor
        )
    except discord.HTTPException:
        return False, "❌ Discord hiccup reactivating — try again."
    # reactivate_member returns a status string; a leading ❌ marks failure.
    if result.startswith("❌"):
        return False, result
    return True, result


def _get_card(ctx: AppContext, card_id: int):
    with ctx.open_db() as conn:
        return svc.get_card(conn, card_id)


def _grant_role_id(ctx: AppContext, guild_id: int) -> int:
    with ctx.open_db() as conn:
        return svc.grant_role_id(conn, guild_id)


async def _edit_resolved(
    interaction: discord.Interaction,
    ctx: AppContext,
    guild: discord.Guild,
    card,
    resolution: str,
    resolver_mention: str,
) -> None:
    message = interaction.message
    if message is None:
        return
    member_id = int(card["user_id"])
    kind = str(card["kind"])
    target = guild.get_member(member_id)
    mention = target.mention if target is not None else f"<@{member_id}>"
    display = str(target) if target is not None else str(member_id)

    def _fetch():
        with ctx.open_db() as conn:
            return (
                svc.pruned_roles_for(conn, guild.id, member_id),
                svc.member_level(conn, guild.id, member_id),
            )

    pruned_roles, level = await asyncio.to_thread(_fetch)
    accent = await resolve_accent_color(ctx.db_path, guild)
    embed = build_review_embed(
        accent,
        kind=kind,
        member_mention=mention,
        member_display=display,
        level=level,
        prune_lines=format_prune_lines(guild, pruned_roles),
        action_hint="",
        resolved=(resolution, resolver_mention),
    )
    try:
        await message.edit(embed=embed, view=None)
    except discord.HTTPException:
        log.debug("promo review: failed to edit resolved card", exc_info=True)
