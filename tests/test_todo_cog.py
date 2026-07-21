"""Tests for cogs/todo_cog.py — the /todo command's mod gate."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.cogs.todo_cog import TodoCog


def _member(*, mod: bool) -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.id = 42
    m.guild_permissions = MagicMock(
        administrator=mod, manage_guild=False, manage_channels=False
    )
    return m


def _interaction(user: MagicMock) -> MagicMock:
    i = MagicMock(spec=discord.Interaction)
    i.user = user
    i.guild = MagicMock(id=123)
    i.response = MagicMock()
    i.response.send_message = AsyncMock()
    return i


def _cog() -> TodoCog:
    return TodoCog(MagicMock(), MagicMock())


@pytest.mark.asyncio
async def test_todo_rejects_non_mods():
    """A non-mod can't add to the list — the gate short-circuits before any write."""
    cog = _cog()
    interaction = _interaction(_member(mod=False))
    with patch("bot_modules.cogs.todo_cog.create_todo") as create:
        await cog.todo.callback(cog, interaction, "clean up the channels")
    create.assert_not_called()
    msg = interaction.response.send_message.await_args.args[0]
    assert "moderator" in msg.lower()


@pytest.mark.asyncio
async def test_todo_allows_mods():
    """A mod's task reaches the service layer."""
    cog = _cog()
    interaction = _interaction(_member(mod=True))
    with patch("bot_modules.cogs.todo_cog.create_todo", return_value=7) as create:
        await cog.todo.callback(cog, interaction, "clean up the channels")
    create.assert_called_once()
    assert "#7" in interaction.response.send_message.await_args.args[0]
