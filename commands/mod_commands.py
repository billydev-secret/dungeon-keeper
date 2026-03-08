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
        description="Command guide for this server. Use the examples as templates and change the values.",
        color=discord.Color.blurple(),
    )

    embed.add_field(
        name="General",
        value=_format_help_lines(
            [
                ("/help", "Show this guide."),
                ("/xp_leaderboards timescale:week", "View top XP and your rank for a time window."),
            ]
        ),
        inline=False,
    )

    if ctx.can_grant_denizen(interaction):
        embed.add_field(
            name="Greeter",
            value=_format_help_lines(
                [("/grant_denizen member:@user", "Give the configured Denizen role to one member.")]
            ),
            inline=False,
        )

    if ctx.can_use_xp_grant(interaction):
        embed.add_field(
            name="XP Grant",
            value=_format_help_lines(
                [("/xp_give member:@user", "Give 20 XP manually to one member.")]
            ),
            inline=False,
        )

    if ctx.is_mod(interaction):
        embed.add_field(
            name="Moderation",
            value=_format_help_lines(
                [
                    ("/listrole role:@Role", "List members who currently have a role."),
                    ("/inactive_role role:@Role days:7", "Show role members inactive in the last N days."),
                ]
            ),
            inline=False,
        )
        embed.add_field(
            name="Configuration",
            value=_format_help_lines(
                [
                    ("/set_greeter_role role:@Role", "Choose who can run /grant_denizen."),
                    ("/set_denizen_role role:@Role", "Choose which role /grant_denizen gives."),
                    ("/xp_give_allow member:@user", "Allow a member to run /xp_give."),
                    ("/xp_give_disallow member:@user", "Remove /xp_give access from a member."),
                    ("/xp_give_allowed", "Show current /xp_give allowlist."),
                    ("/xp_set_levelup_log_here", "Run in a channel/thread to receive all level-up posts."),
                    ("/xp_set_level5_log_here", "Run in a channel/thread for level 5 alerts."),
                    ("/auto_delete del_age:30d run:1d", "Delete old posts now and schedule repeats."),
                    ("/auto_delete_configs", "List active auto-delete schedules in this server."),
                    ("/spoiler_guard_add_here", "Enable spoiler guard in this channel/thread."),
                    ("/spoiler_guard_remove_here", "Disable spoiler guard in this channel/thread."),
                    ("/spoiler_guarded_channels", "List channels/threads with spoiler guard enabled."),
                    ("/xp_exclude_here", "Disable XP gain in this channel/thread."),
                    ("/xp_include_here", "Re-enable XP gain in this channel/thread."),
                    ("/xp_excluded_channels", "List channels/threads where XP is off."),
                    ("/xp_backfill_history days:30", "Import historical message XP for the last N days."),
                ]
            ),
            inline=False,
        )
        embed.add_field(
            name="Auto-Delete Notes",
            value=(
                "`del_age` accepts values like `15m`, `2h`, `30d`, `1h30m`.\n"
                "`run` accepts `once`, `off`, or a duration like `30m`, `1h`, `1d`.\n"
                "Recurring runs delete tracked messages posted after the rule is enabled."
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
