"""General moderation and help commands."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from settings import AUTO_DELETE_SETTINGS

if TYPE_CHECKING:
    from app_context import AppContext, Bot


def _fmt(command_specs: list[tuple[str, str]]) -> str:
    return "\n".join(f"`{name}`\n{desc}" for name, desc in command_specs)


_SECTION_META: dict[str, tuple[str, discord.Color]] = {
    "General": ("🌿", discord.Color.from_str("#5865F2")),
    "Role Grants": ("🎭", discord.Color.from_str("#57F287")),
    "XP Grant": ("⭐", discord.Color.from_str("#FEE75C")),
    "Moderation Actions": ("🛡️", discord.Color.from_str("#ED4245")),
    "Reports": ("📊", discord.Color.from_str("#EB459E")),
    "Activity & Graphs": ("📈", discord.Color.from_str("#5DADE2")),
    "Watch List": ("🔍", discord.Color.from_str("#3498DB")),
    "AI Moderation": ("🤖", discord.Color.from_str("#5865F2")),
    "Fun": ("🎉", discord.Color.from_str("#F1C40F")),
    "Data Management": ("🗄️", discord.Color.from_str("#95A5A6")),
    "Configuration": ("⚙️", discord.Color.from_str("#1ABC9C")),
}


def _page(name: str, body: str) -> discord.Embed:
    emoji, color = _SECTION_META.get(name, ("📖", discord.Color.blurple()))
    return discord.Embed(
        title=f"{emoji}  {name}",
        description=body,
        color=color,
    )


def _build_help_pages(
    ctx: AppContext, interaction: discord.Interaction
) -> list[discord.Embed]:
    pages: list[discord.Embed] = []

    pages.append(
        _page(
            "General",
            "Commands available to everyone.\n\n"
            + _fmt(
                [
                    ("/help", "This guide."),
                    (
                        "/xp_leaderboards timescale:week",
                        "See the top XP earners and your own rank. "
                        "Pick a window: hour, day, week, month, year, or alltime.",
                    ),
                    (
                        "/ticket open description:...",
                        "Open a private ticket with the mod team. "
                        "Only you and mods can see the channel.",
                    ),
                    ("/foolsday_exclude", "Opt out of the April Fools name shuffle."),
                    ("/foolsday_join", "Join an active name shuffle."),
                ]
            ),
        )
    )

    if ctx.can_grant_denizen(interaction):
        grant_cmds: list[tuple[str, str]] = []
        for gname, gcfg in ctx.grant_roles.items():
            grant_cmds.append(
                (
                    f"/grant role:{gname} member:@user",
                    f"Give the **{gcfg['label']}** role to a member.",
                )
            )
        pages.append(
            _page(
                "Role Grants",
                "Give community roles to members. "
                "Manage roles and permissions in `/config roles`.\n\n"
                + _fmt(grant_cmds),
            )
        )

    if ctx.can_use_xp_grant(interaction):
        pages.append(
            _page(
                "XP Grant",
                "Award XP for contributions outside normal chat — events, art, helpful DMs, etc.\n\n"
                + _fmt(
                    [
                        ("/xp_give member:@user", "Award 20 XP to a member."),
                    ]
                ),
            )
        )

    if ctx.is_mod(interaction):
        pages.append(
            _page(
                "Moderation Actions",
                "All actions are logged and transcribed.\n\n"
                "**🔒 Jail**\n"
                + _fmt(
                    [
                        (
                            "/setup",
                            "First-time setup — creates jail role, categories, log channels, and role pickers. "
                            "Admin only. Re-run to adjust.",
                        ),
                        (
                            "/jail user:@user duration:24h reason:...",
                            "Place a member in a private jail channel. "
                            "Duration: `30m`, `2h`, `7d`, or omit for indefinite.",
                        ),
                        (
                            "/unjail user:@user reason:...",
                            "Release a jailed member. Restores their roles and saves a transcript.",
                        ),
                        (
                            "/pull user:@user",
                            "Bring someone into this jail/ticket channel.",
                        ),
                        (
                            "/remove user:@user",
                            "Remove someone you pulled into this channel.",
                        ),
                    ]
                )
                + "\n\n**⚠️ Warnings**\n"
                + _fmt(
                    [
                        (
                            "/warn user:@user reason:...",
                            "Issue a warning (mod-only, user is not notified). Admins are pinged at the threshold.",
                        ),
                        (
                            "/warnings user:@user",
                            "List all warnings for a member (active + revoked).",
                        ),
                        (
                            "/revokewarn user:@user warning_id:42 reason:...",
                            "Cancel a warning by ID. Stays in history but stops counting.",
                        ),
                        (
                            "/modinfo user:@user",
                            "Full mod profile: jail status, warnings, and tickets. Run this first.",
                        ),
                    ]
                )
                + "\n\n**📩 Tickets**\n"
                + _fmt(
                    [
                        (
                            "/ticket panel channel:#support",
                            "Post the ticket button in a channel. Users click it to open a private ticket.",
                        ),
                        (
                            "/ticket close reason:...",
                            "Close this ticket. It becomes read-only with Reopen/Delete buttons.",
                        ),
                        ("/ticket reopen", "Reopen a closed ticket."),
                        (
                            "/ticket delete",
                            "Delete a closed ticket. A transcript is saved first.",
                        ),
                        (
                            "/ticket claim",
                            "Claim this ticket — you'll get DM pings on new activity.",
                        ),
                        (
                            "/ticket escalate reason:...",
                            "Bring admins into this ticket and ping them.",
                        ),
                    ]
                ),
            )
        )

        pages.append(
            _page(
                "Reports",
                "Charts and tables about members, roles, and engagement.\n\n"
                + _fmt(
                    [
                        ("/report list_role role:@Role", "List every member who has a role."),
                        ("/report inactive_role role:@Role days:7", "Members of a role who haven't posted in N days."),
                        ("/report inactive time_period:7d", "All members inactive for a period. Add `channel:` or `exclude_gif_only:True`."),
                        ("/report oldest_sfw count:10", "Members without NSFW access, ranked by longest silence."),
                        ("/xp_level_review level:5", "How long does it take to reach a level? Histogram with stats."),
                        ("/purge count:50", "Bulk-delete messages. Use `after:19:35` for time-based, or both."),
                        ("/dropoff period:week", "Who disengaged the most? Compares two consecutive windows. Add `member:@user` for a full profile instead."),
                        ("/burst_ranking limit:5", "Who sparks conversation when they return vs. who posts quietly?"),
                        ("/interaction_heatmap timescale:week", "Matrix heatmap of who talks to whom (replies + mentions)."),
                        ("/report role_growth resolution:week", "Cumulative role grants over time. Add `roles:Name1,Name2` to filter."),
                        ("/report promotion_review", "Members past level 5 who still lack NSFW access."),
                        ("/report message_cadence resolution:day", "Time between messages as a candlestick chart. Green = speeding up."),
                        ("/report join_times resolution:hour_of_day", "When do new members join? By hour or day of week."),
                        ("/report quality_scores", "Ranked quality scores: engagement, consistency, resonance, activity."),
                        ("/report message_rate days:7", "When is the server busy? Messages per 10-min slot, averaged."),
                        ("/report greeter_response days:30", "How long do new members wait for a greeter hello?"),
                        ("/report nsfw_gender resolution:week", "NSFW posting by gender. `display:bar` or `display:line`."),
                        ("/report backfill_roles", "Sync role events with current state. Run after bulk role edits."),
                    ]
                ),
            )
        )

        pages.append(
            _page(
                "Activity & Graphs",
                "Visual tools for understanding engagement.\n\n"
                + _fmt(
                    [
                        ("/activity resolution:day mode:xp", "Bar chart of messages or XP over time. Scope with `member:` or `channel:`. Resolutions: hour, day, week, month, hour_of_day, day_of_week."),
                        ("/session_burst member:@user", "How active is someone in the hour after a break? Shows whether they spark conversation or post quietly."),
                        ("/connection_web timescale:week", "Network graph of replies and mentions. Add `member:@user` to focus. Key options: `min_pct`, `layers`, `limit`."),
                        ("/chilling_effect lookback_days:30", "Who makes others go quiet when they arrive? Correlation-based."),
                        ("/invite_web member:@user", "Who invited whom, as a network graph."),
                    ]
                ),
            )
        )

        pages.append(
            _page(
                "Fun",
                "Seasonal tools.\n\n"
                + _fmt(
                    [
                        ("/foolsday action:shuffle", "Randomize nicknames for active members. `action:restore` to undo."),
                        ("/foolsday_exclude user:@User", "Exclude a member from the shuffle."),
                        ("/foolsday_join user:@User", "Add a member to the active shuffle."),
                        ("/foolsday_include user", "Remove someone from the exclusion list."),
                        ("/foolsday_exclusions", "List excluded members."),
                        ("/foolsday_samename name", "Give everyone the same name (random or custom). Restore to undo."),
                        ("/foolsday_repair", "Fix broken mappings by walking through each one."),
                    ]
                ),
            )
        )

        pages.append(
            _page(
                "Data Management",
                "Populate and maintain bot data.\n\n"
                + _fmt(
                    [
                        ("/interaction_scan days:0", "Backfill the interaction graph from message history. Run once after setup. `reset:True` clears old data first."),
                        ("/gender set member:@user gender:Female", "Tag a member as Male, Female, or Non-binary for NSFW analytics."),
                        ("/gender check member:@user", "See a member's current gender tag."),
                        ("/gender classify", "Step through unclassified members one by one with buttons."),
                    ]
                ),
            )
        )

        pages.append(
            _page(
                "Watch List",
                "Silently forward a member's public messages to your DMs. "
                "With an AI key set, only flagged messages are forwarded.\n\n"
                + _fmt(
                    [
                        ("/watch add user:@user", "Start watching a member."),
                        ("/watch remove user:@user", "Stop watching a member."),
                        ("/watch list", "List everyone you are watching."),
                    ]
                ),
            )
        )

        pages.append(
            _page(
                "AI Moderation",
                "Requires `ANTHROPIC_API_KEY`.\n\n"
                + _fmt(
                    [
                        ("/ai review member:@user days:7", "AI flags rule violations and patterns in a member's recent messages."),
                        ("/ai scan count:50", "AI scans the last N messages in this channel for rule violations."),
                        ("/ai channel question:... minutes:60", "Ask the AI about a channel's recent activity. e.g. *'Did anyone harass a new member?'*"),
                        ("/ai query member:@user question:...", "Ask the AI about a member's message history. e.g. *'Has this person been hostile toward newcomers?'*"),
                    ]
                ),
            )
        )

        pages.append(
            _page(
                "Configuration",
                "Use `/config <section>` to open a settings panel.\n\n"
                "**`/config global`** — Timezone, mod channel, bypass roles\n"
                "**`/config roles`** — Grant roles, permissions, log channels, messages\n"
                "**`/config xp`** — XP channels, level-5 role, grant allowlist\n"
                "**`/config welcome`** — Welcome/leave channels and message templates\n"
                "**`/config spoiler`** — Spoiler-guard channel list\n"
                "**`/config prune`** — Inactivity prune role and threshold\n"
                "**`/config booster`** — Cosmetic role picker for boosters\n\n"
                "**Related commands**\n"
                + _fmt(
                    [
                        ("/xp_excluded_channels", "Show which channels have XP turned off."),
                        ("/xp_backfill_history days:30", "Scan past messages to fill XP gaps."),
                        ("/welcome_preview", "Preview the welcome message."),
                        ("/leave_preview", "Preview the leave message."),
                        ("/inactivity_prune status", "Current prune rule, threshold, and exemptions."),
                        ("/inactivity_prune exempt member:@user", "Protect a member from pruning."),
                        ("/inactivity_prune unexempt member:@user", "Remove a prune exemption."),
                        ("/inactivity_prune run", "Run a prune now instead of waiting for the daily schedule."),
                        ("/auto_delete del_age:30d run:1d", "Delete old messages, optionally on a schedule. `run:once` or `run:off`."),
                        ("/auto_delete_configs", "View and manage all auto-delete schedules."),
                        ("/quality_leave add member:@user days:30", "Put a member on leave (pauses quality scoring)."),
                        ("/quality_leave remove member:@user", "End a member's leave of absence."),
                        ("/quality_leave list", "List members currently on leave."),
                    ]
                ),
            )
        )

    return pages


class HelpSelect(discord.ui.Select):
    def __init__(self, pages: list[discord.Embed], invoker_id: int):
        self.pages = pages
        self.invoker_id = invoker_id
        options = []
        for i, p in enumerate(pages):
            title = p.title or ""
            if "  " in title:
                emoji, label = title.split("  ", 1)
            else:
                emoji, label = "📖", title
            options.append(discord.SelectOption(label=label, emoji=emoji, value=str(i)))
        super().__init__(placeholder="Choose a section…", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.defer()
            return
        index = int(self.values[0])
        embed = self.pages[index]
        embed.set_footer(text="Tip: Discord shows parameter hints while you type.")
        await interaction.response.edit_message(embed=embed, view=self.view)


class HelpView(discord.ui.View):
    def __init__(self, pages: list[discord.Embed], invoker_id: int):
        super().__init__(timeout=120)
        self.select = HelpSelect(pages, invoker_id)
        self.add_item(self.select)

    def current_embed(self) -> discord.Embed:
        embed = self.select.pages[0]
        embed.set_footer(text="Tip: Discord shows parameter hints while you type.")
        return embed

    async def on_timeout(self) -> None:
        self.select.disabled = True


class ModCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    @app_commands.command(
        name="help", description="Browse all available commands organized by category."
    )
    async def help_command(self, interaction: discord.Interaction) -> None:
        pages = _build_help_pages(self.ctx, interaction)
        view = HelpView(pages, invoker_id=interaction.user.id)
        await interaction.response.send_message(
            embed=view.current_embed(), view=view, ephemeral=True
        )

    @app_commands.command(
        name="purge",
        description="Bulk-delete messages in this channel by count and/or time.",
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(
        count="How many messages to delete (max 1000). Omit to delete all since `after`.",
        after="Delete messages from this time today onward (HH:MM in server time).",
    )
    async def purge(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, 1, 1000] | None = None,
        after: str | None = None,
    ) -> None:
        ctx = self.ctx
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        after_dt: datetime | None = None
        if after is not None:
            try:
                parts = after.strip().split(":")
                if len(parts) not in (2, 3):
                    raise ValueError
                h, m = int(parts[0]), int(parts[1])
                s = int(parts[2]) if len(parts) == 3 else 0
                server_tz = timezone(timedelta(hours=ctx.tz_offset_hours))
                now = datetime.now(server_tz)
                after_dt = now.replace(hour=h, minute=m, second=s, microsecond=0)
                if after_dt > now:
                    after_dt -= timedelta(days=1)
            except (ValueError, IndexError):
                tz_label = (
                    f"UTC{ctx.tz_offset_hours:+g}"
                    if ctx.tz_offset_hours != 0
                    else "UTC"
                )
                await interaction.response.send_message(
                    f"Invalid time format. Use `HH:MM` or `HH:MM:SS` (server time is {tz_label}), e.g. `19:35`.",
                    ephemeral=True,
                )
                return

        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "This command only works in text channels and threads.", ephemeral=True
            )
            return

        bot_member = channel.guild.me if hasattr(channel, "guild") else None
        if bot_member and not channel.permissions_for(bot_member).manage_messages:
            await interaction.response.send_message(
                "I need the **Manage Messages** permission in this channel to delete messages.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        deleted: list[discord.Message] = []
        remaining = count
        while True:
            batch_limit = min(remaining, 100) if remaining is not None else 100
            batch = await channel.purge(limit=batch_limit, after=after_dt)
            deleted.extend(batch)
            if remaining is not None:
                remaining -= len(batch)
                if remaining <= 0:
                    break
            if len(batch) < batch_limit:
                break
            await asyncio.sleep(AUTO_DELETE_SETTINGS.bulk_delete_pause_seconds)

        if count is not None and after_dt is not None:
            label = f"last {count} since {after}"
        elif count is not None:
            label = f"last {count}"
        elif after_dt is not None:
            label = f"since {after}"
        else:
            label = "entire channel"
        await interaction.followup.send(
            f"Deleted {len(deleted)} message{'s' if len(deleted) != 1 else ''} ({label}).",
            ephemeral=True,
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(ModCog(bot, bot.ctx))
