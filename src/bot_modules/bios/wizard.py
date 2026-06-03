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
    headline_value,
)
from bot_modules.bios.trigger import reposition_trigger_button
from bot_modules.bios.views import BrowseQuestionsView, CombinedStepView

if TYPE_CHECKING:
    from bot_modules.cogs.bios_cog import BiosCog

log = logging.getLogger("dungeonkeeper.bios.wizard")


# ── Step action types ─────────────────────────────────────────────────


ActionKind = Literal[
    "answer",
    "skip",
    "back",
    "cancel",
    "keep",
    "pick_question",  # value = str(question_id), entered question_answer phase
    "browse_prev",
    "browse_next",
    "browse_done",
    "timeout",
]


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    value: str = ""  # for "answer" steps
    interaction: discord.Interaction | None = None


PAGE_SIZE = 25  # Discord select-menu cap


# ── Session ───────────────────────────────────────────────────────────


@dataclass
class WizardSession:
    cog: "BiosCog"
    member: discord.Member
    config: BiosConfig
    state: WizardState
    prior_field_values: dict[int, str] = field(default_factory=dict)

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
        if kind == "question_browse":
            return await self._render_question_browse_step()
        if kind == "question_answer":
            assert self.state.pending_question is not None
            return await self._render_question_answer_step(self.state.pending_question)
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

    async def _render_question_browse_step(self) -> Action:
        """Show the paginated icebreaker pool. The user picks one to answer,
        navigates Prev/Next, or clicks Done."""

        def _load() -> list[BioQuestion]:
            with self.cog.ctx.open_db() as conn:
                return bios_db.list_active_questions(conn, self.member.guild.id)

        pool = await asyncio.to_thread(_load)
        answered_by_id = {q.id: ans for (q, ans) in self.state.question_answers}
        # Show unanswered questions first, then already-answered ones
        # (labeled so the user can re-pick to change their answer).
        unanswered = [q for q in pool if q.id not in answered_by_id]
        answered_pool = [q for q in pool if q.id in answered_by_id]
        # Also include answered questions whose source row was retired
        # (so they're not in `pool`) — the snapshot lives in question_answers.
        retired_ids_in_pool = {q.id for q in pool}
        for q, _ in self.state.question_answers:
            if q.id not in retired_ids_in_pool:
                answered_pool.append(q)
        all_listed = unanswered + answered_pool

        total_pages = max(1, (len(all_listed) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(self.state.browse_page, total_pages - 1))
        self.state.browse_page = page
        start = page * PAGE_SIZE
        page_slice = all_listed[start : start + PAGE_SIZE]

        # Label answered ones with their current answer so the user can
        # spot them and re-pick to change.
        page_options: list[tuple[int, str]] = []
        for q in page_slice:
            if q.id in answered_by_id:
                preview = answered_by_id[q.id].replace("\n", " ")[:40]
                label = f"✏️ {q.prompt} — already: {preview}"
            else:
                label = q.prompt
            page_options.append((q.id, label))

        embed = self._build_browse_embed(
            page=page,
            total_pages=total_pages,
            unanswered_count=len(unanswered),
        )

        async def on_pick(interaction: discord.Interaction, qid: int) -> None:
            await interaction.response.defer()
            self.push_action(Action(kind="pick_question", value=str(qid)))

        async def on_prev(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            self.push_action(Action(kind="browse_prev"))

        async def on_next(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            self.push_action(Action(kind="browse_next"))

        async def on_done(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            self.push_action(Action(kind="browse_done"))

        async def on_back(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            self.push_action(Action(kind="back"))

        async def on_cancel(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            self.push_action(Action(kind="cancel"))

        view = BrowseQuestionsView(
            owner_id=self.member.id,
            page_options=page_options,
            on_pick=on_pick,
            on_prev=on_prev,
            on_next=on_next,
            on_done=on_done,
            on_back=on_back if self.state.fields else None,
            on_cancel=on_cancel,
            can_prev=page > 0,
            can_next=page < total_pages - 1,
            can_done=True,
        )

        assert self.channel is not None
        await self.channel.send(embed=embed, view=view)

        return await self._action_q.get()

    async def _render_question_answer_step(self, q: BioQuestion) -> Action:
        """Render the prompt for the user's picked question and capture their text answer."""
        prompt_embed = self._build_question_prompt_embed(q)

        async def on_back(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            self.push_action(Action(kind="back"))

        async def on_cancel(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            self.push_action(Action(kind="cancel"))

        view = CombinedStepView(
            owner_id=self.member.id,
            on_skip=None,
            on_back=on_back,
            on_cancel=on_cancel,
            on_keep=None,
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
        elif kind == "question_browse":
            if action.kind == "browse_prev":
                self.state.browse_page = max(0, self.state.browse_page - 1)
            elif action.kind == "browse_next":
                self.state.browse_page += 1
            elif action.kind == "browse_done":
                self.state.questions_complete = True
            elif action.kind == "back":
                # Back to the last field for editing.
                if self.state.fields:
                    self.state.step_index = len(self.state.fields) - 1
            elif action.kind == "pick_question":
                try:
                    picked_qid = int(action.value)
                except (TypeError, ValueError):
                    return
                picked = await self._load_question(picked_qid)
                if picked is None:
                    # Could be an answered-but-retired question; reconstruct
                    # from the snapshot inside question_answers.
                    for q, _ in self.state.question_answers:
                        if q.id == picked_qid:
                            picked = q
                            break
                if picked is not None:
                    self.state.pending_question = picked
        elif kind == "question_answer":
            pending = self.state.pending_question
            if pending is None:
                return
            if action.kind == "answer":
                # If the user re-picked an already-answered question,
                # update that entry in place; otherwise append.
                existing_idx = next(
                    (
                        i
                        for i, (q, _) in enumerate(self.state.question_answers)
                        if q.id == pending.id
                    ),
                    None,
                )
                if existing_idx is not None:
                    self.state.question_answers[existing_idx] = (pending, action.value)
                else:
                    self.state.question_answers.append((pending, action.value))
                self.state.pending_question = None
                self.state.browse_page = 0
            elif action.kind == "back":
                # Drop the pending question; return to browse without saving.
                self.state.pending_question = None

    async def _load_question(self, qid: int) -> BioQuestion | None:
        def _load() -> BioQuestion | None:
            with self.cog.ctx.open_db() as conn:
                return bios_db.get_question(conn, qid)

        return await asyncio.to_thread(_load)

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
        is_new_post = existing is None
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
                # The original message was deleted — fall back to a fresh
                # post at the bottom of the channel. That counts as a new
                # post for trigger-button repositioning.
                try:
                    posted_msg = await bios_channel.send(embed=embed)
                    is_new_post = True
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

        # When a new bio embed was posted (or a 404-fallback turned an
        # edit into a fresh post), the trigger button is now above the
        # new embed. Move it back to the bottom so the next member can
        # tap it without scrolling.
        if is_new_post:
            try:
                await reposition_trigger_button(self.cog.ctx, bios_channel)
            except Exception:
                log.exception(
                    "Failed to reposition trigger button in guild %d",
                    guild.id,
                )

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
        for slot, (q, answer) in enumerate(self.state.question_answers):
            if not answer:
                continue
            answer_rows.append((slot, q.id, q.prompt, answer))
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
        n_fields = len(self.state.fields)
        n_q = self.state.target_questions
        e = discord.Embed(
            title=f"📝 {mode_text}",
            description=(
                f"Hi {self.member.display_name}! First I'll walk you through "
                f"{n_fields} profile field{'s' if n_fields != 1 else ''}, "
                f"then you'll pick up to {n_q} icebreaker question"
                f"{'s' if n_q != 1 else ''} to answer.\n\n"
                "Reply with your answer for text fields. Use the buttons to "
                "**Skip**, go **Back**, or **Cancel**."
            ),
            color=self.config.embed_color,
        )
        e.set_footer(text="This channel disappears when you finish or cancel.")
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

    def _build_question_prompt_embed(self, q: BioQuestion) -> discord.Embed:
        e = discord.Embed(
            title=f"› {q.prompt}",
            description="Reply with your answer, or use **Back** to pick a different question.",
            color=self.config.embed_color,
        )
        answered = len(self.state.question_answers)
        e.set_footer(
            text=f"Icebreaker {answered + 1} of up to {self.state.target_questions}"
        )
        return e

    def _build_browse_embed(
        self, *, page: int, total_pages: int, unanswered_count: int
    ) -> discord.Embed:
        answered = len(self.state.question_answers)
        target = self.state.target_questions
        if unanswered_count or answered:
            desc = (
                f"Pick a question to answer from the dropdown below. "
                f"You can answer up to **{target}** ({answered} so far). "
                "Already-answered questions are marked ✏️ — pick one to change your answer. "
                "Click **Done** when you're happy with what you've answered."
            )
        else:
            desc = "No questions configured for this server. Click **Done** to skip icebreakers."
        e = discord.Embed(
            title="🎲 Icebreakers",
            description=desc,
            color=self.config.embed_color,
        )
        if self.state.question_answers:
            lines = []
            for i, (q, ans) in enumerate(self.state.question_answers, start=1):
                preview = ans.replace("\n", " ")[:80]
                lines.append(f"**{i}.** › {q.prompt[:80]} — _{preview}_")
            e.add_field(
                name=f"Answered ({answered})", value="\n".join(lines)[:1024], inline=False
            )
        if total_pages > 1:
            e.set_footer(text=f"Pool page {page + 1} of {total_pages}")
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
        for q, ans in self.state.question_answers:
            q_snaps.append(
                QuestionSnapshot(
                    question_text=q.prompt,
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

    def _load() -> tuple[list[BioField], bios_db.StoredBio | None]:
        with cog.ctx.open_db() as conn:
            tmpl = bios_db.get_template(conn, guild_id)
            fields: list[BioField] = []
            if tmpl is not None:
                fields = bios_db.list_fields(conn, tmpl.id, active_only=True)
            existing = bios_db.get_user_bio(conn, guild_id, member.id)
        return fields, existing

    fields, existing = await asyncio.to_thread(_load)

    mode: Literal["new", "edit"] = "edit" if existing is not None else "new"
    prior_field_values: dict[int, str] = {}
    prior_question_answers: list[tuple[BioQuestion, str]] = []

    if existing is not None:
        for fid, (_, value) in existing.field_values.items():
            prior_field_values[fid] = value
        # Preserve answer order from stored slot indices.
        for slot in sorted(existing.answers.keys()):
            qid, qtext, ans = existing.answers[slot]
            prior_question_answers.append(
                (BioQuestion(id=qid, prompt=qtext, weight=1), ans)
            )

    state = WizardState(
        mode=mode,
        fields=fields,
        target_questions=max(1, config.questions_per_bio),
        # Pre-fill answered questions in edit mode so the user sees their
        # prior picks. They can still change them (the user can answer
        # different questions and the new set replaces the old at save).
        question_answers=list(prior_question_answers),
    )
    return WizardSession(
        cog=cog,
        member=member,
        config=config,
        state=state,
        prior_field_values=prior_field_values,
    )
