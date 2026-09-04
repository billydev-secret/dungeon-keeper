import asyncio
import functools
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord

from bot_modules.core.utils import disable_all_items, is_host_or_mod
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import HOW_TO_PLAY, play_description
from bot_modules.games.command_groups import play
from bot_modules.games.utils.audit import audit_anonymous
from bot_modules.services.anon_audit_service import (
    EVENT_ENTRY_SUBMITTED,
)
from bot_modules.games.utils.game_manager import (
    ConfirmCloseView,
    finish_launch_response,
    create_game,
    get_game_options,
    update_game_message,
    update_game_state,
    get_game_payload,
    end_game,
    update_session,
    modify_payload,
    channel_name,
)
from bot_modules.games.utils.launch_guard import launch_refusal
from bot_modules.games.utils.live_bar import LiveBarUpdater
from bot_modules.games.utils.round_pacing import (
    MAX_ROUND_SECONDS,
    RoundPacing,
    resolve_pacing,
)
from bot_modules.core.branding import safe_resolve_accent
from bot_modules.games_fantasies.embeds import (
    build_lobby_embed,
    build_recap_embed,
    build_round_submit_embed,
    build_vote_embed,
)
from bot_modules.games_fantasies.logic import (
    CATEGORY_DEALBREAKER,
    CATEGORY_FANTASY,
    DEFAULT_ENTRY_SECONDS,
    SELF_VOTE_REFUSAL,
    active_voters,
    add_entry,
    apply_vote,
    build_result_entry,
    everyone_has_voted,
    get_round_entries,
    roster_from_results,
)
from bot_modules.services.game_start_ping_service import resolve_start_epoch

log = logging.getLogger(__name__)


class SubmitEntryModal(discord.ui.Modal):
    """The entry box alone — the button that opened it chose the category.

    It used to ask members to *type* "Fantasy" or "Dealbreaker" into a box
    beside their 500-character entry. An unrecognised word closed the modal
    with an ephemeral error, and the entry went with it: a binary choice was
    costing people everything they had just written.
    """

    entry: discord.ui.TextInput = discord.ui.TextInput(
        label="Your Entry",
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    def __init__(self, game_id: str, db, round_num: int, category: str):
        super().__init__(title=f"Submit a {category}")
        self.game_id = game_id
        self.db = db
        self.round_num = round_num
        self.category = category

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, "Submit Entry", channel_name(interaction.channel))
        category = self.category

        def _add_entry(payload):
            add_entry(
                payload,
                round_num=self.round_num,
                user_id=interaction.user.id,
                text=self.entry.value,
                category=category,
            )

        await modify_payload(self.db, self.game_id, _add_entry)

        await interaction.response.send_message(
            f"Your {category.lower()} has been submitted!", ephemeral=True
        )

        # After the member has been answered — this is best-effort logging and
        # must not spend any of Discord's 3s initial-response budget.
        if interaction.guild:
            await audit_anonymous(
                interaction.client, self.db, interaction.guild,
                game_type="fantasies", user=interaction.user,
                event=EVENT_ENTRY_SUBMITTED,
                content=self.entry.value, label=f"{category} Submission",
                game_id=self.game_id,
                channel_id=interaction.channel.id if interaction.channel else None,
                extra={"category": category, "round": self.round_num},
            )


class FantasiesMainView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, db, bot, cog):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self.cog = cog
        self.round_num = 0
        self._message: discord.Message | None = None
        # The round loop's live pieces, so End Game can wake and disable them.
        self._active_submit_view: SubmitRoundView | None = None
        self._active_vote_view: FantasiesVoteView | None = None

    @discord.ui.button(label="Start Round", style=discord.ButtonStyle.primary, custom_id="fan_start_round")
    async def start_round(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can start rounds.", ephemeral=True)
            return

        self.round_num += 1
        await interaction.response.defer()
        # The row must stop reading as an open lobby — the start-ping sweep
        # polls state='joining' (Fantasies is a lobby game since 2026-09-04)
        # and a game with a round underway is not idle.
        await update_game_state(self.db, self.game_id, "playing")

        await self.cog._run_round(
            game_id=self.game_id,
            host_id=self.host_id,
            host_name=interaction.user.display_name,
            round_num=self.round_num,
            channel=interaction.channel,
            main_view=self,
        )

    @discord.ui.button(label="❓ Help", style=discord.ButtonStyle.secondary, custom_id="fan_htp")
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["fantasies"], ephemeral=True)

    @discord.ui.button(label="End Game", style=discord.ButtonStyle.secondary, custom_id="fan_end")
    async def end_game_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Post the recap and pay the room — the ending the game never had.

        Fantasies shipped with Start Round and Help only: the recap builder
        was dead code and ``end_game`` was reachable solely through ``/games
        end`` or the 24h sweep (anon-tail-65). Host or mod, behind the usual
        confirm popup.
        """
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can end the game.", ephemeral=True)
            return
        channel = interaction.channel
        anchor = self._message or interaction.message

        async def _confirmed(_confirm: discord.Interaction) -> None:
            await self.cog._finish_game(self, channel=channel, anchor=anchor)

        await interaction.response.send_message(
            "⚠️ Are you sure you want to end this game?", view=ConfirmCloseView(_confirmed), ephemeral=True,
        )


class SubmitRoundView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, round_num: int, db, bot):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.round_num = round_num
        self.db = db
        self.bot = bot
        self._message: discord.Message | None = None

    @discord.ui.button(label="Submit a Fantasy", emoji="💖", style=discord.ButtonStyle.primary, custom_id="fan_submit_fantasy")
    async def submit_fantasy(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_entry_modal(interaction, button, CATEGORY_FANTASY)

    @discord.ui.button(label="Submit a Dealbreaker", emoji="🚩", style=discord.ButtonStyle.primary, custom_id="fan_submit_dealbreaker")
    async def submit_dealbreaker(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_entry_modal(interaction, button, CATEGORY_DEALBREAKER)

    async def _open_entry_modal(
        self, interaction: discord.Interaction, button: discord.ui.Button, category: str
    ) -> None:
        """One button per category — the choice is made before any typing."""
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        modal = SubmitEntryModal(self.game_id, self.db, self.round_num, category)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Close Submissions", style=discord.ButtonStyle.secondary, custom_id="fan_close_sub")
    async def close_submissions(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can close submissions.", ephemeral=True)
            return
        self._closed = True
        self.stop()
        disable_all_items(self)
        await interaction.response.edit_message(content=f"✅ Submissions closed for Round {self.round_num}!", view=self)


class FantasiesVoteView(discord.ui.View):
    def __init__(
        self,
        game_id: str,
        host_id: int,
        entry_text: str,
        entry_num: int,
        category: str,
        db,
        bot,
        host_name: str,
        advance_callback,
        entry_author_id: int = 0,
        total_entries: int = 0,
        pacing: RoundPacing | None = None,
        expected_voters: "set[int] | None" = None,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.entry_text = entry_text
        self.entry_num = entry_num
        self.total_entries = total_entries
        self.category = category
        self.db = db
        self.bot = bot
        self.host_name = host_name
        self.advance_callback = advance_callback
        self.entry_author_id = entry_author_id
        # Per-entry pacing (anon-tail-71): the timer that closes the vote,
        # the event Next/End/force-end set, and the room the entry waits on —
        # the vote closes itself once every one of them has voted.
        self.pacing = pacing or RoundPacing()
        self.expected_voters: set[int] = set(expected_voters or ())
        self.same_votes: list[int] = []
        self.nope_votes: list[int] = []
        self._updater = LiveBarUpdater()
        self._closed = False
        # force_end_active_game pokes this alias to wake the round loop.
        self._advanced_event = self.pacing.advanced
        self._accent_color: "discord.Color | None" = None
        self._message: discord.Message | None = None

    def _build_embed(self, closed: bool = False) -> discord.Embed:
        return build_vote_embed(
            entry_text=self.entry_text,
            entry_num=self.entry_num,
            category=self.category,
            same_votes=self.same_votes,
            nope_votes=self.nope_votes,
            total_entries=self.total_entries,
            closed=closed,
            color=self._accent_color,
            advance_at=self.pacing.advance_at(),
        )

    async def _after_vote(self, interaction: discord.Interaction) -> None:
        """Refresh the bars — or close the entry when the room is done."""
        if everyone_has_voted(self.expected_voters, self.same_votes + self.nope_votes):
            await self.advance_callback(interaction.message)
            return
        await self._updater.schedule_update(interaction.message, self._build_embed)

    @discord.ui.button(label="✅ Same", style=discord.ButtonStyle.success, custom_id="fan_same", row=0)
    async def vote_same(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s voted in game %s in #%s", interaction.user.display_name, self.game_id, channel_name(interaction.channel))
        if self._closed:
            await interaction.response.send_message("Voting is closed.", ephemeral=True)
            return
        if interaction.user.id == self.entry_author_id:
            await interaction.response.send_message(SELF_VOTE_REFUSAL, ephemeral=True)
            return
        changed = apply_vote(
            self.same_votes, self.nope_votes, interaction.user.id, "same"
        )
        msg = f"✅ Voted **Same**{' (changed)' if changed else ''}"
        await interaction.response.send_message(msg, ephemeral=True, delete_after=3)
        await self._after_vote(interaction)

    @discord.ui.button(label="❌ Not for Me", style=discord.ButtonStyle.danger, custom_id="fan_nope", row=0)
    async def vote_nope(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s voted in game %s in #%s", interaction.user.display_name, self.game_id, channel_name(interaction.channel))
        if self._closed:
            await interaction.response.send_message("Voting is closed.", ephemeral=True)
            return
        if interaction.user.id == self.entry_author_id:
            await interaction.response.send_message(SELF_VOTE_REFUSAL, ephemeral=True)
            return
        changed = apply_vote(
            self.same_votes, self.nope_votes, interaction.user.id, "nope"
        )
        msg = f"✅ Voted **Not for me**{' (changed)' if changed else ''}"
        await interaction.response.send_message(msg, ephemeral=True, delete_after=3)
        await self._after_vote(interaction)

    @discord.ui.button(label="⏭️ Next", style=discord.ButtonStyle.secondary, custom_id="fan_next", row=1)
    async def next_entry(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Skip ahead — the timer (or a complete vote) closes the entry on
        its own; Next is the host's early close."""
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can advance.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.advance_callback(interaction.message)


class FantasiesCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(name="fantasies", description=play_description("fantasies"))
    @app_commands.describe(
        start_in="Show a countdown — the first round starts in this many minutes (host still clicks Start Round)",
        entry_seconds="Seconds each entry stays open for votes (0 = you press Next; default from the dashboard)",
    )
    async def fantasies(
        self,
        interaction: discord.Interaction,
        start_in: app_commands.Range[int, 1, 60] | None = None,
        entry_seconds: app_commands.Range[int, 0, MAX_ROUND_SECONDS] | None = None,
    ):
        log.info("%s used /games play fantasies in #%s", interaction.user.display_name, channel_name(interaction.channel))
        refusal = await launch_refusal(
            self.db, "fantasies", interaction.channel_id, interaction.guild_id or 0,
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
            options={"start_in": start_in, "round_seconds": entry_seconds},
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

        The per-entry timer is the launch's ``round_seconds`` (a slash
        ``entry_seconds`` or schedule option, even 0), else the dashboard's
        **Seconds per Entry** dial, else :data:`DEFAULT_ENTRY_SECONDS`; ``0``
        is host-paced. The panel opens as a ``joining`` lobby (Fantasies is
        in ``LOBBY_GAME_TYPES`` since 2026-09-04): a ``start_in`` stamps
        ``start_epoch`` for the countdown and the host nudge, and the
        idle-lobby dials and Game Night ping apply until the first round.
        """
        game_opts = await get_game_options(self.db, "fantasies", guild_id)
        round_seconds, _ = resolve_pacing(options, {"round_seconds": DEFAULT_ENTRY_SECONDS, **game_opts})
        start_epoch = resolve_start_epoch(options)
        payload: dict = {"rounds": {}, "results": [], "round_seconds": round_seconds}
        if start_epoch:
            payload["start_epoch"] = start_epoch
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "fantasies",
            state="joining",
            payload=payload,
            guild_id=guild_id,
        )

        guild = getattr(channel, "guild", None)
        color = await safe_resolve_accent(self.bot, guild, log_label="fantasies")
        embed = build_lobby_embed(host_name, color=color, start_at=start_epoch)

        log.info("Game %s (fantasies) created by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))
        view = FantasiesMainView(game_id, host_id, self.db, self.bot, self)
        self.bot.active_views[game_id] = view

        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("fantasies launch lacked send perms in channel %s", channel.id)
            return None
        view._message = msg
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id

    async def _run_round(
        self,
        game_id: str,
        host_id: int,
        host_name: str,
        round_num: int,
        channel,
        main_view: "FantasiesMainView | None" = None,
    ):
        """Run one round: collect entries, then vote on each in turn.

        ``main_view`` is the control panel that started the round. While a
        round runs, ``bot.active_views[game_id]`` points at the live vote view
        (that is where ``/games end`` looks for the event to wake), and the
        panel is put back when the round is over — until 2026-09-04 it never
        was, so from round two on nothing could find the submit view to stop.
        """
        if main_view is None:
            candidate = self.bot.active_views.get(game_id)
            main_view = candidate if isinstance(candidate, FantasiesMainView) else None
        guild = getattr(channel, "guild", None)
        accent_color = await safe_resolve_accent(self.bot, guild, log_label="fantasies")
        submit_embed = build_round_submit_embed(round_num, color=accent_color)
        submit_view = SubmitRoundView(game_id, host_id, round_num, self.db, self.bot)
        # Let the main view know so it can stop us on close
        if main_view is not None:
            main_view._active_submit_view = submit_view
        submit_view._message = await channel.send(embed=submit_embed, view=submit_view)

        await submit_view.wait()

        # Clear reference now that submission phase is over
        if main_view is not None:
            main_view._active_submit_view = None

        # If game was closed during submission, bail out
        if game_id not in self.bot.active_views:
            return

        payload = await get_game_payload(self.db, game_id)
        entries = get_round_entries(payload, round_num)

        if not entries:
            # The zero-entry round is the skip: the panel stays live, and the
            # host picks between another round and the ending.
            await channel.send(
                "No entries this round — press **Start Round** to try again, or **End Game** to wrap up."
            )
            self._restore_panel(game_id, main_view)
            return

        results = list(payload.get("results") or [])
        for i, entry_data in enumerate(entries):
            entry_text = entry_data["text"]
            entry_category = entry_data.get("category", "Fantasy")
            entry_num = i + 1
            entry_author = entry_data["user_id"]

            # Per-entry pacing: the dial's timer, and the room the entry waits
            # on — this round's submitters plus everyone who has voted so far,
            # minus the author, who cannot vote on their own entry.
            entry_seconds = int(payload.get("round_seconds", 0) or 0)
            pacing = RoundPacing(round_seconds=entry_seconds, opened_at=time.time())
            expected = active_voters(entries, results, exclude=entry_author)

            async def advance(message: discord.Message, _text=entry_text, _num=entry_num, _author=entry_author, _cat=entry_category) -> None:
                if view._closed:
                    return
                view._closed = True

                result_entry = build_result_entry(
                    text=_text,
                    category=_cat,
                    author=_author,
                    same_votes=view.same_votes,
                    nope_votes=view.nope_votes,
                )
                results.append(result_entry)

                # Persist incrementally so mid-game close doesn't lose prior results
                def _save_result(payload, _entry=result_entry):
                    payload.setdefault("results", []).append(_entry)
                await modify_payload(self.db, game_id, _save_result)

                disable_all_items(view)
                try:
                    await message.edit(embed=view._build_embed(closed=True), view=view)
                except discord.HTTPException:
                    pass
                view.pacing.advanced.set()

            view = FantasiesVoteView(
                game_id=game_id,
                host_id=host_id,
                entry_text=entry_text,
                entry_num=entry_num,
                category=entry_category,
                db=self.db,
                bot=self.bot,
                host_name=host_name,
                advance_callback=advance,
                entry_author_id=entry_author,
                total_entries=len(entries),
                pacing=pacing,
                expected_voters=expected,
            )
            view._accent_color = accent_color
            self.bot.active_views[game_id] = view
            if main_view is not None:
                main_view._active_vote_view = view

            embed = view._build_embed()
            sent = await channel.send(embed=embed, view=view)
            view._message = sent
            # The timer closes the entry unless Next, a complete vote, End or
            # a force-end gets there first (round_pacing's wait_for pattern).
            closer: Callable[[], Awaitable[None]] = functools.partial(advance, sent)
            pacing.start_timer(closer)
            await pacing.advanced.wait()
            # If the game was closed mid-round, stop the loop
            if view._closed and game_id not in self.bot.active_views:
                break
            await asyncio.sleep(1)

        if main_view is not None:
            main_view._active_vote_view = None

        # If the game was already closed by the host, skip saving
        if game_id not in self.bot.active_views:
            return

        # Results were saved incrementally in advance(); no extra save needed
        self._restore_panel(game_id, main_view)

    def _restore_panel(self, game_id: str, main_view: "FantasiesMainView | None") -> None:
        """Hand ``active_views`` back to the control panel after a round."""
        if main_view is not None and game_id in self.bot.active_views:
            self.bot.active_views[game_id] = main_view

    async def _post_recap(self, channel, payload: dict) -> bool:
        """Post the final recap with the payout footer; False when there is
        nothing to recap (no entry was ever voted on)."""
        results = payload.get("results", [])
        guild = getattr(channel, "guild", None)
        color = await safe_resolve_accent(self.bot, guild, log_label="fantasies")
        embed = build_recap_embed(results, color=color)
        if embed is None:
            return False
        if guild:
            from bot_modules.economy.game_rewards import append_payout_footer
            await append_payout_footer(self.bot, embed, guild.id, "fantasies")
        await channel.send(embed=embed)
        return True

    async def _finish_game(self, main_view: FantasiesMainView, *, channel, anchor) -> None:
        """The host's ending: wake and disable whatever the round loop is
        blocked on, disable the panel, post the recap, pay the roster.

        Mirrors Hot Takes' completion: the roster is entry authors plus every
        voter (``roster_from_results``), the round count is the entries voted
        on, and ``end_game`` gets ``bot=``/``player_ids=`` so the faucet
        fires. An entry mid-vote when End is pressed is dropped, as it is on
        ``/games end`` — its votes were never persisted.
        """
        game_id = main_view.game_id
        # Claim the game in memory first so the round loop returns at its
        # guard instead of posting the next entry on top of the recap.
        self.bot.active_views.pop(game_id, None)

        sub = main_view._active_submit_view
        if sub is not None and not sub.is_finished():
            sub.stop()
            disable_all_items(sub)
            await self._edit_quietly(sub._message, view=sub)
        vote = main_view._active_vote_view
        if vote is not None and not vote._closed:
            vote._closed = True
            disable_all_items(vote)
            await self._edit_quietly(vote._message, embed=vote._build_embed(closed=True), view=vote)
            if vote._advanced_event is not None:
                vote._advanced_event.set()

        main_view.stop()
        disable_all_items(main_view)
        await self._edit_quietly(anchor, view=main_view)

        payload = await get_game_payload(self.db, game_id)
        results = payload.get("results", [])
        roster = roster_from_results(results)
        if not await self._post_recap(channel, payload):
            await channel.send("✨ Fantasies & Dealbreakers ended — no entries were voted on, so there's nothing to recap.")
        log.info("Game %s (fantasies) ended — %d players, %d entries", game_id, len(roster), len(results))
        await end_game(
            self.db, game_id,
            player_count=len(roster), round_count=len(results), payload=payload,
            bot=self.bot, player_ids=roster,
        )

    @staticmethod
    async def _edit_quietly(message, **kwargs) -> None:
        """Best-effort edit of a message we may no longer hold or that is gone."""
        if message is None:
            return
        try:
            await message.edit(**kwargs)
        except discord.HTTPException:
            pass

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Re-register the host control panel after a restart.

        Fantasies is host-driven: the FantasiesMainView (the tracked message) is
        the persistent control panel, and the host presses "Start Round" to run
        each round. A round's submit/vote messages aren't tracked, so a round
        interrupted by a crash is abandoned — the host simply starts the next
        round. We restore the round counter so numbering continues correctly.
        """
        game_id = row["game_id"]
        host_id = int(row["host_id"])
        view = FantasiesMainView(game_id, host_id, self.db, self.bot, self)
        view._message = message
        rounds = payload.get("rounds", {})
        if rounds:
            try:
                view.round_num = max(int(k) for k in rounds)
            except ValueError:
                view.round_num = 0
        self.bot.active_views[game_id] = view
        self.bot.add_view(view, message_id=message.id)
        log.info(
            "Recovered fantasies game %s (control panel, round_num=%d) in #%s",
            game_id, view.round_num, getattr(channel, "name", channel.id),
        )
        return True


async def setup(bot: "Bot"):
    cog = FantasiesCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("fantasies")
    play.add_command(cog.fantasies, override=True)
    bot.game_launchers["fantasies"] = cog.launch
    bot.game_recoverers["fantasies"] = cog.recover_game
