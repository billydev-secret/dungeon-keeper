import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord

from bot_modules.core.branding import safe_resolve_accent
from bot_modules.core.utils import disable_all_items
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import GAME_ICONS, HOW_TO_PLAY, play_description
from bot_modules.games.command_groups import play
from bot_modules.games.utils.game_manager import (
    ConfirmCloseView,
    finish_launch_response,
    create_game,
    get_active_game_by_id,
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
from bot_modules.games.utils.launch_guard import refuse_launch
from bot_modules.games.utils.live_bar import LiveBarUpdater
from bot_modules.games.utils.question_source import (
    get_nhie_statement,
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
    launch_pacing,
    round_cap_reached,
    seconds_left,
)
from bot_modules.games_nhie.embeds import (
    build_recap_embed,
    build_round_embed,
)
from bot_modules.games_nhie.logic import (
    DEFAULT_LIVES,
    MAX_QUEUED_STATEMENTS,
    apply_round_lives,
    apply_vote,
    bump_guilt_scores,
    encode_round_state,
    find_winner,
    payload_to_round_state,
    queue_statement,
)

log = logging.getLogger(__name__)

# After a restart, a timed round that already ran out still gets a moment
# for the room to see the board before it auto-advances.
_RECOVERY_GRACE_SECONDS = 5.0


class PoseStatementModal(discord.ui.Modal, title="Pose a Statement"):
    statement = discord.ui.TextInput(
        label="Never have I ever…",
        placeholder="e.g. gone skydiving",
        style=discord.TextStyle.paragraph,
        max_length=300,
    )

    def __init__(self, view, message: discord.Message):
        super().__init__()
        self._view = view
        self._message = message

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, "Pose a Statement", channel_name(interaction.channel))
        if self._view._closed:
            await interaction.response.send_message("This round already ended.", ephemeral=True)
            return
        text = self.statement.value.strip()
        if not text:
            await interaction.response.send_message("A statement is required.", ephemeral=True)
            return
        if self._view.waiting:
            # The bank had nothing to serve, so this statement *is* the round.
            await self._view.begin_round(text, self._message)
            await interaction.response.send_message("✅ Your statement opened the round!", ephemeral=True)
            return
        # Same cap as WYR's and MLT's Pose queues (vote-games-62).
        count = queue_statement(self._view.queued_statements, text)
        if count is None:
            await interaction.response.send_message(
                f"The statement queue is full ({MAX_QUEUED_STATEMENTS}). Let some play first!",
                ephemeral=True,
            )
            return
        self._view.next_btn.label = f"⏭️ Next ({count} queued)"
        try:
            await self._message.edit(view=self._view)
        except discord.HTTPException:
            pass
        await interaction.response.send_message("✅ Your statement has been queued!", ephemeral=True)


class NHIERoundView(discord.ui.View):
    def __init__(
        self,
        game_id: str,
        host_id: int,
        statement: str,
        round_num: int,
        db,
        bot,
        host_name: str,
        advance_callback,
        lives: dict[int, int] | None = None,
        eliminated: set[int] | None = None,
        guild=None,
        max_lives: int = DEFAULT_LIVES,
        accent: discord.Color | None = None,
        *,
        pacing: RoundPacing | None = None,
        finish_callback=None,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.statement = statement
        self.round_num = round_num
        self.db = db
        self.bot = bot
        self.host_name = host_name
        self.advance_callback = advance_callback
        self.finish_callback = finish_callback
        self.guilty: list[int] = []
        self.innocent: list[int] = []
        self.queued_statements: list[str] = []
        self._updater = LiveBarUpdater()
        self._closed = False
        self.lives = lives or {}
        self.eliminated = eliminated or set()
        self.guild = guild
        self.max_lives = max_lives
        # Guild accent, resolved once at view-construction time and reused
        # for every round-embed edit — never re-resolved on the per-vote path.
        self.accent = accent
        self.pacing = pacing or RoundPacing()
        # force_end_active_game pokes this alias to wake a timed round.
        self._advanced_event = self.pacing.advanced
        self.message: discord.Message | None = None
        # No statement yet (the bank had nothing to serve): only Pose, End and
        # Help are live until someone poses one (vote-games-50).
        self.waiting = not statement
        if self.waiting:
            self._set_round_controls(enabled=False)

    def _set_round_controls(self, *, enabled: bool) -> None:
        for item in (self.vote_guilty, self.vote_innocent, self.next_btn):
            item.disabled = not enabled

    def _build_embed(self, closed=False) -> discord.Embed:
        return build_round_embed(
            statement=self.statement,
            guilty=self.guilty,
            innocent=self.innocent,
            round_num=self.round_num,
            closed=closed,
            lives=self.lives,
            eliminated=self.eliminated,
            guild=self.guild,
            max_lives=self.max_lives,
            color=self.accent,
            waiting=self.waiting,
            advance_at=self.pacing.advance_at(),
        )

    async def persist_votes(self) -> None:
        """Write this round's tallies on every vote, not only on Next, so a
        restart mid-round rebuilds the bars it shows (vote-games-58)."""
        guilty, innocent = list(self.guilty), list(self.innocent)

        def _save(payload):
            rd = payload.setdefault("rounds", {}).setdefault(str(self.round_num), {})
            rd["guilty"] = guilty
            rd["innocent"] = innocent

        await modify_payload(self.db, self.game_id, _save)

    async def begin_round(self, statement: str, message: discord.Message) -> None:
        """A posed statement starts a round that was waiting for one."""
        self.statement = statement
        self.waiting = False
        self._set_round_controls(enabled=True)
        opened = self.pacing.open()

        def _save(payload):
            rd = payload.setdefault("rounds", {}).setdefault(str(self.round_num), {})
            rd["stmt"] = statement
            rd["opened_at"] = opened

        await modify_payload(self.db, self.game_id, _save)
        self.message = message
        self.pacing.start_timer(lambda: self.advance_callback(message))
        try:
            await message.edit(embed=self._build_embed(), view=self)
        except discord.HTTPException:
            pass

    async def _vote(self, interaction: discord.Interaction, kind: str, label: str) -> None:
        log.info("%s voted in game %s in #%s", interaction.user.display_name, self.game_id, channel_name(interaction.channel))
        if self._closed or self.waiting:
            await interaction.response.send_message("This round is over." if self._closed else "No statement yet — pose one first!", ephemeral=True)
            return
        uid = interaction.user.id
        if uid in self.eliminated:
            await interaction.response.send_message("💀 You've been eliminated!", ephemeral=True)
            return
        changed = apply_vote(
            self.guilty, self.innocent, self.lives, uid, kind, self.max_lives,  # type: ignore[arg-type]
        )
        msg = f"✅ Voted **{label}**{' (changed)' if changed else ''}"
        await interaction.response.send_message(msg, ephemeral=True, delete_after=3)
        await self.persist_votes()
        await self._updater.schedule_update(interaction.message, self._build_embed)

    @discord.ui.button(label="😈 Guilty", style=discord.ButtonStyle.danger, custom_id="nhie_guilty", row=0)
    async def vote_guilty(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._vote(interaction, "guilty", "😈 Guilty")

    @discord.ui.button(label="😇 Innocent", style=discord.ButtonStyle.success, custom_id="nhie_innocent", row=0)
    async def vote_innocent(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._vote(interaction, "innocent", "😇 Innocent")

    @discord.ui.button(label="✍️ Pose Statement", style=discord.ButtonStyle.primary, custom_id="nhie_pose", row=1)
    async def pose_statement(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if self._closed:
            await interaction.response.send_message("This round is over.", ephemeral=True)
            return
        assert interaction.message  # component interactions always carry their message
        await interaction.response.send_modal(PoseStatementModal(self, interaction.message))

    @discord.ui.button(label="⏭️ Next", style=discord.ButtonStyle.secondary, custom_id="nhie_next", row=1)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        uid = interaction.user.id
        refusal = await advance_check(
            interaction, host_id=self.host_id, db=self.db, pacing=self.pacing,
            has_voted=uid in self.guilty or uid in self.innocent,
        )
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        if self._closed:
            await interaction.response.send_message("This round is already over.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.advance_callback(interaction.message)

    @discord.ui.button(label="🏁 End Game", style=discord.ButtonStyle.secondary, custom_id="nhie_end", row=2)
    async def end_game_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Close the game with its guilt board and pay the room (vote-games-52)."""
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

    @discord.ui.button(label="❓ Help", style=discord.ButtonStyle.secondary, custom_id="nhie_htp", row=2)
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["nhie"], ephemeral=True)


class NHIECog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(name="nhie", description=play_description("nhie"))
    @app_commands.describe(
        question="Opening statement (e.g. 'gone skydiving') — defaults to question bank",
        lives="Number of lives per player (default 3, 0 = no elimination)",
        tags="Comma-separated tags to filter the question bank",
        round_seconds="Seconds per round before it advances itself (0 = you press Next; default from the dashboard)",
        rounds="How many rounds before the recap (0 = until you end it; default from the dashboard)",
    )
    async def nhie(
        self,
        interaction: discord.Interaction,
        question: str = "",
        lives: int = DEFAULT_LIVES,
        tags: str = "",
        round_seconds: app_commands.Range[int, 0, MAX_ROUND_SECONDS] | None = None,
        rounds: app_commands.Range[int, 0, MAX_ROUNDS_CAP] | None = None,
    ):
        log.info("%s used /games play nhie in #%s", interaction.user.display_name, channel_name(interaction.channel))
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        # The one launch guard every door shares: allowed channel, enabled
        # dial, no game already running here, and a bank with something to
        # serve unless the host brought their own statement.
        refusal = await refuse_launch(
            self.db, interaction, "nhie",
            tags=tag_list, host_supplied=bool(question.strip()),
        )
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={
                "question": question, "lives": lives, "tags": tag_list,
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
        lives = max(0, min(int(options.get("lives", DEFAULT_LIVES)), 10))
        guild = getattr(channel, "guild", None)

        pacing = await launch_pacing(self.db, "nhie", guild_id, options)
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "nhie",
            state="playing",
            payload={
                "rounds": {}, "guilt_scores": {}, "lives": {}, "eliminated": [], "max_lives": lives,
                "tags": options.get("tags") or [],
                "round_seconds": pacing.round_seconds, "max_rounds": pacing.max_rounds,
                "scheduled": is_scheduled_launch(options, host_id),
            },
            guild_id=guild_id,
        )
        log.info("Game %s (nhie) created by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))

        try:
            await self._run_round(
                interaction=None,
                game_id=game_id,
                host_id=host_id,
                host_name=host_name,
                round_num=1,
                channel=channel,
                guild=guild,
                custom_statement=question or None,
                max_lives=lives,
            )
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("nhie launch lacked send perms in channel %s", channel.id)
            return None
        # _run_round can end the game and unwind normally (a send that failed
        # for a reason other than permissions). Reporting a game id for that
        # would mark the schedule 'launched' and ping under a dead board. A
        # missing row is the signal the round bailed.
        if await get_active_game_by_id(self.db, game_id) is None:
            return None
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id

    @staticmethod
    def _roster_from(payload: dict) -> list[int]:
        """Everyone who played: the lives tracker (which keeps eliminated
        players at 0 hp) plus every voter in any round — the latter is the
        whole roster when lives are off and the tracker stays empty."""
        ids = {int(k) for k in (payload.get("lives") or {})}
        for rd in (payload.get("rounds") or {}).values():
            if isinstance(rd, dict):
                ids.update(int(v) for v in (rd.get("guilty") or []) + (rd.get("innocent") or []))
        return sorted(ids)

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
        guild,
        custom_statement: str | None = None,
        carry_over_queue: list[str] | None = None,
        lives: dict[int, int] | None = None,
        eliminated: set[int] | None = None,
        max_lives: int = DEFAULT_LIVES,
    ):
        payload = await get_game_payload(self.db, game_id)
        if custom_statement:
            statement = custom_statement
        else:
            tags = payload.get("tags") or None
            statement = await get_nhie_statement(
                self.db, tags=tags, allow_nsfw=channel_allows_nsfw(channel)
            ) or ""
        # Nothing to serve: the round opens *waiting* for a posed statement
        # rather than ending the game (vote-games-50).
        waiting = not statement

        if lives is None:
            lives, eliminated, max_lives = payload_to_round_state(payload)
        if eliminated is None:
            eliminated = set()

        pacing = self._pacing_from_payload(payload, None)
        if not waiting:
            pacing.open()
        rounds_data = payload.setdefault("rounds", {})
        rounds_data[str(round_num)] = {
            "guilty": [], "innocent": [], "stmt": statement, "opened_at": pacing.opened_at,
        }
        await update_game_payload(self.db, game_id, payload)

        accent = await safe_resolve_accent(self.bot, guild, log_label="nhie")
        view = self._build_round_view(
            game_id=game_id,
            host_id=host_id,
            host_name=host_name,
            round_num=round_num,
            channel=channel,
            guild=guild,
            statement=statement,
            lives=lives,
            eliminated=eliminated,
            max_lives=max_lives,
            interaction=interaction,
            accent=accent,
            pacing=pacing,
        )
        if carry_over_queue:
            view.queued_statements = carry_over_queue
            count = len(carry_over_queue)
            view.next_btn.label = f"⏭️ Next ({count} queued)"
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
        guild,
        statement: str,
        lives: dict[int, int] | None,
        eliminated: set[int] | None,
        max_lives: int,
        interaction=None,
        accent: discord.Color | None = None,
        pacing: RoundPacing | None = None,
    ) -> "NHIERoundView":
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

        async def resolve_round() -> tuple[dict, dict[int, int], set[int], list[int]]:
            """Write the round's votes, guilt and lives; return the new state."""
            payload = await get_game_payload(self.db, game_id)
            rd = payload.setdefault("rounds", {}).setdefault(str(round_num), {})
            rd["guilty"] = list(view.guilty)
            rd["innocent"] = list(view.innocent)
            guilt_scores = payload.setdefault("guilt_scores", {})
            bump_guilt_scores(guilt_scores, view.guilty)

            current_lives, current_eliminated, _ = payload_to_round_state(payload)
            newly_eliminated = apply_round_lives(
                current_lives, current_eliminated, view.guilty, view.innocent, max_lives,
            )
            lives_serialized, eliminated_serialized = encode_round_state(
                current_lives, current_eliminated
            )
            payload["lives"] = lives_serialized
            payload["eliminated"] = eliminated_serialized
            await update_game_payload(self.db, game_id, payload)
            return payload, current_lives, current_eliminated, newly_eliminated

        async def finish(message: discord.Message | None, reason: str = REASON_HOST_ENDED) -> None:
            """End with the guilt board through the paying path — the host's
            End Game, the round cap, an expired game at Next, and /games end."""
            if not view._closed:
                await close_round(message)
                if not view.waiting:
                    await resolve_round()
            await self._finish_game(game_id, channel, guild, reason=reason)

        async def advance(message: discord.Message) -> None:
            if view._closed:
                return
            await close_round(message)
            payload, current_lives, current_eliminated, newly_eliminated = await resolve_round()

            # Announce eliminations
            for uid in newly_eliminated:
                name = resolve_name(guild, uid)
                try:
                    await channel.send(
                        f"💀 **{discord.utils.escape_markdown(name)}** has been eliminated!",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass

            if max_lives > 0:
                status, winner_id = find_winner(current_lives, current_eliminated)
                if status != "continue":
                    guilt_scores = payload.get("guilt_scores", {})
                    try:
                        if status == "winner":
                            embed = build_recap_embed(
                                winner_id=winner_id,
                                guilt_scores=guilt_scores,
                                guild=guild,
                                color=view.accent,
                            )
                            if guild:
                                from bot_modules.economy.game_rewards import append_payout_footer
                                await append_payout_footer(self.bot, embed, guild.id, "nhie")
                            await channel.send(embed=embed)
                        else:
                            await channel.send(
                                f"{GAME_ICONS['nhie']} Everyone's been eliminated! No winner this time."
                            )
                    except discord.HTTPException:
                        pass
                    # Roster = everyone who played. ``current_lives`` retains
                    # eliminated players (at 0 hp), so it is the full participant
                    # set — the guiltiest winner may have been eliminated, and a
                    # survivors-only roster would drop their win bonus + credit.
                    await end_game(self.db, game_id, player_count=len(current_lives), round_count=round_num, payload=payload,
                                   bot=self.bot, player_ids=sorted(current_lives))
                    if game_id in self.bot.active_views:
                        del self.bot.active_views[game_id]
                    return

            if await is_game_expired(self.db, game_id):
                # Past the 24h line: end with the guilt board and pay the room
                # (vote-games-59 — this used to be a bare, guild-0 end).
                await self._finish_game(game_id, channel, guild, reason=REASON_EXPIRED)
                return

            if round_cap_reached(round_num, view.pacing.max_rounds):
                await self._finish_game(game_id, channel, guild, reason=REASON_ROUND_CAP)
                return

            remaining = list(view.queued_statements)
            next_custom = remaining.pop(0) if remaining else None
            try:
                await self._run_round(
                    interaction=interaction,
                    game_id=game_id,
                    host_id=host_id,
                    host_name=host_name,
                    round_num=round_num + 1,
                    channel=channel,
                    guild=guild,
                    custom_statement=next_custom,
                    carry_over_queue=remaining if remaining else None,
                    lives=current_lives,
                    eliminated=current_eliminated,
                    max_lives=max_lives,
                )
            except Exception:
                log.exception("Error advancing NHIE game %s to round %d", game_id, round_num + 1)
                await end_game(self.db, game_id, reason="crash")
                self.bot.active_views.pop(game_id, None)
                try:
                    await channel.send("❌ Something went wrong advancing the round. Game ended.")
                except discord.HTTPException:
                    pass

        view = NHIERoundView(
            game_id=game_id,
            host_id=host_id,
            statement=statement,
            round_num=round_num,
            db=self.db,
            bot=self.bot,
            host_name=host_name,
            # pyright reports a circular inference here (advance captures
            # `view`, whose initializer takes `advance`); the closure itself
            # is fully annotated above.
            advance_callback=advance,  # pyright: ignore[reportGeneralTypeIssues]
            lives=lives,
            eliminated=eliminated,
            guild=guild,
            max_lives=max_lives,
            accent=accent,
            pacing=pacing,
            finish_callback=finish,
        )
        return view

    async def _finish_game(self, game_id: str, channel, guild, *, reason: str) -> bool:
        """Post the guilt board and end through the paying path.

        Returns False when the game had already been ended by another path
        (``end_game``'s DELETE claim makes the payout exactly-once either way).
        """
        row = await get_active_game_by_id(self.db, game_id)
        if row is None:
            self.bot.active_views.pop(game_id, None)
            return False
        payload = await get_game_payload(self.db, game_id)
        roster = self._roster_from(payload)
        accent = await safe_resolve_accent(self.bot, guild, log_label="nhie")
        embed = build_recap_embed(
            winner_id=None, guilt_scores=payload.get("guilt_scores", {}),
            guild=guild, color=accent, ended=True, reason=reason,
        )
        if guild:
            from bot_modules.economy.game_rewards import append_payout_footer
            await append_payout_footer(self.bot, embed, guild.id, "nhie")
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            log.info("NHIE recap not delivered for game %s", game_id)
        rounds = payload.get("rounds", {})
        played = sum(1 for rd in rounds.values() if isinstance(rd, dict) and rd.get("stmt"))
        ended = await end_game(
            self.db, game_id,
            player_count=len(roster), round_count=played, payload=payload,
            bot=self.bot, player_ids=roster, reason=reason,
        )
        self.bot.active_views.pop(game_id, None)
        return ended is not None

    async def end_with_recap(self, channel, game_id: str) -> bool:
        """``/games end`` on a Never Have I Ever game: the same guilt-board
        ending as the host's 🏁 End Game, instead of the red Force-Closed card."""
        view = self.bot.active_views.get(game_id)
        if isinstance(view, NHIERoundView) and view.finish_callback is not None:
            await view.finish_callback(view.message, REASON_HOST_ENDED)
            return True
        return await self._finish_game(
            game_id, channel, getattr(channel, "guild", None), reason=REASON_HOST_ENDED,
        )

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Rebuild the current round's view after a restart, restoring state."""
        rounds = payload.get("rounds", {})
        if not rounds:
            return False
        cur = max(rounds, key=lambda k: int(k))
        rd = rounds.get(cur, {})
        statement = rd.get("stmt", "") or ""

        game_id = row["game_id"]
        host_id = int(row["host_id"])
        guild = getattr(channel, "guild", None)
        host_name = resolve_name(guild, host_id) if guild else "Host"
        lives, eliminated, max_lives = payload_to_round_state(payload)

        accent = await safe_resolve_accent(self.bot, guild, log_label="nhie")
        pacing = self._pacing_from_payload(payload, rd.get("opened_at"))
        view = self._build_round_view(
            game_id=game_id,
            host_id=host_id,
            host_name=host_name,
            round_num=int(cur),
            channel=channel,
            guild=guild,
            statement=statement,
            lives=lives,
            eliminated=eliminated,
            max_lives=max_lives,
            interaction=None,
            accent=accent,
            pacing=pacing,
        )
        view.guilty = list(rd.get("guilty", []))
        view.innocent = list(rd.get("innocent", []))
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
        log.info("Recovered nhie game %s (round %s) in #%s", game_id, cur, getattr(channel, "name", channel.id))
        return True


async def setup(bot: "Bot"):
    cog = NHIECog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("nhie")
    play.add_command(cog.nhie, override=True)
    bot.game_launchers["nhie"] = cog.launch
    bot.game_recoverers["nhie"] = cog.recover_game
