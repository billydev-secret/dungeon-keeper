import logging
import random
import discord
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import GOLDEN_MEADOW_COLOR, GAME_ICONS, HOW_TO_PLAY
from bot_modules.games.utils.game_manager import (
    check_allowed_channel,
    create_game,
    update_game_message,
    update_game_payload,
    get_game_payload,
    modify_payload,
    end_game,
    update_session,
    ConfirmCloseView,
)

log = logging.getLogger(__name__)

CATEGORIES = ["sfw_truth", "sfw_dare", "nsfw_truth", "nsfw_dare"]
CAT_LABELS = {
    "sfw_truth": "SFW Truth",
    "sfw_dare": "SFW Dare",
    "nsfw_truth": "NSFW Truth",
    "nsfw_dare": "NSFW Dare",
}


def build_tod_embed(host_name: str, payload: dict, guild=None, closed: bool = False) -> discord.Embed:
    title = f"{GAME_ICONS['traditional']} TRUTH OR DARE"
    if closed:
        title += " — GAME OVER"
    embed = discord.Embed(title=title, color=GOLDEN_MEADOW_COLOR)
    embed.add_field(name="Host", value=host_name, inline=True)

    participants: list = payload.get("participants", [])
    embed.add_field(name="Participants", value=str(len(participants)), inline=True)

    asked = payload.get("asked", {})
    embed.add_field(name="Questions Asked", value=str(len(asked)), inline=True)

    embed.set_footer(text=f"{GAME_ICONS['traditional']} Truth or Dare")
    return embed



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
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, self.title, interaction.channel.name if interaction.channel else "unknown")
        payload = await get_game_payload(self.db, self.game_id)

        # Record the question
        asked = payload.setdefault("asked", {})
        key = f"{self.target_id}:{self.cat}"
        asked[key] = self.question.value
        await update_game_payload(self.db, self.game_id, payload)

        target_member = interaction.guild.get_member(int(self.target_id)) if interaction.guild else None
        mention = target_member.mention if target_member else f"**{self.target_name}**"

        await self.channel.send(
            f"**{GAME_ICONS['traditional']} {CAT_LABELS[self.cat]}** for {mention}\n"
            f"**{self.question.value}**"
        )

        await interaction.response.send_message("Question posted!", ephemeral=True)


class TraditionalHostView(discord.ui.View):
    """Main embed view — host controls + player preference toggles."""

    def __init__(self, game_id: str, host_id: int, db, bot):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot

    def is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild:
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    async def _get_payload(self) -> dict:
        return await get_game_payload(self.db, self.game_id)

    async def _save_payload(self, payload: dict):
        await update_game_payload(self.db, self.game_id, payload)

    async def _update_embed(self, interaction: discord.Interaction, payload: dict):
        host_member = interaction.guild.get_member(self.host_id) if interaction.guild else None
        host_name = host_member.display_name if host_member else "Host"
        embed = build_tod_embed(host_name, payload, guild=interaction.guild)
        if hasattr(self, '_message') and self._message:
            try:
                await self._message.edit(embed=embed, view=self)
            except Exception:
                pass

    # --- Player preference toggles (row 0) ---

    @discord.ui.button(label="SFW Truth", style=discord.ButtonStyle.primary, custom_id="tod_sfw_truth", row=0)
    async def sfw_truth(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        await self._toggle_pref(interaction, "sfw_truth")

    @discord.ui.button(label="SFW Dare", style=discord.ButtonStyle.primary, custom_id="tod_sfw_dare", row=0)
    async def sfw_dare(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        await self._toggle_pref(interaction, "sfw_dare")

    @discord.ui.button(label="NSFW Truth", style=discord.ButtonStyle.danger, custom_id="tod_nsfw_truth", row=0)
    async def nsfw_truth(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        await self._toggle_pref(interaction, "nsfw_truth")

    @discord.ui.button(label="NSFW Dare", style=discord.ButtonStyle.danger, custom_id="tod_nsfw_dare", row=0)
    async def nsfw_dare(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        await self._toggle_pref(interaction, "nsfw_dare")

    async def _toggle_pref(self, interaction: discord.Interaction, category: str):
        user_id = interaction.user.id
        str_id = str(user_id)
        action_holder = {}

        def _do_toggle(payload):
            participants = payload.setdefault("participants", [])
            prefs = payload.setdefault("prefs", {})

            if user_id not in participants:
                participants.append(user_id)

            user_prefs = prefs.setdefault(str_id, [])
            if category in user_prefs:
                user_prefs.remove(category)
                action_holder["action"] = "removed"
                if not user_prefs:
                    participants.remove(user_id)
                    del prefs[str_id]
            else:
                user_prefs.append(category)
                action_holder["action"] = "added"

        payload = await modify_payload(self.db, self.game_id, _do_toggle)
        await self._update_embed(interaction, payload)
        await interaction.response.send_message(
            f"**{CAT_LABELS[category]}** {action_holder['action']} from your preferences.", ephemeral=True
        )

    # --- Host controls (row 1) ---

    @discord.ui.button(label="Ask Question", style=discord.ButtonStyle.success, custom_id="tod_ask", row=1)
    async def ask_question(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can ask questions.", ephemeral=True)
            return
        payload = await self._get_payload()
        prefs = payload.get("prefs", {})
        asked = payload.get("asked", {})
        if not prefs:
            await interaction.response.send_message("No players have joined yet!", ephemeral=True)
            return

        available = []
        for user_id, user_cats in prefs.items():
            member = interaction.guild.get_member(int(user_id)) if interaction.guild else None
            name = member.display_name if member else str(user_id)
            for cat in user_cats:
                key = f"{user_id}:{cat}"
                if key not in asked:
                    available.append((user_id, name, cat))

        if not available:
            await interaction.response.send_message(
                "All player/category combinations have been asked!", ephemeral=True
            )
            return

        target_counts = {}
        for user_id, name, cat in available:
            count = sum(1 for k in asked if k.startswith(f"{user_id}:"))
            target_counts.setdefault(user_id, count)

        min_count = min(target_counts.values())
        least_asked = [(uid, name, cat) for uid, name, cat in available if target_counts[uid] == min_count]
        chosen_uid, chosen_name, chosen_cat = random.choice(least_asked)

        modal = AskQuestionModal(
            self.game_id, self.db, interaction.channel, self.host_id, self.bot,
            target_id=chosen_uid, target_name=chosen_name, category=chosen_cat,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="🛑 Close Game", style=discord.ButtonStyle.danger, custom_id="tod_close", row=1)
    async def close_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can close this game.", ephemeral=True)
            return
        game_msg = interaction.message
        channel = interaction.channel

        async def _confirmed(confirm_interaction):
            await self._do_close(confirm_interaction, game_msg, channel)

        view = ConfirmCloseView(_confirmed)
        await interaction.response.send_message("⚠️ Are you sure you want to end this game?", view=view, ephemeral=True)

    @discord.ui.button(label="❓ How to Play", style=discord.ButtonStyle.secondary, custom_id="tod_htp", row=1)
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        await interaction.response.send_message(HOW_TO_PLAY["traditional"], ephemeral=True)

    async def _do_close(self, interaction: discord.Interaction, game_msg=None, channel=None):
        payload = await self._get_payload()
        participants = payload.get("participants", [])
        asked = payload.get("asked", {})

        total_q = len(asked)
        by_cat = {cat: 0 for cat in CATEGORIES}
        for key in asked:
            _, cat = key.rsplit(":", 1)
            if cat in by_cat:
                by_cat[cat] += 1

        embed = discord.Embed(
            title=f"{GAME_ICONS['traditional']} TRUTH OR DARE — GAME OVER",
            color=0x808080,
        )
        embed.add_field(name="Total Questions Asked", value=str(total_q), inline=True)
        embed.add_field(name="Participants", value=str(len(participants)), inline=True)
        for cat, count in by_cat.items():
            if count:
                embed.add_field(name=CAT_LABELS[cat], value=str(count), inline=True)

        self.stop()
        for item in self.children:
            item.disabled = True

        if game_msg:
            try:
                await game_msg.edit(view=self)
            except Exception:
                pass
            await channel.send(embed=embed)
        else:
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(embed=embed)

        log.info("Game %s ended — %d players", self.game_id, len(participants))
        await end_game(self.db, self.game_id, player_count=len(participants), round_count=total_q, payload=payload)
        if self.game_id in self.bot.active_views:
            del self.bot.active_views[self.game_id]


class TraditionalCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(name="traditional", description="Start a Traditional Truth or Dare game!")
    async def traditional(self, interaction: discord.Interaction):
        log.info("%s used /traditional in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it with `/config allow-channel`.",
                ephemeral=True,
            )
            return

        game_id = await create_game(
            self.db,
            interaction.channel_id,
            interaction.user.id,
            "traditional",
            state="joining",
        )

        embed = discord.Embed(
            title=f"{GAME_ICONS['traditional']} TRUTH OR DARE",
            description="Select your preferences below to join!",
            color=GOLDEN_MEADOW_COLOR,
        )
        embed.add_field(name="Host", value=interaction.user.display_name, inline=True)
        embed.add_field(name="Participants", value="0", inline=True)
        embed.add_field(name="Questions Asked", value="0", inline=True)
        embed.set_footer(text=f"{GAME_ICONS['traditional']} Truth or Dare")

        log.info("Game %s (traditional) created by %s in #%s", game_id, interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        host_view = TraditionalHostView(game_id, interaction.user.id, self.db, self.bot)
        self.bot.active_views[game_id] = host_view

        await interaction.response.send_message(embed=embed, view=host_view)
        msg = await interaction.original_response()
        host_view._message = msg
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, interaction.channel_id, game_id, [interaction.user.id])


async def setup(bot: commands.Bot):
    await bot.add_cog(TraditionalCog(bot))
