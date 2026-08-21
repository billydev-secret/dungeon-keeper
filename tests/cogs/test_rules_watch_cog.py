"""Cog-surface tests for what Rules Watch still exposes in Discord.

The ``/rules-watch`` slash commands (digest / stats / label / status) were
removed 2026-07-28 — they duplicated the dashboard's rules-watch panel. The
only surviving Discord surface is the "Report Rule Violation" message context
menu, which is in-the-moment mod work. These are wiring assertions: that the
menu is still registered behind a manage_guild gate, that its mod gate and
bot-message guard hold, and that the deleted command group has not crept back.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import discord
import pytest
from discord import app_commands

from bot_modules.cogs.rules_watch_cog import (
    _REPORT_CTX_MENU_NAME,
    RulesWatchCog,
    _ReportViolationModal,
)


def _make_cog() -> tuple[RulesWatchCog, MagicMock]:
    """Return (cog, tree_add_command_mock)."""
    bot = MagicMock()
    add_command = MagicMock()
    bot.tree.add_command = add_command
    return RulesWatchCog(bot), add_command


def _registered_menu(add_command: MagicMock) -> app_commands.ContextMenu:
    menus = [
        c.args[0]
        for c in add_command.call_args_list
        if isinstance(c.args[0], app_commands.ContextMenu)
    ]
    assert len(menus) == 1
    return menus[0]


@pytest.mark.asyncio
async def test_cog_load_registers_report_menu_behind_manage_guild():
    cog, add_command = _make_cog()
    await cog.cog_load()

    menu = _registered_menu(add_command)
    assert menu.name == _REPORT_CTX_MENU_NAME
    # The gate CLAUDE.md's safety rule demands: reporting is mod-only, and a
    # passing assertion here is the enforcement.
    assert menu.default_permissions == discord.Permissions(manage_guild=True)


@pytest.mark.asyncio
async def test_report_menu_rejects_non_mod():
    cog, add_command = _make_cog()
    cog.bot.ctx.is_mod.return_value = False
    await cog.cog_load()
    menu = _registered_menu(add_command)

    interaction = MagicMock()
    interaction.response.send_message = _async_noop()
    interaction.response.send_modal = _async_noop()
    message = MagicMock()
    message.author.bot = False

    await menu.callback(interaction, message)

    interaction.response.send_modal.assert_not_called()
    assert interaction.response.send_message.call_count == 1


@pytest.mark.asyncio
async def test_report_menu_refuses_bot_messages():
    cog, add_command = _make_cog()
    cog.bot.ctx.is_mod.return_value = True
    await cog.cog_load()
    menu = _registered_menu(add_command)

    interaction = MagicMock()
    interaction.response.send_message = _async_noop()
    interaction.response.send_modal = _async_noop()
    message = MagicMock()
    message.author.bot = True

    await menu.callback(interaction, message)

    interaction.response.send_modal.assert_not_called()


@pytest.mark.asyncio
async def test_report_menu_opens_modal_for_mod_on_human_message():
    cog, add_command = _make_cog()
    cog.bot.ctx.is_mod.return_value = True
    await cog.cog_load()
    menu = _registered_menu(add_command)

    interaction = MagicMock()
    interaction.response.send_message = _async_noop()
    interaction.response.send_modal = _async_noop()
    message = MagicMock()
    message.author.bot = False

    await menu.callback(interaction, message)

    interaction.response.send_modal.assert_called_once()
    assert isinstance(
        interaction.response.send_modal.call_args.args[0], _ReportViolationModal
    )


def test_rules_watch_slash_group_stays_deleted():
    """Regression guard: the queue, stats, and labeling live on the dashboard
    (rules-watch.js). Re-adding a /rules-watch group would re-split the
    surface CLAUDE.md wants kept on the web."""
    assert not hasattr(RulesWatchCog, "rules_watch")


def _async_noop() -> MagicMock:
    async def _call(*_a, **_kw):
        return None

    return MagicMock(side_effect=_call)
