import logging

import discord
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import GAME_ICONS, HOW_TO_PLAY
from bot_modules.games.utils.audit import send_audit_log
from bot_modules.games.utils.game_manager import (
    check_allowed_channel,
    get_active_game_by_id,
    create_game,
    update_game_message,
    end_game,
    modify_payload,
    update_session,
    ConfirmCloseView,
)
from bot_modules.games.command_groups import play
from bot_modules.games_ffa.embeds import build_ffa_embed
from bot_modules.games_ffa.logic import add_anon_reply

log = logging.getLogger(__name__)


class AnonymousReplyModal(discord.ui.Modal, title="Anonymous Reply"):
    answer = discord.ui.TextInput(
        label="Your Answer",
        style=discord.TextStyle.paragraph,
        placeholder="Type your anonymous answer here...",
        max_length=1000,
    )

    def __init__(self, game_id: str, db, channel: discord.TextChannel, ffa_view):
        super().__init__()
        self.game_id = game_id
        self.db = db
        self.channel = channel
        self.ffa_view = ffa_view

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, "Anonymous Reply", interaction.channel.name if interaction.channel else "unknown")
        def _add_reply(payload):
            add_anon_reply(payload, interaction.user.id, self.answer.value)
        payload = await modify_payload(self.db, self.game_id, _add_reply)

        # Audit log
        if interaction.guild:
            await send_audit_log(
                interaction.client, self.db, interaction.guild,
                game_type="ffa", user=interaction.user,
                content=self.answer.value, label="FFA Anonymous Reply",
            )

        # Post reply WITHOUT echoing the original question
        await self.channel.send(f"💬 **Anonymous:** {discord.utils.escape_markdown(self.answer.value)}")
        await interaction.response.send_message(
            "✅ Your anonymous reply has been posted!", ephemeral=True
        )

        # Update status bar on main embed
        anon_replies = payload.get("anon_replies", {})
        if self.ffa_view._game_msg:
            try:
                embed = build_ffa_embed(
                    self.ffa_view.question,
                    reply_count=len(anon_replies),
                )
                await self.ffa_view._game_msg.edit(embed=embed)
            except Exception as e:
                log.debug("Failed to update FFA status bar: %s", e)


class FFAView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, question: str, db, bot):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.question = question
        self.db = db
        self.bot = bot
        self._game_msg: discord.Message | None = None

    @discord.ui.button(
        label="Reply Anonymously",
        style=discord.ButtonStyle.secondary,
        custom_id="ffa_anon_reply",
    )
    async def anon_reply(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        row = await get_active_game_by_id(self.db, self.game_id)
        if not row:
            await interaction.response.send_message("This game is no longer active.", ephemeral=True)
            return
        modal = AnonymousReplyModal(self.game_id, self.db, interaction.channel, self)
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="🛑 Close Game",
        style=discord.ButtonStyle.danger,
        custom_id="ffa_close",
    )
    async def close_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if interaction.user.id != self.host_id:
            if interaction.guild:
                perms = interaction.user.guild_permissions
                if not (perms.administrator or perms.manage_guild):
                    await interaction.response.send_message("Only the host or a mod can close.", ephemeral=True)
                    return
        game_msg = self._game_msg

        async def _confirmed(confirm_interaction):
            await end_game(self.db, self.game_id)
            self.stop()
            for item in self.children:
                item.disabled = True
            if self.game_id in self.bot.active_views:
                del self.bot.active_views[self.game_id]
            try:
                if game_msg:
                    embed = game_msg.embeds[0] if game_msg.embeds else None
                    if embed:
                        embed.title = f"{GAME_ICONS['ffa']} FREE FOR ALL — CLOSED"
                    await game_msg.edit(embed=embed, view=self)
            except Exception:
                pass

        view = ConfirmCloseView(_confirmed)
        await interaction.response.send_message("⚠️ Are you sure you want to end this game?", view=view, ephemeral=True)

    @discord.ui.button(
        label="❓ How to Play",
        style=discord.ButtonStyle.secondary,
        custom_id="ffa_htp",
    )
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        await interaction.response.send_message(HOW_TO_PLAY["ffa"], ephemeral=True)


class FFACog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(
        name="ffa",
        description="Start a Free For All — ask the server a question!",
    )
    @app_commands.describe(question="The question to ask")
    async def ffa(
        self,
        interaction: discord.Interaction,
        question: str,
    ):
        await self.start_ffa(interaction, question)

    async def start_ffa(
        self,
        interaction: discord.Interaction,
        question: str,
    ):
        log.info("%s used /games play ffa in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it with `/games config allow-channel`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={"question": question},
        )
        if game_id is None:
            try:
                await interaction.followup.send(
                    "I don't have access to send messages in that channel. "
                    "Please grant me **View Channel**, **Send Messages**, and **Embed Links**.",
                    ephemeral=True,
                )
            except Exception:
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
        question = options.get("question", "") or ""

        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "ffa",
            state="open",
            payload={"question": question},
        )

        log.info("Game %s (ffa) created by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))
        embed = build_ffa_embed(question)
        view = FFAView(game_id, host_id, question, self.db, self.bot)
        self.bot.active_views[game_id] = view

        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("ffa launch lacked send perms in channel %s", channel.id)
            return None
        view._game_msg = msg
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id


async def setup(bot: commands.Bot):
    cog = FFACog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("ffa")
    play.add_command(cog.ffa)
    bot.game_launchers["ffa"] = cog.launch
