"""General moderation and help commands."""
from __future__ import annotations

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
            ("/activity resolution:day",
             "Bar chart of message volume over time — server-wide or for one member. "
             "Useful for spotting quiet periods, activity spikes, or a member's posting rhythm. "
             "Resolutions: hour, day, week."),
            ("/session_burst member:@user",
             "Histogram of how active a member is in the 60 minutes after returning from a "
             "20-min break, compared to their idle baseline. "
             "Reveals whether someone energises conversation or tends to post quietly."),
            ("/connection_web",
             "Who replies to and @mentions whom. Renders a weighted network graph of interactions "
             "across the server. Add `member:@user` to zoom in on one person — their direct "
             "contacts appear in blue, extended connections in green. "
             "Use `timescale` to limit the graph to recent activity."),
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
            "Inspect member activity and role membership.\n\n"
            + _fmt([
                ("/listrole role:@Role",
                 "List every current holder of a role — handy before mass-messaging, "
                 "auditing permissions, or checking coverage."),
                ("/inactive_role role:@Role days:7",
                 "Members of a role who haven't posted in N days. "
                 "Good for identifying who might need a check-in before a prune run."),
                ("/report_inactive time_period:7d",
                 "All server members inactive for a given period, regardless of role. "
                 "Use this to plan prune runs or spot disengaged members early."),
                ("/oldest_sfw_members count:10",
                 "Members without NSFW access, ranked by how long since they last posted. "
                 "Useful for finding long-inactive accounts that were never fully onboarded."),
                ("/xp_level_review level:5",
                 "Histogram of how many days members take to reach a given XP level, "
                 "with mean, mode, and std dev overlaid. "
                 "Use this to judge whether your XP thresholds feel earned or too fast."),
                ("/purge count:50",
                 "Delete the last N messages in this channel. "
                 "Omit N to delete all recent messages. "
                 "Useful for clearing spam, failed bot responses, or accidental posts."),
            ])
        ))

        # ── Activity & Graphs ─────────────────────────────────────────────────
        pages.append(_page("Activity & Graphs",
            "Charts and graphs for understanding server engagement.\n\n"
            "**Message Rate**\n"
            + _fmt([
                ("/dropoff period:week limit:10",
                 "Compares message counts across two equal consecutive time windows and surfaces "
                 "members with the steepest drop. "
                 "A week-over-week dropoff is often the earliest signal of disengagement "
                 "before someone goes fully quiet."),
            ])
            + "\n\n**Session Burst**\n"
            + _fmt([
                ("/burst_ranking limit:5",
                 "Server-wide ranking of who most reliably drives conversation after returning "
                 "from a 20-min break, and who tends to post without pulling others in. "
                 "Shows the top and bottom N members side by side — useful for identifying "
                 "your community's key conversation starters."),
            ])
            + "\n\n**Interaction Web**\n"
            + _fmt([
                ("/connection_web member:@user timescale:week min_pct:5 layers:2 limit:40",
                 "Network graph of replies and @mentions, with edge thickness weighted by frequency. "
                 "Use it to understand social structure, spot isolated members, or trace how "
                 "conversation flows between cliques.\n"
                 "— `timescale` limits the graph to recent interactions (hour/day/week/month) or "
                 "all time. Comparing week vs all-time reveals whether the social graph has shifted.\n"
                 "— `min_pct` hides edges below X% of either user's total interaction volume. "
                 "Raise it to show only strong recurring bonds; drop it to 1–2% to expose weak ties.\n"
                 "— `member:@user` focuses the graph on one person. Direct contacts appear in blue; "
                 "extended connections in green.\n"
                 "— `layers` controls how many hops to expand from the focused member. "
                 "`layers:1` shows only direct contacts; `layers:3` reveals their wider network.\n"
                 "— `limit` caps the number of members in the server-wide view — lower on large servers.\n"
                 "— `spread` adjusts visual spacing. Raise if nodes overlap; lower to tighten a sparse graph."),
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
                ("/watch_user user:@user",
                 "Start monitoring a member. Every public message they post in a channel "
                 "the bot can read is forwarded to your DMs (filtered by AI if configured)."),
                ("/unwatch_user user:@user", "Stop monitoring a member."),
                ("/watch_list", "Show every member you are currently watching."),
            ])
        ))

        # ── AI Moderation ─────────────────────────────────────────────────────
        pages.append(_page("AI Moderation",
            "AI-powered tools for reviewing user behaviour and channel activity. "
            "Requires `OPENAI_API_KEY` in the bot's environment. "
            "All commands use the stored message archive — run `/interaction_scan` first "
            "if the bot is newly set up.\n\n"
            + _fmt([
                ("/ai_review member:@user days:7",
                 "Pulls a member's recent messages and asks the AI to flag rule violations, "
                 "concerning patterns, or escalating behaviour. "
                 "Use this before taking moderation action to build a clear picture."),
                ("/ai_scan count:50",
                 "Scans the last N messages in this channel and flags any that may breach the rules. "
                 "Good for reviewing a channel after a reported incident or a heated argument."),
                ("/ai_query member:@user question:... days:14",
                 "Ask the AI a free-form question about a member based on their message history — "
                 "e.g. 'Has this member been hostile toward new users?' or "
                 "'Does this person engage constructively or mostly argue?' "
                 "Useful when you need a specific answer, not a full review."),
            ])
        ))

        # ── Role grant config ─────────────────────────────────────────────────
        pages.append(_page("Role Grant Config",
            "Configure which roles are granted by the bot and where those grants are "
            "logged and announced. Each role type (Greeter, Denizen, NSFW, Veteran) has its "
            "own role ID, log channel, announcement channel, and grant message template.\n\n"
            + _fmt([
                ("/config roles",
                 "Open the roles config panel. Select a role type to update its settings. "
                 "The grant message template supports `{member}` and `{server}` placeholders."),
            ])
        ))

        # ── XP config ─────────────────────────────────────────────────────────
        pages.append(_page("XP Config",
            "Control how XP is earned, who can award it manually, and where progress is logged.\n\n"
            "**Manual Grant Allowlist**\n"
            + _fmt([
                ("/xp_give_allow member:@user",
                 "Add a member to the `/xp_give` allowlist so they can manually award XP "
                 "without being a mod — useful for event hosts or community managers."),
                ("/xp_give_disallow member:@user", "Remove a member from the allowlist."),
                ("/xp_give_allowed", "Show everyone currently on the allowlist."),
            ])
            + "\n\n**Log Channels & Channel Exclusions**\n"
            + _fmt([
                ("/config xp",
                 "Open the XP config panel — set the level-up log channel, the level-5 "
                 "announcement channel, and toggle XP earning on or off for the current channel. "
                 "Disable XP in bot-spam or off-topic channels to keep it meaningful."),
                ("/xp_excluded_channels",
                 "List every channel where XP is currently disabled."),
            ])
            + "\n\n**History**\n"
            + _fmt([
                ("/xp_backfill_history days:30",
                 "Scans message history to fill gaps in XP and activity data — "
                 "useful after adding the bot to an existing server or after downtime."),
            ])
        ))

        # ── Welcome / Leave config ─────────────────────────────────────────────
        pages.append(_page("Welcome & Leave",
            "Customise the messages posted when members join or leave the server. "
            "Templates support `{member}` (mention), `{name}` (display name), "
            "and `{server}` (server name) placeholders.\n\n"
            + _fmt([
                ("/config welcome",
                 "Open the welcome & leave config modal — set channel IDs and message "
                 "templates for both events in one form."),
                ("/welcome_preview",
                 "Preview how the welcome message will look using your own profile, "
                 "without posting it publicly."),
                ("/leave_preview",
                 "Preview how the leave message will look using your own profile."),
            ])
        ))

        # ── Spoiler guard ─────────────────────────────────────────────────────
        pages.append(_page("Spoiler Guard",
            "Auto-deletes unspoilered images posted in designated channels and notifies "
            "the author. Useful for media-sharing or episode-discussion channels where "
            "spoilers need to be hidden.\n\n"
            + _fmt([
                ("/config spoiler",
                 "Open the spoiler guard panel — shows which channels are currently guarded "
                 "and lets you guard or unguard the current channel with one click."),
            ])
        ))

        # ── Inactivity prune ──────────────────────────────────────────────────
        pages.append(_page("Inactivity Prune",
            "Automatically removes a role from members who haven't posted in N days, "
            "then re-grants it when they return. "
            "Runs once daily at midnight UTC — no manual intervention needed once configured.\n\n"
            "**Setup**\n"
            + _fmt([
                ("/config prune",
                 "Open the prune config panel — set the role to manage and the inactivity "
                 "threshold in days, disable the prune entirely, or trigger an immediate run."),
                ("/inactivity_prune_status",
                 "Show the current threshold, the last run time, and the full exemption list."),
            ])
            + "\n\n**Exemptions**\n"
            + _fmt([
                ("/inactivity_prune_exempt member:@user",
                 "Permanently protect a member from being pruned — useful for bots, staff, "
                 "or members on a known hiatus who should keep their role."),
                ("/inactivity_prune_unexempt member:@user",
                 "Remove a member's exemption, returning them to normal prune eligibility."),
            ])
        ))

        # ── Auto-delete ───────────────────────────────────────────────────────
        pages.append(_page("Auto-Delete",
            "Deletes old messages in a channel on a repeating schedule. "
            "Useful for keeping announcement channels clean, enforcing message lifetimes "
            "in high-volume channels, or automatically clearing temporary event threads.\n"
            "`del_age` and `run` accept values like `15m`, `2h`, `30d`, `1h30m`. "
            "`run` also accepts `once` (run immediately and stop) or `off` (disable).\n\n"
            + _fmt([
                ("/auto_delete del_age:30d run:1d",
                 "Delete messages older than `del_age` from this channel, "
                 "then repeat on the `run` interval. "
                 "Each channel can have its own schedule."),
                ("/auto_delete_configs",
                 "List every active auto-delete schedule across the server, "
                 "including the channel, age threshold, and next run time."),
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
        description="Delete the last N messages in this channel, or all recent messages if N is omitted.",
    )
    @app_commands.describe(count="Number of messages to delete. Omit to delete all recent messages.")
    async def purge(
        interaction: discord.Interaction,
        count: app_commands.Range[int, 1, 1000] | None = None,
    ):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
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

        deleted = await channel.purge(limit=count)
        label = f"last {count}" if count is not None else "all recent"
        await interaction.followup.send(
            f"Deleted {len(deleted)} message{'s' if len(deleted) != 1 else ''} ({label}).",
            ephemeral=True,
        )
