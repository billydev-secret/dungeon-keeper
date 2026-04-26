"""Wellness Guardian admin commands — `/wellness-admin *` group."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from services.embeds import WELLNESS_OVERVIEW, WELLNESS_PRIMARY
from commands.wellness_commands import (
    _format_days_mask,
    _format_minute,
    parse_days_mask,
    parse_time_to_minute,
)
from services.wellness_service import (
    ENFORCEMENT_LEVELS,
    NOTIFICATION_PREFS,
    add_blackout,
    add_cap,
    add_exempt_channel,
    find_blackout_by_name,
    find_cap_by_label,
    get_wellness_config,
    get_wellness_user,
    list_active_users,
    list_blackouts,
    list_caps,
    list_exempt_channels,
    remove_blackout,
    remove_cap,
    remove_exempt_channel,
    update_cap_limit,
    update_user_settings,
    upsert_wellness_config,
)

if TYPE_CHECKING:
    from app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.wellness.admin")

WELLNESS_ROLE_NAME = "Wellness Guardian"
WELLNESS_CHANNEL_NAME = "wellness"

CRISIS_RESOURCES_DEFAULT = (
    "If you're in crisis, please reach out: "
    "988 Suicide & Crisis Lifeline (US, call/text 988) — "
    "Crisis Text Line (text HOME to 741741) — "
    "https://findahelpline.com (international)."
)


def _check_admin(interaction: discord.Interaction) -> bool:
    member = interaction.user
    if not isinstance(member, discord.Member):
        return False
    return (
        member.guild_permissions.manage_guild or member.guild_permissions.administrator
    )


async def _create_wellness_channel(
    guild: discord.Guild,
    role: discord.Role,
    *,
    channel_name: str = WELLNESS_CHANNEL_NAME,
    crisis_resource_url: str = "",
) -> discord.TextChannel:
    overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        role: discord.PermissionOverwrite(
            view_channel=True,
            read_message_history=True,
            send_messages=True,
        ),
    }
    if guild.me:
        overwrites[guild.me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_messages=True,
            read_message_history=True,
            embed_links=True,
            attach_files=True,
        )

    topic_parts: list[str] = [
        "Wellness Guardian — tips, encouragement, check-ins, and accountability.",
    ]
    if crisis_resource_url:
        topic_parts.append(f"Crisis resources: {crisis_resource_url}")
    else:
        topic_parts.append(CRISIS_RESOURCES_DEFAULT)
    topic = "  •  ".join(topic_parts)[:1024]

    return await guild.create_text_channel(
        channel_name,
        overwrites=overwrites,  # type: ignore[arg-type]
        topic=topic,
    )


def _flatten_options(options: list) -> list:
    out: list = []
    for opt in options:
        if isinstance(opt, dict):
            if "options" in opt and isinstance(opt["options"], list):
                out.extend(_flatten_options(opt["options"]))
            else:
                out.append(opt)
    return out


class WellnessAdminCog(commands.Cog):
    wellness_admin = app_commands.Group(
        name="wellness-admin",
        description="Server-wide wellness setup, defaults, and admin overrides.",
        default_permissions=discord.Permissions(manage_guild=True),
    )
    exempt = app_commands.Group(
        name="exempt",
        description="Channels where wellness caps don't count messages.",
        parent=wellness_admin,
    )
    cap = app_commands.Group(
        name="cap",
        description="Create, edit, or remove message caps for a user.",
        parent=wellness_admin,
    )
    blackout = app_commands.Group(
        name="blackout",
        description="Create or remove blackout schedules for a user.",
        parent=wellness_admin,
    )

    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    # ── /wellness-admin setup ─────────────────────────────────────────────

    @wellness_admin.command(
        name="setup",
        description="Create the wellness role and channel for your server.",
    )
    @app_commands.describe(
        role_name="Name for the wellness role.",
        channel_name="Name for the wellness channel.",
        crisis_resource_url="Crisis resource link or text shown in the channel topic.",
    )
    async def setup_cmd(
        self,
        interaction: discord.Interaction,
        role_name: str | None = None,
        channel_name: str | None = None,
        crisis_resource_url: str | None = None,
    ) -> None:
        ctx = self.ctx
        if not _check_admin(interaction):
            await interaction.response.send_message(
                "❌ You need Manage Server to use this command.", ephemeral=True
            )
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            await interaction.followup.send(
                "❌ I need **Manage Roles** to create the wellness role.", ephemeral=True
            )
            return
        if not bot_member.guild_permissions.manage_channels:
            await interaction.followup.send(
                "❌ I need **Manage Channels** to create the wellness channel.",
                ephemeral=True,
            )
            return

        manage_messages_warning = ""
        if not bot_member.guild_permissions.manage_messages:
            manage_messages_warning = (
                "\n\n⚠️ I'm missing **Manage Messages** — friction enforcement "
                "(per-user slow mode that deletes overage messages) will degrade "
                "to nudge-only until I have this permission."
            )

        with ctx.open_db() as conn:
            existing = get_wellness_config(conn, guild.id)

        role: discord.Role | None = None
        channel: discord.TextChannel | None = None

        if existing:
            if existing.role_id:
                role = guild.get_role(existing.role_id)
            if existing.channel_id:
                ch = guild.get_channel(existing.channel_id)
                if isinstance(ch, discord.TextChannel):
                    channel = ch

        try:
            if role is None:
                role = await guild.create_role(
                    name=role_name or WELLNESS_ROLE_NAME,
                    reason="Wellness Guardian setup",
                    mentionable=False,
                )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I don't have permission to create the wellness role.", ephemeral=True
            )
            return

        crisis_text = (crisis_resource_url or "").strip() or (
            existing.crisis_resource_url if existing else ""
        )

        if channel is None:
            try:
                channel = await _create_wellness_channel(
                    guild,
                    role,
                    channel_name=channel_name or WELLNESS_CHANNEL_NAME,
                    crisis_resource_url=crisis_text,
                )
            except discord.Forbidden:
                await interaction.followup.send(
                    "❌ I don't have permission to create the wellness channel.",
                    ephemeral=True,
                )
                return

        with ctx.open_db() as conn:
            upsert_wellness_config(
                conn,
                guild.id,
                role_id=role.id,
                channel_id=channel.id,
                crisis_resource_url=crisis_text or None,
            )

        embed = discord.Embed(
            title="🌿 Wellness Guardian — Setup Complete",
            description=(
                f"**Role:** {role.mention}\n"
                f"**Channel:** {channel.mention}\n\n"
                "Members can now run `/wellness setup` to opt in."
                + manage_messages_warning
            ),
            color=WELLNESS_PRIMARY,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /wellness-admin defaults ──────────────────────────────────────────

    @wellness_admin.command(
        name="defaults",
        description="Set the default enforcement level and crisis resources for new opt-ins.",
    )
    @app_commands.describe(
        default_enforcement="Enforcement style applied to new participants.",
        crisis_resource_url="Crisis resource link or text.",
    )
    @app_commands.choices(
        default_enforcement=[
            app_commands.Choice(name="Gentle reminders", value="gentle"),
            app_commands.Choice(name="Cooldown breaks", value="cooldown"),
            app_commands.Choice(name="Slow mode", value="slow_mode"),
            app_commands.Choice(name="Gradual (recommended)", value="gradual"),
        ]
    )
    async def defaults_cmd(
        self,
        interaction: discord.Interaction,
        default_enforcement: app_commands.Choice[str] | None = None,
        crisis_resource_url: str | None = None,
    ) -> None:
        ctx = self.ctx
        if not _check_admin(interaction):
            await interaction.response.send_message(
                "❌ You need Manage Server to use this command.", ephemeral=True
            )
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        if default_enforcement is None and crisis_resource_url is None:
            await interaction.response.send_message(
                "Provide at least one option to update.", ephemeral=True
            )
            return

        with ctx.open_db() as conn:
            upsert_wellness_config(
                conn,
                guild.id,
                default_enforcement=default_enforcement.value
                if default_enforcement
                else None,
                crisis_resource_url=crisis_resource_url,
            )

        lines = []
        if default_enforcement:
            lines.append(f"**Default enforcement:** {default_enforcement.name}")
        if crisis_resource_url is not None:
            lines.append(f"**Crisis resource:** {crisis_resource_url or '(cleared)'}")
        await interaction.response.send_message(
            "✅ Server wellness defaults updated.\n" + "\n".join(lines),
            ephemeral=True,
        )

    # ── /wellness-admin exempt ────────────────────────────────────────────

    @exempt.command(name="add", description="Mark a channel as exempt from wellness caps.")
    @app_commands.describe(
        channel="Channel to exempt.",
        label="Optional label like 'support' or 'wellness'.",
    )
    async def exempt_add_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        label: str | None = None,
    ) -> None:
        ctx = self.ctx
        if not _check_admin(interaction):
            await interaction.response.send_message(
                "❌ You need Manage Server to use this command.", ephemeral=True
            )
            return
        guild = interaction.guild
        if guild is None:
            return
        with ctx.open_db() as conn:
            add_exempt_channel(conn, guild.id, channel.id, label or "")
        await interaction.response.send_message(
            f"✅ {channel.mention} flagged as exempt.", ephemeral=True
        )

    @exempt.command(
        name="remove", description="Stop exempting a channel from wellness caps."
    )
    @app_commands.describe(channel="Channel to un-exempt.")
    async def exempt_remove_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        ctx = self.ctx
        if not _check_admin(interaction):
            await interaction.response.send_message(
                "❌ You need Manage Server to use this command.", ephemeral=True
            )
            return
        guild = interaction.guild
        if guild is None:
            return
        with ctx.open_db() as conn:
            removed = remove_exempt_channel(conn, guild.id, channel.id)
        if removed:
            await interaction.response.send_message(
                f"✅ {channel.mention} is no longer exempt.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"{channel.mention} was not in the exempt list.", ephemeral=True
            )

    @exempt.command(name="list", description="List all exempt channels.")
    async def exempt_list_cmd(self, interaction: discord.Interaction) -> None:
        ctx = self.ctx
        if not _check_admin(interaction):
            await interaction.response.send_message(
                "❌ You need Manage Server to use this command.", ephemeral=True
            )
            return
        guild = interaction.guild
        if guild is None:
            return
        with ctx.open_db() as conn:
            entries = list_exempt_channels(conn, guild.id)
        if not entries:
            await interaction.response.send_message(
                "No exempt channels configured.", ephemeral=True
            )
            return
        lines = []
        for ch_id, label in entries:
            ch = guild.get_channel(ch_id)
            mention = ch.mention if ch else f"`#{ch_id}`"
            label_text = f" — *{label}*" if label else ""
            lines.append(f"• {mention}{label_text}")
        embed = discord.Embed(
            title="🌿 Wellness — Exempt Channels",
            description="\n".join(lines),
            color=WELLNESS_PRIMARY,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /wellness-admin cap ───────────────────────────────────────────────

    async def _admin_user_cap_label_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        target_id: int | None = None
        if interaction.data and "options" in interaction.data:
            for opt in _flatten_options(interaction.data["options"]):  # type: ignore[typeddict-item]
                if opt.get("name") == "user" and "value" in opt:
                    try:
                        target_id = int(opt["value"])
                    except (TypeError, ValueError):
                        target_id = None
                    break
        if target_id is None or interaction.guild_id is None:
            return []
        with self.ctx.open_db() as conn:
            caps = list_caps(conn, interaction.guild_id, target_id)
        out: list[app_commands.Choice[str]] = []
        for c in caps:
            if not current or current.lower() in c.label.lower():
                out.append(app_commands.Choice(name=c.label[:100], value=c.label))
        return out[:25]

    @cap.command(name="add", description="Create a cap on behalf of a user.")
    @app_commands.describe(
        user="User to create the cap for.",
        label="Friendly name for this cap.",
        scope="Where this cap applies.",
        window="How often the counter resets.",
        limit="Max messages per window.",
        channel="Required if scope is 'channel'.",
        category="Required if scope is 'category'.",
        exclude_exempt="If true (default), exempt channels don't count.",
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="Global", value="global"),
            app_commands.Choice(name="Channel", value="channel"),
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
    async def admin_cap_add_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        label: str,
        scope: app_commands.Choice[str],
        window: app_commands.Choice[str],
        limit: app_commands.Range[int, 1, 100000],
        channel: discord.TextChannel | None = None,
        category: discord.CategoryChannel | None = None,
        exclude_exempt: bool = True,
    ) -> None:
        ctx = self.ctx
        if not _check_admin(interaction):
            await interaction.response.send_message(
                "❌ You need Manage Server to use this command.", ephemeral=True
            )
            return
        guild = interaction.guild
        if guild is None:
            return

        if scope.value == "voice":
            await interaction.response.send_message(
                "🎙️ Voice caps are coming in v2.", ephemeral=True
            )
            return

        scope_target_id = 0
        if scope.value == "channel":
            if channel is None:
                await interaction.response.send_message(
                    "Please pick a channel when scope is `channel`.", ephemeral=True
                )
                return
            scope_target_id = channel.id
        elif scope.value == "category":
            if category is None:
                await interaction.response.send_message(
                    "Please pick a category when scope is `category`.", ephemeral=True
                )
                return
            scope_target_id = category.id

        with ctx.open_db() as conn:
            target = get_wellness_user(conn, guild.id, user.id)
            if target is None or not target.is_active:
                await interaction.response.send_message(
                    f"{user.mention} hasn't opted in to Wellness Guardian yet.",
                    ephemeral=True,
                )
                return
            existing = find_cap_by_label(conn, guild.id, user.id, label)
            if existing:
                await interaction.response.send_message(
                    f"A cap named **{label}** already exists for {user.mention}.",
                    ephemeral=True,
                )
                return
            add_cap(
                conn,
                guild.id,
                user.id,
                label=label,
                scope=scope.value,
                scope_target_id=scope_target_id,
                window=window.value,
                cap_limit=int(limit),
                exclude_exempt=exclude_exempt,
            )
        await interaction.response.send_message(
            f"✅ Created cap **{label}** for {user.mention} — {limit} / {window.value}.",
            ephemeral=True,
        )

    @cap.command(name="edit", description="Edit a user's cap limit.")
    @app_commands.describe(user="Cap owner.", label="Cap to edit.", new_limit="New limit.")
    @app_commands.autocomplete(label=_admin_user_cap_label_autocomplete)
    async def admin_cap_edit_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        label: str,
        new_limit: app_commands.Range[int, 1, 100000],
    ) -> None:
        ctx = self.ctx
        if not _check_admin(interaction):
            await interaction.response.send_message(
                "❌ You need Manage Server to use this command.", ephemeral=True
            )
            return
        guild = interaction.guild
        if guild is None:
            return
        with ctx.open_db() as conn:
            cap = find_cap_by_label(conn, guild.id, user.id, label)
            if not cap:
                await interaction.response.send_message(
                    f"No cap named **{label}** for {user.mention}.", ephemeral=True
                )
                return
            update_cap_limit(conn, cap.id, int(new_limit))
        await interaction.response.send_message(
            f"✅ Updated **{label}** for {user.mention} → {new_limit} / {cap.window}.",
            ephemeral=True,
        )

    @cap.command(name="remove", description="Remove a user's cap.")
    @app_commands.describe(user="Cap owner.", label="Cap to remove.")
    @app_commands.autocomplete(label=_admin_user_cap_label_autocomplete)
    async def admin_cap_remove_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        label: str,
    ) -> None:
        ctx = self.ctx
        if not _check_admin(interaction):
            await interaction.response.send_message(
                "❌ You need Manage Server to use this command.", ephemeral=True
            )
            return
        guild = interaction.guild
        if guild is None:
            return
        with ctx.open_db() as conn:
            cap = find_cap_by_label(conn, guild.id, user.id, label)
            if not cap:
                await interaction.response.send_message(
                    f"No cap named **{label}** for {user.mention}.", ephemeral=True
                )
                return
            remove_cap(conn, cap.id)
        await interaction.response.send_message(
            f"🗑️ Removed cap **{label}** from {user.mention}.", ephemeral=True
        )

    # ── /wellness-admin blackout ──────────────────────────────────────────

    async def _admin_user_blackout_name_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        target_id: int | None = None
        if interaction.data and "options" in interaction.data:
            for opt in _flatten_options(interaction.data["options"]):  # type: ignore[typeddict-item]
                if opt.get("name") == "user" and "value" in opt:
                    try:
                        target_id = int(opt["value"])
                    except (TypeError, ValueError):
                        target_id = None
                    break
        if target_id is None or interaction.guild_id is None:
            return []
        with self.ctx.open_db() as conn:
            blackouts = list_blackouts(conn, interaction.guild_id, target_id)
        out: list[app_commands.Choice[str]] = []
        for b in blackouts:
            if not current or current.lower() in b.name.lower():
                out.append(app_commands.Choice(name=b.name[:100], value=b.name))
        return out[:25]

    @blackout.command(name="add", description="Create a blackout on behalf of a user.")
    @app_commands.describe(
        user="User to schedule the blackout for.",
        name="Friendly name (e.g. 'sleep').",
        start_time="Start in 24h HH:MM (the user's local time).",
        end_time="End in 24h HH:MM (the user's local time).",
        days="Days: 'all', 'weekdays', 'weekends', or comma list (mon,wed,fri).",
    )
    async def admin_blackout_add_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        name: str,
        start_time: str,
        end_time: str,
        days: str = "all",
    ) -> None:
        ctx = self.ctx
        if not _check_admin(interaction):
            await interaction.response.send_message(
                "❌ You need Manage Server to use this command.", ephemeral=True
            )
            return
        guild = interaction.guild
        if guild is None:
            return
        start_min = parse_time_to_minute(start_time)
        end_min = parse_time_to_minute(end_time)
        if start_min is None or end_min is None:
            await interaction.response.send_message(
                "Times must be `HH:MM` in 24-hour format.", ephemeral=True
            )
            return
        days_mask = parse_days_mask(days)
        if days_mask is None:
            await interaction.response.send_message(
                "Days must be `all`, `weekdays`, `weekends`, or a comma list.",
                ephemeral=True,
            )
            return
        with ctx.open_db() as conn:
            target = get_wellness_user(conn, guild.id, user.id)
            if target is None or not target.is_active:
                await interaction.response.send_message(
                    f"{user.mention} hasn't opted in to Wellness Guardian.",
                    ephemeral=True,
                )
                return
            existing = find_blackout_by_name(conn, guild.id, user.id, name)
            if existing:
                await interaction.response.send_message(
                    f"{user.mention} already has a blackout named **{name}**.",
                    ephemeral=True,
                )
                return
            add_blackout(
                conn,
                guild.id,
                user.id,
                name=name,
                start_minute=start_min,
                end_minute=end_min,
                days_mask=days_mask,
            )
        await interaction.response.send_message(
            f"✅ Created blackout **{name}** for {user.mention} — "
            f"{_format_minute(start_min)}–{_format_minute(end_min)}, {_format_days_mask(days_mask)}.",
            ephemeral=True,
        )

    @blackout.command(name="remove", description="Remove a user's blackout.")
    @app_commands.describe(user="Blackout owner.", name="Blackout name.")
    @app_commands.autocomplete(name=_admin_user_blackout_name_autocomplete)
    async def admin_blackout_remove_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        name: str,
    ) -> None:
        ctx = self.ctx
        if not _check_admin(interaction):
            await interaction.response.send_message(
                "❌ You need Manage Server to use this command.", ephemeral=True
            )
            return
        guild = interaction.guild
        if guild is None:
            return
        with ctx.open_db() as conn:
            blackout = find_blackout_by_name(conn, guild.id, user.id, name)
            if not blackout:
                await interaction.response.send_message(
                    f"No blackout named **{name}** for {user.mention}.", ephemeral=True
                )
                return
            remove_blackout(conn, blackout.id)
        await interaction.response.send_message(
            f"🗑️ Removed blackout **{name}** from {user.mention}.", ephemeral=True
        )

    # ── /wellness-admin dashboard ─────────────────────────────────────────

    @wellness_admin.command(
        name="dashboard",
        description="Overview of participants, caps, blackouts, partnerships, and streaks.",
    )
    async def admin_dashboard_cmd(self, interaction: discord.Interaction) -> None:
        ctx = self.ctx
        if not _check_admin(interaction):
            await interaction.response.send_message(
                "❌ You need Manage Server to use this command.", ephemeral=True
            )
            return
        guild = interaction.guild
        if guild is None:
            return

        with ctx.open_db() as conn:
            cfg = get_wellness_config(conn, guild.id)
            users = list_active_users(conn, guild.id)
            exempt = list_exempt_channels(conn, guild.id)
            cap_count_row = conn.execute(
                "SELECT COUNT(*) AS n FROM wellness_caps WHERE guild_id = ?",
                (guild.id,),
            ).fetchone()
            blackout_count_row = conn.execute(
                "SELECT COUNT(*) AS n FROM wellness_blackouts WHERE guild_id = ? AND enabled = 1",
                (guild.id,),
            ).fetchone()
            partner_count_row = conn.execute(
                "SELECT COUNT(*) AS n FROM wellness_partners WHERE guild_id = ? AND status = 'accepted'",
                (guild.id,),
            ).fetchone()
            slow_mode_count_row = conn.execute(
                "SELECT COUNT(*) AS n FROM wellness_slow_mode WHERE guild_id = ?",
                (guild.id,),
            ).fetchone()
            top_streaks = conn.execute(
                """
                SELECT s.user_id, s.current_days, s.current_badge
                  FROM wellness_streaks s
                  JOIN wellness_users u ON u.guild_id = s.guild_id AND u.user_id = s.user_id
                 WHERE s.guild_id = ?
                   AND u.opted_in_at IS NOT NULL AND u.opted_out_at IS NULL
                 ORDER BY s.current_days DESC
                 LIMIT 5
                """,
                (guild.id,),
            ).fetchall()

        cap_total = int(cap_count_row["n"]) if cap_count_row else 0
        blackout_total = int(blackout_count_row["n"]) if blackout_count_row else 0
        partner_total = int(partner_count_row["n"]) if partner_count_row else 0
        slow_mode_total = int(slow_mode_count_row["n"]) if slow_mode_count_row else 0
        paused_total = sum(1 for u in users if u.is_paused)

        embed = discord.Embed(title="🌿 Wellness — server overview", color=WELLNESS_OVERVIEW)
        embed.add_field(name="Active participants", value=str(len(users)), inline=True)
        embed.add_field(name="Currently paused", value=str(paused_total), inline=True)
        embed.add_field(name="In slow mode", value=str(slow_mode_total), inline=True)
        embed.add_field(name="Active caps", value=str(cap_total), inline=True)
        embed.add_field(name="Active blackouts", value=str(blackout_total), inline=True)
        embed.add_field(name="Partnerships", value=str(partner_total), inline=True)
        embed.add_field(name="Exempt channels", value=str(len(exempt)), inline=True)
        if cfg:
            embed.add_field(
                name="Default enforcement", value=cfg.default_enforcement, inline=True
            )

        if top_streaks:
            lines = []
            for row in top_streaks:
                uid = int(row["user_id"])
                days = int(row["current_days"])
                badge = str(row["current_badge"]) or "🌱"
                member = guild.get_member(uid)
                uname = member.display_name if member else f"User {uid}"
                lines.append(f"{badge} **{uname}** — {days}d")
            embed.add_field(name="Top streaks", value="\n".join(lines), inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /wellness-admin settings ──────────────────────────────────────────

    @wellness_admin.command(
        name="settings",
        description="Change a user's wellness settings on their behalf.",
    )
    @app_commands.describe(
        user="Wellness participant to update.",
        enforcement_level="Enforcement style.",
        notifications_pref="How they receive notifications.",
        public_commitment="Whether they appear on the active list.",
        daily_reset_hour="Local hour (0-23) when daily caps reset.",
    )
    @app_commands.choices(
        enforcement_level=[
            app_commands.Choice(name=lvl, value=lvl) for lvl in ENFORCEMENT_LEVELS
        ],
        notifications_pref=[
            app_commands.Choice(name=p, value=p) for p in NOTIFICATION_PREFS
        ],
    )
    async def admin_settings_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        enforcement_level: app_commands.Choice[str] | None = None,
        notifications_pref: app_commands.Choice[str] | None = None,
        public_commitment: bool | None = None,
        daily_reset_hour: int | None = None,
    ) -> None:
        ctx = self.ctx
        if not _check_admin(interaction):
            await interaction.response.send_message(
                "❌ You need Manage Server to use this command.", ephemeral=True
            )
            return
        guild = interaction.guild
        if guild is None:
            return

        if daily_reset_hour is not None and not (0 <= daily_reset_hour < 24):
            await interaction.response.send_message(
                "Daily reset hour must be between 0 and 23.", ephemeral=True
            )
            return

        with ctx.open_db() as conn:
            wuser = get_wellness_user(conn, guild.id, user.id)
            if wuser is None or not wuser.is_active:
                await interaction.response.send_message(
                    f"{user.mention} hasn't opted in to wellness.", ephemeral=True
                )
                return
            update_user_settings(
                conn,
                guild.id,
                user.id,
                enforcement_level=enforcement_level.value if enforcement_level else None,
                notifications_pref=notifications_pref.value
                if notifications_pref
                else None,
                public_commitment=public_commitment,
                daily_reset_hour=daily_reset_hour,
            )

        changed = []
        if enforcement_level:
            changed.append(f"enforcement → **{enforcement_level.value}**")
        if notifications_pref:
            changed.append(f"notifications → **{notifications_pref.value}**")
        if public_commitment is not None:
            changed.append(f"public_commitment → **{public_commitment}**")
        if daily_reset_hour is not None:
            changed.append(f"daily_reset_hour → **{daily_reset_hour}**")
        summary = ", ".join(changed) if changed else "no changes specified"
        await interaction.response.send_message(
            f"✅ Updated {user.mention}: {summary}", ephemeral=True
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(WellnessAdminCog(bot, bot.ctx))
