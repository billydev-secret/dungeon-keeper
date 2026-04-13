import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from app_context import Bot


class BotSetupHookTests(unittest.TestCase):
    def test_setup_hook_skips_sync_for_non_positive_guild_id(self):
        bot = Bot(intents=discord.Intents.none(), debug=True, guild_id="0")
        bot.tree.sync = AsyncMock()
        with patch("builtins.print") as print_mock:
            asyncio.run(bot.setup_hook())
            asyncio.run(bot.close())

        bot.tree.sync.assert_not_called()
        self.assertTrue(print_mock.called)
        self.assertIn(
            "skipping guild command sync", print_mock.call_args_list[0][0][0].lower()
        )

    def test_setup_hook_handles_forbidden_during_debug_guild_sync(self):
        bot = Bot(intents=discord.Intents.none(), debug=True, guild_id=123)
        forbidden = discord.Forbidden(
            MagicMock(status=403, reason="Forbidden"),
            {"code": 50001, "message": "Missing Access"},
        )
        bot.tree.sync = AsyncMock(side_effect=forbidden)
        with patch("builtins.print") as print_mock:
            asyncio.run(bot.setup_hook())
            asyncio.run(bot.close())

        bot.tree.sync.assert_called_once()
        printed_text = "\n".join(
            str(call.args[0]) for call in print_mock.call_args_list if call.args
        )
        self.assertIn("missing access", printed_text.lower())


if __name__ == "__main__":
    unittest.main()
