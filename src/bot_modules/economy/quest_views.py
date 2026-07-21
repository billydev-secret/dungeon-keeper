"""Discord views for economy quests — the ``/bank quests`` claim UI and the
manager sign-off cards.

Two surfaces live here:

* ``QuestClaimView`` — an ephemeral select attached to ``/bank quests``. Its
  callback claims the picked quest at click time (the ``period`` key is
  computed then, so a claim that straddles a day/week roll lands in the right
  bucket) and either confirms an instant payout or posts a sign-off card.

* The sign-off card — a **persistent** Approve/Deny pair. The buttons are
  ``discord.ui.DynamicItem`` subclasses whose ``custom_id`` embeds the
  ``claim_id`` (``econ_claim:approve:<id>`` / ``econ_claim:deny:<id>``), so a
  click still routes after a bot restart once the cog re-registers the classes
  with ``bot.add_dynamic_items``. Approve resolves straight away; Deny opens a
  reason modal first. Every handler is fail-safe: a service error surfaces as
  an ephemeral note, never a dead button.

The claim-credit booster flag is always the *claimant's* boost status at the
moment of the credit — for instant claims that is the clicker, for sign-off it
is read from the claim row's ``user_id`` when a manager approves (not the
manager's own status).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, cast

import discord

from bot_modules.core.branding import resolve_accent_color
from bot_modules.core.db_utils import get_tz_offset_hours
from bot_modules.economy.logic import is_economy_manager, local_day_for
from bot_modules.economy.leaderboard import progress_bar
from bot_modules.economy.quests import quest_period
from bot_modules.services.economy_quests_service import (
    claim_quest,
    deny_history,
    get_quest,
    reroll_board_slot,
    resolve_claim,
    set_claim_card,
)
from bot_modules.services.economy_service import (
    EconSettings,
    load_econ_settings,
    member_is_booster,
    notify_member,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.economy")

MANAGE_DENIED_MSG = "You don't have permission to review quest claims."
_CLAIM_VIEW_TIMEOUT = 180  # seconds; the ephemeral claim select is short-lived.

# Member-facing explainer per quest state / trigger kind — shown in the
# per-quest detail view (the /bank quests list itself stays one line per
# quest; this is where the long form lives).
# Glyphs for the three live states stay in lock-step with the one-line
# board (_quest_line_status in economy_cog): 🔶 = your move / claimable,
# ⏳ = awaiting sign-off, ✅ = done. Sharing the vocabulary keeps ✅ from
# meaning "claimable" here and "done" on the board.
QUEST_STATE_LABEL = {
    "claimable": "🔶 Ready to claim",
    "pending": "⏳ Awaiting sign-off",
    "done": "✅ Completed this period",
    "trigger": "🗣️ Completes automatically when you say its trigger phrase",
    "photo_post": "📸 Completes automatically when you post a photo in the Photo Challenge channel",
    "party_game": "🎲 Completes automatically when you finish a party game",
    "duel": "⚔️ Completes automatically when you finish a 1v1 duel",
    "risky_roll": "🎰 Completes automatically when you take a Risky Roll dare",
    "guess": "🕵️ Completes automatically when you play a Guess Who round",
    "voice_session": "🎙️ Completes automatically when you're active in voice chat",
    "qotd_reply": "📣 Completes automatically when you answer the Question of the Day",
    "starboard": "⭐ Completes automatically when a message of yours hits the starboard",
    "invite": "📨 Completes automatically when someone you invited joins",
    "boost": "🚀 Completes automatically when you boost the server",
    "bio_set": "📇 Completes automatically when you set up your bio",
    "birthday_set": "🎂 Completes automatically when you set your birthday",
    "media_post": "🖼️ Completes automatically when you post an image",
    "pen_pal": "💌 Completes automatically when you're matched with a Pen Pal",
    "message_sent": "💬 Completes automatically as you chat",
    "reply_sent": "↩️ Completes automatically when you reply to people",
    "reaction_given": "👍 Completes automatically when you react to people's messages",
    "game_win": "🏆 Completes automatically when you win a party game",
    "duel_win": "🥇 Completes automatically when you win a duel",
}
_DENY_REASON_MAX = 300


def can_manage_economy(member: discord.Member, settings: EconSettings) -> bool:
    """True for server admins or holders of the configured manager role.

    Canonical home for the economy-manager gate — the cog imports this so the
    ``/bank`` grant check and the sign-off buttons share one rule (defined here
    rather than in the cog to avoid a views→cog import cycle).
    """
    return is_economy_manager(
        is_admin=member.guild_permissions.administrator,
        role_ids=[r.id for r in member.roles],
        manager_role_id=settings.manager_role_id,
    )


def _unit(settings: EconSettings, amount: int) -> str:
    return settings.currency_name if abs(amount) == 1 else settings.currency_plural


def _reward_text(settings: EconSettings, reward: int) -> str:
    return f"{settings.currency_emoji} **{reward:,}** {_unit(settings, reward)}"


# ── sign-off card embed ───────────────────────────────────────────────────────


def render_signoff_card_embed(
    accent: discord.Color,
    settings: EconSettings,
    *,
    claimant_mention: str,
    quest_title: str,
    reward: int,
    criteria: str,
    deny_count: int,
    state: str,
    resolver_id: int | None = None,
    deny_reason: str | None = None,
) -> discord.Embed:
    """Build the bank-channel sign-off card for a claim in the given state.

    Reused for the initial ``pending`` post and for the resolved/refresh edit,
    so the card always mirrors the claim's true state. Color is semantic on
    resolution (green approved, red denied) and accent while pending.
    """
    if state == "paid":
        embed = discord.Embed(title="Quest approved", color=discord.Color.green())
    elif state in ("denied", "expired"):
        embed = discord.Embed(title="Quest denied", color=discord.Color.red())
    else:
        embed = discord.Embed(title="Quest sign-off requested", color=accent)

    embed.add_field(name="👤 Member", value=claimant_mention, inline=True)
    embed.add_field(name="🎯 Quest", value=quest_title, inline=True)
    embed.add_field(name="💰 Reward", value=_reward_text(settings, reward), inline=True)
    if criteria:
        embed.add_field(name="📋 Criteria", value=criteria, inline=False)
    if deny_count > 0:
        embed.add_field(
            name="Prior denials/expirations", value=str(deny_count), inline=True
        )
    if state == "paid" and resolver_id:
        embed.add_field(name="Approved by", value=f"<@{resolver_id}>", inline=True)
    if state in ("denied", "expired") and resolver_id:
        embed.add_field(name="Denied by", value=f"<@{resolver_id}>", inline=True)
        if deny_reason:
            embed.add_field(name="Reason", value=deny_reason, inline=False)
    if state in ("paid", "denied", "expired"):
        embed.timestamp = discord.utils.utcnow()
    return embed


# ── persistent sign-off buttons ───────────────────────────────────────────────


class QuestApproveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_claim:approve:(?P<cid>\d+)"),
):
    """Persistent Approve button; ``custom_id`` carries the claim id."""

    def __init__(self, claim_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Approve",
                emoji="✅",
                style=discord.ButtonStyle.success,
                custom_id=f"econ_claim:approve:{claim_id}",
            )
        )
        self.claim_id = claim_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> QuestApproveButton:
        return cls(int(match["cid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _handle_resolution(
            interaction, self.claim_id, approve=True, deny_reason=None
        )


class QuestDenyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"econ_claim:deny:(?P<cid>\d+)"),
):
    """Persistent Deny button; opens a reason modal, then resolves."""

    def __init__(self, claim_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Deny",
                emoji="✖️",
                style=discord.ButtonStyle.danger,
                custom_id=f"econ_claim:deny:{claim_id}",
            )
        )
        self.claim_id = claim_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> QuestDenyButton:
        return cls(int(match["cid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        # Gate before opening the modal so a non-manager never even sees it.
        settings = await _load_settings(interaction, interaction.guild)
        member = interaction.user
        if settings is None or not isinstance(member, discord.Member):
            await _safe_ephemeral(interaction, "This only works in a server.")
            return
        if not can_manage_economy(member, settings):
            await _safe_ephemeral(interaction, MANAGE_DENIED_MSG)
            return
        await interaction.response.send_modal(
            QuestDenyModal(self.claim_id, interaction.message)
        )


class QuestSignoffView(discord.ui.View):
    """The persistent Approve/Deny pair posted with a sign-off card."""

    def __init__(self, claim_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(QuestApproveButton(claim_id))
        self.add_item(QuestDenyButton(claim_id))


class QuestDenyModal(discord.ui.Modal, title="Deny quest claim"):
    reason: discord.ui.TextInput = discord.ui.TextInput(  # type: ignore[assignment]
        label="Reason (shown to the member)",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=_DENY_REASON_MAX,
    )

    def __init__(self, claim_id: int, card_message: discord.Message | None) -> None:
        super().__init__()
        self.claim_id = claim_id
        self.card_message = card_message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await _handle_resolution(
            interaction,
            self.claim_id,
            approve=False,
            deny_reason=self.reason.value.strip(),
            card_message=self.card_message,
        )


# ── resolution plumbing ───────────────────────────────────────────────────────


async def _load_settings(
    interaction: discord.Interaction, guild: discord.Guild | None
) -> EconSettings | None:
    if guild is None:
        return None
    bot = cast("Bot", interaction.client)

    def _read() -> EconSettings:
        with bot.ctx.open_db() as conn:
            return load_econ_settings(conn, guild.id)

    return await asyncio.to_thread(_read)


async def _safe_ephemeral(interaction: discord.Interaction, message: str) -> None:
    """Send an ephemeral note whether or not the interaction was answered."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        log.debug("econ quests: failed to send ephemeral note", exc_info=True)


def _read_claim_bundle(ctx: AppContext, claim_id: int) -> dict | None:
    """Load everything the card needs for a claim in one connection."""
    with ctx.open_db() as conn:
        claim = conn.execute(
            "SELECT * FROM econ_quest_claims WHERE id = ?", (claim_id,)
        ).fetchone()
        if claim is None:
            return None
        guild_id = int(claim["guild_id"])
        quest = get_quest(conn, guild_id, int(claim["quest_id"]))
        deny_count = len(
            deny_history(conn, int(claim["quest_id"]), int(claim["user_id"]))
        )
        settings = load_econ_settings(conn, guild_id)
    return {
        "claim": claim,
        "quest": quest,
        "deny_count": deny_count,
        "settings": settings,
    }


async def _handle_resolution(
    interaction: discord.Interaction,
    claim_id: int,
    *,
    approve: bool,
    deny_reason: str | None,
    card_message: discord.Message | None = None,
) -> None:
    """Approve/deny a sign-off claim: gate, resolve, edit card, DM claimant.

    Fail-safe throughout — any error becomes an ephemeral note so the button
    never dies. ``card_message`` is supplied by the deny modal (a modal-submit
    interaction has no ``.message``); the approve button uses its own message.
    """
    guild = interaction.guild
    member = interaction.user
    bot = cast("Bot", interaction.client)
    ctx = bot.ctx
    card = card_message if card_message is not None else interaction.message

    # Ack immediately: this chain does several awaits (accent, resolve, card
    # edit, DM) before it replies, which can blow past the 3s interaction
    # window. Deferring keeps the token alive; _safe_ephemeral is defer-aware.
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException:
        log.debug("econ quests: failed to defer resolution", exc_info=True)

    try:
        bundle = await asyncio.to_thread(_read_claim_bundle, ctx, claim_id)
    except Exception:
        log.exception("econ quests: failed to load claim %s", claim_id)
        await _safe_ephemeral(interaction, "Couldn't load that claim — try again.")
        return

    if bundle is None or bundle["quest"] is None:
        await _safe_ephemeral(interaction, "That claim no longer exists.")
        return

    settings: EconSettings = bundle["settings"]
    claim = bundle["claim"]
    quest = bundle["quest"]

    if guild is None or not isinstance(member, discord.Member):
        await _safe_ephemeral(interaction, "This only works in a server.")
        return
    if not can_manage_economy(member, settings):
        await _safe_ephemeral(interaction, MANAGE_DENIED_MSG)
        return

    accent = await resolve_accent_color(ctx.db_path, guild)
    claimant_id = int(claim["user_id"])

    # Already resolved (e.g. from the dashboard) — refresh the card to the true
    # state and tell the clicker, rather than double-crediting.
    if claim["state"] != "pending":
        await _refresh_resolved_card(card, accent, settings, claim, quest, bundle)
        await _safe_ephemeral(
            interaction, "That claim was already resolved — I refreshed the card."
        )
        return

    # Booster flag is the CLAIMANT's status at credit time, not the manager's.
    booster = (
        member_is_booster(bot, guild.id, claimant_id) if approve else False
    )

    def _resolve() -> object:
        with ctx.open_db() as conn:
            return resolve_claim(
                conn,
                settings,
                claim_id,
                approve=approve,
                resolver_id=member.id,
                deny_reason=deny_reason,
                booster=booster,
            )

    try:
        resolution = await asyncio.to_thread(_resolve)
    except ValueError:
        # Lost the race (resolved between our read and write) — refresh + note.
        try:
            fresh = await asyncio.to_thread(_read_claim_bundle, ctx, claim_id)
        except Exception:
            fresh = None
        if fresh is not None and fresh["quest"] is not None:
            await _refresh_resolved_card(
                card, accent, settings, fresh["claim"], fresh["quest"], fresh
            )
        await _safe_ephemeral(
            interaction, "That claim was already resolved — I refreshed the card."
        )
        return
    except Exception:
        log.exception("econ quests: failed to resolve claim %s", claim_id)
        await _safe_ephemeral(interaction, "Couldn't resolve that claim — try again.")
        return

    new_state = "paid" if approve else "denied"
    embed = render_signoff_card_embed(
        accent,
        settings,
        claimant_mention=f"<@{claimant_id}>",
        quest_title=str(quest["title"]),
        reward=int(quest["reward"]),
        criteria=str(quest["criteria"]),
        deny_count=bundle["deny_count"],
        state=new_state,
        resolver_id=member.id,
        deny_reason=deny_reason,
    )
    if card is not None:
        try:
            await card.edit(embed=embed, view=None)
        except discord.HTTPException:
            log.warning("econ quests: failed to edit sign-off card for %s", claim_id)

    paid = int(getattr(resolution, "paid", 0))
    await _dm_resolution(
        bot, ctx.db_path, guild.id, claimant_id, settings, quest, approve, paid,
        deny_reason,
    )

    if approve:
        ack = f"Approved — paid {_reward_text(settings, paid)} to <@{claimant_id}>."
    else:
        ack = f"Denied <@{claimant_id}>'s claim."
    await _safe_ephemeral(interaction, ack)


async def _refresh_resolved_card(
    card: discord.Message | None,
    accent: discord.Color,
    settings: EconSettings,
    claim,
    quest,
    bundle: dict,
) -> None:
    if card is None:
        return
    embed = render_signoff_card_embed(
        accent,
        settings,
        claimant_mention=f"<@{int(claim['user_id'])}>",
        quest_title=str(quest["title"]),
        reward=int(quest["reward"]),
        criteria=str(quest["criteria"]),
        deny_count=bundle["deny_count"],
        state=str(claim["state"]),
        resolver_id=(
            int(claim["resolver_id"]) if claim["resolver_id"] is not None else None
        ),
        deny_reason=claim["deny_reason"],
    )
    try:
        await card.edit(embed=embed, view=None)
    except discord.HTTPException:
        log.debug("econ quests: failed to refresh resolved card", exc_info=True)


async def _dm_resolution(
    bot: Bot,
    db_path,
    guild_id: int,
    claimant_id: int,
    settings: EconSettings,
    quest,
    approve: bool,
    paid: int,
    deny_reason: str | None,
) -> None:
    title = str(quest["title"])
    if approve:
        embed = discord.Embed(
            title="Quest approved",
            description=(
                f"Your claim for **{title}** was approved — "
                f"{_reward_text(settings, paid)} added to your wallet."
            ),
            color=discord.Color.green(),
        )
    else:
        embed = discord.Embed(
            title="Quest claim denied",
            description=(
                f"Your claim for **{title}** was denied. You can try again."
            ),
            color=discord.Color.red(),
        )
        if deny_reason:
            embed.add_field(name="Reason", value=deny_reason, inline=False)
    try:
        await notify_member(bot, db_path, guild_id, claimant_id, embed=embed)
    except Exception:
        log.debug("econ quests: failed to DM claim resolution", exc_info=True)


# ── sign-off card posting (from the claim select) ─────────────────────────────


async def post_signoff_card(
    bot: Bot,
    ctx: AppContext,
    guild: discord.Guild,
    settings: EconSettings,
    accent: discord.Color,
    claim_id: int,
    claimant: discord.Member,
) -> None:
    """Best-effort: post a sign-off card to the bank channel and record its ids.

    The pending claim row already exists, so a missing/forbidden bank channel
    must never raise back to the claimant — a cardless pending is still
    resolvable from the dashboard. Records the card ids only on a real send.
    """
    if not settings.bank_channel_id:
        return
    channel = guild.get_channel(settings.bank_channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        return

    def _card_ctx() -> dict | None:
        with ctx.open_db() as conn:
            quest = conn.execute(
                """
                SELECT q.* FROM econ_quests q
                JOIN econ_quest_claims c ON c.quest_id = q.id
                WHERE c.id = ?
                """,
                (claim_id,),
            ).fetchone()
            if quest is None:
                return None
            deny_count = len(
                deny_history(conn, int(quest["id"]), claimant.id)
            )
        return {"quest": quest, "deny_count": deny_count}

    try:
        card_ctx = await asyncio.to_thread(_card_ctx)
        if card_ctx is None:
            return
        quest = card_ctx["quest"]
        embed = render_signoff_card_embed(
            accent,
            settings,
            claimant_mention=claimant.mention,
            quest_title=str(quest["title"]),
            reward=int(quest["reward"]),
            criteria=str(quest["criteria"]),
            deny_count=card_ctx["deny_count"],
            state="pending",
        )
        message = await channel.send(embed=embed, view=QuestSignoffView(claim_id))
    except discord.HTTPException:
        log.warning("econ quests: failed to post sign-off card for %s", claim_id)
        return
    except Exception:
        log.exception("econ quests: unexpected error posting card %s", claim_id)
        return

    def _record() -> None:
        with ctx.open_db() as conn:
            set_claim_card(conn, claim_id, channel.id, message.id)

    try:
        await asyncio.to_thread(_record)
    except Exception:
        log.debug("econ quests: failed to record card ids", exc_info=True)


# ── /bank quests claim select ─────────────────────────────────────────────────


class QuestDetailSelect(discord.ui.Select):
    """Ephemeral select showing one quest's full story on demand.

    The /bank quests list is deliberately terse (one line per quest); this
    select carries the long form — description, how it completes, progress —
    without the list paying for it up front.
    """

    def __init__(
        self,
        settings: EconSettings,
        quests: list[dict],
        *,
        accent: discord.Color | None = None,
    ) -> None:
        self.settings = settings
        self.accent = accent
        self._quests = {str(q["id"]): q for q in quests[:25]}
        options = [
            discord.SelectOption(
                label=str(q["title"])[:100],
                value=str(q["id"]),
                description=str(q["qtype"])[:100],
            )
            for q in quests[:25]
        ]
        super().__init__(
            placeholder="ℹ️ Quest details…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        q = self._quests.get(self.values[0])
        if q is None:  # stale view after a reroll — just re-run /bank quests
            await interaction.response.send_message(
                "That quest is no longer on your board — re-run "
                "`/bank quests`.",
                ephemeral=True,
            )
            return
        settings = self.settings
        reward = int(q["reward"])
        reward_line = f"**{reward:,}** {_unit(settings, reward)}"
        if q.get("reward_xp"):
            reward_line += f" + ⭐ {int(q['reward_xp']):,} XP"
        reward_line += f" · {q['qtype']}"
        if q.get("spotlight"):
            reward_line += " · ⚡×2 this week"
        lines = [reward_line]
        if q.get("description"):
            lines.append(str(q["description"]))
        state = str(q.get("state") or "")
        if state == "community":
            lines.append(progress_bar(int(q["current"]), int(q["target"])))
        else:
            label = QUEST_STATE_LABEL.get(state, "")
            if label:
                lines.append(label)
            if q.get("progress_target") and state not in ("done", "pending"):
                lines.append(
                    progress_bar(
                        int(q["progress_current"]), int(q["progress_target"])
                    )
                )
        embed = discord.Embed(
            title=f"{settings.currency_emoji} {q['title']}",
            description="\n".join(lines),
            color=self.accent,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class QuestClaimSelect(discord.ui.Select):
    """Ephemeral select of the caller's currently-claimable quests."""

    def __init__(
        self,
        ctx: AppContext,
        settings: EconSettings,
        guild: discord.Guild,
        claimable: list[dict],
    ) -> None:
        self.ctx = ctx
        self.settings = settings
        self.guild = guild
        # qtype/signoff keyed by quest id for the click-time claim.
        self._meta = {q["id"]: q for q in claimable}
        options = [
            discord.SelectOption(
                label=str(q["title"])[:100],
                value=str(q["id"]),
                description=(f"{q['reward']} {_unit(settings, int(q['reward']))} · {q['qtype']}")[
                    :100
                ],
            )
            for q in claimable
        ]
        super().__init__(
            placeholder="Claim a quest…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        bot = cast("Bot", interaction.client)
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.followup.send("This only works in a server.", ephemeral=True)
            return

        quest_id = int(self.values[0])
        meta = self._meta.get(quest_id)
        if meta is None:
            await interaction.followup.send("That quest is no longer available.", ephemeral=True)
            return
        qtype = str(meta["qtype"])
        booster = member.premium_since is not None
        guild_id = self.guild.id
        settings = self.settings

        def _claim() -> object:
            with self.ctx.open_db() as conn:
                offset = get_tz_offset_hours(conn, guild_id)
                day = local_day_for(time.time(), offset)
                period = quest_period(qtype, day)
                return claim_quest(
                    conn, settings, guild_id, quest_id, member.id,
                    period=period, booster=booster,
                )

        try:
            outcome = await asyncio.to_thread(_claim)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            log.exception("econ quests: claim failed for quest %s", quest_id)
            await interaction.followup.send(
                "Something went wrong claiming that quest — try again.", ephemeral=True
            )
            return

        state = getattr(outcome, "state", "")
        if state == "paid":
            paid = int(getattr(outcome, "paid", 0))
            accent = await resolve_accent_color(self.ctx.db_path, self.guild)
            embed = discord.Embed(
                title="🎉 Quest complete!",
                description=(
                    f"**{meta['title']}** — {_reward_text(settings, paid)} "
                    "added to your wallet."
                ),
                color=accent,
            )
            if settings.currency_icon_url:
                embed.set_thumbnail(url=settings.currency_icon_url)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # Sign-off: post the card best-effort, then confirm submission.
        accent = await resolve_accent_color(self.ctx.db_path, self.guild)
        claim_id = int(getattr(outcome, "claim_id", 0))
        await post_signoff_card(
            bot, self.ctx, self.guild, settings, accent, claim_id, member
        )
        await interaction.followup.send(
            f"Submitted **{meta['title']}** for manager sign-off — "
            "you'll be notified when it's reviewed.",
            ephemeral=True,
        )


class QuestRerollSelect(discord.ui.Select):
    """Board reroll: swap an untouched quest for a new one.

    One free reroll per guild-local day, then paid ones up to the daily cap.
    The service picks the replacement (member's own shuffle order, different
    trigger kind preferred), validates everything and charges — this select
    just surfaces the result; on success it confirms old → new and disables
    itself, since the price of the *next* reroll may have changed.
    """

    def __init__(
        self,
        ctx: AppContext,
        settings: EconSettings,
        guild: discord.Guild,
        rerollable: list[dict],
        local_day: str,
        reroll_cost: int | None = 0,
    ) -> None:
        self.ctx = ctx
        self.settings = settings
        self.guild = guild
        self.local_day = local_day
        options = [
            discord.SelectOption(
                label=str(q["title"])[:100],
                value=str(q["id"]),
                description=str(q["qtype"])[:100],
            )
            for q in rerollable[:25]
        ]
        if reroll_cost:
            unit = _unit(settings, int(reroll_cost))
            price = f"{reroll_cost} {unit}"
        else:
            price = "free"
        super().__init__(
            placeholder=f"🎲 Reroll one untouched quest ({price})…"[:150],
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        quest_id = int(self.values[0])
        user_id = interaction.user.id
        guild_id = self.guild.id

        def _do_reroll():
            with self.ctx.open_db() as conn:
                old = get_quest(conn, guild_id, quest_id)
                new, cost = reroll_board_slot(
                    conn, self.settings, guild_id, user_id, quest_id,
                    self.local_day,
                )
                return old, new, cost

        try:
            old, new, cost = await asyncio.to_thread(_do_reroll)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        self.disabled = True
        try:
            await interaction.edit_original_response(view=self.view)
        except discord.HTTPException:
            pass
        old_title = str(old["title"]) if old else "that quest"
        unit = _unit(self.settings, int(new["reward"]))
        paid = (
            f" Cost {cost} {_unit(self.settings, int(cost))}."
            if cost
            else " That was today's free reroll."
        )
        await interaction.followup.send(
            f"🎲 Swapped **{old_title}** for **{new['title']}** "
            f"({new['reward']} {unit} · {new['qtype']}).{paid} "
            "Run `/bank quests` again to see your refreshed board.",
            ephemeral=True,
        )


class QuestClaimView(discord.ui.View):
    """Ephemeral view for /bank quests: claim select and/or reroll select."""

    def __init__(
        self,
        ctx: AppContext,
        settings: EconSettings,
        guild: discord.Guild,
        claimable: list[dict],
        *,
        rerollable: list[dict] | None = None,
        reroll_cost: int | None = 0,
        local_day: str = "",
        detailable: list[dict] | None = None,
        accent: discord.Color | None = None,
    ) -> None:
        super().__init__(timeout=_CLAIM_VIEW_TIMEOUT)
        if detailable:
            self.add_item(QuestDetailSelect(settings, detailable, accent=accent))
        if claimable:
            self.add_item(QuestClaimSelect(ctx, settings, guild, claimable))
        if rerollable and local_day:
            self.add_item(
                QuestRerollSelect(
                    ctx, settings, guild, rerollable, local_day, reroll_cost
                )
            )


# ── quest-board "Show my quests" button ──────────────────────────────────────

QUEST_BOARD_CUSTOM_ID = "econ:show_my_quests"


class ShowMyQuestsButton(discord.ui.Button):
    """Persistent button on the public leaderboard/quest-board panel.

    The board is anonymous by design (no member names, no per-member draw), so
    this is the members' door from it into their own private quest list — the
    same ephemeral panel ``/bank quests`` opens. One button serves everyone; the
    per-member state lives entirely in the ephemeral reply, so it carries no id.
    """

    def __init__(self) -> None:
        super().__init__(
            label="Show my quests",
            emoji="📋",
            style=discord.ButtonStyle.secondary,
            custom_id=QUEST_BOARD_CUSTOM_ID,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = cast("Bot", interaction.client)
        cog = bot.get_cog("EconomyCog")
        if cog is None:  # cog unloaded mid-restart — never a dead button
            await interaction.response.send_message(
                "Quests are unavailable right now — try again in a moment.",
                ephemeral=True,
            )
            return
        await cog.send_quests_panel(interaction)  # type: ignore[attr-defined]


class QuestBoardView(discord.ui.View):
    """The persistent view attached to the leaderboard/quest-board panel."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(ShowMyQuestsButton())
