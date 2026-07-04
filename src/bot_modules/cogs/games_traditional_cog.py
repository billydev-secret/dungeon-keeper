import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord

from bot_modules.core.utils import disable_all_items
from discord import app_commands
from discord.ext import commands

from bot_modules.core.branding import resolve_accent_color
from bot_modules.games.constants import HOW_TO_PLAY
from bot_modules.games.command_groups import play
from bot_modules.games.utils.game_manager import (
    check_allowed_channel,
    create_game,
    end_game,
    get_game_payload,
    modify_payload,
    update_game_message,
    update_game_payload,
    update_session,
    channel_name,
)
from bot_modules.games_traditional.embeds import (
    build_lobby_embed,
    build_question_embed,
    build_recap_embed,
    build_tod_embed,
)
from bot_modules.games_traditional.logic import (
    CAT_LABELS,
    record_asked,
    select_next_question_target,
    toggle_pref,
)

log = logging.getLogger(__name__)


class AskQuestionModal(discord.ui.Modal):
    question = discord.ui.TextInput(
        label="Your Question",
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    def __init__(self, game_id: str, db, channel, host_id: int, bot, target_id: str, target_name: str, category: str):
        super().__init__(title=f"{CAT_LABELS[category]} for {target_name}"[:45])
        self.game_id = game_id
        self.db = db
        self.channel = channel
        self.host_id = host_id
        self.bot = bot
        self.target_id = target_id
        self.target_name = target_name
        self.cat = category

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, self.title, channel_name(interaction.channel))
        payload = await get_game_payload(self.db, self.game_id)

        # Record the question (pure dict transform)
        record_asked(payload, self.target_id, self.cat, self.question.value)
        await update_game_payload(self.db, self.game_id, payload)

        target_member = interaction.guild.get_member(int(self.target_id)) if interaction.guild else None
        mention = target_member.mention if target_member else f"**{self.target_name}**"

        await self.channel.send(
            content=mention,
            embed=build_question_embed(self.cat, self.question.value, self.target_name),
        )

        await interaction.response.defer()

        view = self.bot.active_views.get(self.game_id)
        if view:
            await view.refresh_embed(interaction.guild, payload)


class TraditionalHostView(discord.ui.View):
    """Main embed view — host controls + player preference toggles."""

    def __init__(self, game_id: str, host_id: int, db, bot):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self._message: discord.Message | None = None

    def is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild and isinstance(interaction.user, discord.Member):
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    async def _get_payload(self) -> dict:
        return await get_game_payload(self.db, self.game_id)

    async def _save_payload(self, payload: dict):
        await update_game_payload(self.db, self.game_id, payload)

    def _resolve_names(self, guild: discord.Guild | None, payload: dict) -> dict[str, str]:
        if not guild:
            return {}
        names: dict[str, str] = {}
        for uid in payload.get("prefs", {}):
            member = guild.get_member(int(uid))
            if member:
                names[uid] = member.display_name
        return names

    async def refresh_embed(self, guild: discord.Guild | None, payload: dict):
        host_member = guild.get_member(self.host_id) if guild else None
        host_name = host_member.display_name if host_member else "Host"
        names = self._resolve_names(guild, payload)
        colour = await resolve_accent_color(self.bot.ctx.db_path, guild) if guild else None
        embed = build_tod_embed(host_name, payload, names=names, colour=colour)
        if hasattr(self, '_message') and self._message:
            try:
                await self._message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass

    async def _update_embed(self, interaction: discord.Interaction, payload: dict):
        await self.refresh_embed(interaction.guild, payload)

    # --- Player preference toggles (row 0) ---

    @discord.ui.button(label="SFW Truth", style=discord.ButtonStyle.primary, custom_id="tod_sfw_truth", row=0)
    async def sfw_truth(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await self._toggle_pref(interaction, "sfw_truth")

    @discord.ui.button(label="SFW Dare", style=discord.ButtonStyle.primary, custom_id="tod_sfw_dare", row=0)
    async def sfw_dare(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await self._toggle_pref(interaction, "sfw_dare")

    @discord.ui.button(label="NSFW Truth", style=discord.ButtonStyle.danger, custom_id="tod_nsfw_truth", row=0)
    async def nsfw_truth(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await self._toggle_pref(interaction, "nsfw_truth")

    @discord.ui.button(label="NSFW Dare", style=discord.ButtonStyle.danger, custom_id="tod_nsfw_dare", row=0)
    async def nsfw_dare(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await self._toggle_pref(interaction, "nsfw_dare")

    async def _toggle_pref(self, interaction: discord.Interaction, category: str):
        user_id = interaction.user.id
        action_holder: dict[str, str] = {}

        def _do_toggle(payload):
            action_holder["action"] = toggle_pref(payload, user_id, category)

        payload = await modify_payload(self.db, self.game_id, _do_toggle)
        await self._update_embed(interaction, payload)
        await interaction.response.send_message(
            f"**{CAT_LABELS[category]}** {action_holder['action']} from your preferences.", ephemeral=True
        )

    # --- Host controls (row 1) ---

    @discord.ui.button(label="Ask Question", style=discord.ButtonStyle.success, custom_id="tod_ask", row=1)
    async def ask_question(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can ask questions.", ephemeral=True)
            return
        payload = await self._get_payload()
        prefs = payload.get("prefs", {})
        asked = payload.get("asked", {})
        if not prefs:
            await interaction.response.send_message("No players have joined yet!", ephemeral=True)
            return

        choice = select_next_question_target(prefs, asked)
        if choice is None:
            await interaction.response.send_message(
                "All player/category combinations have been asked!", ephemeral=True
            )
            return

        chosen_uid, chosen_cat = choice
        member = interaction.guild.get_member(int(chosen_uid)) if interaction.guild else None
        chosen_name = member.display_name if member else str(chosen_uid)

        modal = AskQuestionModal(
            self.game_id, self.db, interaction.channel, self.host_id, self.bot,
            target_id=chosen_uid, target_name=chosen_name, category=chosen_cat,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="❓ Help", style=discord.ButtonStyle.secondary, custom_id="tod_htp", row=1)
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["traditional"], ephemeral=True)

    async def _do_close(self, interaction: discord.Interaction, game_msg=None, channel=None):
        payload = await self._get_payload()
        participants = payload.get("participants", [])
        asked = payload.get("asked", {})
        total_q = len(asked)

        guild = (interaction.guild if interaction else None) or getattr(channel, "guild", None)
        colour = await resolve_accent_color(self.bot.ctx.db_path, guild) if guild else None
        embed = build_recap_embed(payload, colour=colour)

        self.stop()
        disable_all_items(self)

        if game_msg:
            try:
                await game_msg.edit(view=self)
            except discord.HTTPException:
                pass
            assert channel is not None
            await channel.send(embed=embed)
        else:
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(embed=embed)

        log.info("Game %s ended — %d players", self.game_id, len(participants))
        await end_game(self.db, self.game_id, player_count=len(participants), round_count=total_q, payload=payload)
        if self.game_id in self.bot.active_views:
            del self.bot.active_views[self.game_id]


class TraditionalCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(name="traditional", description="Start a Traditional Truth or Dare game!")
    async def traditional(self, interaction: discord.Interaction):
        log.info("%s used /games play traditional in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it from the web dashboard.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={},
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
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "traditional",
            state="joining",
        )

        guild = getattr(channel, "guild", None)
        colour = await resolve_accent_color(self.bot.ctx.db_path, guild) if guild else None
        embed = build_lobby_embed(host_name, colour=colour)

        log.info("Game %s (traditional) created by %s in #%s", game_id, host_name, getattr(channel, "name", channel.id))
        host_view = TraditionalHostView(game_id, host_id, self.db, self.bot)
        self.bot.active_views[game_id] = host_view

        try:
            msg = await channel.send(embed=embed, view=host_view)
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("traditional launch lacked send perms in channel %s", channel.id)
            return None
        host_view._message = msg
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Re-register the host view after a restart so its buttons work again."""
        game_id = row["game_id"]
        host_view = TraditionalHostView(game_id, int(row["host_id"]), self.db, self.bot)
        host_view._message = message
        self.bot.active_views[game_id] = host_view
        self.bot.add_view(host_view, message_id=message.id)
        log.info("Recovered traditional game %s in #%s", game_id, getattr(channel, "name", channel.id))
        return True


async def setup(bot: "Bot"):
    cog = TraditionalCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("traditional")
    play.add_command(cog.traditional, override=True)
    bot.game_launchers["traditional"] = cog.launch
    bot.game_recoverers["traditional"] = cog.recover_game
