"""General moderation and help commands."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from settings import AUTO_DELETE_SETTINGS

if TYPE_CHECKING:
    from app_context import AppContext, Bot


def _fmt(command_specs: list[tuple[str, str]]) -> str:
    return "\n".join(f"`{name}`\n{desc}" for name, desc in command_specs)


# (label, emoji, color, intro, [(command, description), ...])
_SECTION_META: dict[str, tuple[str, discord.Color]] = {
    "General":            ("🌿", discord.Color.from_str("#5865F2")),
    "Role Grants":        ("🎭", discord.Color.from_str("#57F287")),
    "XP Grant":           ("⭐", discord.Color.from_str("#FEE75C")),
    "Moderation Actions": ("🛡️", discord.Color.from_str("#ED4245")),
    "Reports":            ("📊", discord.Color.from_str("#EB459E")),
    "Activity & Graphs":  ("📈", discord.Color.from_str("#5DADE2")),
    "Watch List":         ("🔍", discord.Color.from_str("#3498DB")),
    "AI Moderation":      ("🤖", discord.Color.from_str("#5865F2")),
    "Fun":                ("🎉", discord.Color.from_str("#F1C40F")),
    "Data Management":    ("🗄️", discord.Color.from_str("#95A5A6")),
    "Configuration":      ("⚙️", discord.Color.from_str("#1ABC9C")),
}


def _page(name: str, body: str) -> discord.Embed:
    emoji, color = _SECTION_META.get(name, ("📖", discord.Color.blurple()))
    return discord.Embed(
        title=f"{emoji}  {name}",
        description=body,
        color=color,
    )


def _build_help_pages(ctx: AppContext, interaction: discord.Interaction) -> list[discord.Embed]:
    pages: list[discord.Embed] = []

    # ── General (everyone) ────────────────────────────────────────────────────
    pages.append(_page("General",
        "Available to everyone in the server.\n\n"
        + _fmt([
            ("/help", "Show this guide."),
            ("/xp_leaderboards timescale:week",
             "Top XP earners for a chosen time window (hour / day / week / month / year / all time), "
             "plus your own rank within that period."),
            ("/ticket open description:...",
             "Open a private support ticket with the mod team. "
             "A new channel is created that only you and the mods can see. "
             "Optional `description` gives mods context up front."),
            ("/foolsday_exclude",
             "Opt out of the April Fools name shuffle. "
             "Mods can specify a user to exclude others."),
            ("/foolsday_join",
             "Join an active name shuffle. "
             "Mods can specify a user to add others."),
        ])
    ))

    # ── Role Grants (per-role permissions or mod) ───────────────────────────
    if ctx.can_grant_denizen(interaction):
        grant_cmds: list[tuple[str, str]] = []
        for gname, gcfg in ctx.grant_roles.items():
            grant_cmds.append((
                f"/grant role:{gname} member:@user",
                f"Grant the {gcfg['label']} role to a member.",
            ))
        pages.append(_page("Role Grants",
            "Grant community roles to members. "
            "Access is configured per-role via `/config roles`.\n\n"
            + _fmt(grant_cmds)
        ))

    # ── XP give (allowlisted users or mod) ────────────────────────────────────
    if ctx.can_use_xp_grant(interaction):
        pages.append(_page("XP Grant",
            "Manually award XP. Useful for rewarding contributions that happen outside "
            "normal message activity — events, art submissions, helpful DMs, etc. "
            "Requires mod or allowlist access.\n\n"
            + _fmt([
                ("/xp_give member:@user", "Give 20 XP to one member."),
            ])
        ))

    if ctx.is_mod(interaction):

        # ── Moderation Actions ────────────────────────────────────────────────
        pages.append(_page("Moderation Actions",
            "Jail, warn, and ticket management. Requires **Manage Server** permission. "
            "All actions are written to the audit log and — where applicable — the configured "
            "log / transcript channels.\n\n"
            "**🔒 Jail**\n"
            + _fmt([
                ("/setup",
                 "First-time moderation setup wizard — creates the jailed role, jail and ticket "
                 "categories, log / transcript channels, and mod/admin role pickers. "
                 "Admin only. Re-run to adjust settings."),
                ("/jail user:@user duration:24h reason:...",
                 "Place a member in a private jail channel. "
                 "`duration` accepts formats like `30m`, `2h`, `7d`; omit for indefinite. "
                 "The member loses access to the rest of the server until released or expiry."),
                ("/unjail user:@user reason:...",
                 "Release a jailed member. Restores their prior role set and posts a transcript "
                 "of the jail channel before deleting it."),
                ("/pull user:@user",
                 "Add a user to the current jail/ticket channel so they can participate "
                 "(useful for bringing in witnesses, reporters, or additional mods)."),
                ("/remove user:@user",
                 "Remove a user you previously `pull`ed from the current jail/ticket channel."),
            ])
            + "\n\n**⚠️ Warnings**\n"
            + _fmt([
                ("/warn user:@user reason:...",
                 "Issue a warning. The member is DM'd, the action is audited, and "
                 "admins are pinged when they hit the configured warning threshold."),
                ("/warnings user:@user",
                 "List every warning (active + revoked) for a member, with reasons and moderators."),
                ("/revokewarn user:@user warning_id:42 reason:...",
                 "Cancel a specific warning by ID. The warning stays in history but no longer "
                 "counts toward the threshold."),
                ("/modinfo user:@user",
                 "Comprehensive moderation profile: current jail status, jail history, "
                 "warning count, and ticket history — the first thing to run before taking action."),
            ])
            + "\n\n**📩 Tickets**\n"
            + _fmt([
                ("/ticket panel channel:#support",
                 "Post the ticket-creation button panel in a channel. "
                 "Users click the button (or run `/ticket open`) to create their own private ticket."),
                ("/ticket close reason:...",
                 "Close the current ticket. The channel stays visible read-only with Reopen / Delete buttons."),
                ("/ticket reopen",
                 "Reopen a closed ticket — restores the creator's write access."),
                ("/ticket delete",
                 "Permanently delete a closed ticket. Generates a transcript in the log channel first."),
                ("/ticket claim",
                 "Mark yourself as handling this ticket. You'll get DM pings on new activity "
                 "so you don't have to watch the channel."),
                ("/ticket escalate reason:...",
                 "Bring admin roles into the ticket and ping them. Use for situations that "
                 "need authority above the normal mod team."),
            ])
        ))

        # ── Reports ───────────────────────────────────────────────────────────
        pages.append(_page("Reports",
            "Inspect member activity and role membership. "
            "Requires **Manage Server** permission.\n\n"
            + _fmt([
                ("/report list_role role:@Role",
                 "List every current holder of a role — handy before mass-messaging, "
                 "auditing permissions, or checking coverage."),
                ("/report inactive_role role:@Role days:7",
                 "Members of a role who haven't posted in N days. "
                 "Good for identifying who might need a check-in before a prune run."),
                ("/report inactive time_period:7d channel:#channel exclude_gif_only:True",
                 "All server members inactive for a given period, regardless of role. "
                 "Optionally filter to a channel or exclude members whose only activity is GIFs."),
                ("/report oldest_sfw count:10",
                 "Members without NSFW access, ranked by how long since they last posted. "
                 "Useful for finding long-inactive accounts that were never fully onboarded."),
                ("/xp_level_review level:5",
                 "Histogram of how many days members take to reach a given XP level, "
                 "with mean, mode, and std dev overlaid. "
                 "Use this to judge whether your XP thresholds feel earned or too fast."),
                ("/purge count:50",
                 "Delete the last N messages in this channel. "
                 "Use `after:19:35` to delete everything since a server-local time today — "
                 "both can be combined. With no arguments, clears the entire channel. "
                 "Useful for clearing spam, failed bot responses, or accidental posts."),
                ("/dropoff period:week limit:10 channel:#channel member:@user",
                 "Compares message counts across two consecutive time windows and surfaces "
                 "members with the steepest drop. "
                 "A week-over-week dropoff is often the earliest signal of disengagement. "
                 "Pass `channel` to restrict the comparison, or `member` to get a full "
                 "engagement profile for one person instead of the ranked list."),
                ("/burst_ranking limit:5",
                 "Server-wide ranking of who most reliably drives conversation after returning "
                 "from a 20-min break, and who tends to post without pulling others in."),
                ("/interaction_heatmap timescale:week min_pct:5 limit:30",
                 "Adjacency-matrix heatmap of reply/mention interactions between members. "
                 "Users are sorted by total interaction volume; colour intensity shows weight. "
                 "A cleaner alternative to the network graph for dense servers."),
                ("/report role_growth resolution:week roles:NSFW,Booster",
                 "Chart of cumulative role grants over time. "
                 "Resolutions: daily (30d), weekly (12wk), monthly (12mo). "
                 "Pass `roles:` as a comma-separated list to chart only specific roles."),
                ("/report promotion_review",
                 "Members above level 5 without spicy access — flags inactivity-pruned users."),
                ("/report message_cadence resolution:day channel:#channel",
                 "Candlestick chart of time between messages. "
                 "Body shows 20th–80th percentile, wick shows min–max, tick shows median. "
                 "Green = chat speeding up, pink = slowing down."),
                ("/report join_times resolution:hour_of_day",
                 "Histogram of when current members joined — by hour of day or day of week."),
                ("/report quality_scores",
                 "Ranked member quality scores. Four components: Engagement Given (40%), "
                 "Consistency/Recency (25%), Content Resonance (20%), Posting Activity (15%). "
                 "New members (<30d) and low-data members (<7 active days) are flagged separately."),
                ("/report message_rate days:7",
                 "Chart of messages per 10-minute interval across the day, averaged over N days. "
                 "Reveals when the server is genuinely busy vs idle in your timezone."),
                ("/report greeter_response days:30",
                 "Histogram of how long new members wait for their first greeter message in the welcome channel. "
                 "Requires the greeter role and welcome channel to be configured."),
                ("/report nsfw_gender resolution:week display:bar media_only:False channel:#channel",
                 "Chart NSFW channel posting broken down by gender (set via `/gender set`). "
                 "`display:bar` = stacked bars, `display:line` = ratio line chart. "
                 "`media_only:True` filters to image/video posts only. "
                 "Defaults to all NSFW channels if `channel` is omitted."),
                ("/report backfill_roles",
                 "Sync the role event log with the current server state so the role growth chart "
                 "is accurate. Run after manual role bulk-edits or after first install."),
            ])
        ))

        # ── Activity & Graphs ─────────────────────────────────────────────────
        pages.append(_page("Activity & Graphs",
            "Charts and graphs for understanding server engagement. "
            "Requires **Manage Server** permission.\n\n"
            + _fmt([
                ("/activity resolution:day mode:messages member:@user channel:#channel",
                 "Bar chart of message volume (or XP earned) over time — server-wide or scoped to a member/channel. "
                 "Resolutions: hour, day, week, month, hour_of_day, day_of_week. "
                 "`mode:xp` charts XP earned instead of message count. "
                 "Times are shown in the server's configured timezone (set via `/config global`)."),
                ("/session_burst member:@user",
                 "Histogram of how active a member is in the 60 minutes after returning from a "
                 "20-min break, compared to their idle baseline. "
                 "Reveals whether someone energises conversation or tends to post quietly."),
                ("/connection_web member:@user timescale:week min_pct:5 layers:2 limit:40",
                 "Community-clustered network graph of replies and @mentions.\n"
                 "— `timescale` limits to recent interactions (hour/day/week/month) or all time.\n"
                 "— `min_pct` hides edges below X% of either user's total interaction volume.\n"
                 "— `member:@user` focuses on one person's connections.\n"
                 "— `layers` controls how many hops to expand from the focused member.\n"
                 "— `limit` caps the number of members in the server-wide view.\n"
                 "— `spread` adjusts visual spacing.\n"
                 "— `max_per_node` keeps only the top N edges per node."),
                ("/chilling_effect lookback_days:30 top:10",
                 "Find members whose arrival in a channel causes others to stop posting. "
                 "Compares activity before and after each member's entries."),
                ("/invite_web member:@user",
                 "Network graph showing who invited whom. "
                 "Optionally focus on one member's invite chain."),
            ])
        ))

        # ── Fun ───────────────────────────────────────────────────────────────
        pages.append(_page("Fun",
            "Seasonal and fun mod tools.\n\n"
            + _fmt([
                ("/foolsday action:shuffle",
                 "April Fools name shuffle — randomises nicknames among members active "
                 "in at least 3 of the last 5 days. Use `action:restore` to undo."),
                ("/foolsday_exclude user:@User",
                 "Exclude another user (self-exclude is in General)."),
                ("/foolsday_join user:@User",
                 "Add another user to the active shuffle (self-join is in General)."),
                ("/foolsday_include user",
                 "Remove a user from the exclusion list."),
                ("/foolsday_exclusions",
                 "List all excluded users."),
                ("/foolsday_samename name",
                 "Set every shuffled member to one name (custom or random from the saved pool). "
                 "Use `/foolsday action:restore` to undo."),
                ("/foolsday_repair",
                 "Fix broken name mappings — walks through each saved name and lets you assign the correct user."),
            ])
        ))

        # ── Data Management ───────────────────────────────────────────────────
        pages.append(_page("Data Management",
            "Admin tools for populating and maintaining bot data.\n\n"
            + _fmt([
                ("/interaction_scan days:0 reset:True",
                 "Backfills the interaction graph and message archive (content, replies, reactions, "
                 "attachments, @mentions) from Discord's message history. "
                 "Run once after first setup — the bot records new activity live from then on. "
                 "`days:0` scans all available history. "
                 "`reset:True` wipes existing data before scanning — use this to fix inflated "
                 "counts if the scan was run multiple times over the same period."),
                ("/gender set member:@user gender:Female",
                 "Tag a member as Male, Female, or Non-binary. "
                 "Used by `/report nsfw_gender` to break down channel posting by gender."),
                ("/gender check member:@user",
                 "Show a member's current gender classification."),
                ("/gender classify",
                 "Walk through every unclassified member one at a time with a picker."),
            ])
        ))

        # ── Watch List ────────────────────────────────────────────────────────
        pages.append(_page("Watch List",
            "Silently monitor a member's public messages. "
            "If `ANTHROPIC_API_KEY` is set, only messages the AI flags as potential rule violations "
            "are forwarded — otherwise every message is relayed.\n\n"
            + _fmt([
                ("/watch add user:@user",
                 "Start monitoring a member. Every public message they post in a channel "
                 "the bot can read is forwarded to your DMs (filtered by AI if configured)."),
                ("/watch remove user:@user", "Stop monitoring a member."),
                ("/watch list", "Show every member you are currently watching."),
            ])
        ))

        # ── AI Moderation ─────────────────────────────────────────────────────
        pages.append(_page("AI Moderation",
            _fmt([
                ("/ai review member:@user days:7",
                 "Pulls a member's recent messages and asks the AI to flag rule violations, "
                 "concerning patterns, or escalating behaviour. "
                 "Use this before taking moderation action to build a clear picture."),
                ("/ai scan count:50",
                 "Scans the last N messages in this channel and flags any that may breach the rules. "
                 "Good for reviewing a channel after a reported incident or a heated argument."),
                ("/ai channel question:... minutes:60 channel:#channel",
                 "Ask the AI a free-form question about a channel's recent activity over a time window — "
                 "e.g. 'Did anyone harass a new member in the last hour?' or "
                 "'Summarise what this argument was about.' "
                 "Defaults to the current channel; supply `channel` to query another. "
                 "`minutes` accepts 1–1440 (up to 24 hours)."),
                ("/ai query member:@user question:... days:14",
                 "Ask the AI a free-form question about a member based on their message history — "
                 "e.g. 'Has this member been hostile toward new users?' or "
                 "'Does this person engage constructively or mostly argue?' "
                 "Useful when you need a specific answer, not a full review."),
            ])
        ))

        # ── Configuration ─────────────────────────────────────────────────────
        pages.append(_page("Configuration",
            "All bot settings are managed through `/config <section>`. "
            "Each section opens a modal or interactive panel.\n\n"
            "**🌐 Web dashboard:** admins can also configure the bot, view reports, and "
            "manage moderation from the web UI. If the dashboard is deployed it's reachable "
            "at the configured base URL; sign in with Discord.\n\n"
            "**`/config global`** — Timezone, mod channel, bypass roles.\n"
            "**`/config roles`** — Grant roles (add/edit/remove), log/announce channels, "
            "permissions, and grant message templates.\n"
            "**`/config xp`** — XP log channels, level-5 announcement channel, "
            "per-channel XP toggle, and grant allowlist.\n"
            "**`/config welcome`** — Welcome and leave channels + message templates. "
            "Supports `{member}`, `{name}`, `{server}` placeholders.\n"
            "**`/config spoiler`** — Spoiler-guard channel list + current-channel toggle.\n"
            "**`/config prune`** — Inactivity prune role, threshold, and manual trigger.\n"
            "**`/config booster`** — Booster cosmetic role picker. "
            "Manage swatch images, sync roles from a directory, and post the picker panel.\n\n"
            "**Related commands**\n"
            + _fmt([
                ("/xp_excluded_channels", "List channels where XP is disabled."),
                ("/xp_backfill_history days:30",
                 "Scan message history to fill gaps in XP and activity data."),
                ("/welcome_preview", "Preview the welcome message with your profile."),
                ("/leave_preview", "Preview the leave message with your profile."),
                ("/inactivity_prune status",
                 "Show current prune threshold, role, schedule, and exemption list."),
                ("/inactivity_prune exempt member:@user",
                 "Protect a member from being pruned."),
                ("/inactivity_prune unexempt member:@user",
                 "Remove a member's prune exemption."),
                ("/inactivity_prune run",
                 "Trigger an immediate prune run instead of waiting for the daily schedule."),
                ("/auto_delete del_age:30d run:1d",
                 "Delete messages older than `del_age` on a repeating schedule. "
                 "Accepts `15m`, `2h`, `30d`, `1h30m`, `once`, or `off`."),
                ("/auto_delete_configs",
                 "List every active auto-delete schedule across the server."),
                ("/quality_leave add member:@user days:30",
                 "Put a member on leave of absence (pauses quality scoring)."),
                ("/quality_leave remove member:@user",
                 "Remove a member's leave of absence."),
                ("/quality_leave list",
                 "List all members currently on leave."),
            ])
        ))

    return pages


class HelpSelect(discord.ui.Select):
    def __init__(self, pages: list[discord.Embed], invoker_id: int):
        self.pages = pages
        self.invoker_id = invoker_id
        options = []
        for i, p in enumerate(pages):
            title = p.title or ""
            # Title format: "{emoji}  {name}"
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


def register_mod_commands(bot: Bot, ctx: AppContext) -> None:
    @bot.tree.command(name="help", description="Show command reference and examples.")
    async def help_command(interaction: discord.Interaction):
        pages = _build_help_pages(ctx, interaction)
        view = HelpView(pages, invoker_id=interaction.user.id)
        await interaction.response.send_message(embed=view.current_embed(), view=view, ephemeral=True)

    @bot.tree.command(
        name="purge",
        description="Delete messages in this channel. No arguments clears the entire channel.",
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(
        count="Number of messages to delete (max 1000). Omit to delete all messages since `after`.",
        after="Delete messages at or after this time today (server local time), e.g. 19:35.",
    )
    async def purge(
        interaction: discord.Interaction,
        count: app_commands.Range[int, 1, 1000] | None = None,
        after: str | None = None,
    ):
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
                    # Time hasn't occurred yet today — treat as yesterday
                    after_dt -= timedelta(days=1)
            except (ValueError, IndexError):
                tz_label = f"UTC{ctx.tz_offset_hours:+g}" if ctx.tz_offset_hours != 0 else "UTC"
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
