"""Economy — the ``/bank`` command surface (wallet view + mod grants).

Thin cog over ``bot_modules.services.economy_service``: it loads per-guild
``econ_`` settings on each interaction (cheap KV reads, no cache for stage 0),
resolves the branded currency naming, and renders the accent-coloured embeds.
See docs/economy_spec.md for the feature design.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.branding import resolve_accent_color
from bot_modules.core.db_utils import get_tz_offset_hours
from bot_modules.economy.logic import local_day_for
from bot_modules.economy.quest_views import (
    QuestApproveButton,
    QuestClaimView,
    QuestDenyButton,
    can_manage_economy,
)
from bot_modules.economy.quests import quest_period
from bot_modules.services.economy_service import (
    EconSettings,
    apply_credit,
    create_qotd,
    get_balance,
    get_ledger,
    get_notify_muted,
    load_econ_settings,
    set_notify_muted,
)
from bot_modules.services.quote_renderer import THEMES, render_quote_card

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.economy")

_DISABLED_MSG = "The economy isn't enabled on this server yet."
_QOTD_CARD_FILENAME = "qotd.png"


async def _resolve_qotd_image(guild: discord.Guild, bot: Bot) -> bytes | None:
    """Bytes for the QOTD card background — the server icon, bot avatar fallback."""
    if guild.icon is not None:
        try:
            return await guild.icon.replace(size=512).read()
        except discord.HTTPException:
            log.warning("qotd: failed to read guild icon for %s", guild.id)
    user = bot.user
    if user is not None:
        try:
            return await user.display_avatar.with_size(512).read()
        except discord.HTTPException:
            log.warning("qotd: failed to read bot avatar")
    return None


def _unit(settings: EconSettings, amount: int) -> str:
    """Currency name matching ``amount``'s grammatical number."""
    return settings.currency_name if abs(amount) == 1 else settings.currency_plural


_QUEST_STATE_LABEL = {
    "claimable": "✅ Ready to claim",
    "pending": "⏳ Awaiting sign-off",
    "done": "☑️ Completed this period",
}


def _progress_bar(current: int, target: int, width: int = 10) -> str:
    """A text meter for a community quest's running total."""
    if target <= 0:
        return f"{current:,}"
    filled = max(0, min(width, round(width * current / target)))
    return f"{'▰' * filled}{'▱' * (width - filled)} {current:,}/{target:,}"


def _can_grant(user: discord.Member, settings: EconSettings) -> bool:
    """True for server admins or holders of the configured manager role.

    Delegates to the canonical gate in ``quest_views`` so the grant command
    and the sign-off buttons enforce one rule.
    """
    return can_manage_economy(user, settings)


class EconomyCog(commands.Cog):
    bank = app_commands.Group(
        name="bank",
        description="Wallet and currency commands.",
        guild_only=True,
    )

    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    @bank.command(name="wallet", description="Check your balance and recent activity.")
    async def bank_wallet(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        guild_id = guild.id
        user_id = interaction.user.id

        def _load() -> tuple[EconSettings, int, list]:
            with self.ctx.open_db() as conn:
                settings = load_econ_settings(conn, guild_id)
                balance = get_balance(conn, guild_id, user_id)
                ledger = get_ledger(conn, guild_id, user_id, limit=10)
            return settings, balance, ledger

        settings, balance, ledger = await asyncio.to_thread(_load)

        if not settings.enabled:
            await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
            return

        accent = await resolve_accent_color(self.ctx.db_path, guild)
        embed = discord.Embed(
            title=settings.wallet_name,
            description=(
                f"{settings.currency_emoji} **{balance:,}** {_unit(settings, balance)}"
            ),
            colour=accent,
        )
        if settings.currency_icon_url:
            embed.set_thumbnail(url=settings.currency_icon_url)

        if ledger:
            lines = []
            for row in ledger:
                amount = int(row["amount"])
                sign = "+" if amount >= 0 else "-"
                ts = int(row["created_at"])
                lines.append(
                    f"{sign}{abs(amount):,} {settings.currency_emoji} · "
                    f"{row['kind']} · <t:{ts}:R>"
                )
            embed.add_field(name="Recent activity", value="\n".join(lines), inline=False)
        else:
            embed.add_field(
                name="Recent activity", value="_No activity yet._", inline=False
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bank.command(name="grant", description="Award currency to a member (staff only).")
    @app_commands.describe(
        member="Who to award",
        amount="How much to award (whole number)",
        reason="Why — recorded in the ledger",
    )
    async def bank_grant(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: int,
        reason: str,
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        guild_id = guild.id
        actor = interaction.user
        assert isinstance(actor, discord.Member)

        settings = await asyncio.to_thread(self._load_settings, guild_id)

        if not settings.enabled:
            await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
            return

        if not _can_grant(actor, settings):
            await interaction.response.send_message(
                "You don't have permission to grant currency.", ephemeral=True
            )
            return

        if member.bot:
            await interaction.response.send_message(
                "Bots don't have wallets.", ephemeral=True
            )
            return

        if amount < 1:
            await interaction.response.send_message(
                "The amount must be at least 1.", ephemeral=True
            )
            return

        booster = member.premium_since is not None
        meta = {"reason": reason, "granted_by": actor.display_name}

        def _grant() -> int:
            with self.ctx.open_db() as conn:
                return apply_credit(
                    conn,
                    guild_id,
                    member.id,
                    amount,
                    "grant",
                    actor_id=actor.id,
                    meta=meta,
                    booster=booster,
                    multiplier=settings.booster_multiplier,
                )

        credited = await asyncio.to_thread(_grant)

        accent = await resolve_accent_color(self.ctx.db_path, guild)
        embed = discord.Embed(
            title="Currency granted",
            description=(
                f"{settings.currency_emoji} **{credited:,}** {_unit(settings, credited)} "
                f"→ {member.mention}"
            ),
            colour=accent,
        )
        if booster and credited != amount:
            embed.add_field(
                name="Booster bonus",
                value=f"Base {amount:,} × {settings.booster_multiplier:g}",
                inline=False,
            )
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_footer(text=f"Granted by {actor.display_name}")

        await interaction.response.send_message(embed=embed)

    @bank.command(
        name="mute", description="Toggle economy DM notifications for yourself."
    )
    async def bank_mute(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        guild_id = guild.id
        user_id = interaction.user.id

        settings = await asyncio.to_thread(self._load_settings, guild_id)
        if not settings.enabled:
            await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
            return

        def _toggle() -> bool:
            with self.ctx.open_db() as conn:
                new_muted = not get_notify_muted(conn, guild_id, user_id)
                set_notify_muted(conn, guild_id, user_id, new_muted)
                return new_muted

        muted = await asyncio.to_thread(_toggle)

        accent = await resolve_accent_color(self.ctx.db_path, guild)
        embed = discord.Embed(
            title="Notifications muted" if muted else "Notifications on",
            description=(
                "You won't get economy DMs anymore. Run this again to turn them back on."
                if muted
                else "You'll get economy DMs again — milestones, streak saves, and more."
            ),
            colour=accent,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bank.command(name="quests", description="View and claim the server's active quests.")
    async def bank_quests(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        guild = interaction.guild

        settings, quests_state = await asyncio.to_thread(
            self._load_quests_state, guild.id, interaction.user.id
        )
        if not settings.enabled:
            await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
            return

        accent = await resolve_accent_color(self.ctx.db_path, guild)
        embed = discord.Embed(title=f"{settings.currency_emoji} Quests", colour=accent)

        if not quests_state:
            embed.description = "_No active quests right now — check back soon!_"
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        for q in quests_state:
            reward = int(q["reward"])
            unit = _unit(settings, reward)
            header = f"{settings.currency_emoji} {q['title']}"
            lines = [f"**{reward:,}** {unit} · {q['qtype']}"]
            if q.get("description"):
                lines.append(str(q["description"]))
            if q["state"] == "community":
                lines.append(_progress_bar(q["current"], q["target"]))
            else:
                lines.append(_QUEST_STATE_LABEL.get(q["state"], ""))
            embed.add_field(name=header, value="\n".join(lines), inline=False)

        claimable = [q for q in quests_state if q["state"] == "claimable"]
        kwargs: dict = {"embed": embed, "ephemeral": True}
        if claimable:
            kwargs["view"] = QuestClaimView(self.ctx, settings, guild, claimable)
        await interaction.response.send_message(**kwargs)

    def _load_quests_state(
        self, guild_id: int, user_id: int
    ) -> tuple[EconSettings, list[dict]]:
        """Load active quests with the caller's per-period claim state.

        Community quests carry their running total (no self-claim); daily/weekly
        carry ``claimable``/``pending``/``done`` for this period's key.
        """
        with self.ctx.open_db() as conn:
            settings = load_econ_settings(conn, guild_id)
            if not settings.enabled:
                return settings, []
            offset = get_tz_offset_hours(conn, guild_id)
            day = local_day_for(time.time(), offset)
            rows = conn.execute(
                """
                SELECT * FROM econ_quests
                WHERE guild_id = ? AND active = 1
                ORDER BY qtype, id
                """,
                (guild_id,),
            ).fetchall()
            out: list[dict] = []
            for row in rows:
                qtype = str(row["qtype"])
                quest_id = int(row["id"])
                entry: dict = {
                    "id": quest_id,
                    "title": row["title"],
                    "description": row["description"],
                    "qtype": qtype,
                    "reward": int(row["reward"]),
                    "signoff": bool(row["signoff"]),
                    "criteria": row["criteria"],
                }
                if qtype == "community":
                    prog = conn.execute(
                        "SELECT current FROM econ_community_progress WHERE quest_id = ?",
                        (quest_id,),
                    ).fetchone()
                    target = row["community_target"]
                    entry["state"] = "community"
                    entry["current"] = int(prog["current"]) if prog else 0
                    entry["target"] = int(target) if target is not None else 0
                else:
                    period = quest_period(qtype, day)
                    claim = conn.execute(
                        """
                        SELECT state FROM econ_quest_claims
                        WHERE quest_id = ? AND user_id = ? AND period = ?
                          AND state IN ('paid', 'pending')
                        ORDER BY CASE state WHEN 'paid' THEN 0 ELSE 1 END
                        LIMIT 1
                        """,
                        (quest_id, user_id, period),
                    ).fetchone()
                    if claim is None:
                        entry["state"] = "claimable"
                    elif claim["state"] == "paid":
                        entry["state"] = "done"
                    else:
                        entry["state"] = "pending"
                out.append(entry)
        return settings, out

    qotd = app_commands.Group(
        name="qotd",
        description="Question of the day.",
        guild_only=True,
    )

    @qotd.command(
        name="post", description="Post today's question of the day (staff only)."
    )
    @app_commands.describe(question="The question to ask the server")
    async def qotd_post(
        self, interaction: discord.Interaction, question: str
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        guild_id = guild.id
        actor = interaction.user
        assert isinstance(actor, discord.Member)

        settings = await asyncio.to_thread(self._load_settings, guild_id)
        if not settings.enabled:
            await interaction.response.send_message(_DISABLED_MSG, ephemeral=True)
            return
        if not _can_grant(actor, settings):
            await interaction.response.send_message(
                "You don't have permission to post a question of the day.",
                ephemeral=True,
            )
            return

        channel = interaction.channel
        if not isinstance(channel, discord.abc.Messageable):
            await interaction.response.send_message(
                "I can't post a question here.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        accent = await resolve_accent_color(self.ctx.db_path, guild)

        # Prefer the rendered quote card; fall back to a plain branded embed if
        # there's no usable background image or the renderer raises.
        card_file: discord.File | None = None
        image_bytes = await _resolve_qotd_image(guild, self.bot)
        if image_bytes is not None:
            try:
                card_bytes = await asyncio.to_thread(
                    render_quote_card,
                    question,
                    author_name="Question of the Day",
                    avatar_bytes=image_bytes,
                    theme=THEMES["midnight"],
                    pfp_shape="none",
                )
                card_file = discord.File(
                    io.BytesIO(card_bytes), filename=_QOTD_CARD_FILENAME
                )
            except Exception:
                log.exception("qotd: failed to render card in guild %s", guild_id)

        try:
            if card_file is not None:
                message = await channel.send(file=card_file)
            else:
                embed = discord.Embed(
                    title="📣 Question of the Day",
                    description=question,
                    colour=accent,
                )
                message = await channel.send(embed=embed)
        except discord.Forbidden:
            await interaction.followup.send(
                "I don't have permission to post in this channel.", ephemeral=True
            )
            return

        def _record() -> None:
            with self.ctx.open_db() as conn:
                offset = get_tz_offset_hours(conn, guild_id)
                today = local_day_for(time.time(), offset)
                create_qotd(
                    conn, guild_id, channel.id, message.id, question, actor.id, today
                )

        await asyncio.to_thread(_record)
        await interaction.followup.send(
            "Posted the question of the day.", ephemeral=True
        )

    async def cog_load(self) -> None:
        # Re-register the persistent sign-off buttons so Approve/Deny clicks on
        # existing bank-channel cards still route after a restart — the
        # custom_ids embed the claim id (econ_claim:{approve,deny}:<id>).
        self.bot.add_dynamic_items(QuestApproveButton, QuestDenyButton)

    def _load_settings(self, guild_id: int) -> EconSettings:
        with self.ctx.open_db() as conn:
            return load_econ_settings(conn, guild_id)


async def setup(bot: Bot) -> None:
    await bot.add_cog(EconomyCog(bot, bot.ctx))
