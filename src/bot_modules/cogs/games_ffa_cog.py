import asyncio
import io
import logging
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import GAME_ICONS
from bot_modules.games.utils.audit import send_audit_log
from bot_modules.games.utils.game_manager import (
    check_allowed_channel,
    create_game,
    get_active_game_by_id,
    modify_payload,
    update_game_message,
    update_session,
    end_game,
    ConfirmCloseView,
    channel_name,
)
from bot_modules.games.command_groups import play
from bot_modules.games_ffa.prompts import label_for_kind
from bot_modules.games.utils.question_source import (
    get_ffa_prompt,
    has_matching_questions,
    channel_allows_nsfw,
)
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
)

log = logging.getLogger(__name__)

CARD_FILENAME = "ffa.png"

# Theme per prompt type — truth reads cool/blue, dare reads hot/pink.
_THEME_FOR_LABEL = {"TRUTH": "midnight", "DARE": "rose"}

# Embed accent mirrors the card themes: TRUTH cool, DARE hot. These are
# semantic (truth vs dare), so they stay hardcoded rather than using the
# per-guild branding accent.
_EMBED_COLOUR_FOR_LABEL = {
    "TRUTH": discord.Colour(0x5B8DEF),  # cool blue
    "DARE": discord.Colour(0xE85A9B),   # hot rose
}
_DEFAULT_EMBED_COLOUR = discord.Colour(0xE85A9B)

MAX_EMBED_DESCRIPTION = 4096

REPLY_HELP = (
    "🎭 **Replying to a Truth or Dare**\n"
    "Your reply is posted by the bot with no name attached.\n\n"
    "• **Reply Anonymously** — you keep the *same* anonymous nickname for this "
    "prompt, so people can follow your back-and-forth.\n"
    "• **Reply as Someone New** — you get a *fresh* nickname every time, so "
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


def build_ffa_embed(
    text: str, label: str, *, colour: discord.Colour, reply_count: int = 0
) -> discord.Embed:
    """Embed for the non-threaded FFA mode.

    The prompt is rendered as a markdown blockquote (matching the confession
    look) under a TRUTH/DARE title. The footer flips to a running reply count
    once at least one anonymous reply has landed.
    """
    quoted = "\n".join(f"> {line}" for line in text.split("\n"))
    if len(quoted) > MAX_EMBED_DESCRIPTION:
        quoted = quoted[: MAX_EMBED_DESCRIPTION - 1].rstrip() + "…"
    embed = discord.Embed(
        title=f"{GAME_ICONS['ffa']} {label}",
        description=quoted,
        colour=colour,
    )
    if reply_count > 0:
        noun = "reply" if reply_count == 1 else "replies"
        embed.set_footer(text=f"Free For All • {reply_count} anonymous {noun}")
    else:
        embed.set_footer(text="Free For All • tap a button to reply anonymously")
    return embed


# ---------------------------------------------------------------------------
# Embed mode (── /games play ffa ──)
# Standard embed with anonymous replies posted back into the channel, a live
# reply-count footer, and a host Close button. Stateful: the view is bound to
# the embed message and re-registered on restart via recover_game.
# ---------------------------------------------------------------------------

class FFAEmbedReplyModal(discord.ui.Modal, title="Anonymous Reply"):
    answer = discord.ui.TextInput(
        label="Your reply",
        style=discord.TextStyle.paragraph,
        placeholder="Posted anonymously in this channel...",
        max_length=1000,
    )

    def __init__(self, game_view: "FFAEmbedView", *, ephemeral_identity: bool):
        super().__init__()
        self.game_view = game_view
        self.ephemeral_identity = ephemeral_identity

    async def on_submit(self, interaction: discord.Interaction):
        view = self.game_view
        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message(
                "These reply buttons only work inside a server channel.", ephemeral=True
            )
            return

        content = str(self.answer.value).strip()
        if not content:
            await interaction.response.send_message("Your reply can't be empty.", ephemeral=True)
            return

        # Extra network hops ahead (post + embed edit + audit) — defer so we
        # never bump into the 3s modal-response deadline.
        await interaction.response.defer(ephemeral=True)

        # The game may have been closed between opening this modal and now;
        # bail so we don't resurrect a closed embed's title/count.
        if not await get_active_game_by_id(view.db, view.game_id):
            await interaction.followup.send("This game has already been closed.", ephemeral=True)
            return

        db_path = interaction.client.ctx.db_path
        guild_id = interaction.guild.id
        # Identity keyed by the EMBED MESSAGE id: stable per-user for
        # "anonymous", fresh each time for "super anonymous".
        root_id = view._game_msg.id if view._game_msg else 0
        if self.ephemeral_identity:
            name_idx, emoji_idx = get_ephemeral_anon_identity(db_path, guild_id, root_id)
        else:
            name_idx, emoji_idx = get_or_assign_anon_identity(
                db_path, guild_id, root_id, interaction.user.id
            )
        body = build_anon_reply(
            content,
            is_op=False,
            circle=anon_circle_from_index(emoji_idx),
            anon_name=anon_name_from_index(name_idx),
        )

        # Post as a Discord reply to the prompt embed so it's clear which prompt
        # this answers (multiple FFAs can share a channel). fail_if_not_exists
        # keeps a deleted embed from erroring the reply.
        try:
            await interaction.channel.send(
                body,
                allowed_mentions=discord.AllowedMentions.none(),
                reference=discord.MessageReference(
                    message_id=root_id,
                    channel_id=interaction.channel_id,
                    guild_id=guild_id,
                    fail_if_not_exists=False,
                ),
            )
        except discord.HTTPException:
            await interaction.followup.send(
                "Couldn't post your reply — please try again.", ephemeral=True
            )
            return

        # Bump the running count and refresh the footer.
        def _bump(payload):
            payload["reply_count"] = int(payload.get("reply_count", 0)) + 1

        payload = await modify_payload(view.db, view.game_id, _bump)
        try:
            if view._game_msg:
                embed = build_ffa_embed(
                    view.text, view.label, colour=view.colour,
                    reply_count=int(payload.get("reply_count", 0)),
                )
                await view._game_msg.edit(embed=embed)
        except Exception:
            log.debug("ffa: failed to update reply-count footer", exc_info=True)

        # Audit log records the real user behind the pseudonym.
        try:
            await send_audit_log(
                interaction.client, interaction.client.games_db, interaction.guild,
                game_type="ffa", user=interaction.user,
                content=content, label="FFA Anonymous Reply",
            )
        except Exception:
            log.debug("ffa: failed to write audit log", exc_info=True)

        await interaction.followup.send("✅ Your reply has been posted!", ephemeral=True)


class FFAEmbedView(discord.ui.View):
    """Stateful persistent view bound to a single FFA embed message.

    Carries the anonymous-reply buttons (identity keyed by the embed message
    id), a host/mod Close button, and the state needed to keep the reply-count
    footer in sync. Re-registered after a restart by :meth:`FFACog.recover_game`.
    """

    def __init__(self, game_id: str, host_id: int, text: str, label: str,
                 colour: discord.Colour, db, bot):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.text = text
        self.label = label
        self.colour = colour
        self.db = db
        self.bot = bot
        self._game_msg: discord.Message | None = None

    async def _guard_active(self, interaction: discord.Interaction) -> bool:
        row = await get_active_game_by_id(self.db, self.game_id)
        if not row:
            await interaction.response.send_message(
                "This game is no longer active.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(
        label="Reply Anonymously",
        emoji="🎭",
        style=discord.ButtonStyle.secondary,
        custom_id="ffa_embed_reply_anon",
        row=0,
    )
    async def reply_anon(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard_active(interaction):
            return
        await interaction.response.send_modal(FFAEmbedReplyModal(self, ephemeral_identity=False))

    @discord.ui.button(
        label="Reply as Someone New",
        emoji="🎲",
        style=discord.ButtonStyle.secondary,
        custom_id="ffa_embed_reply_super",
        row=0,
    )
    async def reply_super(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard_active(interaction):
            return
        await interaction.response.send_modal(FFAEmbedReplyModal(self, ephemeral_identity=True))

    @discord.ui.button(
        label="What's this?",
        emoji="❓",
        style=discord.ButtonStyle.secondary,
        custom_id="ffa_embed_help",
        row=1,
    )
    async def reply_help(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(REPLY_HELP, ephemeral=True)

    @discord.ui.button(
        label="Close Game",
        emoji="🛑",
        style=discord.ButtonStyle.danger,
        custom_id="ffa_embed_close",
        row=1,
    )
    async def close_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed Close Game in #%s", interaction.user.display_name, getattr(interaction.channel, "name", "?"))
        if interaction.user.id != self.host_id:
            perms = interaction.user.guild_permissions if interaction.guild and isinstance(interaction.user, discord.Member) else None
            if not (perms and (perms.administrator or perms.manage_guild)):
                await interaction.response.send_message(
                    "Only the host or a mod can close this game.", ephemeral=True
                )
                return
        game_msg = self._game_msg

        async def _confirmed(confirm_interaction: discord.Interaction):
            await end_game(self.db, self.game_id)
            self.stop()
            for item in self.children:
                item.disabled = True
            self.bot.active_views.pop(self.game_id, None)
            try:
                if game_msg:
                    embed = game_msg.embeds[0] if game_msg.embeds else None
                    if embed:
                        embed.title = f"{GAME_ICONS['ffa']} {self.label} — CLOSED"
                    await game_msg.edit(embed=embed, view=self)
            except Exception:
                log.debug("ffa: failed to mark embed closed", exc_info=True)

        view = ConfirmCloseView(_confirmed)
        await interaction.response.send_message(
            "⚠️ Are you sure you want to end this game?", view=view, ephemeral=True
        )


class FFACog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    async def cog_load(self):
        # Ensure the shared anonymous-identity pool tables exist (idempotent).
        # Embed-mode reply views are per-message and recovered via recover_game;
        # the banner mode has no interactive state.
        init_confessions_db(self.bot.ctx.db_path)

    @app_commands.command(
        name="ffa",
        description="Post a Truth or Dare and collect anonymous replies!",
    )
    @app_commands.describe(
        kind="Truth, Dare, or a random pick (default: random)",
        tags="Comma-separated tags to filter the prompt bank",
        prompt="Write your own prompt instead of pulling a random one (optional)",
    )
    async def ffa(
        self,
        interaction: discord.Interaction,
        kind: Literal["random", "truth", "dare"] = "random",
        tags: str = "",
        prompt: str | None = None,
    ):
        await self.start_ffa(interaction, kind, tags, prompt, banner=False)

    @app_commands.command(
        name="ffa_banner",
        description="Drop a Truth or Dare prompt card in the channel!",
    )
    @app_commands.describe(
        kind="Truth, Dare, or a random pick (default: random)",
        tags="Comma-separated tags to filter the prompt bank",
        prompt="Write your own prompt instead of pulling a random one (optional)",
    )
    async def ffa_banner(
        self,
        interaction: discord.Interaction,
        kind: Literal["random", "truth", "dare"] = "random",
        tags: str = "",
        prompt: str | None = None,
    ):
        await self.start_ffa(interaction, kind, tags, prompt, banner=True)

    async def start_ffa(
        self,
        interaction: discord.Interaction,
        kind: str = "random",
        tags: str = "",
        prompt: str | None = None,
        *,
        banner: bool = False,
    ):
        cmd = "ffa_banner" if banner else "ffa"
        log.info("%s used /games play %s in #%s", interaction.user.display_name, cmd, channel_name(interaction.channel))
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it from the web dashboard.",
                ephemeral=True,
            )
            return

        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if tag_list and not (prompt or "").strip() and not await has_matching_questions(
            self.db, "ffa", tag_list, allow_nsfw=channel_allows_nsfw(interaction.channel)
        ):
            await interaction.response.send_message(
                f"No prompts match tags: {', '.join(tag_list)} for this game.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        launcher = self.launch_banner if banner else self.launch
        game_id = await launcher(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={"kind": kind, "tags": tag_list, "prompt": prompt or ""},
        )
        if game_id is None:
            perms = (
                "**View Channel**, **Send Messages**, and **Attach Files**."
                if banner
                else "**View Channel**, **Send Messages**, and **Embed Links**."
            )
            try:
                await interaction.followup.send(
                    f"I couldn't start the game here. Please grant me {perms}",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass

    async def _resolve_prompt(self, channel, options: dict):
        """Resolve (kind, tags, (label, text)) for a launch. (label, text) is None on miss."""
        kind = (options.get("kind") or "random").lower()
        tags = list(options.get("tags") or [])
        custom = (options.get("prompt") or "").strip()
        if custom:
            return kind, tags, (label_for_kind(kind), custom)
        picked = await get_ffa_prompt(
            self.db, kind=kind, tags=tags or None,
            allow_nsfw=channel_allows_nsfw(channel),
        )
        return kind, tags, picked

    async def launch(
        self,
        *,
        channel,
        host_id: int,
        host_name: str,
        guild_id: int,
        options: dict,
    ) -> str | None:
        """Embed mode (default). Standard embed + in-channel anonymous replies.

        Interaction-free (slash command + scheduler). Returns game_id, or None.
        """
        kind, tags, picked = await self._resolve_prompt(channel, options)
        if picked is None:
            log.info("ffa embed launch: no prompt for kind=%s tags=%s in channel %s", kind, tags, channel.id)
            return None
        label, text = picked
        colour = _EMBED_COLOUR_FOR_LABEL.get(label, _DEFAULT_EMBED_COLOUR)

        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "ffa",
            state="open",
            payload={
                "prompt": text,
                "label": label,
                "kind": kind,
                "tags": tags,
                "mode": "embed",
                "reply_count": 0,
            },
        )
        embed = build_ffa_embed(text, label, colour=colour, reply_count=0)
        view = FFAEmbedView(game_id, host_id, text, label, colour, self.db, self.bot)
        self.bot.active_views[game_id] = view

        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("ffa embed launch lacked send perms in channel %s", channel.id)
            return None

        view._game_msg = msg
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])
        log.info("Game %s (ffa/embed) posted by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))
        return game_id

    async def launch_banner(
        self,
        *,
        channel,
        host_id: int,
        host_name: str,
        guild_id: int,
        options: dict,
    ) -> str | None:
        """Banner mode. Drops a Truth-or-Dare card in the channel. Returns game_id, or None.

        No thread, no buttons — members just chat freely in the channel. The
        anonymous-reply flow lives on the embed command (:meth:`launch`).
        """
        kind, tags, picked = await self._resolve_prompt(channel, options)
        if picked is None:
            log.info("ffa banner launch: no prompt for kind=%s tags=%s in channel %s", kind, tags, channel.id)
            return None
        label, text = picked

        guild = getattr(channel, "guild", None)
        image_bytes = await _resolve_card_image(guild, self.bot, host_id)
        if image_bytes is None:
            log.warning("ffa banner launch could not resolve a card image in channel %s", channel.id)
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
            log.exception("ffa banner launch failed to render card in channel %s", channel.id)
            return None

        # Post the card (bare image — no thread, no buttons).
        try:
            msg = await channel.send(file=discord.File(io.BytesIO(card_bytes), filename=CARD_FILENAME))
        except discord.Forbidden:
            log.warning("ffa banner launch lacked send perms in channel %s", channel.id)
            return None

        # Record the play to history for stats (fire-and-forget: there's no
        # interactive state — people just chat in the channel).
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
                "tags": tags,
                "mode": "banner",
            },
        )
        log.info("Game %s (ffa/banner) posted by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))
        await update_session(self.db, channel.id, game_id, [host_id])
        await end_game(self.db, game_id)
        return game_id

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Re-register a stateful embed-mode FFA view after a restart.

        Banner games are fire-and-forget (ended immediately, never persisted as
        active), so only embed games recover.
        """
        if payload.get("mode") != "embed":
            return False
        game_id = row["game_id"]
        label = payload.get("label") or "TRUTH"
        text = payload.get("prompt", "") or ""
        colour = _EMBED_COLOUR_FOR_LABEL.get(label, _DEFAULT_EMBED_COLOUR)
        view = FFAEmbedView(game_id, int(row["host_id"]), text, label, colour, self.db, self.bot)
        view._game_msg = message
        self.bot.active_views[game_id] = view
        self.bot.add_view(view, message_id=message.id)
        log.info("Recovered ffa (embed) game %s in #%s", game_id, getattr(channel, "name", channel.id))
        return True


async def setup(bot: "Bot"):
    cog = FFACog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("ffa")
    bot.tree.remove_command("ffa_banner")
    play.add_command(cog.ffa, override=True)
    play.add_command(cog.ffa_banner, override=True)
    bot.game_launchers["ffa"] = cog.launch
    bot.game_launchers["ffa_banner"] = cog.launch_banner
    bot.game_recoverers["ffa"] = cog.recover_game
