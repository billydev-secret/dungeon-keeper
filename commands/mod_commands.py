"""General moderation and help commands."""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from app_context import AppContext, Bot


def _fmt(command_specs: list[tuple[str, str]]) -> str:
    return "\n".join(f"`{name}`\n{desc}" for name, desc in command_specs)


# (label, emoji, color, intro, [(command, description), ...])
_SECTION_META: dict[str, tuple[str, discord.Color]] = {
    "General":          ("🌿", discord.Color.from_str("#5865F2")),
    "Role Grants":      ("🎭", discord.Color.from_str("#57F287")),
    "XP Grant":         ("⭐", discord.Color.from_str("#FEE75C")),
    "Reports":          ("📊", discord.Color.from_str("#EB459E")),
    "Role Grant Config":("⚙️",  discord.Color.from_str("#1ABC9C")),
    "XP Config":        ("🔧", discord.Color.from_str("#2ECC71")),
    "Welcome & Leave":  ("👋", discord.Color.from_str("#9B59B6")),
    "Spoiler Guard":    ("🛡️",  discord.Color.from_str("#E74C3C")),
    "Inactivity Prune": ("✂️",  discord.Color.from_str("#E67E22")),
    "Auto-Delete":      ("🗑️",  discord.Color.from_str("#992D22")),
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
            ])
        ))

        # ── Role grant config ─────────────────────────────────────────────────
        pages.append(_page("Role Grant Config",
            "Configure which roles are granted and where grants are logged or announced.\n\n"
            "**Greeter**\n"
            + _fmt([
                ("/set_greeter_role role:@Role", "Allow this role to use grant commands alongside mods."),
            ])
            + "\n\n**Denizen**\n"
            + _fmt([
                ("/set_denizen_role role:@Role", "Role assigned by `/grant_denizen`."),
                ("/set_denizen_log_here · /denizen_log_disable", "Log grants in this channel / turn off logging."),
                ("/set_denizen_announce_here · /denizen_announce_disable", "Post grant announcements here / turn off."),
                ("/set_denizen_message", "Customise the grant announcement template."),
            ])
            + "\n\n**NSFW**\n"
            + _fmt([
                ("/set_nsfw_role role:@Role", "Role assigned by `/grant_nsfw`."),
                ("/set_nsfw_log_here · /nsfw_log_disable", "Log grants here / turn off."),
                ("/set_nsfw_announce_here · /nsfw_announce_disable", "Post announcements here / turn off."),
                ("/set_nsfw_message", "Customise the grant announcement template."),
            ])
            + "\n\n**Veteran**\n"
            + _fmt([
                ("/set_veteran_role role:@Role", "Role assigned by `/grant_veteran`."),
                ("/set_veteran_log_here · /veteran_log_disable", "Log grants here / turn off."),
                ("/set_veteran_announce_here · /veteran_announce_disable", "Post announcements here / turn off."),
                ("/set_veteran_message", "Customise the grant announcement template."),
            ])
        ))

        # ── XP config ─────────────────────────────────────────────────────────
        pages.append(_page("XP Config",
            "Control how XP is earned and tracked across the server.\n\n"
            + _fmt([
                ("/xp_give_allow member:@user", "Add a member to the `/xp_give` allowlist."),
                ("/xp_give_disallow member:@user", "Remove a member from the allowlist."),
                ("/xp_give_allowed", "Show everyone currently on the allowlist."),
                ("/xp_set_levelup_log_here", "Log every level-up event in this channel."),
                ("/xp_set_level5_log_here", "Log level 5 milestone grants here."),
                ("/xp_exclude_here", "Stop XP from being earned in this channel or thread."),
                ("/xp_include_here", "Re-enable XP in a previously excluded channel or thread."),
                ("/xp_excluded_channels", "List every channel where XP is currently disabled."),
                ("/xp_backfill_history days:30", "Scan message history to fill gaps in XP and activity data."),
            ])
        ))

        # ── Welcome / Leave config ─────────────────────────────────────────────
        pages.append(_page("Welcome & Leave",
            "Customise the messages posted when members join or leave.\n\n"
            "**Welcome**\n"
            + _fmt([
                ("/welcome_set_here", "Post welcome messages in this channel."),
                ("/welcome_disable", "Turn off welcome messages."),
                ("/welcome_set_message", "Edit the welcome message template."),
                ("/welcome_preview", "Preview how the welcome message looks with your profile."),
            ])
            + "\n\n**Leave**\n"
            + _fmt([
                ("/leave_set_here", "Post leave messages in this channel."),
                ("/leave_disable", "Turn off leave messages."),
                ("/leave_set_message", "Edit the leave message template."),
                ("/leave_preview", "Preview how the leave message looks with your profile."),
            ])
        ))

        # ── Spoiler guard ─────────────────────────────────────────────────────
        pages.append(_page("Spoiler Guard",
            "Auto-delete unspoilered images in designated channels.\n\n"
            + _fmt([
                ("/spoiler_guard_add_here", "Require spoilers on all images posted in this channel or thread."),
                ("/spoiler_guard_remove_here", "Remove the spoiler requirement from this channel or thread."),
                ("/spoiler_guarded_channels", "List every channel currently under spoiler guard."),
            ])
        ))

        # ── Inactivity prune ──────────────────────────────────────────────────
        pages.append(_page("Inactivity Prune",
            "Automatically remove a role from members who haven't posted in N days. "
            "Runs once daily at midnight UTC.\n\n"
            + _fmt([
                ("/inactivity_prune_setup role:@Role days:30", "Configure the role and inactivity threshold."),
                ("/inactivity_prune_disable", "Turn off the scheduled prune."),
                ("/inactivity_prune_status", "Show the current config and full exemption list."),
                ("/inactivity_prune_exempt member:@user", "Protect a member from ever being pruned."),
                ("/inactivity_prune_unexempt member:@user", "Remove a member's exemption."),
                ("/inactivity_prune_run", "Run the prune right now without waiting for midnight."),
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
