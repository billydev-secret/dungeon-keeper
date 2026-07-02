"""General moderation and help commands."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.branding import resolve_accent_color
from bot_modules.core.db_utils import get_tz_offset_hours
from bot_modules.core.settings import AUTO_DELETE_SETTINGS

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot


def _fmt(command_specs: list[tuple[str, str]]) -> str:
    return "\n".join(f"`{name}`\n{desc}" for name, desc in command_specs)


def _get_cog_commands(cog: commands.Cog) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for cmd in cog.get_app_commands():
        if isinstance(cmd, app_commands.Group):
            for sub in cmd.commands:
                if isinstance(sub, app_commands.Group):
                    for subsub in sub.commands:
                        result.append((f"/{cmd.name} {sub.name} {subsub.name}", subsub.description))
                else:
                    result.append((f"/{cmd.name} {sub.name}", sub.description))
        else:
            result.append((f"/{cmd.name}", cmd.description))
    return sorted(result)


def _build_cog_pages(
    bot: "Bot", colour: "discord.Colour | None" = None
) -> list[discord.Embed]:
    pages: list[discord.Embed] = []
    page_colour = colour if colour is not None else discord.Color.greyple()
    for cog in sorted(bot.cogs.values(), key=lambda c: c.qualified_name.lower()):
        cmds = _get_cog_commands(cog)
        if not cmds:
            continue
        display = cog.qualified_name.removesuffix("Cog").replace("_", " ").strip()
        body = "\n".join(f"`{name}` — {desc}" for name, desc in cmds)
        if len(body) > 4000:
            body = body[:3997] + "…"
        pages.append(
            discord.Embed(
                title=f"📦  {display}",
                description=body,
                color=page_colour,
            )
        )
    return pages


_SECTION_META: dict[str, tuple[str, discord.Color]] = {
    "General": ("🌿", discord.Color.from_str("#5865F2")),
    "Role Grants": ("🎭", discord.Color.from_str("#57F287")),
    "XP Grant": ("⭐", discord.Color.from_str("#FEE75C")),
    "Moderation": ("🛡️", discord.Color.from_str("#ED4245")),
    "Voice": ("🔊", discord.Color.from_str("#2ECC71")),
    "Music": ("🎵", discord.Color.from_str("#E74C3C")),
    "Whisper": ("🤫", discord.Color.from_str("#E67E22")),
    "Image Guessing Games": ("🎭", discord.Color.from_str("#9B59B6")),
    "Games Night": ("🎲", discord.Color.from_str("#F1C40F")),
}


def _page(name: str, body: str) -> discord.Embed:
    emoji, color = _SECTION_META.get(name, ("📖", discord.Color.blurple()))
    return discord.Embed(
        title=f"{emoji}  {name}",
        description=body,
        color=color,
    )


def _build_help_pages(
    ctx: AppContext,
    interaction: discord.Interaction,
    colour: "discord.Colour | None" = None,
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
                    ("/penpals join", "Enter the Pen Pals pool — you'll be matched into a private 2-person channel with a conversation starter."),
                    ("/penpals leave", "Leave the pool before you're matched."),
                    ("/penpals status", "Check your current Pen Pals status."),
                    ("/penpals new-question", "Swap in a fresh conversation-starter question."),
                    ("/penpals end", "End your current pen pal chat early."),
                    ("/todo task:...", "Add a task to the server's shared todo list."),
                    ("/support", "Get a link to the support Discord server."),
                    ("/invite", "Get a bot invite link to add DungeonKeeper to another server."),
                    ("/delete_me", "Permanently delete your data from this server."),
                ]
            ),
        )
    )

    if ctx.can_grant_any_role(interaction):
        grant_cmds: list[tuple[str, str]] = []
        for gname, gcfg in ctx.guild_config(interaction.guild_id or 0).grant_roles.items():
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
                "Moderation",
                "Moderator-only tools.\n\n"
                + _fmt(
                    [
                        ("/purge count:... after:...", "Bulk-delete messages in this channel by count and/or time."),
                        ("/rename target:@user new_name:...", "Change a member's nickname (leave blank to reset to their username). Requires Manage Nicknames."),
                    ]
                ),
            )
        )

    pages.append(
        _page(
            "Voice",
            "Join the hub channel to create your own personal voice channel.\n\n"
            "**Channel Owner**\n"
            + _fmt(
                [
                    ("/voice lock / /voice unlock", "Restrict or re-open your channel."),
                    ("/voice hide / /voice unhide", "Make your channel invisible or visible."),
                    ("/voice rename name:...", "Set a custom channel name."),
                    ("/voice limit n:...", "Set user capacity (0 = unlimited)."),
                    ("/voice invite @user", "Add a member to your allow-list and invite them."),
                    ("/voice kick @user", "Remove a member and add them to your block-list."),
                    ("/voice transfer @user", "Give ownership to another member in your channel."),
                    ("/voice claim", "Claim an abandoned channel (original owner left)."),
                    ("/voice owner", "Show who owns the channel you're in."),
                    ("/voice trusted add/remove/list", "Manage your auto-invite list."),
                    ("/voice blocked add/remove/list", "Manage your block list."),
                    ("/voice profile show/reset", "View or delete your saved channel preferences."),
                ]
            ),
        )
    )

    pages.append(
        _page(
            "Music",
            "YouTube / Spotify playback. Join a voice channel, then `/play` to start.\n\n"
            + _fmt(
                [
                    ("/play query:...", "Play a YouTube URL, Spotify URL, or free-text search. Joins your VC automatically."),
                    ("/skip", "Skip the current track."),
                    ("/pause / /resume", "Pause or resume playback."),
                    ("/stop", "Clear the queue. Disconnects unless 24/7 mode is on."),
                    ("/shuffle", "Shuffle the remaining queue."),
                    ("/loop mode:[off|track|queue]", "Set loop mode for the current track or full queue."),
                    ("/queue page:1", "View the current queue (10 tracks per page)."),
                    ("/nowplaying", "Repost the now-playing embed with control buttons."),
                    ("/disconnect", "Force-disconnect the bot from voice."),
                    ("/247_status", "List all 24/7-enabled channels in this server."),
                ]
            ),
        )
    )

    pages.append(
        _page(
            "Whisper",
            "Anonymous messages delivered to opted-in members via DM. "
            "Sender identities are always logged for mods.\n\n"
            + _fmt(
                [
                    ("/whisper optin", "Opt in to send and receive anonymous whispers."),
                    ("/whisper optout", "Stop receiving whispers; you disappear from others' autocomplete."),
                    ("/whisper send target:@user message:...", "Send an anonymous whisper to an opted-in member. Cooldown: 30 s between sends."),
                    ("/whisper forget-me", "Permanently delete all your whisper data from this server."),
                ]
            ),
        )
    )

    pages.append(
        _page(
            "Image Guessing Games",
            "Submit a cropped NSFW image; opted-in members guess whose body it is. "
            "Guess uses written-name guessing with a leaderboard.\n\n"
            "**❓ Guess**\n"
            + _fmt(
                [
                    ("/guess submit image:<attachment>", "Open the Guess crop editor — position the reveal, then post."),
                    ("/guess optin", "Join the Guess pool so you appear in submitter / guesser autocomplete."),
                    ("/guess confess text:...", "Drop an anonymous text confession into the Guess channel."),
                    ("/guess leaderboard", "Top submitters and top guessers in this server."),
                ]
            ),
        )
    )

    pages.append(
        _page(
            "Games Night",
            "Group games for hangouts. Anyone can start one in an allowed channel; only one game per channel at a time.\n\n"
            "**Vote / react games**\n"
            + _fmt(
                [
                    ("/games play wyr", "Would You Rather."),
                    ("/games play nhie", "Never Have I Ever."),
                    ("/games play mfk", "Marry, Fornicate, Kiss — pick from three members."),
                    ("/games play mlt", "Most Likely To."),
                    ("/games play twotruths", "Two Truths and a Lie."),
                    ("/games play traditional", "Traditional Truth or Dare."),
                ]
            )
            + "\n\n**Anonymous & themed**\n"
            + _fmt(
                [
                    ("/games play fantasies", "Fantasies & Dealbreakers — anonymous matching."),
                    ("/games play ama", "Anonymous Ask Me Anything."),
                    ("/games play hottakes", "Hot Takes / Unpopular Opinions debate."),
                    ("/games play compliment", "Spin the Compliment — random anonymous pairing."),
                ]
            )
            + "\n\n**Creative & strategy**\n"
            + _fmt(
                [
                    ("/games play story", "Story Builder (Exquisite Corpse) — collaborative writing."),
                    ("/games play price", "Name Your Price — bidding game."),
                    ("/games play rushmore", "Mt. Rushmore Draft — pick your top 4."),
                    ("/games play clapback", "Clapback comedy head-to-head."),
                    ("/risky start", "Open a Risky Rolls round — dice-based dare ladder."),
                ]
            )
            + "\n\n**Settings & help**\n"
            + _fmt(
                [
                    ("/games help", "Full game-mode browser."),
                    ("/games support", "Link to the support server."),
                ]
            ),
        )
    )

    # Collapse the decorative per-section colours to the shared guild accent
    # when one is available; the per-section palette in ``_page`` stays as the
    # fallback for contexts without a resolvable guild (e.g. DMs).
    if colour is not None:
        for page in pages:
            page.colour = colour

    return pages


class CogPager(discord.ui.View):
    def __init__(self, pages: list[discord.Embed], invoker_id: int):
        super().__init__(timeout=120)
        self.pages = pages
        self.invoker_id = invoker_id
        self._index = 0
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.prev_btn.disabled = self._index == 0
        self.next_btn.disabled = self._index >= len(self.pages) - 1
        self.counter_btn.label = f"{self._index + 1} / {len(self.pages)}"

    def current_embed(self) -> discord.Embed:
        embed = self.pages[self._index]
        embed.set_footer(text="All commands registered on this bot.")
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.defer()
            return
        self._index = max(0, self._index - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label="1 / ?", style=discord.ButtonStyle.secondary, disabled=True)
    async def counter_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.defer()

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.defer()
            return
        self._index = min(len(self.pages) - 1, self._index + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]


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
    def __init__(
        self,
        pages: list[discord.Embed],
        invoker_id: int,
        bot: "Bot",
        colour: "discord.Colour | None" = None,
    ):
        super().__init__(timeout=120)
        self.bot = bot
        self.invoker_id = invoker_id
        self._accent = colour
        self.select = HelpSelect(pages, invoker_id)
        self.add_item(self.select)

    def current_embed(self) -> discord.Embed:
        embed = self.select.pages[0]
        embed.set_footer(text="Tip: Discord shows parameter hints while you type.")
        return embed

    @discord.ui.button(label="Browse by Module", style=discord.ButtonStyle.secondary, row=1)
    async def browse_modules(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.defer()
            return
        cog_pages = _build_cog_pages(self.bot, self._accent)
        if not cog_pages:
            await interaction.response.send_message("No modules found.", ephemeral=True)
            return
        pager = CogPager(cog_pages, self.invoker_id)
        await interaction.response.send_message(embed=pager.current_embed(), view=pager, ephemeral=True)

    async def on_timeout(self) -> None:
        self.select.disabled = True
        self.browse_modules.disabled = True


class ModCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    @app_commands.command(
        name="help", description="Browse all available commands organized by category."
    )
    async def help_command(self, interaction: discord.Interaction) -> None:
        accent = None
        if interaction.guild is not None:
            accent = await resolve_accent_color(self.ctx.db_path, interaction.guild)
        pages = _build_help_pages(self.ctx, interaction, accent)
        view = HelpView(
            pages, invoker_id=interaction.user.id, bot=self.bot, colour=accent
        )
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

        with ctx.open_db() as conn:
            tz_hours = get_tz_offset_hours(conn, interaction.guild_id or 0)

        after_dt: datetime | None = None
        if after is not None:
            try:
                parts = after.strip().split(":")
                if len(parts) not in (2, 3):
                    raise ValueError
                h, m = int(parts[0]), int(parts[1])
                s = int(parts[2]) if len(parts) == 3 else 0
                server_tz = timezone(timedelta(hours=tz_hours))
                now = datetime.now(server_tz)
                after_dt = now.replace(hour=h, minute=m, second=s, microsecond=0)
                if after_dt > now:
                    after_dt -= timedelta(days=1)
            except (ValueError, IndexError):
                tz_label = (
                    f"UTC{tz_hours:+g}"
                    if tz_hours != 0
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
