"""General moderation and help commands."""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from app_context import AppContext, Bot


def _format_help_lines(command_specs: list[tuple[str, str]]) -> str:
    return "\n".join(f"`{name}` - {description}" for name, description in command_specs)


def _build_help_embed(ctx: AppContext, interaction: discord.Interaction) -> discord.Embed:
    embed = discord.Embed(
        title="Dungeon Keeper Help",
        description="Command guide for this server.",
        color=discord.Color.blurple(),
    )

    # ── General (everyone) ────────────────────────────────────────────────────
    embed.add_field(
        name="General",
        value=_format_help_lines(
            [
                ("/help", "Show this guide."),
                ("/xp_leaderboards timescale:week", "View top XP and your rank for a time window."),
                ("/activity resolution:day", "Message activity chart for the server or a member."),
            ]
        ),
        inline=False,
    )

    # ── Greeter (greeter role or mod) ─────────────────────────────────────────
    if ctx.can_grant_denizen(interaction):
        embed.add_field(
            name="Role Grants",
            value=_format_help_lines(
                [
                    ("/grant_denizen member:@user", "Grant the Denizen role to a member."),
                    ("/grant_nsfw member:@user", "Grant the NSFW role to a member."),
                    ("/grant_veteran member:@user", "Grant the Veteran role to a member."),
                ]
            ),
            inline=False,
        )

    # ── XP give (allowlisted users or mod) ────────────────────────────────────
    if ctx.can_use_xp_grant(interaction):
        embed.add_field(
            name="XP Grant",
            value=_format_help_lines(
                [("/xp_give member:@user", "Give 20 XP manually to one member.")]
            ),
            inline=False,
        )

    if ctx.is_mod(interaction):

        # ── Reports ───────────────────────────────────────────────────────────
        embed.add_field(
            name="Reports",
            value=_format_help_lines(
                [
                    ("/listrole role:@Role", "List all members with a given role."),
                    ("/inactive_role role:@Role days:7", "Show role members inactive in the last N days."),
                    ("/report_inactive time_period:7d", "Show all server members inactive for a given period."),
                ]
            ),
            inline=False,
        )

        # ── Role grant config ─────────────────────────────────────────────────
        embed.add_field(
            name="Role Grant Config",
            value=_format_help_lines(
                [
                    ("/set_greeter_role role:@Role", "Set which role can use grant commands."),
                    ("/set_denizen_role role:@Role", "Role assigned by /grant_denizen."),
                    ("/set_denizen_log_here", "Log Denizen grants here."),
                    ("/denizen_log_disable", "Stop logging Denizen grants."),
                    ("/set_denizen_message", "Set the message posted on Denizen grant (opens editor)."),
                    ("/set_nsfw_role role:@Role", "Role assigned by /grant_nsfw."),
                    ("/set_nsfw_log_here", "Log NSFW grants here."),
                    ("/nsfw_log_disable", "Stop logging NSFW grants."),
                    ("/set_nsfw_message", "Set the message posted on NSFW grant (opens editor)."),
                    ("/set_veteran_role role:@Role", "Role assigned by /grant_veteran."),
                    ("/set_veteran_log_here", "Log Veteran grants here."),
                    ("/veteran_log_disable", "Stop logging Veteran grants."),
                    ("/set_veteran_message", "Set the message posted on Veteran grant (opens editor)."),
                ]
            ),
            inline=False,
        )

        # ── XP config ─────────────────────────────────────────────────────────
        embed.add_field(
            name="XP Config",
            value=_format_help_lines(
                [
                    ("/xp_give_allow member:@user", "Allow a member to use /xp_give."),
                    ("/xp_give_disallow member:@user", "Remove /xp_give access from a member."),
                    ("/xp_give_allowed", "Show the current /xp_give allowlist."),
                    ("/xp_set_levelup_log_here", "Log all level-up events in this channel."),
                    ("/xp_set_level5_log_here", "Log level 5 milestones in this channel."),
                    ("/xp_exclude_here", "Disable XP gain in this channel/thread."),
                    ("/xp_include_here", "Re-enable XP gain in this channel/thread."),
                    ("/xp_excluded_channels", "List channels where XP is disabled."),
                    ("/xp_backfill_history days:30", "Import historical message XP for the last N days."),
                ]
            ),
            inline=False,
        )

        # ── Welcome / Leave config ─────────────────────────────────────────────
        embed.add_field(
            name="Welcome & Leave",
            value=_format_help_lines(
                [
                    ("/welcome_set_here", "Post welcome messages in this channel."),
                    ("/welcome_disable", "Disable welcome messages."),
                    ("/welcome_set_message", "Edit the welcome message template (opens editor)."),
                    ("/welcome_preview", "Preview the welcome message with your profile."),
                    ("/leave_set_here", "Post leave messages in this channel."),
                    ("/leave_disable", "Disable leave messages."),
                    ("/leave_set_message", "Edit the leave message template (opens editor)."),
                    ("/leave_preview", "Preview the leave message with your profile."),
                ]
            ),
            inline=False,
        )

        # ── Spoiler guard ─────────────────────────────────────────────────────
        embed.add_field(
            name="Spoiler Guard",
            value=_format_help_lines(
                [
                    ("/spoiler_guard_add_here", "Enable spoiler guard in this channel/thread."),
                    ("/spoiler_guard_remove_here", "Disable spoiler guard in this channel/thread."),
                    ("/spoiler_guarded_channels", "List channels with spoiler guard enabled."),
                ]
            ),
            inline=False,
        )

        # ── Auto-delete ───────────────────────────────────────────────────────
        embed.add_field(
            name="Auto-Delete",
            value=_format_help_lines(
                [
                    ("/auto_delete del_age:30d run:1d", "Delete old posts now and schedule repeats."),
                    ("/auto_delete_configs", "List active auto-delete schedules."),
                ]
            ),
            inline=False,
        )
        embed.add_field(
            name="Auto-Delete Notes",
            value=(
                "`del_age` and `run` accept values like `15m`, `2h`, `30d`, `1h30m`.\n"
                "`run` also accepts `once` or `off`.\n"
                "Recurring runs only delete messages tracked after the rule is enabled."
            ),
            inline=False,
        )

    embed.set_footer(text="Tip: Discord command prompts show parameter hints while you type.")
    return embed


def register_mod_commands(bot: Bot, ctx: AppContext) -> None:
    @bot.tree.command(name="help", description="Show command reference and examples.")
    async def help_command(interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=_build_help_embed(ctx, interaction), ephemeral=True
        )
