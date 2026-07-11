"""Economy — the ``/bank`` command surface (wallet view + mod grants).

Thin cog over ``bot_modules.services.economy_service``: it loads per-guild
``econ_`` settings on each interaction (cheap KV reads, no cache for stage 0),
resolves the branded currency naming, and renders the accent-coloured embeds.
See docs/economy_spec.md for the feature design.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.branding import resolve_accent_color
from bot_modules.services.economy_service import (
    EconSettings,
    apply_credit,
    get_balance,
    get_ledger,
    load_econ_settings,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

_DISABLED_MSG = "The economy isn't enabled on this server yet."


def _unit(settings: EconSettings, amount: int) -> str:
    """Currency name matching ``amount``'s grammatical number."""
    return settings.currency_name if abs(amount) == 1 else settings.currency_plural


def _can_grant(user: discord.Member, settings: EconSettings) -> bool:
    """True for server admins or holders of the configured manager role."""
    if user.guild_permissions.administrator:
        return True
    role_id = settings.manager_role_id
    return role_id != 0 and any(r.id == role_id for r in user.roles)


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

    def _load_settings(self, guild_id: int) -> EconSettings:
        with self.ctx.open_db() as conn:
            return load_econ_settings(conn, guild_id)


async def setup(bot: Bot) -> None:
    await bot.add_cog(EconomyCog(bot, bot.ctx))
