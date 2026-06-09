import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import HOW_TO_PLAY
from bot_modules.games.command_groups import play
from bot_modules.games.utils.game_manager import (
    check_allowed_channel,
    check_game_enabled,
    create_game,
    update_game_message,
    update_game_payload,
    get_game_payload,
    modify_payload,
    end_game,
    update_session,
    ConfirmCloseView,
)
from bot_modules.games.utils.audit import send_audit_log
from bot_modules.games.utils.ai_client import generate_text
from bot_modules.games_ama.embeds import (
    build_answered_embed,
    build_asker_dm_embed,
    build_idle_ai_question_embed,
    build_lobby_embed,
    build_main_embed,
    build_question_embed,
    build_recap_embed,
)
from bot_modules.games_ama.logic import (
    add_question,
    bottom_bar_label,
    build_question_entry,
    compute_recap_stats,
    first_content_line,
    is_resolved_status,
    mark_question_answered,
    mark_question_approved,
    mark_question_expired,
    mark_question_message,
    mark_question_passed,
    mark_question_rejected,
    parse_iso_ts,
    recompute_totals,
    should_expire,
    utcnow_iso,
)

log = logging.getLogger(__name__)


# ── Modals ───────────────────────────────────────────────────────────────────


class AskQuestionModal(discord.ui.Modal, title="Your Question"):
    question = discord.ui.TextInput(
        label="Question",
        style=discord.TextStyle.paragraph,
        max_length=500,
        placeholder="Ask anything...",
    )

    def __init__(self, game_id: str, db, channel, mode: str, host_id: int, hot_seat_id: int, ama_view):
        super().__init__()
        self.game_id = game_id
        self.db = db
        self.channel = channel
        self.mode = mode
        self.host_id = host_id
        self.hot_seat_id = hot_seat_id  # captured at button-press time
        self.ama_view = ama_view

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, "Your Question", interaction.channel.name if interaction.channel else "unknown")

        # Guard: game was closed while the modal was open
        if self.ama_view._closed:
            await interaction.response.send_message(
                "⚠️ The game closed while you were typing — your question was not submitted.",
                ephemeral=True,
            )
            return

        # Guard: if hot seat changed between modal open and submit, reject
        if self.ama_view.hot_seat_id != self.hot_seat_id:
            await interaction.response.send_message(
                "⚠️ The hot seat changed while you were typing — please try again.",
                ephemeral=True,
            )
            return

        q_entry = build_question_entry(
            asker_id=interaction.user.id,
            text=self.question.value,
            hot_seat_id=self.hot_seat_id,
        )

        def _add_question(payload):
            add_question(payload, q_entry)

        payload = await modify_payload(self.db, self.game_id, _add_question)
        q_idx = len(payload.get("questions", [])) - 1

        # Audit log
        if interaction.guild:
            await send_audit_log(
                interaction.client, self.db, interaction.guild,
                game_type="ama", user=interaction.user,
                content=self.question.value, label="AMA Question",
            )

        if self.mode == "unfiltered":
            embed = build_question_embed(self.question.value)
            hot_seat_member = interaction.guild.get_member(self.hot_seat_id) if interaction.guild else None
            question_view = QuestionView(self.game_id, self.hot_seat_id, self.db, q_idx, interaction.user.id, self.ama_view, self.question.value)
            question_msg = await self.channel.send(
                content=hot_seat_member.mention if hot_seat_member else None,
                embed=embed,
                view=question_view,
            )

            def _mark_posted(payload):
                mark_question_approved(payload, q_idx, message_id=question_msg.id)

            await modify_payload(self.db, self.game_id, _mark_posted)
            await interaction.response.send_message("Your question has been posted anonymously!", ephemeral=True)
            # Increment turn counter only after the question is actually posted
            await self.ama_view.register_question_asked(self.channel)
        else:
            # Screened — DM the host so the question stays hidden from the channel
            approve_view = ScreenedQuestionView(
                game_id=self.game_id,
                question_text=self.question.value,
                question_idx=q_idx,
                db=self.db,
                channel=self.channel,
                hot_seat_id=self.hot_seat_id,
                asker_id=interaction.user.id,
                ama_view=self.ama_view,
            )
            dm_sent = False
            try:
                host_member = interaction.guild.get_member(self.host_id) if interaction.guild else None
                if host_member:
                    hot_seat_name = (interaction.guild.get_member(self.hot_seat_id).display_name
                                     if interaction.guild.get_member(self.hot_seat_id) else "the hot seat")
                    await host_member.send(
                        f"📨 New screened question for **{hot_seat_name}** (in {self.channel.mention}):",
                        view=approve_view,
                    )
                    dm_sent = True
            except discord.Forbidden:
                pass
            except Exception:
                pass
            if dm_sent:
                await interaction.response.send_message(
                    "✅ Your question has been submitted for host review.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "⚠️ Couldn't reach the host (DMs may be disabled) — your question was not submitted.", ephemeral=True
                )
            # Turn counter for screened mode advances only when the host approves


class ReplyModal(discord.ui.Modal, title="Your Reply"):
    """Modal for the hot-seat player to reply to a question."""

    reply = discord.ui.TextInput(
        label="Reply",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        placeholder="Type your answer...",
    )

    def __init__(self, game_id: str, db, question_idx: int, asker_id: int, ama_view, question_text: str):
        super().__init__()
        self.game_id = game_id
        self.db = db
        self.question_idx = question_idx
        self.asker_id = asker_id
        self.ama_view = ama_view
        self.question_text = question_text

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted reply modal in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")

        answered_embed = build_answered_embed(
            self.question_text,
            self.reply.value,
            interaction.user.display_name,
        )
        await interaction.response.edit_message(embed=answered_embed, view=None)

        # DM the anonymous asker so they know their question was answered
        try:
            asker = interaction.guild.get_member(self.asker_id) if interaction.guild else None
            if asker:
                dm_embed = build_asker_dm_embed(interaction.channel.mention)
                await asker.send(embed=dm_embed)
        except discord.Forbidden:
            pass  # DMs disabled
        except Exception as e:
            log.debug("Could not DM asker %s: %s", self.asker_id, e)

        # Mark question as answered in payload — does NOT increment game tallies
        payload = await get_game_payload(self.db, self.game_id)
        mark_question_answered(
            payload,
            self.question_idx,
            message_id=interaction.message.id,
        )
        await update_game_payload(self.db, self.game_id, payload)

        # Update main game embed status bar
        if self.ama_view and hasattr(self.ama_view, "refresh_status"):
            await self.ama_view.refresh_status(interaction.channel)


# ── Views ────────────────────────────────────────────────────────────────────


class ScreenedQuestionView(discord.ui.View):
    def __init__(self, game_id, question_text, question_idx, db, channel, hot_seat_id, asker_id, ama_view):
        super().__init__(timeout=300)
        self.game_id = game_id
        self.question_text = question_text
        self.question_idx = question_idx
        self.db = db
        self.channel = channel
        self.hot_seat_id = hot_seat_id
        self.asker_id = asker_id
        self.ama_view = ama_view

    @discord.ui.button(label="✅ Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if interaction.user.id != self.ama_view.host_id:
            await interaction.response.send_message("Only the host can approve questions.", ephemeral=True)
            return
        embed = build_question_embed(self.question_text)
        hot_seat_member = interaction.guild.get_member(self.hot_seat_id) if interaction.guild else None
        question_view = QuestionView(self.game_id, self.hot_seat_id, self.db, self.question_idx, self.asker_id, self.ama_view, self.question_text)
        question_msg = await self.channel.send(
            content=hot_seat_member.mention if hot_seat_member else None,
            embed=embed,
            view=question_view,
        )
        self.stop()
        await interaction.response.edit_message(content="✅ Question approved.", view=None)

        # Update status in payload
        payload = await get_game_payload(self.db, self.game_id)
        mark_question_approved(
            payload,
            self.question_idx,
            message_id=question_msg.id,
            hot_seat_id=self.hot_seat_id,
            now_iso=utcnow_iso(),
        )
        await update_game_payload(self.db, self.game_id, payload)

        # Advance turn counter now that the question is actually posted
        await self.ama_view.register_question_asked(self.channel)

    @discord.ui.button(label="❌ Reject", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if interaction.user.id != self.ama_view.host_id:
            await interaction.response.send_message("Only the host can reject questions.", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(content="❌ Question rejected.", view=None)

        payload = await get_game_payload(self.db, self.game_id)
        mark_question_rejected(payload, self.question_idx)
        await update_game_payload(self.db, self.game_id, payload)


class QuestionView(discord.ui.View):
    """Shown with each posted question — Reply and Pass buttons for the hot seat player."""

    def __init__(self, game_id, hot_seat_id, db, question_idx, asker_id, ama_view=None, question_text: str = ""):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.hot_seat_id = hot_seat_id
        self.db = db
        self.question_idx = question_idx
        self.asker_id = asker_id
        self.ama_view = ama_view
        self.question_text = question_text

    @discord.ui.button(label="💬 Reply", style=discord.ButtonStyle.primary, custom_id="ama_reply")
    async def reply_question(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if interaction.user.id != self.hot_seat_id:
            await interaction.response.send_message("Only the hot seat player can reply.", ephemeral=True)
            return
        modal = ReplyModal(self.game_id, self.db, self.question_idx, self.asker_id, self.ama_view, self.question_text)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Pass", style=discord.ButtonStyle.secondary, custom_id="ama_pass")
    async def pass_question(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if interaction.user.id != self.hot_seat_id:
            await interaction.response.send_message("Only the hot seat player can pass.", ephemeral=True)
            return
        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed:
            embed.set_footer(text="⏩ Passed")
        await interaction.response.edit_message(embed=embed, view=None)

        payload = await get_game_payload(self.db, self.game_id)
        mark_question_passed(
            payload,
            self.question_idx,
            message_id=interaction.message.id,
        )
        await update_game_payload(self.db, self.game_id, payload)

        # Update main game embed status bar
        if self.ama_view and hasattr(self.ama_view, "refresh_status"):
            await self.ama_view.refresh_status(interaction.channel)


class AMAView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, mode: str, db, bot):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.mode = mode
        self.db = db
        self.bot = bot
        self.hot_seat_id: int | None = None
        self._game_msg: discord.Message | None = None
        self._hot_seat_name: str | None = None
        self.queue: list[int] = []          # user IDs waiting for hot seat
        self.questions_this_turn: int = 0   # answered questions for current hot seat
        self._suppress_resend: bool = False # suppress bottom-bar resend during system messages
        self._closed: bool = False          # True once close has been confirmed; blocks new questions
        self._ping_subscribers: set[int] = set()  # users who want pings on new hot seat
        self._hot_seat_timer_task: asyncio.Task | None = None  # 1-hour auto-rotate timer
        self._idle_ai_question_task: asyncio.Task | None = None  # 15-min AI fallback timer

    def is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild:
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    def _build_embed(self, host_name: str, payload: dict | None = None) -> discord.Embed:
        guild = self._game_msg.guild if self._game_msg else None

        def _name_resolver(uid: int) -> str:
            m = guild.get_member(uid) if guild else None
            return m.display_name if m else str(uid)

        return build_main_embed(
            host_name=host_name,
            mode=self.mode,
            hot_seat_name=self._hot_seat_name,
            questions_this_turn=self.questions_this_turn,
            queue=list(self.queue),
            name_resolver=_name_resolver,
            payload=payload,
        )

    async def refresh_status(self, channel):
        """Re-fetch payload and update the main game message with current stats."""
        if self._game_msg is None:
            return
        try:
            payload = await get_game_payload(self.db, self.game_id)
            host_member = channel.guild.get_member(self.host_id) if channel.guild else None
            embed = self._build_embed(
                host_member.display_name if host_member else "Host",
                payload=payload,
            )
            await self._game_msg.edit(embed=embed, view=self)
        except Exception as e:
            log.debug("Failed to refresh AMA status bar: %s", e)

    def _start_hot_seat_timer(self, channel):
        """Start (or restart) the 1-hour auto-rotate timer for the current hot seat."""
        if self._hot_seat_timer_task and not self._hot_seat_timer_task.done():
            self._hot_seat_timer_task.cancel()

        async def _timeout():
            await asyncio.sleep(3600)
            if self._closed or self.hot_seat_id is None:
                return
            try:
                await channel.send("⏰ Hot seat timed out after 1 hour — rotating to next player.")
                self.questions_this_turn = 4
                await self.check_turn_rotation(channel)
            except Exception as e:
                log.debug("AMA timeout handler error: %s", e)

        self._hot_seat_timer_task = asyncio.create_task(_timeout())

    def _start_idle_question_timer(self, channel):
        """Start (or restart) the 15-minute idle timer for AI fallback questions."""
        current_task = asyncio.current_task()
        if (
            self._idle_ai_question_task
            and not self._idle_ai_question_task.done()
            and self._idle_ai_question_task is not current_task
        ):
            self._idle_ai_question_task.cancel()

        hot_seat_snapshot = self.hot_seat_id
        question_count_snapshot = self.questions_this_turn

        async def _timeout():
            await asyncio.sleep(900)
            if self._closed or self.hot_seat_id is None:
                return
            if self.hot_seat_id != hot_seat_snapshot:
                return
            if self.questions_this_turn != question_count_snapshot:
                return
            try:
                await self._post_idle_ai_question(channel)
            except Exception as e:
                log.debug("AMA idle AI question error: %s", e)
                self._start_idle_question_timer(channel)

        self._idle_ai_question_task = asyncio.create_task(_timeout())

    async def _generate_idle_ai_question(self) -> str | None:
        hot_seat_name = self._hot_seat_name or "the hot seat player"
        system_prompt = (
            "You create one anonymous AMA question for a Discord party game. "
            "Keep it short, engaging, and answerable in chat. "
            "Return only one question and nothing else."
        )
        user_prompt = (
            f"Target player: {hot_seat_name}\n"
            "Generate exactly one question under 180 characters."
        )
        text = await generate_text(system_prompt, user_prompt, max_tokens=80)
        if not text:
            return None
        return first_content_line(text)

    async def _post_idle_ai_question(self, channel):
        if self._closed or self.hot_seat_id is None:
            return

        question_text = await self._generate_idle_ai_question()
        if not question_text:
            self._start_idle_question_timer(channel)
            return

        q_entry = build_question_entry(
            asker_id=0,
            text=question_text,
            hot_seat_id=self.hot_seat_id,
            status="approved",
            source="ai_idle",
        )

        def _add_question(payload):
            add_question(payload, q_entry)

        payload = await modify_payload(self.db, self.game_id, _add_question)
        q_idx = len(payload.get("questions", [])) - 1

        hot_seat_member = channel.guild.get_member(self.hot_seat_id) if channel.guild else None
        question_view = QuestionView(self.game_id, self.hot_seat_id, self.db, q_idx, 0, self, question_text)
        embed = build_idle_ai_question_embed(question_text)
        await channel.send("No player question arrived in 15 minutes, so here's an anonymous AI question.")
        question_msg = await channel.send(
            content=hot_seat_member.mention if hot_seat_member else None,
            embed=embed,
            view=question_view,
        )

        def _mark_posted(payload):
            mark_question_message(payload, q_idx, question_msg.id)

        await modify_payload(self.db, self.game_id, _mark_posted)

        await self.register_question_asked(channel)

    @staticmethod
    def _ama_role_mention(guild: discord.Guild | None) -> str | None:
        if guild is None:
            return None
        role = discord.utils.find(lambda r: r.name and r.name.lower() == "ama", guild.roles)
        return role.mention if role else None

    async def register_question_asked(self, channel):
        """Advance turn counters after a question has been posted."""
        self.questions_this_turn += 1
        await self.refresh_status(channel)
        await self.check_turn_rotation(channel)
        if not self._closed and self.hot_seat_id is not None and self.questions_this_turn < 4:
            self._start_idle_question_timer(channel)

    async def _set_hot_seat(self, member: discord.Member, channel, announce: bool = True):
        """Put a member in the hot seat and reset the turn counter."""
        self.hot_seat_id = member.id
        self._hot_seat_name = member.display_name
        self.questions_this_turn = 0

        payload = await get_game_payload(self.db, self.game_id)
        payload["hot_seat_id"] = member.id
        payload["hot_seat_rotations"] = payload.get("hot_seat_rotations", 0) + 1
        await update_game_payload(self.db, self.game_id, payload)

        self._start_hot_seat_timer(channel)
        self._start_idle_question_timer(channel)

        self._suppress_resend = True
        try:
            await self.refresh_status(channel)
            await self._update_bottom_bar()
        finally:
            self._suppress_resend = False

        if announce:
            # Notify AMA role + opt-in users when hot seat changes.
            mentions: list[str] = []
            role_mention = self._ama_role_mention(channel.guild if channel else None)
            if role_mention:
                mentions.append(role_mention)
            mentions.extend(
                f"<@{uid}>" for uid in self._ping_subscribers if uid != member.id
            )
            mention_prefix = f"{' '.join(mentions)} " if mentions else ""
            await channel.send(f"{mention_prefix}A new host: {member.mention} is in the hot seat!")

    async def _update_bottom_bar(self):
        """Update bottom bar text with current hot seat and queue info."""
        if not getattr(self, '_bottom_msg', None):
            return
        label = bottom_bar_label(self._hot_seat_name, len(self.queue))
        try:
            await self._bottom_msg.edit(content=label)
        except Exception:
            pass

    async def check_turn_rotation(self, channel):
        """Check if the current hot seat has hit 4 answered questions — rotate if so."""
        if self.questions_this_turn < 4:
            return

        current_task = asyncio.current_task()
        if (
            self._idle_ai_question_task
            and not self._idle_ai_question_task.done()
            and self._idle_ai_question_task is not current_task
        ):
            self._idle_ai_question_task.cancel()

        self._suppress_resend = True
        try:
            if self._closed:
                # Game is closing — end immediately regardless of queue
                await self._do_close(channel)
                return

            if not self.queue:
                # No one queued — announce turn is done, seat opens up
                if self._hot_seat_timer_task and not self._hot_seat_timer_task.done():
                    self._hot_seat_timer_task.cancel()
                self.hot_seat_id = None
                self._hot_seat_name = None
                self.questions_this_turn = 0

                payload = await get_game_payload(self.db, self.game_id)
                payload["hot_seat_id"] = None
                await update_game_payload(self.db, self.game_id, payload)

                await channel.send("🎙️ Hot seat turn complete! Volunteer to take the next turn.")
                await self.refresh_status(channel)
                await self._update_bottom_bar()
                return

            # Pop next from queue and set them in the hot seat
            next_id = self.queue.pop(0)
            guild = channel.guild if channel else None
            member = guild.get_member(next_id) if guild else None
            if member:
                # _set_hot_seat also sets _suppress_resend, but we're already suppressed
                await self._set_hot_seat(member, channel, announce=True)
            else:
                # Member left — try next in queue recursively
                await self.check_turn_rotation(channel)
        finally:
            self._suppress_resend = False

    @discord.ui.button(label="Volunteer for Hot Seat", style=discord.ButtonStyle.primary, custom_id="ama_volunteer")
    async def volunteer(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if self._closed:
            await interaction.response.send_message("This game is closing — no new volunteers.", ephemeral=True)
            return
        user_id = interaction.user.id

        # Already in the hot seat
        if user_id == self.hot_seat_id:
            await interaction.response.send_message("You're already in the hot seat!", ephemeral=True)
            return

        # Already in queue
        if user_id in self.queue:
            self.queue.remove(user_id)
            await interaction.response.send_message("You've left the queue.", ephemeral=True)
            await self.refresh_status(interaction.channel)
            await self._update_bottom_bar()
            return

        # No one in hot seat — go straight in
        if self.hot_seat_id is None:
            await interaction.response.defer()
            await self._set_hot_seat(interaction.user, interaction.channel, announce=True)
            return

        # Seat is taken — add to queue
        self.queue.append(user_id)
        pos = len(self.queue)
        await interaction.response.send_message(
            f"You're #{pos} in the queue. You'll be notified when it's your turn!",
            ephemeral=True,
        )
        await self.refresh_status(interaction.channel)
        await self._update_bottom_bar()

    @discord.ui.button(label="Ask a Question", style=discord.ButtonStyle.secondary, custom_id="ama_ask")
    async def ask_question(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if self._closed:
            await interaction.response.send_message("This game is closing — no new questions.", ephemeral=True)
            return
        if self.hot_seat_id is None:
            await interaction.response.send_message("No one is in the hot seat yet!", ephemeral=True)
            return
        modal = AskQuestionModal(
            self.game_id, self.db, interaction.channel, self.mode, self.host_id, self.hot_seat_id, self
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary, custom_id="ama_skip", row=1)
    async def skip_hot_seat(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can skip.", ephemeral=True)
            return
        if self.hot_seat_id is None:
            await interaction.response.send_message("No one is in the hot seat.", ephemeral=True)
            return
        await interaction.response.defer()
        # Force rotation by maxing out the turn counter
        self.questions_this_turn = 4
        await self.check_turn_rotation(interaction.channel)

    @discord.ui.button(label="🔄 New Hot Seat", style=discord.ButtonStyle.secondary, custom_id="ama_new_hs", row=1)
    async def new_hot_seat(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can select the hot seat.", ephemeral=True)
            return
        select = HotSeatSelect(self.game_id, self.db, self)
        view = discord.ui.View(timeout=60)
        view.add_item(select)
        await interaction.response.send_message("Select the new hot seat:", view=view, ephemeral=True)

    @discord.ui.button(label="🛑 Close Game", style=discord.ButtonStyle.danger, custom_id="ama_close", row=1)
    async def close_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can close.", ephemeral=True)
            return
        if self._closed:
            await interaction.response.send_message("The game is already closing.", ephemeral=True)
            return
        channel = interaction.channel

        async def _confirmed(_confirm_interaction):
            # Mark closed immediately so no new questions slip in
            # (ConfirmCloseView already responded to the interaction before calling us)
            self._closed = True

            if self.hot_seat_id is None:
                # Nobody in the hot seat — close right away
                await self._do_close(channel=channel)
            else:
                # Let the current hot seat finish their turn, then auto-close
                await channel.send(
                    "🛑 Game is closing after this turn — no new questions or volunteers accepted.",
                    delete_after=30,
                )
                await self.refresh_status(channel)

        view = ConfirmCloseView(_confirmed)
        await interaction.response.send_message("⚠️ Are you sure you want to end this game?", view=view, ephemeral=True)

    @discord.ui.button(label="❓ How to Play", style=discord.ButtonStyle.secondary, custom_id="ama_htp", row=1)
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        await interaction.response.send_message(HOW_TO_PLAY["ama"], ephemeral=True)

    async def _do_close(self, channel):
        self._closed = True
        if self._hot_seat_timer_task and not self._hot_seat_timer_task.done():
            self._hot_seat_timer_task.cancel()
        current_task = asyncio.current_task()
        if (
            self._idle_ai_question_task
            and not self._idle_ai_question_task.done()
            and self._idle_ai_question_task is not current_task
        ):
            self._idle_ai_question_task.cancel()

        # Remove the bottom bar immediately when the game closes.
        cog = self.bot.get_cog("AMACog")
        if cog and hasattr(cog, "cleanup_ended_game"):
            await cog.cleanup_ended_game(channel.id, self.game_id, channel=channel)

        payload = await get_game_payload(self.db, self.game_id)
        stats = compute_recap_stats(payload)
        total_q = stats["total_q"]
        unique_askers = stats["unique_askers"]

        embed = build_recap_embed(self.mode, stats)

        self.stop()
        for item in self.children:
            item.disabled = True

        if self._game_msg:
            try:
                await self._game_msg.edit(view=self)
            except Exception:
                pass
        await channel.send(embed=embed)

        log.info("Game %s ended — %d questions asked", self.game_id, total_q)
        await end_game(self.db, self.game_id, player_count=unique_askers, round_count=total_q, payload=payload)
        if self.game_id in self.bot.active_views:
            del self.bot.active_views[self.game_id]


class HotSeatSelect(discord.ui.UserSelect):
    def __init__(self, game_id: str, db, ama_view: AMAView):
        super().__init__(placeholder="Select new hot seat", min_values=1, max_values=1)
        self.game_id = game_id
        self.db = db
        self.ama_view = ama_view

    async def callback(self, interaction: discord.Interaction):
        new_member = self.values[0]
        # Remove from queue if they were queued
        if new_member.id in self.ama_view.queue:
            self.ama_view.queue.remove(new_member.id)

        await interaction.response.defer()
        await interaction.edit_original_response(content="Hot seat updated.", view=None)
        await self.ama_view._set_hot_seat(new_member, interaction.channel, announce=True)


class AMABottomView(discord.ui.View):
    """Persistent one-line bar at the bottom of chat for AMA."""

    def __init__(self, game_id: str, db, ama_view: AMAView, game_msg_url: str):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.db = db
        self.ama_view = ama_view
        self.message_id: int | None = None
        self.add_item(discord.ui.Button(
            label="Jump to Answers",
            style=discord.ButtonStyle.link,
            url=game_msg_url,
        ))

    @discord.ui.button(label="Ask Question", style=discord.ButtonStyle.primary, custom_id="ama_bottom_ask")
    async def ask_question(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' (bottom bar) in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if self.ama_view._closed:
            await interaction.response.send_message("This game is closing — no new questions.", ephemeral=True)
            return
        if self.ama_view.hot_seat_id is None:
            await interaction.response.send_message("No one is in the hot seat yet!", ephemeral=True)
            return
        modal = AskQuestionModal(
            self.game_id, self.db, interaction.channel, self.ama_view.mode,
            self.ama_view.host_id, self.ama_view.hot_seat_id, self.ama_view,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="🔔 Notify Me", style=discord.ButtonStyle.secondary, custom_id="ama_notify_toggle")
    async def toggle_notify(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' (bottom bar) in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        user_id = interaction.user.id
        if user_id in self.ama_view._ping_subscribers:
            self.ama_view._ping_subscribers.discard(user_id)
            await interaction.response.send_message("🔕 You'll no longer be pinged when the hot seat changes.", ephemeral=True)
        else:
            self.ama_view._ping_subscribers.add(user_id)
            await interaction.response.send_message("🔔 You'll be pinged whenever the hot seat changes!", ephemeral=True)

    @discord.ui.button(label="🙋 Volunteer", style=discord.ButtonStyle.success, custom_id="ama_bottom_volunteer")
    async def volunteer(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' (bottom bar) in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if self.ama_view._closed:
            await interaction.response.send_message("This game is closing — no new volunteers.", ephemeral=True)
            return
        user_id = interaction.user.id

        if user_id == self.ama_view.hot_seat_id:
            await interaction.response.send_message("You're already in the hot seat!", ephemeral=True)
            return

        if user_id in self.ama_view.queue:
            self.ama_view.queue.remove(user_id)
            await interaction.response.send_message("You've left the queue.", ephemeral=True)
            await self.ama_view.refresh_status(interaction.channel)
            await self.ama_view._update_bottom_bar()
            return

        if self.ama_view.hot_seat_id is None:
            await interaction.response.defer()
            await self.ama_view._set_hot_seat(interaction.user, interaction.channel, announce=True)
            return

        self.ama_view.queue.append(user_id)
        pos = len(self.ama_view.queue)
        await interaction.response.send_message(
            f"You're #{pos} in the queue. You'll be notified when it's your turn!",
            ephemeral=True,
        )
        await self.ama_view.refresh_status(interaction.channel)
        await self.ama_view._update_bottom_bar()


async def _resend_ama_bottom(bot, game_id: str, channel):
    """Delete and re-send the AMA bottom bar so it stays at the bottom."""
    ama_view = bot.active_views.get(game_id)
    bottom_view = bot.active_views.get(f"{game_id}_bottom")
    if not ama_view or not bottom_view or not getattr(ama_view, '_bottom_msg', None):
        return

    ama_view._suppress_resend = True
    try:
        try:
            await ama_view._bottom_msg.delete()
        except Exception:
            pass

        hot_seat_name = getattr(ama_view, '_hot_seat_name', None)
        label = bottom_bar_label(hot_seat_name, len(ama_view.queue))
        new_msg = await channel.send(content=label, view=bottom_view)
        ama_view._bottom_msg = new_msg
        if hasattr(bottom_view, "message_id"):
            bottom_view.message_id = new_msg.id
    finally:
        ama_view._suppress_resend = False


class AMACog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._active_channels: dict[int, str] = {}
        self._question_views_rehydrated: bool = False
        self._question_maintenance_task: asyncio.Task | None = None
        self._resend_tasks: dict[int, asyncio.Task] = {}  # channel_id → pending debounce task

    @property
    def db(self):
        return self.bot.games_db

    def cog_unload(self):
        if self._question_maintenance_task and not self._question_maintenance_task.done():
            self._question_maintenance_task.cancel()
        for task in self._resend_tasks.values():
            task.cancel()
        self._resend_tasks.clear()

    async def _resolve_channel(self, channel_id: int):
        channel = self.bot.get_channel(channel_id)
        if channel:
            return channel
        try:
            return await self.bot.fetch_channel(channel_id)
        except Exception:
            return None

    async def cleanup_ended_game(self, channel_id: int, game_id: str, channel=None):
        """Remove AMA tracking + bottom bar artifacts for a finished game."""
        if channel is None:
            channel = await self._resolve_channel(channel_id)

        ama_view = self.bot.active_views.get(game_id)
        bottom_view = self.bot.active_views.get(f"{game_id}_bottom")
        deleted = False

        if ama_view:
            try:
                ama_view.stop()
            except Exception:
                pass

        if bottom_view:
            try:
                bottom_view.stop()
                for item in bottom_view.children:
                    item.disabled = True
            except Exception:
                pass

        if channel and ama_view and getattr(ama_view, "_bottom_msg", None):
            try:
                await ama_view._bottom_msg.delete()
                deleted = True
            except Exception:
                pass

        if channel and (not deleted) and bottom_view and getattr(bottom_view, "message_id", None):
            try:
                msg = await channel.fetch_message(int(bottom_view.message_id))
                await msg.delete()
                deleted = True
            except Exception:
                pass

        # Last resort: edit the bottom bar to show disabled buttons
        if channel and (not deleted) and bottom_view:
            bottom_msg = getattr(ama_view, "_bottom_msg", None) if ama_view else None
            if bottom_msg:
                try:
                    await bottom_msg.edit(content="🛑 AMA ended", view=bottom_view)
                except Exception:
                    pass

        self.bot.active_views.pop(f"{game_id}_bottom", None)
        self.bot.active_views.pop(game_id, None)
        self._active_channels.pop(channel_id, None)

    async def _prune_question_message_view(self, channel, message_id: int, footer_text: str | None = None) -> bool:
        try:
            msg = await channel.fetch_message(int(message_id))
        except Exception:
            return False

        embed = msg.embeds[0] if msg.embeds else None
        if embed and footer_text:
            embed.set_footer(text=footer_text)
            try:
                await msg.edit(embed=embed, view=None)
            except Exception:
                return False
            return True

        try:
            await msg.edit(view=None)
        except Exception:
            return False
        return True

    async def _recover_question_views(self):
        rows = await self.db.fetchall(
            "SELECT game_id, channel_id FROM games_active_games WHERE game_type = 'ama'"
        )
        if not rows:
            return

        now = datetime.now(timezone.utc)
        recovered = 0
        expired = 0
        games_updated = 0

        for row in rows:
            game_id = row["game_id"]
            channel = await self._resolve_channel(row["channel_id"])
            if channel is None:
                continue

            payload = await get_game_payload(self.db, game_id)
            questions = payload.get("questions", [])
            changed = False

            for idx, question in enumerate(questions):
                msg_id = question.get("question_message_id") or question.get("message_id")
                if not msg_id:
                    continue

                status = (question.get("status") or "").lower()
                if is_resolved_status(status):
                    await self._prune_question_message_view(channel, msg_id)
                    continue

                asked_at = parse_iso_ts(
                    question.get("asked_at")
                    or question.get("created_at")
                    or question.get("posted_at")
                )
                if should_expire(asked_at, now):
                    pruned = await self._prune_question_message_view(
                        channel,
                        msg_id,
                        footer_text="Expired after 7 days without an answer",
                    )
                    if pruned:
                        mark_question_expired(question)
                        changed = True
                        expired += 1
                    continue

                hot_seat_id = question.get("hot_seat_id") or payload.get("hot_seat_id")
                if not hot_seat_id:
                    continue

                view = QuestionView(
                    game_id=game_id,
                    hot_seat_id=hot_seat_id,
                    db=self.db,
                    question_idx=idx,
                    asker_id=question.get("asker_id", 0),
                    ama_view=None,
                    question_text=question.get("text", ""),
                )
                self.bot.add_view(view, message_id=int(msg_id))
                recovered += 1

            if changed:
                recompute_totals(payload)
                await update_game_payload(self.db, game_id, payload)
                games_updated += 1

        log.info(
            "AMA question view recovery complete: %d restored, %d expired, %d games updated.",
            recovered, expired, games_updated,
        )

    async def _prune_stale_question_views(self):
        rows = await self.db.fetchall(
            "SELECT game_id, channel_id FROM games_active_games WHERE game_type = 'ama'"
        )
        if not rows:
            return

        now = datetime.now(timezone.utc)

        for row in rows:
            game_id = row["game_id"]
            channel = await self._resolve_channel(row["channel_id"])
            if channel is None:
                continue

            payload = await get_game_payload(self.db, game_id)
            questions = payload.get("questions", [])
            changed = False

            for question in questions:
                msg_id = question.get("question_message_id") or question.get("message_id")
                if not msg_id:
                    continue

                status = (question.get("status") or "").lower()
                if is_resolved_status(status):
                    continue

                asked_at = parse_iso_ts(
                    question.get("asked_at")
                    or question.get("created_at")
                    or question.get("posted_at")
                )
                if not should_expire(asked_at, now):
                    continue

                pruned = await self._prune_question_message_view(
                    channel,
                    msg_id,
                    footer_text="Expired after 7 days without an answer",
                )
                if not pruned:
                    continue
                mark_question_expired(question)
                changed = True

            if changed:
                recompute_totals(payload)
                await update_game_payload(self.db, game_id, payload)

    async def _question_maintenance_loop(self):
        while not self.bot.is_closed():
            await asyncio.sleep(21600)  # every 6 hours
            try:
                await self._prune_stale_question_views()
            except Exception as e:
                log.debug("AMA stale question prune loop error: %s", e)

    @commands.Cog.listener()
    async def on_ready(self):
        if not self._question_views_rehydrated:
            await self._recover_question_views()
            self._question_views_rehydrated = True
        if self._question_maintenance_task is None or self._question_maintenance_task.done():
            self._question_maintenance_task = asyncio.create_task(self._question_maintenance_loop())

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        game_id = self._active_channels.get(message.channel.id)
        if not game_id:
            return
        active_row = await self.db.fetchone(
            "SELECT game_id FROM games_active_games WHERE game_id = ? AND game_type = 'ama'",
            (game_id,),
        )
        if not active_row:
            await self.cleanup_ended_game(message.channel.id, game_id, channel=message.channel)
            return
        ama_view = self.bot.active_views.get(game_id)
        if not ama_view or not getattr(ama_view, '_bottom_msg', None):
            self._active_channels.pop(message.channel.id, None)
            return
        # Skip while the AMA system is posting its own messages (rotation, etc.)
        if ama_view._suppress_resend:
            return
        # Skip if this IS the bottom bar message (avoid infinite loop)
        if message.id == ama_view._bottom_msg.id:
            return
        if ama_view._bottom_msg.id >= message.id:
            return

        channel_id = message.channel.id
        existing = self._resend_tasks.get(channel_id)
        if existing and not existing.done():
            return  # resend already queued

        async def _debounced():
            await asyncio.sleep(2)
            self._resend_tasks.pop(channel_id, None)
            await _resend_ama_bottom(self.bot, game_id, message.channel)

        self._resend_tasks[channel_id] = asyncio.create_task(_debounced())

    @app_commands.command(name="ama", description="Start an Anonymous Ask Me Anything!")
    @app_commands.describe(mode="screened = host approves questions first, unfiltered = posts immediately")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Unfiltered", value="unfiltered"),
            app_commands.Choice(name="Screened", value="screened"),
        ]
    )
    async def ama(self, interaction: discord.Interaction, mode: str = "unfiltered"):
        log.info("%s used /games play ama in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it with `/games config allow-channel`.",
                ephemeral=True,
            )
            return
        if not await check_game_enabled(self.db, "ama", interaction.guild_id or 0):
            await interaction.response.send_message("Anonymous AMA is currently disabled on this server.", ephemeral=True)
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={"mode": mode},
        )
        if game_id is None:
            try:
                await interaction.followup.send(
                    "I don't have access to send messages in that channel. "
                    "Please grant me **View Channel**, **Send Messages**, and **Embed Links**.",
                    ephemeral=True,
                )
            except Exception:
                pass

    async def launch(
        self,
        *,
        channel,
        host_id: int,
        host_name: str,
        guild_id: int,
        options: dict,
    ) -> str | None:
        """Interaction-free launch (slash command + scheduler). Returns game_id, or None."""
        mode = options.get("mode", "unfiltered")
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "ama",
            state="open",
            payload={
                "mode": mode,
                "questions": [],
                "hot_seat_id": None,
                "hot_seat_rotations": 0,
                "total_passed": 0,
                "total_answered": 0,
            },
        )

        embed = build_lobby_embed(host_name, mode)

        log.info("Game %s (ama) created by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))
        view = AMAView(game_id, host_id, mode, self.db, self.bot)
        self.bot.active_views[game_id] = view

        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("ama launch lacked send perms in channel %s", channel.id)
            return None
        view._game_msg = msg
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])

        # Send the persistent bottom bar (best-effort — the game is already live).
        try:
            bottom_view = AMABottomView(game_id, self.db, view, msg.jump_url)
            bottom_msg = await channel.send(content="🎙️ AMA", view=bottom_view)
            view._bottom_msg = bottom_msg
            bottom_view.message_id = bottom_msg.id
            self.bot.active_views[f"{game_id}_bottom"] = bottom_view
        except Exception:
            log.warning("ama launch: failed to post bottom bar in channel %s", channel.id)
        self._active_channels[channel.id] = game_id
        return game_id


async def setup(bot: commands.Bot):
    cog = AMACog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("ama")
    play.add_command(cog.ama)
    bot.game_launchers["ama"] = cog.launch
