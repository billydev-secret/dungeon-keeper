"""Needle cog — auto-thread creation for designated channels.

Inspired by discord-needle (github.com/MarcusOtter/discord-needle).
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.db_utils import get_config_value, open_db

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.needle")

TitleType = Literal["first_fifty", "first_line", "user_date", "custom"]

_DEFAULT_EMOJI_UNANSWERED = "🔵"
_DEFAULT_EMOJI_ARCHIVED = "✅"
_DEFAULT_EMOJI_LOCKED = "🔒"
_DEFAULT_REPLY = "Thread created by $USER in $CHANNEL"


# ── DB helpers ────────────────────────────────────────────────────────────────


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS needle_channels (
            guild_id            INTEGER NOT NULL,
            channel_id          INTEGER NOT NULL,
            title_type          TEXT    NOT NULL DEFAULT 'first_fifty',
            custom_title        TEXT    NOT NULL DEFAULT '',
            include_bots        INTEGER NOT NULL DEFAULT 0,
            slowmode            INTEGER NOT NULL DEFAULT 0,
            delete_behavior     TEXT    NOT NULL DEFAULT 'archive_if_empty',
            reply_type          TEXT    NOT NULL DEFAULT 'default',
            custom_reply        TEXT    NOT NULL DEFAULT '',
            status_reactions    INTEGER NOT NULL DEFAULT 0,
            archive_immediately INTEGER NOT NULL DEFAULT 0,
            default_reactions   TEXT    NOT NULL DEFAULT '',
            PRIMARY KEY (guild_id, channel_id)
        )
    """)


@dataclass
class NeedleChannelConfig:
    guild_id: int
    channel_id: int
    title_type: TitleType
    custom_title: str
    include_bots: bool
    slowmode: int
    delete_behavior: str
    reply_type: str
    custom_reply: str
    status_reactions: bool
    archive_immediately: bool
    default_reactions: str


@dataclass
class NeedleGlobalConfig:
    emoji_unanswered: str
    emoji_archived: str
    emoji_locked: str
    default_reply: str


def _row_to_config(row: sqlite3.Row) -> NeedleChannelConfig:
    return NeedleChannelConfig(
        guild_id=row["guild_id"],
        channel_id=row["channel_id"],
        title_type=row["title_type"],
        custom_title=row["custom_title"],
        include_bots=bool(row["include_bots"]),
        slowmode=row["slowmode"],
        delete_behavior=row["delete_behavior"],
        reply_type=row["reply_type"],
        custom_reply=row["custom_reply"],
        status_reactions=bool(row["status_reactions"]),
        archive_immediately=bool(row["archive_immediately"]),
        default_reactions=row["default_reactions"] or "",
    )


def _get_global_config(conn: sqlite3.Connection, guild_id: int) -> NeedleGlobalConfig:
    return NeedleGlobalConfig(
        emoji_unanswered=get_config_value(conn, "needle_emoji_unanswered", _DEFAULT_EMOJI_UNANSWERED, guild_id),
        emoji_archived=get_config_value(conn, "needle_emoji_archived", _DEFAULT_EMOJI_ARCHIVED, guild_id),
        emoji_locked=get_config_value(conn, "needle_emoji_locked", _DEFAULT_EMOJI_LOCKED, guild_id),
        default_reply=get_config_value(conn, "needle_default_reply", _DEFAULT_REPLY, guild_id),
    )


def _get_channel_config(
    conn: sqlite3.Connection, guild_id: int, channel_id: int
) -> NeedleChannelConfig | None:
    row = conn.execute(
        "SELECT * FROM needle_channels WHERE guild_id = ? AND channel_id = ?",
        (guild_id, channel_id),
    ).fetchone()
    return _row_to_config(row) if row else None


def _upsert_channel(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    channel_id: int,
    title_type: TitleType,
    custom_title: str,
    include_bots: bool,
    slowmode: int,
    delete_behavior: str,
    reply_type: str,
    custom_reply: str,
    status_reactions: bool,
    archive_immediately: bool,
    default_reactions: str,
) -> None:
    conn.execute(
        """
        INSERT INTO needle_channels
            (guild_id, channel_id, title_type, custom_title, include_bots, slowmode,
             delete_behavior, reply_type, custom_reply, status_reactions,
             archive_immediately, default_reactions)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (guild_id, channel_id) DO UPDATE SET
            title_type          = excluded.title_type,
            custom_title        = excluded.custom_title,
            include_bots        = excluded.include_bots,
            slowmode            = excluded.slowmode,
            delete_behavior     = excluded.delete_behavior,
            reply_type          = excluded.reply_type,
            custom_reply        = excluded.custom_reply,
            status_reactions    = excluded.status_reactions,
            archive_immediately = excluded.archive_immediately,
            default_reactions   = excluded.default_reactions
        """,
        (
            guild_id, channel_id, title_type, custom_title, int(include_bots), slowmode,
            delete_behavior, reply_type, custom_reply,
            int(status_reactions), int(archive_immediately),
            ",".join(e.strip() for e in default_reactions.split(",") if e.strip()),
        ),
    )


def _delete_channel(conn: sqlite3.Connection, guild_id: int, channel_id: int) -> int:
    cur = conn.execute(
        "DELETE FROM needle_channels WHERE guild_id = ? AND channel_id = ?",
        (guild_id, channel_id),
    )
    return cur.rowcount


def _list_channels(
    conn: sqlite3.Connection, guild_id: int
) -> list[NeedleChannelConfig]:
    rows = conn.execute(
        "SELECT * FROM needle_channels WHERE guild_id = ? ORDER BY channel_id",
        (guild_id,),
    ).fetchall()
    return [_row_to_config(r) for r in rows]


# ── Thread title logic ────────────────────────────────────────────────────────


def _build_thread_name(message: discord.Message, cfg: NeedleChannelConfig) -> str:
    content = message.clean_content.strip()
    if cfg.title_type == "first_line":
        title = content.split("\n", 1)[0].strip()
    elif cfg.title_type == "user_date":
        display = getattr(message.author, "display_name", None) or message.author.name
        date_str = message.created_at.strftime("%Y-%m-%d")
        title = f"{display} ({date_str})"
    elif cfg.title_type == "custom":
        display = getattr(message.author, "display_name", None) or message.author.name
        date_str = message.created_at.strftime("%Y-%m-%d")
        title = cfg.custom_title.replace("$USER", display).replace("$DATE", date_str)
    else:  # first_fifty
        title = content[:50].replace("\n", " ")
    return (title[:100].strip()) or "New Thread"


def _apply_variables(
    template: str,
    *,
    message: discord.Message,
    thread: discord.Thread,
) -> str:
    display = getattr(message.author, "display_name", None) or message.author.name
    return (
        template
        .replace("$USER", display)
        .replace("$CHANNEL", f"<#{message.channel.id}>")
        .replace("$THREAD", thread.mention)
    )


# ── Persistent welcome-message view ──────────────────────────────────────────


class NeedleTitleModal(discord.ui.Modal, title="Edit thread title"):
    new_title: discord.ui.TextInput = discord.ui.TextInput(
        label="New title",
        required=True,
        max_length=100,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("Not in a thread.", ephemeral=True)
            return
        name = str(self.new_title.value).strip()[:100]
        if not name:
            await interaction.response.send_message("Title can't be empty.", ephemeral=True)
            return
        await interaction.channel.edit(name=name)
        await interaction.response.send_message(f"Renamed to **{name}**.", ephemeral=True)


class NeedleThreadView(discord.ui.View):
    """Persistent view with Archive + Edit-Title buttons, attached to welcome messages."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Archive thread",
        style=discord.ButtonStyle.success,
        custom_id="needle:close",
    )
    async def close_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("Not in a thread.", ephemeral=True)
            return
        thread = interaction.channel
        if not _has_thread_perm(interaction.user, thread):
            await interaction.response.send_message(
                "Only the thread owner or a moderator can archive this thread.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message("Thread archived.")
        await thread.edit(archived=True, locked=False)

    @discord.ui.button(
        label="Edit title",
        style=discord.ButtonStyle.primary,
        custom_id="needle:title",
    )
    async def title_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("Not in a thread.", ephemeral=True)
            return
        if not _has_thread_perm(interaction.user, interaction.channel):
            await interaction.response.send_message(
                "Only the thread owner or a moderator can rename this thread.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(NeedleTitleModal())


def _has_thread_perm(user: discord.User | discord.Member, thread: discord.Thread) -> bool:
    if user.id == thread.owner_id:
        return True
    return isinstance(user, discord.Member) and user.guild_permissions.manage_threads


# ── Cog ──────────────────────────────────────────────────────────────────────


class NeedleCog(commands.Cog):
    needle = app_commands.Group(
        name="needle",
        description="Auto-thread channel management.",
        default_permissions=discord.Permissions(manage_threads=True),
    )

    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    async def cog_load(self) -> None:
        await asyncio.to_thread(self._init_db)
        self.bot.add_view(NeedleThreadView())

    def _init_db(self) -> None:
        with open_db(self.ctx.db_path) as conn:
            _ensure_tables(conn)

    # ── DB thread helpers ──────────────────────────────────────────────────

    def _load_channel_config(
        self, guild_id: int, channel_id: int
    ) -> NeedleChannelConfig | None:
        with open_db(self.ctx.db_path) as conn:
            return _get_channel_config(conn, guild_id, channel_id)

    def _load_global_config(self, guild_id: int) -> NeedleGlobalConfig:
        with open_db(self.ctx.db_path) as conn:
            return _get_global_config(conn, guild_id)

    def _upsert(self, *, guild_id: int, channel_id: int, **kwargs) -> None:  # type: ignore[override]
        with open_db(self.ctx.db_path) as conn:
            _upsert_channel(conn, guild_id=guild_id, channel_id=channel_id, **kwargs)

    def _delete(self, guild_id: int, channel_id: int) -> bool:
        with open_db(self.ctx.db_path) as conn:
            return bool(_delete_channel(conn, guild_id, channel_id))

    def _list(self, guild_id: int) -> list[NeedleChannelConfig]:
        with open_db(self.ctx.db_path) as conn:
            return _list_channels(conn, guild_id)

    # ── on_message ────────────────────────────────────────────────────────

    @commands.Cog.listener("on_message")
    async def _on_message(self, message: discord.Message) -> None:
        if not message.guild:
            return
        if message.is_system():
            return

        # Messages inside threads: handle archive_immediately
        if isinstance(message.channel, discord.Thread):
            await self._handle_thread_reply(message)
            return

        if not isinstance(message.channel, discord.TextChannel):
            return
        if self.bot.user and message.author.id == self.bot.user.id:
            return

        cfg = await asyncio.to_thread(
            self._load_channel_config, message.guild.id, message.channel.id
        )
        if cfg is None:
            return
        if message.author.bot and not cfg.include_bots:
            return

        name = _build_thread_name(message, cfg)
        try:
            thread, gcfg = await asyncio.gather(
                message.create_thread(
                    name=name,
                    auto_archive_duration=1440,
                    slowmode_delay=cfg.slowmode or None,
                ),
                asyncio.to_thread(self._load_global_config, message.guild.id),
            )
        except discord.Forbidden:
            log.warning(
                "Needle: missing permission in channel %s (guild %s)",
                message.channel.id, message.guild.id,
            )
            return
        except discord.HTTPException as exc:
            log.warning("Needle: failed to create thread: %s", exc)
            return

        # Welcome message with action buttons
        await self._post_welcome(message, thread, cfg, gcfg)

        # Status reaction: mark original message as unanswered
        if cfg.status_reactions and gcfg.emoji_unanswered:
            try:
                await message.add_reaction(gcfg.emoji_unanswered)
            except discord.HTTPException:
                pass

        # Default emoji reactions
        if cfg.default_reactions:
            for emoji in cfg.default_reactions.split(","):
                try:
                    await message.add_reaction(emoji)
                except discord.HTTPException:
                    log.debug("Needle: couldn't add reaction %r: skipping", emoji)

    async def _post_welcome(
        self,
        message: discord.Message,
        thread: discord.Thread,
        cfg: NeedleChannelConfig,
        gcfg: NeedleGlobalConfig,
    ) -> None:
        if cfg.reply_type == "none":
            return

        if cfg.reply_type == "custom":
            template = cfg.custom_reply
        else:
            template = gcfg.default_reply

        if not template.strip():
            return

        content = _apply_variables(template, message=message, thread=thread)
        try:
            msg = await thread.send(content, view=NeedleThreadView())
            # Pin if we have permission; delete the "pinned a message" system message
            if thread.guild.me and thread.permissions_for(thread.guild.me).manage_messages:
                await msg.pin()
                async for sys_msg in thread.history(limit=5):
                    if (
                        sys_msg.type == discord.MessageType.pins_add
                        and sys_msg.author.id == (self.bot.user.id if self.bot.user else 0)
                    ):
                        await sys_msg.delete()
                        break
        except discord.HTTPException as exc:
            log.warning("Needle: failed to post welcome message: %s", exc)

    async def _handle_thread_reply(self, message: discord.Message) -> None:
        """Remove the unanswered reaction when a non-OP replies (archive_immediately mode)."""
        if message.author.bot or not message.guild:
            return
        thread = message.channel
        if not isinstance(thread, discord.Thread) or thread.parent_id is None:
            return

        cfg = await asyncio.to_thread(
            self._load_channel_config, message.guild.id, thread.parent_id
        )
        if cfg is None or not cfg.status_reactions or not cfg.archive_immediately:
            return

        # Get the starter message (thread ID == starter message ID in Discord)
        parent = thread.parent
        if not isinstance(parent, discord.TextChannel):
            return
        try:
            starter, gcfg = await asyncio.gather(
                parent.fetch_message(thread.id),
                asyncio.to_thread(self._load_global_config, message.guild.id),
            )
        except discord.HTTPException:
            return

        # Only act when a non-OP replies
        if starter.author.id == message.author.id:
            return

        if gcfg.emoji_unanswered and self.bot.user:
            try:
                await starter.remove_reaction(gcfg.emoji_unanswered, self.bot.user)
            except discord.HTTPException:
                pass

    # ── on_message_delete ─────────────────────────────────────────────────

    @commands.Cog.listener("on_message_delete")
    async def _on_message_delete(self, message: discord.Message) -> None:
        if not message.guild:
            return
        thread = getattr(message, "thread", None)
        if not isinstance(thread, discord.Thread):
            return

        cfg = await asyncio.to_thread(
            self._load_channel_config, message.guild.id, message.channel.id
        )
        if cfg is None or cfg.delete_behavior == "nothing":
            return

        bot_member = message.guild.get_member(self.bot.user.id) if self.bot.user else None
        can_delete = (
            bot_member is not None
            and isinstance(message.channel, discord.TextChannel)
            and message.channel.permissions_for(bot_member).manage_threads
        )

        behavior = cfg.delete_behavior

        if behavior == "archive":
            try:
                await thread.edit(archived=True)
            except discord.HTTPException:
                pass
            return

        if behavior == "delete":
            if can_delete:
                try:
                    await thread.delete()
                except discord.HTTPException:
                    pass
            else:
                try:
                    await thread.edit(archived=True)
                except discord.HTTPException:
                    pass
            return

        # archive_if_empty: delete thread if only OP/bot messages, else archive
        try:
            messages = [m async for m in thread.history(limit=10)]
        except discord.HTTPException:
            messages = []
        is_empty = all(
            m.author.id in {message.author.id, self.bot.user.id if self.bot.user else 0}
            for m in messages
        )
        if is_empty and can_delete:
            try:
                await thread.delete()
                return
            except discord.HTTPException:
                pass
        try:
            await thread.edit(archived=True)
        except discord.HTTPException:
            pass

    # ── on_thread_update ─────────────────────────────────────────────────

    @commands.Cog.listener("on_thread_update")
    async def _on_thread_update(
        self, before: discord.Thread, after: discord.Thread
    ) -> None:
        if not after.guild or after.parent_id is None:
            return

        cfg = await asyncio.to_thread(
            self._load_channel_config, after.guild.id, after.parent_id
        )
        if cfg is None or not cfg.status_reactions:
            return

        was_archived = not before.archived and after.archived
        was_unarchived = before.archived and not after.archived
        was_locked = not before.locked and after.locked

        if not (was_archived or was_unarchived or was_locked):
            return

        parent = after.parent
        if not isinstance(parent, discord.TextChannel):
            return

        try:
            starter, gcfg = await asyncio.gather(
                parent.fetch_message(after.id),
                asyncio.to_thread(self._load_global_config, after.guild.id),
            )
        except discord.HTTPException:
            return

        # Clear all bot status reactions concurrently
        if self.bot.user:
            emojis = [e for e in [gcfg.emoji_unanswered, gcfg.emoji_archived, gcfg.emoji_locked] if e]
            await asyncio.gather(
                *(starter.remove_reaction(emoji, self.bot.user) for emoji in emojis),
                return_exceptions=True,
            )

        if was_locked and gcfg.emoji_locked:
            try:
                await starter.add_reaction(gcfg.emoji_locked)
            except discord.HTTPException:
                pass
        elif was_archived and gcfg.emoji_archived:
            try:
                await starter.add_reaction(gcfg.emoji_archived)
            except discord.HTTPException:
                pass
        # was_unarchived: reactions cleared above — user reopened, state is unknown

    # ── /needle add ───────────────────────────────────────────────────────

    @needle.command(name="add", description="Enable auto-threading in a channel.")
    @app_commands.describe(
        channel="Channel to auto-thread (defaults to current channel).",
        title_type="How to name new threads.",
        custom_title="Custom title template — supports $USER and $DATE.",
        include_bots="Also auto-thread messages from bots.",
        slowmode="Per-message slowmode in new threads (seconds, 0 = off).",
        delete_behavior="What to do when the original message is deleted.",
        reply_type="Welcome message to post in new threads.",
        custom_reply="Custom welcome text (reply_type=custom). Supports $USER, $CHANNEL, $THREAD.",
        status_reactions="Emoji-react to original message to show thread status.",
        archive_immediately="Remove unanswered reaction as soon as a non-OP replies.",
        default_reactions="Comma-separated emoji to always react with (e.g. 👍,👎).",
    )
    @app_commands.choices(
        title_type=[
            app_commands.Choice(name="First 50 chars (default)", value="first_fifty"),
            app_commands.Choice(name="First line of message",    value="first_line"),
            app_commands.Choice(name="Username + date",          value="user_date"),
            app_commands.Choice(name="Custom template",          value="custom"),
        ],
        delete_behavior=[
            app_commands.Choice(name="Delete if empty, else archive (default)", value="archive_if_empty"),
            app_commands.Choice(name="Always archive",  value="archive"),
            app_commands.Choice(name="Always delete",   value="delete"),
            app_commands.Choice(name="Do nothing",      value="nothing"),
        ],
        reply_type=[
            app_commands.Choice(name="Default server message", value="default"),
            app_commands.Choice(name="Custom message",         value="custom"),
            app_commands.Choice(name="No message",             value="none"),
        ],
    )
    async def needle_add(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        title_type: str = "first_fifty",
        custom_title: str = "",
        include_bots: bool = False,
        slowmode: int = 0,
        delete_behavior: str = "archive_if_empty",
        reply_type: str = "default",
        custom_reply: str = "",
        status_reactions: bool = False,
        archive_immediately: bool = False,
        default_reactions: str = "",
    ) -> None:
        assert interaction.guild is not None
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message(
                "Auto-threading only works in text channels.", ephemeral=True
            )
            return

        await asyncio.to_thread(
            self._upsert,
            guild_id=interaction.guild.id,
            channel_id=target.id,
            title_type=title_type,  # type: ignore[arg-type]
            custom_title=custom_title,
            include_bots=include_bots,
            slowmode=max(0, slowmode),
            delete_behavior=delete_behavior,
            reply_type=reply_type,
            custom_reply=custom_reply,
            status_reactions=status_reactions,
            archive_immediately=archive_immediately,
            default_reactions=default_reactions,
        )
        await interaction.response.send_message(
            f"Auto-threading enabled in {target.mention}.", ephemeral=True
        )

    # ── /needle remove ────────────────────────────────────────────────────

    @needle.command(name="remove", description="Disable auto-threading in a channel.")
    @app_commands.describe(channel="Channel to stop auto-threading (defaults to current).")
    async def needle_remove(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        assert interaction.guild is not None
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message("That's not a text channel.", ephemeral=True)
            return
        deleted = await asyncio.to_thread(self._delete, interaction.guild.id, target.id)
        if deleted:
            await interaction.response.send_message(
                f"Auto-threading disabled in {target.mention}.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"{target.mention} wasn't configured for auto-threading.", ephemeral=True
            )

    # ── /needle list ──────────────────────────────────────────────────────

    @needle.command(name="list", description="List channels with auto-threading enabled.")
    async def needle_list(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        configs = await asyncio.to_thread(self._list, interaction.guild.id)
        if not configs:
            await interaction.response.send_message(
                "No channels have auto-threading enabled.", ephemeral=True
            )
            return
        lines = []
        for cfg in configs:
            ch = interaction.guild.get_channel(cfg.channel_id)
            mention = ch.mention if ch else f"<#{cfg.channel_id}>"
            parts = [f"title: `{cfg.title_type}`", f"delete: `{cfg.delete_behavior}`"]
            if cfg.slowmode:
                parts.append(f"slowmode: {cfg.slowmode}s")
            if cfg.status_reactions:
                parts.append("reactions: ✓")
            if cfg.default_reactions:
                parts.append(f"reacts: {cfg.default_reactions}")
            lines.append(f"• {mention} — {', '.join(parts)}")
        await interaction.response.send_message(
            "**Auto-thread channels:**\n" + "\n".join(lines), ephemeral=True
        )

    # ── /close ────────────────────────────────────────────────────────────

    @app_commands.command(
        name="close",
        description="Archive this thread (thread owner or manage-threads).",
    )
    async def close(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "This command can only be used inside a thread.", ephemeral=True
            )
            return
        thread = interaction.channel
        if not _has_thread_perm(interaction.user, thread):
            await interaction.response.send_message(
                "Only the thread owner or a moderator can close this thread.", ephemeral=True
            )
            return
        await interaction.response.send_message("Thread archived.")
        await thread.edit(archived=True, locked=False)

    # ── /title ────────────────────────────────────────────────────────────

    @app_commands.command(
        name="title",
        description="Rename this thread (thread owner or manage-threads).",
    )
    @app_commands.describe(name="New title for this thread.")
    async def title(self, interaction: discord.Interaction, name: str) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "This command can only be used inside a thread.", ephemeral=True
            )
            return
        if not _has_thread_perm(interaction.user, interaction.channel):
            await interaction.response.send_message(
                "Only the thread owner or a moderator can rename this thread.", ephemeral=True
            )
            return
        name = name.strip()[:100]
        if not name:
            await interaction.response.send_message("Title can't be empty.", ephemeral=True)
            return
        await interaction.channel.edit(name=name)
        await interaction.response.send_message(f"Thread renamed to **{name}**.", ephemeral=True)


async def setup(bot: Bot) -> None:
    await bot.add_cog(NeedleCog(bot, bot.ctx))
