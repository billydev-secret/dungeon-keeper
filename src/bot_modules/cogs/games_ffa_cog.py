import asyncio
import io
import logging
from typing import Literal

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
from bot_modules.games_ffa.embeds import build_ffa_embed, CARD_FILENAME
from bot_modules.games_ffa.logic import add_anon_reply
from bot_modules.games_ffa.prompts import pick_prompt, label_for_kind
from bot_modules.services.quote_renderer import render_quote_card, THEMES

log = logging.getLogger(__name__)

# Theme per prompt type — truth reads cool/blue, dare reads hot/pink.
_THEME_FOR_LABEL = {"TRUTH": "midnight", "DARE": "rose"}


async def _next_number(db, channel_id: int, label: str) -> int:
    """Per-channel sequential number for a label (e.g. TRUTH #5).

    Counts prior FFA cards of the same label in this channel across both
    live and archived games. Pre-rework history rows have no ``$.label``
    so they're excluded automatically. Called before ``create_game`` so
    the new card isn't yet counted — the ``+ 1`` makes it the next number.
    """
    row = await db.fetchone(
        """
        SELECT
          (SELECT COUNT(*) FROM games_active_games
             WHERE channel_id = ? AND game_type = 'ffa'
               AND json_extract(payload, '$.label') = ?)
        + (SELECT COUNT(*) FROM games_game_history
             WHERE channel_id = ? AND game_type = 'ffa'
               AND json_extract(payload, '$.label') = ?)
        """,
        (channel_id, label, channel_id, label),
    )
    return (int(row[0]) if row and row[0] is not None else 0) + 1


async def _resolve_card_image(guild: discord.Guild, bot, host_id: int) -> bytes | None:
    """Bytes for the card background — the server avatar, host avatar fallback.

    The card *is* the deliverable, so this tries hard to return something:
    guild icon first, then the host's avatar if the server has no icon.
    Returns None only if everything fails.
    """
    if guild is not None and guild.icon is not None:
        try:
            return await guild.icon.replace(size=512).read()
        except discord.HTTPException:
            log.warning("ffa: failed to read guild icon for %s", getattr(guild, "id", "?"))
    # Fallback: host avatar
    member = guild.get_member(host_id) if guild else None
    user = member
    if user is None:
        try:
            user = await bot.fetch_user(host_id)
        except discord.HTTPException:
            user = None
    if user is not None:
        try:
            return await user.display_avatar.with_size(512).read()
        except discord.HTTPException:
            log.warning("ffa: failed to read host avatar for %s", host_id)
    return None


class AnonymousReplyModal(discord.ui.Modal, title="Anonymous Reply"):
    answer = discord.ui.TextInput(
        label="Your Answer",
        style=discord.TextStyle.paragraph,
        placeholder="Type your anonymous answer here...",
        max_length=1000,
    )

    def __init__(self, game_id: str, db, ffa_view):
        super().__init__()
        self.game_id = game_id
        self.db = db
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

        # Post the reply into the card's thread (fall back to the card's
        # channel if the thread couldn't be created / was lost on restart).
        target = self.ffa_view.thread
        if target is None and self.ffa_view._game_msg is not None:
            target = self.ffa_view._game_msg.channel
        if target is not None:
            await target.send(f"💬 **Anonymous:** {discord.utils.escape_markdown(self.answer.value)}")
        await interaction.response.send_message(
            "✅ Your anonymous reply has been posted!", ephemeral=True
        )

        # Update reply-count footer on the card embed
        anon_replies = payload.get("anon_replies", {})
        if self.ffa_view._game_msg:
            try:
                embed = build_ffa_embed(
                    self.ffa_view.label,
                    self.ffa_view.number,
                    reply_count=len(anon_replies),
                )
                await self.ffa_view._game_msg.edit(embed=embed, attachments=self.ffa_view._game_msg.attachments)
            except Exception as e:
                log.debug("Failed to update FFA status bar: %s", e)


class FFAView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, label: str, number: int, db, bot):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.label = label
        self.number = number
        self.db = db
        self.bot = bot
        self._game_msg: discord.Message | None = None
        self.thread: discord.Thread | None = None

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
        modal = AnonymousReplyModal(self.game_id, self.db, self)
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
                        embed.title = f"{GAME_ICONS['ffa']} {self.label} #{self.number} — CLOSED"
                    await game_msg.edit(embed=embed, view=self, attachments=game_msg.attachments)
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
        description="Post a Truth or Dare card and open a thread for replies!",
    )
    @app_commands.describe(
        kind="Truth, Dare, or a random pick (default: random)",
        nsfw="Use the spicier prompt bank (default: off)",
        prompt="Write your own prompt instead of pulling a random one (optional)",
    )
    async def ffa(
        self,
        interaction: discord.Interaction,
        kind: Literal["random", "truth", "dare"] = "random",
        nsfw: bool = False,
        prompt: str | None = None,
    ):
        await self.start_ffa(interaction, kind, nsfw, prompt)

    async def start_ffa(
        self,
        interaction: discord.Interaction,
        kind: str = "random",
        nsfw: bool = False,
        prompt: str | None = None,
    ):
        log.info("%s used /games play ffa in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        if isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "Run this in a regular text channel — I can't open a new thread from inside a thread.",
                ephemeral=True,
            )
            return
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
            options={"kind": kind, "nsfw": nsfw, "prompt": prompt or ""},
        )
        if game_id is None:
            try:
                await interaction.followup.send(
                    "I couldn't start the game here. Please grant me **View Channel**, "
                    "**Send Messages**, **Embed Links**, **Attach Files**, and "
                    "**Create Public Threads**.",
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
        kind = (options.get("kind") or "random").lower()
        nsfw = bool(options.get("nsfw", False))
        custom = (options.get("prompt") or "").strip()

        if custom:
            label = label_for_kind(kind)
            text = custom
        else:
            label, text = pick_prompt(kind, nsfw)

        number = await _next_number(self.db, channel.id, label)

        guild = getattr(channel, "guild", None)
        image_bytes = await _resolve_card_image(guild, self.bot, host_id)
        if image_bytes is None:
            log.warning("ffa launch could not resolve a card image in channel %s", channel.id)
            return None

        try:
            card_bytes = await asyncio.to_thread(
                render_quote_card,
                text,
                author_name=f"{label} #{number}",
                avatar_bytes=image_bytes,
                theme=THEMES[_THEME_FOR_LABEL.get(label, "rose")],
            )
        except Exception:
            log.exception("ffa launch failed to render card in channel %s", channel.id)
            return None

        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "ffa",
            state="open",
            payload={
                "prompt": text,
                "label": label,
                "number": number,
                "kind": kind,
                "nsfw": nsfw,
                "anon_replies": {},
            },
        )

        log.info("Game %s (ffa) created by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))
        embed = build_ffa_embed(label, number)
        view = FFAView(game_id, host_id, label, number, self.db, self.bot)
        self.bot.active_views[game_id] = view

        try:
            msg = await channel.send(
                embed=embed,
                file=discord.File(io.BytesIO(card_bytes), filename=CARD_FILENAME),
                view=view,
            )
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("ffa launch lacked send perms in channel %s", channel.id)
            return None
        view._game_msg = msg

        # Open a thread for replies. If it fails (missing perms), keep the
        # card and fall back to channel replies rather than dropping the game.
        try:
            thread = await msg.create_thread(name=f"{label} #{number}", auto_archive_duration=1440)
            view.thread = thread
        except (discord.HTTPException, discord.Forbidden):
            log.warning("ffa: could not open reply thread in channel %s", channel.id)
            try:
                await channel.send(
                    "⚠️ I couldn't open a reply thread (I need **Create Public Threads**). "
                    "Anonymous replies will post here instead."
                )
            except Exception:
                pass

        await update_game_message(self.db, game_id, msg.id)
        if view.thread is not None:
            await modify_payload(self.db, game_id, lambda p: p.__setitem__("thread_id", view.thread.id))
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Re-register the FFA view after a restart so its buttons work again."""
        game_id = row["game_id"]
        label = payload.get("label", "TRUTH")
        number = int(payload.get("number", 0) or 0)
        view = FFAView(game_id, int(row["host_id"]), label, number, self.db, self.bot)
        view._game_msg = message

        thread_id = payload.get("thread_id")
        if thread_id:
            thread = self.bot.get_channel(int(thread_id))
            if thread is None:
                try:
                    thread = await self.bot.fetch_channel(int(thread_id))
                except discord.HTTPException:
                    thread = None
            if isinstance(thread, discord.Thread):
                view.thread = thread

        self.bot.active_views[game_id] = view
        self.bot.add_view(view, message_id=message.id)
        log.info("Recovered ffa game %s in #%s", game_id, getattr(channel, "name", channel.id))
        return True


async def setup(bot: commands.Bot):
    cog = FFACog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("ffa")
    play.add_command(cog.ffa)
    bot.game_launchers["ffa"] = cog.launch
    bot.game_recoverers["ffa"] = cog.recover_game
