import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord

from bot_modules.core.utils import disable_all_items
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import HOW_TO_PLAY
from bot_modules.games.command_groups import play
from bot_modules.games.utils.game_manager import (
    finish_launch_response,
    check_allowed_channel,
    check_game_enabled,
    create_game,
    update_game_message,
    update_game_payload,
    get_game_payload,
    modify_payload,
    end_game,
    update_session,
    channel_name,
)
from bot_modules.core.branding import resolve_accent_color
from bot_modules.games.utils.audit import send_audit_log
from bot_modules.games_ama.embeds import (
    build_answered_embed,
    build_asker_dm_embed,
    build_lobby_embed,
    build_main_embed,
    build_panel_embed,
    build_question_embed,
    build_recap_embed,
)
from bot_modules.games_ama.logic import (
    AMA_FORMAT_HOT_SEAT,
    AMA_FORMAT_PANEL,
    add_question,
    bottom_bar_label,
    build_question_entry,
    compute_recap_stats,
    is_panel_target,
    is_resolved_status,
    mark_question_answered,
    mark_question_approved,
    mark_question_expired,
    mark_question_passed,
    mark_question_rejected,
    normalize_format,
    panel_bottom_bar_label,
    parse_iso_ts,
    recompute_totals,
    should_expire,
    toggle_panel_member,
    utcnow_iso,
)

log = logging.getLogger(__name__)


async def _fire_ama_ask_trigger(client, channel, asker_id: int, game_id: str, q_idx: int) -> None:
    """Credit an AMA question-asker's ``ama_ask`` economy quest trigger.

    Fired only when the question actually becomes visible: on submit in
    unfiltered mode, on host approval in screened mode (rejected questions
    never pay). ``channel`` is the guild text channel — screened approval
    happens in the host's DMs, so we take the guild from it rather than the
    interaction. ``asker_id`` of 0 (AI-seeded idle questions) is skipped by
    the guarded ``fire_member_trigger``. Occurrence keys per question so
    "ask N questions" quests count each one once.
    """
    from bot_modules.economy.game_rewards import fire_member_trigger  # noqa: PLC0415

    guild = getattr(channel, "guild", None)
    if guild is None:
        return
    await fire_member_trigger(
        cast("Bot", client), guild.id, asker_id, "ama_ask",
        occurrence=f"{game_id}:{q_idx}",
    )


# ── Modals ───────────────────────────────────────────────────────────────────


class AskQuestionModal(discord.ui.Modal, title="Your Question"):
    question = discord.ui.TextInput(
        label="Question",
        style=discord.TextStyle.paragraph,
        max_length=500,
        placeholder="Ask anything…",
    )

    def __init__(self, game_id: str, db, channel, mode: str, host_id: int, target_id: int, ama_view):
        super().__init__()
        self.game_id = game_id
        self.db = db
        self.channel = channel
        self.mode = mode
        self.host_id = host_id
        # Who the question is directed at, captured at button-press time. In
        # hot-seat format this is the current hot seat; in panel format it's
        # the panelist the asker picked from the dropdown.
        self.target_id = target_id
        self.ama_view = ama_view

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, "Your Question", channel_name(interaction.channel))

        # Guard: game was closed while the modal was open
        if self.ama_view._closed:
            await interaction.response.send_message(
                "⚠️ The game closed while you were typing — your question was not submitted.",
                ephemeral=True,
            )
            return

        # Guard: the target moved on while the modal was open. In hot-seat
        # format the seat may have rotated; in panel format the person may
        # have left the panel. Either way the captured target is stale.
        if self.ama_view.game_format == AMA_FORMAT_PANEL:
            if not is_panel_target(self.ama_view.panel, self.target_id):
                await interaction.response.send_message(
                    "⚠️ That person left the panel while you were typing — please try again.",
                    ephemeral=True,
                )
                return
        elif self.ama_view.hot_seat_id != self.target_id:
            await interaction.response.send_message(
                "⚠️ The hot seat changed while you were typing — please try again.",
                ephemeral=True,
            )
            return

        q_entry = build_question_entry(
            asker_id=interaction.user.id,
            text=self.question.value,
            hot_seat_id=self.target_id,
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
            color = await resolve_accent_color(cast("Bot", interaction.client).ctx.db_path, interaction.guild) if interaction.guild else None
            embed = build_question_embed(self.question.value, color=color)
            target_member = interaction.guild.get_member(self.target_id) if interaction.guild else None
            question_view = QuestionView(self.game_id, self.target_id, self.db, q_idx, interaction.user.id, self.ama_view, self.question.value)
            question_msg = await self.channel.send(
                content=target_member.mention if target_member else None,
                embed=embed,
                view=question_view,
            )

            def _mark_posted(payload):
                mark_question_approved(payload, q_idx, message_id=question_msg.id)

            await modify_payload(self.db, self.game_id, _mark_posted)
            await interaction.response.send_message("Your question has been posted anonymously!", ephemeral=True)
            # Advance turn/status only after the question is actually posted
            await self.ama_view.after_question_posted(self.channel)
            # Unfiltered questions post immediately → credit the asker now.
            await _fire_ama_ask_trigger(
                interaction.client, self.channel, interaction.user.id, self.game_id, q_idx
            )
        else:
            # Screened — DM the host so the question stays hidden from the channel
            approve_view = ScreenedQuestionView(
                game_id=self.game_id,
                question_text=self.question.value,
                question_idx=q_idx,
                db=self.db,
                channel=self.channel,
                hot_seat_id=self.target_id,
                asker_id=interaction.user.id,
                ama_view=self.ama_view,
            )
            dm_sent = False
            try:
                guild = interaction.guild
                host_member = guild.get_member(self.host_id) if guild else None
                if host_member and guild:
                    target_member = guild.get_member(self.target_id)
                    target_name = target_member.display_name if target_member else "the hot seat"
                    await host_member.send(
                        f"📨 New screened question for **{target_name}** (in {self.channel.mention}):",
                        view=approve_view,
                    )
                    dm_sent = True
            except discord.Forbidden:
                pass
            except discord.HTTPException:
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
        placeholder="Type your answer…",
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
        log.info("%s submitted reply modal in #%s", interaction.user.display_name, channel_name(interaction.channel))

        color = await resolve_accent_color(cast("Bot", interaction.client).ctx.db_path, interaction.guild) if interaction.guild else None
        answered_embed = build_answered_embed(
            self.question_text,
            self.reply.value,
            interaction.user.display_name,
            color=color,
        )
        await interaction.response.edit_message(embed=answered_embed, view=None)

        # DM the anonymous asker so they know their question was answered
        try:
            asker = interaction.guild.get_member(self.asker_id) if interaction.guild else None
            channel = interaction.channel
            if asker and channel is not None and not isinstance(channel, (discord.DMChannel, discord.GroupChannel)):
                dm_embed = build_asker_dm_embed(channel.mention, color=color)
                await asker.send(embed=dm_embed)
        except discord.Forbidden:
            pass  # DMs disabled
        except Exception as e:
            log.debug("Could not DM asker %s: %s", self.asker_id, e)

        # Mark question as answered in payload — does NOT increment game tallies
        payload = await get_game_payload(self.db, self.game_id)
        assert interaction.message
        mark_question_answered(
            payload,
            self.question_idx,
            message_id=interaction.message.id,
        )
        await update_game_payload(self.db, self.game_id, payload)

        # Quest hook: answering as the hot seat — twin of ama_ask, keyed per
        # question so a busy AMA counts each answer once. Guarded wrapper.
        if interaction.guild is not None:
            from bot_modules.economy.game_rewards import fire_member_trigger  # noqa: PLC0415

            await fire_member_trigger(
                cast("Bot", interaction.client), interaction.guild.id,
                interaction.user.id, "ama_answer",
                occurrence=f"{self.game_id}:{self.question_idx}",
            )

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
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if interaction.user.id != self.ama_view.host_id:
            await interaction.response.send_message("Only the host can approve questions.", ephemeral=True)
            return
        color = await resolve_accent_color(cast("Bot", interaction.client).ctx.db_path, interaction.guild) if interaction.guild else None
        embed = build_question_embed(self.question_text, color=color)
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

        # Advance turn/status now that the question is actually posted
        await self.ama_view.after_question_posted(self.channel)
        # Screened questions credit the asker only on approval (rejected ones
        # never reach here). The host approves from their DMs, so the guild
        # comes from the game channel, not this interaction.
        await _fire_ama_ask_trigger(
            interaction.client, self.channel, self.asker_id, self.game_id, self.question_idx
        )

    @discord.ui.button(label="❌ Reject", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
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
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if interaction.user.id != self.hot_seat_id:
            await interaction.response.send_message("Only the hot seat player can reply.", ephemeral=True)
            return
        modal = ReplyModal(self.game_id, self.db, self.question_idx, self.asker_id, self.ama_view, self.question_text)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Pass", style=discord.ButtonStyle.secondary, custom_id="ama_pass")
    async def pass_question(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if interaction.user.id != self.hot_seat_id:
            await interaction.response.send_message("Only the hot seat player can pass.", ephemeral=True)
            return
        assert interaction.message
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
    def __init__(self, game_id: str, host_id: int, mode: str, db, bot, game_format: str = AMA_FORMAT_HOT_SEAT):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.mode = mode
        self.game_format = normalize_format(game_format)
        self.db = db
        self.bot = bot
        self.hot_seat_id: int | None = None
        self._game_msg: discord.Message | None = None
        self._bottom_msg: discord.Message | None = None
        self._hot_seat_name: str | None = None
        self.queue: list[int] = []          # user IDs waiting for hot seat (hot-seat format)
        self.panel: list[int] = []          # user IDs answering questions (panel format)
        self.questions_this_turn: int = 0   # answered questions for current hot seat
        self._suppress_resend: bool = False # suppress bottom-bar resend during system messages
        self._closed: bool = False          # True once close has been confirmed; blocks new questions
        self._ping_subscribers: set[int] = set()  # users who want pings on new hot seat
        self._hot_seat_timer_task: asyncio.Task | None = None  # 1-hour auto-rotate timer

        # Panel format has no single seat to rotate, so the host controls that
        # only make sense for hot-seat games are dropped from the panel.
        if self.game_format == AMA_FORMAT_PANEL:
            for item in list(self.children):
                if getattr(item, "custom_id", None) in {"ama_skip", "ama_new_hs"}:
                    self.remove_item(item)

    def is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild and isinstance(interaction.user, discord.Member):
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    async def _build_embed(self, host_name: str, payload: dict | None = None) -> discord.Embed:
        guild = self._game_msg.guild if self._game_msg else None
        color = await resolve_accent_color(self.bot.ctx.db_path, guild) if guild else None

        def _name_resolver(uid: int) -> str:
            m = guild.get_member(uid) if guild else None
            return m.display_name if m else str(uid)

        if self.game_format == AMA_FORMAT_PANEL:
            return build_panel_embed(
                host_name=host_name,
                mode=self.mode,
                panel=list(self.panel),
                name_resolver=_name_resolver,
                payload=payload,
                color=color,
            )

        return build_main_embed(
            host_name=host_name,
            mode=self.mode,
            hot_seat_name=self._hot_seat_name,
            questions_this_turn=self.questions_this_turn,
            queue=list(self.queue),
            name_resolver=_name_resolver,
            payload=payload,
            color=color,
        )

    async def refresh_status(self, channel):
        """Re-fetch payload and update the main game message with current stats."""
        if self._game_msg is None:
            return
        try:
            payload = await get_game_payload(self.db, self.game_id)
            host_member = channel.guild.get_member(self.host_id) if channel.guild else None
            embed = await self._build_embed(
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

    async def after_question_posted(self, channel):
        """Advance game state after a question is posted, per format.

        Hot-seat games count the question toward the 4-per-turn rotation.
        Panel games have no single seat or turn limit, so we only refresh
        the status bar to pick up the new question/answer totals.
        """
        if self.game_format == AMA_FORMAT_PANEL:
            await self.refresh_status(channel)
        else:
            await self.register_question_asked(channel)

    async def _handle_volunteer(self, interaction: discord.Interaction):
        """Shared Volunteer handler for the main view and the bottom bar."""
        if self._closed:
            await interaction.response.send_message("This game is closing — no new volunteers.", ephemeral=True)
            return
        if self.game_format == AMA_FORMAT_PANEL:
            await self._toggle_panel(interaction)
            return

        user_id = interaction.user.id
        # Already in the hot seat
        if user_id == self.hot_seat_id:
            await interaction.response.send_message("You're already in the hot seat!", ephemeral=True)
            return
        # Already queued — treat a second tap as "leave the queue"
        if user_id in self.queue:
            self.queue.remove(user_id)
            await interaction.response.send_message("You've left the queue.", ephemeral=True)
            await self.refresh_status(interaction.channel)
            await self._update_bottom_bar()
            return
        # No one in the hot seat — go straight in
        if self.hot_seat_id is None:
            assert isinstance(interaction.user, discord.Member)
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

    async def _toggle_panel(self, interaction: discord.Interaction):
        """Join or leave the panel of people taking questions (panel format)."""
        assert isinstance(interaction.user, discord.Member)
        member = interaction.user
        joined = toggle_panel_member(self.panel, member.id)
        if joined:
            await interaction.response.send_message(
                "🙋 You're on the panel — anyone can now ask you anonymous questions. "
                "Tap **Volunteer** again to leave.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "👋 You've left the panel — no new questions will be directed to you.",
                ephemeral=True,
            )
        self._suppress_resend = True
        try:
            await self.refresh_status(interaction.channel)
            await self._update_bottom_bar()
        finally:
            self._suppress_resend = False
        if joined and isinstance(interaction.channel, discord.abc.Messageable):
            await interaction.channel.send(
                f"🙋 {member.mention} joined the AMA panel — ask them anything!"
            )

    async def _begin_ask(self, interaction: discord.Interaction):
        """Shared 'Ask a Question' handler for the main view and the bottom bar."""
        if self._closed:
            await interaction.response.send_message("This game is closing — no new questions.", ephemeral=True)
            return

        if self.game_format == AMA_FORMAT_PANEL:
            guild = interaction.guild
            candidates = [
                m for m in (guild.get_member(uid) if guild else None for uid in self.panel) if m
            ]
            if not candidates:
                await interaction.response.send_message(
                    "No one has joined the panel yet — tap 🙋 Volunteer to be the first!",
                    ephemeral=True,
                )
                return
            select = AskTargetSelect(self, candidates)
            view = discord.ui.View(timeout=120)
            view.add_item(select)
            note = f"\n(Showing the first 25 of {len(candidates)} panelists.)" if len(candidates) > 25 else ""
            await interaction.response.send_message(
                f"Who do you want to ask?{note}", view=view, ephemeral=True
            )
            return

        # Hot-seat format
        if self.hot_seat_id is None:
            await interaction.response.send_message("No one is in the hot seat yet!", ephemeral=True)
            return
        modal = AskQuestionModal(
            self.game_id, self.db, interaction.channel, self.mode, self.host_id, self.hot_seat_id, self
        )
        await interaction.response.send_modal(modal)

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
        """Update bottom bar text with current hot seat / panel info."""
        if not self._bottom_msg:
            return
        if self.game_format == AMA_FORMAT_PANEL:
            label = panel_bottom_bar_label(len(self.panel))
        else:
            label = bottom_bar_label(self._hot_seat_name, len(self.queue))
        try:
            await self._bottom_msg.edit(content=label)
        except discord.HTTPException:
            pass

    async def check_turn_rotation(self, channel):
        """Check if the current hot seat has hit 4 answered questions — rotate if so."""
        if self.questions_this_turn < 4:
            return

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

    @discord.ui.button(label="🙋 Volunteer", style=discord.ButtonStyle.success, custom_id="ama_volunteer")
    async def volunteer(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await self._handle_volunteer(interaction)

    @discord.ui.button(label="Ask a Question", style=discord.ButtonStyle.primary, custom_id="ama_ask")
    async def ask_question(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await self._begin_ask(interaction)

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary, custom_id="ama_skip", row=1)
    async def skip_hot_seat(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
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
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can select the hot seat.", ephemeral=True)
            return
        # Only members who volunteered (are in the queue) may be promoted —
        # nobody gets forced into the public hot seat without opting in.
        guild = interaction.guild
        candidates = [
            m for m in (guild.get_member(uid) if guild else None for uid in self.queue) if m
        ]
        if not candidates:
            await interaction.response.send_message(
                "No one is waiting in the queue. Members can tap **🙋 Volunteer** "
                "to opt in, then you can pick them here.",
                ephemeral=True,
            )
            return
        select = HotSeatSelect(self.game_id, self.db, self, candidates)
        view = discord.ui.View(timeout=60)
        view.add_item(select)
        await interaction.response.send_message(
            "Select the new hot seat (from volunteers):", view=view, ephemeral=True
        )

    @discord.ui.button(label="❓ Help", style=discord.ButtonStyle.secondary, custom_id="ama_htp", row=1)
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["ama"], ephemeral=True)

    async def _do_close(self, channel):
        self._closed = True
        if self._hot_seat_timer_task and not self._hot_seat_timer_task.done():
            self._hot_seat_timer_task.cancel()

        # Remove the bottom bar immediately when the game closes.
        cog = self.bot.get_cog("AMACog")
        if cog and hasattr(cog, "cleanup_ended_game"):
            await cog.cleanup_ended_game(channel.id, self.game_id, channel=channel)

        payload = await get_game_payload(self.db, self.game_id)
        stats = compute_recap_stats(payload)
        total_q = stats["total_q"]
        unique_askers = stats["unique_askers"]

        color = await resolve_accent_color(self.bot.ctx.db_path, channel.guild) if channel.guild else None
        embed = build_recap_embed(self.mode, stats, color=color)
        if channel.guild:
            from bot_modules.economy.game_rewards import append_payout_footer
            await append_payout_footer(self.bot, embed, channel.guild.id, "ama")

        self.stop()
        disable_all_items(self)

        if self._game_msg:
            try:
                await self._game_msg.edit(view=self)
            except discord.HTTPException:
                pass
        await channel.send(embed=embed)

        log.info("Game %s ended — %d questions asked", self.game_id, total_q)
        # Participants = members who asked a question (asker_id 0 is the AI
        # idle-question sentinel — mirror unique_asker_count's filter) plus the
        # hot-seat occupants who answered them.
        _qs = payload.get("questions", [])
        participants = sorted(
            {q["asker_id"] for q in _qs if q.get("asker_id", 0) > 0}
            | {q["hot_seat_id"] for q in _qs if q.get("hot_seat_id", 0) > 0}
        )
        await end_game(self.db, self.game_id, player_count=unique_askers, round_count=total_q, payload=payload,
                       bot=self.bot, player_ids=participants)
        if self.game_id in self.bot.active_views:
            del self.bot.active_views[self.game_id]


class HotSeatSelect(discord.ui.Select):
    """Picker restricted to members who volunteered (are in the AMA queue),
    so the host can rotate the hot seat only among people who opted in."""

    def __init__(self, game_id: str, db, ama_view: AMAView, candidates: list[discord.Member]):
        options = [
            discord.SelectOption(label=m.display_name[:100], value=str(m.id))
            for m in candidates[:25]
        ]
        super().__init__(
            placeholder="Select new hot seat", min_values=1, max_values=1, options=options
        )
        self.game_id = game_id
        self.db = db
        self.ama_view = ama_view

    async def callback(self, interaction: discord.Interaction):
        uid = int(self.values[0])
        guild = interaction.guild
        new_member = guild.get_member(uid) if guild else None
        if new_member is None:
            await interaction.response.send_message(
                "That member is no longer available.", ephemeral=True
            )
            return
        # Remove from queue if they were queued
        if new_member.id in self.ama_view.queue:
            self.ama_view.queue.remove(new_member.id)

        await interaction.response.defer()
        await interaction.edit_original_response(content="Hot seat updated.", view=None)
        await self.ama_view._set_hot_seat(new_member, interaction.channel, announce=True)


class AskTargetSelect(discord.ui.Select):
    """Panel-format picker: choose which panelist to send an anonymous question to."""

    def __init__(self, ama_view: AMAView, candidates: list[discord.Member]):
        options = [
            discord.SelectOption(label=m.display_name[:100], value=str(m.id))
            for m in candidates[:25]
        ]
        super().__init__(
            placeholder="Choose who to ask", min_values=1, max_values=1, options=options
        )
        self.ama_view = ama_view

    async def callback(self, interaction: discord.Interaction):
        target_id = int(self.values[0])
        if self.ama_view._closed:
            await interaction.response.send_message("This game is closing — no new questions.", ephemeral=True)
            return
        if not is_panel_target(self.ama_view.panel, target_id):
            await interaction.response.send_message(
                "That person left the panel — please pick someone else.", ephemeral=True
            )
            return
        modal = AskQuestionModal(
            self.ama_view.game_id, self.ama_view.db, interaction.channel,
            self.ama_view.mode, self.ama_view.host_id, target_id, self.ama_view,
        )
        await interaction.response.send_modal(modal)


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
        # "Notify Me" pings on hot-seat changes, which never happen in panel
        # format — drop it so the bottom bar has no dead button there.
        if ama_view.game_format == AMA_FORMAT_PANEL:
            for item in list(self.children):
                if getattr(item, "custom_id", None) == "ama_notify_toggle":
                    self.remove_item(item)

    @discord.ui.button(label="Ask a Question", style=discord.ButtonStyle.primary, custom_id="ama_bottom_ask")
    async def ask_question(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' (bottom bar) in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await self.ama_view._begin_ask(interaction)

    @discord.ui.button(label="🔔 Notify Me", style=discord.ButtonStyle.secondary, custom_id="ama_notify_toggle")
    async def toggle_notify(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' (bottom bar) in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        user_id = interaction.user.id
        if user_id in self.ama_view._ping_subscribers:
            self.ama_view._ping_subscribers.discard(user_id)
            await interaction.response.send_message("🔕 You'll no longer be pinged when the hot seat changes.", ephemeral=True)
        else:
            self.ama_view._ping_subscribers.add(user_id)
            await interaction.response.send_message("🔔 You'll be pinged whenever the hot seat changes!", ephemeral=True)

    @discord.ui.button(label="🙋 Volunteer", style=discord.ButtonStyle.success, custom_id="ama_bottom_volunteer")
    async def volunteer(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' (bottom bar) in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await self.ama_view._handle_volunteer(interaction)


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
        except discord.HTTPException:
            pass

        if getattr(ama_view, 'game_format', AMA_FORMAT_HOT_SEAT) == AMA_FORMAT_PANEL:
            label = panel_bottom_bar_label(len(ama_view.panel))
        else:
            hot_seat_name = getattr(ama_view, '_hot_seat_name', None)
            label = bottom_bar_label(hot_seat_name, len(ama_view.queue))
        new_msg = await channel.send(content=label, view=bottom_view)
        ama_view._bottom_msg = new_msg
        if hasattr(bottom_view, "message_id"):
            bottom_view.message_id = new_msg.id
        # Keep the persisted id in step so recovery rebinds the current bar.
        try:
            def _store_bottom(p, _mid=new_msg.id):
                p["bottom_message_id"] = _mid
            await modify_payload(ama_view.db, game_id, _store_bottom)
        except Exception:
            pass
    finally:
        ama_view._suppress_resend = False


class AMACog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot
        self._active_channels: dict[int, str] = {}
        self._question_maintenance_task: asyncio.Task | None = None
        self._resend_tasks: dict[int, asyncio.Task] = {}  # channel_id → pending debounce task

    @property
    def db(self):
        return self.bot.games_db

    async def cog_unload(self) -> None:
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

        ama_view_raw = self.bot.active_views.get(game_id)
        ama_view = ama_view_raw if isinstance(ama_view_raw, AMAView) else None
        bottom_view_raw = self.bot.active_views.get(f"{game_id}_bottom")
        bottom_view = bottom_view_raw if isinstance(bottom_view_raw, AMABottomView) else None
        deleted = False

        if ama_view:
            try:
                ama_view.stop()
            except Exception:
                log.exception("ama: failed to stop AMA view during cleanup")

        if bottom_view:
            try:
                bottom_view.stop()
                disable_all_items(bottom_view)
            except Exception:
                log.exception("ama: failed to stop bottom view during cleanup")

        if channel and ama_view and ama_view._bottom_msg:
            try:
                await ama_view._bottom_msg.delete()
                deleted = True
            except discord.HTTPException:
                pass

        if channel and (not deleted) and bottom_view and bottom_view.message_id and isinstance(channel, discord.abc.Messageable):
            try:
                msg = await channel.fetch_message(int(bottom_view.message_id))
                await msg.delete()
                deleted = True
            except discord.HTTPException:
                pass

        # Last resort: edit the bottom bar to show disabled buttons
        if channel and (not deleted) and bottom_view:
            bottom_msg = getattr(ama_view, "_bottom_msg", None) if ama_view else None
            if bottom_msg:
                try:
                    await bottom_msg.edit(content="🛑 AMA ended", view=bottom_view)
                except discord.HTTPException:
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

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Rebuild an in-flight AMA's persistent views after a bot restart.

        Registered in ``bot.game_recoverers["ama"]`` and driven by the shared
        startup sweep, which hands us the game's main embed *message*. On a
        restart ``bot.active_views`` is bare and discord.py's persistent-view
        registry is empty, so every AMA button (Volunteer / Ask / New Hot Seat,
        the sticky bottom bar, and each open question card) is dead and the
        channel stays "in progress".

        We rebuild the top control panel (``AMAView``) bound to the main embed,
        the sticky bottom bar (``AMABottomView``) rebound to its persisted
        message so it can keep re-sticking, and every unresolved question card
        (``QuestionView``) — crucially wired to the live ``ama_view`` so Reply /
        Pass refresh the main embed — and repopulate ``_active_channels`` so the
        ``on_message`` re-stick loop re-arms. Expired cards are pruned in passing.
        """
        game_id = row["game_id"]
        host_id = int(row["host_id"])
        game_format = normalize_format(payload.get("format"))
        mode = payload.get("mode", "unfiltered")
        guild = getattr(message, "guild", None) or getattr(channel, "guild", None)

        # Top control panel — bound to the main embed message.
        view = AMAView(game_id, host_id, mode, self.db, self.bot, game_format=game_format)
        view._game_msg = message
        hot_seat_id = payload.get("hot_seat_id")
        if hot_seat_id:
            view.hot_seat_id = int(hot_seat_id)
            member = guild.get_member(int(hot_seat_id)) if guild else None
            if member:
                view._hot_seat_name = member.display_name
        self.bot.active_views[game_id] = view
        self.bot.add_view(view, message_id=int(message.id))

        # Sticky bottom bar — rebound to its persisted message when it survived,
        # otherwise registered as a bare persistent view so its buttons still work.
        bottom_view = AMABottomView(game_id, self.db, view, message.jump_url)
        bottom_id = payload.get("bottom_message_id")
        bottom_msg = None
        if bottom_id:
            try:
                bottom_msg = await channel.fetch_message(int(bottom_id))
            except Exception:
                bottom_msg = None
        if bottom_msg is not None:
            view._bottom_msg = bottom_msg
            bottom_view.message_id = bottom_msg.id
            self.bot.add_view(bottom_view, message_id=bottom_msg.id)
        else:
            self.bot.add_view(bottom_view)
        self.bot.active_views[f"{game_id}_bottom"] = bottom_view

        # Per-question cards — rebuilt with the live ama_view; expired cards pruned.
        now = datetime.now(timezone.utc)
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
                continue
            q_hot_seat = question.get("hot_seat_id") or payload.get("hot_seat_id")
            if not q_hot_seat:
                continue
            qview = QuestionView(
                game_id=game_id,
                hot_seat_id=q_hot_seat,
                db=self.db,
                question_idx=idx,
                asker_id=question.get("asker_id", 0),
                ama_view=view,
                question_text=question.get("text", ""),
            )
            self.bot.add_view(qview, message_id=int(msg_id))

        if changed:
            recompute_totals(payload)
            await update_game_payload(self.db, game_id, payload)

        self._active_channels[channel.id] = game_id
        log.info("Recovered ama game %s in #%s", game_id, getattr(channel, "name", channel.id))
        return True

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
        # View re-registration now runs through the shared startup recovery
        # sweep (bot.game_recoverers["ama"] -> recover_game); on_ready only
        # (re)arms the periodic stale-question prune loop.
        if self._question_maintenance_task is None or self._question_maintenance_task.done():
            self._question_maintenance_task = asyncio.create_task(self._question_maintenance_loop())

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
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
        if not isinstance(ama_view, AMAView) or not ama_view._bottom_msg:
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
    @app_commands.describe(
        mode="screened = host approves questions first, unfiltered = posts immediately",
        format="hot seat = one person at a time, open panel = ask anyone who's opted in",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Unfiltered", value="unfiltered"),
            app_commands.Choice(name="Screened", value="screened"),
        ],
        format=[
            app_commands.Choice(name="Hot Seat (one at a time)", value=AMA_FORMAT_HOT_SEAT),
            app_commands.Choice(name="Open Panel (ask anyone opted in)", value=AMA_FORMAT_PANEL),
        ],
    )
    async def ama(self, interaction: discord.Interaction, mode: str = "unfiltered", format: str = AMA_FORMAT_HOT_SEAT):
        log.info("%s used /games play ama in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it from the web dashboard.",
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
            options={"mode": mode, "format": format},
        )
        await finish_launch_response(interaction, game_id)

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
        game_format = normalize_format(options.get("format"))
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "ama",
            state="open",
            payload={
                "mode": mode,
                "format": game_format,
                "questions": [],
                "hot_seat_id": None,
                "hot_seat_rotations": 0,
                "total_passed": 0,
                "total_answered": 0,
            },
        )

        launch_guild = getattr(channel, "guild", None)
        color = await resolve_accent_color(self.bot.ctx.db_path, launch_guild) if launch_guild else None
        if game_format == AMA_FORMAT_PANEL:
            embed = build_panel_embed(host_name, mode, [], str, color=color)
        else:
            embed = build_lobby_embed(host_name, mode, color=color)

        log.info("Game %s (ama) created by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))
        view = AMAView(game_id, host_id, mode, self.db, self.bot, game_format=game_format)
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
            initial_label = panel_bottom_bar_label(0) if game_format == AMA_FORMAT_PANEL else "🎙️ AMA"
            bottom_msg = await channel.send(content=initial_label, view=bottom_view)
            view._bottom_msg = bottom_msg
            bottom_view.message_id = bottom_msg.id
            self.bot.active_views[f"{game_id}_bottom"] = bottom_view
            # Persist the bottom-bar message id so crash recovery can rebind it
            # (and keep re-sticking it) after a restart.
            def _store_bottom(p, _mid=bottom_msg.id):
                p["bottom_message_id"] = _mid
            await modify_payload(self.db, game_id, _store_bottom)
        except Exception:
            log.warning("ama launch: failed to post bottom bar in channel %s", channel.id)
        self._active_channels[channel.id] = game_id
        return game_id


async def setup(bot: "Bot"):
    cog = AMACog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("ama")
    play.add_command(cog.ama, override=True)
    bot.game_launchers["ama"] = cog.launch
    bot.game_recoverers["ama"] = cog.recover_game
