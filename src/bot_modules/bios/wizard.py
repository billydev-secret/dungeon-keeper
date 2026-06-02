"""Wizard session orchestration for the bios cog.

One ``WizardSession`` per in-flight bio. The cog owns the registry; the
session owns its private wizard channel, the in-memory ``WizardState``,
and the message/button capture loop. Nothing is persisted until the
last step succeeds.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

import discord

from bot_modules.bios import db as bios_db
from bot_modules.bios.config import BiosConfig
from bot_modules.bios.embeds import build_bio_embed
from bot_modules.bios.logic import (
    BioField,
    BioQuestion,
    BioRenderPayload,
    FieldSnapshot,
    QuestionSnapshot,
    WizardState,
    draw_weighted,
    headline_value,
)
from bot_modules.bios.views import CombinedStepView

if TYPE_CHECKING:
    from bot_modules.cogs.bios_cog import BiosCog

log = logging.getLogger("dungeonkeeper.bios.wizard")


# ── Step action types ─────────────────────────────────────────────────


ActionKind = Literal[
    "answer", "skip", "back", "cancel", "keep", "pick_question", "timeout"
]


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    value: str = ""  # for "answer" steps
    interaction: discord.Interaction | None = None


# ── Session ───────────────────────────────────────────────────────────


@dataclass
class WizardSession:
    cog: "BiosCog"
    member: discord.Member
    config: BiosConfig
    state: WizardState
    prior_field_values: dict[int, str] = field(default_factory=dict)
    prior_slot_answers: dict[int, tuple[int, str, str]] = field(default_factory=dict)
    snapshotted_question_texts: dict[int, str] = field(default_factory=dict)

    channel: discord.TextChannel | None = None
    _loop_task: asyncio.Task | None = None
    _action_q: asyncio.Queue[Action] = field(default_factory=asyncio.Queue)
    _idle_task: asyncio.Task | None = None

    # ── Lifecycle ────────────────────────────────────────────────────

    @property
    def key(self) -> tuple[int, int]:
        return (self.member.guild.id, self.member.id)

    async def create_channel(self) -> discord.TextChannel:
        """Create the private wizard channel under the configured category."""
        guild = self.member.guild
        category = guild.get_channel(self.config.wizard_category_id)
        if not isinstance(category, discord.CategoryChannel):
            raise RuntimeError("wizard category not found or not a category")
        overwrites: dict[
            discord.Role | discord.Member | discord.Object, discord.PermissionOverwrite
        ] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            self.member: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
        }
        me = guild.me
        if me is not None:
            overwrites[me] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_messages=True
            )
        self.channel = await guild.create_text_channel(
            name=f"bio-{self.member.id}",
            category=category,
            overwrites=overwrites,
            reason=f"Bios wizard for {self.member} ({self.member.id})",
        )
        intro = self._build_intro_embed()
        await self.channel.send(content=self.member.mention, embed=intro)
        return self.channel

    def start_loop(self) -> None:
        """Kick off the background step loop; returns immediately."""
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._run_loop())

    async def cancel(self, reason: str = "user") -> None:
        """Tear down: delete the wizard channel, drop the in-memory state."""
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None
        if self._loop_task is not None and self._loop_task is not asyncio.current_task():
            self._loop_task.cancel()
        if self.channel is not None:
            try:
                await self.channel.delete(reason=f"Bios wizard {reason}")
            except (discord.NotFound, discord.Forbidden):
                pass
            except discord.HTTPException:
                log.exception("Failed to delete wizard channel %d", self.channel.id)
            self.channel = None
        self.cog._sessions.pop(self.key, None)

    # ── Action capture ───────────────────────────────────────────────

    def push_action(self, action: Action) -> None:
        """Called by view callbacks and the message listener."""
        self._action_q.put_nowait(action)
        self._reset_idle_timer()

    def _reset_idle_timer(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
        self._idle_task = asyncio.create_task(self._idle_watch())

    async def _idle_watch(self) -> None:
        try:
            await asyncio.sleep(self.config.wizard_timeout_minutes * 60)
        except asyncio.CancelledError:
            return
        self._action_q.put_nowait(Action(kind="timeout"))

    # ── Step loop ────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        self._reset_idle_timer()
        try:
            while True:
                kind = self.state.step_kind()
                if kind == "done":
                    await self._complete()
                    return
                step_action = await self._run_step()
                if step_action.kind == "cancel":
                    await self.cancel("user")
                    return
                if step_action.kind == "timeout":
                    await self._handle_timeout()
                    return
                await self._apply_action_to_state(step_action)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Bios wizard loop crashed for %d", self.member.id)
            await self.cancel("error")

    async def _run_step(self) -> Action:
        kind = self.state.step_kind()
        assert self.channel is not None
        if kind == "field":
            f = self.state.current_field()
            assert f is not None
            return await self._render_field_step(f)
        if kind == "question":
            slot = self.state.current_slot_index()
            assert slot is not None
            q = self.state.slots[slot]
            return await self._render_question_step(slot, q)
        return Action(kind="cancel")

    async def _render_field_step(self, f: BioField) -> Action:
        prior = self.prior_field_values.get(f.id, "")
        prompt_embed = self._build_field_prompt_embed(f, prior)
        is_choice = f.field_type == "choice"
        text_capture_task: asyncio.Task | None = None

        async def on_skip(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            self.push_action(Action(kind="skip"))

        async def on_back(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            self.push_action(Action(kind="back"))

        async def on_cancel(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            self.push_action(Action(kind="cancel"))

        async def on_keep(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            self.push_action(Action(kind="keep"))

        async def on_pick(interaction: discord.Interaction, value: str) -> None:
            await interaction.response.defer()
            self.push_action(Action(kind="answer", value=value))

        view = CombinedStepView(
            owner_id=self.member.id,
            on_skip=on_skip if not f.required else None,
            on_back=on_back if self.state.step_index > 0 else None,
            on_cancel=on_cancel,
            on_keep=on_keep if (self.state.mode == "edit" and prior) else None,
            on_choice_pick=on_pick if (is_choice and f.choices) else None,
            choice_options=list(f.choices) if (is_choice and f.choices) else None,
            current_choice=prior,
        )

        assert self.channel is not None
        await self.channel.send(embed=prompt_embed, view=view)
        if not is_choice:
            # Text capture for short / paragraph
            text_capture_task = asyncio.create_task(self._capture_one_message(f.max_len))

        try:
            action = await self._action_q.get()
        except asyncio.CancelledError:
            if text_capture_task is not None:
                text_capture_task.cancel()
            raise

        if text_capture_task is not None and not text_capture_task.done():
            text_capture_task.cancel()
        return action

    async def _render_question_step(self, slot: int, q: BioQuestion) -> Action:
        prior_answer = ""
        if slot in self.prior_slot_answers:
            _, _, prior_answer = self.prior_slot_answers[slot]
        prompt_embed = self._build_question_prompt_embed(q, prior_answer)

        async def on_skip(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            self.push_action(Action(kind="skip"))

        async def on_back(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            self.push_action(Action(kind="back"))

        async def on_cancel(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            self.push_action(Action(kind="cancel"))

        async def on_keep(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            self.push_action(Action(kind="keep"))

        async def on_pick(interaction: discord.Interaction, qid: int) -> None:
            await interaction.response.defer()
            self.push_action(Action(kind="pick_question", value=str(qid)))

        picker_options = await self._build_picker_options(slot)

        view = CombinedStepView(
            owner_id=self.member.id,
            on_skip=on_skip,
            on_back=on_back if self.state.step_index > 0 else None,
            on_cancel=on_cancel,
            on_keep=on_keep if (self.state.mode == "edit" and prior_answer) else None,
            on_question_pick=on_pick if picker_options else None,
            question_options=picker_options if picker_options else None,
            current_question_id=q.id,
        )

        assert self.channel is not None
        await self.channel.send(embed=prompt_embed, view=view)

        capture_task = asyncio.create_task(self._capture_one_message(1024))
        try:
            action = await self._action_q.get()
        finally:
            if not capture_task.done():
                capture_task.cancel()
        return action

    async def _build_picker_options(self, slot: int) -> list[tuple[int, str]]:
        """Return up to 25 (question_id, prompt) pairs for the picker —
        always includes the current slot's question, plus other active
        questions not currently assigned to other slots."""

        def _load() -> list[BioQuestion]:
            with self.cog.ctx.open_db() as conn:
                return bios_db.list_active_questions(conn, self.member.guild.id)

        pool = await asyncio.to_thread(_load)
        current = self.state.slots[slot]
        used_elsewhere = {
            self.state.slots[i].id for i in range(len(self.state.slots)) if i != slot
        }
        seen: set[int] = set()
        opts: list[tuple[int, str]] = []
        if current.id != 0:
            opts.append((current.id, current.prompt))
            seen.add(current.id)
        for cand in pool:
            if cand.id in used_elsewhere or cand.id in seen:
                continue
            opts.append((cand.id, cand.prompt))
            seen.add(cand.id)
            if len(opts) >= 25:
                break
        return opts if len(opts) > 1 else []

    async def _reroll_unavailable_note(self) -> str | None:
        def _load() -> list[BioQuestion]:
            with self.cog.ctx.open_db() as conn:
                return bios_db.list_active_questions(conn, self.member.guild.id)

        pool = await asyncio.to_thread(_load)
        excluded = {q.id for q in self.state.slots}
        alternatives = [q for q in pool if q.id not in excluded]
        if not alternatives:
            return "No other questions available right now."
        return None

    async def _capture_one_message(self, max_len: int) -> None:
        """Wait for one text message in the wizard channel from the owner.

        On capture: pushes an "answer" Action onto the queue. Validates
        ``max_len`` (re-prompts on overflow). Cancelled when a control
        button preempts the step.
        """
        assert self.channel is not None
        while True:
            try:
                msg: discord.Message = await self.cog.bot.wait_for(
                    "message",
                    check=(
                        lambda m: m.author.id == self.member.id
                        and m.channel.id == (self.channel.id if self.channel else 0)
                    ),
                    timeout=self.config.wizard_timeout_minutes * 60 + 30,
                )
            except asyncio.TimeoutError:
                return
            except asyncio.CancelledError:
                raise
            content = msg.content
            if len(content) > max_len:
                try:
                    await self.channel.send(
                        f"That's {len(content)} chars; please keep it under {max_len}."
                    )
                except discord.HTTPException:
                    pass
                continue
            self.push_action(Action(kind="answer", value=content))
            return

    async def _apply_action_to_state(self, action: Action) -> None:
        kind = self.state.step_kind()
        if kind == "field":
            f = self.state.current_field()
            assert f is not None
            if action.kind == "answer":
                self.state.field_values[f.id] = action.value
                self.state.field_skipped.discard(f.id)
                self.state.step_index += 1
            elif action.kind == "keep":
                prior = self.prior_field_values.get(f.id, "")
                self.state.field_values[f.id] = prior
                self.state.field_skipped.discard(f.id)
                self.state.step_index += 1
            elif action.kind == "skip":
                self.state.field_values.pop(f.id, None)
                self.state.field_skipped.add(f.id)
                self.state.step_index += 1
            elif action.kind == "back":
                self.state.step_index = max(0, self.state.step_index - 1)
        elif kind == "question":
            slot = self.state.current_slot_index()
            assert slot is not None
            if action.kind == "answer":
                self.state.slot_answers[slot] = action.value
                self.state.slot_skipped.discard(slot)
                self.state.step_index += 1
            elif action.kind == "keep":
                _, _, prior = self.prior_slot_answers.get(slot, (0, "", ""))
                self.state.slot_answers[slot] = prior
                self.state.slot_skipped.discard(slot)
                self.state.step_index += 1
            elif action.kind == "skip":
                self.state.slot_answers.pop(slot, None)
                self.state.slot_skipped.add(slot)
                self.state.step_index += 1
            elif action.kind == "back":
                self.state.step_index = max(0, self.state.step_index - 1)
            elif action.kind == "pick_question":
                try:
                    picked_qid = int(action.value)
                except (TypeError, ValueError):
                    return
                await self._swap_slot_to(slot, picked_qid)
                # don't advance — re-render with the new question

    async def _swap_slot_to(self, slot: int, qid: int) -> None:
        """Replace the slot's question with the one the user picked."""
        if self.state.slots[slot].id == qid:
            return

        def _load() -> BioQuestion | None:
            with self.cog.ctx.open_db() as conn:
                return bios_db.get_question(conn, qid)

        picked = await asyncio.to_thread(_load)
        if picked is None:
            return
        self.state.slots[slot] = picked
        self.snapshotted_question_texts[picked.id] = picked.prompt
        # The question changed, so any stored answer for this slot
        # belongs to a different question — drop it.
        self.state.slot_answers.pop(slot, None)
        self.prior_slot_answers.pop(slot, None)

    # ── Timeout & completion ─────────────────────────────────────────

    async def _handle_timeout(self) -> None:
        assert self.channel is not None
        try:
            await self.channel.send("⏰ Timed out — cancelling. No changes saved.")
        except discord.HTTPException:
            pass
        try:
            await self.member.send(
                "Your bio session timed out. You can run /bio again any time."
            )
        except (discord.Forbidden, discord.HTTPException):
            pass
        await self.cancel("timeout")

    async def _complete(self) -> None:
        assert self.channel is not None
        guild = self.member.guild
        bios_channel = guild.get_channel(self.config.bios_channel_id)
        if not isinstance(bios_channel, discord.TextChannel):
            try:
                await self.channel.send(
                    "❌ Bios channel is no longer available — ask an admin to fix the config."
                )
            except discord.HTTPException:
                pass
            await self.cancel("missing_channel")
            return

        payload = self._build_render_payload()
        embed = build_bio_embed(payload)

        # New vs edit dispatch
        existing = await asyncio.to_thread(
            self._load_existing_bio_sync, guild.id, self.member.id
        )

        posted_msg: discord.Message | None = None
        if existing is None:
            try:
                posted_msg = await bios_channel.send(embed=embed)
            except discord.HTTPException:
                log.exception("Failed to post new bio for %d", self.member.id)
                await self.cancel("post_failed")
                return
        else:
            try:
                old_msg = await bios_channel.fetch_message(existing.message_id)
                await old_msg.edit(embed=embed)
                posted_msg = old_msg
            except discord.NotFound:
                try:
                    posted_msg = await bios_channel.send(embed=embed)
                except discord.HTTPException:
                    log.exception("Failed to re-post bio for %d", self.member.id)
                    await self.cancel("post_failed")
                    return
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Failed to edit bio for %d", self.member.id)
                await self.cancel("edit_failed")
                return

        assert posted_msg is not None
        try:
            await asyncio.to_thread(
                self._persist_sync,
                guild_id=guild.id,
                user_id=self.member.id,
                message_id=posted_msg.id,
                channel_id=bios_channel.id,
            )
        except Exception:
            log.exception("Persist failed for bio %d", self.member.id)
            try:
                if existing is None:
                    await posted_msg.delete()
            except discord.HTTPException:
                pass
            await self.cancel("persist_failed")
            return

        try:
            await self.channel.send(
                f"✅ Your bio is up: {posted_msg.jump_url}\n"
                f"This channel will self-destruct in {self.config.archive_grace_seconds}s."
            )
        except discord.HTTPException:
            pass

        await asyncio.sleep(self.config.archive_grace_seconds)
        await self.cancel("complete")

    def _load_existing_bio_sync(
        self, guild_id: int, user_id: int
    ) -> bios_db.StoredBio | None:
        with self.cog.ctx.open_db() as conn:
            return bios_db.get_user_bio(conn, guild_id, user_id)

    def _persist_sync(
        self,
        *,
        guild_id: int,
        user_id: int,
        message_id: int,
        channel_id: int,
    ) -> None:
        field_rows: list[tuple[int, str, str]] = []
        for f in self.state.fields:
            if f.id in self.state.field_skipped:
                continue
            value = self.state.field_values.get(f.id, "")
            if not value and not f.required:
                continue
            field_rows.append((f.id, f.label, value))
        answer_rows: list[tuple[int, int, str, str]] = []
        for slot, q in enumerate(self.state.slots):
            if slot in self.state.slot_skipped:
                continue
            answer = self.state.slot_answers.get(slot, "")
            if not answer:
                continue
            qtext = self.snapshotted_question_texts.get(q.id) or q.prompt
            answer_rows.append((slot, q.id, qtext, answer))
        with self.cog.ctx.open_db() as conn:
            bios_db.upsert_bio(
                conn,
                guild_id=guild_id,
                user_id=user_id,
                message_id=message_id,
                channel_id=channel_id,
                field_rows=field_rows,
                answer_rows=answer_rows,
            )

    # ── Embed builders for the in-wizard prompts ─────────────────────

    def _build_intro_embed(self) -> discord.Embed:
        mode_text = "Update your bio" if self.state.mode == "edit" else "Create your bio"
        e = discord.Embed(
            title=f"📝 {mode_text}",
            description=(
                f"Hi {self.member.display_name}! I'll walk you through "
                f"{len(self.state.fields)} profile field"
                f"{'s' if len(self.state.fields) != 1 else ''} and "
                f"{len(self.state.slots)} icebreaker question"
                f"{'s' if len(self.state.slots) != 1 else ''}.\n\n"
                "Reply with your answer for text fields. Use the buttons to "
                "**Skip**, go **Back**, **Cancel**, or "
                f"{'**Keep** what you had before' if self.state.mode == 'edit' else 'pick from choices'}."
            ),
            color=self.config.embed_color,
        )
        e.set_footer(
            text="This channel disappears when you finish or cancel."
        )
        return e

    def _build_field_prompt_embed(self, f: BioField, prior: str) -> discord.Embed:
        progress = f"Step {self.state.step_index + 1} / {self.state.total_steps}"
        e = discord.Embed(
            title=f.label,
            color=self.config.embed_color,
        )
        if self.state.mode == "edit" and prior:
            e.add_field(name="Current", value=prior[:1024], inline=False)
        if f.field_type == "paragraph":
            e.description = "Reply with a longer answer."
        elif f.field_type == "choice":
            e.description = "Pick one below."
        else:
            e.description = "Reply with a short answer."
        if not f.required:
            e.add_field(name="​", value="*(optional — Skip is allowed)*", inline=False)
        e.set_footer(text=progress)
        return e

    def _build_question_prompt_embed(
        self, q: BioQuestion, prior_answer: str
    ) -> discord.Embed:
        progress = f"Step {self.state.step_index + 1} / {self.state.total_steps}"
        e = discord.Embed(
            title=f"› {q.prompt}",
            description="Reply with your answer.",
            color=self.config.embed_color,
        )
        if self.state.mode == "edit" and prior_answer:
            e.add_field(name="Current", value=prior_answer[:1024], inline=False)
        e.add_field(
            name="​",
            value="*Pick a different icebreaker from the dropdown below, or Skip to omit this one.*",
            inline=False,
        )
        e.set_footer(text=progress)
        return e

    def _build_render_payload(self) -> BioRenderPayload:
        field_snaps: list[FieldSnapshot] = []
        answers_by_id: dict[int, str] = {}
        for f in self.state.fields:
            if f.id in self.state.field_skipped:
                field_snaps.append(
                    FieldSnapshot(
                        label=f.label, value="", field_type=f.field_type, skipped=True
                    )
                )
                continue
            value = self.state.field_values.get(f.id, "")
            answers_by_id[f.id] = value
            field_snaps.append(
                FieldSnapshot(
                    label=f.label, value=value, field_type=f.field_type, skipped=not value
                )
            )

        title, _ = headline_value(self.state.fields, answers_by_id)

        q_snaps: list[QuestionSnapshot] = []
        for slot, q in enumerate(self.state.slots):
            if slot in self.state.slot_skipped:
                q_snaps.append(QuestionSnapshot(question_text=q.prompt, answer="", skipped=True))
                continue
            ans = self.state.slot_answers.get(slot, "")
            q_snaps.append(
                QuestionSnapshot(
                    question_text=self.snapshotted_question_texts.get(q.id, q.prompt),
                    answer=ans,
                    skipped=not ans,
                )
            )

        return BioRenderPayload(
            display_name=self.member.display_name,
            avatar_url=self.member.display_avatar.url,
            headline_value=title,
            fields=tuple(field_snaps),
            questions=tuple(q_snaps),
            embed_color=self.config.embed_color,
            created_at_iso=datetime.now(timezone.utc).isoformat(),
        )


# ── Construction helper ──────────────────────────────────────────────


async def build_session(
    cog: "BiosCog",
    member: discord.Member,
    config: BiosConfig,
) -> WizardSession:
    """Load template + question pool + prior bio, then construct a session."""
    guild_id = member.guild.id

    def _load() -> tuple[
        list[BioField],
        list[BioQuestion],
        bios_db.StoredBio | None,
    ]:
        with cog.ctx.open_db() as conn:
            tmpl = bios_db.get_template(conn, guild_id)
            fields: list[BioField] = []
            if tmpl is not None:
                fields = bios_db.list_fields(conn, tmpl.id, active_only=True)
            pool = bios_db.list_active_questions(conn, guild_id)
            existing = bios_db.get_user_bio(conn, guild_id, member.id)
        return fields, pool, existing

    fields, pool, existing = await asyncio.to_thread(_load)

    mode: Literal["new", "edit"] = "edit" if existing is not None else "new"
    prior_field_values: dict[int, str] = {}
    prior_slot_answers: dict[int, tuple[int, str, str]] = {}
    snapshotted: dict[int, str] = {}

    if existing is not None:
        for fid, (_, value) in existing.field_values.items():
            prior_field_values[fid] = value
        for slot, (qid, qtext, ans) in existing.answers.items():
            prior_slot_answers[slot] = (qid, qtext, ans)
            snapshotted[qid] = qtext

    # Question slot setup
    if existing is not None and existing.answers:
        # Preserve original slot indices: load the stored question into each
        # slot it occupied, draw fresh for any gap (a previously-skipped slot),
        # and respect the current configured size if it grew/shrunk.
        max_stored = max(existing.answers.keys()) + 1
        slot_count = max(max_stored, config.questions_per_bio)
        slots = [
            BioQuestion(id=0, prompt="", weight=1) for _ in range(slot_count)
        ]
        used_ids: set[int] = set()
        for slot_idx, (qid, qtext, _) in existing.answers.items():
            if slot_idx >= slot_count:
                continue
            slots[slot_idx] = BioQuestion(id=qid, prompt=qtext, weight=1)
            used_ids.add(qid)
        # Draw replacements for any gap (id=0 sentinel means "not filled").
        for slot_idx, q in enumerate(slots):
            if q.id != 0:
                continue
            remaining_pool = [p for p in pool if p.id not in used_ids]
            fresh = draw_weighted(remaining_pool, 1)
            if fresh:
                slots[slot_idx] = fresh[0]
                used_ids.add(fresh[0].id)
                snapshotted[fresh[0].id] = fresh[0].prompt
        # Strip any unfilled trailing slots (pool exhausted).
        slots = [q for q in slots if q.id != 0]
    else:
        slots = draw_weighted(pool, config.questions_per_bio)
        for q in slots:
            snapshotted[q.id] = q.prompt

    state = WizardState(mode=mode, fields=fields, slots=slots)
    return WizardSession(
        cog=cog,
        member=member,
        config=config,
        state=state,
        prior_field_values=prior_field_values,
        prior_slot_answers=prior_slot_answers,
        snapshotted_question_texts=snapshotted,
    )
