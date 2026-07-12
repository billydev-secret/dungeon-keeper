import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord

from bot_modules.core.utils import disable_all_items
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import HOW_TO_PLAY
from bot_modules.core.branding import resolve_accent_color
from bot_modules.games.command_groups import play
from bot_modules.games.utils.game_manager import (
    check_allowed_channel,
    create_game,
    update_game_message,
    get_game_payload,
    modify_payload,
    end_game,
    update_session,
    resolve_names,
    channel_name,
)
from bot_modules.games_mfk.embeds import (
    build_assignments_embed,
    build_lobby_embed,
)
from bot_modules.games_mfk.logic import (
    DEFAULT_LABELS,
    assign_targets,
    parse_labels,
    serialize_assignments,
    toggle_participant,
)

log = logging.getLogger(__name__)


class MFKView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, db, bot, labels: list[str] | None = None):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self.labels = labels or DEFAULT_LABELS

    def is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild and isinstance(interaction.user, discord.Member):
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="mfk_join")
    async def join_pool(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        user_id = interaction.user.id
        action_holder: dict[str, str] = {}

        def _toggle(payload):
            action_holder["action"] = toggle_participant(payload, user_id)

        payload = await modify_payload(self.db, self.game_id, _toggle)
        action = action_holder["action"]
        log.info("%s %s game %s in #%s", interaction.user.display_name, action, self.game_id, channel_name(interaction.channel))

        host_member = interaction.guild.get_member(self.host_id) if interaction.guild else None
        names = resolve_names(interaction.guild, payload.get("participants", []))
        colour = await resolve_accent_color(self.bot.ctx.db_path, interaction.guild) if interaction.guild else None
        embed = build_lobby_embed(
            host_member.display_name if host_member else "Host",
            names,
            labels=self.labels,
            colour=colour,
        )
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(
            f"You've {action} the pool.", ephemeral=True
        )

    @discord.ui.button(label="Close & Assign", style=discord.ButtonStyle.primary, custom_id="mfk_assign")
    async def close_assign(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can assign roles.", ephemeral=True)
            return

        payload = await get_game_payload(self.db, self.game_id)
        participants = payload.get("participants", [])
        if len(participants) < 4:
            await interaction.response.send_message(
                "Need at least 4 players in the pool!", ephemeral=True
            )
            return

        await interaction.response.defer()

        # Each player gets 3 random names from the pool (not themselves)
        assignments = assign_targets(participants)

        # Resolve mentions + target display names against the live guild
        player_assignments: list[tuple[str, list[str]]] = []
        mentions: list[str] = []
        for player_id, trio in assignments.items():
            player = interaction.guild.get_member(player_id) if interaction.guild else None
            player_str = player.mention if player else str(player_id)
            if player:
                mentions.append(player.mention)
            target_names: list[str] = []
            for uid in trio:
                m = interaction.guild.get_member(uid) if interaction.guild else None
                target_names.append(m.display_name if m else str(uid))
            player_assignments.append((player_str, target_names))

        colour = await resolve_accent_color(self.bot.ctx.db_path, interaction.guild) if interaction.guild else None
        embed = build_assignments_embed(player_assignments, labels=self.labels, colour=colour)

        self.stop()
        disable_all_items(self)

        await interaction.edit_original_response(view=self)

        unique_mentions = list(dict.fromkeys(mentions))
        await interaction.followup.send(
            content=" ".join(unique_mentions),
            embed=embed,
        )

        log.info("Game %s ended — %d players", self.game_id, len(participants))
        await end_game(
            self.db,
            self.game_id,
            player_count=len(participants),
            payload={"assignments": serialize_assignments(assignments)},
            bot=self.bot, player_ids=list(participants),
        )
        if self.game_id in self.bot.active_views:
            del self.bot.active_views[self.game_id]

    @discord.ui.button(label="❓ Help", style=discord.ButtonStyle.secondary, custom_id="mfk_htp")
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["mfk"], ephemeral=True)


class MFKCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(name="mfk", description="Start a Marry, Fornicate, Kiss game!")
    @app_commands.describe(
        options='Custom categories (comma-separated, exactly 3). e.g. "Cruise, Wedding, Vacation"',
    )
    async def mfk(self, interaction: discord.Interaction, options: str | None = None):
        log.info("%s used /games play mfk in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it from the web dashboard.",
                ephemeral=True,
            )
            return

        # Parse custom labels
        labels, label_error = parse_labels(options)
        if label_error is not None:
            await interaction.response.send_message(label_error, ephemeral=True)
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={"options": options},
        )
        if game_id is None:
            try:
                await interaction.followup.send(
                    "I don't have access to send messages in that channel. "
                    "Please grant me **View Channel**, **Send Messages**, and **Embed Links**.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass

    async def launch(
        self,
        *,
        channel,
        host_id: int,
        host_name: str,
        guild_id: int,
        options: dict,
    ) -> str | None:
        """Interaction-free launch (slash command + scheduler). Returns game_id, or None."""
        labels, _ = parse_labels(options.get("options"))

        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "mfk",
            state="joining",
            payload={"labels": labels or DEFAULT_LABELS},
        )

        log.info("Game %s (mfk) created by %s in #%s", game_id, host_name, getattr(channel, "name", channel.id))
        guild = getattr(channel, "guild", None)
        colour = await resolve_accent_color(self.bot.ctx.db_path, guild) if guild else None
        embed = build_lobby_embed(host_name, [], labels=labels, colour=colour)
        view = MFKView(game_id, host_id, self.db, self.bot, labels=labels)
        self.bot.active_views[game_id] = view

        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("mfk launch lacked send perms in channel %s", channel.id)
            return None
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Re-register the MFK view after a restart so its buttons work again."""
        game_id = row["game_id"]
        view = MFKView(game_id, int(row["host_id"]), self.db, self.bot, labels=payload.get("labels"))
        self.bot.active_views[game_id] = view
        self.bot.add_view(view, message_id=message.id)
        log.info("Recovered mfk game %s in #%s", game_id, getattr(channel, "name", channel.id))
        return True


async def setup(bot: "Bot"):
    cog = MFKCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("mfk")
    play.add_command(cog.mfk, override=True)
    bot.game_launchers["mfk"] = cog.launch
    bot.game_recoverers["mfk"] = cog.recover_game
