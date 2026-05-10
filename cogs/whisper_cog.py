"""Whisper cog — anonymous-message guessing game (Whisper clone)."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from db_utils import open_db
from services.whisper_models import Whisper, WhisperConfig
from services.whisper_repo import (
    decrement_guesses_left,
    get_whisper,
    get_whisper_config,
    insert_guess,
    insert_whisper,
    list_received,
    mark_exposed,
    mark_solved,
    set_whisper_message_ids,
    update_whisper_state,
)

if TYPE_CHECKING:
    from app_context import Bot

log = logging.getLogger("dungeonkeeper.whisper")


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


def _do_update_state(db_path: Path, whisper_id: int, new_state: str) -> None:
    with open_db(db_path) as conn:
        update_whisper_state(conn, whisper_id, new_state)


def _do_mark_exposed(db_path: Path, whisper_id: int) -> None:
    with open_db(db_path) as conn:
        mark_exposed(conn, whisper_id)


def _do_list_received(
    db_path: Path, *, guild_id: int, target_id: int, state: str
) -> list[Whisper]:
    with open_db(db_path) as conn:
        return list_received(conn, guild_id=guild_id, target_id=target_id, state=state)


# ── Cog ──────────────────────────────────────────────────────────────────────

class WhisperCog(commands.Cog):
    whisper_group = app_commands.Group(name="whisper", description="Send anonymous whispers.")

    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self.ctx = bot.ctx

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
        try:
            await interaction.user.add_roles(role, reason="Whisper opt-in")  # type: ignore[union-attr]
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to assign that role.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "You've opted in. You can now send and receive whispers.",
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


async def setup(bot: Bot) -> None:
    await bot.add_cog(WhisperCog(bot))
