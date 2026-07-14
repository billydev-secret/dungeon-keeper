"""Role Menus cog — registers the persistent components; no slash commands.

Everything member-facing happens through the DynamicItems (clicks on published
menu messages), and everything admin-facing happens on the dashboard
(``web_server/routes/role_menus.py``). The cog's only job is making the
components survive restarts.
"""

from __future__ import annotations

from discord.ext import commands

from bot_modules.core.app_context import AppContext, Bot
from bot_modules.role_menus.views import ROLE_MENU_DYNAMIC_ITEMS


class RoleMenusCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx

    async def cog_load(self) -> None:
        for cls in ROLE_MENU_DYNAMIC_ITEMS:
            self.bot.add_dynamic_items(cls)


async def setup(bot: Bot) -> None:
    await bot.add_cog(RoleMenusCog(bot, bot.ctx))
