import asyncio
import functools
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord

from bot_modules.core.branding import safe_resolve_accent
from bot_modules.core.utils import disable_all_items, is_host_or_mod
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import HOW_TO_PLAY, play_description
from bot_modules.games.command_groups import play
from bot_modules.games.utils.audit import audit_anonymous
from bot_modules.services.anon_audit_service import (
    EVENT_TAKE_SUBMITTED,
)
from bot_modules.games.utils.game_manager import (
    ConfirmCloseView,
    finish_launch_response,
    create_game,
    update_game_message,
    update_game_payload,
    update_game_state,
    get_game_payload,
    modify_payload,
    end_game,
    update_session,
    resolve_name,
    channel_name,
)
from bot_modules.games.utils.launch_guard import refuse_launch
from bot_modules.games.utils.live_bar import LiveBarUpdater
from bot_modules.games.utils.recovery import start_redrive
from bot_modules.games.utils.round_pacing import (
    MAX_ROUND_SECONDS,
    RoundPacing,
    launch_pacing,
)
from bot_modules.games_hottakes.embeds import (
    build_lobby_embed,
    build_recap_embed,
    build_vote_embed,
)
from bot_modules.games_hottakes.logic import (
    DEFAULT_TAKE_SECONDS,
    SELF_VOTE_REFUSAL,
    VOTE_LABELS,
    VOTE_VALUES,
    active_voters,
    add_take,
    build_voting_start_message,
    everyone_has_voted,
    shuffle_takes,
    tally_votes,
    voting_refusal,
)

log = logging.getLogger(__name__)


class SubmitHotTakeModal(discord.ui.Modal, title="Your Hot Take"):
    take = discord.ui.TextInput(
        label="Hot Take",
        style=discord.TextStyle.paragraph,
        max_length=500,
        placeholder="Type your spiciest opinion here…",
    )

    def __init__(self, game_id: str, db, origin_message: discord.Message | None = None, *, queue_mode: bool = False):
        super().__init__()
        self.game_id = game_id
        self.db = db
        self._origin_message = origin_message
        self.queue_mode = queue_mode

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, "Your Hot Take", channel_name(interaction.channel))

        def _add_take(payload):
            add_take(payload, interaction.user.id, self.take.value)

        payload = await modify_payload(self.db, self.game_id, _add_take)

        if self.queue_mode:
            await interaction.response.send_message(
                "✅ Hot take queued! It will be voted on after the current takes.",
                ephemeral=True,
            )
        else:
            take_count = len(payload.get("takes", []))
            await interaction.response.send_message(
                f"✅ Hot take submitted! Total submissions: {take_count}", ephemeral=True
            )
            msg = self._origin_message or interaction.message
            if msg:
                embed = msg.embeds[0]
                for i, field in enumerate(embed.fields):
                    if field.name == "Submissions":
                        embed.set_field_at(i, name="Submissions", value=str(take_count), inline=True)
                        break
                try:
                    await msg.edit(embed=embed)
                except discord.HTTPException:
                    pass

        # After the member has been answered — this is best-effort logging and
        # must not spend any of Discord's 3s initial-response budget.
        if interaction.guild:
            await audit_anonymous(
                interaction.client, self.db, interaction.guild,
                game_type="hottakes", user=interaction.user,
                event=EVENT_TAKE_SUBMITTED,
                content=self.take.value, label="Hot Take Submission",
                game_id=self.game_id,
                channel_id=interaction.channel.id if interaction.channel else None,
                extra={"queued": self.queue_mode},
            )


class HotTakesSubmitView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, db, bot, cog):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self.cog = cog
        self._message: discord.Message | None = None

    @discord.ui.button(label="Submit Hot Take", style=discord.ButtonStyle.primary, custom_id="ht_submit")
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        modal = SubmitHotTakeModal(self.game_id, self.db, origin_message=interaction.message)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Start Voting", style=discord.ButtonStyle.primary, custom_id="ht_start")
    async def start_voting(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can start voting.", ephemeral=True)
            return
        payload = await get_game_payload(self.db, self.game_id)
        takes = payload.get("takes", [])
        # Two takes minimum (anon-tail-72): with one, everyone knows whose it is.
        refusal = voting_refusal(takes)
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return

        payload["takes"] = shuffle_takes(takes)
        await update_game_payload(self.db, self.game_id, payload)
        # The phase is what a restart branches on: 'joining' re-registers this
        # lobby's buttons, anything else resumes the vote. Until 2026-09-04
        # nothing wrote it, so recovery guessed from the take count and
        # force-started voting on any lobby that held a take (anon-tail-66).
        await update_game_state(self.db, self.game_id, "playing")

        self.stop()
        disable_all_items(self)
        await interaction.response.edit_message(view=self)

        channel = interaction.channel
        assert channel is not None and not isinstance(channel, (discord.ForumChannel, discord.CategoryChannel))

        # A public heads-up that names nobody: takes are anonymous, and
        # @-mentioning the submitters here (as this once did) gave them away.
        try:
            await channel.send(
                build_voting_start_message(takes),
                delete_after=15,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.warning("hottakes: voting heads-up failed in #%s", channel_name(channel))

        try:
            await self.cog._run_voting(
                interaction=interaction,
                game_id=self.game_id,
                host_id=self.host_id,
                host_name=interaction.user.display_name,
                channel=channel,
            )
        except Exception as e:
            log.error("Failed to start voting for game %s: %s", self.game_id, e, exc_info=True)
            await channel.send("❌ Something went wrong starting the vote. Game ended.")
            await end_game(self.db, self.game_id)
            self.bot.active_views.pop(self.game_id, None)

    @discord.ui.button(label="❓ Help", style=discord.ButtonStyle.secondary, custom_id="ht_htp")
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["hottakes"], ephemeral=True)

    @discord.ui.button(label="Cancel Game", style=discord.ButtonStyle.secondary, custom_id="ht_cancel")
    async def cancel_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Scrap the lobby before voting starts — host or mod, behind the
        usual confirm popup. Before this the only ways out of a lobby that
        never got going were ``/games end`` or the 24h sweep, and a lobby
        left over a restart sat with dead buttons (anon-tail-66).
        """
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can cancel the game.", ephemeral=True)
            return
        anchor = self._message or interaction.message

        async def _confirmed(_confirm: discord.Interaction) -> None:
            await self._cancel(anchor)

        await interaction.response.send_message(
            "⚠️ Are you sure you want to end this game?", view=ConfirmCloseView(_confirmed), ephemeral=True,
        )

    async def _cancel(self, anchor: discord.Message | None) -> None:
        """Disable the lobby and archive the row as ``cancelled``. A lobby has
        no roster yet (nobody has voted), so a bare ``end_game`` pays nobody
        and records the takes that were collected."""
        self.stop()
        disable_all_items(self)
        if anchor is not None:
            try:
                await anchor.edit(content="🛑 Hot Takes was cancelled before voting started.", view=self)
            except discord.HTTPException:
                pass
        await end_game(self.db, self.game_id, reason="cancelled")
        self.bot.active_views.pop(self.game_id, None)


class HotTakeVoteView(discord.ui.View):
    def __init__(
        self,
        game_id: str,
        host_id: int,
        take_text: str,
        take_num: int,
        total_takes: int,
        db,
        bot,
        host_name: str,
        advance_callback: Callable[[discord.Message], Awaitable[None]],
        accent: "discord.Color | None" = None,
        take_author_id: int | None = None,
        pacing: RoundPacing | None = None,
        expected_voters: "set[int] | None" = None,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.take_text = take_text
        self.take_num = take_num
        self.total_takes = total_takes
        self.db = db
        self.bot = bot
        self.host_name = host_name
        self.advance_callback = advance_callback
        self.accent = accent
        # The take's author may not rate their own take (anon-tail-74): in a
        # two-voter room one 🔥 self-vote decided the winner bonus.
        self.take_author_id = take_author_id
        # Per-take pacing (anon-tail-71): the timer that closes the vote, the
        # event Next/force-end set, and the room the take is waiting on — the
        # vote closes itself the moment every one of them has voted.
        self.pacing = pacing or RoundPacing()
        self.expected_voters: set[int] = set(expected_voters or ())
        self.votes: dict[int, int] = {}  # user_id -> 0-4 index
        self._updater = LiveBarUpdater()
        self._closed = False
        # force_end_active_game pokes this alias to wake the vote loop.
        self._advanced_event = self.pacing.advanced

    def _build_embed(self, closed: bool = False) -> discord.Embed:
        return build_vote_embed(
            take_text=self.take_text,
            take_num=self.take_num,
            total_takes=self.total_takes,
            votes_by_user=self.votes,
            closed=closed,
            color=self.accent,
            advance_at=self.pacing.advance_at(),
        )

    @discord.ui.button(label="🧊", style=discord.ButtonStyle.secondary, custom_id="ht_v0", row=0)
    async def vote_0(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do_vote(interaction, 0)

    @discord.ui.button(label="👎", style=discord.ButtonStyle.secondary, custom_id="ht_v1", row=0)
    async def vote_1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do_vote(interaction, 1)

    @discord.ui.button(label="😐", style=discord.ButtonStyle.secondary, custom_id="ht_v2", row=0)
    async def vote_2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do_vote(interaction, 2)

    @discord.ui.button(label="👍", style=discord.ButtonStyle.secondary, custom_id="ht_v3", row=0)
    async def vote_3(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do_vote(interaction, 3)

    @discord.ui.button(label="🔥", style=discord.ButtonStyle.secondary, custom_id="ht_v4", row=0)
    async def vote_4(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do_vote(interaction, 4)

    async def _do_vote(self, interaction: discord.Interaction, idx: int):
        log.info("%s voted in game %s in #%s", interaction.user.display_name, self.game_id, channel_name(interaction.channel))
        if self._closed:
            await interaction.response.send_message("This vote is closed.", ephemeral=True)
            return
        if self.take_author_id is not None and interaction.user.id == self.take_author_id:
            await interaction.response.send_message(SELF_VOTE_REFUSAL, ephemeral=True)
            return
        prev = self.votes.get(interaction.user.id)
        self.votes[interaction.user.id] = idx
        label = VOTE_LABELS[idx]
        changed = prev is not None and prev != idx
        msg = f"✅ Voted **{label}**{' (changed)' if changed else ''}"
        await interaction.response.send_message(msg, ephemeral=True, delete_after=3)
        message = interaction.message
        assert message is not None  # component interactions always carry their message
        # Everyone the take was waiting on has spoken: close it now rather
        # than sit out the rest of the timer.
        if everyone_has_voted(self.expected_voters, self.votes):
            await self.advance_callback(message)
            return
        await self._updater.schedule_update(message, self._build_embed)

    @discord.ui.button(label="📝 Submit Take", style=discord.ButtonStyle.secondary, custom_id="ht_v_submit", row=1)
    async def submit_take(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if self.game_id not in self.bot.active_views:
            await interaction.response.send_message("This game has already ended.", ephemeral=True)
            return
        modal = SubmitHotTakeModal(self.game_id, self.db, queue_mode=True)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="⏭️ Next Take", style=discord.ButtonStyle.secondary, custom_id="ht_next", row=1)
    async def next_take(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Skip ahead — the timer (or a complete vote) closes the take on
        its own; Next is the host's early close, never the only way on."""
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can advance.", ephemeral=True)
            return
        await interaction.response.defer()
        assert interaction.message is not None  # component interactions always carry their message
        await self.advance_callback(interaction.message)

    async def _post_recap(self, channel, payload: dict):
        results = payload.get("results", [])
        embed = build_recap_embed(results, color=self.accent)
        if embed is None:
            return
        guild = getattr(channel, "guild", None)
        if guild:
            from bot_modules.economy.game_rewards import append_payout_footer
            await append_payout_footer(self.bot, embed, guild.id, "hottakes")
        await channel.send(embed=embed)


class HotTakesCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(name="hottakes", description=play_description("hottakes"))
    @app_commands.describe(
        start_in="Show a lobby countdown — voting starts in this many minutes (host still clicks Start Voting)",
        take_seconds="Seconds each take is open (0 = you press Next)",
    )
    async def hottakes(
        self,
        interaction: discord.Interaction,
        start_in: app_commands.Range[int, 1, 60] | None = None,
        take_seconds: app_commands.Range[int, 0, MAX_ROUND_SECONDS] | None = None,
    ):
        log.info("%s used /games play hottakes in #%s", interaction.user.display_name, channel_name(interaction.channel))
        refusal = await refuse_launch(self.db, interaction, "hottakes")
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={"start_in": start_in, "round_seconds": take_seconds},
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
        """Interaction-free launch (slash command + scheduler). Returns game_id, or None.

        The per-take timer is the launch's ``round_seconds`` (a slash
        ``take_seconds`` or schedule option, even 0), else the dashboard's
        **Seconds per Take** dial, else :data:`DEFAULT_TAKE_SECONDS`; ``0`` is
        host-paced. Hot Takes is a lobby game (``LOBBY_GAME_TYPES``): a
        ``start_in`` stamps ``start_epoch`` for the countdown and the host
        nudge, and the idle-lobby dials and Game Night ping apply.
        """
        pacing = await launch_pacing(
            self.db, "hottakes", guild_id, options,
            default_round_seconds=DEFAULT_TAKE_SECONDS,
        )
        start_epoch = pacing.start_epoch
        payload: dict = pacing.stamp({
            "takes": [], "results": [], "participants": [],
            "round_seconds": pacing.round_seconds,
        })
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "hottakes",
            state="joining",
            payload=payload,
            guild_id=guild_id,
        )

        accent = await safe_resolve_accent(self.bot, getattr(channel, "guild", None), log_label="hottakes")
        embed = build_lobby_embed(host_name, color=accent, start_at=start_epoch)

        log.info("Game %s (hottakes) created by %s in #%s", game_id, host_name, getattr(channel, "name", channel.id))
        view = HotTakesSubmitView(game_id, host_id, self.db, self.bot, self)
        self.bot.active_views[game_id] = view

        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("hottakes launch lacked send perms in channel %s", channel.id)
            return None
        view._message = msg
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id

    async def _run_voting(
        self,
        interaction,
        game_id: str,
        host_id: int,
        host_name: str,
        channel,
        resume: bool = False,
    ):
        # On resume after a restart, seed from persisted results so already-voted
        # takes are skipped; the take whose round was interrupted is re-voted.
        results: list[dict] = []
        if resume:
            payload = await get_game_payload(self.db, game_id)
            results = list(payload.get("results", []))
            processed = len(results)
        else:
            processed = 0

        # Resolve the guild accent once for the whole game — never per vote /
        # per round. Every vote view + the recap reuse this cached value.
        accent = await safe_resolve_accent(self.bot, getattr(channel, "guild", None), log_label="hottakes")

        view: HotTakeVoteView | None = None
        while True:
            payload = await get_game_payload(self.db, game_id)
            if game_id not in self.bot.active_views:
                return

            all_takes = payload.get("takes", [])
            if processed >= len(all_takes):
                break

            take_data = all_takes[processed]
            processed += 1
            take_text = take_data["text"]
            take_num = processed
            total_takes = len(all_takes)
            take_author = take_data.get("user_id")

            # Per-take pacing: the dial's timer, and the room the take waits
            # on — every submitter plus everyone who has voted so far, minus
            # the author, who cannot rate their own take.
            take_seconds = int(payload.get("round_seconds", 0) or 0)
            pacing = RoundPacing(round_seconds=take_seconds, opened_at=time.time())
            expected = active_voters(all_takes, results, exclude=take_author)

            async def advance(
                message: discord.Message,
                _take: str = take_text,
                _num: int = take_num,
                _taker_id: Any = take_author,
            ) -> None:
                assert view is not None
                if view._closed:
                    return
                view._closed = True

                vote_counts, avg, std = tally_votes(view.votes)
                voters = list(view.votes.keys())

                result_entry = {
                    "text": _take,
                    "counts": vote_counts,
                    "avg": avg,
                    "std": std,
                    "voters": voters,
                    "author": _taker_id,
                }
                results.append(result_entry)

                # Persist incrementally so mid-game close doesn't lose prior results
                def _save_result(payload, _entry=result_entry):
                    payload.setdefault("results", []).append(_entry)
                await modify_payload(self.db, game_id, _save_result)

                final_embed = view._build_embed(closed=True)
                disable_all_items(view)
                try:
                    await message.edit(embed=final_embed, view=view)
                except discord.HTTPException:
                    pass
                view.pacing.advanced.set()

            view = HotTakeVoteView(
                game_id=game_id,
                host_id=host_id,
                take_text=take_text,
                take_num=take_num,
                total_takes=total_takes,
                db=self.db,
                bot=self.bot,
                host_name=host_name,
                advance_callback=advance,
                accent=accent,
                take_author_id=int(take_author) if take_author is not None else None,
                pacing=pacing,
                expected_voters=expected,
            )
            self.bot.active_views[game_id] = view

            embed = view._build_embed()
            msg = await channel.send(embed=embed, view=view)
            await update_game_message(self.db, game_id, msg.id)
            # The timer closes the take unless Next, a complete vote, or a
            # force-end gets there first (round_pacing's wait_for pattern).
            closer: Callable[[], Awaitable[None]] = functools.partial(advance, msg)
            pacing.start_timer(closer)

            await pacing.advanced.wait()
            # If the game was closed mid-round, stop the loop
            if view._closed and game_id not in self.bot.active_views:
                break
            await asyncio.sleep(1)

        # If the game was already closed by the host, skip final results
        if game_id not in self.bot.active_views:
            return

        # Results were saved incrementally in advance(); just read final state
        payload = await get_game_payload(self.db, game_id)

        if processed > 0:
            assert view is not None
            await view._post_recap(channel, payload)
        # Roster = everyone who voted or authored a take; the winning take's
        # author may not have voted, so a voters-only set would drop their bonus.
        participants = sorted(
            {v for r in results for v in r.get("voters", [])}
            | {r["author"] for r in results if r.get("author") is not None}
        )
        await end_game(
            self.db, game_id,
            player_count=len(participants),
            round_count=processed,
            payload=payload,
            bot=self.bot, player_ids=participants,
        )
        if game_id in self.bot.active_views:
            del self.bot.active_views[game_id]

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Bring a game back after a restart, by phase.

        A lobby (row state ``joining``, no results yet) gets its submit view
        re-registered on the anchor message, the way WYR and Story do — the
        host still presses Start Voting. Until 2026-09-04 the phase was never
        recorded and recovery keyed on the take count alone: an empty lobby
        was skipped (dead buttons until the sweep) and a lobby holding any
        take was re-driven straight into voting (platform-29, anon-tail-66).
        A row with results but still marked ``joining`` predates the phase
        write and is treated as voting underway — results are only written
        during a vote.

        Voting underway re-drives the loop: completed takes live in
        payload["results"]; the take being voted on at crash time can't be
        reconstructed (live votes aren't persisted), so we retire the stale
        message and re-vote that take. The re-driven loop seeds results from
        the payload and continues with the remaining takes.
        """
        game_id = row["game_id"]
        host_id = int(row["host_id"])
        takes = payload.get("takes", [])
        results = payload.get("results", [])
        if row["state"] == "joining" and not results:
            view = HotTakesSubmitView(game_id, host_id, self.db, self.bot, self)
            view._message = message
            self.bot.active_views[game_id] = view
            self.bot.add_view(view, message_id=message.id)
            log.info(
                "Recovered hottakes game %s (lobby, %d takes) in #%s",
                game_id, len(takes), getattr(channel, "name", channel.id),
            )
            return True
        if not takes or len(results) >= len(takes):
            return False  # nothing left to resume; cleanup loop will archive it
        guild = getattr(channel, "guild", None)
        host_name = resolve_name(guild, host_id) if guild else "Host"
        await start_redrive(
            self.bot, game_id, message,
            self._run_voting(
                interaction=None, game_id=game_id, host_id=host_id,
                host_name=host_name, channel=channel, resume=True,
            ),
            channel=channel, log_label=f"hottakes game {game_id} (re-driving voting)",
        )
        return True


async def setup(bot: "Bot"):
    cog = HotTakesCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("hottakes")
    play.add_command(cog.hottakes, override=True)
    bot.game_launchers["hottakes"] = cog.launch
    bot.game_recoverers["hottakes"] = cog.recover_game


# Re-export VOTE_VALUES so any tests / external callers that imported it
# from this module continue to work.
__all__ = [
    "HotTakesCog",
    "HotTakesSubmitView",
    "HotTakeVoteView",
    "SubmitHotTakeModal",
    "VOTE_LABELS",
    "VOTE_VALUES",
    "setup",
]
