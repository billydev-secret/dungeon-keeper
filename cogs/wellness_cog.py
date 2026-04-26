"""Wellness Guardian user commands — `/wellness *` group."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from services.embeds import WELLNESS_PRIMARY
from services.wellness_scheduler import (
    wellness_active_list_loop,
    wellness_tick_loop,
    wellness_weekly_report_loop,
)
from services.wellness_service import (
    get_wellness_config,
    get_wellness_user,
    opt_in_user,
    update_away_message,
    update_user_settings,
)
from utils import format_user_for_log

if TYPE_CHECKING:
    from app_context import AppContext, Bot

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
    "cooldown": "☕ Cooldown breaks",
    "slow_mode": "🐢 Slow mode",
    "gradual": "🌱 Gradual",
}

ENFORCEMENT_DESCRIPTIONS: dict[str, str] = {
    "gentle": "I'll send you a heads-up, but won't stop you.",
    "cooldown": "I'll suggest a 5-minute breather when you go over.",
    "slow_mode": "I'll add a per-user slow mode so you can still post, just slower.",
    "gradual": "Start with reminders, then breaks, then slow mode if needed.",
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
        title=f"💚 {member.display_name} is away",
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
            placeholder="Select your timezone…",
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
            placeholder="Select your enforcement level…",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=ENFORCEMENT_LABELS[key],
                    value=key,
                    description=ENFORCEMENT_DESCRIPTIONS[key][:100],
                )
                for key in ("gentle", "cooldown", "slow_mode", "gradual")
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
                "If you're ever struggling, please reach out to a trusted person "
                "or a crisis resource.\n\n"
                "**Step 1 of 2** — 🕐 What's your timezone?"
            ),
            color=WELLNESS_PRIMARY,
        )

    def _build_step2_embed(self) -> discord.Embed:
        return discord.Embed(
            title="🛡️ How firm should your boundaries be?",
            description=(
                "**Step 2 of 2** — All levels preserve your ability to post. Nothing locks you out.\n\n"
                + "\n".join(
                    f"**{ENFORCEMENT_LABELS[k]}** — {ENFORCEMENT_DESCRIPTIONS[k]}"
                    for k in ("gentle", "cooldown", "slow_mode", "gradual")
                )
            ),
            color=WELLNESS_PRIMARY,
        )

    def _build_done_embed(self, member: discord.Member) -> discord.Embed:
        return discord.Embed(
            title="✅ You're all set!",
            description=(
                "Your **Wellness Guardian** role has been assigned — "
                "check out the new 🌿 Wellness channels in your channel list.\n\n"
                "**Next steps:**\n"
                "• `/wellness cap add` — Set your first message limit\n"
                "• `/wellness blackout add` — Schedule offline hours\n"
                "• `/wellness partner request` — Find an accountability buddy\n"
                "• `/wellness away on` — Set a custom away message anytime\n"
                "• `/wellness settings` — Fine-tune your preferences"
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

        with self._ctx.open_db() as conn:
            cfg = get_wellness_config(conn, guild.id)
            opt_in_user(
                conn,
                guild.id,
                interaction.user.id,
                timezone=self._timezone,
                enforcement_level=self._enforcement,
            )

        if cfg is None or not cfg.role_id:
            await interaction.response.edit_message(
                content=(
                    "⚠️ Wellness Guardian isn't set up on this server yet. "
                    "An admin must run `/wellness-admin setup` first."
                ),
                embed=None,
                view=None,
            )
            return

        member = guild.get_member(interaction.user.id)
        if member is None:
            await interaction.response.edit_message(
                content="Could not resolve your member record.", embed=None, view=None
            )
            return

        role = guild.get_role(cfg.role_id)
        if role is None:
            await interaction.response.edit_message(
                content=(
                    "⚠️ The wellness role no longer exists. "
                    "Ask an admin to re-run `/wellness-admin setup`."
                ),
                embed=None,
                view=None,
            )
            return

        try:
            await member.add_roles(role, reason="Wellness Guardian opt-in")
        except discord.HTTPException:
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
# Settings view
# ---------------------------------------------------------------------------


class _SettingsView(discord.ui.View):
    def __init__(
        self,
        ctx: AppContext,
        invoker_id: int,
        current_enforcement: str,
        current_notifications: str,
        current_public: bool,
    ) -> None:
        super().__init__(timeout=300)
        self._ctx = ctx
        self._invoker_id = invoker_id

        enf_select: discord.ui.Select[discord.ui.View] = discord.ui.Select(
            placeholder="Enforcement level…",
            options=[
                discord.SelectOption(
                    label=ENFORCEMENT_LABELS[k],
                    value=k,
                    default=(k == current_enforcement),
                    description=ENFORCEMENT_DESCRIPTIONS[k][:100],
                )
                for k in ("gentle", "cooldown", "slow_mode", "gradual")
            ],
            row=0,
        )
        enf_select.callback = self._make_enforcement_cb()  # type: ignore[assignment]
        self.add_item(enf_select)

        notif_select: discord.ui.Select[discord.ui.View] = discord.ui.Select(
            placeholder="Notifications…",
            options=[
                discord.SelectOption(
                    label="Ephemeral (only in chat)",
                    value="ephemeral",
                    default=(current_notifications == "ephemeral"),
                ),
                discord.SelectOption(
                    label="DM only",
                    value="dm",
                    default=(current_notifications == "dm"),
                ),
                discord.SelectOption(
                    label="Both",
                    value="both",
                    default=(current_notifications == "both"),
                ),
            ],
            row=1,
        )
        notif_select.callback = self._make_notifications_cb()  # type: ignore[assignment]
        self.add_item(notif_select)

        commit_btn: discord.ui.Button[discord.ui.View] = discord.ui.Button(
            label="Public commitment: ON" if current_public else "Public commitment: OFF",
            style=discord.ButtonStyle.success if current_public else discord.ButtonStyle.secondary,
            row=2,
        )
        commit_btn.callback = self._make_commit_cb(current_public)  # type: ignore[assignment]
        self.add_item(commit_btn)

    def _check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self._invoker_id

    def _make_enforcement_cb(self):
        async def cb(interaction: discord.Interaction) -> None:
            if not self._check(interaction):
                await interaction.response.defer()
                return
            value = interaction.data["values"][0]  # type: ignore[index,typeddict-item]
            with self._ctx.open_db() as conn:
                update_user_settings(
                    conn, interaction.guild_id or 0, interaction.user.id,
                    enforcement_level=value,
                )
            await interaction.response.send_message(
                f"✅ Enforcement set to **{ENFORCEMENT_LABELS[value]}**.", ephemeral=True
            )
        return cb

    def _make_notifications_cb(self):
        async def cb(interaction: discord.Interaction) -> None:
            if not self._check(interaction):
                await interaction.response.defer()
                return
            value = interaction.data["values"][0]  # type: ignore[index,typeddict-item]
            with self._ctx.open_db() as conn:
                update_user_settings(
                    conn, interaction.guild_id or 0, interaction.user.id,
                    notifications_pref=value,
                )
            await interaction.response.send_message(
                f"✅ Notifications set to **{value}**.", ephemeral=True
            )
        return cb

    def _make_commit_cb(self, current: bool):
        async def cb(interaction: discord.Interaction) -> None:
            if not self._check(interaction):
                await interaction.response.defer()
                return
            new_value = not current
            with self._ctx.open_db() as conn:
                update_user_settings(
                    conn, interaction.guild_id or 0, interaction.user.id,
                    public_commitment=new_value,
                )
            await interaction.response.send_message(
                "✅ You're now on the **Active in Commitment** list."
                if new_value
                else "✅ You've been removed from the **Active in Commitment** list.",
                ephemeral=True,
            )
        return cb


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


def _require_active_user(ctx: AppContext, interaction: discord.Interaction):
    if interaction.guild_id is None:
        return None
    with ctx.open_db() as conn:
        user = get_wellness_user(conn, interaction.guild_id, interaction.user.id)
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

    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    async def cog_load(self) -> None:
        bot = self.bot
        db_path = self.ctx.db_path
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
        ctx = self.ctx
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        with ctx.open_db() as conn:
            cfg = get_wellness_config(conn, guild.id)
        if cfg is None or not cfg.role_id:
            await interaction.response.send_message(
                "⚠️ Wellness Guardian isn't set up on this server yet. "
                "Ask an admin to run `/wellness-admin setup`.",
                ephemeral=True,
            )
            return

        view = _SetupWizardView(ctx, interaction.user.id)
        await interaction.response.send_message(
            embed=view._build_step1_embed(), view=view, ephemeral=True
        )

    # ── /wellness away ────────────────────────────────────────────────────

    @away.command(name="on", description="Turn on your away auto-reply.")
    @app_commands.describe(
        message=f"Optional new away message (max {AWAY_MESSAGE_MAX} chars).",
    )
    async def away_on_cmd(
        self, interaction: discord.Interaction, message: str | None = None
    ) -> None:
        ctx = self.ctx
        guild = interaction.guild
        if guild is None:
            return
        user = _require_active_user(ctx, interaction)
        if user is None:
            await interaction.response.send_message(
                "You haven't opted in yet — run `/wellness setup` first.", ephemeral=True
            )
            return
        if message is not None and len(message) > AWAY_MESSAGE_MAX:
            await interaction.response.send_message(
                f"Away message must be {AWAY_MESSAGE_MAX} characters or fewer.", ephemeral=True
            )
            return
        with ctx.open_db() as conn:
            update_away_message(conn, guild.id, interaction.user.id, enabled=True, message=message)
            updated = get_wellness_user(conn, guild.id, interaction.user.id)
        text = (updated.away_message if updated else "") or AWAY_DEFAULT_TEXT
        embed = _render_away_preview(text, interaction.user)
        embed.set_footer(text="Away mode ON. Use /wellness away off to turn it off.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @away.command(name="off", description="Turn off your away auto-reply.")
    async def away_off_cmd(self, interaction: discord.Interaction) -> None:
        ctx = self.ctx
        guild = interaction.guild
        if guild is None:
            return
        user = _require_active_user(ctx, interaction)
        if user is None:
            await interaction.response.send_message(
                "You haven't opted in yet — run `/wellness setup` first.", ephemeral=True
            )
            return
        with ctx.open_db() as conn:
            update_away_message(conn, guild.id, interaction.user.id, enabled=False)
        await interaction.response.send_message(
            "💚 Away mode is off. Welcome back!", ephemeral=True
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(WellnessCog(bot, bot.ctx))
