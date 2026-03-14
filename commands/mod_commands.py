"""General moderation and help commands."""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from app_context import AppContext, Bot


def _format_help_lines(command_specs: list[tuple[str, str]]) -> str:
    return "\n".join(f"`{name}` - {description}" for name, description in command_specs)


def _build_help_pages(ctx: AppContext, interaction: discord.Interaction) -> list[discord.Embed]:
    pages: list[discord.Embed] = []

    def page(title: str, value: str) -> discord.Embed:
        return discord.Embed(
            title=f"Dungeon Keeper Help — {title}",
            description=value,
            color=discord.Color.blurple(),
        )

    # ── General (everyone) ────────────────────────────────────────────────────
    pages.append(page(
        "General",
        _format_help_lines([
            ("/help", "Show this guide."),
            ("/xp_leaderboards timescale:week", "View top XP and your rank for a time window."),
            ("/activity resolution:day", "Message activity chart for the server or a member."),
        ]),
    ))

    # ── Greeter (greeter role or mod) ─────────────────────────────────────────
    if ctx.can_grant_denizen(interaction):
        pages.append(page(
            "Role Grants",
            _format_help_lines([
                ("/grant_denizen member:@user", "Grant the Denizen role to a member."),
                ("/grant_nsfw member:@user", "Grant the NSFW role to a member."),
                ("/grant_veteran member:@user", "Grant the Veteran role to a member."),
            ]),
        ))

    # ── XP give (allowlisted users or mod) ────────────────────────────────────
    if ctx.can_use_xp_grant(interaction):
        pages.append(page(
            "XP Grant",
            _format_help_lines([
                ("/xp_give member:@user", "Give 20 XP manually to one member."),
            ]),
        ))

    if ctx.is_mod(interaction):

        # ── Reports ───────────────────────────────────────────────────────────
        pages.append(page(
            "Reports",
            _format_help_lines([
                ("/listrole role:@Role", "List all members with a given role."),
                ("/inactive_role role:@Role days:7", "Show role members inactive in the last N days."),
                ("/report_inactive time_period:7d", "Show all server members inactive for a given period."),
                ("/oldest_sfw_members count:10", "Show members without spicy access with oldest last messages."),
            ]),
        ))

        # ── Role grant config ─────────────────────────────────────────────────
        pages.append(page(
            "Role Grant Config",
            _format_help_lines([
                ("/set_greeter_role role:@Role", "Set which role can use grant commands."),
                ("/set_denizen_role role:@Role", "Role assigned by /grant_denizen."),
                ("/set_denizen_log_here", "Log Denizen grants here."),
                ("/denizen_log_disable", "Stop logging Denizen grants."),
                ("/set_denizen_announce_here", "Post Denizen grant message in this channel."),
                ("/denizen_announce_disable", "Stop posting Denizen grant messages."),
                ("/set_denizen_message", "Set the Denizen grant message template (opens editor)."),
                ("/set_nsfw_role role:@Role", "Role assigned by /grant_nsfw."),
                ("/set_nsfw_log_here", "Log NSFW grants here."),
                ("/nsfw_log_disable", "Stop logging NSFW grants."),
                ("/set_nsfw_announce_here", "Post NSFW grant message in this channel."),
                ("/nsfw_announce_disable", "Stop posting NSFW grant messages."),
                ("/set_nsfw_message", "Set the NSFW grant message template (opens editor)."),
                ("/set_veteran_role role:@Role", "Role assigned by /grant_veteran."),
                ("/set_veteran_log_here", "Log Veteran grants here."),
                ("/veteran_log_disable", "Stop logging Veteran grants."),
                ("/set_veteran_announce_here", "Post Veteran grant message in this channel."),
                ("/veteran_announce_disable", "Stop posting Veteran grant messages."),
                ("/set_veteran_message", "Set the Veteran grant message template (opens editor)."),
            ]),
        ))

        # ── XP config ─────────────────────────────────────────────────────────
        pages.append(page(
            "XP Config",
            _format_help_lines([
                ("/xp_give_allow member:@user", "Allow a member to use /xp_give."),
                ("/xp_give_disallow member:@user", "Remove /xp_give access from a member."),
                ("/xp_give_allowed", "Show the current /xp_give allowlist."),
                ("/xp_set_levelup_log_here", "Log all level-up events in this channel."),
                ("/xp_set_level5_log_here", "Log level 5 milestones in this channel."),
                ("/xp_exclude_here", "Disable XP gain in this channel/thread."),
                ("/xp_include_here", "Re-enable XP gain in this channel/thread."),
                ("/xp_excluded_channels", "List channels where XP is disabled."),
                ("/xp_backfill_history days:30", "Import historical message XP for the last N days."),
            ]),
        ))

        # ── Welcome / Leave config ─────────────────────────────────────────────
        pages.append(page(
            "Welcome & Leave",
            _format_help_lines([
                ("/welcome_set_here", "Post welcome messages in this channel."),
                ("/welcome_disable", "Disable welcome messages."),
                ("/welcome_set_message", "Edit the welcome message template (opens editor)."),
                ("/welcome_preview", "Preview the welcome message with your profile."),
                ("/leave_set_here", "Post leave messages in this channel."),
                ("/leave_disable", "Disable leave messages."),
                ("/leave_set_message", "Edit the leave message template (opens editor)."),
                ("/leave_preview", "Preview the leave message with your profile."),
            ]),
        ))

        # ── Spoiler guard ─────────────────────────────────────────────────────
        pages.append(page(
            "Spoiler Guard",
            _format_help_lines([
                ("/spoiler_guard_add_here", "Enable spoiler guard in this channel/thread."),
                ("/spoiler_guard_remove_here", "Disable spoiler guard in this channel/thread."),
                ("/spoiler_guarded_channels", "List channels with spoiler guard enabled."),
            ]),
        ))

        # ── Auto-delete ───────────────────────────────────────────────────────
        auto_delete_text = _format_help_lines([
            ("/auto_delete del_age:30d run:1d", "Delete old posts now and schedule repeats."),
            ("/auto_delete_configs", "List active auto-delete schedules."),
        ])
        auto_delete_text += (
            "\n\n`del_age` and `run` accept values like `15m`, `2h`, `30d`, `1h30m`.\n"
            "`run` also accepts `once` or `off`.\n"
            "Recurring runs only delete messages tracked after the rule is enabled."
        )
        pages.append(page("Auto-Delete", auto_delete_text))

    return pages


def _page_title(embed: discord.Embed) -> str:
    """Extract the section name from an embed title like 'Dungeon Keeper Help — Section'."""
    title = embed.title or ""
    return title.split(" — ", 1)[-1] if " — " in title else title


class HelpSelect(discord.ui.Select):
    def __init__(self, pages: list[discord.Embed], invoker_id: int):
        self.pages = pages
        self.invoker_id = invoker_id
        options = [
            discord.SelectOption(label=_page_title(p), value=str(i))
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
