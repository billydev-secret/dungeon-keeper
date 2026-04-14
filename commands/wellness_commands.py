"""Wellness Guardian user commands — `/wellness *` group.

Onboarding, settings, pause/resume, caps, blackouts, away, and partner commands.
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from services.wellness_partners import make_partner_request_view
from utils import format_user_for_log
from services.wellness_service import (
    ALL_DAYS_MASK,
    BLACKOUT_TEMPLATES,
    DAY_BIT,
    WEEKDAY_MASK,
    WEEKEND_MASK,
    add_blackout,
    add_cap,
    badge_for_days,
    create_partner_request,
    dissolve_partnership,
    ensure_streak,
    find_blackout_by_name,
    find_cap_by_label,
    get_cap_counter,
    get_partnership,
    get_wellness_config,
    get_wellness_user,
    lift_slow_mode,
    list_blackouts,
    list_caps,
    list_partnerships,
    next_milestone,
    opt_in_user,
    opt_out_user,
    pause_user,
    remove_blackout,
    remove_cap,
    resume_user,
    toggle_blackout,
    update_away_message,
    update_cap_limit,
    update_user_settings,
    user_now,
    window_start_epoch,
)

if TYPE_CHECKING:
    from app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.wellness")

# Common timezone choices for the dropdown.  Users can also set custom via /wellness settings.
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


# Duration parsing for /wellness pause
_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_DURATION_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def parse_duration(text: str) -> int | None:
    """Parse a short duration like '24h', '3d', '30m'. Returns seconds or None."""
    m = _DURATION_RE.match(text)
    if not m:
        return None
    value = int(m.group(1))
    unit = m.group(2).lower()
    if value <= 0:
        return None
    return value * _DURATION_SECONDS[unit]


_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def parse_time_to_minute(text: str) -> int | None:
    """Parse 'HH:MM' (24h) into minute-of-day. Returns None on failure."""
    m = _TIME_RE.match(text)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return hour * 60 + minute


_DAY_NAME_TO_INDEX = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tuesday": 1,
    "tues": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thursday": 3,
    "thur": 3,
    "thurs": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}


def parse_days_mask(text: str) -> int | None:
    """Parse a comma-separated day list or shorthand into a bitmask.

    Examples:
        'all'             → 127
        'weekdays'        → 31
        'weekends'        → 96
        'mon,wed,fri'     → 21
    """
    s = text.strip().lower()
    if s in ("all", "every day", "everyday", "daily"):
        return ALL_DAYS_MASK
    if s in ("weekdays", "weekday"):
        return WEEKDAY_MASK
    if s in ("weekends", "weekend"):
        return WEEKEND_MASK
    mask = 0
    for tok in s.replace(" ", "").split(","):
        if not tok:
            continue
        idx = _DAY_NAME_TO_INDEX.get(tok)
        if idx is None:
            return None
        mask |= DAY_BIT[idx]
    return mask if mask else None


def _format_days_mask(mask: int) -> str:
    if mask == ALL_DAYS_MASK:
        return "every day"
    if mask == WEEKDAY_MASK:
        return "weekdays"
    if mask == WEEKEND_MASK:
        return "weekends"
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return ", ".join(name for i, name in enumerate(names) if mask & DAY_BIT[i])


def _format_minute(m: int) -> str:
    h, mm = divmod(m, 60)
    return f"{h:02d}:{mm:02d}"


# ---------------------------------------------------------------------------
# Setup wizard view
# ---------------------------------------------------------------------------


class _SetupWizardView(discord.ui.View):
    """Two-step wizard: timezone → enforcement level → finish."""

    def __init__(self, ctx: AppContext, invoker_id: int) -> None:
        super().__init__(timeout=300)
        self._ctx = ctx
        self._invoker_id = invoker_id
        self._timezone: str | None = None
        self._enforcement: str | None = None
        self._step: int = 1

        # Step 1 — timezone select
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

        # Step 2 — enforcement select (added after tz pick)
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
        # Don't add yet — only after step 1

    def _check_invoker(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._invoker_id:
            return False
        return True

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
            color=discord.Color.from_str("#7BC97B"),
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
            color=discord.Color.from_str("#7BC97B"),
        )

    def _build_done_embed(self, member: discord.Member) -> discord.Embed:
        embed = discord.Embed(
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
            color=discord.Color.from_str("#7BC97B"),
        )
        return embed

    async def _on_tz_pick(self, interaction: discord.Interaction) -> None:
        if not self._check_invoker(interaction):
            await interaction.response.defer()
            return
        self._timezone = self._tz_select.values[0]
        # Lock the tz select and add enforcement select
        self._tz_select.disabled = True
        self._tz_select.placeholder = f"Timezone: {self._timezone}"
        if self._enf_select not in self.children:
            self.add_item(self._enf_select)
        self._step = 2
        await interaction.response.edit_message(
            embed=self._build_step2_embed(),
            view=self,
        )

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

        # Persist + assign role
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
                content="Could not resolve your member record.",
                embed=None,
                view=None,
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

        # Disable selects
        self._tz_select.disabled = True
        self._enf_select.disabled = True
        self._enf_select.placeholder = (
            f"Enforcement: {ENFORCEMENT_LABELS[self._enforcement]}"
        )

        await interaction.response.edit_message(
            embed=self._build_done_embed(member),
            view=None,
        )
        self.stop()


# ---------------------------------------------------------------------------
# Settings view
# ---------------------------------------------------------------------------


class _SettingsView(discord.ui.View):
    """Lightweight settings editor — enforcement level + notifications + commit toggle."""

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
            label="Public commitment: ON"
            if current_public
            else "Public commitment: OFF",
            style=discord.ButtonStyle.success
            if current_public
            else discord.ButtonStyle.secondary,
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
                    conn,
                    interaction.guild_id or 0,
                    interaction.user.id,
                    enforcement_level=value,
                )
            await interaction.response.send_message(
                f"✅ Enforcement set to **{ENFORCEMENT_LABELS[value]}**.",
                ephemeral=True,
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
                    conn,
                    interaction.guild_id or 0,
                    interaction.user.id,
                    notifications_pref=value,
                )
            await interaction.response.send_message(
                f"✅ Notifications set to **{value}**.",
                ephemeral=True,
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
                    conn,
                    interaction.guild_id or 0,
                    interaction.user.id,
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
# Common helpers used by all wellness commands
# ---------------------------------------------------------------------------


def require_active_user(ctx: AppContext, interaction: discord.Interaction):
    """Return the WellnessUser if the invoker has opted in, else None."""
    if interaction.guild_id is None:
        return None
    with ctx.open_db() as conn:
        user = get_wellness_user(conn, interaction.guild_id, interaction.user.id)
    if user is None or not user.is_active:
        return None
    return user


# ---------------------------------------------------------------------------
# Command registration
# ---------------------------------------------------------------------------


def register_wellness_commands(bot: Bot, ctx: AppContext) -> None:
    wellness_group = app_commands.Group(
        name="wellness",
        description="Set message caps, blackout hours, and healthy boundaries for yourself.",
    )

    # ── /wellness setup ───────────────────────────────────────────────────

    @wellness_group.command(
        name="setup", description="Opt in — pick your timezone and enforcement style."
    )
    async def setup_cmd(interaction: discord.Interaction) -> None:
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
            embed=view._build_step1_embed(),
            view=view,
            ephemeral=True,
        )

    # ── /wellness optout ──────────────────────────────────────────────────

    @wellness_group.command(
        name="optout",
        description="Leave Wellness Guardian. Your settings are kept for 30 days.",
    )
    async def optout_cmd(interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        user = require_active_user(ctx, interaction)
        if user is None:
            await interaction.response.send_message(
                "You're not currently opted in to Wellness Guardian.",
                ephemeral=True,
            )
            return

        with ctx.open_db() as conn:
            cfg = get_wellness_config(conn, guild.id)
            opt_out_user(conn, guild.id, interaction.user.id)
            lift_slow_mode(conn, guild.id, interaction.user.id)

        # Strip the wellness role
        member = guild.get_member(interaction.user.id)
        role = guild.get_role(cfg.role_id) if cfg and cfg.role_id else None
        if member and role and role in member.roles:
            try:
                await member.remove_roles(role, reason="Wellness Guardian opt-out")
            except discord.Forbidden:
                pass

        await interaction.response.send_message(
            "💚 You've been opted out. Your settings will be kept for 30 days "
            "in case you change your mind — just run `/wellness setup` again.",
            ephemeral=True,
        )

    # ── /wellness settings ────────────────────────────────────────────────

    @wellness_group.command(
        name="settings",
        description="Change your enforcement level, notification style, or public visibility.",
    )
    async def settings_cmd(interaction: discord.Interaction) -> None:
        user = require_active_user(ctx, interaction)
        if user is None:
            await interaction.response.send_message(
                "You haven't opted in yet — run `/wellness setup` first.",
                ephemeral=True,
            )
            return

        view = _SettingsView(
            ctx,
            interaction.user.id,
            user.enforcement_level,
            user.notifications_pref,
            user.public_commitment,
        )
        embed = discord.Embed(
            title="🌿 Your Wellness Settings",
            description=(
                f"**Timezone:** `{user.timezone}`\n"
                f"**Enforcement:** {ENFORCEMENT_LABELS.get(user.enforcement_level, user.enforcement_level)}\n"
                f"**Notifications:** {user.notifications_pref}\n"
                f"**Slow-mode rate:** 1 message / {user.slow_mode_rate_seconds}s\n"
                f"**Public commitment:** {'on' if user.public_commitment else 'off'}\n"
                f"**Daily reset hour:** {user.daily_reset_hour:02d}:00\n\n"
                "Use the controls below to adjust. Visual editors live in the web panel."
            ),
            color=discord.Color.from_str("#7BC97B"),
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ── /wellness pause ───────────────────────────────────────────────────

    @wellness_group.command(
        name="pause",
        description="Take a break from wellness tracking. Any active slow mode is lifted.",
    )
    @app_commands.describe(
        duration="How long to pause (e.g. 24h, 3d, 30m). Leave blank for indefinite."
    )
    async def pause_cmd(
        interaction: discord.Interaction,
        duration: str | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        user = require_active_user(ctx, interaction)
        if user is None:
            await interaction.response.send_message(
                "You haven't opted in yet — run `/wellness setup` first.",
                ephemeral=True,
            )
            return

        if duration:
            seconds = parse_duration(duration)
            if seconds is None:
                await interaction.response.send_message(
                    "Couldn't parse that duration. Try `24h`, `3d`, or `30m`.",
                    ephemeral=True,
                )
                return
            until = time.time() + seconds
        else:
            # Pause for ~99 years if no duration given (effectively indefinite)
            until = time.time() + 99 * 365 * 86400

        with ctx.open_db() as conn:
            pause_user(conn, guild.id, interaction.user.id, until)

        if duration:
            await interaction.response.send_message(
                f"⏸️ Tracking paused for **{duration}**. Slow mode lifted. Use `/wellness resume` anytime.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "⏸️ Tracking paused indefinitely. Use `/wellness resume` when you're ready.",
                ephemeral=True,
            )

    # ── /wellness resume ──────────────────────────────────────────────────

    @wellness_group.command(
        name="resume", description="Pick up where you left off after a pause."
    )
    async def resume_cmd(interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        user = require_active_user(ctx, interaction)
        if user is None:
            await interaction.response.send_message(
                "You haven't opted in yet — run `/wellness setup` first.",
                ephemeral=True,
            )
            return
        with ctx.open_db() as conn:
            resume_user(conn, guild.id, interaction.user.id)
        await interaction.response.send_message(
            "▶️ Tracking resumed. Welcome back. 💚",
            ephemeral=True,
        )

    # ── /wellness cap subgroup ────────────────────────────────────────────

    cap_group = app_commands.Group(
        name="cap",
        description="Set limits on how many messages you send per window.",
        parent=wellness_group,
    )

    async def _user_cap_label_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []
        with ctx.open_db() as conn:
            caps = list_caps(conn, interaction.guild_id, interaction.user.id)
        choices: list[app_commands.Choice[str]] = []
        for c in caps:
            if not current or current.lower() in c.label.lower():
                choices.append(app_commands.Choice(name=c.label[:100], value=c.label))
        return choices[:25]

    @cap_group.command(name="add", description="Create a new message cap.")
    @app_commands.describe(
        label="Friendly name for this cap (e.g. 'daily chatter').",
        scope="Where this cap applies.",
        window="How often the counter resets.",
        limit="Max messages per window.",
        channel="Required if scope is 'channel'.",
        category="Required if scope is 'category'.",
        exclude_exempt="If true (default), exempt channels don't count.",
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="Global (all messages)", value="global"),
            app_commands.Choice(name="Single channel", value="channel"),
            app_commands.Choice(name="Category", value="category"),
            app_commands.Choice(name="Voice (coming soon)", value="voice"),
        ]
    )
    @app_commands.choices(
        window=[
            app_commands.Choice(name="Hourly", value="hourly"),
            app_commands.Choice(name="Daily", value="daily"),
            app_commands.Choice(name="Weekly", value="weekly"),
        ]
    )
    async def cap_add_cmd(
        interaction: discord.Interaction,
        label: str,
        scope: app_commands.Choice[str],
        window: app_commands.Choice[str],
        limit: app_commands.Range[int, 1, 100000],
        channel: discord.TextChannel | None = None,
        category: discord.CategoryChannel | None = None,
        exclude_exempt: bool = True,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        user = require_active_user(ctx, interaction)
        if user is None:
            await interaction.response.send_message(
                "You haven't opted in yet — run `/wellness setup` first.",
                ephemeral=True,
            )
            return

        if scope.value == "voice":
            await interaction.response.send_message(
                "🎙️ Voice caps are coming in v2 — stay tuned!",
                ephemeral=True,
            )
            return

        scope_target_id = 0
        if scope.value == "channel":
            if channel is None:
                await interaction.response.send_message(
                    "Please pick a `channel` when scope is `channel`.",
                    ephemeral=True,
                )
                return
            scope_target_id = channel.id
        elif scope.value == "category":
            if category is None:
                await interaction.response.send_message(
                    "Please pick a `category` when scope is `category`.",
                    ephemeral=True,
                )
                return
            scope_target_id = category.id

        with ctx.open_db() as conn:
            existing = find_cap_by_label(conn, guild.id, interaction.user.id, label)
            if existing:
                await interaction.response.send_message(
                    f"A cap named **{label}** already exists. Pick a different name or use `/wellness cap edit`.",
                    ephemeral=True,
                )
                return
            add_cap(
                conn,
                guild.id,
                interaction.user.id,
                label=label,
                scope=scope.value,
                scope_target_id=scope_target_id,
                window=window.value,
                cap_limit=int(limit),
                exclude_exempt=exclude_exempt,
            )

        scope_text = scope.name
        if scope.value == "channel" and channel:
            scope_text = f"{channel.mention}"
        elif scope.value == "category" and category:
            scope_text = f"category **{category.name}**"

        await interaction.response.send_message(
            f"💚 Cap **{label}** created — {limit} messages / {window.value} ({scope_text}).",
            ephemeral=True,
        )

    @cap_group.command(
        name="list", description="Show all your active caps with current counts."
    )
    async def cap_list_cmd(interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        user = require_active_user(ctx, interaction)
        if user is None:
            await interaction.response.send_message(
                "You haven't opted in yet — run `/wellness setup` first.",
                ephemeral=True,
            )
            return

        with ctx.open_db() as conn:
            caps = list_caps(conn, guild.id, interaction.user.id)

        if not caps:
            await interaction.response.send_message(
                "You have no caps set. Add one with `/wellness cap add`.",
                ephemeral=True,
            )
            return

        now_local = user_now(user.timezone)
        lines: list[str] = []
        with ctx.open_db() as conn:
            for c in caps:
                window_start = window_start_epoch(
                    c.window, now_local, user.daily_reset_hour
                )
                count = get_cap_counter(conn, c.id, window_start)
                bar_full = min(c.cap_limit, count)
                bar = "█" * (bar_full * 10 // c.cap_limit if c.cap_limit > 0 else 0)
                bar = bar.ljust(10, "░")
                scope_text = c.scope
                if c.scope == "channel":
                    ch = guild.get_channel(c.scope_target_id)
                    scope_text = f"channel {ch.mention if ch else c.scope_target_id}"
                elif c.scope == "category":
                    cat = guild.get_channel(c.scope_target_id)
                    scope_text = f"category {cat.name if cat else c.scope_target_id}"
                lines.append(
                    f"**{c.label}** — `{bar}` {count}/{c.cap_limit} *{c.window}* ({scope_text})"
                )

        embed = discord.Embed(
            title="🌿 Your Caps",
            description="\n".join(lines),
            color=discord.Color.from_str("#7BC97B"),
        )
        embed.set_footer(text=f"Counts reset based on your {user.timezone} timezone")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @cap_group.command(name="edit", description="Change the limit on an existing cap.")
    @app_commands.describe(label="Cap to edit.", new_limit="New message limit.")
    @app_commands.autocomplete(label=_user_cap_label_autocomplete)
    async def cap_edit_cmd(
        interaction: discord.Interaction,
        label: str,
        new_limit: app_commands.Range[int, 1, 100000],
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        user = require_active_user(ctx, interaction)
        if user is None:
            await interaction.response.send_message(
                "You haven't opted in yet — run `/wellness setup` first.",
                ephemeral=True,
            )
            return
        with ctx.open_db() as conn:
            cap = find_cap_by_label(conn, guild.id, interaction.user.id, label)
            if not cap:
                await interaction.response.send_message(
                    f"No cap named **{label}**.", ephemeral=True
                )
                return
            update_cap_limit(conn, cap.id, int(new_limit))
        await interaction.response.send_message(
            f"💚 Cap **{label}** updated to **{new_limit}** per {cap.window}.",
            ephemeral=True,
        )

    @cap_group.command(name="remove", description="Delete a cap.")
    @app_commands.describe(label="Cap to remove.")
    @app_commands.autocomplete(label=_user_cap_label_autocomplete)
    async def cap_remove_cmd(
        interaction: discord.Interaction,
        label: str,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        user = require_active_user(ctx, interaction)
        if user is None:
            await interaction.response.send_message(
                "You haven't opted in yet — run `/wellness setup` first.",
                ephemeral=True,
            )
            return
        with ctx.open_db() as conn:
            cap = find_cap_by_label(conn, guild.id, interaction.user.id, label)
            if not cap:
                await interaction.response.send_message(
                    f"No cap named **{label}**.", ephemeral=True
                )
                return
            remove_cap(conn, cap.id)
        await interaction.response.send_message(
            f"🗑️ Cap **{label}** removed.", ephemeral=True
        )

    # ── /wellness blackout subgroup ───────────────────────────────────────

    blackout_group = app_commands.Group(
        name="blackout",
        description="Schedule times when you want to stay offline.",
        parent=wellness_group,
    )

    async def _user_blackout_name_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []
        with ctx.open_db() as conn:
            blackouts = list_blackouts(conn, interaction.guild_id, interaction.user.id)
        out: list[app_commands.Choice[str]] = []
        for b in blackouts:
            if not current or current.lower() in b.name.lower():
                out.append(app_commands.Choice(name=b.name[:100], value=b.name))
        return out[:25]

    @blackout_group.command(
        name="add", description="Schedule a custom blackout window."
    )
    @app_commands.describe(
        name="Friendly name for this blackout (e.g. 'sleep', 'focus').",
        start_time="Start in 24h HH:MM (your local time).",
        end_time="End in 24h HH:MM (your local time). May wrap past midnight.",
        days="Days: 'all', 'weekdays', 'weekends', or comma list (mon,wed,fri).",
    )
    async def blackout_add_cmd(
        interaction: discord.Interaction,
        name: str,
        start_time: str,
        end_time: str,
        days: str = "all",
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        user = require_active_user(ctx, interaction)
        if user is None:
            await interaction.response.send_message(
                "You haven't opted in yet — run `/wellness setup` first.",
                ephemeral=True,
            )
            return
        start_min = parse_time_to_minute(start_time)
        end_min = parse_time_to_minute(end_time)
        if start_min is None or end_min is None:
            await interaction.response.send_message(
                "Times must be `HH:MM` in 24-hour format (e.g. `23:00`, `07:00`).",
                ephemeral=True,
            )
            return
        if start_min == end_min:
            await interaction.response.send_message(
                "Start and end can't be identical — try a different end time.",
                ephemeral=True,
            )
            return
        days_mask = parse_days_mask(days)
        if days_mask is None:
            await interaction.response.send_message(
                "Days must be `all`, `weekdays`, `weekends`, or a comma list like `mon,wed,fri`.",
                ephemeral=True,
            )
            return
        with ctx.open_db() as conn:
            existing = find_blackout_by_name(conn, guild.id, interaction.user.id, name)
            if existing:
                await interaction.response.send_message(
                    f"You already have a blackout named **{name}**.",
                    ephemeral=True,
                )
                return
            add_blackout(
                conn,
                guild.id,
                interaction.user.id,
                name=name,
                start_minute=start_min,
                end_minute=end_min,
                days_mask=days_mask,
            )
        wrap_note = " (wraps past midnight)" if start_min > end_min else ""
        await interaction.response.send_message(
            f"🌙 Blackout **{name}** scheduled — "
            f"{_format_minute(start_min)}–{_format_minute(end_min)}{wrap_note}, "
            f"{_format_days_mask(days_mask)}.",
            ephemeral=True,
        )

    @blackout_group.command(
        name="template", description="Apply a preset blackout template."
    )
    @app_commands.describe(template="Pick a preset.")
    @app_commands.choices(
        template=[
            app_commands.Choice(
                name="Night Owl (23:00–07:00, every day)", value="night_owl"
            ),
            app_commands.Choice(
                name="Work Hours (09:00–17:00, weekdays)", value="work_hours"
            ),
            app_commands.Choice(
                name="School Hours (08:00–15:00, weekdays)", value="school_hours"
            ),
            app_commands.Choice(
                name="Weekend Detox (all day Sat–Sun)", value="weekend_detox"
            ),
        ]
    )
    async def blackout_template_cmd(
        interaction: discord.Interaction,
        template: app_commands.Choice[str],
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        user = require_active_user(ctx, interaction)
        if user is None:
            await interaction.response.send_message(
                "You haven't opted in yet — run `/wellness setup` first.",
                ephemeral=True,
            )
            return
        tpl = BLACKOUT_TEMPLATES.get(template.value)
        if tpl is None:
            await interaction.response.send_message("Unknown template.", ephemeral=True)
            return
        with ctx.open_db() as conn:
            existing = find_blackout_by_name(
                conn, guild.id, interaction.user.id, tpl["name"]
            )
            if existing:
                await interaction.response.send_message(
                    f"You already have a blackout named **{tpl['name']}**.",
                    ephemeral=True,
                )
                return
            add_blackout(
                conn,
                guild.id,
                interaction.user.id,
                name=tpl["name"],
                start_minute=tpl["start_minute"],
                end_minute=tpl["end_minute"],
                days_mask=tpl["days_mask"],
            )
        await interaction.response.send_message(
            f"🌙 Applied **{tpl['name']}** — "
            f"{_format_minute(tpl['start_minute'])}–{_format_minute(tpl['end_minute'])}, "
            f"{_format_days_mask(tpl['days_mask'])}.",
            ephemeral=True,
        )

    @blackout_group.command(name="list", description="Show your blackouts.")
    async def blackout_list_cmd(interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        user = require_active_user(ctx, interaction)
        if user is None:
            await interaction.response.send_message(
                "You haven't opted in yet — run `/wellness setup` first.",
                ephemeral=True,
            )
            return
        with ctx.open_db() as conn:
            blackouts = list_blackouts(conn, guild.id, interaction.user.id)
        if not blackouts:
            await interaction.response.send_message(
                "You have no blackouts. Try `/wellness blackout template` for a quick start.",
                ephemeral=True,
            )
            return
        lines: list[str] = []
        for b in blackouts:
            status = "✅" if b.enabled else "⏸️"
            wrap = " (wraps past midnight)" if b.start_minute > b.end_minute else ""
            lines.append(
                f"{status} **{b.name}** — {_format_minute(b.start_minute)}–"
                f"{_format_minute(b.end_minute)}{wrap}, {_format_days_mask(b.days_mask)}"
            )
        embed = discord.Embed(
            title="🌙 Your Blackouts",
            description="\n".join(lines),
            color=discord.Color.from_str("#7BC97B"),
        )
        embed.set_footer(text=f"Times shown in your {user.timezone} timezone")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @blackout_group.command(name="toggle", description="Enable or disable a blackout.")
    @app_commands.describe(
        name="Blackout name.", enabled="Set to true to enable, false to pause."
    )
    @app_commands.autocomplete(name=_user_blackout_name_autocomplete)
    async def blackout_toggle_cmd(
        interaction: discord.Interaction,
        name: str,
        enabled: bool,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        user = require_active_user(ctx, interaction)
        if user is None:
            await interaction.response.send_message(
                "You haven't opted in yet — run `/wellness setup` first.",
                ephemeral=True,
            )
            return
        with ctx.open_db() as conn:
            blackout = find_blackout_by_name(conn, guild.id, interaction.user.id, name)
            if not blackout:
                await interaction.response.send_message(
                    f"No blackout named **{name}**.", ephemeral=True
                )
                return
            toggle_blackout(conn, blackout.id, enabled)
        state = "enabled" if enabled else "paused"
        await interaction.response.send_message(
            f"🌙 Blackout **{name}** is now **{state}**.",
            ephemeral=True,
        )

    @blackout_group.command(name="remove", description="Delete a blackout.")
    @app_commands.describe(name="Blackout to remove.")
    @app_commands.autocomplete(name=_user_blackout_name_autocomplete)
    async def blackout_remove_cmd(
        interaction: discord.Interaction,
        name: str,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        user = require_active_user(ctx, interaction)
        if user is None:
            await interaction.response.send_message(
                "You haven't opted in yet — run `/wellness setup` first.",
                ephemeral=True,
            )
            return
        with ctx.open_db() as conn:
            blackout = find_blackout_by_name(conn, guild.id, interaction.user.id, name)
            if not blackout:
                await interaction.response.send_message(
                    f"No blackout named **{name}**.", ephemeral=True
                )
                return
            remove_blackout(conn, blackout.id)
        await interaction.response.send_message(
            f"🗑️ Blackout **{name}** removed.", ephemeral=True
        )

    # ── /wellness away subgroup ───────────────────────────────────────────

    away_group = app_commands.Group(
        name="away",
        description="Auto-reply when someone mentions you while you're away.",
        parent=wellness_group,
    )

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
            color=discord.Color.from_str("#7BC97B"),
        )

    @away_group.command(name="on", description="Turn on your away auto-reply.")
    @app_commands.describe(
        message=f"Optional new away message (max {AWAY_MESSAGE_MAX} chars).",
    )
    async def away_on_cmd(
        interaction: discord.Interaction,
        message: str | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        user = require_active_user(ctx, interaction)
        if user is None:
            await interaction.response.send_message(
                "You haven't opted in yet — run `/wellness setup` first.",
                ephemeral=True,
            )
            return
        if message is not None and len(message) > AWAY_MESSAGE_MAX:
            await interaction.response.send_message(
                f"Away message must be {AWAY_MESSAGE_MAX} characters or fewer.",
                ephemeral=True,
            )
            return
        with ctx.open_db() as conn:
            update_away_message(
                conn, guild.id, interaction.user.id, enabled=True, message=message
            )
            updated = get_wellness_user(conn, guild.id, interaction.user.id)
        text = (updated.away_message if updated else "") or AWAY_DEFAULT_TEXT
        embed = _render_away_preview(text, interaction.user)
        embed.set_footer(text="Away mode ON. Use /wellness away off to turn it off.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @away_group.command(name="off", description="Turn off your away auto-reply.")
    async def away_off_cmd(interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        user = require_active_user(ctx, interaction)
        if user is None:
            await interaction.response.send_message(
                "You haven't opted in yet — run `/wellness setup` first.",
                ephemeral=True,
            )
            return
        with ctx.open_db() as conn:
            update_away_message(conn, guild.id, interaction.user.id, enabled=False)
        await interaction.response.send_message(
            "💚 Away mode is off. Welcome back!", ephemeral=True
        )

    @away_group.command(
        name="set", description="Update your away message text without toggling state."
    )
    @app_commands.describe(
        message=f"New away message (max {AWAY_MESSAGE_MAX} chars).",
    )
    async def away_set_cmd(
        interaction: discord.Interaction,
        message: str,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        user = require_active_user(ctx, interaction)
        if user is None:
            await interaction.response.send_message(
                "You haven't opted in yet — run `/wellness setup` first.",
                ephemeral=True,
            )
            return
        if len(message) > AWAY_MESSAGE_MAX:
            await interaction.response.send_message(
                f"Away message must be {AWAY_MESSAGE_MAX} characters or fewer.",
                ephemeral=True,
            )
            return
        with ctx.open_db() as conn:
            update_away_message(
                conn,
                guild.id,
                interaction.user.id,
                enabled=user.away_enabled,
                message=message,
            )
        embed = _render_away_preview(message, interaction.user)
        state = "ON" if user.away_enabled else "OFF"
        embed.set_footer(text=f"Saved. Away mode is currently {state}.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @away_group.command(
        name="preview",
        description="Preview what others will see when they mention you.",
    )
    async def away_preview_cmd(interaction: discord.Interaction) -> None:
        user = require_active_user(ctx, interaction)
        if user is None:
            await interaction.response.send_message(
                "You haven't opted in yet — run `/wellness setup` first.",
                ephemeral=True,
            )
            return
        text = user.away_message or AWAY_DEFAULT_TEXT
        embed = _render_away_preview(text, interaction.user)
        state = "ON" if user.away_enabled else "OFF"
        embed.set_footer(text=f"Away mode is currently {state}.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /wellness partner subgroup ────────────────────────────────────────

    partner_group = app_commands.Group(
        name="partner",
        description="Pair up with a friend for mutual accountability.",
        parent=wellness_group,
    )

    @partner_group.command(
        name="request",
        description="Send a partnership request to another opted-in member.",
    )
    @app_commands.describe(member="The person you'd like to be partners with.")
    async def partner_request_cmd(
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        if member.bot:
            await interaction.response.send_message(
                "Bots can't be partners. 💚", ephemeral=True
            )
            return
        if member.id == interaction.user.id:
            await interaction.response.send_message(
                "You can't partner with yourself.", ephemeral=True
            )
            return
        user = require_active_user(ctx, interaction)
        if user is None:
            await interaction.response.send_message(
                "You haven't opted in yet — run `/wellness setup` first.",
                ephemeral=True,
            )
            return
        with ctx.open_db() as conn:
            target = get_wellness_user(conn, guild.id, member.id)
            if target is None or not target.is_active:
                await interaction.response.send_message(
                    f"{member.display_name} hasn't opted in to Wellness Guardian.",
                    ephemeral=True,
                )
                return
            partnership = create_partner_request(
                conn, guild.id, interaction.user.id, member.id
            )
        if partnership is None:
            await interaction.response.send_message(
                f"There's already a request or partnership with {member.display_name}.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="💚 Accountability partner request",
            description=(
                f"**{interaction.user.display_name}** would like to be your wellness "
                "accountability partner.\n\n"
                "Partners can encourage each other, share goals, and celebrate "
                "milestones together. You can dissolve a partnership any time with "
                "`/wellness partner dissolve`."
            ),
            color=discord.Color.from_str("#7BC97B"),
        )
        view = make_partner_request_view(partnership.id)
        try:
            await member.send(embed=embed, view=view)
        except (discord.Forbidden, discord.HTTPException):
            with ctx.open_db() as conn:
                dissolve_partnership(conn, partnership.id)
            await interaction.response.send_message(
                f"Couldn't DM {member.display_name} — they may have DMs closed. "
                "Ask them to enable DMs from server members and try again.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"💚 Request sent to **{member.display_name}**. They'll see it in their DMs.",
            ephemeral=True,
        )

    @partner_group.command(
        name="list", description="Show your accountability partners."
    )
    async def partner_list_cmd(interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        user = require_active_user(ctx, interaction)
        if user is None:
            await interaction.response.send_message(
                "You haven't opted in yet — run `/wellness setup` first.",
                ephemeral=True,
            )
            return
        with ctx.open_db() as conn:
            partnerships = list_partnerships(
                conn,
                guild.id,
                interaction.user.id,
                accepted_only=False,
            )
        if not partnerships:
            await interaction.response.send_message(
                "No partnerships yet. Use `/wellness partner request @member` to send one.",
                ephemeral=True,
            )
            return

        accepted_lines: list[str] = []
        pending_lines: list[str] = []
        for p in partnerships:
            other_id = p.other(interaction.user.id)
            other = guild.get_member(other_id)
            name = other.display_name if other else f"User {other_id}"
            if p.status == "accepted":
                accepted_lines.append(f"• **{name}**")
            else:
                if p.requester_id == interaction.user.id:
                    pending_lines.append(f"• {name} *(awaiting their response)*")
                else:
                    pending_lines.append(f"• {name} *(awaiting your response)*")

        embed = discord.Embed(
            title="💚 Your accountability partners",
            color=discord.Color.from_str("#7BC97B"),
        )
        if accepted_lines:
            embed.add_field(
                name="Active partnerships",
                value="\n".join(accepted_lines),
                inline=False,
            )
        if pending_lines:
            embed.add_field(
                name="Pending requests",
                value="\n".join(pending_lines),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _partner_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild_id is None or interaction.guild is None:
            return []
        with ctx.open_db() as conn:
            partnerships = list_partnerships(
                conn,
                interaction.guild_id,
                interaction.user.id,
                accepted_only=False,
            )
        choices: list[app_commands.Choice[str]] = []
        for p in partnerships:
            other_id = p.other(interaction.user.id)
            other = interaction.guild.get_member(other_id)
            name = other.display_name if other else f"User {other_id}"
            if not current or current.lower() in name.lower():
                choices.append(app_commands.Choice(name=name[:100], value=str(p.id)))
        return choices[:25]

    @partner_group.command(
        name="dissolve", description="End a partnership or cancel a pending request."
    )
    @app_commands.describe(partner="The partnership to end.")
    @app_commands.autocomplete(partner=_partner_autocomplete)
    async def partner_dissolve_cmd(
        interaction: discord.Interaction,
        partner: str,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        try:
            partner_id = int(partner)
        except ValueError:
            await interaction.response.send_message(
                "Pick a partnership from the autocomplete suggestions.",
                ephemeral=True,
            )
            return
        with ctx.open_db() as conn:
            partnership = get_partnership(conn, partner_id)
            if partnership is None or partnership.guild_id != guild.id:
                await interaction.response.send_message(
                    "That partnership doesn't exist.", ephemeral=True
                )
                return
            if interaction.user.id not in (partnership.user_a, partnership.user_b):
                await interaction.response.send_message(
                    "That partnership isn't yours to dissolve.", ephemeral=True
                )
                return
            other_id = partnership.other(interaction.user.id)
            dissolve_partnership(conn, partner_id)

        # Best-effort notification to the other partner
        other = guild.get_member(other_id)
        if other is not None:
            try:
                await other.send(
                    f"💚 **{interaction.user.display_name}** has ended your wellness "
                    "partnership. No hard feelings — sometimes it's just the right call."
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

        await interaction.response.send_message(
            "💚 Partnership ended. Wishing you both well.", ephemeral=True
        )

    # ── /wellness score ───────────────────────────────────────────────────

    @wellness_group.command(
        name="score",
        description="Show your current wellness streak, badge, and next milestone.",
    )
    async def score_cmd(interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        user = require_active_user(ctx, interaction)
        if user is None:
            await interaction.response.send_message(
                "You haven't opted in yet — run `/wellness setup` first.",
                ephemeral=True,
            )
            return
        now_local = user_now(user.timezone)
        today_iso = now_local.date().isoformat()
        with ctx.open_db() as conn:
            streak = ensure_streak(conn, guild.id, interaction.user.id, today_iso)

        badge = streak.current_badge or badge_for_days(streak.current_days)
        nxt = next_milestone(streak.current_days)

        day_word = "day" if streak.current_days == 1 else "days"
        lines = [
            f"{badge} **{streak.current_days} {day_word}**",
            "",
            f"*Personal best:* **{streak.personal_best} days**",
        ]
        if nxt is not None:
            remain = nxt[0] - streak.current_days
            lines.append(
                f"*Next milestone:* {nxt[1]} **{nxt[0]} days** (`{remain}` to go)"
            )
        else:
            lines.append("*You're at the top milestone tier.* 👑")

        if streak.last_violation_date == today_iso:
            lines.append("")
            lines.append(
                "*You had a slip today — that's okay. Streak decay is gentle (10%).*"
            )

        embed = discord.Embed(
            title="💚 Your Wellness Streak",
            description="\n".join(lines),
            color=discord.Color.from_str("#7BC97B"),
        )
        embed.set_footer(
            text="Streaks decay by ~10% on slips — they never reset to zero."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    bot.tree.add_command(wellness_group)
