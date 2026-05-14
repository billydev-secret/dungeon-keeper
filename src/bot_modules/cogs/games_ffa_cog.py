import logging

import discord
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import GOLDEN_MEADOW_COLOR, GAME_ICONS, HOW_TO_PLAY
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

log = logging.getLogger(__name__)


def build_ffa_embed(question: str, host_name: str, reply_count: int = 0) -> discord.Embed:
    embed = discord.Embed(
        title=f"{GAME_ICONS['ffa']} FREE FOR ALL",
        color=GOLDEN_MEADOW_COLOR,
    )
    embed.add_field(name="Question", value=f"# {discord.utils.escape_markdown(question)}", inline=False)
    footer_parts = [f"{GAME_ICONS['ffa']} Free For All"]
    if reply_count > 0:
        footer_parts.append(f"📊 {reply_count} anonymous replies")
    embed.set_footer(text=" • ".join(footer_parts))
    return embed


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
            anon_replies = payload.setdefault("anon_replies", {})
            anon_id = len(anon_replies) + 1
            anon_replies[str(anon_id)] = {
                "user_id": interaction.user.id,
                "text": self.answer.value,
            }
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
                    "",
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
        log.info("%s used /ffa in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
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
            "ffa",
            state="open",
            payload={"question": question},
        )

        log.info("Game %s (ffa) created by %s in #%s", game_id, interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        embed = build_ffa_embed(question, interaction.user.display_name)
        view = FFAView(game_id, interaction.user.id, question, self.db, self.bot)
        self.bot.active_views[game_id] = view

        await interaction.response.send_message(embed=embed, view=view)
        msg = await interaction.original_response()
        view._game_msg = msg
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, interaction.channel_id, game_id, [interaction.user.id])


async def setup(bot: commands.Bot):
    await bot.add_cog(FFACog(bot))
