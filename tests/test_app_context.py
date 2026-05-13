from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord

from bot_modules.core.app_context import Bot


async def test_setup_hook_skips_sync_for_non_positive_guild_id():
    bot = Bot(intents=discord.Intents.none(), debug=True, guild_id=0)
    bot.tree.sync = AsyncMock()
    with patch("builtins.print") as print_mock:
        await bot.setup_hook()
        await bot.close()

    bot.tree.sync.assert_not_called()
    assert print_mock.called
    assert "skipping guild command sync" in print_mock.call_args_list[0][0][0].lower()


async def test_setup_hook_handles_forbidden_during_debug_guild_sync(tmp_path):
    bot = Bot(intents=discord.Intents.none(), debug=True, guild_id=123)
    bot.ctx = MagicMock()
    bot.ctx.db_path = tmp_path / "test.db"
    forbidden = discord.Forbidden(
        MagicMock(status=403, reason="Forbidden"),
        {"code": 50001, "message": "Missing Access"},
    )
    bot.tree.sync = AsyncMock(side_effect=forbidden)

    async def fake_sync_if_changed(tree, _db_path, *, guild):
        # Mirror real behaviour: call tree.sync, propagate Forbidden upward.
        if guild is None:
            await tree.sync()
        else:
            await tree.sync(guild=guild)
        return [], True

    with patch(
        "bot_modules.services.command_sync.sync_if_changed",
        side_effect=fake_sync_if_changed,
    ), patch("builtins.print") as print_mock:
        await bot.setup_hook()
        await bot.close()

    bot.tree.sync.assert_called_once()
    printed_text = "\n".join(
        str(call.args[0]) for call in print_mock.call_args_list if call.args
    )
    assert "missing access" in printed_text.lower()
