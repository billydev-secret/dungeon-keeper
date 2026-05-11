"""Whisper cog — anonymous-message guessing game (Whisper clone)."""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from db_utils import open_db
from services.whisper_models import (
    STATE_HIDDEN,
    STATE_PENDING,
    STATE_SHARED,
    Whisper,
    WhisperConfig,
    WhisperState,
)
from services.whisper_repo import (
    decrement_guesses_left,
    delete_whisper,
    get_whisper,
    get_whisper_config,
    insert_guess,
    insert_reply,
    insert_whisper,
    list_received,
    list_received_in_states,
    mark_exposed,
    mark_solved,
    set_whisper_launcher_message_id,
    set_whisper_message_ids,
    update_whisper_state,
)
from services.whisper_service import (
    ERROR_BOT_DM_FAILED,
    ERROR_GUESS_ALREADY_SOLVED,
    ERROR_GUESS_NO_ATTEMPTS,
    ERROR_GUESS_NOT_TARGET,
    GuessValidationError,
    SendValidationError,
    TransitionValidationError,
    evaluate_guess,
    validate_expose,
    validate_hide,
    validate_send,
    validate_share,
)

if TYPE_CHECKING:
    from app_context import Bot

log = logging.getLogger("dungeonkeeper.whisper")


def _parse_member_id(raw: str) -> int | None:
    """Parse a member ID from raw input that may be a <@123> mention or a bare ID.
    Returns None if no digits are present."""
    digits = "".join(ch for ch in raw.strip() if ch.isdigit())
    return int(digits) if digits else None


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


_INBOX_PAGE_SIZE = 5  # Discord ActionRow limit per message


def _build_inbox(
    bot: Bot,
    whispers: list[Whisper],
    *,
    title: str,
    hidden_view: bool,
) -> tuple[discord.Embed, discord.ui.View]:
    visible = whispers[:_INBOX_PAGE_SIZE]
    embed = discord.Embed(
        title=f"{title} ({len(whispers)})",
        color=discord.Color.blurple(),
    )
    if not visible:
        embed.description = "*No whispers.*"
    else:
        lines: list[str] = []
        for idx, w in enumerate(visible, start=1):
            lines.append(
                f"**Message #{idx}:**\n```{w.message}```"
                f" *({_format_time_ago(w.created_at)})*"
            )
        embed.description = "\n".join(lines)

    if len(whispers) > _INBOX_PAGE_SIZE:
        hint = "Hide old messages to see more." if not hidden_view else ""
        suffix = f" {hint}" if hint else ""
        embed.set_footer(
            text=f"Last {_INBOX_PAGE_SIZE} messages displayed.{suffix} "
            f"{len(whispers)} messages total."
        )
    elif visible:
        plural = "messages" if len(whispers) != 1 else "message"
        embed.set_footer(text=f"{len(whispers)} {plural} total.")

    view = discord.ui.View(timeout=None)
    for idx, w in enumerate(visible, start=1):
        row = idx - 1
        view.add_item(WhisperShareButton(bot, w.id, index=idx, row=row))
        view.add_item(WhisperHideButton(bot, w.id, index=idx, row=row))
        view.add_item(WhisperGuessButton(bot, w.id, index=idx, row=row))
        view.add_item(WhisperReplyButton(bot, w.id, index=idx, row=row))
        view.add_item(WhisperReportButton(bot, w.id, index=idx, row=row))
    return embed, view


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
) -> None:
    with open_db(db_path) as conn:
        insert_guess(conn, whisper_id=whisper_id, guessed_id=guessed_id, correct=correct)
        decrement_guesses_left(conn, whisper_id)
        if correct:
            mark_solved(conn, whisper_id)


def _do_update_state(db_path: Path, whisper_id: int, new_state: WhisperState) -> None:
    with open_db(db_path) as conn:
        update_whisper_state(conn, whisper_id, new_state)


def _do_mark_exposed(db_path: Path, whisper_id: int) -> None:
    with open_db(db_path) as conn:
        mark_exposed(conn, whisper_id)


def _do_list_received(
    db_path: Path, *, guild_id: int, target_id: int, state: WhisperState
) -> list[Whisper]:
    with open_db(db_path) as conn:
        return list_received(conn, guild_id=guild_id, target_id=target_id, state=state)


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
        await interaction.response.send_modal(WhisperGuessModal(self.bot, self.whisper_id))


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

        if interaction.guild:
            cfg = await asyncio.to_thread(
                _load_config, self.bot.ctx.db_path, whisper.guild_id
            )
            feed_channel = interaction.guild.get_channel(cfg.channel_id)
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
                        f"```{whisper.message}```",
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

        if interaction.message:
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


class WhisperHideButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=re.compile(r"whisper:hide:(?P<id>\d+)"),
):
    def __init__(
        self,
        bot: Bot,
        whisper_id: int,
        *,
        index: int | None = None,
        row: int | None = None,
    ) -> None:
        label = f"Hide #{index}" if index else "Hide"
        super().__init__(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.secondary,
                custom_id=f"whisper:hide:{whisper_id}",
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
    ) -> WhisperHideButton:
        return cls(interaction.client, int(match["id"]))  # type: ignore[arg-type]

    async def callback(self, interaction: discord.Interaction) -> None:
        whisper = await asyncio.to_thread(
            _do_load_whisper, self.bot.ctx.db_path, self.whisper_id
        )
        if whisper is None:
            await interaction.response.send_message("Whisper not found.", ephemeral=True)
            return
        try:
            validate_hide(whisper, invoker_id=interaction.user.id)
        except TransitionValidationError as e:
            await interaction.response.send_message(e.message, ephemeral=True)
            return

        await asyncio.to_thread(
            _do_update_state, self.bot.ctx.db_path, self.whisper_id, STATE_HIDDEN
        )

        if interaction.message:
            new_view: discord.ui.View | None
            if whisper.guesses_left > 0 and not whisper.solved:
                new_view = WhisperDmView.without_decide(self.bot, self.whisper_id)
            else:
                new_view = None
            try:
                await interaction.message.edit(view=new_view)
            except discord.HTTPException:
                log.warning("Failed to edit DM view after hide")

        await interaction.response.send_message(
            "Whisper hidden. You can find it under Check Hidden Whispers.",
            ephemeral=True,
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
                    + f"\n\n\U0001f4a5 Sender was {sender_label}."
                )
                await interaction.message.edit(
                    content=new_content,
                    view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                log.warning("Failed to edit message on expose")

        await interaction.response.send_message("Exposed.", ephemeral=True)


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
        if interaction.user.id not in (whisper.sender_id, whisper.target_id):
            await interaction.response.send_message(
                "Only the sender or recipient can reply.", ephemeral=True
            )
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
        if interaction.user.id == whisper.target_id:
            to_user_id = whisper.sender_id
        elif interaction.user.id == whisper.sender_id:
            to_user_id = whisper.target_id
        else:
            await interaction.response.send_message(
                "Only the sender or recipient can reply.", ephemeral=True
            )
            return

        content = str(self.reply_input.value).strip()
        if not content:
            await interaction.response.send_message(
                "Reply can't be empty.", ephemeral=True
            )
            return

        # Try to DM the other party first so we don't persist if undeliverable.
        recipient = interaction.client.get_user(to_user_id) or await interaction.client.fetch_user(to_user_id)  # type: ignore[attr-defined]
        try:
            preview = whisper.message
            if len(preview) > 200:
                preview = preview[:197] + "…"
            await recipient.send(
                f"\U0001f4ec Anonymous reply on whisper *(\"{preview}\")*:\n"
                f"```{content}```",
                view=WhisperReplyDmView(self.bot, self.whisper_id),
            )
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message(
                "Couldn't deliver — they have DMs disabled.", ephemeral=True
            )
            return

        await asyncio.to_thread(
            _do_insert_reply,
            self.bot.ctx.db_path,
            whisper_id=self.whisper_id,
            from_user_id=interaction.user.id,
            to_user_id=to_user_id,
            content=content,
        )
        await interaction.response.send_message(
            "Reply delivered anonymously.", ephemeral=True
        )


class WhisperReplyDmView(discord.ui.View):
    """View attached to incoming reply DMs so the recipient can reply back."""

    def __init__(self, bot: Bot, whisper_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(WhisperReplyButton(bot, whisper_id))


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
        emb = discord.Embed(
            title="Whisper Reported",
            description=whisper.message,
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


# ── Expose view: posted in feed channel after correct guess ──────────────────

class WhisperExposeView(discord.ui.View):
    def __init__(self, bot: Bot, whisper_id: int) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.whisper_id = whisper_id
        self.add_item(WhisperExposeButton(bot, whisper_id))


# ── Per-whisper DM view (Guess + Share + Hide) ───────────────────────────────

class WhisperGuessModal(discord.ui.Modal, title="Guess the sender"):
    guess_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Member ID or @mention",
        placeholder="Right-click a member → Copy ID, or paste a mention",
        required=True,
        max_length=80,
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
            await interaction.response.send_message("Whisper not found.", ephemeral=True)
            return

        guessed_id = _parse_member_id(str(self.guess_input.value))
        if guessed_id is None:
            await interaction.response.send_message(
                "Couldn't parse that. Paste a member ID or @mention.",
                ephemeral=True,
            )
            return

        try:
            outcome = evaluate_guess(
                whisper, guesser_id=interaction.user.id, guessed_id=guessed_id
            )
        except GuessValidationError as e:
            await interaction.response.send_message(e.message, ephemeral=True)
            return

        await asyncio.to_thread(
            _do_record_guess,
            self.bot.ctx.db_path,
            whisper_id=self.whisper_id,
            guessed_id=guessed_id,
            correct=outcome.correct,
        )

        if outcome.correct:
            sender_member = (
                interaction.guild.get_member(whisper.sender_id)
                if interaction.guild else None
            )
            sender_label = (
                sender_member.mention if sender_member else f"<@{whisper.sender_id}>"
            )
            await interaction.response.send_message(
                f"You're right! It was {sender_label}.", ephemeral=True
            )
            cfg = await asyncio.to_thread(
                _load_config, self.bot.ctx.db_path, whisper.guild_id
            )
            if interaction.guild:
                feed_channel = interaction.guild.get_channel(cfg.channel_id)
                if isinstance(feed_channel, discord.TextChannel):
                    try:
                        await feed_channel.send(
                            f"✅ You're Right! <@{whisper.target_id}> figured out who sent the whisper:\n"
                            f"```{whisper.message}```",
                            view=WhisperExposeView(self.bot, self.whisper_id),
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except discord.HTTPException:
                        log.warning("Failed to post solved message to feed")
        elif outcome.exhausted:
            # Remove the Guess button from the original DM message so the
            # target sees Share/Hide only (no more guesses possible).
            if whisper.dm_msg_id:
                try:
                    dm_channel = await interaction.user.create_dm()
                    dm_msg = await dm_channel.fetch_message(whisper.dm_msg_id)
                    await dm_msg.edit(
                        view=WhisperDmView.without_guess(self.bot, self.whisper_id)
                    )
                except discord.HTTPException:
                    log.warning("Failed to remove Guess button from exhausted whisper DM")
            await interaction.response.send_message(
                "Wrong! No more guesses. The sender stays anonymous forever.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"Wrong! {outcome.attempts_remaining} guesses left.",
                ephemeral=True,
            )


class WhisperDmView(discord.ui.View):
    def __init__(self, bot: Bot, whisper_id: int) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.whisper_id = whisper_id
        self.add_item(WhisperGuessButton(bot, whisper_id))
        self.add_item(WhisperShareButton(bot, whisper_id))
        self.add_item(WhisperHideButton(bot, whisper_id))

    @classmethod
    def without_guess(cls, bot: Bot, whisper_id: int) -> WhisperDmView:
        """Build a DM view that omits the Guess button (used after the
        target exhausts their guesses or the whisper is solved)."""
        v: WhisperDmView = cls.__new__(cls)
        discord.ui.View.__init__(v, timeout=None)
        v.bot = bot
        v.whisper_id = whisper_id
        v.add_item(WhisperShareButton(bot, whisper_id))
        v.add_item(WhisperHideButton(bot, whisper_id))
        return v

    @classmethod
    def without_decide(cls, bot: Bot, whisper_id: int) -> WhisperDmView:
        """Build a DM view that omits Share/Hide (used after the target has
        already shared or hidden the whisper)."""
        v: WhisperDmView = cls.__new__(cls)
        discord.ui.View.__init__(v, timeout=None)
        v.bot = bot
        v.whisper_id = whisper_id
        v.add_item(WhisperGuessButton(bot, whisper_id))
        return v


# ── Persistent feed-channel view (Send / Check / Check Hidden) ───────────────

class WhisperSendModal(discord.ui.Modal, title="Send a Whisper"):
    target_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Recipient (member ID or @mention)",
        placeholder="Right-click a member → Copy ID, or paste a mention",
        required=True,
        max_length=80,
    )
    message_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Your anonymous message",
        style=discord.TextStyle.long,
        required=True,
        max_length=1000,
    )

    def __init__(self, bot: Bot) -> None:
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        member_id = _parse_member_id(str(self.target_input.value))
        if member_id is None:
            await interaction.response.send_message(
                "Couldn't parse a member from that input.", ephemeral=True
            )
            return
        target = interaction.guild.get_member(member_id)
        if target is None:
            await interaction.response.send_message(
                "That member isn't in this server.", ephemeral=True
            )
            return
        cog = self.bot.get_cog("WhisperCog")
        if not isinstance(cog, WhisperCog):
            await interaction.response.send_message(
                "Whisper cog isn't loaded.", ephemeral=True
            )
            return
        await cog._send_impl(interaction, target=target, message=str(self.message_input.value))


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
            label="Check Whispers",
            style=discord.ButtonStyle.secondary,
            custom_id="whisper:check",
        )
        check_btn.callback = self._on_check_click
        self.add_item(check_btn)

        hidden_btn = discord.ui.Button(
            label="Check Hidden Whispers",
            style=discord.ButtonStyle.secondary,
            custom_id="whisper:check_hidden",
        )
        hidden_btn.callback = self._on_check_hidden_click
        self.add_item(hidden_btn)

    async def _on_send_click(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(WhisperSendModal(self.bot))

    async def _on_check_click(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        whispers = await asyncio.to_thread(
            _do_list_received_in_states,
            self.bot.ctx.db_path,
            guild_id=interaction.guild.id,
            target_id=interaction.user.id,
            states=[STATE_PENDING, STATE_SHARED],
        )
        embed, view = _build_inbox(
            self.bot, whispers, title="Your Inbox", hidden_view=False
        )
        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True
        )

    async def _on_check_hidden_click(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        whispers = await asyncio.to_thread(
            _do_list_received,
            self.bot.ctx.db_path,
            guild_id=interaction.guild.id,
            target_id=interaction.user.id,
            state=STATE_HIDDEN,
        )
        embed, view = _build_inbox(
            self.bot, whispers, title="Hidden Whispers", hidden_view=True
        )
        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True
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

class WhisperCog(commands.Cog):
    whisper_group = app_commands.Group(name="whisper", description="Send anonymous whispers.")

    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self.ctx = bot.ctx
        self._launcher_locks: dict[int, asyncio.Lock] = {}

    def _get_launcher_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._launcher_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._launcher_locks[guild_id] = lock
        return lock

    async def cog_load(self) -> None:
        # Register persistent views so static-id buttons survive restart.
        self.bot.add_view(WhisperFeedView(self.bot))
        # Register dynamic-id buttons so per-whisper Guess/Share/Hide/Expose
        # button clicks on existing DMs and feed messages still route after
        # a bot restart (custom_ids embed the whisper_id).
        self.bot.add_dynamic_items(
            WhisperGuessButton,
            WhisperShareButton,
            WhisperHideButton,
            WhisperExposeButton,
            WhisperReplyButton,
            WhisperReportButton,
        )
        # Bootstrap launcher in every configured guild so the button bar is
        # at the bottom of the channel from boot.
        for guild in self.bot.guilds:
            try:
                await self.refresh_whisper_launcher(guild.id)
            except Exception:
                log.exception(
                    "Failed to bootstrap whisper launcher for guild %s", guild.id
                )

    async def refresh_whisper_launcher(self, guild_id: int) -> None:
        """Delete the previous launcher (if any) and post a fresh one at the
        bottom of the configured whisper channel. Serialized per-guild."""
        async with self._get_launcher_lock(guild_id):
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

    @commands.Cog.listener("on_message")
    async def _on_message_launcher_bump(self, message: discord.Message) -> None:
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

    @whisper_group.command(name="optin", description="Opt in to send and receive whispers.")
    async def whisper_optin(self, interaction: discord.Interaction) -> None:
        await self._optin_impl(interaction)

    @whisper_group.command(name="optout", description="Opt out of whispers.")
    async def whisper_optout(self, interaction: discord.Interaction) -> None:
        await self._optout_impl(interaction)

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
                f"Someone in **{interaction.guild.name}** sent you a secret message:\n"
                f"```{message.strip()}```",
                view=WhisperDmView(self.bot, whisper_id),
            )
        except (discord.Forbidden, discord.HTTPException):
            # rollback inserted row if DM fails
            await asyncio.to_thread(_do_delete_whisper, self.ctx.db_path, whisper_id)
            await interaction.response.send_message(ERROR_BOT_DM_FAILED, ephemeral=True)
            return

        feed_channel = interaction.guild.get_channel(cfg.channel_id)
        feed_msg = None
        if isinstance(feed_channel, discord.TextChannel):
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

        log_channel = interaction.guild.get_channel(cfg.log_channel_id)
        if isinstance(log_channel, discord.TextChannel):
            try:
                emb = discord.Embed(
                    title="Whisper sent",
                    description=message.strip(),
                    timestamp=discord.utils.utcnow(),
                )
                emb.add_field(
                    name="Sender",
                    value=f"{interaction.user.mention} (`{interaction.user.id}`)",
                    inline=False,
                )
                emb.add_field(
                    name="Target",
                    value=f"{target.mention} (`{target.id}`)",
                    inline=False,
                )
                emb.add_field(name="Whisper ID", value=str(whisper_id), inline=False)
                await log_channel.send(
                    embed=emb, allowed_mentions=discord.AllowedMentions.none()
                )
            except discord.HTTPException:
                log.warning("Failed to write whisper mod log")

        await interaction.response.send_message("Whisper delivered.", ephemeral=True)

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
