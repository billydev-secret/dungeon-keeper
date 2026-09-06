"""The ``/games help`` panel: a select menu of games, a detail card, and a
Start button that goes through the shared launch guard.

The panel is ephemeral and short-lived (no persistent ``custom_id``): a
member who wants it again types the command again. Everything that decides
*what* to show lives in :mod:`bot_modules.games_help.logic`; everything that
decides *whether a launch may go ahead* lives in
:mod:`bot_modules.games.utils.launch_guard` — this module only wires the two
to Discord, and :func:`start_from_help` is the one seam the wiring test
exercises.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import discord

from bot_modules.games.constants import GAME_NAMES
from bot_modules.games.utils.game_manager import (
    DEFAULT_LAUNCH_PERMS_HINT,
    channel_name,
    sign_off_game_chore,
)
from bot_modules.games.utils.launch_guard import launch_refusal
from bot_modules.games.utils.question_source import channel_allows_nsfw
from bot_modules.games_help.embeds import build_game_detail_embed, build_help_embed
from bot_modules.games_help.logic import game_detail, select_options

log = logging.getLogger(__name__)

PANEL_TIMEOUT_SECONDS = 600


async def start_from_help(
    bot,
    *,
    game_type: str,
    channel,
    channel_id: int | None,
    guild_id: int,
    user_id: int,
    user_name: str,
) -> tuple[bool, str]:
    """Launch *game_type* the way its slash entry would, from the panel.

    Runs the shared launch guard first (allowed channel, enabled dial, busy
    channel, empty bank) and returns its refusal untouched; otherwise calls
    the cog's registered headless ``launch()`` with default options — the
    same door the scheduler and the recap's Play Again use. Returns
    ``(started, message)``; the message is what the panel shows in place of
    itself. Never raises for a missing launcher: a game the panel lists but
    no cog registered gets the permissions hint, not a traceback.
    """
    db = getattr(bot, "games_db", None)
    launcher = getattr(bot, "game_launchers", {}).get(game_type)
    if db is None or launcher is None:
        return False, DEFAULT_LAUNCH_PERMS_HINT
    refusal = await launch_refusal(
        db, game_type, channel_id, guild_id, allow_nsfw=channel_allows_nsfw(channel),
    )
    if refusal:
        return False, refusal
    game_id = await launcher(
        channel=channel,
        host_id=user_id,
        host_name=user_name,
        guild_id=guild_id,
        options={},
    )
    if game_id is None:
        return False, DEFAULT_LAUNCH_PERMS_HINT
    await sign_off_game_chore(bot, guild_id, user_id)
    return True, f"✅ **{GAME_NAMES.get(game_type, game_type)}** is starting in this channel."


class GamePicker(discord.ui.Select):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Pick a game…",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=label, value=key, description=desc)
                for key, label, desc in select_options()
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, GameHelpView)
        await view.show_game(interaction, self.values[0])


class GameHelpView(discord.ui.View):
    """The ephemeral panel. ``color`` is the guild accent the cog resolved;
    ``extra_lines`` are the open rooms for the overview."""

    def __init__(
        self,
        *,
        color: discord.Color | None,
        extra_lines: Sequence[str] = (),
    ) -> None:
        super().__init__(timeout=PANEL_TIMEOUT_SECONDS)
        self.color = color
        self.extra_lines = list(extra_lines)
        self.selected: str | None = None
        self.picker = GamePicker()
        self._build()

    def _build(self) -> None:
        self.clear_items()
        self.add_item(self.picker)
        if self.selected is not None:
            if game_detail(self.selected).startable:
                self.add_item(self.start_button)
            self.add_item(self.back_button)

    def overview_embed(self) -> discord.Embed:
        return build_help_embed(self.color, extra_lines=self.extra_lines)

    async def show_game(self, interaction: discord.Interaction, key: str) -> None:
        self.selected = key
        self._build()
        await interaction.response.edit_message(
            embed=build_game_detail_embed(key, self.color), view=self,
        )

    @discord.ui.button(label="▶️ Start Here", style=discord.ButtonStyle.primary)
    async def start_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        key = self.selected
        if key is None:
            await interaction.response.defer()
            return
        log.info(
            "%s pressed Start Here for %s in #%s",
            interaction.user.display_name, key, channel_name(interaction.channel),
        )
        # The launch posts the game's own board; defer so the panel edit
        # can wait for it without the interaction token expiring.
        await interaction.response.defer()
        started, message = await start_from_help(
            interaction.client,
            game_type=key,
            channel=interaction.channel,
            channel_id=interaction.channel_id,
            guild_id=interaction.guild_id or 0,
            user_id=interaction.user.id,
            user_name=interaction.user.display_name,
        )
        if started:
            self.stop()
            await interaction.edit_original_response(content=message, embed=None, view=None)
        else:
            # A refusal keeps the panel: the member can pick something else.
            await interaction.followup.send(message, ephemeral=True)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary)
    async def back_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.selected = None
        self._build()
        await interaction.response.edit_message(embed=self.overview_embed(), view=self)
