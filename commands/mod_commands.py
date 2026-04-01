"""General moderation and help commands."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands

if TYPE_CHECKING:
    from app_context import AppContext, Bot


def _fmt(command_specs: list[tuple[str, str]]) -> str:
    return "\n".join(f"`{name}`\n{desc}" for name, desc in command_specs)


# (label, emoji, color, intro, [(command, description), ...])
_SECTION_META: dict[str, tuple[str, discord.Color]] = {
    "General":           ("🌿", discord.Color.from_str("#5865F2")),
    "Role Grants":       ("🎭", discord.Color.from_str("#57F287")),
    "XP Grant":          ("⭐", discord.Color.from_str("#FEE75C")),
    "Reports":           ("📊", discord.Color.from_str("#EB459E")),
    "Activity & Graphs": ("📈", discord.Color.from_str("#5DADE2")),
    "Watch List":        ("🔍", discord.Color.from_str("#3498DB")),
    "Role Grant Config": ("⚙️",  discord.Color.from_str("#1ABC9C")),
    "XP Config":         ("🔧", discord.Color.from_str("#2ECC71")),
    "Welcome & Leave":   ("👋", discord.Color.from_str("#9B59B6")),
    "Spoiler Guard":     ("🛡️",  discord.Color.from_str("#E74C3C")),
    "Inactivity Prune":  ("✂️",  discord.Color.from_str("#E67E22")),
    "Auto-Delete":       ("🗑️",  discord.Color.from_str("#992D22")),
    "AI Moderation":     ("🤖", discord.Color.from_str("#5865F2")),
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
             "Top XP earners for a chosen time window (hour / day / week / month / all time), "
             "plus your own rank within that period."),
            ("/give_role member:@user role:@Role",
             "Give a role to a member. Mods can give any role; other users need to be "
             "authorised for specific roles via `/give_role_allow`."),
        ])
    ))

    # ── Greeter (greeter role or mod) ─────────────────────────────────────────
    if ctx.can_grant_denizen(interaction):
        pages.append(_page("Role Grants",
            "Grant community roles to members. Available to greeters and mods.\n\n"
            + _fmt([
                ("/grant_denizen member:@user",
                 "Welcome a new member by granting them the Denizen role, "
                 "opening up the main community channels."),
                ("/grant_nsfw member:@user",
                 "Grant access to age-restricted channels after confirming eligibility."),
                ("/grant_veteran member:@user",
                 "Recognise a longtime member with the Veteran role."),
                ("/grant_kink member:@user",
                 "Grant the Kink role to a member."),
                ("/grant_goldengirl member:@user",
                 "Grant the Golden Girl role to a member."),
            ])
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
                ("/report inactive time_period:7d",
                 "All server members inactive for a given period, regardless of role. "
                 "Use this to plan prune runs or spot disengaged members early."),
                ("/report oldest_sfw count:10",
                 "Members without NSFW access, ranked by how long since they last posted. "
                 "Useful for finding long-inactive accounts that were never fully onboarded."),
                ("/xp_level_review level:5",
                 "Histogram of how many days members take to reach a given XP level, "
                 "with mean, mode, and std dev overlaid. "
                 "Use this to judge whether your XP thresholds feel earned or too fast."),
                ("/purge count:50",
                 "Delete the last N messages in this channel. "
                 "Use `after:19:35` to delete everything since a UTC time today — "
                 "both can be combined. "
                 "Useful for clearing spam, failed bot responses, or accidental posts."),
                ("/dropoff period:week limit:10",
                 "Compares message counts across two consecutive time windows and surfaces "
                 "members with the steepest drop. "
                 "A week-over-week dropoff is often the earliest signal of disengagement."),
                ("/burst_ranking limit:5",
                 "Server-wide ranking of who most reliably drives conversation after returning "
                 "from a 20-min break, and who tends to post without pulling others in."),
                ("/interaction_heatmap timescale:week min_pct:5 limit:30",
                 "Adjacency-matrix heatmap of reply/mention interactions between members. "
                 "Users are sorted by total interaction volume; colour intensity shows weight. "
                 "A cleaner alternative to the network graph for dense servers."),
                ("/report role_growth resolution:week",
                 "Chart of cumulative role grants over time. "
                 "Resolutions: daily (30d), weekly (12wk), monthly (12mo)."),
                ("/report promotion_review",
                 "Members above level 5 without spicy access — flags inactivity-pruned users."),
            ])
        ))

        # ── Activity & Graphs ─────────────────────────────────────────────────
        pages.append(_page("Activity & Graphs",
            "Charts and graphs for understanding server engagement. "
            "Requires **Manage Server** permission.\n\n"
            + _fmt([
                ("/activity resolution:day",
                 "Bar chart of message volume over time — server-wide or for one member. "
                 "Resolutions: hour, day, week, month, hour_of_day, day_of_week. "
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
            ])
        ))

        # ── Fun ───────────────────────────────────────────────────────────────
        pages.append(_page("Fun",
            "Seasonal and fun mod tools.\n\n"
            + _fmt([
                ("/foolsday action:shuffle",
                 "April Fools name shuffle — randomises nicknames among members active "
                 "in at least 3 of the last 5 days. Reshuffles every hour. Use `action:restore` to stop and undo."),
                ("/foolsday_exclude user",
                 "Exclude a user from the name shuffle."),
                ("/foolsday_include user",
                 "Remove a user from the exclusion list."),
                ("/foolsday_exclusions",
                 "List all excluded users."),
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
            ])
        ))

        # ── Watch List ────────────────────────────────────────────────────────
        pages.append(_page("Watch List",
            "Silently monitor a member's public messages. "
            "If `OPENAI_API_KEY` is set, only messages the AI flags as potential rule violations "
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
            "AI-powered tools for reviewing user behaviour and channel activity. "
            "Requires `OPENAI_API_KEY` in the bot's environment. "
            "All commands use the stored message archive — run `/interaction_scan` first "
            "if the bot is newly set up.\n\n"
            + _fmt([
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
            "**`/config global`** — Timezone, mod channel, bypass roles.\n"
            "**`/config roles`** — Role IDs, log/announce channels, and grant message "
            "templates for Greeter, Denizen, NSFW, Veteran, Kink, Golden Girl.\n"
            "**`/config xp`** — XP log channels, level-5 announcement channel, "
            "per-channel XP toggle, and grant allowlist.\n"
            "**`/config welcome`** — Welcome and leave channels + message templates. "
            "Supports `{member}`, `{name}`, `{server}` placeholders.\n"
            "**`/config spoiler`** — Spoiler-guard channel list + current-channel toggle.\n"
            "**`/config prune`** — Inactivity prune role, threshold, and manual trigger.\n\n"
            "**Related commands**\n"
            + _fmt([
                ("/xp_excluded_channels", "List channels where XP is disabled."),
                ("/xp_backfill_history days:30",
                 "Scan message history to fill gaps in XP and activity data."),
                ("/welcome_preview", "Preview the welcome message with your profile."),
                ("/leave_preview", "Preview the leave message with your profile."),
                ("/inactivity_prune status",
                 "Show current prune threshold, last run time, and exemption list."),
                ("/inactivity_prune exempt member:@user",
                 "Protect a member from being pruned."),
                ("/inactivity_prune unexempt member:@user",
                 "Remove a member's prune exemption."),
                ("/auto_delete del_age:30d run:1d",
                 "Delete messages older than `del_age` on a repeating schedule. "
                 "Accepts `15m`, `2h`, `30d`, `1h30m`, `once`, or `off`."),
                ("/auto_delete_configs",
                 "List every active auto-delete schedule across the server."),
                ("/give_role_allow role_to_give:@Role allowed:@user_or_role",
                 "Authorise a user or role to give a specific role via `/give_role`."),
                ("/give_role_deny role_to_give:@Role denied:@user_or_role",
                 "Remove a user or role's `/give_role` permission."),
                ("/give_role_list",
                 "List all `/give_role` permission rules."),
            ])
        ))

    return pages


class HelpSelect(discord.ui.Select):
    def __init__(self, pages: list[discord.Embed], invoker_id: int):
        self.pages = pages
        self.invoker_id = invoker_id
        options = [
            discord.SelectOption(
                label=(p.title or "").lstrip("🌿🎭⭐📊⚙️🔧👋🛡️✂️🗑️📖 "),
                emoji=(p.title or " ")[0],
                value=str(i),
            )
            for i, p in enumerate(pages)
        ]
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
        description="Delete messages in this channel by count, cutoff time, or both.",
    )
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

        deleted = await channel.purge(limit=count, after=after_dt)

        if count is not None and after_dt is not None:
            label = f"last {count} since {after}"
        elif count is not None:
            label = f"last {count}"
        elif after_dt is not None:
            label = f"since {after}"
        else:
            label = "all recent"
        await interaction.followup.send(
            f"Deleted {len(deleted)} message{'s' if len(deleted) != 1 else ''} ({label}).",
            ephemeral=True,
        )
