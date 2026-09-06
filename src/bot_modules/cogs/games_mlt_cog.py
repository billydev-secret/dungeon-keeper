import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord

from bot_modules.core.branding import safe_resolve_accent
from bot_modules.services.name_resolver import NameFn, build_name_fn
from bot_modules.services.no_contact_service import is_no_contact
from bot_modules.services.game_start_ping_service import (
    extract_start_epoch,
)
from bot_modules.core.utils import disable_all_items, is_host_or_mod
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import HOW_TO_PLAY, play_description
from bot_modules.games.command_groups import play
from bot_modules.games.utils.game_manager import (
    ConfirmCloseView,
    finish_launch_response,
    create_game,
    get_active_game_by_id,
    update_game_message,
    update_game_payload,
    update_game_state,
    modify_payload,
    get_game_payload,
    end_game,
    update_session,
    is_game_expired,
    resolve_name,
    resolve_names,
    channel_name,
)
from bot_modules.games.utils.launch_guard import refuse_launch
from bot_modules.games.utils.question_source import (
    get_mlt_prompt,
    channel_allows_nsfw,
)
from bot_modules.games.utils.round_pacing import (
    END_DENIED,
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
from bot_modules.games_mlt.embeds import (
    build_final_standings_embed,
    build_join_embed,
    build_results_embed,
    build_round_embed,
)
from bot_modules.games_mlt.logic import (
    MAX_PLAYERS,
    MIN_PLAYERS,
    add_player,
    clamp_player_limits,
    bump_crowns,
    can_start,
    encode_round_votes,
    find_round_winners,
    is_eligible_voter,
    lobby_is_full,
    pop_next_prompt,
    queue_prompt,
    record_vote,
    remove_player,
    tally_votes,
)

log = logging.getLogger(__name__)

# Cap the player-submitted prompt queue to prevent flooding.
_MAX_QUEUED_PROMPTS = 15

# After a restart, a timed round that already ran out still gets a moment
# for the room to see the board before it auto-advances.
_RECOVERY_GRACE_SECONDS = 5.0

REASON_TOO_FEW_PLAYERS = "too_few_players"


class MLTJoinView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, db, bot, cog, accent=None):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self.cog = cog
        # Guild accent resolved once at view creation; reused on every
        # Join/Leave press so we never re-resolve per interaction.
        self.accent = accent

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="mlt_join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        log.info("%s joined game %s in #%s", interaction.user.display_name, self.game_id, channel_name(interaction.channel))
        payload = await get_game_payload(self.db, self.game_id)
        players = payload.setdefault("players", [])
        limit = int(payload.get("max_players") or MAX_PLAYERS)
        if interaction.user.id not in players and lobby_is_full(players, limit):
            await interaction.response.send_message(
                f"❌ This lobby is full — this game is set to take up to "
                f"{limit} players.",
                ephemeral=True,
            )
            return
        add_player(players, interaction.user.id, limit)
        await update_game_payload(self.db, self.game_id, payload)

        guild = interaction.guild
        names = resolve_names(guild, players)
        host_member = guild.get_member(self.host_id) if guild else None
        embed = build_join_embed(
            host_member.display_name if host_member else "Host",
            names,
            color=self.accent,
            # Re-read from the payload, not the view: the countdown must
            # survive a restart that rebuilt this view from the DB.
            start_at=extract_start_epoch(payload),
        )
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send("✅ You've joined the pool!", ephemeral=True)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, custom_id="mlt_leave")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        log.info("%s left game %s in #%s", interaction.user.display_name, self.game_id, channel_name(interaction.channel))
        payload = await get_game_payload(self.db, self.game_id)
        players = payload.setdefault("players", [])
        remove_player(players, interaction.user.id)
        await update_game_payload(self.db, self.game_id, payload)

        guild = interaction.guild
        names = resolve_names(guild, players)
        host_member = guild.get_member(self.host_id) if guild else None
        embed = build_join_embed(
            host_member.display_name if host_member else "Host",
            names,
            color=self.accent,
            # Re-read from the payload, not the view: the countdown must
            # survive a restart that rebuilt this view from the DB.
            start_at=extract_start_epoch(payload),
        )
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send("✅ You've left the pool.", ephemeral=True)

    @discord.ui.button(label="Start", style=discord.ButtonStyle.primary, custom_id="mlt_start")
    async def start_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can start.", ephemeral=True)
            return
        payload = await get_game_payload(self.db, self.game_id)
        players = payload.get("players", [])
        floor = int(payload.get("min_players") or MIN_PLAYERS)
        if not can_start(players, floor):
            await interaction.response.send_message(
                f"❌ Need at least {floor} players to start!", ephemeral=True
            )
            return

        self.stop()
        disable_all_items(self)
        await interaction.response.edit_message(view=self)

        # Ping joined players
        if interaction.guild:
            mentions = [
                member.mention
                for uid in players
                if (member := interaction.guild.get_member(uid)) is not None
            ]
            if mentions:
                channel = interaction.channel
                assert isinstance(channel, discord.abc.Messageable)
                await channel.send(
                    f"👑 **Most Likely To is starting!** {' '.join(mentions)} — get ready!",
                    delete_after=15,
                )

        # The row must stop reading as an open lobby — the start-ping sweep
        # polls state='joining' and a game outlives its countdown.
        await update_game_state(self.db, self.game_id, "playing")

        await self.cog._run_round(
            interaction=interaction,
            game_id=self.game_id,
            host_id=self.host_id,
            host_name=interaction.user.display_name,
            round_num=1,
            players=players,
            channel=interaction.channel,
            custom_prompt=payload.get("opening_prompt"),
            accent=self.accent,
        )

    @discord.ui.button(label="❓ Help", style=discord.ButtonStyle.secondary, custom_id="mlt_htp")
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["mlt"], ephemeral=True)


class PoseMLTModal(discord.ui.Modal, title="Pose a Prompt"):
    prompt = discord.ui.TextInput(
        label="Most likely to…",
        placeholder="e.g. win a staring contest",
        style=discord.TextStyle.paragraph,
        max_length=300,
    )

    def __init__(self, view, message: discord.Message):
        super().__init__()
        self._view = view
        self._message = message

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, "Pose a Prompt", channel_name(interaction.channel))
        if self._view._closed:
            await interaction.response.send_message("This round already ended.", ephemeral=True)
            return
        text = self.prompt.value.strip()
        if not text:
            await interaction.response.send_message("A prompt is required.", ephemeral=True)
            return
        if self._view.waiting:
            # The bank had nothing to serve, so this prompt *is* the round.
            await self._view.begin_round(text, self._message)
            await interaction.response.send_message("✅ Your prompt opened the round!", ephemeral=True)
            return
        if len(self._view.queued_prompts) >= _MAX_QUEUED_PROMPTS:
            await interaction.response.send_message(
                f"The prompt queue is full ({_MAX_QUEUED_PROMPTS}). Let some play first!",
                ephemeral=True,
            )
            return
        count = queue_prompt(self._view.queued_prompts, text)
        self._view.next_btn.label = f"⏭️ Next ({count} queued)"
        try:
            await self._message.edit(view=self._view)
        except discord.HTTPException:
            pass
        await interaction.response.send_message("✅ Your prompt has been queued!", ephemeral=True)


class MLTVoteView(discord.ui.View):
    def __init__(
        self,
        game_id: str,
        host_id: int,
        prompt: str,
        round_num: int,
        players: list[int],
        db,
        bot,
        host_name: str,
        guild,
        advance_callback,
        accent=None,
        *,
        pacing: RoundPacing | None = None,
        finish_callback=None,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.prompt = prompt
        self.round_num = round_num
        self.players = players
        self.db = db
        self.bot = bot
        self.host_name = host_name
        self.guild = guild
        self.advance_callback = advance_callback
        self.finish_callback = finish_callback
        # Guild accent resolved once at view creation; reused on every
        # vote/edit so we never re-resolve per interaction.
        self.accent = accent
        self.votes: dict[int, int] = {}
        self._closed = False
        self.queued_prompts: list[str] = []
        self.pacing = pacing or RoundPacing()
        # force_end_active_game pokes this alias to wake a timed round.
        self._advanced_event = self.pacing.advanced
        self.message: discord.Message | None = None
        # No prompt yet (the bank had nothing to serve): only Pose, End and
        # Help are live until someone poses one (vote-games-50).
        self.waiting = not prompt

        options = []
        # A Discord Select allows at most 25 options; the lobby is capped
        # at MAX_PLAYERS join-side, but slice defensively so a stale/over-
        # sized roster can never 400 the round message.
        for uid in players[:25]:
            member = guild.get_member(uid) if guild else None
            name = member.display_name if member else str(uid)
            options.append(discord.SelectOption(label=name, value=str(uid)))
        self.select = discord.ui.Select(
            placeholder="🗳️ Vote: Pick a player…",
            options=options,
            custom_id="mlt_vote_select",
        )
        self.select.callback = self._vote_select_callback
        self.add_item(self.select)
        if self.waiting:
            self._set_round_controls(enabled=False)

    def _set_round_controls(self, *, enabled: bool) -> None:
        self.select.disabled = not enabled
        self.next_btn.disabled = not enabled

    async def begin_round(self, prompt: str, message: discord.Message) -> None:
        """A posed prompt starts a round that was waiting for one."""
        self.prompt = prompt
        self.waiting = False
        self._set_round_controls(enabled=True)
        opened = self.pacing.open()

        def _save(payload):
            rd = payload.setdefault("rounds", {}).setdefault(str(self.round_num), {})
            rd["prompt"] = prompt
            rd["opened_at"] = opened

        await modify_payload(self.db, self.game_id, _save)
        self.message = message
        self.pacing.start_timer(lambda: self.advance_callback(message))
        try:
            await message.edit(embed=self._build_embed(), view=self)
        except discord.HTTPException:
            pass

    async def _vote_select_callback(self, interaction: discord.Interaction):
        log.info("%s voted in game %s in #%s", interaction.user.display_name, self.game_id, channel_name(interaction.channel))
        if self._closed or self.waiting:
            await interaction.response.send_message("This round is over." if self._closed else "No prompt yet — pose one first!", ephemeral=True)
            return
        if not is_eligible_voter(interaction.user.id, self.players):
            await interaction.response.send_message("You're not in the player pool.", ephemeral=True)
            return
        values = (interaction.data or {}).get("values") or []
        target_id = int(values[0])
        voter_id = interaction.user.id
        guild_id = interaction.guild_id or 0

        # The no-contact gate: a blocked pair's pick is dropped, and the ack
        # below is sent exactly as for a counted vote (docs/no_contact_spec.md).
        blocked = await asyncio.to_thread(
            is_no_contact, self.bot.ctx.db_path, guild_id, voter_id, target_id
        )
        counted, changed = record_vote(self.votes, voter_id, target_id, blocked=blocked)

        if counted:
            # Persist live votes so a crash mid-round doesn't lose them.
            def _save(payload):
                rounds = payload.setdefault("rounds", {})
                rd = rounds.setdefault(str(self.round_num), {})
                rd["votes"] = encode_round_votes(self.votes)

            await modify_payload(self.db, self.game_id, _save)

        member = self.guild.get_member(target_id) if self.guild else None
        name = member.display_name if member else str(target_id)
        msg = f"✅ Voted for **{name}**{' (changed)' if changed else ''}"
        await interaction.response.send_message(msg, ephemeral=True)

    def _build_embed(self, closed=False) -> discord.Embed:
        return build_round_embed(
            prompt=self.prompt,
            round_num=self.round_num,
            vote_count=len(self.votes),
            closed=closed,
            color=self.accent,
            waiting=self.waiting,
            advance_at=self.pacing.advance_at(),
        )

    def _build_results_embed(
        self, tally: dict, name_fn: NameFn, winners: list[int] | None = None,
    ) -> discord.Embed:
        return build_results_embed(
            prompt=self.prompt,
            round_num=self.round_num,
            tally=tally,
            color=self.accent,
            name_fn=name_fn,
            winners=winners,
        )

    @discord.ui.button(label="✍️ Pose Prompt", style=discord.ButtonStyle.primary, custom_id="mlt_pose", row=1)
    async def pose_prompt(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if self._closed:
            await interaction.response.send_message("This round is over.", ephemeral=True)
            return
        assert interaction.message
        await interaction.response.send_modal(PoseMLTModal(self, interaction.message))

    @discord.ui.button(label="⏭️ Next", style=discord.ButtonStyle.secondary, custom_id="mlt_next", row=1)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        refusal = await advance_check(
            interaction, host_id=self.host_id, db=self.db, pacing=self.pacing,
            has_voted=interaction.user.id in self.votes,
        )
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        if self._closed:
            await interaction.response.send_message("This round is already over.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.advance_callback(interaction.message)

    @discord.ui.button(label="🏁 End Game", style=discord.ButtonStyle.secondary, custom_id="mlt_end", row=1)
    async def end_game_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Close the game with its final standings and pay the room (vote-games-52)."""
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

    @discord.ui.button(label="❓ Help", style=discord.ButtonStyle.secondary, custom_id="mlt_htp2", row=1)
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["mlt"], ephemeral=True)


class MLTCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(name="mlt", description=play_description("mlt"))
    @app_commands.describe(
        question="Opening prompt (e.g. 'win a staring contest') — defaults to question bank",
        tags="Comma-separated tags to filter the question bank",
        start_in="Countdown before the game starts, in minutes",
    )
    async def mlt(
        self,
        interaction: discord.Interaction,
        question: str = "",
        tags: str = "",
        start_in: app_commands.Range[int, 1, 60] | None = None,
    ):
        # These moved to the dashboard. Passing None is exactly what an
        # omitted option always meant, so the per-guild dial applies.
        round_seconds = rounds = None
        log.info("%s used /games play mlt in #%s", interaction.user.display_name, channel_name(interaction.channel))
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        # The one launch guard every door shares: allowed channel, enabled
        # dial, no game already running here, and a bank with something to
        # serve unless the host brought their own prompt (platform-27: a bare
        # /mlt used to fill a lobby and die at Start on an empty bank).
        refusal = await refuse_launch(
            self.db, interaction, "mlt",
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
                "question": question, "tags": tag_list, "start_in": start_in,
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
        question = options.get("question", "")
        # One read of the dial row covers the pacing and the roster limits.
        pacing = await launch_pacing(self.db, "mlt", guild_id, options)
        game_opts = pacing.game_opts
        # The two dials this game actually has somewhere to enforce: it is one
        # of only two with a join phase. Clamped on the way in so a value saved
        # before the dashboard bounded them cannot outgrow the vote Select.
        min_players, max_players = clamp_player_limits(
            options.get("min_players", game_opts.get("min_players", MIN_PLAYERS)),
            options.get("max_players", game_opts.get("max_players", MAX_PLAYERS)),
        )
        start_epoch = pacing.start_epoch
        payload = pacing.stamp({
            "opening_prompt": question.strip() or None, "rounds": {}, "crowns": {}, "players": [],
            "tags": options.get("tags") or [],
            "min_players": min_players, "max_players": max_players,
            "round_seconds": pacing.round_seconds, "max_rounds": pacing.max_rounds,
            "scheduled": is_scheduled_launch(options, host_id),
        })
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "mlt",
            state="joining",
            payload=payload,
            guild_id=guild_id,
        )

        log.info("Game %s (mlt) created by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))
        accent = await safe_resolve_accent(self.bot, getattr(channel, "guild", None), log_label="MLT")
        embed = build_join_embed(host_name, [], color=accent, start_at=start_epoch)
        view = MLTJoinView(game_id, host_id, self.db, self.bot, self, accent=accent)
        self.bot.active_views[game_id] = view

        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("mlt launch lacked send perms in channel %s", channel.id)
            return None
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id

    @staticmethod
    def _voter_roster_from(payload: dict) -> list[int]:
        """Everyone who cast a vote in any round — the real participant set
        for economy payouts (survivors-only ``players`` would drop members who
        voted for several rounds then left)."""
        return sorted({
            int(v)
            for rd in payload.get("rounds", {}).values()
            for v in (rd.get("votes") or {})
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
        players: list[int],
        channel,
        custom_prompt: str | None = None,
        carry_over_queue: list[str] | None = None,
        accent=None,
    ):
        payload = await get_game_payload(self.db, game_id)
        if custom_prompt:
            prompt = custom_prompt
        else:
            tags = payload.get("tags") or None
            prompt = await get_mlt_prompt(
                self.db, tags=tags, allow_nsfw=channel_allows_nsfw(channel)
            ) or ""
        # Nothing to serve: the round opens *waiting* for a posed prompt
        # rather than ending the game (vote-games-50).
        waiting = not prompt

        pacing = self._pacing_from_payload(payload, None)
        if not waiting:
            pacing.open()
        rounds_data = payload.setdefault("rounds", {})
        rounds_data[str(round_num)] = {"votes": {}, "prompt": prompt, "opened_at": pacing.opened_at}
        await update_game_payload(self.db, game_id, payload)

        view = self._build_vote_view(
            game_id=game_id,
            host_id=host_id,
            host_name=host_name,
            round_num=round_num,
            players=players,
            channel=channel,
            prompt=prompt,
            interaction=interaction,
            accent=accent,
            pacing=pacing,
        )
        if carry_over_queue:
            view.queued_prompts = carry_over_queue
            count = len(carry_over_queue)
            view.next_btn.label = f"⏭️ Next ({count} queued)"
        self.bot.active_views[game_id] = view

        embed = view._build_embed()
        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await end_game(self.db, game_id)
            if game_id in self.bot.active_views:
                del self.bot.active_views[game_id]
            await self._tell_host(
                interaction,
                "❌ I don't have permission to send messages in that channel. "
                "Please grant me **Send Messages** and **Embed Links** permissions.",
            )
            return
        except Exception:
            # Any other send failure (e.g. a 400 from an oversized select or
            # embed) would otherwise leave the view registered and the game
            # row un-ended, so recover_game re-registers the dead lobby every
            # restart. Tear the game down cleanly and tell the host.
            log.exception("mlt: failed to send round message for game %s", game_id)
            await end_game(self.db, game_id, reason="crash")
            if game_id in self.bot.active_views:
                del self.bot.active_views[game_id]
            await self._tell_host(
                interaction,
                "❌ Something went wrong starting that round, so the game "
                "was ended. Please start a new one.",
            )
            return
        view.message = msg
        await update_game_message(self.db, game_id, msg.id)
        if not waiting:
            view.pacing.start_timer(lambda: view.advance_callback(msg))

    @staticmethod
    async def _tell_host(interaction, text: str) -> None:
        """Ephemeral follow-up to whoever pressed Start, when there is one —
        a timer-driven round has no interaction to answer."""
        if interaction is None:
            return
        try:
            await interaction.followup.send(text, ephemeral=True)
        except discord.HTTPException:
            pass

    def _build_vote_view(
        self,
        *,
        game_id: str,
        host_id: int,
        host_name: str,
        round_num: int,
        players: list[int],
        channel,
        prompt: str,
        interaction=None,
        accent=None,
        pacing: RoundPacing | None = None,
    ) -> "MLTVoteView":
        """Construct a vote-round view with its advance and finish callbacks wired.

        Shared by _run_round (fresh round) and recover_game (post-restart) so
        round-to-round advancement behaves identically after a crash. ``accent``
        is resolved once by the caller and reused for every round's embeds.
        """
        guild = getattr(channel, "guild", None)

        async def close_round(message: discord.Message | None) -> dict[int, int]:
            """Close the board and, when anyone voted, post the round's
            results and bank its crowns. Returns the tally."""
            view._closed = True
            # Wakes a timed round's wait (and never cancels it — the timer
            # task may be the caller).
            view.pacing.advanced.set()
            disable_all_items(view)
            if message is not None:
                try:
                    await message.edit(embed=view._build_embed(closed=True), view=view)
                except discord.HTTPException:
                    pass
            if view.waiting:
                return {}
            tally = tally_votes(view.votes, players)
            # Decide the crowns before showing them: a tie at the top is
            # broken without self-votes, and the board must crown exactly
            # what gets banked (vote-games-62).
            winners = find_round_winners(tally, view.votes)
            name_fn = await build_name_fn(
                guild=guild,
                db_path=self.bot.ctx.db_path,
                guild_id=getattr(guild, "id", 0),
                user_ids=list(tally),
            )
            try:
                await channel.send(embed=view._build_results_embed(tally, name_fn, winners))
            except discord.HTTPException:
                pass
            votes = encode_round_votes(view.votes)

            def _save(payload):
                bump_crowns(payload.setdefault("crowns", {}), winners)
                payload.setdefault("rounds", {}).setdefault(str(round_num), {})["votes"] = votes

            await modify_payload(self.db, game_id, _save)
            return tally

        async def finish(message: discord.Message | None, reason: str = REASON_HOST_ENDED) -> None:
            """End with the final standings through the paying path — the
            host's End Game, the round cap, an expired game at Next, and
            /games end."""
            if not view._closed:
                await close_round(message)
            await self._finish_game(game_id, channel, reason=reason)

        async def advance(message: discord.Message) -> None:
            if view._closed:
                return
            await close_round(message)

            if await is_game_expired(self.db, game_id):
                # Past the 24h line: end with the standings and pay the room
                # (vote-games-59 — this used to be a bare, guild-0 end).
                await self._finish_game(game_id, channel, reason=REASON_EXPIRED)
                return

            if round_cap_reached(round_num, view.pacing.max_rounds):
                await self._finish_game(game_id, channel, reason=REASON_ROUND_CAP)
                return

            # Re-read the roster so /games join and /games leave take effect next
            # round. NOTE: keep this round's `players` (used above by tally_votes)
            # untouched — only the next round runs with the updated roster.
            payload = await get_game_payload(self.db, game_id)
            next_players = [int(p) for p in payload.get("players", players)]

            # Mid-game leaves can drop the roster below a playable size — end
            # cleanly rather than trying to build a vote with < 2 candidates.
            if len(next_players) < 2:
                try:
                    await channel.send("🎲 Not enough players left — ending the game.")
                except discord.HTTPException:
                    pass
                await self._finish_game(game_id, channel, reason=REASON_TOO_FEW_PLAYERS)
                return

            next_custom, remaining = pop_next_prompt(view.queued_prompts)
            try:
                await self._run_round(
                    interaction=interaction,
                    game_id=game_id,
                    host_id=host_id,
                    host_name=host_name,
                    round_num=round_num + 1,
                    players=next_players,
                    channel=channel,
                    custom_prompt=next_custom,
                    carry_over_queue=remaining if remaining else None,
                    accent=accent,
                )
            except Exception:
                log.exception("Error advancing MLT game %s to round %d", game_id, round_num + 1)
                await end_game(self.db, game_id, reason="crash")
                self.bot.active_views.pop(game_id, None)
                try:
                    await channel.send("❌ Something went wrong advancing the round. Game ended.")
                except discord.HTTPException:
                    pass

        view = MLTVoteView(
            game_id=game_id,
            host_id=host_id,
            prompt=prompt,
            round_num=round_num,
            players=players,
            db=self.db,
            bot=self.bot,
            host_name=host_name,
            guild=guild,
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
        """Post the final crown standings and end through the paying path.

        Returns False when the game had already been ended by another path
        (``end_game``'s DELETE claim makes the payout exactly-once either way).
        """
        row = await get_active_game_by_id(self.db, game_id)
        if row is None:
            self.bot.active_views.pop(game_id, None)
            return False
        payload = await get_game_payload(self.db, game_id)
        crowns = payload.get("crowns") or {}
        roster = self._voter_roster_from(payload)
        guild = getattr(channel, "guild", None)
        try:
            accent = await safe_resolve_accent(self.bot, guild, log_label="MLT")
            name_fn = await build_name_fn(
                guild=guild,
                db_path=self.bot.ctx.db_path,
                guild_id=getattr(guild, "id", 0),
                user_ids=[int(uid) for uid in crowns],
            )
            embed = build_final_standings_embed(crowns, color=accent, name_fn=name_fn)
            if reason == REASON_ROUND_CAP:
                embed.description = "That's the last round!\n" + (embed.description or "")
            if guild:
                from bot_modules.economy.game_rewards import append_payout_footer
                await append_payout_footer(self.bot, embed, guild.id, "mlt")
            await channel.send(embed=embed)
        except Exception:
            log.exception("MLT: failed to post final standings for %s", game_id)
        rounds = payload.get("rounds", {})
        played = sum(1 for rd in rounds.values() if isinstance(rd, dict) and rd.get("prompt"))
        ended = await end_game(
            self.db, game_id,
            player_count=len(roster), round_count=played, payload=payload,
            bot=self.bot, player_ids=roster, reason=reason,
        )
        self.bot.active_views.pop(game_id, None)
        return ended is not None

    async def end_with_recap(self, channel, game_id: str) -> bool:
        """``/games end`` on a started Most Likely To game: the same standings
        ending as the host's 🏁 End Game, instead of the red Force-Closed card.
        A lobby that never started has nothing to recap and answers False."""
        view = self.bot.active_views.get(game_id)
        if isinstance(view, MLTVoteView) and view.finish_callback is not None:
            await view.finish_callback(view.message, REASON_HOST_ENDED)
            return True
        payload = await get_game_payload(self.db, game_id)
        if not payload.get("rounds"):
            return False
        return await self._finish_game(game_id, channel, reason=REASON_HOST_ENDED)

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Rebuild the current phase's view after a restart.

        Join lobby -> MLTJoinView; a started game -> the current vote round's
        view with its live votes restored.
        """
        game_id = row["game_id"]
        host_id = int(row["host_id"])
        rounds = payload.get("rounds", {})

        if not rounds:
            accent = await safe_resolve_accent(self.bot, getattr(channel, "guild", None), log_label="MLT")
            view = MLTJoinView(
                game_id, host_id, self.db, self.bot, self, accent=accent
            )
            self.bot.active_views[game_id] = view
            self.bot.add_view(view, message_id=message.id)
            log.info("Recovered mlt game %s (join phase) in #%s", game_id, getattr(channel, "name", channel.id))
            return True

        cur = max(rounds, key=lambda k: int(k))
        rd = rounds.get(cur, {})
        prompt = rd.get("prompt", "") or ""
        players = [int(p) for p in payload.get("players", [])]
        guild = getattr(channel, "guild", None)
        host_name = resolve_name(guild, host_id) if guild else "Host"
        accent = await safe_resolve_accent(self.bot, guild, log_label="MLT")

        pacing = self._pacing_from_payload(payload, rd.get("opened_at"))
        view = self._build_vote_view(
            game_id=game_id,
            host_id=host_id,
            host_name=host_name,
            round_num=int(cur),
            players=players,
            channel=channel,
            prompt=prompt,
            interaction=None,
            accent=accent,
            pacing=pacing,
        )
        view.votes = {int(k): int(v) for k, v in (rd.get("votes") or {}).items()}
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
        log.info("Recovered mlt game %s (round %s) in #%s", game_id, cur, getattr(channel, "name", channel.id))
        return True


    async def mid_game_join(self, channel, game_id: str, member):
        """Add *member* to a running game; they're in from the next round."""
        uid = member.id
        state: dict = {}

        def _add(payload):
            players = payload.setdefault("players", [])
            state["limit"] = int(payload.get("max_players") or MAX_PLAYERS)
            state["already_in"] = uid in players
            state["full"] = lobby_is_full(players, state["limit"])
            state["added"] = add_player(players, uid, state["limit"])

        await modify_payload(self.db, game_id, _add)
        if not state.get("added"):
            if state.get("already_in"):
                return False, f"**{member.display_name}** is already in this game."
            if state.get("full"):
                return (
                    False,
                    f"This game is full — it is set to take up to "
                    f"{state.get('limit', MAX_PLAYERS)} players.",
                )
            return False, f"**{member.display_name}** is already in this game."
        return True, f"🎲 **{member.display_name}** joined Most Likely To — in from the next round!"

    async def mid_game_leave(self, channel, game_id: str, member):
        """Remove *member* from a running game. Their crowns stay on the board."""
        uid = member.id
        state: dict = {}

        def _remove(payload):
            players = payload.setdefault("players", [])
            state["removed"] = remove_player(players, uid)

        await modify_payload(self.db, game_id, _remove)
        if not state.get("removed"):
            return False, f"**{member.display_name}** isn't in this game."
        return True, f"🎲 **{member.display_name}** left Most Likely To — their crowns stay on the board."


async def setup(bot: "Bot"):
    cog = MLTCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("mlt")
    play.add_command(cog.mlt, override=True)
    bot.game_launchers["mlt"] = cog.launch
    bot.game_recoverers["mlt"] = cog.recover_game
    bot.game_joiners["mlt"] = cog.mid_game_join
    bot.game_leavers["mlt"] = cog.mid_game_leave
