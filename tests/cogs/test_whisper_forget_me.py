"""Cog-level tests for /whisper forget-me command (S7)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.whisper_repo import get_whisper, insert_whisper
from tests.fakes import FakeMember, fake_interaction


def _find_button_by_label(view: discord.ui.View, label: str) -> discord.ui.Button:
    for item in view.children:
        if isinstance(item, discord.ui.Button) and item.label == label:
            return item
    raise AssertionError(f"button {label!r} not found on view")

GUILD_ID = 9001
USER_A = 1001
USER_B = 2001
USER_C = 3001


# ── Unit: _do_forget_user ─────────────────────────────────────────────────────


def test_forget_user_deletes_sent_and_received(sync_db_path: Path):
    """Deletes whispers where user is sender or target; leaves others untouched."""
    with open_db(sync_db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        # Whisper user A sent to B
        wid_a_to_b = insert_whisper(conn, guild_id=GUILD_ID, sender_id=USER_A, target_id=USER_B, message="from A")
        # Whisper B sent to A
        wid_b_to_a = insert_whisper(conn, guild_id=GUILD_ID, sender_id=USER_B, target_id=USER_A, message="to A")
        # Whisper unrelated (B→C)
        wid_b_to_c = insert_whisper(conn, guild_id=GUILD_ID, sender_id=USER_B, target_id=USER_C, message="unrelated")

    from bot_modules.cogs.whisper_cog import _do_forget_user
    _do_forget_user(sync_db_path, guild_id=GUILD_ID, user_id=USER_A)

    with open_db(sync_db_path) as conn:
        assert get_whisper(conn, wid_a_to_b) is None  # sent by A — deleted
        assert get_whisper(conn, wid_b_to_a) is None  # received by A — deleted
        assert get_whisper(conn, wid_b_to_c) is not None  # unrelated — preserved


def test_forget_user_does_not_touch_other_guilds(sync_db_path: Path):
    """Only deletes rows for the specified guild_id."""
    other_guild = 8888
    with open_db(sync_db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        wid_mine = insert_whisper(conn, guild_id=GUILD_ID, sender_id=USER_A, target_id=USER_B, message="mine")
        wid_other = insert_whisper(conn, guild_id=other_guild, sender_id=USER_A, target_id=USER_B, message="other guild")

    from bot_modules.cogs.whisper_cog import _do_forget_user
    _do_forget_user(sync_db_path, guild_id=GUILD_ID, user_id=USER_A)

    with open_db(sync_db_path) as conn:
        assert get_whisper(conn, wid_mine) is None  # deleted
        assert get_whisper(conn, wid_other) is not None  # other guild — preserved


# ── Cog: /whisper forget-me command ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_forget_me_shows_confirmation_view():
    """The command should respond with a confirmation view."""
    from bot_modules.cogs.whisper_cog import WhisperCog, WhisperForgetMeConfirmView

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    cog = WhisperCog.__new__(WhisperCog)
    cog.bot = bot
    cog.ctx = bot.ctx

    user = FakeMember(id=USER_A)
    interaction = fake_interaction(user=user)
    interaction.guild = MagicMock()
    interaction.guild.id = GUILD_ID
    interaction.response.send_message = AsyncMock()

    # The app_commands.command decorator wraps the method; use .callback
    await cog.whisper_forget_me.callback(cog, interaction)  # type: ignore[attr-defined]

    interaction.response.send_message.assert_awaited_once()
    call_kwargs = interaction.response.send_message.call_args.kwargs
    assert call_kwargs.get("ephemeral") is True
    assert isinstance(call_kwargs.get("view"), WhisperForgetMeConfirmView)


@pytest.mark.asyncio
async def test_forget_me_confirm_deletes_data():
    """Confirming the forget-me view calls _do_forget_user and edits the message."""
    from bot_modules.cogs.whisper_cog import WhisperForgetMeConfirmView

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    view = WhisperForgetMeConfirmView(bot, guild_id=GUILD_ID, user_id=USER_A)
    confirm_button = _find_button_by_label(view, "Yes, delete my data")

    interaction = fake_interaction(user=FakeMember(id=USER_A))
    interaction.response.edit_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._do_forget_user") as forget:
        await confirm_button.callback(interaction)

    forget.assert_called_once_with(":memory:", guild_id=GUILD_ID, user_id=USER_A)
    interaction.response.edit_message.assert_awaited_once()
