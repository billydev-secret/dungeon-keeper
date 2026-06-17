import asyncio
import io
import logging
from typing import Literal

import discord
from discord.ext import commands
from discord import app_commands
from bot_modules.games.utils.audit import send_audit_log
from bot_modules.games.utils.game_manager import (
    check_allowed_channel,
    create_game,
    update_session,
    end_game,
)
from bot_modules.games.command_groups import play
from bot_modules.games_ffa.prompts import pick_prompt, label_for_kind
from bot_modules.services.quote_renderer import render_quote_card, THEMES
# Reuse the confession bot's anonymous-identity machinery so replies look and
# behave exactly like confession replies. These live in the confessions DB
# tables, which share the same SQLite file as the games DB.
from bot_modules.services.confessions_service import (
    init_db as init_confessions_db,
    get_or_assign_anon_identity,
    get_ephemeral_anon_identity,
    anon_name_from_index,
    anon_circle_from_index,
    build_anon_reply,
    thread_name_from_content,
)

log = logging.getLogger(__name__)

CARD_FILENAME = "ffa.png"

# Theme per prompt type — truth reads cool/blue, dare reads hot/pink.
_THEME_FOR_LABEL = {"TRUTH": "midnight", "DARE": "rose"}

REPLY_HELP = (
    "🎭 **Replying to a Truth or Dare**\n"
    "Your reply is posted by the bot with no name attached.\n\n"
    "• **Reply Anonymously** — you keep the *same* anonymous nickname for this "
    "thread, so people can follow your back-and-forth.\n"
    "• **Reply Super Anonymously** — you get a *fresh* nickname every time, so "
    "even your own replies can't be linked together.\n\n"
    "Mods can still see who actually sent a reply (logged for safety)."
)


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


class FFAReplyModal(discord.ui.Modal, title="Anonymous Reply"):
    answer = discord.ui.TextInput(
        label="Your reply",
        style=discord.TextStyle.paragraph,
        placeholder="Posted anonymously into this thread...",
        max_length=1000,
    )

    def __init__(self, *, ephemeral_identity: bool):
        super().__init__()
        self.ephemeral_identity = ephemeral_identity

    async def on_submit(self, interaction: discord.Interaction):
        thread = interaction.channel
        if not isinstance(thread, discord.Thread) or interaction.guild is None:
            await interaction.response.send_message(
                "These reply buttons only work inside a Truth-or-Dare thread.", ephemeral=True
            )
            return

        content = str(self.answer.value).strip()
        if not content:
            await interaction.response.send_message("Your reply can't be empty.", ephemeral=True)
            return

        db_path = interaction.client.ctx.db_path
        guild_id = interaction.guild.id
        # Identity keyed by THREAD id: stable per-user for "anonymous", fresh
        # each time for "super anonymous".
        if self.ephemeral_identity:
            name_idx, emoji_idx = get_ephemeral_anon_identity(db_path, guild_id, thread.id)
        else:
            name_idx, emoji_idx = get_or_assign_anon_identity(
                db_path, guild_id, thread.id, interaction.user.id
            )
        body = build_anon_reply(
            content,
            is_op=False,
            circle=anon_circle_from_index(emoji_idx),
            anon_name=anon_name_from_index(name_idx),
        )

        try:
            await thread.send(body, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            await interaction.response.send_message(
                "Couldn't post your reply — the thread may be locked or archived.", ephemeral=True
            )
            return

        # Audit log records the real user behind the pseudonym.
        try:
            await send_audit_log(
                interaction.client, interaction.client.games_db, interaction.guild,
                game_type="ffa", user=interaction.user,
                content=content, label="FFA Anonymous Reply",
            )
        except Exception:
            log.debug("ffa: failed to write audit log", exc_info=True)

        await interaction.response.send_message("✅ Your reply has been posted!", ephemeral=True)


class FFAReplyView(discord.ui.View):
    """Stateless persistent view posted inside each card's thread.

    Because the buttons act on whatever thread they live in (no game-specific
    state), one instance registered at startup handles every thread, surviving
    restarts without per-message recovery.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Reply Anonymously",
        emoji="🎭",
        style=discord.ButtonStyle.secondary,
        custom_id="ffa_reply_anon",
    )
    async def reply_anon(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed Reply Anonymously in #%s", interaction.user.display_name, getattr(interaction.channel, "name", "?"))
        await interaction.response.send_modal(FFAReplyModal(ephemeral_identity=False))

    @discord.ui.button(
        label="Reply Super Anonymously",
        emoji="🎲",
        style=discord.ButtonStyle.secondary,
        custom_id="ffa_reply_super",
    )
    async def reply_super(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed Reply Super Anonymously in #%s", interaction.user.display_name, getattr(interaction.channel, "name", "?"))
        await interaction.response.send_modal(FFAReplyModal(ephemeral_identity=True))

    @discord.ui.button(
        label="What's this?",
        emoji="❓",
        style=discord.ButtonStyle.secondary,
        custom_id="ffa_reply_help",
    )
    async def reply_help(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(REPLY_HELP, ephemeral=True)


class FFACog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    async def cog_load(self):
        # Ensure the shared anonymous-identity pool tables exist (idempotent),
        # and register the stateless thread reply view so it survives restarts.
        init_confessions_db(self.bot.ctx.db_path)
        self.bot.add_view(FFAReplyView())

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
                    "**Send Messages**, **Attach Files**, and **Create Public Threads**.",
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
            label, text = label_for_kind(kind), custom
        else:
            label, text = pick_prompt(kind, nsfw)

        guild = getattr(channel, "guild", None)
        image_bytes = await _resolve_card_image(guild, self.bot, host_id)
        if image_bytes is None:
            log.warning("ffa launch could not resolve a card image in channel %s", channel.id)
            return None

        try:
            card_bytes = await asyncio.to_thread(
                render_quote_card,
                text,
                author_name=label,
                avatar_bytes=image_bytes,
                theme=THEMES[_THEME_FOR_LABEL.get(label, "rose")],
                pfp_shape="none",
            )
        except Exception:
            log.exception("ffa launch failed to render card in channel %s", channel.id)
            return None

        # Post the card (bare image — no top-level buttons).
        try:
            msg = await channel.send(file=discord.File(io.BytesIO(card_bytes), filename=CARD_FILENAME))
        except discord.Forbidden:
            log.warning("ffa launch lacked send perms in channel %s", channel.id)
            return None

        # Open the thread (named after the prompt) and drop the reply buttons in it.
        thread = None
        try:
            thread = await msg.create_thread(
                name=thread_name_from_content(text), auto_archive_duration=1440
            )
            await thread.send(view=FFAReplyView())
        except (discord.HTTPException, discord.Forbidden):
            log.warning("ffa: could not open reply thread in channel %s", channel.id)
            try:
                await channel.send(
                    "⚠️ I couldn't open a reply thread (I need **Create Public Threads**)."
                )
            except Exception:
                pass

        # Record the play to history for stats (fire-and-forget: there's no
        # interactive game state to keep alive — the reply view is global).
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "ffa",
            message_id=msg.id,
            state="open",
            payload={
                "prompt": text,
                "label": label,
                "kind": kind,
                "nsfw": nsfw,
                "thread_id": thread.id if thread is not None else None,
            },
        )
        log.info("Game %s (ffa) posted by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))
        await update_session(self.db, channel.id, game_id, [host_id])
        await end_game(self.db, game_id)
        return game_id


async def setup(bot: commands.Bot):
    cog = FFACog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("ffa")
    play.add_command(cog.ffa)
    bot.game_launchers["ffa"] = cog.launch
