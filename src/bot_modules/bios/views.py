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


# ── Combined step view (one message per step) ────────────────────────


class CombinedStepView(discord.ui.View):
    """One View that holds the step's input control on row 0 and the
    Skip/Back/Keep/Cancel buttons on row 1. Used so a single message
    per step carries the prompt embed plus all interactive widgets — it
    keeps the active prompt at the bottom of the channel as the user
    works through the wizard.

    All component callbacks are gated on ``owner_id``.
    """

    def __init__(
        self,
        *,
        owner_id: int,
        on_skip: ControlCallback | None,
        on_back: ControlCallback | None,
        on_cancel: ControlCallback,
        on_keep: ControlCallback | None = None,
        # Choice-field input (≤5 buttons OR a select for >5)
        on_choice_pick: PickCallback | None = None,
        choice_options: list[str] | None = None,
        current_choice: str = "",
        # Question-step picker (always a select)
        on_question_pick: Callable[[discord.Interaction, int], Awaitable[None]] | None = None,
        question_options: list[tuple[int, str]] | None = None,
        current_question_id: int | None = None,
        timeout: float = 900.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self._owner_id = owner_id

        # Row 0 — input control (choice picker OR question picker)
        if on_choice_pick is not None and choice_options:
            opts = list(choice_options)
            if len(opts) <= 5:
                for choice in opts[:5]:
                    style = (
                        discord.ButtonStyle.success
                        if choice == current_choice
                        else discord.ButtonStyle.primary
                    )
                    btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
                        label=(choice[:75] or " "), style=style, row=0
                    )
                    btn.callback = self._wrap_pick(on_choice_pick, choice)  # type: ignore[assignment]
                    self.add_item(btn)
            else:
                sel: discord.ui.Select = discord.ui.Select(  # type: ignore[type-arg]
                    placeholder="Pick one…",
                    options=[
                        discord.SelectOption(
                            label=c[:100] or " ",
                            value=c[:100] or " ",
                            default=(c == current_choice),
                        )
                        for c in opts[:25]
                    ],
                    min_values=1,
                    max_values=1,
                    row=0,
                )

                async def _on_choice_select(interaction: discord.Interaction) -> None:
                    value = sel.values[0] if sel.values else ""
                    sel.disabled = True
                    await on_choice_pick(interaction, value)

                sel.callback = _on_choice_select  # type: ignore[assignment]
                self.add_item(sel)
        elif on_question_pick is not None and question_options:
            qopts = list(question_options)
            current_qid = current_question_id if current_question_id is not None else 0
            qsel: discord.ui.Select = discord.ui.Select(  # type: ignore[type-arg]
                placeholder="Pick a different icebreaker…",
                options=[
                    discord.SelectOption(
                        label=(prompt[:100] or "—"),
                        value=str(qid),
                        default=(qid == current_qid),
                    )
                    for (qid, prompt) in qopts[:25]
                ],
                min_values=1,
                max_values=1,
                row=0,
            )

            async def _on_question_select(interaction: discord.Interaction) -> None:
                value = qsel.values[0] if qsel.values else ""
                try:
                    picked_id = int(value)
                except (TypeError, ValueError):
                    await interaction.response.defer()
                    return
                qsel.disabled = True
                await on_question_pick(interaction, picked_id)

            qsel.callback = _on_question_select  # type: ignore[assignment]
            self.add_item(qsel)

        # Row 1 — control buttons (Skip / Back / Keep / Cancel)
        if on_skip is not None:
            self._add_ctrl_btn("Skip", discord.ButtonStyle.secondary, on_skip)
        if on_back is not None:
            self._add_ctrl_btn("Back", discord.ButtonStyle.secondary, on_back)
        if on_keep is not None:
            self._add_ctrl_btn("Keep current", discord.ButtonStyle.success, on_keep)
        self._add_ctrl_btn("Cancel", discord.ButtonStyle.danger, on_cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self._owner_id

    def _add_ctrl_btn(
        self, label: str, style: discord.ButtonStyle, cb: ControlCallback
    ) -> None:
        btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label=label, style=style, row=1
        )
        btn.callback = self._wrap_ctrl(cb)  # type: ignore[assignment]
        self.add_item(btn)

    def _wrap_ctrl(self, cb: ControlCallback) -> ControlCallback:
        async def _inner(interaction: discord.Interaction) -> None:
            for child in self.children:
                if isinstance(child, (discord.ui.Button, discord.ui.Select)):
                    child.disabled = True
            await cb(interaction)
        return _inner

    def _wrap_pick(
        self, on_pick: PickCallback, choice: str
    ) -> ControlCallback:
        async def _inner(interaction: discord.Interaction) -> None:
            for child in self.children:
                if isinstance(child, (discord.ui.Button, discord.ui.Select)):
                    child.disabled = True
            await on_pick(interaction, choice)
        return _inner


# ── Paginated question browser ────────────────────────────────────────


class BrowseQuestionsView(discord.ui.View):
    """Paginated dropdown of unanswered active questions.

    Row 0: select menu (up to 25 options for the current page).
    Row 1: ◀ Prev, ▶ Next, ✓ Done, Cancel.

    Pager buttons are sent even when there's only one page so the user
    has a consistent set of controls; they're disabled when there's
    nowhere to go.
    """

    def __init__(
        self,
        *,
        owner_id: int,
        page_options: list[tuple[int, str]],
        on_pick: Callable[[discord.Interaction, int], Awaitable[None]],
        on_prev: ControlCallback,
        on_next: ControlCallback,
        on_done: ControlCallback,
        on_cancel: ControlCallback,
        on_back: ControlCallback | None,
        can_prev: bool,
        can_next: bool,
        can_done: bool,
        timeout: float = 900.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self._owner_id = owner_id
        self._on_pick = on_pick

        if page_options:
            sel: discord.ui.Select = discord.ui.Select(  # type: ignore[type-arg]
                placeholder="Pick a question to answer…",
                options=[
                    discord.SelectOption(
                        label=(prompt[:100] or "—"),
                        value=str(qid),
                    )
                    for (qid, prompt) in page_options[:25]
                ],
                min_values=1,
                max_values=1,
                row=0,
            )
            sel.callback = self._on_select  # type: ignore[assignment]
            self._select = sel
            self.add_item(sel)
        else:
            self._select = None

        prev_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="◀ Prev",
            style=discord.ButtonStyle.secondary,
            disabled=not can_prev,
            row=1,
        )
        prev_btn.callback = self._wrap(on_prev)  # type: ignore[assignment]
        self.add_item(prev_btn)

        next_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="Next ▶",
            style=discord.ButtonStyle.secondary,
            disabled=not can_next,
            row=1,
        )
        next_btn.callback = self._wrap(on_next)  # type: ignore[assignment]
        self.add_item(next_btn)

        if on_back is not None:
            back_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
                label="Back to fields",
                style=discord.ButtonStyle.secondary,
                row=1,
            )
            back_btn.callback = self._wrap(on_back)  # type: ignore[assignment]
            self.add_item(back_btn)

        done_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="✓ Done",
            style=discord.ButtonStyle.success,
            disabled=not can_done,
            row=1,
        )
        done_btn.callback = self._wrap(on_done)  # type: ignore[assignment]
        self.add_item(done_btn)

        cancel_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="Cancel",
            style=discord.ButtonStyle.danger,
            row=1,
        )
        cancel_btn.callback = self._wrap(on_cancel)  # type: ignore[assignment]
        self.add_item(cancel_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self._owner_id

    def _wrap(self, cb: ControlCallback) -> ControlCallback:
        async def _inner(interaction: discord.Interaction) -> None:
            for child in self.children:
                if isinstance(child, (discord.ui.Button, discord.ui.Select)):
                    child.disabled = True
            await cb(interaction)
        return _inner

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if self._select is None:
            await interaction.response.defer()
            return
        value = self._select.values[0] if self._select.values else ""
        try:
            qid = int(value)
        except (TypeError, ValueError):
            await interaction.response.defer()
            return
        for child in self.children:
            if isinstance(child, (discord.ui.Button, discord.ui.Select)):
                child.disabled = True
        await self._on_pick(interaction, qid)


__all__ = [
    "PersistentTriggerView",
    "ResumeRestartView",
    "CombinedStepView",
    "BrowseQuestionsView",
]
