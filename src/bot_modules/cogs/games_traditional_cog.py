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
    finish_launch_response,
    check_allowed_channel,
    check_game_enabled,
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
from bot_modules.games.utils.question_source import (
    channel_allows_nsfw,
    get_traditional_question,
)
from bot_modules.games_traditional.logic import (
    CAT_LABELS,
    category_allowed,
    filter_nsfw_prefs,
    record_asked,
    select_bank_categories_for_all,
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
        color = await resolve_accent_color(self.bot.ctx.db_path, guild) if guild else None
        embed = build_tod_embed(host_name, payload, names=names, color=color)
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
        # NSFW prompts ride Discord's own age gate, never a bot-side toggle.
        # Gating the preference is what keeps NSFW content out of a SFW
        # channel: every serve path draws only from opted-in categories.
        if not category_allowed(category, channel_allows_nsfw(interaction.channel)):
            await interaction.response.send_message(
                "❌ NSFW categories are only available in age-restricted channels.",
                ephemeral=True,
            )
            return
        user_id = interaction.user.id
        action_holder: dict[str, str] = {}

        def _do_toggle(payload):
            single_choice = bool(payload.get("single_choice", False))
            action_holder["action"] = toggle_pref(
                payload, user_id, category, single_choice=single_choice
            )

        payload = await modify_payload(self.db, self.game_id, _do_toggle)
        await self._update_embed(interaction, payload)
        action = action_holder["action"]
        if action == "switched":
            msg = f"Switched to **{CAT_LABELS[category]}**."
        else:
            msg = f"**{CAT_LABELS[category]}** {action} from your preferences."
        await interaction.response.send_message(msg, ephemeral=True)

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

    @discord.ui.button(label="Bank Round", emoji="🎲", style=discord.ButtonStyle.primary, custom_id="tod_bank_round", row=1)
    async def bank_round(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Serve every opted-in player a fresh question pulled from the bank.

        Each participant gets one question in a category they opted into,
        drawn from the web-managed question bank (no repeats within a game).
        Bank questions land in the same ``asked`` history as written ones,
        so each player is served at most once per opted-in category —
        pressing the button again after new people join only serves the
        newcomers. Players with no available bank question for their picked
        category are reported back.
        """
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can run a bank round.", ephemeral=True)
            return

        payload = await self._get_payload()
        prefs = payload.get("prefs", {})
        if not prefs:
            await interaction.response.send_message("No players have joined yet!", ephemeral=True)
            return

        await interaction.response.defer()

        guild = interaction.guild
        channel = interaction.channel
        assert isinstance(channel, discord.abc.Messageable)  # games run in text channels
        used: list[str] = list(payload.get("bank_used", []))
        asked = payload.get("asked", {})
        # Belt-and-braces: prefs set while the channel was still age-restricted
        # must not serve here if the flag has since been removed.
        prefs = filter_nsfw_prefs(prefs, channel_allows_nsfw(channel))
        choices = select_bank_categories_for_all(prefs, asked)  # {uid: category}
        already_asked = sum(1 for cats in prefs.values() if cats) - len(choices)

        served = 0
        unserved: list[str] = []
        for uid, cat in choices.items():
            member = guild.get_member(int(uid)) if guild else None
            name = member.display_name if member else str(uid)
            question = await get_traditional_question(self.db, cat, exclude=used)
            if question is None:
                unserved.append(f"{name} ({CAT_LABELS.get(cat, cat)})")
                continue
            used.append(question)
            record_asked(payload, uid, cat, question)
            mention = member.mention if member else f"**{name}**"
            await channel.send(content=mention, embed=build_question_embed(cat, question, name))
            served += 1

        payload["bank_used"] = used
        payload["bank_asked"] = payload.get("bank_asked", 0) + served
        await self._save_payload(payload)
        await self.refresh_embed(guild, payload)

        if not choices and already_asked:
            msg = (
                "Everyone has already been asked in all their chosen categories — "
                "run this again when new players join."
            )
        elif served == 0 and unserved:
            msg = (
                "No bank questions were available. Add some in the web dashboard "
                "under **Games → Traditional Truth or Dare → Questions**."
            )
        else:
            msg = f"Served **{served}** question{'s' if served != 1 else ''} from the bank."
            if unserved:
                msg += "\nNo bank question available for: " + ", ".join(unserved) + "."
            if already_asked:
                msg += f"\nSkipped {already_asked} player{'s' if already_asked != 1 else ''} already asked in all their categories."
        await interaction.followup.send(msg, ephemeral=True)

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
        color = await resolve_accent_color(self.bot.ctx.db_path, guild) if guild else None
        embed = build_recap_embed(payload, color=color)
        if guild:
            from bot_modules.economy.game_rewards import append_payout_footer
            await append_payout_footer(self.bot, embed, guild.id, "traditional")

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
        await end_game(self.db, self.game_id, player_count=len(participants), round_count=total_q, payload=payload,
                       bot=self.bot, player_ids=participants)
        if self.game_id in self.bot.active_views:
            del self.bot.active_views[self.game_id]


class TraditionalCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(name="traditional", description="Start a Traditional Truth or Dare game!")
    @app_commands.describe(
        single_choice="Each player picks only one category (radio-style) instead of as many as they like",
    )
    async def traditional(self, interaction: discord.Interaction, single_choice: bool = False):
        log.info("%s used /games play traditional in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it from the web dashboard.",
                ephemeral=True,
            )
            return
        if not await check_game_enabled(self.db, "traditional", interaction.guild_id or 0):
            await interaction.response.send_message(
                "Traditional Truth or Dare is currently disabled on this server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={"single_choice": single_choice},
        )
        await finish_launch_response(interaction, game_id)

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
        single_choice = bool(options.get("single_choice", False))
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "traditional",
            state="joining",
            payload={"single_choice": True} if single_choice else None,
        )

        guild = getattr(channel, "guild", None)
        color = await resolve_accent_color(self.bot.ctx.db_path, guild) if guild else None
        embed = build_lobby_embed(host_name, color=color, single_choice=single_choice)

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
