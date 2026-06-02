"""Discord UI views for the bios wizard.

`PersistentTriggerView` — the public "Create / Update Bio" button posted
into the bios channel. Persistent (`timeout=None`) with a fixed
`custom_id` so it survives bot restarts; its callback re-acquires the
cog instance via ``interaction.client.get_cog("BiosCog")``.

`ResumeRestartView` — ephemeral two-button choice shown when the user
re-triggers the wizard while a session is already live.

`StepControlsView` — the controls strip rendered on every wizard step:
Skip / Back / Cancel, plus a Keep button in edit mode when a stored
value exists, plus a Re-roll button on question steps. The wizard owns
the state and passes a ``callbacks`` mapping; this view just dispatches.

`ChoiceButtonsView` / `ChoiceSelectView` — the input controls for a
``field_type='choice'`` step. ≤5 options → buttons; >5 → a select menu.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast

import discord
from discord.ext import commands

ControlCallback = Callable[[discord.Interaction], Awaitable[None]]
PickCallback = Callable[[discord.Interaction, str], Awaitable[None]]


# ── Public trigger button ─────────────────────────────────────────────


class PersistentTriggerView(discord.ui.View):
    """The "Create / Update Bio" button posted into the bios channel."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Create / Update Bio",
        style=discord.ButtonStyle.primary,
        custom_id="bios_trigger",
        emoji="📝",
    )
    async def trigger(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        bot = cast(commands.Bot, interaction.client)
        cog = bot.get_cog("BiosCog")
        if cog is None:
            await interaction.response.send_message(
                "Bios are temporarily unavailable. Try again in a moment.",
                ephemeral=True,
            )
            return
        # `_start_or_resume` is the cog's dispatcher.
        await cog._start_or_resume(interaction)  # type: ignore[attr-defined]


# ── Resume / Restart prompt ───────────────────────────────────────────


class ResumeRestartView(discord.ui.View):
    """Shown when the user re-triggers while a session is already live."""

    def __init__(
        self,
        *,
        on_resume: ControlCallback,
        on_restart: ControlCallback,
        owner_id: int,
        timeout: float = 60.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self._on_resume = on_resume
        self._on_restart = on_restart
        self._owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self._owner_id

    @discord.ui.button(label="Resume", style=discord.ButtonStyle.primary)
    async def resume(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await self._on_resume(interaction)

    @discord.ui.button(label="Restart", style=discord.ButtonStyle.danger)
    async def restart(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await self._on_restart(interaction)


# ── Step controls (Skip / Back / Cancel / Keep / Re-roll) ─────────────


class StepControlsView(discord.ui.View):
    """Step controls rendered above every wizard prompt.

    All buttons are gated on ``interaction.user == owner``. The wizard
    owns step navigation logic and passes coroutine callbacks that this
    view simply dispatches to.
    """

    def __init__(
        self,
        *,
        owner_id: int,
        on_skip: ControlCallback | None,
        on_back: ControlCallback | None,
        on_cancel: ControlCallback,
        on_keep: ControlCallback | None = None,
        on_reroll: ControlCallback | None = None,
        reroll_note: str | None = None,
        timeout: float = 900.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self._owner_id = owner_id

        if on_skip is not None:
            skip_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
                label="Skip", style=discord.ButtonStyle.secondary
            )
            skip_btn.callback = self._wrap(on_skip)  # type: ignore[assignment]
            self.add_item(skip_btn)

        if on_back is not None:
            back_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
                label="Back", style=discord.ButtonStyle.secondary
            )
            back_btn.callback = self._wrap(on_back)  # type: ignore[assignment]
            self.add_item(back_btn)

        if on_keep is not None:
            keep_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
                label="Keep current", style=discord.ButtonStyle.success
            )
            keep_btn.callback = self._wrap(on_keep)  # type: ignore[assignment]
            self.add_item(keep_btn)

        if on_reroll is not None:
            reroll_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
                label="Re-roll question",
                style=discord.ButtonStyle.secondary,
                emoji="🎲",
                disabled=reroll_note is not None,
            )
            reroll_btn.callback = self._wrap(on_reroll)  # type: ignore[assignment]
            self.add_item(reroll_btn)

        cancel_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="Cancel", style=discord.ButtonStyle.danger
        )
        cancel_btn.callback = self._wrap(on_cancel)  # type: ignore[assignment]
        self.add_item(cancel_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self._owner_id

    def _wrap(self, cb: ControlCallback) -> ControlCallback:
        async def _inner(interaction: discord.Interaction) -> None:
            for child in self.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = True
            await cb(interaction)
        return _inner


# ── Choice input (buttons ≤5, select >5) ──────────────────────────────


class ChoiceButtonsView(discord.ui.View):
    """≤5 choice buttons for a `field_type='choice'` step."""

    def __init__(
        self,
        *,
        owner_id: int,
        choices: list[str],
        on_pick: PickCallback,
        current: str = "",
        timeout: float = 900.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self._owner_id = owner_id
        for choice in choices[:5]:
            style = (
                discord.ButtonStyle.success
                if choice == current
                else discord.ButtonStyle.primary
            )
            btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
                label=(choice[:75] or " "), style=style
            )
            btn.callback = self._make_callback(on_pick, choice)  # type: ignore[assignment]
            self.add_item(btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self._owner_id

    def _make_callback(
        self, on_pick: PickCallback, choice: str
    ) -> ControlCallback:
        async def _cb(interaction: discord.Interaction) -> None:
            for child in self.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = True
            await on_pick(interaction, choice)
        return _cb


class ChoiceSelectView(discord.ui.View):
    """A select menu for >5 choices."""

    def __init__(
        self,
        *,
        owner_id: int,
        choices: list[str],
        on_pick: PickCallback,
        current: str = "",
        timeout: float = 900.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self._owner_id = owner_id
        self._on_pick = on_pick

        options = [
            discord.SelectOption(
                label=c[:100] or " ",
                value=c[:100] or " ",
                default=(c == current),
            )
            for c in choices[:25]
        ]
        self._select: discord.ui.Select = discord.ui.Select(  # type: ignore[type-arg]
            placeholder="Pick one…",
            options=options,
            min_values=1,
            max_values=1,
        )
        self._select.callback = self._on_select  # type: ignore[assignment]
        self.add_item(self._select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self._owner_id

    async def _on_select(self, interaction: discord.Interaction) -> None:
        value = self._select.values[0] if self._select.values else ""
        self._select.disabled = True
        await self._on_pick(interaction, value)


__all__ = [
    "PersistentTriggerView",
    "ResumeRestartView",
    "StepControlsView",
    "ChoiceButtonsView",
    "ChoiceSelectView",
]
