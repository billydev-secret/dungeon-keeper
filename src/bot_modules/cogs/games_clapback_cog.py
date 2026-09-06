import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord

from bot_modules.core.utils import disable_all_items, is_host_or_mod
from discord.ext import commands
from discord import app_commands

from bot_modules.games.constants import (
    GAME_ICONS,
    HOW_TO_PLAY,
    play_description,
)
from bot_modules.games.utils.game_manager import (
    ConfirmCloseView,
    sign_off_game_chore,
    finish_launch_response,
    get_game_options,
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
from bot_modules.core.branding import safe_resolve_accent
from bot_modules.services.name_resolver import NameFn, build_name_fn
from bot_modules.services.no_contact_service import no_contact_pairs_among
from bot_modules.services.game_start_ping_service import resolve_start_epoch
from bot_modules.games.utils.launch_guard import refuse_launch
from bot_modules.games.utils.recovery import start_redrive
from bot_modules.games.utils.question_source import (
    get_clapback_prompt,
    has_clapback_prompts,
    channel_allows_nsfw,
)
from bot_modules.games.command_groups import play
from bot_modules.games_clapback import logic as clapback_logic
from bot_modules.games_clapback.logic import (
    MAX_PLAYERS,
    MIN_ANSWERS,
    MIN_PLAYERS,
    THREE_PLAYER_NOTE,
    accept_answer,
    admit_pending_players,
    admit_player_now,
    all_eligible_voted,
    calculate_bye_award,
    drain_pending_players,
    pick_round_bye,
    playable_players,
    submit_window_may_close,
    vote_button_label,
    calculate_matchup_score,
    clamp_config_values,
    create_matchups,
    shuffled_replay_config,
    withdraw_player,
)
from bot_modules.games_clapback.embeds import (
    build_lobby_embed,
    build_recap_embed,
    build_reveal_embed,
    build_scoreboard_embed,
    build_submit_embed,
    build_vote_embed,
)
from bot_modules.games.utils.audit import audit_anonymous
from bot_modules.services.anon_audit_service import (
    EVENT_ANSWER_SUBMITTED,
)

log = logging.getLogger(__name__)

ICON = GAME_ICONS["clapback"]


# ── Prompt fetching ──────────────────────────────────────────────────────────


async def fetch_prompt(db, config: dict, used: list[str]) -> str | None:
    """Get a prompt from the question bank (Clapback is bank-only)."""
    tags = config.get("tags") or None
    allow_nsfw = bool(config.get("allow_nsfw", False))
    return await get_clapback_prompt(db, exclude=used, tags=tags, allow_nsfw=allow_nsfw)


# ── Modal ────────────────────────────────────────────────────────────────────


class ClapbackAnswerModal(discord.ui.Modal, title="Your Answer"):
    answer_input = discord.ui.TextInput(
        label="Your funniest answer",
        placeholder="Type your answer here…",
        style=discord.TextStyle.short,
        required=True,
        max_length=200,
    )

    def __init__(self, game_id: str, round_num: int, db, cog):
        super().__init__()
        self.title = f"Round {round_num} — Your Answer"
        self.game_id = game_id
        self.round_num = round_num
        self.db = db
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        log.info(
            "%s submitted answer in game %s",
            interaction.user.display_name, self.game_id,
        )
        answer = re.sub(r'@(everyone|here)', '@​\\1', self.answer_input.value.strip())
        if not answer:
            await interaction.response.send_message(
                "Nice try, but you need to actually write something. 😄",
                ephemeral=True,
            )
            return

        uid = interaction.user.id
        round_num = self.round_num
        accepted = False

        # Discord keeps a modal open indefinitely: one sent after its window
        # closed used to be written anyway — lost, or filed under the next
        # round's prompt (clapback-4). Checked inside the write lock so the
        # window can't close between the check and the write.
        def _store(payload):
            nonlocal accepted
            accepted = accept_answer(payload, round_num)
            if not accepted:
                return
            answers = payload.setdefault("answers", {})
            answers[str(uid)] = answer

        payload = await modify_payload(self.db, self.game_id, _store)
        if not accepted:
            await interaction.response.send_message(
                f"❌ Answers for round {round_num} are closed.", ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Answer submitted! You can click Submit again to change it before time runs out.",
            ephemeral=True,
        )

        # Only audited when the game is running anonymously. With attribution
        # on, the answer is posted under the player's own name and there is no
        # anonymity to account for — logging it would be plain surveillance.
        anonymous = bool((payload.get("config") or {}).get("anonymous", False))
        if anonymous and interaction.guild is not None:
            await audit_anonymous(
                interaction.client, self.db, interaction.guild,
                game_type="clapback", user=interaction.user,
                event=EVENT_ANSWER_SUBMITTED,
                content=answer, label="Clapback Anonymous Answer",
                game_id=self.game_id,
                channel_id=interaction.channel.id if interaction.channel else None,
                extra={"anonymous": True},
            )

        # Signal the cog that a new answer arrived
        cog = self.cog
        if self.game_id in cog._submit_events:
            cog._submit_events[self.game_id].set()


# ── Views ────────────────────────────────────────────────────────────────────


class ClapbackJoinView(discord.ui.View):
    # Inactivity window; every button press resets it. A scheduled start
    # (start_in) extends the first window past the advertised start time.
    LOBBY_TIMEOUT = 600

    def __init__(
        self, game_id: str, host_id: int, db, bot, cog, config: dict,
        accent: "discord.Color | None" = None,
    ):
        timeout = float(self.LOBBY_TIMEOUT)
        start_epoch = config.get("start_epoch")
        if start_epoch:
            timeout = max(timeout, start_epoch - time.time() + 120)
        super().__init__(timeout=timeout)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self.cog = cog
        self.config = config
        # Guild accent resolved once at game start; reused for every live
        # lobby edit (join/leave) instead of re-resolving per button press.
        self.accent = accent
        self.message: discord.Message | None = None

    async def on_timeout(self):
        await self.cog._cancel_game(self.game_id, reason="lobby_timeout")
        # Retire the lobby message — a live-looking Join/Start row on a dead
        # view swallows clicks as "This interaction failed".
        if self.message is not None:
            disable_all_items(self)
            try:
                await self.message.edit(
                    content="⌛ **Lobby timed out** — the game wasn't started in time. Run `/games play clapback` to open a new one.",
                    view=self,
                )
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="ql_join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        uid = interaction.user.id

        def _add(payload):
            players = payload.setdefault("players", [])
            if uid not in players:
                players.append(uid)

        payload = await modify_payload(self.db, self.game_id, _add)
        log.info("%s joined game %s", interaction.user.display_name, self.game_id)
        await self._update_embed(interaction, payload)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, custom_id="ql_leave")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        uid = interaction.user.id

        if uid == self.host_id:
            await interaction.response.send_message(
                "You're the host! Press **Cancel** to close the lobby instead.",
                ephemeral=True,
            )
            return

        def _remove(payload):
            players = payload.setdefault("players", [])
            if uid in players:
                players.remove(uid)

        payload = await modify_payload(self.db, self.game_id, _remove)
        log.info("%s left game %s", interaction.user.display_name, self.game_id)
        await self._update_embed(interaction, payload)

    @discord.ui.button(label="Start", style=discord.ButtonStyle.primary, custom_id="ql_start")
    async def start_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can start.", ephemeral=True)
            return

        payload = await get_game_payload(self.db, self.game_id)
        players = payload.get("players", [])
        min_p = MIN_PLAYERS

        # A member the no-contact list keeps apart from every other player can
        # never be seated in a matchup, so they don't count toward the
        # minimum. The refusal is the ordinary short-lobby line, roster count
        # and all — nothing about it says which rule it came from.
        forbidden = await self.cog._forbidden_pairs(interaction.guild, players)
        if len(playable_players(players, forbidden)) < min_p:
            await interaction.response.send_message(
                f"Need at least {min_p} players to start Clapback. Currently: {len(players)}.",
                ephemeral=True,
            )
            return
        if len(players) > MAX_PLAYERS:
            await interaction.response.send_message(
                f"Clapback supports up to {MAX_PLAYERS} players. {len(players)} are joined — ask some to sit this one out.",
                ephemeral=True,
            )
            return

        channel = interaction.channel
        assert channel is not None and not isinstance(channel, (discord.ForumChannel, discord.CategoryChannel))

        self.stop()
        disable_all_items(self)
        await interaction.response.edit_message(view=self)

        payload = await self.cog._begin_game(self.game_id, channel, payload)
        await self.cog._play(self.game_id, channel, payload)

    @discord.ui.button(label="❓ Help", style=discord.ButtonStyle.secondary, custom_id="ql_htp")
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        cfg = self.config
        text = HOW_TO_PLAY["clapback"] + (
            f"\n\n⏱️ **{cfg['timer']}s** to write each answer\n"
            f"🗳️ **{cfg['vote_timer']}s** to vote each matchup\n"
            f"🏆 **{cfg['rounds']}** rounds — highest score wins"
        )
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="ql_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Close the lobby before it starts — host or mod, behind the usual
        confirm popup. The host's Leave reply pointed at a Cancel button that
        did not exist, leaving `/games end` or the idle sweep as the only
        ways out (clapback-12)."""
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message(
                "❌ Only the host or a mod can cancel the lobby.", ephemeral=True,
            )
            return
        anchor = self.message or interaction.message

        async def _confirmed(_confirm: discord.Interaction) -> None:
            await self._cancel(anchor)

        await interaction.response.send_message(
            "⚠️ Are you sure you want to cancel this lobby?",
            view=ConfirmCloseView(_confirmed), ephemeral=True,
        )

    async def _cancel(self, anchor) -> None:
        """Archive the lobby as cancelled and retire its message, the way
        ``on_timeout`` does."""
        self.stop()
        await self.cog._cancel_game(self.game_id, reason="cancelled")
        disable_all_items(self)
        if anchor is not None:
            try:
                await anchor.edit(
                    content="🛑 **Lobby cancelled** by the host. Run `/games play clapback` to open a new one.",
                    view=self,
                )
            except discord.HTTPException:
                pass

    async def _update_embed(self, interaction: discord.Interaction, payload: dict):
        players = payload.get("players", [])
        guild = interaction.guild

        host_member = guild.get_member(self.host_id) if guild else None
        host_name = host_member.display_name if host_member else "Host"

        embed = build_lobby_embed(
            host_name=host_name,
            config=self.config,
            players=players,
            name_resolver=lambda uid: resolve_name(guild, uid),
            start_at=self.config.get("start_epoch"),
            color=self.accent,
        )
        await interaction.response.edit_message(embed=embed, view=self)


class ClapbackSubmitView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, round_num: int, db, bot, cog):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.round_num = round_num
        self.db = db
        self.bot = bot
        self.cog = cog
        # Set by Close answers; the submit loop reads it every tick.
        self.close_requested = False

    @discord.ui.button(label="✏️ Submit", style=discord.ButtonStyle.primary, custom_id="ql_submit")
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        payload = await get_game_payload(self.db, self.game_id)
        players = payload.get("players", [])
        bye_player = payload.get("round_bye")
        if bye_player is not None and str(interaction.user.id) == str(bye_player):
            await interaction.response.send_message(
                "🪑 You're sitting this round out — no answer needed. You'll be "
                "paid the round's average, and you can still vote on everyone "
                "else's matchups.",
                ephemeral=True,
            )
            return
        if interaction.user.id not in players:
            await interaction.response.send_message(
                "You're not in this game — hit **Join now**!", ephemeral=True,
            )
            return
        modal = ClapbackAnswerModal(self.game_id, self.round_num, self.db, self.cog)
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="🙋 Join now", style=discord.ButtonStyle.secondary,
        # custom_id kept from when this button said "Join next round", so the
        # panels of games already running across a restart keep routing.
        custom_id="ql_join_midgame",
    )
    async def join_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Put a latecomer straight into the round that's taking answers.

        The roster used to be sealed at start, so a game running on odd
        numbers could not be evened out and people tried another bot's `&add`
        at it. Then it queued them for the *next* round — safe, but it made
        someone watching a prompt they had a clapback for sit the round out.
        Matchups are built from the answers dict once the window closes, so
        there was never anything to protect: `admit_player_now` seats them and
        the modal opens on the same press.

        Off the submit phase it still queues (nothing can be written mid-vote),
        and the reply says which of the two happened.
        """
        uid = interaction.user.id
        verdict = ""
        round_num = self.round_num
        bye_before = None

        # The parity rule may un-bench the pre-picked bye, and only if the
        # no-contact gate would still seat them — so the pairs are read over
        # the roster-plus-joiner before the write.
        current = await get_game_payload(self.db, self.game_id)
        forbidden = await self.cog._forbidden_pairs(
            interaction.guild, [*current.get("players", []), uid],
        )

        def _join(p):
            nonlocal verdict, round_num, bye_before
            bye_before = p.get("round_bye")
            verdict = admit_player_now(p, uid, MAX_PLAYERS, forbidden_pairs=forbidden)
            round_num = int(p.get("current_round") or self.round_num)

        await modify_payload(self.db, self.game_id, _join)

        if verdict in ("joined", "joined-unbenched"):
            log.info(
                "%s joined game %s mid-round", interaction.user.display_name, self.game_id
            )
            await interaction.response.send_modal(
                ClapbackAnswerModal(self.game_id, round_num, self.db, self.cog)
            )
            self.cog._poke_submit(self.game_id)
            # The room sees the answer counter jump; say why. Best-effort — the
            # player is already in and writing, so a failed post is cosmetic.
            channel = interaction.channel
            if channel is not None and not isinstance(
                channel, (discord.ForumChannel, discord.CategoryChannel)
            ):
                text = (
                    f"🙋 {interaction.user.mention} jumped in — they're playing "
                    f"this round, starting on 0 points."
                )
                if verdict == "joined-unbenched" and bye_before is not None:
                    text += (
                        f"\n🪑 <@{bye_before}> you're back in this round — that "
                        f"evens the numbers, so hit **Submit**!"
                    )
                try:
                    await channel.send(text)
                except discord.HTTPException:
                    log.debug("clapback: couldn't announce a mid-round join", exc_info=True)
            return

        text = {
            "already-in": "You're already in this game — hit **Submit**.",
            "full": (
                f"🙅 This game is full at {MAX_PLAYERS} players. Jump into the "
                f"next one!"
            ),
            "queued": (
                "🙋 Answers are closed for this round, so you're in from the "
                "**next** one — you'll start on 0 points. If this turns out to "
                "be the last round, you'll be told and can join the next game."
            ),
            "queued-parity": (
                "🙋 Jumping in now would leave an odd number of writers and "
                "bench someone who's already written, so you're in from the "
                "**next** round — you'll start on 0 points. If this turns out "
                "to be the last round, you'll be told and can join the next game."
            ),
            "already-queued": (
                "You're already queued — you'll be in from the next round."
            ),
        }.get(verdict, "Couldn't add you to this game.")
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(
        label="🔒 Close answers", style=discord.ButtonStyle.secondary, custom_id="ql_close_answers",
    )
    async def close_answers(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Host or mod ends the write window early (clapback-2): the phase
        that actually stalls a game had no host control, so every round with
        one absent writer ran its whole window. Refused below two answers —
        that would only skip the round."""
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message(
                "❌ Only the host or a mod can close answers.", ephemeral=True,
            )
            return
        payload = await get_game_payload(self.db, self.game_id)
        count = len(payload.get("answers") or {})
        if count < MIN_ANSWERS:
            await interaction.response.send_message(
                f"❌ Only {count} answer{'s' if count != 1 else ''} in — at least "
                f"{MIN_ANSWERS} are needed to run the round.",
                ephemeral=True,
            )
            return
        self.close_requested = True
        event = self.cog._submit_events.get(self.game_id)
        if event is not None:
            event.set()
        await interaction.response.send_message(
            f"🔒 Closing answers with {count} in.", ephemeral=True,
        )


class ClapbackVoteView(discord.ui.View):
    def __init__(
        self, game_id: str, host_id: int, matchup_index: int,
        player_a: int, player_b: int, players: list[int],
        db, bot, cog,
        answer_a: str = "", answer_b: str = "",
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.matchup_index = matchup_index
        self.player_a = player_a
        self.player_b = player_b
        self.players = players
        self.db = db
        self.bot = bot
        self.cog = cog
        self._closed = False
        # The buttons carried the bare 🅰️/🅱️ emoji, so voting meant mapping
        # "the left one" back to an answer and people plainly weren't ("I can
        # never remember if left is yes or no"). Put the answer on the button.
        self.vote_a.label = vote_button_label("🅰️", answer_a)
        self.vote_b.label = vote_button_label("🅱️", answer_b)

    @discord.ui.button(label="🅰️", style=discord.ButtonStyle.primary, custom_id="ql_vote_a", row=0)
    async def vote_a(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do_vote(interaction, self.player_a, "🅰️")

    @discord.ui.button(label="🅱️", style=discord.ButtonStyle.primary, custom_id="ql_vote_b", row=0)
    async def vote_b(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do_vote(interaction, self.player_b, "🅱️")

    async def _do_vote(self, interaction: discord.Interaction, voted_for: int, label: str):
        log.info("%s voted in game %s", interaction.user.display_name, self.game_id)
        if self._closed:
            await interaction.response.send_message("Voting is closed.", ephemeral=True)
            return

        uid = interaction.user.id
        if uid in (self.player_a, self.player_b):
            await interaction.response.send_message(
                "❌ You can't vote on your own matchup! 😎",
                ephemeral=True,
            )
            return

        # Anyone (players and spectators alike) can vote — the only people
        # blocked are the two contestants in this matchup, above.

        idx = self.matchup_index

        def _store_vote(payload):
            matchups = payload.get("matchups", [])
            if idx < len(matchups):
                matchups[idx]["votes"][str(uid)] = voted_for

        await modify_payload(self.db, self.game_id, _store_vote)

        await interaction.response.send_message(
            f"Voted for {label}!", ephemeral=True, delete_after=3,
        )


class ClapbackRoundSummaryView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, db, bot, cog):
        super().__init__(timeout=15)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self.cog = cog
        self._advanced = asyncio.Event()

    async def on_timeout(self):
        self._advanced.set()

    @discord.ui.button(label="▶️ Next Round", style=discord.ButtonStyle.primary, custom_id="ql_next")
    async def next_round(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can advance.", ephemeral=True)
            return
        self.stop()
        disable_all_items(self)
        await interaction.response.edit_message(view=self)
        self._advanced.set()


class ClapbackRecapView(discord.ui.View):
    # Matches the lobby's inactivity window: a host back from a drink should
    # still find a live Play Again (clapback-13).
    RECAP_TIMEOUT = 600

    def __init__(
        self, game_id: str, host_id: int, config: dict, db, bot, cog,
        players: list[int] | None = None,
    ):
        super().__init__(timeout=float(self.RECAP_TIMEOUT))
        self.game_id = game_id
        self.host_id = host_id
        # The finished game's countdown is spent; carrying it would make the
        # rematch lobby read as "starting <in the past>" and, with the roster
        # seeded below, the start-ping sweep would start it on its next tick
        # (clapback-8) — the host presses Start on a rematch.
        self.config = {k: v for k, v in config.items() if k != "start_epoch"}
        # The finished roster (leavers already off it): the rematch lobby
        # opens with them seated instead of empty (clapback-10).
        self.players = list(players or [])
        self.db = db
        self.bot = bot
        self.cog = cog
        self.message: discord.Message | None = None

    async def on_timeout(self):
        # A live-looking Play Again on a dead view fails with "This interaction
        # failed" — retire the buttons instead.
        disable_all_items(self)
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="🔁 Play Again", style=discord.ButtonStyle.primary, custom_id="ql_replay")
    async def play_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host can start a rematch.", ephemeral=True)
            return
        # Same gate as the slash entry: an admin who unticks the game on the
        # dashboard mid-evening must not be overridden by the recap card. The
        # bank check honours the channel's age-gate the way the slash entry
        # does, or an NSFW-only bank would refuse a rematch in its own room.
        refusal = await refuse_launch(self.cog.db, interaction, "clapback")
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        self.stop()
        disable_all_items(self)
        await interaction.response.edit_message(view=self)
        game_id = await self.cog._start_new_game(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild=interaction.guild,
            config=self.config,
            players=self.players,
        )
        # A rematch started from the recap misses the shared
        # finish_launch_response seam, but a mod pressing it has run a game by
        # hand. Only on a launch that produced one, and after it, so a failed
        # rematch never ticks a chore green.
        if game_id:
            await sign_off_game_chore(
                self.cog.bot, interaction.guild_id, interaction.user.id
            )


    @discord.ui.button(label="🔀 Play Again (Shuffled)", style=discord.ButtonStyle.secondary, custom_id="ql_replay_shuffle")
    async def play_again_shuffled(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host can start a rematch.", ephemeral=True)
            return
        refusal = await refuse_launch(self.cog.db, interaction, "clapback")
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        channel = interaction.channel
        assert channel is not None and not isinstance(channel, (discord.ForumChannel, discord.CategoryChannel))
        self.stop()
        disable_all_items(self)
        await interaction.response.edit_message(view=self)

        shuffled = shuffled_replay_config(self.config)
        await channel.send(
            f"🔀 **Shuffled settings:** {shuffled['rounds']} rounds, "
            f"{shuffled['timer']}s submit, {shuffled['vote_timer']}s vote"
        )
        game_id = await self.cog._start_new_game(
            channel=channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild=interaction.guild,
            config=shuffled,
            players=self.players,
        )
        # A rematch started from the recap misses the shared
        # finish_launch_response seam, but a mod pressing it has run a game by
        # hand. Only on a launch that produced one, and after it, so a failed
        # rematch never ticks a chore green.
        if game_id:
            await sign_off_game_chore(
                self.cog.bot, interaction.guild_id, interaction.user.id
            )



# ── Cog ──────────────────────────────────────────────────────────────────────


class ClapbackCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot
        # Events used to signal early completion of submit / vote phases
        self._submit_events: dict[str, asyncio.Event] = {}
        self._vote_events: dict[str, asyncio.Event] = {}
        self._game_cancelled: set[str] = set()
        # Guild accent resolved ONCE per game at start (or on recovery) and
        # reused by every phase's embed — never re-resolved per vote / update.
        self._accents: dict[str, "discord.Color | None"] = {}
        # Games the countdown auto-start spawned; held so the tasks aren't
        # garbage-collected mid-game.
        self._auto_tasks: set[asyncio.Task] = set()

    @property
    def db(self):
        return self.bot.games_db

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Recover after a restart, by phase.

        A lobby (``state == 'joining'``) gets its Join/Leave/Start row rebuilt
        on the lobby message, as Rushmore does — re-driving the loop from a
        lobby played five no-op rounds on an empty roster and crashed on the
        missing scores key with a joined one (clapback-3). discord.py only
        re-binds a persistent view, so the recovered lobby has no inactivity
        timeout; the start-ping sweep and the 24h sweep still cover it.

        A game in play is re-driven from the next un-played round: completed
        rounds live in payload["round_history"]; _run_game resumes at
        len(round_history)+1, re-running the interrupted round after rolling
        scores back to the last-completed-round checkpoint so its partial
        mid-scoring mutations can't double-count. The stale phase message is
        retired and the game loop is re-spawned in the background.
        """
        config = payload.get("config")
        if not config:
            return False
        game_id = row["game_id"]
        self._game_cancelled.discard(game_id)
        # Accent cache is lost across a restart — re-resolve it once here so the
        # resumed phases stay on-theme without re-resolving per update.
        accent = await safe_resolve_accent(self.bot, getattr(channel, "guild", None), log_label="clapback")
        self._accents[game_id] = accent
        if row["state"] == "joining":
            host_id = int(payload.get("host_id") or row["host_id"])
            view = ClapbackJoinView(game_id, host_id, self.db, self.bot, self, config, accent=accent)
            view.timeout = None
            view.message = message
            self.bot.active_views[game_id] = view
            self.bot.add_view(view, message_id=message.id)
            log.info("Recovered clapback game %s (lobby) in #%s", game_id, getattr(channel, "name", channel.id))
            return True
        resume_round = len(payload.get("round_history", [])) + 1
        await start_redrive(
            self.bot, game_id, message,
            self._run_game(game_id, channel, payload),
            channel=channel, log_label=f"clapback game {game_id} (resuming at round {resume_round})",
        )
        return True

    # ── Slash command ────────────────────────────────────────────────────

    @app_commands.command(name="clapback", description=play_description("clapback"))
    @app_commands.describe(
        start_in="Lobby countdown in minutes — the game starts itself when it runs out (3+ joined)",
    )
    async def clapback(
        self,
        interaction: discord.Interaction,
        start_in: app_commands.Range[int, 1, 60] | None = None,
    ):
        log.info(
            "%s used /games play clapback in #%s",
            interaction.user.display_name,
            channel_name(interaction.channel),
        )

        # The one launch guard every door shares: allowed channel, enabled
        # dial, no game already running here, and — Clapback is bank-only —
        # a bank with a prompt this channel's age-gate lets it serve.
        refusal = await refuse_launch(self.db, interaction, "clapback")
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={"start_in": start_in},
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
        # Clapback is bank-only; nothing to play if the bank is empty. Covers
        # headless (scheduled) launches, which skip the slash pre-check.
        if not await has_clapback_prompts(self.db):
            log.warning("clapback launch skipped in channel %s — question bank is empty", getattr(channel, "id", "?"))
            return None
        # Pacing/content knobs live in the per-server dashboard config (game_opts);
        # an explicit *options* value (e.g. from a saved schedule) still wins.
        game_opts = await get_game_options(self.db, "clapback", guild_id)
        rounds, timer, vote_timer = clamp_config_values(
            int(options.get("rounds", game_opts.get("rounds", 5))),
            int(options.get("timer", game_opts.get("timer", 120))),
            int(options.get("vote_timer", game_opts.get("vote_timer", 40))),
        )
        # Stays under `config` (the lobby view's timeout and the embed both read
        # it there); the ping service's accessor knows to look for it.
        start_epoch = resolve_start_epoch(options)
        # Normalize tags to a list — the dashboard/scheduler store a
        # comma-separated string, an explicit options value may be a list.
        tags_cfg = options.get("tags", game_opts.get("tags", ""))
        if isinstance(tags_cfg, str):
            tags = [t.strip() for t in tags_cfg.split(",") if t.strip()]
        else:
            tags = [str(t).strip() for t in (tags_cfg or []) if str(t).strip()]
        config = {
            "rounds": rounds,
            "timer": timer,
            "vote_timer": vote_timer,
            "anonymous": bool(options.get("anonymous", game_opts.get("anonymous", False))),
            "start_epoch": start_epoch,
            "tags": tags,
            "allow_nsfw": channel_allows_nsfw(channel),
        }
        return await self._start_new_game(
            channel=channel, host_id=host_id, host_name=host_name,
            guild=getattr(channel, "guild", None), config=config,
        )

    async def _start_new_game(
        self,
        *,
        channel,
        host_id: int,
        host_name: str,
        guild,
        config: dict,
        players: list[int] | None = None,
    ) -> str | None:
        """Open a lobby. ``players`` seeds it (a rematch's finished roster);
        the host still presses Start. The age-gate is re-read from the channel
        rather than trusted from a carried config — a rematch after the room's
        age-restriction changed must draw from the right bank (safety-sweep-10)."""
        config = {**config, "allow_nsfw": channel_allows_nsfw(channel)}
        seed = list(dict.fromkeys(int(p) for p in (players or [])))[:MAX_PLAYERS]
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "clapback",
            state="joining",
            payload={"config": config, "players": seed, "host_id": host_id},
        )
        log.info("Game %s (clapback) created by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))

        # Resolve the guild accent ONCE for the whole game and cache it; every
        # phase (lobby / submit / vote / reveal / scoreboard / recap) reuses it.
        accent = await safe_resolve_accent(self.bot, guild, log_label="clapback")
        self._accents[game_id] = accent
        embed = build_lobby_embed(
            host_name=host_name,
            config=config,
            players=seed,
            name_resolver=lambda uid: resolve_name(guild, uid),
            start_at=config.get("start_epoch"),
            color=accent,
        )

        view = ClapbackJoinView(game_id, host_id, self.db, self.bot, self, config, accent=accent)
        self.bot.active_views[game_id] = view

        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("clapback launch lacked send perms in channel %s", channel.id)
            return None

        view.message = msg
        await update_game_message(self.db, game_id, msg.id)
        await update_session(
            self.db, channel.id, game_id, list(dict.fromkeys([host_id, *seed])),
        )
        return game_id

    # ── Lobby → play ─────────────────────────────────────────────────────

    async def _begin_game(self, game_id: str, channel, payload: dict) -> dict:
        """Take a validated lobby into play: seed the scoreboard, retire the
        ``joining`` state. Shared by the Start button and the countdown
        auto-start (``auto_start``), so both paths start a game the same way.
        The caller has already checked the roster against the floor."""
        players = payload.get("players", [])
        # Players are pinged per-round when each round's prompt is posted
        # (see _submit_phase), so there's no separate start ping here. At the
        # three-player floor, though, say what that means before the first
        # vote card does (clapback-7).
        if len(players) == 3:
            try:
                await channel.send(THREE_PLAYER_NOTE)
            except discord.HTTPException:
                pass

        # Initialize scores
        payload["scores"] = {str(p): 0 for p in players}
        # Snapshot of scores (and CLAPBACK tallies) as of the last fully-
        # completed round. Restored on crash-resume so a round interrupted
        # mid-scoring can't double-count either (clapback-14).
        payload["scores_checkpoint"] = {str(p): 0 for p in players}
        payload["clapbacks"] = {str(p): 0 for p in players}
        payload["clapbacks_checkpoint"] = {str(p): 0 for p in players}
        payload["current_round"] = 0
        payload["round_history"] = []
        payload["used_prompts"] = []
        payload["phase"] = "playing"
        payload["last_bye"] = None
        # Every bye handed out this game, in order. Drives the
        # fewest-byes-first rotation in create_matchups.
        payload["bye_history"] = []
        await update_game_payload(self.db, game_id, payload)
        # The row must stop reading as an open lobby: the start-ping sweep polls
        # state='joining', and clapback rounds outlive a 10-minute countdown, so
        # leaving it would nudge "time to start" mid-game.
        await update_game_state(self.db, game_id, "playing")
        return payload

    async def _play(self, game_id: str, channel, payload: dict) -> None:
        """Run the game to its end, archiving a crash as one."""
        try:
            await self._run_game(game_id, channel, payload)
        except Exception as e:
            log.error("Clapback game %s crashed: %s", game_id, e, exc_info=True)
            await channel.send("❌ Something went wrong. Game ended.")
            await self._cancel_game(game_id, reason="crash")

    async def auto_start(self, row, payload: dict, channel) -> bool:
        """Start a countdown lobby without a button press (clapback-8).

        Registered in ``bot.lobby_auto_starters``; the start-ping sweep calls
        it when ``start_epoch`` arrives with at least ``MIN_PLAYERS`` joined.
        Applies the Start button's own gates — the no-contact floor
        (``playable_players``) and the ceiling — and returns False when they
        refuse, in which case the sweep nudges the host instead and Start
        gives them the ordinary refusal line. The lobby view is stopped and
        its buttons retired exactly as a press would, and the game runs as a
        background task so the sweep is never held for the length of a game.
        """
        game_id = row["game_id"]
        view = self.bot.active_views.get(game_id)
        if not isinstance(view, ClapbackJoinView):
            # No live lobby view means no game loop could see itself as
            # running (``_is_cancelled``); leave it to the host / sweeps.
            log.debug("auto-start: clapback lobby %s has no live view", game_id)
            return False
        # Fresh roster: a join may have landed since the sweep read the row.
        payload = await get_game_payload(self.db, game_id)
        players = list(payload.get("players", []))
        guild = getattr(channel, "guild", None)
        forbidden = await self._forbidden_pairs(guild, players)
        if len(playable_players(players, forbidden)) < MIN_PLAYERS or len(players) > MAX_PLAYERS:
            return False

        view.stop()
        disable_all_items(view)
        message = view.message
        if message is None and row["message_id"]:
            try:
                message = await channel.fetch_message(int(row["message_id"]))
            except Exception:
                message = None
        if message is not None:
            try:
                await message.edit(view=view)
            except discord.HTTPException:
                pass

        payload = await self._begin_game(game_id, channel, payload)
        task = asyncio.create_task(self._play(game_id, channel, payload))
        self._auto_tasks.add(task)
        task.add_done_callback(self._auto_tasks.discard)
        log.info("Game %s (clapback) auto-started at its countdown with %d players", game_id, len(players))
        return True

    # ── Game loop ────────────────────────────────────────────────────────

    async def _run_game(self, game_id: str, channel, payload: dict):
        config = payload["config"]
        total_rounds = config["rounds"]
        host_id = payload.get("host_id") or payload["players"][0]

        # Resume-aware: a fresh game has an empty round_history (starts at round
        # 1); after a crash we continue from the round after the last completed
        # one. That interrupted round is re-run from scratch, so we roll scores
        # back to the last-completed-round checkpoint first — otherwise the
        # round's partial mid-scoring mutations would be counted twice.
        payload = await get_game_payload(self.db, game_id)
        start_round = len(payload.get("round_history", [])) + 1
        if "scores_checkpoint" in payload:
            payload["scores"] = dict(payload["scores_checkpoint"])
            # The CLAPBACK tally used to survive the rollback, so a resume in
            # matchup 3 re-counted whoever swept 1–2 (clapback-14).
            if "clapbacks_checkpoint" in payload:
                payload["clapbacks"] = dict(payload["clapbacks_checkpoint"])
            await update_game_payload(self.db, game_id, payload)

        for round_num in range(start_round, total_rounds + 1):
            if self._is_cancelled(game_id):
                return

            payload = await get_game_payload(self.db, game_id)
            payload["current_round"] = round_num
            payload["phase"] = "submitting"
            payload["answers"] = {}

            # Latecomers queued during the previous round join here, at the
            # boundary, so nothing about a live round shifts under it.
            roster, admitted, turned_away = admit_pending_players(
                payload.get("players", []),
                payload.get("pending_players", []),
                MAX_PLAYERS,
                left=payload.get("left"),
            )
            if admitted or turned_away:
                payload["players"] = roster
                payload["pending_players"] = []
                for uid in admitted:
                    payload.setdefault("scores", {}).setdefault(str(uid), 0)
                    payload.setdefault("scores_checkpoint", {}).setdefault(str(uid), 0)
                    payload.setdefault("clapbacks", {}).setdefault(str(uid), 0)
                    payload.setdefault("clapbacks_checkpoint", {}).setdefault(str(uid), 0)
            await update_game_payload(self.db, game_id, payload)
            if admitted:
                joined = ", ".join(f"<@{uid}>" for uid in admitted)
                await channel.send(
                    f"🙋 {joined} joined the game — starting on 0 points."
                )
            if turned_away:
                names = ", ".join(f"<@{uid}>" for uid in turned_away)
                await channel.send(
                    f"🙅 {names} couldn't join — Clapback is full at "
                    f"{MAX_PLAYERS} players."
                )

            # Get prompt
            prompt = await fetch_prompt(self.db, config, payload.get("used_prompts", []))
            if not prompt:
                await channel.send(
                    f"Couldn't generate a prompt for round {round_num} — skipping.",
                )
                continue

            payload["prompt"] = prompt
            payload["used_prompts"] = payload.get("used_prompts", []) + [prompt]

            # Who sits out is settled before the prompt goes out, so the
            # benched player is never asked for an answer nobody will read.
            # bye_history is every bye handed out so far; games started before
            # it existed carry only `last_bye`, so seed from that on
            # crash-resume rather than restarting the rotation. Ids are
            # normalised to str — that is how answers/scores are keyed, and
            # the rotation counts wouldn't match across types.
            bye_history = payload.get("bye_history")
            if bye_history is None:
                legacy = payload.get("last_bye")
                bye_history = [legacy] if legacy is not None else []
            bye_history = [str(b) for b in bye_history]
            # The no-contact pairs among the people in play, read fresh each
            # time the bracket needs them (the service keeps no cache on
            # purpose — a stale read fails toward seating a pair). Here the
            # roster decides the pre-pick; after the window the submitters
            # decide the matchups, and anyone who pressed Join now is in
            # that second read without any bookkeeping.
            guild = getattr(channel, "guild", None)
            forbidden = await self._forbidden_pairs(guild, payload["players"])
            round_bye = pick_round_bye(
                [str(p) for p in payload["players"]], bye_history,
                forbidden_pairs=forbidden,
            )
            payload["round_bye"] = round_bye
            await update_game_payload(self.db, game_id, payload)

            # Submit phase
            answers = await self._submit_phase(
                game_id, channel, payload, prompt, round_num, config, host_id,
                bye_player=round_bye,
            )
            if self._is_cancelled(game_id):
                return

            if len(answers) < MIN_ANSWERS:
                await channel.send("Not enough answers this round — moving on!")
                continue

            # Join now may have un-benched the pre-picked bye to keep the
            # writer count even (clapback-5); the payload's word is final.
            round_bye = (await get_game_payload(self.db, game_id)).get("round_bye")

            # The pre-picked bye never submitted, so they are already out of
            # `answers`. A second bye can still fall out here when someone
            # else misses the window and leaves an odd number of submitters,
            # or when the no-contact gate has to bench one of a pair — every
            # bye gets paid the round average, whatever caused it. A round
            # the gate leaves with nothing safe to vote on is skipped exactly
            # like a round short on answers.
            forbidden = await self._forbidden_pairs(guild, list(answers))
            matchups, late_byes = create_matchups(
                answers, bye_history, forbidden_pairs=forbidden,
            )
            if not matchups:
                await channel.send("Not enough answers this round — moving on!")
                continue
            byes = [b for b in (round_bye, *late_byes) if b is not None]
            payload = await get_game_payload(self.db, game_id)
            payload["matchups"] = matchups
            payload["phase"] = "voting"
            await update_game_payload(self.db, game_id, payload)

            # Vote phase — process each matchup sequentially
            round_matchup_results = []
            round_points: list[int] = []
            for mi, matchup in enumerate(matchups):
                if self._is_cancelled(game_id):
                    return
                result = await self._vote_matchup(
                    game_id, channel, payload, mi, matchup,
                    answers, config, host_id, round_num, len(matchups), prompt,
                    byes=byes,
                )
                if result is None:
                    return  # game cancelled
                # Contestants' points feed the bye award below; they're not
                # part of the persisted round record.
                round_points.extend(result.pop("_scores", {}).values())
                round_matchup_results.append(result)
                await asyncio.sleep(1)

            if self._is_cancelled(game_id):
                return

            # Record round history
            payload = await get_game_payload(self.db, game_id)

            # Bye pays the field's average for this round, so it's settled
            # here — after the matchups have scored, not before they run.
            bye_award = None
            if byes:
                bye_award = calculate_bye_award(round_points)
                history = payload.setdefault("bye_history", list(bye_history))
                for bye in byes:
                    payload["scores"][str(bye)] = (
                        payload["scores"].get(str(bye), 0) + bye_award
                    )
                    history.append(bye)
                payload["last_bye"] = byes[-1]

            round_record = {
                "round": round_num,
                "prompt": prompt,
                "matchups": round_matchup_results,
            }
            if byes:
                # bye_player (singular) is kept for round records written
                # before a round could produce two byes.
                round_record["bye_player"] = byes[0]
                round_record["bye_players"] = list(byes)
                round_record["bye_award"] = bye_award
            payload.setdefault("round_history", []).append(round_record)
            # Round fully scored — checkpoint so a later crash resumes from here.
            payload["scores_checkpoint"] = dict(payload.get("scores", {}))
            payload["clapbacks_checkpoint"] = dict(payload.get("clapbacks", {}))
            payload["phase"] = "revealing"
            await update_game_payload(self.db, game_id, payload)

            # Round summary
            is_last = round_num == total_rounds
            if not is_last:
                should_continue = await self._round_summary(
                    game_id, channel, payload, round_num, total_rounds, host_id,
                    byes, bye_award,
                )
                if not should_continue or self._is_cancelled(game_id):
                    return
                await asyncio.sleep(2)  # between-round breather
            else:
                # Show final summary scoreboard briefly before recap
                await self._post_scoreboard(
                    game_id, channel, payload, round_num, total_rounds,
                    byes, bye_award, final=True,
                )

        if self._is_cancelled(game_id):
            return

        # Final recap
        payload = await get_game_payload(self.db, game_id)
        # Anyone who pressed Join during the last round was promised a round
        # that never arrives — admission only happens at a round boundary, and
        # there are none left. Tell them rather than leaving them queued.
        stranded = drain_pending_players(payload)
        if stranded:
            await update_game_payload(self.db, game_id, payload)
            names = ", ".join(f"<@{uid}>" for uid in stranded)
            await channel.send(
                f"🙅 {names} — that was the last round, so you didn't make it "
                f"into this game. Jump into the next one!"
            )
        await self._post_recap(game_id, channel, payload, config)

    # ── Submit phase ─────────────────────────────────────────────────────

    async def _submit_phase(
        self, game_id, channel, payload, prompt, round_num, config, host_id,
        bye_player=None,
    ):
        from bot_modules.games.utils.timer import format_deadline, now_plus
        # The benched player is left out of the ping, the answer count and the
        # submit gate: being asked for an answer that will never be shown is
        # what made sitting out read as a bug rather than a rotation. The bye
        # is read off the payload each time: Join now can un-bench them
        # mid-window (clapback-5).
        def _expected(p: dict) -> list:
            bye = p.get("round_bye")
            return [
                uid for uid in p.get("players", [])
                if bye is None or str(uid) != str(bye)
            ]

        players = _expected({**payload, "round_bye": bye_player})
        timer_secs = config["timer"]
        deadline = now_plus(timer_secs)

        guild = getattr(channel, "guild", None)
        name_fn = await self._names(guild, [] if bye_player is None else [bye_player])
        embed = build_submit_embed(
            prompt=prompt,
            round_num=round_num,
            total_rounds=config["rounds"],
            deadline_str=format_deadline(deadline),
            answers_in=0,
            total_players=len(players),
            bye_player=bye_player,
            color=self._accents.get(game_id),
            name_resolver=name_fn,
        )

        view = ClapbackSubmitView(game_id, host_id, round_num, self.db, self.bot, self)
        self.bot.active_views[game_id] = view

        # Ping the active players so nobody misses a new round starting. Only
        # user mentions go in the content, so no @everyone/@role pings.
        content = None
        if guild:
            mentions = " ".join(
                member.mention
                for uid in players
                if (member := guild.get_member(uid))
            )
            if mentions:
                content = f"{ICON} **Round {round_num} starting!** {mentions}"
            if bye_player is not None:
                # Told up front and by name, rather than discovered at the
                # scoreboard once the round is already over.
                content = (
                    f"{content or ''}\n🪑 <@{bye_player}> is **sitting this "
                    f"round out** — no answer needed, you'll be paid the "
                    f"round's average. You can still vote!"
                ).strip()

        msg = await channel.send(content=content, embed=embed, view=view)
        await update_game_message(self.db, game_id, msg.id)

        submit_event = asyncio.Event()
        self._submit_events[game_id] = submit_event

        elapsed = 0
        last_count = 0
        last_expected = len(players)
        last_change_at = 0
        last_edit_at = -5  # triggers first timer update at elapsed=1
        bye_shown = bye_player is not None
        while elapsed < timer_secs:
            if self._is_cancelled(game_id):
                break
            submit_event.clear()
            try:
                await asyncio.wait_for(submit_event.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
            elapsed += 1

            p = await get_game_payload(self.db, game_id)
            count = len(p.get("answers", {}))
            # Re-read every tick: Join now seats a latecomer mid-window, so the
            # denominator is not the roster this phase started with. Reading it
            # once is what would make "3/2" show up, and would end the window
            # early the moment the original players were all in.
            expected = len(_expected(p))
            count_changed = (count, expected) != (last_count, last_expected)
            timer_due = (elapsed - last_edit_at) >= 5
            if bye_shown and p.get("round_bye") is None:
                # Un-benched by a Join now: the "Sitting out" field is stale.
                bye_shown = False
                embed.remove_field(2)
                count_changed = True

            if count_changed or timer_due:
                if count_changed:
                    last_count, last_expected = count, expected
                    last_change_at = elapsed
                last_edit_at = elapsed
                remaining = max(0, timer_secs - elapsed)
                mins, secs = divmod(remaining, 60)
                timer_val = f"⏰ {mins}:{secs:02d}" if mins else f"⏰ {secs}s"
                embed.set_field_at(0, name="Timer", value=timer_val, inline=True)
                embed.set_field_at(1, name="Answers In", value=f"{count}/{expected}", inline=True)
                try:
                    await msg.edit(embed=embed)
                except discord.HTTPException:
                    pass

            # Full house, one short and quiet for a while, or the host's
            # Close answers (clapback-2) — the absent writer is not coming.
            if view.close_requested or submit_window_may_close(
                count, expected, elapsed - last_change_at,
            ):
                break

        self._submit_events.pop(game_id, None)

        # The window is shut before the answers are read, so a modal that
        # lands from here on is refused rather than bracketed late or filed
        # under the next round (clapback-4).
        def _shut(p):
            p["phase"] = "bracketing"

        payload = await modify_payload(self.db, game_id, _shut)

        # Disable submit view
        view.stop()
        disable_all_items(view)
        try:
            count = len(payload.get("answers", {}))
            expected = len(_expected(payload))
            embed.set_field_at(0, name="Timer", value="⏱️ Closed", inline=True)
            embed.set_field_at(1, name="Answers In", value=f"{count}/{expected}", inline=True)
            await msg.edit(embed=embed, view=view)
        except discord.HTTPException:
            pass

        return payload.get("answers", {})

    # ── Vote matchup ─────────────────────────────────────────────────────

    #: Seconds the reveal card sits before the next matchup; a class attribute
    #: so a test can run a matchup without the wait.
    REVEAL_SECONDS = 4

    async def _vote_matchup(
        self, game_id, channel, payload, matchup_index, matchup,
        answers, config, host_id, round_num, total_matchups, prompt,
        byes=(),
    ):
        from bot_modules.games.utils.timer import format_deadline, now_plus
        player_a, player_b = int(matchup["pair"][0]), int(matchup["pair"][1])
        players = payload["players"]
        answer_a = answers.get(str(player_a), "???")
        answer_b = answers.get(str(player_b), "???")
        anonymous = config.get("anonymous", False)
        vote_timer = config["vote_timer"]
        deadline = now_plus(vote_timer)

        accent = self._accents.get(game_id)
        embed = build_vote_embed(
            answer_a=answer_a,
            answer_b=answer_b,
            round_num=round_num,
            matchup_index=matchup_index,
            total_matchups=total_matchups,
            deadline_str=format_deadline(deadline),
            vote_count=0,
            prompt=prompt,
            color=accent,
        )

        view = ClapbackVoteView(
            game_id, host_id, matchup_index,
            player_a, player_b, players,
            self.db, self.bot, self,
            answer_a=answer_a, answer_b=answer_b,
        )
        self.bot.active_views[game_id] = view

        msg = await channel.send(embed=embed, view=view)

        vote_event = asyncio.Event()
        self._vote_events[game_id] = vote_event

        elapsed = 0
        last_vcount = 0
        last_edit_at = -5  # triggers first timer update at elapsed=1
        # Decision D1 (2026-09-04): the matchup closes once every eligible
        # player has voted — the roster minus the contestants minus a silent
        # bye — after a short grace for a spectator mid-click. A spectator
        # vote reopens the electorate and the full timer runs, which is the
        # one case the June decision (ab27201b) was protecting.
        grace_from: int | None = None
        while elapsed < vote_timer:
            if self._is_cancelled(game_id):
                return None
            vote_event.clear()
            try:
                await asyncio.wait_for(vote_event.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
            elapsed += 1

            p = await get_game_payload(self.db, game_id)
            m = p.get("matchups", [])
            if matchup_index < len(m):
                votes = m[matchup_index].get("votes", {})
                vcount = len(votes)
                vcount_changed = vcount != last_vcount
                timer_due = (elapsed - last_edit_at) >= 5

                if vcount_changed or timer_due:
                    if vcount_changed:
                        last_vcount = vcount
                    last_edit_at = elapsed
                    remaining = max(0, vote_timer - elapsed)
                    mins, secs = divmod(remaining, 60)
                    timer_val = f"⏰ {mins}:{secs:02d}" if mins else f"⏰ {secs}s"
                    embed.set_field_at(0, name="Timer", value=timer_val, inline=True)
                    embed.set_field_at(1, name="Votes", value=str(vcount), inline=True)
                    try:
                        await msg.edit(embed=embed)
                    except discord.HTTPException:
                        pass

                if all_eligible_voted(
                    votes, p.get("players", players), (player_a, player_b), byes,
                ):
                    if grace_from is None:
                        grace_from = elapsed
                    if elapsed - grace_from >= clapback_logic.VOTE_CLOSE_GRACE_SECONDS:
                        break
                else:
                    grace_from = None

        # A host /games end pops the game from active_views, which the next
        # _is_cancelled check catches within a second.

        self._vote_events.pop(game_id, None)
        view._closed = True
        view.stop()

        # Calculate result
        payload = await get_game_payload(self.db, game_id)
        matchup_data = payload["matchups"][matchup_index]
        result = calculate_matchup_score(matchup_data["votes"], player_a, player_b)

        # Update scores in payload
        for pid, pts in result["scores"].items():
            payload["scores"][str(pid)] = payload["scores"].get(str(pid), 0) + pts
        if result["clapback"]:
            winner_id = result["winner"]
            if winner_id:
                payload["clapbacks"][str(winner_id)] = payload["clapbacks"].get(str(winner_id), 0) + 1
        payload["matchups"][matchup_index]["winner"] = result["winner"]
        await update_game_payload(self.db, game_id, payload)

        # Build reveal embed
        guild = channel.guild if hasattr(channel, "guild") else None
        reveal = build_reveal_embed(
            result=result,
            answers=answers,
            player_a=player_a,
            player_b=player_b,
            anonymous=anonymous,
            name_resolver=lambda uid: resolve_name(guild, uid),
            prompt=prompt,
            color=accent,
        )

        # Edit message with reveal (buttons disabled)
        disable_all_items(view)
        try:
            await msg.edit(embed=reveal, view=view)
        except discord.HTTPException:
            pass

        await asyncio.sleep(self.REVEAL_SECONDS)  # Let players read the reveal

        # Build result record for round history
        vc = result["vote_counts"]
        return {
            "player_a": player_a,
            "answer_a": answer_a,
            "votes_a": vc[player_a],
            "player_b": player_b,
            "answer_b": answer_b,
            "votes_b": vc[player_b],
            "clapback": result["clapback"],
            # Stripped off by _run_game to compute the bye award — the
            # persisted round record keeps its original shape.
            "_scores": result["scores"],
        }

    # ── Round summary ────────────────────────────────────────────────────

    async def _round_summary(
        self, game_id, channel, payload, round_num, total_rounds, host_id,
        bye_players, bye_award=None,
    ):
        guild = getattr(channel, "guild", None)
        name_fn = await self._names(guild, self._scoreboard_ids(payload, bye_players))
        embed = build_scoreboard_embed(
            payload, round_num, total_rounds, bye_players, bye_award=bye_award,
            final=False, color=self._accents.get(game_id),
            name_resolver=name_fn,
        )
        view = ClapbackRoundSummaryView(game_id, host_id, self.db, self.bot, self)
        self.bot.active_views[game_id] = view
        msg = await channel.send(embed=embed, view=view)

        # Auto-advance after 10s or host click
        try:
            await asyncio.wait_for(view._advanced.wait(), timeout=10)
        except asyncio.TimeoutError:
            view._advanced.set()

        if self._is_cancelled(game_id):
            return False

        view.stop()
        disable_all_items(view)
        try:
            await msg.edit(view=view)
        except discord.HTTPException:
            pass
        return True

    async def _forbidden_pairs(self, guild, user_ids) -> set[tuple[int, int]]:
        """The no-contact pairs with both members in ``user_ids``.

        Read off the event loop; an empty set outside a guild or for fewer
        than two ids, so the bracket functions can take it unconditionally.
        """
        if guild is None or len(user_ids) < 2:
            return set()
        return await asyncio.to_thread(
            no_contact_pairs_among, self.bot.ctx.db_path, guild.id, user_ids
        )

    async def _names(self, guild, user_ids) -> NameFn:
        """The shared resolver (live cache → ``known_users`` → ``<@id>``,
        markdown-escaped) over one card's players. ``resolve_name`` in this
        cog is cache-only, so a player who left mid-game rendered as bare
        digits and a ``*`` in a nickname broke the bold (review 2026-09-04)."""
        ids: list[int] = []
        for uid in user_ids:
            try:
                ids.append(int(uid))
            except (TypeError, ValueError):
                continue
        return await build_name_fn(
            guild=guild,
            db_path=self.bot.ctx.db_path,
            guild_id=getattr(guild, "id", 0),
            user_ids=ids,
        )

    @staticmethod
    def _scoreboard_ids(payload, bye_players) -> list:
        byes = (
            list(bye_players) if isinstance(bye_players, (list, tuple, set))
            else [bye_players] if bye_players is not None else []
        )
        return [*payload.get("scores", {}).keys(), *byes]

    async def _post_scoreboard(
        self, game_id, channel, payload, round_num, total_rounds,
        bye_players, bye_award=None, final=False,
    ):
        guild = getattr(channel, "guild", None)
        name_fn = await self._names(guild, self._scoreboard_ids(payload, bye_players))
        embed = build_scoreboard_embed(
            payload, round_num, total_rounds, bye_players, bye_award=bye_award,
            final=final, color=self._accents.get(game_id),
            name_resolver=name_fn,
        )
        await channel.send(embed=embed)

    # ── Final recap ──────────────────────────────────────────────────────

    async def _post_recap(self, game_id, channel, payload, config):
        players = payload.get("players", [])
        guild = channel.guild if hasattr(channel, "guild") else None

        # A leaver's withdrawn score still names them, and they may be out of
        # the cache by now — the shared resolver covers that (see _names).
        name_fn = await self._names(guild, self._scoreboard_ids(payload, None))
        embed = build_recap_embed(
            payload=payload,
            config=config,
            name_resolver=name_fn,
            color=self._accents.get(game_id),
        )
        if guild:
            from bot_modules.economy.game_rewards import append_payout_footer
            await append_payout_footer(self.bot, embed, guild.id, "clapback")

        rounds_played = len(payload.get("round_history", []))
        host_id = payload.get("host_id") or (players[0] if players else 0)
        view = ClapbackRecapView(
            game_id, host_id, config, self.db, self.bot, self, players=list(players),
        )
        view.message = await channel.send(embed=embed, view=view)

        # End game
        log.info("Game %s ended — %d players, %d rounds", game_id, len(players), rounds_played)
        await end_game(
            self.db, game_id,
            player_count=len(players),
            round_count=rounds_played,
            payload=payload,
            bot=self.bot, player_ids=list(players),
        )
        self.bot.active_views.pop(game_id, None)
        self._cleanup(game_id)

    # ── Helpers ──────────────────────────────────────────────────────────

    async def _cancel_game(self, game_id: str, reason: str = "cancelled"):
        """Silently cancel a game (lobby timeout, crash) and archive it as one.

        The archive carries the roster as it stood and *reason*
        (``lobby_timeout`` / ``crash``), so a lobby nobody started is telling
        about how many had joined when it died instead of a bare row
        (clapback-9). Recording only — nothing here pays.
        """
        self._game_cancelled.add(game_id)
        log.info("Game %s cancelled: %s", game_id, reason)
        payload = await get_game_payload(self.db, game_id)
        await end_game(
            self.db, game_id,
            player_count=len(payload.get("players", [])),
            round_count=len(payload.get("round_history", [])),
            payload=payload or None,
            reason=reason,
        )
        self.bot.active_views.pop(game_id, None)
        self._cleanup(game_id)

    def _is_cancelled(self, game_id: str) -> bool:
        return game_id in self._game_cancelled or game_id not in self.bot.active_views

    def _poke_submit(self, game_id: str) -> None:
        """Wake the submit loop so the panel's count updates at once."""
        event = self._submit_events.get(game_id)
        if event is not None:
            event.set()

    def _cleanup(self, game_id: str):
        self._submit_events.pop(game_id, None)
        self._vote_events.pop(game_id, None)
        self._accents.pop(game_id, None)
        self._game_cancelled.discard(game_id)

    # ── Mid-game join / leave (dispatched from /games join, /games leave) ──

    async def mid_game_join(self, channel, game_id: str, member):
        """Add *member* to a running game, through the Join now rules.

        Shares :func:`admit_player_now` with the button rather than appending
        to the roster itself, so `/games join` lands the player in the same
        place the button would — this round if answers are open, the next one
        otherwise — and says which. It used to append unconditionally and
        report "from the next round" either way, which was wrong in both
        directions, and it seeded neither the checkpoint nor the player cap.
        """
        uid = member.id
        verdict = ""
        bye_before = None
        current = await get_game_payload(self.db, game_id)
        forbidden = await self._forbidden_pairs(
            getattr(channel, "guild", None), [*current.get("players", []), uid],
        )

        def _add(payload):
            nonlocal verdict, bye_before
            bye_before = payload.get("round_bye")
            verdict = admit_player_now(payload, uid, MAX_PLAYERS, forbidden_pairs=forbidden)

        await modify_payload(self.db, game_id, _add)
        self._poke_submit(game_id)
        name = member.display_name
        if verdict in ("joined", "joined-unbenched"):
            text = (
                f"{ICON} **{name}** joined Clapback — they're in this round, "
                f"starting on 0 points!"
            )
            if verdict == "joined-unbenched" and bye_before is not None:
                text += (
                    f"\n🪑 <@{bye_before}> you're back in this round — that "
                    f"evens the numbers, so hit **Submit**!"
                )
            return True, text
        if verdict in ("queued", "queued-parity"):
            return True, (
                f"{ICON} **{name}** joined Clapback — they'll play from the "
                f"next round!"
            )
        if verdict == "already-queued":
            return False, f"**{name}** is already queued for the next round."
        if verdict == "full":
            return False, f"Clapback is full at {MAX_PLAYERS} players."
        return False, f"**{name}** is already in this game."

    async def mid_game_leave(self, channel, game_id: str, member):
        """Remove *member* from a running game. Their score is withdrawn from
        the board (clapback-17): they are paid nothing either way, and the
        recap must not crown someone who walked out."""
        uid = member.id
        removed = False

        def _remove(payload):
            nonlocal removed
            removed = withdraw_player(payload, uid)

        await modify_payload(self.db, game_id, _remove)
        if not removed:
            return False, f"**{member.display_name}** isn't in this game."
        return True, (
            f"{ICON} **{member.display_name}** left Clapback — their score is "
            f"withdrawn from the board."
        )


async def setup(bot: "Bot"):
    cog = ClapbackCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("clapback")
    play.add_command(cog.clapback, override=True)
    bot.game_launchers["clapback"] = cog.launch
    bot.game_recoverers["clapback"] = cog.recover_game
    bot.lobby_auto_starters["clapback"] = cog.auto_start
    bot.game_joiners["clapback"] = cog.mid_game_join
    bot.game_leavers["clapback"] = cog.mid_game_leave
