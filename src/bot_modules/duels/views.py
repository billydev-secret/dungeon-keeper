"""Discord UI views shared across all duel game types."""
from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable

import discord

from bot_modules.core.utils import disable_all_items
from bot_modules.duels.db import CHALLENGE_RESPONSE_SECONDS, REMATCH_WINDOW_SECONDS

log = logging.getLogger("dungeonkeeper.duels")

#: What a late presser is told. One literal for the two places that say it —
#: the stale-state reply in ``BaseDuel`` and the deadline check on a view
#: re-attached after a restart — so the number can't drift between them.
CHALLENGE_TIMED_OUT_TEXT = (
    "⏱️ That challenge timed out before you pressed — challenges expire "
    f"{CHALLENGE_RESPONSE_SECONDS // 60} minutes after they're posted. "
    "Ask them to send another one."
)


class ChallengeView(discord.ui.View):
    """Accept/Decline embed — target only.

    Fresh from a challenge it is NOT persistent: the timeout is
    :data:`CHALLENGE_RESPONSE_SECONDS`, the same number the card counts down
    to and the expiry sweep uses. Re-attached after a restart (``deadline``
    given) it is persistent — ``bot.add_view`` accepts nothing else — and
    enforces the card's original deadline itself, refusing a press past it
    with :data:`CHALLENGE_TIMED_OUT_TEXT`.
    """

    def __init__(
        self,
        game_id: int,
        target_id: int,
        on_accept: Callable[[discord.Interaction, int], Awaitable[None]],
        on_decline: Callable[[discord.Interaction, int], Awaitable[None]],
        *,
        deadline: float | None = None,
    ) -> None:
        super().__init__(timeout=None if deadline is not None else CHALLENGE_RESPONSE_SECONDS)
        self.game_id = game_id
        self.target_id = target_id
        self.deadline = deadline
        self._on_accept = on_accept
        self._on_decline = on_decline

        accept_btn = discord.ui.Button(
            label="Accept",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id=f"challenge_accept:{game_id}",
        )
        accept_btn.callback = self._accept_callback

        decline_btn = discord.ui.Button(
            label="Decline",
            style=discord.ButtonStyle.danger,
            emoji="❌",
            custom_id=f"challenge_decline:{game_id}",
        )
        decline_btn.callback = self._decline_callback

        self.add_item(accept_btn)
        self.add_item(decline_btn)

    def _disable_all(self) -> None:
        disable_all_items(self)

    async def _refuse_if_past_deadline(self, interaction: discord.Interaction) -> bool:
        """True (and answered) when a re-attached view's deadline has passed."""
        if self.deadline is None or time.time() <= self.deadline:
            return False
        await interaction.response.send_message(CHALLENGE_TIMED_OUT_TEXT, ephemeral=True)
        return True

    async def _accept_callback(self, interaction: discord.Interaction) -> None:
        log.info("%s accepted challenge (game %d)", interaction.user.display_name, self.game_id)
        if interaction.user.id != self.target_id:
            await interaction.response.send_message(
                "❌ Only the challenged player can accept.", ephemeral=True
            )
            return
        if await self._refuse_if_past_deadline(interaction):
            return
        self.stop()
        self._disable_all()
        await self._on_accept(interaction, self.game_id)

    async def _decline_callback(self, interaction: discord.Interaction) -> None:
        log.info("%s declined challenge (game %d)", interaction.user.display_name, self.game_id)
        if interaction.user.id != self.target_id:
            await interaction.response.send_message(
                "❌ Only the challenged player can decline.", ephemeral=True
            )
            return
        if await self._refuse_if_past_deadline(interaction):
            return
        self.stop()
        self._disable_all()
        await self._on_decline(interaction, self.game_id)

    async def on_timeout(self) -> None:
        self._disable_all()

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        log.exception("ChallengeView error (game %d)", self.game_id, exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong.", ephemeral=True)


#: What a late Run It Back presser is told.
REMATCH_EXPIRED_TEXT = (
    "❌ That result is too old to run back — the button works for "
    f"{REMATCH_WINDOW_SECONDS // 60} minutes after a game ends. "
    "Start a fresh one with the command."
)


class ResultView(discord.ui.View):
    """Post-game buttons — persistent (timeout=None).

    ``📝 Name the Loser`` (winner-only, nickname games only) and
    ``🔁 Run It Back`` (either duelist, or the lobby host — the handler
    decides). The rematch button has a life of its own: the view is
    persistent so it survives a restart, so the deadline is enforced in the
    callback rather than by a view timeout, and a press past it gets
    :data:`REMATCH_EXPIRED_TEXT`. ``rematch_deadline`` of None omits the
    button (a card re-attached after the window has already closed).
    """

    def __init__(
        self,
        game_id: int,
        winner_id: int,
        loser_id: int,
        on_set_nick: Callable[[discord.Interaction, int], Awaitable[None]] | None,
        *,
        on_rematch: Callable[[discord.Interaction, int], Awaitable[None]] | None = None,
        rematch_deadline: float | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.game_id = game_id
        self.winner_id = winner_id
        self.loser_id = loser_id
        self._on_set_nick = on_set_nick
        self._on_rematch = on_rematch
        self.rematch_deadline = rematch_deadline

        if on_set_nick is not None:
            nick_btn = discord.ui.Button(
                label="Name the Loser",
                style=discord.ButtonStyle.primary,
                emoji="📝",
                custom_id=f"set_nick:{game_id}",
            )
            nick_btn.callback = self._set_nick_callback
            self.add_item(nick_btn)

        if on_rematch is not None and rematch_deadline is not None:
            rematch_btn = discord.ui.Button(
                label="Run It Back",
                style=discord.ButtonStyle.secondary,
                emoji="🔁",
                custom_id=f"rematch:{game_id}",
            )
            rematch_btn.callback = self._rematch_callback
            self.add_item(rematch_btn)

    async def _set_nick_callback(self, interaction: discord.Interaction) -> None:
        log.info(
            "%s pressed Name the loser (game %d)", interaction.user.display_name, self.game_id
        )
        if interaction.user.id != self.winner_id:
            await interaction.response.send_message(
                "❌ Only the winner can name the loser.", ephemeral=True
            )
            return
        assert self._on_set_nick is not None
        await self._on_set_nick(interaction, self.game_id)

    async def _rematch_callback(self, interaction: discord.Interaction) -> None:
        log.info(
            "%s pressed Run It Back (game %d)", interaction.user.display_name, self.game_id
        )
        if self.rematch_deadline is not None and time.time() > self.rematch_deadline:
            await interaction.response.send_message(REMATCH_EXPIRED_TEXT, ephemeral=True)
            return
        assert self._on_rematch is not None
        await self._on_rematch(interaction, self.game_id)

    def disable(self) -> None:
        disable_all_items(self)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        log.exception("ResultView error (game %d)", self.game_id, exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong.", ephemeral=True)
