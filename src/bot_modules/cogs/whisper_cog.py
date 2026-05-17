"""Whisper cog — anonymous-message guessing game (Whisper clone)."""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.db_utils import open_db
from bot_modules.services.whisper_models import (
    STATE_PENDING,
    STATE_SHARED,
    Whisper,
    WhisperConfig,
    WhisperState,
)
from bot_modules.services.whisper_repo import (
    count_replies,
    delete_reply,
    delete_whisper,
    get_reply,
    get_whisper,
    get_whisper_config,
    insert_guess,
    insert_reply,
    insert_reply_report,
    insert_report,
    insert_whisper,
    list_received_in_states,
    list_sent,
    mark_exposed,
    mark_solved,
    set_whisper_launcher_message_id,
    set_whisper_message_ids,
    soft_delete_whisper,
    try_consume_guess,
    update_whisper_state,
)
from bot_modules.services.whisper_service import (
    ERROR_BOT_DM_FAILED,
    ERROR_GUESS_ALREADY_SOLVED,
    ERROR_GUESS_NO_ATTEMPTS,
    ERROR_GUESS_NOT_TARGET,
    MAX_MESSAGE_LENGTH,
    GuessValidationError,
    SendValidationError,
    TransitionValidationError,
    evaluate_guess,
    is_locked,
    is_terminal_for_sender,
    safe_codefence_content,
    validate_delete,
    validate_expose,
    validate_reply,
    validate_send,
    validate_share,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.whisper")



def _format_time_ago(created_at: float, now: float | None = None) -> str:
    import time as _t  # noqa: PLC0415
    current = now if now is not None else _t.time()
    delta = max(0, int(current - created_at))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    days = delta // 86400
    return f"{days} day{'s' if days != 1 else ''} ago"


_INBOX_PAGE_SIZE = 25  # max Discord dropdown options


# ── DB shims (sync, called via asyncio.to_thread) ────────────────────────────

def _load_config(db_path: Path, guild_id: int) -> WhisperConfig:
    with open_db(db_path) as conn:
        return get_whisper_config(conn, guild_id)


def _do_insert_whisper(
    db_path: Path,
    *,
    guild_id: int,
    sender_id: int,
    target_id: int,
    message: str,
) -> int:
    with open_db(db_path) as conn:
        return insert_whisper(
            conn, guild_id=guild_id, sender_id=sender_id,
            target_id=target_id, message=message,
        )


def _do_set_message_ids(
    db_path: Path, whisper_id: int, *, channel_msg_id: int, dm_msg_id: int
) -> None:
    with open_db(db_path) as conn:
        set_whisper_message_ids(
            conn, whisper_id, channel_msg_id=channel_msg_id, dm_msg_id=dm_msg_id
        )


def _do_load_whisper(db_path: Path, whisper_id: int) -> Whisper | None:
    with open_db(db_path) as conn:
        return get_whisper(conn, whisper_id)


def _do_delete_whisper(db_path: Path, whisper_id: int) -> None:
    with open_db(db_path) as conn:
        delete_whisper(conn, whisper_id)


def _do_record_guess(
    db_path: Path,
    *,
    whisper_id: int,
    guessed_id: int,
    correct: bool,
) -> bool:
    """Returns True if the guess was consumed. False means race-lost (someone else solved it first)."""
    with open_db(db_path) as conn:
        if not try_consume_guess(conn, whisper_id):
            return False
        insert_guess(conn, whisper_id=whisper_id, guessed_id=guessed_id, correct=correct)
        if correct:
            mark_solved(conn, whisper_id)
        return True


def _do_update_state(db_path: Path, whisper_id: int, new_state: WhisperState) -> None:
    with open_db(db_path) as conn:
        update_whisper_state(conn, whisper_id, new_state)


def _do_mark_exposed(db_path: Path, whisper_id: int) -> None:
    with open_db(db_path) as conn:
        mark_exposed(conn, whisper_id)


def _do_list_received_in_states(
    db_path: Path,
    *,
    guild_id: int,
    target_id: int,
    states: list[WhisperState],
) -> list[Whisper]:
    with open_db(db_path) as conn:
        return list_received_in_states(
            conn, guild_id=guild_id, target_id=target_id, states=states
        )


def _do_list_sent(
    db_path: Path, *, guild_id: int, sender_id: int
) -> list[Whisper]:
    with open_db(db_path) as conn:
        return list_sent(conn, guild_id=guild_id, sender_id=sender_id)


def _do_soft_delete(db_path: Path, whisper_id: int) -> None:
    with open_db(db_path) as conn:
        soft_delete_whisper(conn, whisper_id)


def _do_count_replies(db_path: Path, whisper_id: int) -> int:
    with open_db(db_path) as conn:
        return count_replies(conn, whisper_id)


def _do_set_launcher_id(db_path: Path, guild_id: int, message_id: int) -> None:
    with open_db(db_path) as conn:
        set_whisper_launcher_message_id(conn, guild_id, message_id)


def _do_insert_reply(
    db_path: Path,
    *,
    whisper_id: int,
    from_user_id: int,
    to_user_id: int,
    content: str,
) -> int:
    with open_db(db_path) as conn:
        return insert_reply(
            conn,
            whisper_id=whisper_id,
            from_user_id=from_user_id,
            to_user_id=to_user_id,
            content=content,
        )


def _do_insert_report(
    db_path: Path,
    *,
    whisper_id: int,
    reporter_id: int,
    reason: str,
) -> bool:
    """Returns True if inserted, False if this reporter already reported this whisper."""
    with open_db(db_path) as conn:
        return insert_report(
            conn,
            whisper_id=whisper_id,
            reporter_id=reporter_id,
            reason=reason,
        )


def _do_get_reply(db_path: Path, reply_id: int):
    with open_db(db_path) as conn:
        return get_reply(conn, reply_id)


def _do_delete_reply(db_path: Path, reply_id: int) -> None:
    with open_db(db_path) as conn:
        delete_reply(conn, reply_id)


def _do_insert_reply_report(
    db_path: Path,
    *,
    reply_id: int,
    reporter_id: int,
    reason: str,
) -> bool:
    """Returns True if inserted, False if duplicate."""
    with open_db(db_path) as conn:
        return insert_reply_report(
            conn,
            reply_id=reply_id,
            reporter_id=reporter_id,
            reason=reason,
        )


# ── Per-whisper Dynamic buttons (custom_id contains whisper_id) ──────────────
#
# These use discord.ui.DynamicItem so that after a bot restart the button
# clicks on existing DMs / feed messages still route correctly via regex
# matching of the persisted custom_id.


class WhisperGuessButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"whisper:guess:(?P<id>\d+)"),
):
    def __init__(
        self,
        bot: Bot,
        whisper_id: int,
        *,
        index: int | None = None,
        row: int | None = None,
    ) -> None:
        label = f"Guess #{index}" if index else "Guess"
        super().__init__(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary,
                custom_id=f"whisper:guess:{whisper_id}",
                row=row,
            )
        )
        self.bot = bot
        self.whisper_id = whisper_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> WhisperGuessButton:
        return cls(interaction.client, int(match["id"]))  # type: ignore[arg-type]

    async def callback(self, interaction: discord.Interaction) -> None:
        whisper = await asyncio.to_thread(
            _do_load_whisper, self.bot.ctx.db_path, self.whisper_id
        )
        if whisper is None:
            await interaction.response.send_message("Whisper not found.", ephemeral=True)
            return
        if interaction.user.id != whisper.target_id:
            await interaction.response.send_message(ERROR_GUESS_NOT_TARGET, ephemeral=True)
            return
        if whisper.solved:
            await interaction.response.send_message(ERROR_GUESS_ALREADY_SOLVED, ephemeral=True)
            return
        if whisper.guesses_left <= 0:
            await interaction.response.send_message(ERROR_GUESS_NO_ATTEMPTS, ephemeral=True)
            return

        guild = interaction.guild or self.bot.get_guild(whisper.guild_id)
        if guild is None:
            await interaction.response.send_message(
                "Couldn't find the server — try again.", ephemeral=True
            )
            return
        cfg = await asyncio.to_thread(_load_config, self.bot.ctx.db_path, whisper.guild_id)
        if cfg.role_id == 0:
            await interaction.response.send_message(
                "Whisper role isn't configured.", ephemeral=True
            )
            return
        role = guild.get_role(cfg.role_id)
        if role is None:
            await interaction.response.send_message(
                "Whisper role no longer exists.", ephemeral=True
            )
            return

        members = sorted(
            [m for m in role.members if m.id != whisper.target_id],
            key=lambda m: m.display_name.lower(),
        )
        if not members:
            await interaction.response.send_message(
                "No other opted-in members to guess from.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            ephemeral=True,
            view=WhisperGuessSelectView(self.bot, self.whisper_id, members),
        )


class WhisperShareButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"whisper:share:(?P<id>\d+)"),
):
    def __init__(
        self,
        bot: Bot,
        whisper_id: int,
        *,
        index: int | None = None,
        row: int | None = None,
    ) -> None:
        label = f"Share #{index}" if index else "Share"
        super().__init__(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.success,
                custom_id=f"whisper:share:{whisper_id}",
                row=row,
            )
        )
        self.bot = bot
        self.whisper_id = whisper_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> WhisperShareButton:
        return cls(interaction.client, int(match["id"]))  # type: ignore[arg-type]

    async def callback(self, interaction: discord.Interaction) -> None:
        whisper = await asyncio.to_thread(
            _do_load_whisper, self.bot.ctx.db_path, self.whisper_id
        )
        if whisper is None:
            await interaction.response.send_message("Whisper not found.", ephemeral=True)
            return
        try:
            validate_share(whisper, invoker_id=interaction.user.id)
        except TransitionValidationError as e:
            await interaction.response.send_message(e.message, ephemeral=True)
            return

        await asyncio.to_thread(
            _do_update_state, self.bot.ctx.db_path, self.whisper_id, STATE_SHARED
        )

        guild = interaction.guild or self.bot.get_guild(whisper.guild_id)
        if guild:
            cfg = await asyncio.to_thread(
                _load_config, self.bot.ctx.db_path, whisper.guild_id
            )
            feed_channel = guild.get_channel(cfg.channel_id)
            if isinstance(feed_channel, discord.TextChannel):
                if whisper.channel_msg_id:
                    try:
                        old = await feed_channel.fetch_message(whisper.channel_msg_id)
                        await old.delete()
                    except discord.HTTPException:
                        log.warning("Failed to delete original announcement on share")
                try:
                    new_msg = await feed_channel.send(
                        f"\U0001f4ec A fresh Whisper was shared. Someone sent "
                        f"<@{whisper.target_id}> an anonymous message!\n"
                        f"```{safe_codefence_content(whisper.message)}```",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    await asyncio.to_thread(
                        _do_set_message_ids,
                        self.bot.ctx.db_path,
                        self.whisper_id,
                        channel_msg_id=new_msg.id,
                        dm_msg_id=whisper.dm_msg_id or 0,
                    )
                except discord.HTTPException:
                    log.warning("Failed to post share announcement to feed")

        if (
            interaction.message
            and whisper.dm_msg_id
            and interaction.message.id == whisper.dm_msg_id
        ):
            new_view: discord.ui.View | None
            if whisper.guesses_left > 0 and not whisper.solved:
                new_view = WhisperDmView.without_decide(self.bot, self.whisper_id)
            else:
                new_view = None
            try:
                await interaction.message.edit(view=new_view)
            except discord.HTTPException:
                log.warning("Failed to edit DM view after share")

        await interaction.response.send_message(
            "Shared to the whisper feed.", ephemeral=True
        )


class WhisperDeleteButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"whisper:delete:(?P<id>\d+)"),
):
    def __init__(
        self,
        bot: Bot,
        whisper_id: int,
        *,
        row: int | None = None,
    ) -> None:
        super().__init__(
            discord.ui.Button(
                label="Delete",
                style=discord.ButtonStyle.secondary,
                custom_id=f"whisper:delete:{whisper_id}",
                row=row,
            )
        )
        self.bot = bot
        self.whisper_id = whisper_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> WhisperDeleteButton:
        return cls(interaction.client, int(match["id"]))  # type: ignore[arg-type]

    async def callback(self, interaction: discord.Interaction) -> None:
        whisper = await asyncio.to_thread(
            _do_load_whisper, self.bot.ctx.db_path, self.whisper_id
        )
        if whisper is None:
            await interaction.response.send_message("Whisper not found.", ephemeral=True)
            return
        try:
            validate_delete(whisper, invoker_id=interaction.user.id)
        except TransitionValidationError as e:
            await interaction.response.send_message(e.message, ephemeral=True)
            return

        await asyncio.to_thread(
            _do_soft_delete, self.bot.ctx.db_path, self.whisper_id
        )

        # DM-context: clear buttons on the DM (terminal action for that whisper).
        if (
            interaction.message
            and whisper.dm_msg_id
            and interaction.message.id == whisper.dm_msg_id
        ):
            try:
                await interaction.message.edit(view=None)
            except discord.HTTPException:
                log.warning("Failed to clear DM view after delete")

        await interaction.response.send_message(
            "Whisper removed from your inbox.", ephemeral=True
        )


class WhisperExposeButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"whisper:expose:(?P<id>\d+)"),
):
    def __init__(
        self,
        bot: Bot,
        whisper_id: int,
        *,
        index: int | None = None,
        row: int | None = None,
    ) -> None:
        label = f"Expose #{index}" if index else "Expose"
        super().__init__(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.danger,
                custom_id=f"whisper:expose:{whisper_id}",
                row=row,
            )
        )
        self.bot = bot
        self.whisper_id = whisper_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> WhisperExposeButton:
        return cls(interaction.client, int(match["id"]))  # type: ignore[arg-type]

    async def callback(self, interaction: discord.Interaction) -> None:
        whisper = await asyncio.to_thread(
            _do_load_whisper, self.bot.ctx.db_path, self.whisper_id
        )
        if whisper is None:
            await interaction.response.send_message("Whisper not found.", ephemeral=True)
            return
        try:
            validate_expose(whisper, invoker_id=interaction.user.id)
        except TransitionValidationError as e:
            await interaction.response.send_message(e.message, ephemeral=True)
            return

        await asyncio.to_thread(
            _do_mark_exposed, self.bot.ctx.db_path, self.whisper_id
        )

        sender_member = (
            interaction.guild.get_member(whisper.sender_id)
            if interaction.guild else None
        )
        sender_label = (
            sender_member.mention if sender_member else f"<@{whisper.sender_id}>"
        )

        if interaction.message:
            try:
                new_content = (
                    (interaction.message.content or "")
                    + f"\n\n\U0001f4a5 Sender: {sender_label}\n"
                    f"```{safe_codefence_content(whisper.message)}```"
                )
                await interaction.message.edit(
                    content=new_content,
                    view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                log.warning("Failed to edit message on expose")

        await interaction.response.send_message("Revealed.", ephemeral=True)


class WhisperReplyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"whisper:reply:(?P<id>\d+)"),
):
    def __init__(
        self,
        bot: Bot,
        whisper_id: int,
        *,
        index: int | None = None,
        row: int | None = None,
    ) -> None:
        label = f"Reply #{index}" if index else "Reply"
        super().__init__(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.success,
                custom_id=f"whisper:reply:{whisper_id}",
                row=row,
            )
        )
        self.bot = bot
        self.whisper_id = whisper_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> WhisperReplyButton:
        return cls(interaction.client, int(match["id"]))  # type: ignore[arg-type]

    async def callback(self, interaction: discord.Interaction) -> None:
        whisper = await asyncio.to_thread(
            _do_load_whisper, self.bot.ctx.db_path, self.whisper_id
        )
        if whisper is None:
            await interaction.response.send_message("Whisper not found.", ephemeral=True)
            return
        reply_count = await asyncio.to_thread(
            _do_count_replies, self.bot.ctx.db_path, self.whisper_id
        )
        try:
            validate_reply(
                whisper,
                invoker_id=interaction.user.id,
                reply_count=reply_count,
            )
        except TransitionValidationError as e:
            await interaction.response.send_message(e.message, ephemeral=True)
            return
        await interaction.response.send_modal(
            WhisperReplyModal(self.bot, self.whisper_id)
        )


class WhisperReportButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"whisper:report:(?P<id>\d+)"),
):
    def __init__(
        self,
        bot: Bot,
        whisper_id: int,
        *,
        index: int | None = None,
        row: int | None = None,
    ) -> None:
        label = f"Report #{index}" if index else "Report"
        super().__init__(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.danger,
                custom_id=f"whisper:report:{whisper_id}",
                row=row,
            )
        )
        self.bot = bot
        self.whisper_id = whisper_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> WhisperReportButton:
        return cls(interaction.client, int(match["id"]))  # type: ignore[arg-type]

    async def callback(self, interaction: discord.Interaction) -> None:
        whisper = await asyncio.to_thread(
            _do_load_whisper, self.bot.ctx.db_path, self.whisper_id
        )
        if whisper is None:
            await interaction.response.send_message("Whisper not found.", ephemeral=True)
            return
        if interaction.user.id != whisper.target_id:
            await interaction.response.send_message(
                "Only the recipient can report a whisper.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            WhisperReportModal(self.bot, self.whisper_id)
        )


# ── Reply / Report modals ────────────────────────────────────────────────────


class WhisperReplyModal(discord.ui.Modal, title="Reply anonymously"):
    reply_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Your reply",
        style=discord.TextStyle.long,
        required=True,
        max_length=1000,
        placeholder="Your identity is logged by admins for moderation.",
    )

    def __init__(self, bot: Bot, whisper_id: int) -> None:
        super().__init__()
        self.bot = bot
        self.whisper_id = whisper_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        whisper = await asyncio.to_thread(
            _do_load_whisper, self.bot.ctx.db_path, self.whisper_id
        )
        if whisper is None:
            await interaction.response.send_message(
                "Whisper not found.", ephemeral=True
            )
            return
        reply_count = await asyncio.to_thread(
            _do_count_replies, self.bot.ctx.db_path, self.whisper_id
        )
        try:
            validate_reply(
                whisper,
                invoker_id=interaction.user.id,
                reply_count=reply_count,
            )
        except TransitionValidationError as e:
            await interaction.response.send_message(e.message, ephemeral=True)
            return

        to_user_id = (
            whisper.sender_id
            if interaction.user.id == whisper.target_id
            else whisper.target_id
        )

        content = str(self.reply_input.value).strip()
        if not content:
            await interaction.response.send_message(
                "Reply can't be empty.", ephemeral=True
            )
            return

        # Persist first so we have the reply_id for the report button.
        reply_id = await asyncio.to_thread(
            _do_insert_reply,
            self.bot.ctx.db_path,
            whisper_id=self.whisper_id,
            from_user_id=interaction.user.id,
            to_user_id=to_user_id,
            content=content,
        )

        recipient = interaction.client.get_user(to_user_id) or await interaction.client.fetch_user(to_user_id)  # type: ignore[attr-defined]
        try:
            preview = whisper.message
            if len(preview) > 200:
                preview = preview[:197] + "…"
            await recipient.send(
                f"\U0001f4ec Anonymous reply on Whisper #{self.whisper_id} *(\"{safe_codefence_content(preview)}\")*:\n"
                f"```{safe_codefence_content(content)}```",
                view=WhisperReplyDmView(self.bot, self.whisper_id, reply_id=reply_id),
            )
        except (discord.Forbidden, discord.HTTPException):
            await asyncio.to_thread(_do_delete_reply, self.bot.ctx.db_path, reply_id)
            await interaction.response.send_message(
                "Couldn't deliver — they have DMs disabled.", ephemeral=True
            )
            return

        # Post reply to mod log (best-effort — don't fail the reply if this fails)
        try:
            cfg = await asyncio.to_thread(
                _load_config, self.bot.ctx.db_path, whisper.guild_id
            )
            if cfg.log_channel_id:
                guild = self.bot.get_guild(whisper.guild_id)
                if guild:
                    log_channel = guild.get_channel(cfg.log_channel_id)
                    if isinstance(log_channel, discord.TextChannel):
                        emb = discord.Embed(
                            title="Whisper Reply",
                            description=safe_codefence_content(content),
                            timestamp=discord.utils.utcnow(),
                        )
                        emb.add_field(
                            name="From",
                            value=f"<@{interaction.user.id}> (`{interaction.user.id}`)",
                            inline=False,
                        )
                        emb.add_field(
                            name="To",
                            value=f"<@{to_user_id}> (`{to_user_id}`)",
                            inline=False,
                        )
                        emb.add_field(
                            name="Whisper ID",
                            value=str(self.whisper_id),
                            inline=False,
                        )
                        await log_channel.send(
                            embed=emb,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
        except Exception:
            log.warning("Failed to post whisper reply to mod log")

        await interaction.response.send_message(
            "Reply delivered anonymously.", ephemeral=True
        )


class WhisperReplyDmView(discord.ui.View):
    """View attached to incoming reply DMs. The reply chain ends here — only a
    Report button is offered for moderation. (One reply per whisper.)"""

    def __init__(self, bot: Bot, whisper_id: int, *, reply_id: int | None = None) -> None:
        super().__init__(timeout=None)
        # whisper_id is preserved on the view for backwards compatibility with
        # any callers that still pass it; no button uses it any more.
        _ = whisper_id  # noqa: F841
        if reply_id is not None:
            self.add_item(WhisperReportReplyButton(bot, reply_id))


class WhisperReportModal(discord.ui.Modal, title="Report whisper"):
    reason_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Reason (optional)",
        style=discord.TextStyle.long,
        required=False,
        max_length=500,
    )

    def __init__(self, bot: Bot, whisper_id: int) -> None:
        super().__init__()
        self.bot = bot
        self.whisper_id = whisper_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        whisper = await asyncio.to_thread(
            _do_load_whisper, self.bot.ctx.db_path, self.whisper_id
        )
        if whisper is None:
            await interaction.response.send_message(
                "Whisper not found.", ephemeral=True
            )
            return
        if interaction.user.id != whisper.target_id:
            await interaction.response.send_message(
                "Only the recipient can report a whisper.", ephemeral=True
            )
            return

        cfg = await asyncio.to_thread(
            _load_config, self.bot.ctx.db_path, whisper.guild_id
        )
        if interaction.guild is None or cfg.log_channel_id == 0:
            await interaction.response.send_message(
                "Mod log channel isn't configured. Report not delivered.",
                ephemeral=True,
            )
            return
        log_channel = interaction.guild.get_channel(cfg.log_channel_id)
        if not isinstance(log_channel, discord.TextChannel):
            await interaction.response.send_message(
                "Mod log channel is misconfigured. Report not delivered.",
                ephemeral=True,
            )
            return

        reason = str(self.reason_input.value).strip() or "(no reason provided)"

        inserted = await asyncio.to_thread(
            _do_insert_report,
            self.bot.ctx.db_path,
            whisper_id=whisper.id,
            reporter_id=interaction.user.id,
            reason=reason,
        )
        if not inserted:
            await interaction.response.send_message(
                "You've already reported this whisper.", ephemeral=True
            )
            return

        emb = discord.Embed(
            title="Whisper Reported",
            description=safe_codefence_content(whisper.message),
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        emb.add_field(
            name="Sender",
            value=f"<@{whisper.sender_id}> (`{whisper.sender_id}`)",
            inline=False,
        )
        emb.add_field(
            name="Reporter (Target)",
            value=f"<@{whisper.target_id}> (`{whisper.target_id}`)",
            inline=False,
        )
        emb.add_field(name="Reason", value=reason, inline=False)
        emb.add_field(name="Whisper ID", value=str(whisper.id), inline=False)
        try:
            await log_channel.send(
                embed=emb, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException:
            await interaction.response.send_message(
                "Failed to deliver report (bot can't post to mod log).",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Report submitted to moderators.", ephemeral=True
        )


class WhisperReportReplyModal(discord.ui.Modal, title="Report reply"):
    reason_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Reason (optional)",
        style=discord.TextStyle.long,
        required=False,
        max_length=500,
    )

    def __init__(self, bot: Bot, reply_id: int) -> None:
        super().__init__()
        self.bot = bot
        self.reply_id = reply_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        reply = await asyncio.to_thread(
            _do_get_reply, self.bot.ctx.db_path, self.reply_id
        )
        if reply is None:
            await interaction.response.send_message("Reply not found.", ephemeral=True)
            return
        if interaction.user.id != reply.to_user_id:
            await interaction.response.send_message(
                "Only the recipient can report a reply.", ephemeral=True
            )
            return

        whisper = await asyncio.to_thread(
            _do_load_whisper, self.bot.ctx.db_path, reply.whisper_id
        )
        if whisper is None:
            await interaction.response.send_message("Whisper not found.", ephemeral=True)
            return

        cfg = await asyncio.to_thread(_load_config, self.bot.ctx.db_path, whisper.guild_id)
        if interaction.guild is None or cfg.log_channel_id == 0:
            await interaction.response.send_message(
                "Mod log channel isn't configured. Report not delivered.",
                ephemeral=True,
            )
            return
        log_channel = interaction.guild.get_channel(cfg.log_channel_id)
        if not isinstance(log_channel, discord.TextChannel):
            await interaction.response.send_message(
                "Mod log channel is misconfigured. Report not delivered.",
                ephemeral=True,
            )
            return

        reason = str(self.reason_input.value).strip() or "(no reason provided)"

        inserted = await asyncio.to_thread(
            _do_insert_reply_report,
            self.bot.ctx.db_path,
            reply_id=self.reply_id,
            reporter_id=interaction.user.id,
            reason=reason,
        )
        if not inserted:
            await interaction.response.send_message(
                "You've already reported this reply.", ephemeral=True
            )
            return

        emb = discord.Embed(
            title="Whisper Reply Reported",
            description=safe_codefence_content(reply.content),
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        emb.add_field(
            name="Sender (anonymous)",
            value=f"<@{reply.from_user_id}> (`{reply.from_user_id}`)",
            inline=False,
        )
        emb.add_field(
            name="Reporter (recipient)",
            value=f"<@{interaction.user.id}> (`{interaction.user.id}`)",
            inline=False,
        )
        emb.add_field(name="Reason", value=reason, inline=False)
        emb.add_field(name="Reply ID", value=str(self.reply_id), inline=False)
        emb.add_field(name="Whisper ID", value=str(reply.whisper_id), inline=False)
        try:
            await log_channel.send(
                embed=emb, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException:
            await interaction.response.send_message(
                "Failed to deliver report (bot can't post to mod log).",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Reply reported to moderators.", ephemeral=True
        )


class WhisperReportReplyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"whisper:report_reply:(?P<id>\d+)"),
):
    def __init__(self, bot: Bot, reply_id: int, *, row: int | None = None) -> None:
        super().__init__(
            discord.ui.Button(
                label="Report",
                style=discord.ButtonStyle.danger,
                custom_id=f"whisper:report_reply:{reply_id}",
                row=row,
            )
        )
        self.bot = bot
        self.reply_id = reply_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> WhisperReportReplyButton:
        return cls(interaction.client, int(match["id"]))  # type: ignore[arg-type]

    async def callback(self, interaction: discord.Interaction) -> None:
        reply = await asyncio.to_thread(
            _do_get_reply, self.bot.ctx.db_path, self.reply_id
        )
        if reply is None:
            await interaction.response.send_message("Reply not found.", ephemeral=True)
            return
        if interaction.user.id != reply.to_user_id:
            await interaction.response.send_message(
                "Only the recipient can report a reply.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            WhisperReportReplyModal(self.bot, self.reply_id)
        )


# ── Expose view: posted in feed channel after correct guess ──────────────────

class WhisperExposeView(discord.ui.View):
    def __init__(self, bot: Bot, whisper_id: int) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.whisper_id = whisper_id
        self.add_item(WhisperExposeButton(bot, whisper_id))


# ── Per-whisper DM view (Guess + Share + Delete) ─────────────────────────────
class WhisperDmView(discord.ui.View):
    def __init__(self, bot: Bot, whisper_id: int) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.whisper_id = whisper_id
        self.add_item(WhisperGuessButton(bot, whisper_id))
        self.add_item(WhisperShareButton(bot, whisper_id))
        self.add_item(WhisperDeleteButton(bot, whisper_id))

    @classmethod
    def without_guess(cls, bot: Bot, whisper_id: int) -> WhisperDmView:
        """Build a DM view that omits the Guess button (used after the
        target exhausts their guesses or the whisper is solved)."""
        v: WhisperDmView = cls.__new__(cls)
        discord.ui.View.__init__(v, timeout=None)
        v.bot = bot
        v.whisper_id = whisper_id
        v.add_item(WhisperShareButton(bot, whisper_id))
        v.add_item(WhisperDeleteButton(bot, whisper_id))
        return v

    @classmethod
    def without_decide(cls, bot: Bot, whisper_id: int) -> WhisperDmView:
        """Build a DM view that omits Share/Delete (used after the target has
        already shared the whisper)."""
        v: WhisperDmView = cls.__new__(cls)
        discord.ui.View.__init__(v, timeout=None)
        v.bot = bot
        v.whisper_id = whisper_id
        v.add_item(WhisperGuessButton(bot, whisper_id))
        return v


# ── Guess outcome helper + select view ──────────────────────────────────────

async def _handle_guess_outcome(
    interaction: discord.Interaction,
    bot: Bot,
    whisper: Whisper,
    guessed_id: int,
) -> None:
    try:
        outcome = evaluate_guess(
            whisper, guesser_id=interaction.user.id, guessed_id=guessed_id
        )
    except GuessValidationError as e:
        await interaction.response.edit_message(content=e.message, view=None)
        return

    consumed = await asyncio.to_thread(
        _do_record_guess,
        bot.ctx.db_path,
        whisper_id=whisper.id,
        guessed_id=guessed_id,
        correct=outcome.correct,
    )
    if not consumed:
        await interaction.response.edit_message(
            content="This whisper was solved by another tab.", view=None
        )
        return

    if outcome.correct:
        guild = interaction.guild or bot.get_guild(whisper.guild_id)
        await interaction.response.edit_message(content="You solved it!", view=None)
        cfg = await asyncio.to_thread(_load_config, bot.ctx.db_path, whisper.guild_id)
        if guild:
            feed_channel = guild.get_channel(cfg.channel_id)
            if isinstance(feed_channel, discord.TextChannel):
                try:
                    await feed_channel.send(
                        f"✅ <@{whisper.target_id}> solved the whisper!",
                        view=WhisperExposeView(bot, whisper.id),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    log.warning("Failed to post solved message to feed")
    elif outcome.exhausted:
        if whisper.dm_msg_id:
            try:
                dm_channel = await interaction.user.create_dm()
                dm_msg = await dm_channel.fetch_message(whisper.dm_msg_id)
                await dm_msg.edit(view=WhisperDmView.without_guess(bot, whisper.id))
            except discord.HTTPException:
                log.warning("Failed to remove Guess button from exhausted whisper DM")
        await interaction.response.edit_message(
            content="Wrong! No more guesses. The sender stays anonymous forever.",
            view=None,
        )
    else:
        await interaction.response.edit_message(
            content=f"Wrong! {outcome.attempts_remaining} guesses left.",
            view=None,
        )


_GUESS_PAGE_SIZE = 25


class _WhisperFilterModal(discord.ui.Modal, title="Filter names"):
    query: discord.ui.TextInput = discord.ui.TextInput(  # type: ignore[assignment]
        label="Search",
        placeholder="Type a name…",
        required=True,
        max_length=50,
    )

    def __init__(self, parent: WhisperGuessSelectView) -> None:
        super().__init__()
        self._parent = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        q = self.query.value.strip()
        q_lower = q.lower()

        def _score(m: discord.Member) -> int:
            name = m.display_name.lower()
            if name == q_lower:
                return 4
            if name.startswith(q_lower):
                return 3
            if q_lower in name:
                return 2
            it = iter(name)
            if all(c in it for c in q_lower):
                return 1
            return 0

        scored = sorted(
            ((m, _score(m)) for m in self._parent._all_members),
            key=lambda x: -x[1],
        )
        self._parent._display_members = [m for m, s in scored if s > 0]
        self._parent._filter_query = q
        self._parent._page = 0
        self._parent._rebuild()
        await interaction.response.edit_message(view=self._parent)


class WhisperGuessMemberSelect(discord.ui.Select):
    def __init__(
        self,
        bot: Bot,
        whisper_id: int,
        members: Sequence[discord.Member],
        page: int,
        *,
        placeholder: str = "Pick the sender…",
    ) -> None:
        page_members = members[page * _GUESS_PAGE_SIZE:(page + 1) * _GUESS_PAGE_SIZE]
        options = [
            discord.SelectOption(label=m.display_name[:100], value=str(m.id))
            for m in page_members
        ]
        super().__init__(
            placeholder=placeholder[:150],
            options=options,
            min_values=1,
            max_values=1,
        )
        self.bot = bot
        self.whisper_id = whisper_id

    async def callback(self, interaction: discord.Interaction) -> None:
        guessed_id = int(self.values[0])
        whisper = await asyncio.to_thread(
            _do_load_whisper, self.bot.ctx.db_path, self.whisper_id
        )
        if whisper is None:
            await interaction.response.edit_message(content="Whisper not found.", view=None)
            return
        if whisper.solved:
            await interaction.response.edit_message(
                content=ERROR_GUESS_ALREADY_SOLVED, view=None
            )
            return
        if whisper.guesses_left <= 0:
            await interaction.response.edit_message(
                content=ERROR_GUESS_NO_ATTEMPTS, view=None
            )
            return
        await _handle_guess_outcome(interaction, self.bot, whisper, guessed_id)


class WhisperGuessSelectView(discord.ui.View):
    def __init__(
        self,
        bot: Bot,
        whisper_id: int,
        members: Sequence[discord.Member],
        page: int = 0,
    ) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        self.whisper_id = whisper_id
        self._all_members = list(members)
        self._display_members = self._all_members
        self._filter_query = ""
        self._page = page
        self._rebuild()

    def _page_count(self) -> int:
        return max(1, (len(self._display_members) + _GUESS_PAGE_SIZE - 1) // _GUESS_PAGE_SIZE)

    def _rebuild(self) -> None:
        self.clear_items()
        page_count = self._page_count()

        if self._filter_query:
            n = len(self._display_members)
            placeholder = f'🔍 "{self._filter_query}" — {n} match{"es" if n != 1 else ""}'
            if page_count > 1:
                placeholder += f" ({self._page + 1}/{page_count})"
        elif page_count > 1:
            placeholder = f"Pick the sender… ({self._page + 1}/{page_count})"
        else:
            placeholder = "Pick the sender…"

        if self._display_members:
            self.add_item(WhisperGuessMemberSelect(
                self.bot, self.whisper_id, self._display_members, self._page,
                placeholder=placeholder,
            ))
        else:
            empty: discord.ui.Select = discord.ui.Select(  # type: ignore[type-arg]
                placeholder="No members match that search.",
                options=[discord.SelectOption(label="No results", value="__none__")],
                disabled=True,
                row=0,
            )
            self.add_item(empty)

        if page_count > 1:
            prev_btn = discord.ui.Button(
                label="◀",
                style=discord.ButtonStyle.secondary,
                disabled=(self._page == 0),
                row=1,
            )
            prev_btn.callback = self._on_prev
            self.add_item(prev_btn)

            next_btn = discord.ui.Button(
                label="▶",
                style=discord.ButtonStyle.secondary,
                disabled=(self._page >= page_count - 1),
                row=1,
            )
            next_btn.callback = self._on_next
            self.add_item(next_btn)

        filter_btn = discord.ui.Button(
            label="🔍 Filter",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        filter_btn.callback = self._on_filter
        self.add_item(filter_btn)

        if self._filter_query:
            clear_btn = discord.ui.Button(
                label="✕ Clear",
                style=discord.ButtonStyle.danger,
                row=1,
            )
            clear_btn.callback = self._on_clear_filter
            self.add_item(clear_btn)

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        self._page = max(0, self._page - 1)
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        self._page = min(self._page_count() - 1, self._page + 1)
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def _on_filter(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(_WhisperFilterModal(self))

    async def _on_clear_filter(self, interaction: discord.Interaction) -> None:
        self._display_members = self._all_members
        self._filter_query = ""
        self._page = 0
        self._rebuild()
        await interaction.response.edit_message(view=self)


# ── Shared share side-effects (feed channel update) ──────────────────────────


async def _share_side_effects(bot: Bot, whisper: Whisper) -> None:
    """Delete the original 'someone sent X a whisper' announcement and post the
    full message to the feed channel. Best-effort — logs but doesn't raise."""
    guild = bot.get_guild(whisper.guild_id)
    if guild is None:
        return
    cfg = await asyncio.to_thread(_load_config, bot.ctx.db_path, whisper.guild_id)
    feed_channel = guild.get_channel(cfg.channel_id)
    if not isinstance(feed_channel, discord.TextChannel):
        return
    if whisper.channel_msg_id:
        try:
            old = await feed_channel.fetch_message(whisper.channel_msg_id)
            await old.delete()
        except discord.HTTPException:
            log.warning("Failed to delete original announcement on share")
    try:
        new_msg = await feed_channel.send(
            f"\U0001f4ec A fresh Whisper was shared. Someone sent "
            f"<@{whisper.target_id}> an anonymous message!\n"
            f"```{safe_codefence_content(whisper.message)}```",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await asyncio.to_thread(
            _do_set_message_ids,
            bot.ctx.db_path,
            whisper.id,
            channel_msg_id=new_msg.id,
            dm_msg_id=whisper.dm_msg_id or 0,
        )
    except discord.HTTPException:
        log.warning("Failed to post share announcement to feed")


# ── Inbox dropdown view (received + sent) ────────────────────────────────────


def _status_pill(w: Whisper) -> str:
    if w.exposed:
        return "Exposed"
    if w.solved:
        return "Solved"
    if is_locked(w):
        return "Locked"
    if w.guesses_left == 0:
        return "No guesses"
    if w.state == STATE_SHARED:
        return "Shared"
    return "New"


def _preview(text: str, n: int = 60) -> str:
    preview = text.replace("\n", " ").strip()
    if len(preview) > n:
        preview = preview[: n - 1] + "…"
    return preview


class _WhisperInboxFilterModal(discord.ui.Modal, title="Filter whispers"):
    query: discord.ui.TextInput = discord.ui.TextInput(  # type: ignore[assignment]
        label="Search by content",
        placeholder="Type a few words from the message…",
        required=True,
        max_length=100,
    )

    def __init__(self, parent: WhisperInboxSelectView) -> None:
        super().__init__()
        self._parent = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        q = str(self.query.value).strip()
        q_lower = q.lower()
        matches = [w for w in self._parent._all if q_lower in w.message.lower()]
        self._parent._filter_query = q
        self._parent._display = matches
        self._parent._page = 0
        if matches and not any(w.id == self._parent._selected_id for w in matches):
            self._parent._selected_id = matches[0].id
        elif not matches:
            self._parent._selected_id = None
        self._parent._rebuild()
        await interaction.response.edit_message(
            embed=self._parent.embed(), view=self._parent
        )


class WhisperInboxSelectView(discord.ui.View):
    """Dropdown-driven whisper inbox. mode='received' for the recipient's inbox,
    'sent' for the sender's own outgoing list."""

    def __init__(
        self,
        bot: Bot,
        whispers: list[Whisper],
        *,
        invoker_id: int,
        mode: str = "received",
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self._invoker_id = invoker_id
        self._mode = mode
        self._all: list[Whisper] = list(whispers)
        self._display: list[Whisper] = list(whispers)
        self._filter_query = ""
        self._page = 0
        self._selected_id: int | None = whispers[0].id if whispers else None
        self._rebuild()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._invoker_id:
            await interaction.response.send_message(
                "This inbox isn't yours.", ephemeral=True
            )
            return False
        return True

    # ── helpers ────────────────────────────────────────────────────────────

    def _selected(self) -> Whisper | None:
        if self._selected_id is None:
            return None
        return next((w for w in self._all if w.id == self._selected_id), None)

    def _page_count(self) -> int:
        return max(1, (len(self._display) + _INBOX_PAGE_SIZE - 1) // _INBOX_PAGE_SIZE)

    def _page_slice(self) -> list[Whisper]:
        start = self._page * _INBOX_PAGE_SIZE
        return self._display[start : start + _INBOX_PAGE_SIZE]

    def _title(self) -> str:
        return "Your Inbox" if self._mode == "received" else "Whispers You've Sent"

    def embed(self) -> discord.Embed:
        emb = discord.Embed(
            title=f"{self._title()} ({len(self._all)})",
            color=discord.Color.blurple(),
        )
        selected = self._selected()
        if not self._all:
            emb.description = (
                "*No whispers in your inbox.*"
                if self._mode == "received"
                else "*You haven't sent any active whispers in this server.*"
            )
            return emb
        if selected is None:
            emb.description = "*Pick a whisper from the dropdown.*"
            return emb

        status = _status_pill(selected)
        time_ago = _format_time_ago(selected.created_at)
        if self._mode == "received":
            header = f"**Whisper #{selected.id}** · {status} · *{time_ago}*"
        else:
            header = (
                f"**Whisper #{selected.id} → <@{selected.target_id}>** · "
                f"{status} · *{time_ago}*"
            )
        emb.description = (
            f"{header}\n```{safe_codefence_content(selected.message)}```"
        )

        if is_locked(selected):
            emb.set_footer(text="Locked — too old to guess on now.")
        elif selected.solved:
            emb.set_footer(text="Solved.")
        elif selected.guesses_left == 0:
            emb.set_footer(text="Out of guesses — the sender stays anonymous.")
        elif self._mode == "received":
            emb.set_footer(text=f"{selected.guesses_left} guesses left.")
        else:
            emb.set_footer(text=f"{selected.guesses_left} guesses remain for the target.")
        return emb

    # ── view building ──────────────────────────────────────────────────────

    def _rebuild(self) -> None:
        self.clear_items()
        if not self._all:
            return

        # Selection fallback if current selection is no longer visible
        if self._selected_id is not None and not any(
            w.id == self._selected_id for w in self._display
        ):
            self._selected_id = self._display[0].id if self._display else None
            self._page = 0

        # Row 0: whisper select
        page = self._page_slice()
        if not page:
            empty: discord.ui.Select = discord.ui.Select(  # type: ignore[type-arg]
                placeholder=f'🔍 No matches for "{self._filter_query}"',
                options=[
                    discord.SelectOption(label="No results", value="__none__")
                ],
                disabled=True,
                row=0,
            )
            self.add_item(empty)
        else:
            options = [
                discord.SelectOption(
                    label=(
                        f"#{w.id} · {_status_pill(w)} · "
                        f"{_format_time_ago(w.created_at)}"
                    )[:100],
                    value=str(w.id),
                    description=_preview(w.message)[:100] or None,
                    default=(w.id == self._selected_id),
                )
                for w in page
            ]
            placeholder = (
                f'🔍 "{self._filter_query}" — {len(self._display)} match'
                f'{"es" if len(self._display) != 1 else ""}'
                if self._filter_query
                else f"Pick a whisper… ({len(self._display)} total)"
            )
            page_count = self._page_count()
            if page_count > 1:
                placeholder += f" ({self._page + 1}/{page_count})"
            sel: discord.ui.Select = discord.ui.Select(  # type: ignore[type-arg]
                placeholder=placeholder[:150],
                options=options,
                row=0,
            )
            sel.callback = self._on_select
            self.add_item(sel)

        # Row 1: nav buttons (pagination + filter)
        page_count = self._page_count()
        if page_count > 1:
            prev_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
                label="◀",
                style=discord.ButtonStyle.secondary,
                disabled=(self._page == 0),
                row=1,
            )
            prev_btn.callback = self._on_prev
            self.add_item(prev_btn)
            next_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
                label="▶",
                style=discord.ButtonStyle.secondary,
                disabled=(self._page >= page_count - 1),
                row=1,
            )
            next_btn.callback = self._on_next
            self.add_item(next_btn)

        filter_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="🔍 Filter", style=discord.ButtonStyle.secondary, row=1
        )
        filter_btn.callback = self._on_filter
        self.add_item(filter_btn)

        if self._filter_query:
            clear_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
                label="✕ Clear", style=discord.ButtonStyle.danger, row=1
            )
            clear_btn.callback = self._on_clear_filter
            self.add_item(clear_btn)

        # Row 2: contextual action buttons for selected whisper
        selected = self._selected()
        if selected is None:
            return
        row = 2
        if self._mode == "received":
            if (
                not selected.solved
                and selected.guesses_left > 0
                and not is_locked(selected)
            ):
                guess_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
                    label="Guess",
                    style=discord.ButtonStyle.primary,
                    row=row,
                )
                guess_btn.callback = self._on_guess
                self.add_item(guess_btn)
            if selected.state == STATE_PENDING:
                share_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
                    label="Share",
                    style=discord.ButtonStyle.success,
                    row=row,
                )
                share_btn.callback = self._on_share
                self.add_item(share_btn)
            reply_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
                label="Reply",
                style=discord.ButtonStyle.success,
                row=row,
            )
            reply_btn.callback = self._on_reply
            self.add_item(reply_btn)
            report_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
                label="Report",
                style=discord.ButtonStyle.danger,
                row=row,
            )
            report_btn.callback = self._on_report
            self.add_item(report_btn)
        delete_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="Delete",
            style=discord.ButtonStyle.secondary,
            row=row,
        )
        delete_btn.callback = self._on_delete
        self.add_item(delete_btn)

    # ── select / nav callbacks ─────────────────────────────────────────────

    async def _on_select(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if values and values[0] != "__none__":
            self._selected_id = int(values[0])
        self._rebuild()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        self._page = max(0, self._page - 1)
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        self._page = min(self._page_count() - 1, self._page + 1)
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def _on_filter(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(_WhisperInboxFilterModal(self))

    async def _on_clear_filter(self, interaction: discord.Interaction) -> None:
        self._filter_query = ""
        self._display = list(self._all)
        self._page = 0
        if self._all and not any(w.id == self._selected_id for w in self._all):
            self._selected_id = self._all[0].id
        self._rebuild()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    # ── action callbacks ───────────────────────────────────────────────────

    async def _on_share(self, interaction: discord.Interaction) -> None:
        selected = self._selected()
        if selected is None:
            return
        try:
            validate_share(selected, invoker_id=interaction.user.id)
        except TransitionValidationError as e:
            await interaction.response.send_message(e.message, ephemeral=True)
            return
        await asyncio.to_thread(
            _do_update_state, self.bot.ctx.db_path, selected.id, STATE_SHARED
        )
        await _share_side_effects(self.bot, selected)
        selected.state = STATE_SHARED
        self._rebuild()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _on_delete(self, interaction: discord.Interaction) -> None:
        selected = self._selected()
        if selected is None:
            return
        try:
            validate_delete(selected, invoker_id=interaction.user.id)
        except TransitionValidationError as e:
            await interaction.response.send_message(e.message, ephemeral=True)
            return
        await asyncio.to_thread(
            _do_soft_delete, self.bot.ctx.db_path, selected.id
        )
        self._all = [w for w in self._all if w.id != selected.id]
        self._display = [w for w in self._display if w.id != selected.id]
        if self._display:
            if self._page * _INBOX_PAGE_SIZE >= len(self._display):
                self._page = max(0, self._page - 1)
            page = self._page_slice()
            self._selected_id = page[0].id if page else None
        else:
            self._selected_id = None
        self._rebuild()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _on_guess(self, interaction: discord.Interaction) -> None:
        selected = self._selected()
        if selected is None:
            return
        # Delegate to WhisperGuessButton's existing logic — it sends a new
        # ephemeral with the member-picker view, leaving the inbox intact.
        btn = WhisperGuessButton(self.bot, selected.id)
        await btn.callback(interaction)

    async def _on_reply(self, interaction: discord.Interaction) -> None:
        selected = self._selected()
        if selected is None:
            return
        reply_count = await asyncio.to_thread(
            _do_count_replies, self.bot.ctx.db_path, selected.id
        )
        try:
            validate_reply(
                selected,
                invoker_id=interaction.user.id,
                reply_count=reply_count,
            )
        except TransitionValidationError as e:
            await interaction.response.send_message(e.message, ephemeral=True)
            return
        await interaction.response.send_modal(
            WhisperReplyModal(self.bot, selected.id)
        )

    async def _on_report(self, interaction: discord.Interaction) -> None:
        selected = self._selected()
        if selected is None:
            return
        if interaction.user.id != selected.target_id:
            await interaction.response.send_message(
                "Only the recipient can report a whisper.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            WhisperReportModal(self.bot, selected.id)
        )


# ── Send picker (button-driven flow) ─────────────────────────────────────────


_SEND_PICKER_PAGE_SIZE = 25


class WhisperSendComposeModal(discord.ui.Modal, title="Send anonymous whisper"):
    message_input: discord.ui.TextInput = discord.ui.TextInput(  # type: ignore[assignment]
        label="Your message",
        style=discord.TextStyle.long,
        required=True,
        max_length=MAX_MESSAGE_LENGTH,
        placeholder="They get 3 guesses to figure out it was you.",
    )

    def __init__(self, cog: WhisperCog, target_id: int) -> None:
        super().__init__()
        self._cog = cog
        self._target_id = target_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Whisper can only be used in a server.", ephemeral=True
            )
            return
        member = interaction.guild.get_member(self._target_id)
        if member is None:
            await interaction.response.send_message(
                "That member isn't in this server any more.", ephemeral=True
            )
            return
        content = str(self.message_input.value)
        await self._cog._send_impl(interaction, target=member, message=content)


class _WhisperSendTargetSelect(discord.ui.Select):  # type: ignore[type-arg]
    def __init__(
        self,
        cog: WhisperCog,
        members: Sequence[discord.Member],
        page: int,
        *,
        placeholder: str,
    ) -> None:
        page_members = members[
            page * _SEND_PICKER_PAGE_SIZE : (page + 1) * _SEND_PICKER_PAGE_SIZE
        ]
        options = [
            discord.SelectOption(
                label=m.display_name[:100],
                value=str(m.id),
                description=(f"@{m.name}" if m.name != m.display_name else None),
            )
            for m in page_members
        ]
        super().__init__(
            placeholder=placeholder[:150],
            options=options,
            min_values=1,
            max_values=1,
        )
        self._cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        target_id = int(self.values[0])
        await interaction.response.send_modal(
            WhisperSendComposeModal(self._cog, target_id)
        )


class _WhisperSendFilterModal(discord.ui.Modal, title="Filter members"):
    query: discord.ui.TextInput = discord.ui.TextInput(  # type: ignore[assignment]
        label="Search",
        placeholder="Type a name…",
        required=True,
        max_length=50,
    )

    def __init__(self, parent: WhisperSendTargetSelectView) -> None:
        super().__init__()
        self._parent = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        q = str(self.query.value).strip()
        q_lower = q.lower()

        def _score(m: discord.Member) -> int:
            name = m.display_name.lower()
            if name == q_lower:
                return 4
            if name.startswith(q_lower):
                return 3
            if q_lower in name:
                return 2
            it = iter(name)
            if all(c in it for c in q_lower):
                return 1
            return 0

        scored = sorted(
            ((m, _score(m)) for m in self._parent._all_members),
            key=lambda x: -x[1],
        )
        self._parent._display_members = [m for m, s in scored if s > 0]
        self._parent._filter_query = q
        self._parent._page = 0
        self._parent._rebuild()
        await interaction.response.edit_message(view=self._parent)


class WhisperSendTargetSelectView(discord.ui.View):
    """Paginated/filterable picker of opt-in members for the button-driven
    send flow. Selecting a member opens WhisperSendComposeModal."""

    def __init__(
        self,
        cog: WhisperCog,
        members: Sequence[discord.Member],
        *,
        invoker_id: int,
        page: int = 0,
    ) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self._invoker_id = invoker_id
        self._all_members = list(members)
        self._display_members = self._all_members
        self._filter_query = ""
        self._page = page
        self._rebuild()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._invoker_id:
            await interaction.response.send_message(
                "This picker isn't for you.", ephemeral=True
            )
            return False
        return True

    def _page_count(self) -> int:
        return max(
            1,
            (len(self._display_members) + _SEND_PICKER_PAGE_SIZE - 1)
            // _SEND_PICKER_PAGE_SIZE,
        )

    def _rebuild(self) -> None:
        self.clear_items()
        page_count = self._page_count()

        if self._filter_query:
            n = len(self._display_members)
            placeholder = (
                f'🔍 "{self._filter_query}" — {n} match'
                f'{"es" if n != 1 else ""}'
            )
            if page_count > 1:
                placeholder += f" ({self._page + 1}/{page_count})"
        elif page_count > 1:
            placeholder = f"Pick recipient… ({self._page + 1}/{page_count})"
        else:
            placeholder = "Pick recipient…"

        if self._display_members:
            self.add_item(
                _WhisperSendTargetSelect(
                    self.cog, self._display_members, self._page,
                    placeholder=placeholder,
                )
            )
        else:
            empty: discord.ui.Select = discord.ui.Select(  # type: ignore[type-arg]
                placeholder="No members match.",
                options=[
                    discord.SelectOption(label="No results", value="__none__")
                ],
                disabled=True,
                row=0,
            )
            self.add_item(empty)

        if page_count > 1:
            prev_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
                label="◀",
                style=discord.ButtonStyle.secondary,
                disabled=(self._page == 0),
                row=1,
            )
            prev_btn.callback = self._on_prev
            self.add_item(prev_btn)
            next_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
                label="▶",
                style=discord.ButtonStyle.secondary,
                disabled=(self._page >= page_count - 1),
                row=1,
            )
            next_btn.callback = self._on_next
            self.add_item(next_btn)

        filter_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="🔍 Filter", style=discord.ButtonStyle.secondary, row=1
        )
        filter_btn.callback = self._on_filter
        self.add_item(filter_btn)

        if self._filter_query:
            clear_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
                label="✕ Clear", style=discord.ButtonStyle.danger, row=1
            )
            clear_btn.callback = self._on_clear_filter
            self.add_item(clear_btn)

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        self._page = max(0, self._page - 1)
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        self._page = min(self._page_count() - 1, self._page + 1)
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def _on_filter(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(_WhisperSendFilterModal(self))

    async def _on_clear_filter(self, interaction: discord.Interaction) -> None:
        self._display_members = self._all_members
        self._filter_query = ""
        self._page = 0
        self._rebuild()
        await interaction.response.edit_message(view=self)


# ── Persistent feed-channel view (Send / My Inbox / My Sent) ─────────────────


class WhisperFeedView(discord.ui.View):
    def __init__(self, bot: Bot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

        send_btn = discord.ui.Button(
            label="Send Whisper",
            style=discord.ButtonStyle.primary,
            custom_id="whisper:send",
        )
        send_btn.callback = self._on_send_click
        self.add_item(send_btn)

        check_btn = discord.ui.Button(
            label="My Inbox",
            style=discord.ButtonStyle.secondary,
            custom_id="whisper:check",
        )
        check_btn.callback = self._on_check_click
        self.add_item(check_btn)

        sent_btn = discord.ui.Button(
            label="My Sent",
            style=discord.ButtonStyle.secondary,
            custom_id="whisper:check_sent",
        )
        sent_btn.callback = self._on_check_sent_click
        self.add_item(sent_btn)

    async def _on_send_click(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Whisper can only be used in a server.", ephemeral=True
            )
            return
        cfg = await asyncio.to_thread(
            _load_config, self.bot.ctx.db_path, interaction.guild.id
        )
        if cfg.role_id == 0:
            await interaction.response.send_message(
                "Whispers aren't configured in this server yet.", ephemeral=True
            )
            return
        role = interaction.guild.get_role(cfg.role_id)
        if role is None:
            await interaction.response.send_message(
                "Whisper role no longer exists. Ask an admin to fix the config.",
                ephemeral=True,
            )
            return
        if cfg.role_id not in {r.id for r in getattr(interaction.user, "roles", [])}:
            await interaction.response.send_message(
                "You need the Whisper role first. Use `/whisper optin` to join.",
                ephemeral=True,
            )
            return
        members = sorted(
            [m for m in role.members if m.id != interaction.user.id],
            key=lambda m: m.display_name.lower(),
        )
        if not members:
            await interaction.response.send_message(
                "No other opted-in members to whisper to yet.", ephemeral=True
            )
            return
        cog = self.bot.get_cog("WhisperCog")
        if not isinstance(cog, WhisperCog):
            await interaction.response.send_message(
                "Whisper cog isn't loaded. Tell an admin.", ephemeral=True
            )
            return
        view = WhisperSendTargetSelectView(
            cog, members, invoker_id=interaction.user.id
        )
        await interaction.response.send_message(
            "Pick someone to whisper. "
            "-# Your identity is logged for moderation.",
            view=view, ephemeral=True,
        )

    async def _on_check_click(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        whispers = await asyncio.to_thread(
            _do_list_received_in_states,
            self.bot.ctx.db_path,
            guild_id=interaction.guild.id,
            target_id=interaction.user.id,
            states=[STATE_PENDING, STATE_SHARED],
        )
        view = WhisperInboxSelectView(
            self.bot, whispers, invoker_id=interaction.user.id, mode="received"
        )
        await interaction.response.send_message(
            embed=view.embed(), view=view, ephemeral=True
        )

    async def _on_check_sent_click(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        whispers = await asyncio.to_thread(
            _do_list_sent,
            self.bot.ctx.db_path,
            guild_id=interaction.guild.id,
            sender_id=interaction.user.id,
        )
        active = [w for w in whispers if not is_terminal_for_sender(w)]
        view = WhisperInboxSelectView(
            self.bot, active, invoker_id=interaction.user.id, mode="sent"
        )
        await interaction.response.send_message(
            embed=view.embed(), view=view, ephemeral=True
        )


# ── Forget-me ────────────────────────────────────────────────────────────────


def _do_forget_user(db_path: Path, *, guild_id: int, user_id: int) -> None:
    """Delete all whisper data for user_id in guild_id (both sent and received)."""
    with open_db(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        # Whispers user sent — cascade deletes replies/guesses/reports
        conn.execute(
            "DELETE FROM whispers WHERE guild_id = ? AND sender_id = ?",
            (guild_id, user_id),
        )
        # Whispers user received
        conn.execute(
            "DELETE FROM whispers WHERE guild_id = ? AND target_id = ?",
            (guild_id, user_id),
        )
        # Orphaned replies they sent/received (parent whisper may already be gone)
        conn.execute(
            "DELETE FROM whisper_replies WHERE from_user_id = ?",
            (user_id,),
        )
        conn.execute(
            "DELETE FROM whisper_replies WHERE to_user_id = ?",
            (user_id,),
        )


class WhisperForgetMeConfirmView(discord.ui.View):
    """Ephemeral confirmation view for /whisper forget-me."""

    def __init__(self, bot: Bot, guild_id: int, user_id: int) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        self.guild_id = guild_id
        self.user_id = user_id

    @discord.ui.button(label="Yes, delete my data", style=discord.ButtonStyle.danger)
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,  # noqa: ARG002
    ) -> None:
        await asyncio.to_thread(
            _do_forget_user,
            self.bot.ctx.db_path,
            guild_id=self.guild_id,
            user_id=self.user_id,
        )
        await interaction.response.edit_message(
            content="Your whisper data for this server has been deleted.", view=None
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,  # noqa: ARG002
    ) -> None:
        await interaction.response.edit_message(
            content="Deletion cancelled.", view=None
        )


# ── Opt-in confirmation view ─────────────────────────────────────────────────


class WhisperOptinConfirmView(discord.ui.View):
    """Ephemeral consent view shown by /whisper optin. The role is only
    granted once the user explicitly clicks Confirm."""

    def __init__(self, bot: Bot, role: discord.Role) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        self.role = role

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        try:
            await interaction.user.add_roles(self.role, reason="Whisper opt-in")  # type: ignore[union-attr]
        except discord.Forbidden:
            await interaction.response.edit_message(
                content="I don't have permission to assign that role.", view=None
            )
            return
        await interaction.response.edit_message(
            content="You've opted in. You can now send and receive whispers.",
            view=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content="Opt-in cancelled.", view=None
        )


# ── Cog ──────────────────────────────────────────────────────────────────────

SEND_COOLDOWN_SECONDS = 30
SEND_PER_TARGET_HOURLY_CAP = 5


class WhisperCog(commands.Cog):
    whisper_group = app_commands.Group(name="whisper", description="Send anonymous whispers.")

    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self.ctx = bot.ctx
        self._launcher_locks: dict[int, asyncio.Lock] = {}
        self._pending_refresh: set[int] = set()
        self._last_send_at: dict[int, float] = {}  # sender_id -> ts
        self._target_sends: dict[tuple[int, int, int], list[float]] = {}  # (guild_id, sender_id, target_id) -> [ts...]

    def _get_launcher_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._launcher_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._launcher_locks[guild_id] = lock
        return lock

    async def cog_load(self) -> None:
        # Register persistent views so static-id buttons survive restart.
        self.bot.add_view(WhisperFeedView(self.bot))
        # Register dynamic-id buttons so per-whisper Guess/Share/Delete/Expose
        # button clicks on existing DMs and feed messages still route after
        # a bot restart (custom_ids embed the whisper_id).
        self.bot.add_dynamic_items(
            WhisperGuessButton,
            WhisperShareButton,
            WhisperDeleteButton,
            WhisperExposeButton,
            WhisperReplyButton,
            WhisperReportButton,
            WhisperReportReplyButton,
        )
        # Bootstrap launcher in every configured guild so the button bar is
        # at the bottom of the channel from boot. Run in parallel with a
        # semaphore to avoid a thundering-herd against Discord on large bots.
        sem = asyncio.Semaphore(5)

        async def _bootstrap_one(guild: discord.Guild) -> None:
            async with sem:
                try:
                    await self.refresh_whisper_launcher(guild.id)
                except Exception:
                    log.exception(
                        "Failed to bootstrap whisper launcher for guild %s", guild.id
                    )

        await asyncio.gather(*[_bootstrap_one(g) for g in self.bot.guilds])

    async def refresh_whisper_launcher(self, guild_id: int) -> None:
        """Delete the previous launcher (if any) and post a fresh one at the
        bottom of the configured whisper channel. Serialized per-guild.

        Multiple concurrent calls for the same guild are coalesced: only one
        actual delete+post cycle runs at a time, and a second cycle fires only
        if at least one more call arrived while the first held the lock.
        """
        self._pending_refresh.add(guild_id)
        async with self._get_launcher_lock(guild_id):
            if guild_id not in self._pending_refresh:
                return  # another invocation already did the work for us
            self._pending_refresh.discard(guild_id)

            cfg = await asyncio.to_thread(
                _load_config, self.ctx.db_path, guild_id
            )
            if cfg.channel_id == 0:
                return
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return
            channel = guild.get_channel(cfg.channel_id)
            if not isinstance(channel, discord.TextChannel):
                return
            if cfg.launcher_message_id:
                try:
                    old = await channel.fetch_message(cfg.launcher_message_id)
                    await old.delete()
                except discord.HTTPException:
                    pass
            try:
                sent = await channel.send(
                    "**Whisper** — anonymous messages with a guessing game.",
                    view=WhisperFeedView(self.bot),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                log.warning(
                    "Failed to post whisper launcher to channel %s", cfg.channel_id
                )
                return
            await asyncio.to_thread(
                _do_set_launcher_id, self.ctx.db_path, guild_id, sent.id
            )

    @commands.Cog.listener("on_guild_remove")
    async def _on_guild_remove(self, guild: discord.Guild) -> None:
        await asyncio.to_thread(self._clear_guild_config, guild.id)

    def _clear_guild_config(self, guild_id: int) -> None:
        from bot_modules.core.db_utils import delete_config_value  # noqa: PLC0415
        with open_db(self.ctx.db_path) as conn:
            for key in (
                "whisper_role_id",
                "whisper_channel_id",
                "whisper_log_channel_id",
                "whisper_launcher_message_id",
            ):
                delete_config_value(conn, key, guild_id)

    @commands.Cog.listener("on_message")
    async def _on_message_launcher_bump(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not message.guild:
            return
        cfg = await asyncio.to_thread(
            _load_config, self.ctx.db_path, message.guild.id
        )
        if cfg.channel_id == 0 or message.channel.id != cfg.channel_id:
            return
        # Skip the launcher message itself to avoid an infinite loop.
        if cfg.launcher_message_id and message.id == cfg.launcher_message_id:
            return
        await self.refresh_whisper_launcher(message.guild.id)

    async def _optin_impl(self, interaction: discord.Interaction) -> None:
        """Pure shared implementation, easy to test directly."""
        assert interaction.guild is not None
        cfg = await asyncio.to_thread(_load_config, self.ctx.db_path, interaction.guild.id)
        if cfg.role_id == 0:
            await interaction.response.send_message(
                "Whisper role hasn't been configured yet.", ephemeral=True
            )
            return
        role = interaction.guild.get_role(cfg.role_id)
        if role is None:
            await interaction.response.send_message(
                "Whisper role no longer exists. Ask an admin to fix the config.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "By opting in, you'll be able to send whispers and receive them "
            "from other opted-in members. You can opt out anytime.",
            view=WhisperOptinConfirmView(self.bot, role),
            ephemeral=True,
        )

    async def _optout_impl(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        cfg = await asyncio.to_thread(_load_config, self.ctx.db_path, interaction.guild.id)
        if cfg.role_id == 0:
            await interaction.response.send_message(
                "Whisper role hasn't been configured yet.", ephemeral=True
            )
            return
        role = interaction.guild.get_role(cfg.role_id)
        if role is not None:
            try:
                await interaction.user.remove_roles(role, reason="Whisper opt-out")  # type: ignore[union-attr]
            except discord.Forbidden:
                await interaction.response.send_message(
                    "I don't have permission to remove that role.", ephemeral=True
                )
                return
        await interaction.response.send_message(
            "You've opted out. Existing whispers are preserved.", ephemeral=True
        )

    @whisper_group.command(
        name="forget-me",
        description="Delete all your whisper data from this server.",
    )
    async def whisper_forget_me(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "This will permanently delete all whispers you sent or received in this server. "
            "This cannot be undone.",
            view=WhisperForgetMeConfirmView(self.bot, interaction.guild.id, interaction.user.id),
            ephemeral=True,
        )

    @whisper_group.command(name="optin", description="Opt in to send and receive whispers.")
    async def whisper_optin(self, interaction: discord.Interaction) -> None:
        await self._optin_impl(interaction)

    @whisper_group.command(name="optout", description="Opt out of whispers.")
    async def whisper_optout(self, interaction: discord.Interaction) -> None:
        await self._optout_impl(interaction)

    @whisper_group.command(
        name="sent",
        description="See the whispers you've sent in this server (active only).",
    )
    async def whisper_sent(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        whispers = await asyncio.to_thread(
            _do_list_sent,
            self.ctx.db_path,
            guild_id=interaction.guild.id,
            sender_id=interaction.user.id,
        )
        active = [w for w in whispers if not is_terminal_for_sender(w)]
        view = WhisperInboxSelectView(
            self.bot, active, invoker_id=interaction.user.id, mode="sent"
        )
        await interaction.response.send_message(
            embed=view.embed(), view=view, ephemeral=True
        )

    async def _send_impl(
        self,
        interaction: discord.Interaction,
        *,
        target: discord.Member,
        message: str,
    ) -> None:
        assert interaction.guild is not None
        cfg = await asyncio.to_thread(_load_config, self.ctx.db_path, interaction.guild.id)

        sender_role_ids = {r.id for r in getattr(interaction.user, "roles", [])}
        target_role_ids = {r.id for r in getattr(target, "roles", [])}
        try:
            validate_send(
                cfg=cfg,
                sender_role_ids=sender_role_ids,
                target_role_ids=target_role_ids,
                sender_id=interaction.user.id,
                target_id=target.id,
                message=message,
            )
        except SendValidationError as e:
            await interaction.response.send_message(e.message, ephemeral=True)
            return

        import time as _t  # noqa: PLC0415
        now = _t.time()
        last = self._last_send_at.get(interaction.user.id, 0)
        if now - last < SEND_COOLDOWN_SECONDS:
            remaining = int(SEND_COOLDOWN_SECONDS - (now - last))
            await interaction.response.send_message(
                f"Slow down — wait {remaining}s before sending another whisper.",
                ephemeral=True,
            )
            return

        rate_key = (interaction.guild.id, interaction.user.id, target.id)
        recent = [t for t in self._target_sends.get(rate_key, []) if now - t < 3600]
        if len(recent) >= SEND_PER_TARGET_HOURLY_CAP:
            await interaction.response.send_message(
                f"You've sent {SEND_PER_TARGET_HOURLY_CAP} whispers to that user in the last hour. Try again later.",
                ephemeral=True,
            )
            return
        self._last_send_at[interaction.user.id] = now
        self._target_sends[rate_key] = recent + [now]

        if getattr(target, "is_timed_out", lambda: False)():
            await interaction.response.send_message(
                "Can't whisper a member who's currently timed out.", ephemeral=True
            )
            return

        feed_channel = interaction.guild.get_channel(cfg.channel_id)
        if not isinstance(feed_channel, discord.TextChannel):
            await interaction.response.send_message(
                "Whisper feed channel is missing or invalid. Tell an admin to fix the config.",
                ephemeral=True,
            )
            return
        whisper_id = await asyncio.to_thread(
            _do_insert_whisper,
            self.ctx.db_path,
            guild_id=interaction.guild.id,
            sender_id=interaction.user.id,
            target_id=target.id,
            message=message.strip(),
        )

        try:
            dm_msg = await target.send(
                f"\U0001f4ec You got a Whisper from someone in **{interaction.guild.name}**.\n"
                f"You have **3 guesses** to figure out who sent it — wrong guesses are gone forever.\n"
                f"```{safe_codefence_content(message.strip())}```",
                view=WhisperDmView(self.bot, whisper_id),
            )
        except (discord.Forbidden, discord.HTTPException):
            # rollback inserted row if DM fails
            await asyncio.to_thread(_do_delete_whisper, self.ctx.db_path, whisper_id)
            await interaction.response.send_message(ERROR_BOT_DM_FAILED, ephemeral=True)
            return

        feed_msg = None
        try:
            feed_msg = await feed_channel.send(
                f"\U0001f4ec Someone sent {target.mention} an anonymous message.",
                allowed_mentions=discord.AllowedMentions(users=[target]),
            )
        except discord.HTTPException:
            log.warning("Failed to post whisper announcement to feed channel")

        await asyncio.to_thread(
            _do_set_message_ids,
            self.ctx.db_path,
            whisper_id,
            channel_msg_id=feed_msg.id if feed_msg else 0,
            dm_msg_id=dm_msg.id,
        )

        await interaction.response.send_message(
            "Whisper delivered.\n-# Your identity is logged by admins for moderation.",
            ephemeral=True,
        )

    async def _target_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete callback restricting /whisper send target to opted-in
        members (those holding the configured whisper role)."""
        if interaction.guild is None:
            return []
        cfg = await asyncio.to_thread(
            _load_config, self.ctx.db_path, interaction.guild.id
        )
        if cfg.role_id == 0:
            return []
        role = interaction.guild.get_role(cfg.role_id)
        if role is None:
            return []
        needle = current.lower()
        matches: list[app_commands.Choice[str]] = []
        for m in role.members:
            if m.id == interaction.user.id:
                continue
            display = (getattr(m, "display_name", None) or m.name).lower()
            handle = m.name.lower()
            if needle and needle not in display and needle not in handle:
                continue
            label = getattr(m, "display_name", None) or m.name
            matches.append(app_commands.Choice(name=label, value=str(m.id)))
            if len(matches) >= 25:
                break
        return matches

    @whisper_group.command(
        name="send",
        description="Send an anonymous whisper to another opted-in member.",
    )
    @app_commands.describe(
        target="Recipient (must be opted in)",
        message="Your anonymous message",
    )
    @app_commands.autocomplete(target=_target_autocomplete)
    async def whisper_send(
        self,
        interaction: discord.Interaction,
        target: str,
        message: str,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Whisper can only be used in a server.", ephemeral=True
            )
            return
        if not target.isdigit():
            await interaction.response.send_message(
                "Pick a recipient from the autocomplete suggestions.",
                ephemeral=True,
            )
            return
        member = interaction.guild.get_member(int(target))
        if member is None:
            await interaction.response.send_message(
                "That member isn't in this server.", ephemeral=True
            )
            return
        await self._send_impl(interaction, target=member, message=message)


async def setup(bot: Bot) -> None:
    await bot.add_cog(WhisperCog(bot))
