"""Discord surface for intake cards (welcome tracker).

Embed + persistent buttons for the per-newcomer card posted to greeter chat
on join, plus the event-hook handlers ``events_cog`` calls. All decisions
(what a message means, which steps tick) live in
:mod:`bot_modules.services.intake_service`; this module is glue that renders
the result and talks to Discord.

Persistent (``timeout=None``) buttons rendered as
:class:`discord.ui.DynamicItem`, registered in ``__main__`` via
``bot.add_dynamic_items(...)``:

* :class:`IntakeStepButton` — one per manual step; toggles the tick.
  ``custom_id`` carries the ``intake_cards`` row id + step key.
* :class:`IntakeDismissButton` — mods close the card without completing.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any, cast

import discord

from bot_modules.core.branding import resolve_accent_color
from bot_modules.economy.leaderboard import progress_bar
from bot_modules.services import intake_service as svc

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger(__name__)

_STEP_CID = re.compile(r"intake:step:(?P<cid>\d+):(?P<key>[\w-]{1,64})")
_DISMISS_CID = re.compile(r"intake:dismiss:(?P<cid>\d+)")

_RESOLVED_HEADLINES = {
    svc.RESOLUTION_COMPLETED: "🎉 Intake complete — welcomed by {by}",
    svc.RESOLUTION_DISMISSED: "🚫 Dismissed by {by}",
    svc.RESOLUTION_LEFT: "👋 They left the server",
    svc.RESOLUTION_BANNED: "🔨 They were banned",
}


# ---------------------------------------------------------------------------
# Embed (pure)
# ---------------------------------------------------------------------------


def format_step_lines(steps: list[Any]) -> list[str]:
    """One checklist line per step: ✅ done / ⏭️ skipped / ⬜ pending.

    Done lines show who ticked (``done_by`` 0 = the bot's auto-tick) and when.
    """
    lines: list[str] = []
    for s in steps:
        label = str(s["label"])
        if s["skipped"]:
            lines.append(f"⏭️ ~~{label}~~ — skipped")
        elif s["done_at"] is not None:
            by = "auto" if not s["done_by"] else f"<@{int(s['done_by'])}>"
            lines.append(f"✅ {label} — {by} <t:{int(s['done_at'])}:R>")
        else:
            lines.append(f"⬜ {label}")
    return lines


def build_intake_embed(
    accent: discord.Color,
    *,
    member_mention: str,
    member_display: str,
    account_created_ts: float | None,
    inviter_mention: str | None,
    steps: list[Any],
    resolved: tuple[str, str] | None = None,
) -> discord.Embed:
    """Card body. ``resolved`` = ``(resolution, resolver_mention)`` once closed."""
    done, total = svc.count_progress(steps)
    embed = discord.Embed(
        title="🚪 New arrival — intake",
        description=f"{member_mention} just joined.\n{progress_bar(done, total)}",
        color=accent,
    )
    embed.add_field(
        name="Member", value=f"{member_mention} (`{member_display}`)", inline=True
    )
    if account_created_ts is not None:
        embed.add_field(
            name="Account created", value=f"<t:{int(account_created_ts)}:R>", inline=True
        )
    embed.add_field(
        name="Invited by", value=inviter_mention or "unknown", inline=True
    )
    checklist = "\n".join(format_step_lines(steps))
    embed.add_field(name="Checklist", value=checklist[:1024] or "—", inline=False)
    if resolved is not None:
        resolution, resolver = resolved
        headline = _RESOLVED_HEADLINES.get(resolution, "Closed")
        embed.add_field(
            name="Resolved", value=headline.format(by=resolver), inline=False
        )
    return embed


# ---------------------------------------------------------------------------
# Persistent buttons
# ---------------------------------------------------------------------------


class IntakeStepButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=_STEP_CID,
):
    """Toggle one manual checklist step (greeters + mods)."""

    def __init__(self, card_id: int, step_key: str, label: str, done: bool) -> None:
        super().__init__(
            discord.ui.Button(
                label=label[:80],
                style=discord.ButtonStyle.success if done else discord.ButtonStyle.secondary,
                custom_id=f"intake:step:{card_id}:{step_key}",
            )
        )
        self.card_id = card_id
        self.step_key = step_key

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ) -> IntakeStepButton:
        return cls(int(match["cid"]), match["key"], item.label or match["key"], False)

    async def callback(self, interaction: discord.Interaction) -> None:
        await _toggle_step(interaction, self.card_id, self.step_key)


class IntakeDismissButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=_DISMISS_CID,
):
    """Close the card without completing (mods only)."""

    def __init__(self, card_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Dismiss",
                emoji="🚫",
                style=discord.ButtonStyle.secondary,
                custom_id=f"intake:dismiss:{card_id}",
            )
        )
        self.card_id = card_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ) -> IntakeDismissButton:
        return cls(int(match["cid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _dismiss_card(interaction, self.card_id)


class IntakeCardView(discord.ui.View):
    """Buttons for one card: a toggle per manual step + Dismiss."""

    def __init__(self, card_id: int, steps: list[Any]) -> None:
        super().__init__(timeout=None)
        manual = [s for s in steps if not str(s["auto_kind"])]
        for s in manual[:24]:  # component budget: 24 steps + Dismiss
            self.add_item(
                IntakeStepButton(
                    card_id,
                    str(s["step_key"]),
                    str(s["label"]),
                    s["done_at"] is not None,
                )
            )
        self.add_item(IntakeDismissButton(card_id))


# ---------------------------------------------------------------------------
# Permission gates
# ---------------------------------------------------------------------------


def _is_mod(ctx: AppContext, member: discord.Member) -> bool:
    perms = member.guild_permissions
    return bool(
        perms.administrator
        or perms.manage_roles
        or ctx.guild_config(member.guild.id).member_is_mod(member)
    )


def _is_greeter(member: discord.Member, greeter_role_id: int) -> bool:
    return greeter_role_id > 0 and any(r.id == greeter_role_id for r in member.roles)


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _card_render_data(
    ctx: AppContext, guild_id: int, card_id: int, user_id: int
):
    """(steps, inviter_id) fetched off-thread for a render."""
    with ctx.open_db() as conn:
        return (
            svc.steps_for(conn, card_id),
            svc.inviter_for(conn, guild_id, user_id),
        )


async def _build_embed_for_card(
    ctx: AppContext,
    guild: discord.Guild,
    card: Any,
    steps: list[Any],
    inviter_id: int | None,
    resolved: tuple[str, str] | None = None,
) -> discord.Embed:
    user_id = int(card["user_id"])
    member = guild.get_member(user_id)
    accent = await resolve_accent_color(ctx.db_path, guild)
    return build_intake_embed(
        accent,
        member_mention=member.mention if member else f"<@{user_id}>",
        member_display=str(member) if member else str(user_id),
        account_created_ts=member.created_at.timestamp() if member else None,
        inviter_mention=f"<@{inviter_id}>" if inviter_id else None,
        steps=steps,
        resolved=resolved,
    )


async def _edit_card_message(
    guild: discord.Guild,
    card: Any,
    embed: discord.Embed,
    view: discord.ui.View | None,
) -> None:
    channel = guild.get_channel(int(card["channel_id"]))
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return
    try:
        await channel.get_partial_message(int(card["message_id"])).edit(
            embed=embed, view=view
        )
    except discord.HTTPException:
        log.debug("intake: failed to edit card message", exc_info=True)


async def _rerender_card(ctx: AppContext, guild: discord.Guild, card: Any) -> None:
    """Refresh an open card's embed + buttons after a tick."""
    card_id = int(card["id"])
    steps, inviter_id = await asyncio.to_thread(
        _card_render_data, ctx, guild.id, card_id, int(card["user_id"])
    )
    embed = await _build_embed_for_card(ctx, guild, card, steps, inviter_id)
    await _edit_card_message(guild, card, embed, IntakeCardView(card_id, steps))


async def _render_resolved(
    ctx: AppContext,
    guild: discord.Guild,
    card: Any,
    resolution: str,
    resolver_mention: str,
) -> None:
    """Flip a card message to its closed state and strip the buttons."""
    steps, inviter_id = await asyncio.to_thread(
        _card_render_data, ctx, guild.id, int(card["id"]), int(card["user_id"])
    )
    embed = await _build_embed_for_card(
        ctx, guild, card, steps, inviter_id, resolved=(resolution, resolver_mention)
    )
    await _edit_card_message(guild, card, embed, None)


# ---------------------------------------------------------------------------
# Posting a card (on_member_join)
# ---------------------------------------------------------------------------


async def intake_enabled(ctx: AppContext, guild_id: int) -> bool:
    def _check() -> bool:
        with ctx.open_db() as conn:
            return svc.is_enabled(conn, guild_id)

    return await asyncio.to_thread(_check)


async def post_intake_card(ctx: AppContext, member: discord.Member) -> bool:
    """Post a card for a fresh join. Returns True when intake owns arrivals.

    A True return tells the caller to skip the legacy bare greeter ping even
    if the send failed (logged) — with intake enabled there is exactly one
    arrival surface. A rejoin while the old card is still open keeps that
    card (create_card dedupes) and re-pings nothing.
    """
    guild = member.guild
    guild_id = guild.id
    now = time.time()

    def _prepare():
        with ctx.open_db() as conn:
            if not svc.is_enabled(conn, guild_id):
                return None
            card_id = svc.create_card(conn, guild_id, member.id, now)
            if card_id is None:  # still-open card from a previous join
                return ("carded",)
            return (
                "post",
                card_id,
                svc.intake_channel_id(conn, guild_id),
                svc.greeter_role_id(conn, guild_id),
                svc.steps_for(conn, card_id),
                svc.inviter_for(conn, guild_id, member.id),
            )

    try:
        prepared = await asyncio.to_thread(_prepare)
    except Exception:
        log.exception("intake: failed to prepare card for %s", member.id)
        return False
    if prepared is None:
        return False
    if prepared[0] == "carded":
        svc.add_watched(guild_id, member.id)
        return True

    _, card_id, channel_id, greeter_role, steps, inviter_id = prepared
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        log.warning("intake: channel %s missing in guild %s", channel_id, guild_id)
        await asyncio.to_thread(_delete_card, ctx, card_id)
        return True

    accent = await resolve_accent_color(ctx.db_path, guild)
    embed = build_intake_embed(
        accent,
        member_mention=member.mention,
        member_display=str(member),
        account_created_ts=member.created_at.timestamp(),
        inviter_mention=f"<@{inviter_id}>" if inviter_id else None,
        steps=steps,
    )
    ping = f"<@&{greeter_role}> — {member.mention} has arrived" if greeter_role > 0 else (
        f"{member.mention} has arrived"
    )
    try:
        posted = await channel.send(
            ping,
            embed=embed,
            view=IntakeCardView(card_id, steps),
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                users=False,
                roles=[discord.Object(id=greeter_role)] if greeter_role > 0 else False,
            ),
        )
    except discord.HTTPException:
        log.warning("intake: failed to post card in guild %s", guild_id)
        await asyncio.to_thread(_delete_card, ctx, card_id)
        return True

    await asyncio.to_thread(_attach_message, ctx, card_id, channel.id, posted.id)
    svc.add_watched(guild_id, member.id)
    return True


def _delete_card(ctx: AppContext, card_id: int) -> None:
    with ctx.open_db() as conn:
        svc.delete_card(conn, card_id)


def _attach_message(ctx: AppContext, card_id: int, channel_id: int, message_id: int) -> None:
    with ctx.open_db() as conn:
        svc.set_card_message(conn, card_id, channel_id, message_id)


# ---------------------------------------------------------------------------
# Event-hook handlers (called from events_cog)
# ---------------------------------------------------------------------------


async def handle_intake_message(ctx: AppContext, message: discord.Message) -> None:
    """Greet + completion-code detection for a message mentioning open cards.

    Caller has already pre-filtered with :func:`intake_service.is_watched`.
    """
    guild = message.guild
    author = message.author
    if guild is None or not isinstance(author, discord.Member):
        return
    guild_id = guild.id
    mentioned = [u.id for u in message.mentions]
    author_role_ids = {r.id for r in author.roles}
    author_is_mod = _is_mod(ctx, author)
    content = message.content
    channel_id = message.channel.id
    now = time.time()

    def _work():
        with ctx.open_db() as conn:
            greeter_role = svc.greeter_role_id(conn, guild_id)
            actions = svc.evaluate_message(
                conn,
                guild_id,
                channel_id=channel_id,
                content=content,
                mentioned_ids=mentioned,
                author_is_greeter=greeter_role in author_role_ids,
                author_is_mod=author_is_mod,
            )
            results = []
            for action, uid in actions:
                if action == svc.ACTION_COMPLETE:
                    completed = svc.complete_card(conn, guild_id, uid, author.id, now)
                    if completed is not None:
                        results.append((svc.ACTION_COMPLETE, completed[0], uid))
                else:
                    card, ticked = svc.auto_tick(
                        conn, guild_id, uid, svc.AUTO_GREETED, now, actor_id=author.id
                    )
                    if card is not None and ticked:
                        results.append((svc.ACTION_GREET, card, uid))
            return results

    try:
        results = await asyncio.to_thread(_work)
    except Exception:
        log.exception("intake: message handling failed in guild %s", guild_id)
        return

    for action, card, uid in results:
        if action == svc.ACTION_COMPLETE:
            svc.discard(guild_id, uid)
            await _render_resolved(
                ctx, guild, card, svc.RESOLUTION_COMPLETED, author.mention
            )
            try:
                await message.add_reaction("🎉")
            except discord.HTTPException:
                log.debug("intake: completion reaction failed", exc_info=True)
        else:
            await _rerender_card(ctx, guild, card)


async def handle_role_changes(
    ctx: AppContext,
    member: discord.Member,
    gained_role_ids: list[int],
    unverified_removed: bool,
) -> None:
    """Auto-tick verified / role_gained steps after a role update."""
    guild_id = member.guild.id
    now = time.time()

    def _work():
        with ctx.open_db() as conn:
            if not svc.is_enabled(conn, guild_id):
                return None
            card = None
            ticked: list[str] = []
            for rid in gained_role_ids:
                card, keys = svc.auto_tick(
                    conn, guild_id, member.id, svc.AUTO_ROLE_GAINED, now, role_id=rid
                )
                ticked.extend(keys)
            if unverified_removed:
                card, keys = svc.auto_tick(
                    conn, guild_id, member.id, svc.AUTO_VERIFIED, now
                )
                ticked.extend(keys)
            return (card, ticked)

    try:
        result = await asyncio.to_thread(_work)
    except Exception:
        log.exception("intake: role-change handling failed for %s", member.id)
        return
    if result is None:
        return
    card, ticked = result
    if card is not None and ticked:
        await _rerender_card(ctx, member.guild, card)


async def close_member_card(
    ctx: AppContext, guild: discord.Guild, user_id: int, resolution: str
) -> None:
    """Close a member's open card on leave/ban and flip its message."""
    now = time.time()

    def _work():
        with ctx.open_db() as conn:
            return svc.close_for_member(conn, guild.id, user_id, resolution, 0, now)

    try:
        card = await asyncio.to_thread(_work)
    except Exception:
        log.exception("intake: close failed for %s in guild %s", user_id, guild.id)
        return
    svc.discard(guild.id, user_id)
    if card is not None and int(card["message_id"]) > 0:
        await _render_resolved(ctx, guild, card, resolution, "")


# ---------------------------------------------------------------------------
# Button callbacks
# ---------------------------------------------------------------------------


async def _safe_ephemeral(interaction: discord.Interaction, text: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        log.debug("intake: failed to send ephemeral", exc_info=True)


async def _toggle_step(
    interaction: discord.Interaction, card_id: int, step_key: str
) -> None:
    """Tick/untick a manual step and refresh the card in one response."""
    guild = interaction.guild
    actor = interaction.user
    bot = cast("Bot", interaction.client)
    ctx = bot.ctx
    if guild is None or not isinstance(actor, discord.Member):
        await _safe_ephemeral(interaction, "❌ This only works in a server.")
        return
    actor_is_mod = _is_mod(ctx, actor)
    actor_role_ids = {r.id for r in actor.roles}
    now = time.time()

    def _work():
        with ctx.open_db() as conn:
            card = svc.get_card(conn, card_id)
            if card is None or card["resolved_at"] is not None:
                return ("closed",)
            greeter_role = svc.greeter_role_id(conn, guild.id)
            if greeter_role not in actor_role_ids and not actor_is_mod:
                return ("denied",)
            step = next(
                (s for s in svc.steps_for(conn, card_id) if s["step_key"] == step_key),
                None,
            )
            if step is None or str(step["auto_kind"]):
                return ("closed",)  # unknown or auto step — buttons never carry these
            svc.set_step_state(
                conn,
                card_id,
                step_key,
                done=step["done_at"] is None,
                actor_id=actor.id,
                at=now,
            )
            return (
                "ok",
                card,
                svc.steps_for(conn, card_id),
                svc.inviter_for(conn, guild.id, int(card["user_id"])),
            )

    try:
        result = await asyncio.to_thread(_work)
    except Exception:
        log.exception("intake: step toggle failed for card %s", card_id)
        await _safe_ephemeral(interaction, "❌ Something went wrong — try again.")
        return

    if result[0] == "closed":
        await _safe_ephemeral(interaction, "This card is no longer active.")
        return
    if result[0] == "denied":
        await _safe_ephemeral(interaction, "❌ Only greeters and mods can tick steps.")
        return

    _, card, steps, inviter_id = result
    embed = await _build_embed_for_card(ctx, guild, card, steps, inviter_id)
    view = IntakeCardView(card_id, steps)
    try:
        await interaction.response.edit_message(embed=embed, view=view)
    except discord.HTTPException:
        log.debug("intake: failed to edit card on toggle", exc_info=True)


async def _dismiss_card(interaction: discord.Interaction, card_id: int) -> None:
    guild = interaction.guild
    actor = interaction.user
    bot = cast("Bot", interaction.client)
    ctx = bot.ctx
    if guild is None or not isinstance(actor, discord.Member):
        await _safe_ephemeral(interaction, "❌ This only works in a server.")
        return
    if not _is_mod(ctx, actor):
        await _safe_ephemeral(interaction, "❌ Only mods can dismiss an intake card.")
        return
    now = time.time()

    def _work():
        with ctx.open_db() as conn:
            card = svc.get_card(conn, card_id)
            if card is None or card["resolved_at"] is not None:
                return None
            svc.resolve_card(conn, card_id, actor.id, now, svc.RESOLUTION_DISMISSED)
            return (
                card,
                svc.steps_for(conn, card_id),
                svc.inviter_for(conn, guild.id, int(card["user_id"])),
            )

    try:
        result = await asyncio.to_thread(_work)
    except Exception:
        log.exception("intake: dismiss failed for card %s", card_id)
        await _safe_ephemeral(interaction, "❌ Something went wrong — try again.")
        return
    if result is None:
        await _safe_ephemeral(interaction, "Already closed.")
        return

    card, steps, inviter_id = result
    svc.discard(guild.id, int(card["user_id"]))
    embed = await _build_embed_for_card(
        ctx, guild, card, steps, inviter_id,
        resolved=(svc.RESOLUTION_DISMISSED, actor.mention),
    )
    try:
        await interaction.response.edit_message(embed=embed, view=None)
    except discord.HTTPException:
        log.debug("intake: failed to edit card on dismiss", exc_info=True)
