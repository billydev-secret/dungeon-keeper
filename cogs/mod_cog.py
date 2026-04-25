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
                    ("/birthday set", "Set your own birthday."),
                    ("/wellness setup", "Opt in to wellness — pick timezone and enforcement style."),
                    ("/wellness away on", "Turn on your away auto-reply."),
                    ("/wellness away off", "Turn off your away auto-reply."),
                    ("/confess", "Open the anonymous confession form."),
                    ("/dm_help", "Overview of the DM request system."),
                    ("/dm_set_mode", "Set your DM preference (open / ask / closed)."),
                    ("/dm_status @user", "Check whether mutual DM permission exists."),
                    ("/dm_revoke @user", "Revoke a DM permission relationship."),
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
                "Give community roles to members.\n\n" + _fmt(grant_cmds),
            )
        )

    if ctx.can_use_xp_grant(interaction):
        pages.append(
            _page(
                "XP Grant",
                "Award XP for contributions outside normal chat — events, art, helpful DMs, etc.\n\n"
                + _fmt([("/xp_give member:@user", "Award 20 XP to a member.")]),
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
                            "/jail user:@user duration:24h reason:...",
                            "Place a member in a private jail channel. "
                            "Duration: `30m`, `2h`, `7d`, or omit for indefinite.",
                        ),
                        (
                            "/unjail user:@user reason:...",
                            "Release a jailed member. Restores their roles and saves a transcript.",
                        ),
                        ("/pull user:@user", "Bring someone into this jail/ticket channel."),
                        ("/remove user:@user", "Remove someone you pulled into this channel."),
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
                            "/modinfo user:@user",
                            "Full mod profile: jail status, warnings, and tickets. Run this first.",
                        ),
                    ]
                )
                + "\n\n**📩 Tickets**\n"
                + _fmt(
                    [
                        (
                            "/ticket close reason:...",
                            "Close this ticket. It becomes read-only with Reopen/Delete buttons.",
                        ),
                        ("/ticket reopen", "Reopen a closed ticket."),
                        ("/ticket delete", "Delete a closed ticket. A transcript is saved first."),
                        ("/ticket claim", "Claim this ticket — you'll get DM pings on new activity."),
                        (
                            "/ticket escalate reason:...",
                            "Bring admins into this ticket and ping them.",
                        ),
                    ]
                )
                + "\n\n**🧹 Cleanup**\n"
                + _fmt(
                    [
                        (
                            "/purge count:50",
                            "Bulk-delete messages. Use `after:19:35` for time-based, or both.",
                        ),
                    ]
                ),
            )
        )

        pages.append(
            _page(
                "Reports",
                _fmt(
                    [
                        ("/report promotion_review", "Members past level 5 who still lack NSFW access."),
                        ("/quality_leave add member:@user days:30", "Put a member on leave (pauses quality scoring)."),
                        ("/quality_leave remove member:@user", "End a member's leave of absence."),
                        ("/quality_leave list", "List members currently on leave."),
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
                        ("/ai channel question:... minutes:60", "Ask the AI about a channel's recent activity."),
                        ("/ai query member:@user question:...", "Ask the AI about a member's message history."),
                    ]
                ),
            )
        )

        pages.append(
            _page(
                "Server Tools",
                _fmt(
                    [
                        ("/setup", "First-time bot setup — jail role, categories, log channels."),
                        ("/starboard channel|threshold|emoji|toggle|status", "Configure the starboard."),
                        ("/todo", "Add a task to the server todo list."),
                        ("/policy open|vote|close|list", "Policy proposals and voting."),
                        ("/delete_user user:@user", "Admin: purge a user's data."),
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
