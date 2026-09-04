import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord

from bot_modules.core.branding import safe_resolve_accent
from bot_modules.core.utils import disable_all_items
from bot_modules.services.name_resolver import NameFn, build_name_fn, mention
from discord.ext import commands
from discord import app_commands
from bot_modules.games.command_groups import play
from bot_modules.games.constants import HOW_TO_PLAY
from bot_modules.games.utils.game_manager import (
    ConfirmCloseView,
    finish_launch_response,
    create_game,
    get_active_game_by_id,
    get_game_options,
    update_game_message,
    update_game_payload,
    get_game_payload,
    modify_payload,
    end_game,
    update_session,
    is_game_expired,
    resolve_name,
    channel_name,
)
from bot_modules.games.utils.launch_guard import launch_refusal
from bot_modules.games.utils.live_bar import LiveBarUpdater
from bot_modules.games.utils.question_source import (
    get_wyr_question,
    channel_allows_nsfw,
)
from bot_modules.games.utils.round_pacing import (
    END_DENIED,
    MAX_ROUNDS_CAP,
    MAX_ROUND_SECONDS,
    REASON_EXPIRED,
    REASON_HOST_ENDED,
    REASON_ROUND_CAP,
    RoundPacing,
    advance_check,
    is_scheduled_launch,
    may_control,
    resolve_pacing,
    round_cap_reached,
    seconds_left,
)
from bot_modules.games_wyr.embeds import build_wyr_embed, build_wyr_recap_embed
from bot_modules.games_wyr.logic import (
    next_button_label,
    parse_question_input,
    played_rounds,
    toggle_vote,
)
from bot_modules.games.utils.audit import audit_anonymous
from bot_modules.services.anon_audit_service import (
    EVENT_QUESTION_POSED,
    EVENT_VOTE,
    EVENT_VOTERS_REVEALED,
)

log = logging.getLogger(__name__)

# Cap the player-submitted question queue to prevent flooding.
_MAX_QUEUED_QUESTIONS = 15

# After a restart, a timed round that already ran out still gets a moment
# for the room to see the board before it auto-advances.
_RECOVERY_GRACE_SECONDS = 5.0


class PoseWYRModal(discord.ui.Modal, title="Pose a Question"):
    option_a = discord.ui.TextInput(
        label="Option A",
        placeholder="e.g. fly",
        style=discord.TextStyle.short,
        max_length=200,
    )
    option_b = discord.ui.TextInput(
        label="Option B",
        placeholder="e.g. be invisible",
        style=discord.TextStyle.short,
        max_length=200,
    )

    def __init__(self, view, message: discord.Message):
        super().__init__()
        self._view = view
        self._message = message

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, "Pose a Question", channel_name(interaction.channel))
        if self._view._closed:
            await interaction.response.send_message("This round already ended.", ephemeral=True)
            return
        a = self.option_a.value.strip()
        b = self.option_b.value.strip()
        if not a or not b:
            await interaction.response.send_message("Both options are required.", ephemeral=True)
            return
        if self._view.waiting:
            # The bank had nothing to serve, so this question *is* the round.
            await self._view.begin_round(a, b, self._message)
            await interaction.response.send_message("✅ Your question opened the round!", ephemeral=True)
            count = 0
        else:
            if len(self._view.queued_questions) >= _MAX_QUEUED_QUESTIONS:
                await interaction.response.send_message(
                    f"The question queue is full ({_MAX_QUEUED_QUESTIONS}). Let some play first!",
                    ephemeral=True,
                )
                return
            self._view.queued_questions.append((a, b))
            count = len(self._view.queued_questions)
            self._view.next_btn.label = next_button_label(count)
            try:
                await self._message.edit(view=self._view)
            except discord.HTTPException:
                pass
            await interaction.response.send_message("✅ Your question has been queued!", ephemeral=True)

        # Free-text a member wrote that the channel will see with no name on
        # it. Queued rather than posted, so there is no message to point at.
        if interaction.guild is not None:
            await audit_anonymous(
                self._view.bot, self._view.db, interaction.guild,
                game_type="wyr", user=interaction.user,
                event=EVENT_QUESTION_POSED,
                content=f"A: {a} / B: {b}", label="WYR Posed Question",
                game_id=self._view.game_id,
                channel_id=interaction.channel.id if interaction.channel else None,
                extra={"queue_position": count},
            )


class WYRRoundView(discord.ui.View):
    def __init__(
        self,
        game_id: str,
        host_id: int,
        option_a: str,
        option_b: str,
        round_num: int,
        anonymous: bool,
        db,
        bot,
        host_name: str,
        advance_callback,
        accent: "discord.Color | None" = None,
        *,
        pacing: RoundPacing | None = None,
        finish_callback=None,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.option_a = option_a
        self.option_b = option_b
        self.round_num = round_num
        self.anonymous = anonymous
        self.db = db
        self.bot = bot
        self.host_name = host_name
        self.advance_callback = advance_callback
        self.finish_callback = finish_callback
        # Guild accent resolved once at view-creation time and reused for
        # every live vote update (never re-resolved on the per-vote path).
        self.accent = accent
        self.votes_a: list[int] = []
        self.votes_b: list[int] = []
        self.revealed = False
        # Voter names only render once revealed; Reveal Voters swaps this for
        # a real resolver (prefetched over the voters) before flipping the
        # flag, so the mention fallback never reaches a rendered embed.
        self._name_fn: NameFn = mention
        self._updater = LiveBarUpdater()
        self._closed = False
        self.queued_questions: list[tuple[str, str]] = []
        self.pacing = pacing or RoundPacing()
        # force_end_active_game pokes this alias to wake a timed round.
        self._advanced_event = self.pacing.advanced
        self.message: discord.Message | None = None
        # No question to show yet (the bank had nothing to serve): only Pose,
        # End and Help are live until someone poses one (vote-games-50).
        self.waiting = not (option_a and option_b)
        if self.waiting:
            self._set_round_controls(enabled=False)

    def _set_round_controls(self, *, enabled: bool) -> None:
        for item in (self.vote_a, self.vote_b, self.next_btn, self.reveal_voters):
            item.disabled = not enabled

    def _build_embed(self, closed=False) -> discord.Embed:
        return build_wyr_embed(
            self.host_name,
            self.option_a,
            self.option_b,
            self.votes_a,
            self.votes_b,
            self.anonymous,
            self.round_num,
            closed=closed,
            revealed=self.revealed,
            color=self.accent,
            name_fn=self._name_fn,
            waiting=self.waiting,
            advance_at=self.pacing.advance_at(),
        )

    async def persist_votes(self) -> None:
        """Write this round's tallies on every vote, not only on Next, so a
        restart mid-round rebuilds the bars it shows (vote-games-58)."""
        a, b = list(self.votes_a), list(self.votes_b)

        def _save(payload):
            rd = payload.setdefault("rounds", {}).setdefault(str(self.round_num), {})
            rd["a"] = a
            rd["b"] = b

        await modify_payload(self.db, self.game_id, _save)

    async def begin_round(self, option_a: str, option_b: str, message: discord.Message) -> None:
        """A posed question starts a round that was waiting for one."""
        self.option_a, self.option_b = option_a, option_b
        self.waiting = False
        self._set_round_controls(enabled=True)
        opened = self.pacing.open()

        def _save(payload):
            rd = payload.setdefault("rounds", {}).setdefault(str(self.round_num), {})
            rd["q"] = f"{option_a} OR {option_b}"
            rd["opened_at"] = opened

        await modify_payload(self.db, self.game_id, _save)
        self.message = message
        self.pacing.start_timer(lambda: self.advance_callback(message))
        try:
            await message.edit(embed=self._build_embed(), view=self)
        except discord.HTTPException:
            pass

    async def _audit_vote(
        self, interaction: discord.Interaction, option: str, changed: bool,
        *, was_already_there: bool,
    ) -> None:
        """Record a vote only while the round is actually anonymous.

        With ``anonymous`` off the tallies name their voters in the embed, so
        there is no anonymity for the audit trail to account for.

        ``was_already_there`` skips re-presses of the side the member is
        already on. ``toggle_vote`` treats those as no-ops, so auditing them
        would let one member write unbounded rows by tapping the same button.
        """
        if not self.anonymous or interaction.guild is None or was_already_there:
            return
        await audit_anonymous(
            self.bot, self.db, interaction.guild,
            game_type="wyr", user=interaction.user,
            event=EVENT_VOTE,
            game_id=self.game_id,
            message_id=interaction.message.id if interaction.message else None,
            channel_id=interaction.channel.id if interaction.channel else None,
            extra={"option": option, "changed": changed, "round": self.round_num},
        )

    async def _vote(self, interaction: discord.Interaction, choice: str, label: str) -> None:
        log.info("%s voted in game %s in #%s", interaction.user.display_name, self.game_id, channel_name(interaction.channel))
        if self._closed or self.waiting:
            await interaction.response.send_message("This round is over." if self._closed else "No question yet — pose one first!", ephemeral=True)
            return
        side = self.votes_a if choice == "a" else self.votes_b
        was_already_there = interaction.user.id in side
        changed = toggle_vote(self.votes_a, self.votes_b, interaction.user.id, choice)
        msg = f"✅ Voted **{label}**{' (changed)' if changed else ''}"
        await interaction.response.send_message(msg, ephemeral=True, delete_after=3)
        await self.persist_votes()
        await self._updater.schedule_update(interaction.message, self._build_embed)
        await self._audit_vote(
            interaction, choice, changed, was_already_there=was_already_there
        )

    @discord.ui.button(label="🅰️ Option A", style=discord.ButtonStyle.primary, custom_id="wyr_a", row=0)
    async def vote_a(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._vote(interaction, "a", "🅰️ Option A")

    @discord.ui.button(label="🅱️ Option B", style=discord.ButtonStyle.primary, custom_id="wyr_b", row=0)
    async def vote_b(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._vote(interaction, "b", "🅱️ Option B")

    @discord.ui.button(label="✍️ Pose Question", style=discord.ButtonStyle.primary, custom_id="wyr_pose", row=1)
    async def pose_question(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if self._closed:
            await interaction.response.send_message("This round is over.", ephemeral=True)
            return
        assert interaction.message  # component interactions always carry their message
        await interaction.response.send_modal(PoseWYRModal(self, interaction.message))

    @discord.ui.button(label="⏭️ Next", style=discord.ButtonStyle.secondary, custom_id="wyr_next", row=1)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        uid = interaction.user.id
        refusal = await advance_check(
            interaction, host_id=self.host_id, db=self.db, pacing=self.pacing,
            has_voted=uid in self.votes_a or uid in self.votes_b,
        )
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        if self._closed:
            await interaction.response.send_message("This round is already over.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.advance_callback(interaction.message)

    @discord.ui.button(label="👀 Reveal Voters", style=discord.ButtonStyle.secondary, custom_id="wyr_reveal", row=2)
    async def reveal_voters(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not await may_control(interaction, self.host_id, self.db):
            await interaction.response.send_message("❌ Only the host or a mod can reveal voters.", ephemeral=True)
            return
        # Resolve the voters' names before revealing: a <@id> inside the
        # embed would show as a bare number to anyone who hasn't cached
        # that member. Later voters are present members, so the resolver's
        # live-cache step covers them with no further prefetch.
        guild = interaction.guild
        self._name_fn = await build_name_fn(
            guild=guild,
            db_path=self.bot.ctx.db_path,
            guild_id=guild.id if guild else 0,
            user_ids=self.votes_a + self.votes_b,
        )
        self.revealed = True
        button.disabled = True
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

        # A host/mod deliberately de-anonymising a round — the single most
        # accountability-relevant action in this game.
        if interaction.guild is not None:
            await audit_anonymous(
                self.bot, self.db, interaction.guild,
                game_type="wyr", user=interaction.user,
                event=EVENT_VOTERS_REVEALED,
                game_id=self.game_id,
                message_id=interaction.message.id if interaction.message else None,
                channel_id=interaction.channel.id if interaction.channel else None,
                extra={"round": self.round_num,
                       "voter_count": len(self.votes_a) + len(self.votes_b)},
            )

    @discord.ui.button(label="🏁 End Game", style=discord.ButtonStyle.secondary, custom_id="wyr_end", row=2)
    async def end_game_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Close the game with its recap and pay the room (vote-games-52)."""
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not await may_control(interaction, self.host_id, self.db):
            await interaction.response.send_message(END_DENIED, ephemeral=True)
            return
        if self._closed:
            await interaction.response.send_message("This game already ended.", ephemeral=True)
            return
        message = interaction.message

        async def _confirmed(_confirm_interaction: discord.Interaction) -> None:
            if self.finish_callback is not None:
                await self.finish_callback(message, REASON_HOST_ENDED)

        await interaction.response.send_message(
            "⚠️ Are you sure you want to end this game?", view=ConfirmCloseView(_confirmed), ephemeral=True,
        )

    @discord.ui.button(label="❓ Help", style=discord.ButtonStyle.secondary, custom_id="wyr_htp", row=2)
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["wyr"], ephemeral=True)


class WYRCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(name="wyr", description="Start a Would You Rather game!")
    @app_commands.describe(
        question="Opening question (format: 'option A | option B') — defaults to question bank",
        tags="Comma-separated tags to filter the question bank",
        round_seconds="Seconds per round before it advances itself (0 = you press Next; default from the dashboard)",
        rounds="How many rounds before the recap (0 = until you end it; default from the dashboard)",
    )
    async def wyr(
        self,
        interaction: discord.Interaction,
        question: str = "",
        tags: str = "",
        round_seconds: app_commands.Range[int, 0, MAX_ROUND_SECONDS] | None = None,
        rounds: app_commands.Range[int, 0, MAX_ROUNDS_CAP] | None = None,
    ):
        log.info("%s used /wyr in #%s", interaction.user.display_name, channel_name(interaction.channel))
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        # The one launch guard every door shares: allowed channel, enabled
        # dial, no game already running here, and a bank with something to
        # serve unless the host brought their own question.
        refusal = await launch_refusal(
            self.db, "wyr", interaction.channel_id, interaction.guild_id or 0,
            tags=tag_list, allow_nsfw=channel_allows_nsfw(interaction.channel),
            host_supplied=bool(question.strip()),
        )
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return

        if question.strip() and parse_question_input(question) is None:
            await interaction.response.send_message(
                "❌ Question must have two options separated by `|`, e.g. `fly | be invisible`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={
                "question": question, "tags": tag_list,
                "round_seconds": round_seconds, "max_rounds": rounds,
            },
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
        question = (options.get("question") or "").strip()
        custom_question: tuple[str, str] | None = None
        if question:
            custom_question = parse_question_input(question)
            if custom_question is None:
                log.warning("WYR launch: invalid question %r ignored", question)

        game_opts = await get_game_options(self.db, "wyr", guild_id)
        round_seconds, max_rounds = resolve_pacing(options, game_opts)
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "wyr",
            state="playing",
            payload={
                "anonymous": True, "rounds": {}, "tags": options.get("tags") or [],
                "round_seconds": round_seconds, "max_rounds": max_rounds,
                "scheduled": is_scheduled_launch(options, host_id),
            },
            guild_id=guild_id,
        )
        log.info("Game %s (wyr) created by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))

        try:
            await self._run_round(
                interaction=None,
                game_id=game_id,
                host_id=host_id,
                host_name=host_name,
                round_num=1,
                channel=channel,
                custom_question=custom_question,
            )
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("WYR launch lacked send perms in channel %s", channel.id)
            return None
        # _run_round can end the game and unwind normally (a send that failed
        # for a reason other than permissions). Reporting a game id for that
        # would mark the schedule 'launched' and ping under a dead board. A
        # missing row is the signal the round bailed.
        if await get_active_game_by_id(self.db, game_id) is None:
            return None
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id

    async def _resolve_accent(self, channel) -> "discord.Color | None":
        """Resolve the guild accent color once, safely.

        Returns ``None`` (letting the embed builder fall back to its
        default PHASE color) when there's no guild in scope, no bot ctx,
        or accent resolution raises — a game must never fail to render
        because branding lookup hiccuped.
        """
        return await safe_resolve_accent(
            self.bot, getattr(channel, "guild", None), log_label="WYR"
        )

    @staticmethod
    def _voter_roster_from(payload: dict) -> list[int]:
        """Everyone who voted for either option in any round — the real
        participant set for economy payouts."""
        return sorted({
            int(v)
            for rd in payload.get("rounds", {}).values()
            for v in (rd.get("a") or []) + (rd.get("b") or [])
        })

    async def _voter_roster(self, game_id: str) -> list[int]:
        return self._voter_roster_from(await get_game_payload(self.db, game_id))

    @staticmethod
    def _pacing_from_payload(payload: dict, opened_at: float | None) -> RoundPacing:
        return RoundPacing(
            round_seconds=payload.get("round_seconds", 0),
            max_rounds=payload.get("max_rounds"),
            scheduled=bool(payload.get("scheduled")),
            opened_at=opened_at,
        )

    async def _run_round(
        self,
        interaction,
        game_id: str,
        host_id: int,
        host_name: str,
        round_num: int,
        channel,
        custom_question: tuple[str, str] | None = None,
        carry_over_queue: list[tuple[str, str]] | None = None,
    ):
        payload = await get_game_payload(self.db, game_id)
        if custom_question:
            option_a, option_b = custom_question
        else:
            tags = payload.get("tags") or None
            question = await get_wyr_question(
                self.db, tags=tags, allow_nsfw=channel_allows_nsfw(channel)
            )
            # Nothing to serve: the round opens *waiting* for a posed question
            # rather than ending the game (vote-games-50).
            option_a, option_b = question if question else ("", "")

        waiting = not (option_a and option_b)
        pacing = self._pacing_from_payload(payload, None)
        if not waiting:
            pacing.open()
        rounds_data = payload.setdefault("rounds", {})
        rounds_data[str(round_num)] = {
            "a": [], "b": [],
            "q": "" if waiting else f"{option_a} OR {option_b}",
            "opened_at": pacing.opened_at,
        }
        await update_game_payload(self.db, game_id, payload)

        accent = await self._resolve_accent(channel)
        view = self._build_round_view(
            game_id=game_id,
            host_id=host_id,
            host_name=host_name,
            round_num=round_num,
            channel=channel,
            option_a=option_a,
            option_b=option_b,
            anonymous=payload.get("anonymous", True),
            interaction=interaction,
            accent=accent,
            pacing=pacing,
        )
        if carry_over_queue:
            view.queued_questions = carry_over_queue
            view.next_btn.label = next_button_label(len(carry_over_queue))
        self.bot.active_views[game_id] = view

        embed = view._build_embed()
        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            # Clean up and let the caller (slash wrapper / scheduler) report the failure.
            await end_game(self.db, game_id)
            if game_id in self.bot.active_views:
                del self.bot.active_views[game_id]
            raise
        view.message = msg
        await update_game_message(self.db, game_id, msg.id)
        if not waiting:
            view.pacing.start_timer(lambda: view.advance_callback(msg))

    def _build_round_view(
        self,
        *,
        game_id: str,
        host_id: int,
        host_name: str,
        round_num: int,
        channel,
        option_a: str,
        option_b: str,
        anonymous: bool = True,
        interaction=None,
        accent: "discord.Color | None" = None,
        pacing: RoundPacing | None = None,
    ) -> "WYRRoundView":
        """Construct a round view with its advance and finish callbacks wired.

        Shared by _run_round (fresh round) and recover_game (post-restart) so
        round-to-round advancement behaves identically after a crash.
        """

        async def close_round(message: discord.Message | None) -> None:
            view._closed = True
            # Wakes a timed round's wait (and never cancels it — the timer
            # task may be the caller).
            view.pacing.advanced.set()
            final_embed = view._build_embed(closed=True)
            disable_all_items(view)
            if message is not None:
                try:
                    await message.edit(embed=final_embed, view=view)
                except discord.HTTPException:
                    pass
            await view.persist_votes()

        async def finish(message: discord.Message | None, reason: str = REASON_HOST_ENDED) -> None:
            """End with the recap through the paying path — the host's End
            Game, the round cap, an expired game at Next, and /games end."""
            if not view._closed:
                await close_round(message)
            await self._finish_game(game_id, channel, reason=reason)

        async def advance(message: discord.Message) -> None:
            if view._closed:
                return
            await close_round(message)

            if await is_game_expired(self.db, game_id):
                # Past the 24h line: end with the recap and pay the room
                # (vote-games-59 — this used to be a bare, guild-0 end).
                await self._finish_game(game_id, channel, reason=REASON_EXPIRED)
                return

            if round_cap_reached(round_num, view.pacing.max_rounds):
                await self._finish_game(game_id, channel, reason=REASON_ROUND_CAP)
                return

            remaining = list(view.queued_questions)
            next_custom = remaining.pop(0) if remaining else None
            try:
                await self._run_round(
                    interaction=interaction,
                    game_id=game_id,
                    host_id=host_id,
                    host_name=host_name,
                    round_num=round_num + 1,
                    channel=channel,
                    custom_question=next_custom,
                    carry_over_queue=remaining if remaining else None,
                )
            except Exception:
                log.exception("Error advancing WYR game %s to round %d", game_id, round_num + 1)
                await end_game(self.db, game_id, reason="crash")
                self.bot.active_views.pop(game_id, None)
                try:
                    await channel.send("❌ Something went wrong advancing the round. Game ended.")
                except discord.HTTPException:
                    pass

        view = WYRRoundView(
            game_id=game_id,
            host_id=host_id,
            option_a=option_a,
            option_b=option_b,
            round_num=round_num,
            anonymous=anonymous,
            db=self.db,
            bot=self.bot,
            host_name=host_name,
            # pyright reports a circular inference here (advance captures
            # `view`, whose initializer takes `advance`); the closure itself
            # is fully annotated above.
            advance_callback=advance,  # pyright: ignore[reportGeneralTypeIssues]
            accent=accent,
            pacing=pacing,
            finish_callback=finish,
        )
        return view

    async def _finish_game(self, game_id: str, channel, *, reason: str) -> bool:
        """Post the game-over recap and end through the paying path.

        Returns False when the game had already been ended by another path
        (``end_game``'s DELETE claim makes the payout exactly-once either way).
        """
        row = await get_active_game_by_id(self.db, game_id)
        if row is None:
            self.bot.active_views.pop(game_id, None)
            return False
        payload = await get_game_payload(self.db, game_id)
        rounds = payload.get("rounds", {})
        roster = self._voter_roster_from(payload)
        guild = getattr(channel, "guild", None)
        accent = await self._resolve_accent(channel)
        embed = build_wyr_recap_embed(rounds, color=accent, reason=reason)
        if guild:
            from bot_modules.economy.game_rewards import append_payout_footer
            await append_payout_footer(self.bot, embed, guild.id, "wyr")
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            log.info("WYR recap not delivered for game %s", game_id)
        ended = await end_game(
            self.db, game_id,
            player_count=len(roster), round_count=len(played_rounds(rounds)), payload=payload,
            bot=self.bot, player_ids=roster, reason=reason,
        )
        self.bot.active_views.pop(game_id, None)
        return ended is not None

    async def end_with_recap(self, channel, game_id: str) -> bool:
        """``/games end`` on a Would You Rather game: the same recap ending as
        the host's 🏁 End Game, instead of the red Force-Closed card."""
        view = self.bot.active_views.get(game_id)
        if isinstance(view, WYRRoundView) and view.finish_callback is not None:
            await view.finish_callback(view.message, REASON_HOST_ENDED)
            return True
        return await self._finish_game(game_id, channel, reason=REASON_HOST_ENDED)

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Rebuild the current round's view after a restart, restoring votes."""
        rounds = payload.get("rounds", {})
        if not rounds:
            return False
        cur = max(rounds, key=lambda k: int(k))
        rd = rounds.get(cur, {})
        q = rd.get("q", "") or ""
        option_a, option_b = (q.split(" OR ", 1) + [""])[:2] if " OR " in q else (q, "")

        game_id = row["game_id"]
        host_id = int(row["host_id"])
        guild = getattr(channel, "guild", None)
        host_name = resolve_name(guild, host_id) if guild else "Host"

        accent = await self._resolve_accent(channel)
        pacing = self._pacing_from_payload(payload, rd.get("opened_at"))
        view = self._build_round_view(
            game_id=game_id,
            host_id=host_id,
            host_name=host_name,
            round_num=int(cur),
            channel=channel,
            option_a=option_a,
            option_b=option_b,
            anonymous=payload.get("anonymous", True),
            interaction=None,
            accent=accent,
            pacing=pacing,
        )
        view.votes_a = list(rd.get("a", []))
        view.votes_b = list(rd.get("b", []))
        view.message = message
        self.bot.active_views[game_id] = view
        self.bot.add_view(view, message_id=message.id)
        if not view.waiting:
            left = seconds_left(pacing.opened_at, pacing.round_seconds)
            if left is not None:
                view.pacing.start_timer(
                    lambda: view.advance_callback(message),
                    seconds=max(left, _RECOVERY_GRACE_SECONDS),
                )
        log.info("Recovered wyr game %s (round %s) in #%s", game_id, cur, getattr(channel, "name", channel.id))
        return True


async def setup(bot: "Bot"):
    cog = WYRCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("wyr")
    play.add_command(cog.wyr, override=True)
    bot.game_launchers["wyr"] = cog.launch
    bot.game_recoverers["wyr"] = cog.recover_game
