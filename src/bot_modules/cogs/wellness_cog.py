"""Wellness Guardian user commands — `/wellness *` group."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.services.embeds import WELLNESS_PRIMARY
from bot_modules.services.wellness_scheduler import (
    wellness_active_list_loop,
    wellness_tick_loop,
    wellness_weekly_report_loop,
)
from bot_modules.services.wellness_service import (
    get_wellness_config,
    get_wellness_user,
    opt_in_user,
    update_away_message,
    wellness_dashboard_link,
)
from bot_modules.core.utils import format_user_for_log

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.wellness")

TIMEZONE_CHOICES: list[tuple[str, str]] = [
    ("UTC", "UTC"),
    ("US Eastern", "America/New_York"),
    ("US Central", "America/Chicago"),
    ("US Mountain", "America/Denver"),
    ("US Pacific", "America/Los_Angeles"),
    ("US Alaska", "America/Anchorage"),
    ("US Hawaii", "Pacific/Honolulu"),
    ("UK / London", "Europe/London"),
    ("Central Europe", "Europe/Berlin"),
    ("Eastern Europe", "Europe/Athens"),
    ("India", "Asia/Kolkata"),
    ("China", "Asia/Shanghai"),
    ("Japan", "Asia/Tokyo"),
    ("AU Eastern", "Australia/Sydney"),
    ("AU Western", "Australia/Perth"),
    ("New Zealand", "Pacific/Auckland"),
    ("Brazil", "America/Sao_Paulo"),
]

ENFORCEMENT_LABELS: dict[str, str] = {
    "gentle": "💛 Gentle reminders",
    "slow_mode": "🐢 Slow mode",
    "gradual": "🌱 Gradual",
}

ENFORCEMENT_DESCRIPTIONS: dict[str, str] = {
    "gentle": "I'll send you a heads-up, but won't stop you.",
    "slow_mode": "I'll add a per-user slow mode so you can still post, just slower.",
    "gradual": "Start with reminders, then breather suggestions, then slow mode if needed.",
}

AWAY_MESSAGE_MAX = 500
AWAY_DEFAULT_TEXT = (
    "I'm taking a wellness break right now and may not see this for a while. "
    "I'll get back to you when I'm back. 💚"
)

def _render_away_preview(
    text: str, member: discord.Member | discord.User
) -> discord.Embed:
    return discord.Embed(
        title=f"💚 {member.display_name} Is Away",
        description=text or AWAY_DEFAULT_TEXT,
        color=WELLNESS_PRIMARY,
    )


# ---------------------------------------------------------------------------
# Setup wizard view
# ---------------------------------------------------------------------------


class _SetupWizardView(discord.ui.View):
    def __init__(self, ctx: AppContext, invoker_id: int) -> None:
        super().__init__(timeout=300)
        self._ctx = ctx
        self._invoker_id = invoker_id
        self._timezone: str | None = None
        self._enforcement: str | None = None
        self._step: int = 1

        self._tz_select: discord.ui.Select[discord.ui.View] = discord.ui.Select(
            placeholder="Pick your timezone…",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=label, value=value)
                for label, value in TIMEZONE_CHOICES
            ],
        )
        self._tz_select.callback = self._on_tz_pick  # type: ignore[assignment]
        self.add_item(self._tz_select)

        self._enf_select: discord.ui.Select[discord.ui.View] = discord.ui.Select(
            placeholder="Pick your enforcement level…",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=ENFORCEMENT_LABELS[key],
                    value=key,
                    description=ENFORCEMENT_DESCRIPTIONS[key][:100],
                )
                for key in ("gentle", "slow_mode", "gradual")
            ],
        )
        self._enf_select.callback = self._on_enf_pick  # type: ignore[assignment]

    def _check_invoker(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self._invoker_id

    def _build_step1_embed(self) -> discord.Embed:
        return discord.Embed(
            title="🌿 Welcome to Wellness Guardian",
            description=(
                "This tool helps you set healthy boundaries with Discord — "
                "**it's not a substitute for professional support.** "
                "If you're ever struggling, please reach out to someone "
                "you trust.\n\n"
                "**Step 1 of 2** — 🕐 What's your timezone?"
            ),
            color=WELLNESS_PRIMARY,
        )

    def _build_step2_embed(self) -> discord.Embed:
        return discord.Embed(
            title="🛡️ How Firm Should Your Boundaries Be?",
            description=(
                "**Step 2 of 2** — All levels preserve your ability to post. Nothing locks you out.\n\n"
                + "\n".join(
                    f"**{ENFORCEMENT_LABELS[k]}** — {ENFORCEMENT_DESCRIPTIONS[k]}"
                    for k in ("gentle", "slow_mode", "gradual")
                )
            ),
            color=WELLNESS_PRIMARY,
        )

    def _build_done_embed(self, member: discord.Member) -> discord.Embed:
        return discord.Embed(
            title="✅ You're All Set!",
            description=(
                "Your **Wellness Guardian** role has been assigned — "
                "check out the new 🌿 Wellness channels in your channel list.\n\n"
                "**Next steps:**\n"
                "• Set message caps, schedule offline hours and fine-tune "
                "everything from the "
                f"**{wellness_dashboard_link()}**.\n"
                "• `/wellness away set` — Turn your away auto-reply on or off, with an\n"
                "  optional custom message"
            ),
            color=WELLNESS_PRIMARY,
        )

    async def _on_tz_pick(self, interaction: discord.Interaction) -> None:
        if not self._check_invoker(interaction):
            await interaction.response.defer()
            return
        self._timezone = self._tz_select.values[0]
        self._tz_select.disabled = True
        self._tz_select.placeholder = f"Timezone: {self._timezone}"
        if self._enf_select not in self.children:
            self.add_item(self._enf_select)
        self._step = 2
        await interaction.response.edit_message(embed=self._build_step2_embed(), view=self)

    async def _on_enf_pick(self, interaction: discord.Interaction) -> None:
        if not self._check_invoker(interaction):
            await interaction.response.defer()
            return
        self._enforcement = self._enf_select.values[0]
        guild = interaction.guild
        if guild is None or self._timezone is None:
            await interaction.response.edit_message(
                content="Setup failed.", embed=None, view=None
            )
            return
        try:
            await self._finish_setup(interaction, guild)
        except Exception:
            log.exception(
                "Wellness setup failed for user %s",
                format_user_for_log(interaction.user),
            )
            try:
                await interaction.response.edit_message(
                    content="⚠️ Something went wrong during setup. Please try again.",
                    embed=None,
                    view=None,
                )
            except discord.NotFound:
                pass

    async def _finish_setup(
        self, interaction: discord.Interaction, guild: discord.Guild
    ) -> None:
        assert self._timezone is not None
        assert self._enforcement is not None

        ctx = self._ctx
        guild_id = guild.id

        def _get_cfg():
            with ctx.open_db() as conn:
                return get_wellness_config(conn, guild_id)

        cfg = await asyncio.to_thread(_get_cfg)

        if cfg is None or not cfg.role_id:
            self.stop()
            await interaction.response.edit_message(
                content=(
                    "⚠️ Wellness Guardian isn't set up on this server yet. "
                    "An admin can activate it on the web dashboard under "
                    "**Config → Members → Wellness**."
                ),
                embed=None,
                view=None,
            )
            return

        member = guild.get_member(interaction.user.id)
        if member is None:
            self.stop()
            await interaction.response.edit_message(
                content="Could not resolve your member record.", embed=None, view=None
            )
            return

        role = guild.get_role(cfg.role_id)
        if role is None:
            self.stop()
            await interaction.response.edit_message(
                content=(
                    "⚠️ The wellness role no longer exists. "
                    "An admin can pick or recreate it on the web dashboard "
                    "under **Config → Members → Wellness**."
                ),
                embed=None,
                view=None,
            )
            return

        timezone = self._timezone
        enforcement = self._enforcement
        user_id = interaction.user.id

        def _opt_in():
            with ctx.open_db() as conn:
                opt_in_user(conn, guild_id, user_id, timezone=timezone, enforcement_level=enforcement)

        await asyncio.to_thread(_opt_in)

        try:
            await member.add_roles(role, reason="Wellness Guardian opt-in")
        except discord.HTTPException:
            self.stop()
            await interaction.response.edit_message(
                content=(
                    "⚠️ I couldn't assign the wellness role — I'm missing permissions. "
                    "Your settings have been saved; ask an admin to fix the bot's role hierarchy."
                ),
                embed=None,
                view=None,
            )
            return

        self._tz_select.disabled = True
        self._enf_select.disabled = True
        self._enf_select.placeholder = (
            f"Enforcement: {ENFORCEMENT_LABELS[self._enforcement]}"
        )
        await interaction.response.edit_message(
            embed=self._build_done_embed(member), view=None
        )
        self.stop()


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


async def _require_active_user(ctx: AppContext, interaction: discord.Interaction):
    if interaction.guild_id is None:
        return None
    guild_id = interaction.guild_id
    user_id = interaction.user.id

    def _q():
        with ctx.open_db() as conn:
            return get_wellness_user(conn, guild_id, user_id)

    user = await asyncio.to_thread(_q)
    if user is None or not user.is_active:
        return None
    return user


class WellnessCog(commands.Cog):
    wellness = app_commands.Group(
        name="wellness",
        description="Wellness opt-in and away auto-reply.",
    )
    away = app_commands.Group(
        name="away",
        description="Auto-reply when someone mentions you while you're away.",
        parent=wellness,
    )

    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        super().__init__()

    async def cog_load(self) -> None:
        bot = self.bot
        db_path = self.bot.ctx.db_path
        self.bot.startup_task_factories.append(lambda: wellness_tick_loop(bot, db_path))
        self.bot.startup_task_factories.append(
            lambda: wellness_active_list_loop(bot, db_path)
        )
        self.bot.startup_task_factories.append(
            lambda: wellness_weekly_report_loop(bot, db_path)
        )

    # ── /wellness setup ───────────────────────────────────────────────────

    @wellness.command(
        name="setup", description="Opt in — pick your timezone and enforcement style."
    )
    async def setup_cmd(self, interaction: discord.Interaction) -> None:
        await self.open_setup(interaction)

    async def open_setup(self, interaction: discord.Interaction) -> None:
        """Shared by ``/wellness setup`` and the ``/info`` panel's button.

        The timezone wizard *is* the opt-in — there is no way to be opted in
        without one — so the panel re-enters this flow rather than writing an
        opt-in row of its own.
        """
        ctx = self.bot.ctx
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("❌ Server only.", ephemeral=True)
            return

        guild_id = guild.id

        def _get_cfg():
            with ctx.open_db() as conn:
                return get_wellness_config(conn, guild_id)

        cfg = await asyncio.to_thread(_get_cfg)
        if cfg is None or not cfg.role_id:
            await interaction.response.send_message(
                "⚠️ Wellness Guardian isn't set up on this server yet. "
                "An admin can activate it on the web dashboard under "
                "**Config → Members → Wellness**.",
                ephemeral=True,
            )
            return

        view = _SetupWizardView(ctx, interaction.user.id)
        await interaction.response.send_message(
            embed=view._build_step1_embed(), view=view, ephemeral=True
        )

    # ── /wellness away ────────────────────────────────────────────────────

    # One dial with two states rather than two commands (CLAUDE.md: "collapse
    # controls"). `message` only means anything when switching on, and is
    # rejected rather than silently dropped when passed with off — a member who
    # typed a new away message deserves to know it wasn't saved.
    @away.command(name="set", description="Turn your away auto-reply on or off.")
    @app_commands.describe(
        state="On or off.",
        message=f"Optional new away message, when turning on (max {AWAY_MESSAGE_MAX} chars).",
    )
    @app_commands.choices(
        state=[
            app_commands.Choice(name="on", value="on"),
            app_commands.Choice(name="off", value="off"),
        ]
    )
    async def away_set_cmd(
        self,
        interaction: discord.Interaction,
        state: app_commands.Choice[str],
        message: str | None = None,
    ) -> None:
        ctx = self.bot.ctx
        guild = interaction.guild
        if guild is None:
            return
        user = await _require_active_user(ctx, interaction)
        if user is None:
            await interaction.response.send_message(
                "❌ You haven't opted in yet — run `/wellness setup` first.", ephemeral=True
            )
            return

        turning_on = state.value == "on"
        if message is not None and not turning_on:
            await interaction.response.send_message(
                "❌ An away message only applies when turning away mode **on**.",
                ephemeral=True,
            )
            return
        if message is not None and len(message) > AWAY_MESSAGE_MAX:
            await interaction.response.send_message(
                f"❌ Away message must be {AWAY_MESSAGE_MAX} characters or fewer.", ephemeral=True
            )
            return

        guild_id = guild.id
        user_id = interaction.user.id

        def _write():
            with ctx.open_db() as conn:
                update_away_message(
                    conn, guild_id, user_id, enabled=turning_on, message=message
                )
                return get_wellness_user(conn, guild_id, user_id)

        updated = await asyncio.to_thread(_write)

        if not turning_on:
            await interaction.response.send_message(
                "💚 Away mode is off. Welcome back!", ephemeral=True
            )
            return

        text = (updated.away_message if updated else "") or AWAY_DEFAULT_TEXT
        embed = _render_away_preview(text, interaction.user)
        embed.set_footer(text="Away mode ON. Use /wellness away set state:off to turn it off.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: Bot) -> None:
    await bot.add_cog(WellnessCog(bot))
