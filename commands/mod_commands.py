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
            ("/xp_leaderboards timescale:week", "Top XP earners for a time window, plus your own rank."),
            ("/activity resolution:day", "Bar chart of message activity — server-wide or for one member."),
            ("/session_burst member:@user",
             "Histogram of a member's message activity in the 60 min after returning "
             "from a 20-min absence, compared to the idle baseline."),
            ("/connection_web",
             "Visualise the web of replies and @mentions between members. "
             "Add `member:@user` to centre the graph on one person's direct connections."),
        ])
    ))

    # ── Greeter (greeter role or mod) ─────────────────────────────────────────
    if ctx.can_grant_denizen(interaction):
        pages.append(_page("Role Grants",
            "Grant community roles to members. Available to greeters and mods.\n\n"
            + _fmt([
                ("/grant_denizen member:@user", "Welcome a new member by granting them the Denizen role."),
                ("/grant_nsfw member:@user", "Grant access to spicy channels."),
                ("/grant_veteran member:@user", "Recognise a longtime member with the Veteran role."),
            ])
        ))

    # ── XP give (allowlisted users or mod) ────────────────────────────────────
    if ctx.can_use_xp_grant(interaction):
        pages.append(_page("XP Grant",
            "Manually award XP to a member. Requires mod or allowlist access.\n\n"
            + _fmt([
                ("/xp_give member:@user", "Give 20 XP to one member."),
            ])
        ))

    if ctx.is_mod(interaction):

        # ── Reports ───────────────────────────────────────────────────────────
        pages.append(_page("Reports",
            "Inspect member activity and role membership.\n\n"
            + _fmt([
                ("/listrole role:@Role", "List everyone currently holding a role."),
                ("/inactive_role role:@Role days:7", "Members of a role who haven't posted in N days."),
                ("/report_inactive time_period:7d", "All server members inactive for a given period."),
                ("/oldest_sfw_members count:10", "Members without spicy access, sorted by oldest last message."),
                ("/xp_level_review level:5",
                 "Histogram of how long members take to reach a given XP level, "
                 "with mean, mode, and std dev overlaid."),
                ("/purge count:50",
                 "Delete the last N messages in this channel (omit N to delete all recent messages)."),
            ])
        ))

        # ── Activity & Graphs ─────────────────────────────────────────────────
        pages.append(_page("Activity & Graphs",
            "Charts and graphs for understanding server engagement patterns.\n\n"
            "**Message Rate**\n"
            + _fmt([
                ("/dropoff period:week limit:10",
                 "Members with the largest drop in message count between two equal time windows."),
            ])
            + "\n\n**Session Burst**\n"
            + _fmt([
                ("/burst_ranking limit:5",
                 "Server-wide ranking of who drives the most (and least) activity after returning "
                 "from a 20-min absence. Shows top and bottom N members side by side."),
            ])
            + "\n\n**Interaction Web**\n"
            + _fmt([
                ("/connection_web member:@user min_pct:5 layers:2 limit:40",
                 "Spring-layout network graph of replies and @mentions between members. "
                 "Omit `member` for the full server view; supply it to focus on one person's connections. "
                 "`min_pct` hides edges below that % of either user's total interactions. "
                 "`layers` expands the graph recursively — each extra layer follows connections-of-connections that meet the threshold."),
                ("/interaction_scan days:0",
                 "Scan message history to seed the interaction graph. "
                 "Run once after setup, then the graph updates live. Use `days:0` for all history."),
            ])
        ))

        # ── Watch List ────────────────────────────────────────────────────────
        pages.append(_page("Watch List",
            "Silently monitor a member — their public messages are forwarded to you by DM.\n\n"
            + _fmt([
                ("/watch_user user:@user", "Start watching a member; their posts will be DM'd to you."),
                ("/unwatch_user user:@user", "Stop watching a member."),
                ("/watch_list", "Show every member you are currently watching."),
            ])
        ))

        # ── AI Moderation ─────────────────────────────────────────────────────
        pages.append(_page("AI Moderation",
            "AI-powered tools for reviewing user behaviour and channel activity. "
            "Requires `OPENAI_API_KEY` to be set in the bot's environment.\n\n"
            + _fmt([
                ("/ai_review member:@user days:7",
                 "Pull a user's recent messages and have the AI flag rule violations or concerns."),
                ("/ai_scan count:50",
                 "Have the AI scan the last N messages in this channel for problems."),
                ("/ai_query member:@user question:... days:14",
                 "Ask the AI a specific question about a user based on their message history."),
            ])
        ))

        # ── Role grant config ─────────────────────────────────────────────────
        pages.append(_page("Role Grant Config",
            "Configure which roles are granted and where grants are logged or announced.\n\n"
            + _fmt([
                ("/config roles",
                 "Open the roles config panel. Select Greeter, Denizen, NSFW, or Veteran to set "
                 "the role ID, log channel, announce channel, and grant message template."),
            ])
        ))

        # ── XP config ─────────────────────────────────────────────────────────
        pages.append(_page("XP Config",
            "Control how XP is earned and tracked across the server.\n\n"
            "**Allowlist**\n"
            + _fmt([
                ("/xp_give_allow member:@user", "Add a member to the `/xp_give` allowlist."),
                ("/xp_give_disallow member:@user", "Remove a member from the allowlist."),
                ("/xp_give_allowed", "Show everyone currently on the allowlist."),
            ])
            + "\n\n**Log Channels & Channel Exclusions**\n"
            + _fmt([
                ("/config xp",
                 "Open the XP config panel — set level-up and level-5 log channels, "
                 "and toggle XP on/off for the current channel."),
                ("/xp_excluded_channels", "List every channel where XP is currently disabled."),
            ])
            + "\n\n**History**\n"
            + _fmt([
                ("/xp_backfill_history days:30", "Scan message history to fill gaps in XP and activity data."),
            ])
        ))

        # ── Welcome / Leave config ─────────────────────────────────────────────
        pages.append(_page("Welcome & Leave",
            "Customise the messages posted when members join or leave.\n\n"
            + _fmt([
                ("/config welcome",
                 "Open the welcome & leave config modal — set channel IDs and message templates "
                 "for both welcome and leave messages in one form."),
                ("/welcome_preview", "Preview how the welcome message looks with your profile."),
                ("/leave_preview", "Preview how the leave message looks with your profile."),
            ])
        ))

        # ── Spoiler guard ─────────────────────────────────────────────────────
        pages.append(_page("Spoiler Guard",
            "Auto-delete unspoilered images in designated channels.\n\n"
            + _fmt([
                ("/config spoiler",
                 "Open the spoiler guard panel — shows guarded channels and lets you "
                 "guard or unguard the current channel."),
            ])
        ))

        # ── Inactivity prune ──────────────────────────────────────────────────
        pages.append(_page("Inactivity Prune",
            "Automatically remove a role from members who haven't posted in N days. "
            "Runs once daily at midnight UTC.\n\n"
            "**Setup**\n"
            + _fmt([
                ("/config prune",
                 "Open the prune config panel — set the role and inactivity threshold, "
                 "disable the prune, or run it immediately."),
                ("/inactivity_prune_status", "Show the current config and full exemption list."),
            ])
            + "\n\n**Exemptions**\n"
            + _fmt([
                ("/inactivity_prune_exempt member:@user", "Protect a member from ever being pruned."),
                ("/inactivity_prune_unexempt member:@user", "Remove a member's exemption."),
            ])
        ))

        # ── Auto-delete ───────────────────────────────────────────────────────
        pages.append(_page("Auto-Delete",
            "Delete old messages in a channel on a schedule.\n"
            "`del_age` and `run` accept values like `15m`, `2h`, `30d`, `1h30m`. "
            "`run` also accepts `once` or `off`.\n\n"
            + _fmt([
                ("/auto_delete del_age:30d run:1d", "Delete posts older than `del_age`, then repeat on the `run` "
                                                    "interval."),
                ("/auto_delete_configs", "List every active auto-delete schedule in this server."),
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
