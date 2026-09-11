"""
Name Your Price — game cog.

A scenario is posed and everyone secretly submits a dollar amount for how much
money it would take for them to do it.  Prices are revealed sorted lowest to
highest, then the room votes on "Most Reasonable" and "Most Unhinged."

The game opens a join lobby (``LOBBY_GAME_TYPES``): the roster is what lets a
round close the moment everyone has answered, and the lobby is what invites
the room in — until 2026-09-04 the command posted a placeholder and pinged the
host to write a scenario, and every round ran its full timer (trivia-tail-85 /
86). Scenarios come from the question bank unless the host chose otherwise;
when the host or the room writes them, the prompt is a button on the board,
not a public ping (trivia-tail-97).

Pure logic and embed builders live in
``bot_modules/games_price/{logic,embeds}.py``; this module keeps only
the Discord glue (slash command, modals, views, round loop).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord

from bot_modules.core.branding import safe_resolve_accent
from bot_modules.core.sticky import PanelContent, StickyPanel
from bot_modules.core.utils import disable_all_items, is_host_or_mod
from discord.ext import commands
from discord import app_commands

from bot_modules.games.constants import HOW_TO_PLAY, play_description
from bot_modules.games.command_groups import play
from bot_modules.games.utils.game_manager import (
    ConfirmCloseView,
    sign_off_game_chore,
    finish_launch_response,
    create_game,
    update_game_message,
    update_game_payload,
    update_game_state,
    get_game_payload,
    get_game_options,
    modify_payload,
    end_game,
    update_session,
    is_game_expired,
    resolve_name,
    resolve_names,
    channel_name,
)
from bot_modules.games.utils.launch_guard import refuse_launch
from bot_modules.games.utils.question_source import (
    channel_allows_nsfw,
    get_price_scenario,
    has_matching_questions,
)
from bot_modules.games.utils.timer import GameTimer
from bot_modules.games_price.embeds import (
    build_lobby_embed,
    build_recap_embed,
    build_reveal_embed,
    build_round_results_embed,
    build_scenario_embed,
    build_scenario_wait_embed,
    build_start_embed,
    build_vote_embed,
)
from bot_modules.games_price.logic import (
    MIN_PLAYERS,
    SOURCE_HOST,
    SOURCE_PLAYERS,
    build_ladder,
    collect_all_players,
    compute_highlight,
    compute_recap_awards,
    format_price,
    lobby_players,
    parse_price,
    resolve_source,
    roster_all_in,
    tally_winners,
    toggle_player,
    vote_possible,
)
from bot_modules.services.game_board_sticky_service import get_board_sticky_enabled
from bot_modules.services.game_start_ping_service import (
    extract_start_epoch,
    resolve_start_epoch,
)

log = logging.getLogger(__name__)

SCENARIO_WAIT_SECONDS = 120.0


# ── Modals ───────────────────────────────────────────────────────────────────

class PriceModal(discord.ui.Modal, title="Name Your Price"):
    price = discord.ui.TextInput(
        label="Your price ($)",
        placeholder="e.g. 500, $1,000, 5k, 1M",
        required=True,
        max_length=20,
    )

    def __init__(self, game_view: "PriceGameView"):
        super().__init__()
        self._view = game_view

    async def on_submit(self, interaction: discord.Interaction):
        log.info(
            "%s submitted price modal in #%s",
            interaction.user.display_name,
            channel_name(interaction.channel),
        )
        amount = parse_price(self.price.value)
        if amount is None:
            await interaction.response.send_message(
                "Couldn't parse that as a price. Try something like `500`, `$1,000`, `5k`, or `1M`.",
                ephemeral=True,
            )
            return

        view = self._view
        uid = interaction.user.id
        changed = uid in view.prices
        view.prices[uid] = amount
        label = f"✅ Submitted **{format_price(amount)}**"
        if changed:
            label += " (updated)"
        await interaction.response.send_message(label, ephemeral=True, delete_after=5)

        # Update submission count on embed
        await view.refresh_embed()

        # Auto-advance once everyone who joined has answered
        if view.everyone_in():
            view.skip_timer()


class HostScenarioModal(discord.ui.Modal, title="Write a Scenario"):
    scenario = discord.ui.TextInput(
        label="Scenario",
        placeholder="How much money would it cost for you to…",
        style=discord.TextStyle.long,
        required=True,
        max_length=500,
    )

    def __init__(self):
        super().__init__()
        self._result: str | None = None
        self._event = asyncio.Event()

    async def on_submit(self, interaction: discord.Interaction):
        log.info(
            "%s submitted scenario modal in #%s",
            interaction.user.display_name,
            channel_name(interaction.channel),
        )
        self._result = self.scenario.value.strip()
        await interaction.response.send_message("✅ Scenario submitted!", ephemeral=True, delete_after=5)
        self._event.set()

    def give_up(self) -> None:
        """Resolve with no text — the caller draws from the bank instead."""
        self._result = None
        self._event.set()

    @property
    def resolved(self) -> bool:
        """Has a scenario been submitted (or the bank chosen)? Discord never
        reports a dismissed modal, so *opening* must not count."""
        return self._event.is_set()

    async def wait_for_result(self, timeout: float = SCENARIO_WAIT_SECONDS) -> str | None:
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return self._result


class AddRoundsModal(discord.ui.Modal, title="Add Rounds"):
    count = discord.ui.TextInput(
        label="How many rounds to add?",
        placeholder="e.g. 3",
        required=True,
        max_length=3,
    )

    def __init__(self, cog: "PriceCog", game_id: str):
        super().__init__()
        self._cog = cog
        self._game_id = game_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            n = int(self.count.value.strip())
            if n < 1 or n > 20:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Enter a number between 1 and 20.", ephemeral=True)
            return

        payload = await get_game_payload(self._cog.db, self._game_id)
        payload["total_rounds"] = payload.get("total_rounds", 5) + n
        await update_game_payload(self._cog.db, self._game_id, payload)

        # Update view if tracked
        view = self._cog.bot.active_views.get(self._game_id)
        if isinstance(view, (PriceGameView, PriceVoteView)):
            view.total_rounds += n

        await interaction.response.send_message(
            f"✅ Added **{n}** rounds! New total: **{payload['total_rounds']}**.",
            ephemeral=True,
        )


# ── Select Menus for voting ──────────────────────────────────────────────────

class ReasonableSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(
            placeholder="🎯 Most Reasonable — Pick a player…",
            options=options,
            custom_id="price_vote_reasonable",
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        view = cast("PriceVoteView", self.view)
        uid = interaction.user.id
        target = int(self.values[0])
        if uid == target:
            await interaction.response.send_message("❌ You can't vote for yourself!", ephemeral=True)
            return
        changed = uid in view.reasonable_votes
        view.reasonable_votes[uid] = target
        target_name = resolve_name(interaction.guild, target)
        msg = f"✅ Voted **🎯 {discord.utils.escape_markdown(target_name)}** as Most Reasonable"
        if changed:
            msg += " (changed)"
        await interaction.response.send_message(msg, ephemeral=True, delete_after=5)

        if view.all_voted():
            view.skip_timer()


class UnhingedSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(
            placeholder="🤯 Most Unhinged — Pick a player…",
            options=options,
            custom_id="price_vote_unhinged",
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        view = cast("PriceVoteView", self.view)
        uid = interaction.user.id
        target = int(self.values[0])
        if uid == target:
            await interaction.response.send_message("❌ You can't vote for yourself!", ephemeral=True)
            return
        changed = uid in view.unhinged_votes
        view.unhinged_votes[uid] = target
        target_name = resolve_name(interaction.guild, target)
        msg = f"✅ Voted **🤯 {discord.utils.escape_markdown(target_name)}** as Most Unhinged"
        if changed:
            msg += " (changed)"
        await interaction.response.send_message(msg, ephemeral=True, delete_after=5)

        if view.all_voted():
            view.skip_timer()


# ── Views ────────────────────────────────────────────────────────────────────

class ScenarioPromptView(discord.ui.View):
    """The board's buttons while a scenario is being written.

    Sits on the game message instead of a public ``<@host>`` ping. For the
    ``host`` source only the host or a mod may write (or hand the round to
    the bank); for ``players`` anyone may write and the first submission
    wins. A second press after a submission is told so — after a *dismissed*
    modal the button simply opens it again, since Discord never reports the
    dismissal and the host used to be locked out for the whole wait.
    """

    def __init__(self, modal: HostScenarioModal, host_id: int, *, open_to_all: bool):
        super().__init__(timeout=SCENARIO_WAIT_SECONDS)
        self._modal = modal
        self.host_id = host_id
        self.open_to_all = open_to_all

    def _may_write(self, interaction: discord.Interaction) -> bool:
        return self.open_to_all or is_host_or_mod(interaction, self.host_id)

    @discord.ui.button(label="📝 Write Scenario", style=discord.ButtonStyle.primary, custom_id="price_write_scenario", row=0)
    async def write(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not self._may_write(interaction):
            await interaction.response.send_message("❌ Only the host or a mod can write the scenario.", ephemeral=True)
            return
        if self._modal.resolved:
            await interaction.response.send_message("Someone already submitted a scenario!", ephemeral=True)
            return
        await interaction.response.send_modal(self._modal)

    @discord.ui.button(label="🎲 Draw From the Bank", style=discord.ButtonStyle.secondary, custom_id="price_draw_bank", row=0)
    async def draw_bank(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can draw from the bank.", ephemeral=True)
            return
        await interaction.response.defer()
        self._modal.give_up()


class PriceLobbyView(discord.ui.View):
    """The join lobby: Join / Leave / Start / Help / Cancel."""

    def __init__(self, game_id: str, host_id: int, db, bot, cog: "PriceCog", accent: discord.Color | None = None):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self.cog = cog
        self.accent = accent
        self.message: discord.Message | None = None

    async def _refresh_lobby(self, interaction: discord.Interaction, payload: dict) -> None:
        guild = interaction.guild
        settings = payload.get("settings") or {}
        embed = build_lobby_embed(
            resolve_name(guild, self.host_id) if guild else "Host",
            resolve_names(guild, lobby_players(payload)),
            payload.get("total_rounds", settings.get("rounds", 5)),
            settings.get("source", SOURCE_HOST),
            color=self.accent,
            # Re-read from the payload, not the view: the countdown must
            # survive a restart that rebuilt this view from the DB.
            start_at=extract_start_epoch(payload),
            min_players=MIN_PLAYERS,
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="price_join", row=0)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        uid = interaction.user.id
        result: dict[str, str] = {}

        def _toggle(payload):
            if uid in lobby_players(payload):
                result["action"] = "already"
                return
            result["action"] = toggle_player(payload, uid)

        payload = await modify_payload(self.db, self.game_id, _toggle)
        await self._refresh_lobby(interaction, payload)
        if result.get("action") == "already":
            await interaction.followup.send("You're already in — press **Leave** to drop out.", ephemeral=True)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, custom_id="price_leave", row=0)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        uid = interaction.user.id

        def _remove(payload):
            if uid in lobby_players(payload):
                toggle_player(payload, uid)

        payload = await modify_payload(self.db, self.game_id, _remove)
        await self._refresh_lobby(interaction, payload)

    @discord.ui.button(label="Start", style=discord.ButtonStyle.primary, custom_id="price_start", row=0)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can start.", ephemeral=True)
            return
        payload = await get_game_payload(self.db, self.game_id)
        players = lobby_players(payload)
        if len(players) < MIN_PLAYERS:
            await interaction.response.send_message(
                f"Need at least {MIN_PLAYERS} players to start Name Your Price. Currently: {len(players)}.",
                ephemeral=True,
            )
            return
        self.stop()
        disable_all_items(self)
        await interaction.response.edit_message(view=self)
        message = self.message or interaction.message
        assert message is not None  # component interactions always carry their message
        await self.cog._begin_game(self.game_id, payload)
        await self.cog._start_rounds(self.game_id, self.host_id, interaction.channel, message, payload)

    @discord.ui.button(label="❓ Help", style=discord.ButtonStyle.secondary, custom_id="price_lobby_htp", row=1)
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["price"], ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="price_cancel", row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Close the lobby before it starts — host or mod, behind the usual
        confirm popup. A lobby has no roster that played, so this pays nobody."""
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can cancel the lobby.", ephemeral=True)
            return
        anchor = self.message or interaction.message

        async def _confirmed(_confirm: discord.Interaction) -> None:
            await self._cancel(anchor)

        await interaction.response.send_message(
            "⚠️ Are you sure you want to end this game?", view=ConfirmCloseView(_confirmed), ephemeral=True,
        )

    async def _cancel(self, anchor: discord.Message | None) -> None:
        self.stop()
        disable_all_items(self)
        if anchor is not None:
            try:
                await anchor.edit(content="🛑 Name Your Price was cancelled before it started.", view=self)
            except discord.HTTPException:
                pass
        await end_game(self.db, self.game_id, reason="cancelled")
        self.bot.active_views.pop(self.game_id, None)


class PriceGameView(discord.ui.View):
    """Main view during the submission phase of a round."""

    def __init__(
        self,
        game_id: str,
        host_id: int,
        host_name: str,
        scenario: str,
        round_num: int,
        total_rounds: int,
        timer_secs: int,
        db,
        bot,
        cog: "PriceCog",
        expected_ids: set[int] | None = None,
        accent: discord.Color | None = None,
        settings: dict | None = None,
        guild: "discord.Guild | None" = None,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.host_name = host_name
        self.scenario = scenario
        self.round_num = round_num
        self.total_rounds = total_rounds
        self.timer_secs = timer_secs
        self.guild = guild
        self.db = db
        self.bot = bot
        self.cog = cog
        # The lobby roster: the round closes as soon as every one of these
        # ids has priced. Anyone in the channel may submit, so this is a set
        # of ids, never a headcount. Empty (no roster) runs the full timer.
        self.expected_ids: set[int] = set(expected_ids or ())
        # Guild accent resolved once at view creation and reused on every
        # refresh — never re-resolve per modal submit / per embed refresh.
        self.accent = accent
        self.settings = settings or {}
        self.prices: dict[int, int] = {}
        self._msg: discord.Message | None = None
        self._timer: GameTimer | None = None
        self._closed = False
        # Set when the guild's sticky dial is on for this round's submission
        # board: the StickyPanel that owns reposting it, and the flag
        # PriceCog._retire_board sets once this round's board is done (the
        # panel's build callback checks this — see PriceCog._board_content).
        # Both stay None/False for a round when the dial is off.
        self._panel: "StickyPanel | None" = None
        self._board_retired: bool = False

    def everyone_in(self) -> bool:
        """Has everyone who joined named a price? (A spectator's price counts
        in the reveal but fills nobody's seat.)"""
        return roster_all_in(self.expected_ids, set(self.prices))

    def _build_embed(self) -> discord.Embed:
        return build_scenario_embed(
            self.host_name,
            self.scenario,
            self.round_num,
            self.total_rounds,
            self._timer.remaining if self._timer else self.timer_secs,
            len(self.prices),
            len(self.expected_ids) or None,
            color=self.accent,
            roster_submitted=(
                len(self.expected_ids & set(self.prices)) if self.expected_ids else None
            ),
        )

    async def refresh_embed(self):
        """Redraw the submission board with the current price count.

        When the sticky dial is on, this routes through the panel's own
        refresh instead of the cached ``self._msg`` — the panel may have
        reposted the board to the bottom of the channel since the last
        redraw, and an edit against the old ``self._msg`` would then
        silently 404 (same reasoning as RushmoreDraftView.refresh_board).
        """
        if self._panel is not None:
            # Guarded the same way PriceCog._open_board guards its own
            # guild.id read — a panel is only ever created when self.guild
            # was truthy at that point, but nothing re-asserts that here.
            if self.guild is not None:
                await self._panel.refresh(self.guild.id)
            return
        if self._msg:
            try:
                await self._msg.edit(embed=self._build_embed())
            except Exception as e:
                log.debug("Failed to refresh price embed: %s", e)

    def skip_timer(self):
        if self._timer:
            self._timer.skip()

    @discord.ui.button(label="💵 Name Your Price", style=discord.ButtonStyle.success, custom_id="price_submit", row=0)
    async def submit_price(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if self._closed:
            await interaction.response.send_message("This round is over.", ephemeral=True)
            return
        await interaction.response.send_modal(PriceModal(self))

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary, custom_id="price_skip", row=1)
    async def skip_round(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can skip.", ephemeral=True)
            return
        await interaction.response.defer()
        self.skip_timer()

    @discord.ui.button(label="➕ Add Rounds", style=discord.ButtonStyle.secondary, custom_id="price_add_rounds", row=1)
    async def add_rounds(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can add rounds.", ephemeral=True)
            return
        await interaction.response.send_modal(AddRoundsModal(self.cog, self.game_id))

    @discord.ui.button(label="❓ Help", style=discord.ButtonStyle.secondary, custom_id="price_htp", row=1)
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["price"], ephemeral=True)

    @discord.ui.button(label="End Game", style=discord.ButtonStyle.secondary, custom_id="price_end", row=1)
    async def end_game_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Stop after this round's prices and post the recap — host or mod,
        behind the usual confirm popup. The recap pays everyone who submitted
        in any round, this one included (trivia-tail-87)."""
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can end the game.", ephemeral=True)
            return
        channel = interaction.channel
        guild = interaction.guild

        async def _confirmed(_confirm: discord.Interaction) -> None:
            await self.cog.end_early(self, channel, guild)

        await interaction.response.send_message(
            "⚠️ Are you sure you want to end this game?", view=ConfirmCloseView(_confirmed), ephemeral=True,
        )


class PriceVoteView(discord.ui.View):
    """View during the voting phase — two select menus."""

    def __init__(
        self,
        game_id: str,
        host_id: int,
        host_name: str,
        submitters: list[int],
        prices: dict[int, int],
        round_num: int,
        total_rounds: int,
        timer_secs: int,
        guild,
        db,
        bot,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.host_name = host_name
        self.submitters = submitters
        self.prices = prices
        self.round_num = round_num
        self.total_rounds = total_rounds
        self.timer_secs = timer_secs
        self.guild = guild
        self.db = db
        self.bot = bot
        self.reasonable_votes: dict[int, int] = {}
        self.unhinged_votes: dict[int, int] = {}
        self._msg: discord.Message | None = None
        self._timer: GameTimer | None = None
        self._closed = False

        # Build select options from submitters
        options = []
        for uid in submitters:
            name = resolve_name(guild, uid)
            options.append(discord.SelectOption(
                label=f"{name} — {format_price(prices[uid])}",
                value=str(uid),
            ))

        self.add_item(ReasonableSelect(list(options)))
        self.add_item(UnhingedSelect(list(options)))

    def all_voted(self) -> bool:
        """True if every submitter has voted in both categories."""
        for uid in self.submitters:
            if uid not in self.reasonable_votes or uid not in self.unhinged_votes:
                return False
        return True

    def skip_timer(self):
        if self._timer:
            self._timer.skip()


class PriceRecapView(discord.ui.View):
    """Rides on the recap: **Run Again** opens the next lobby under whoever
    pressed it — that *is* the hand-off, so the old Hand Off button is gone
    and the host/mod gate with it, matching Mt. Rushmore Draft's recap card
    (social-prompt-45; Price's own Hand Off came from trivia-tail-97)."""

    def __init__(self, game_id: str, host_id: int, cog: "PriceCog", settings: dict):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.cog = cog
        self._settings = settings

    async def _relaunch(self, interaction: discord.Interaction) -> None:
        """Open a fresh lobby with the presser as host, behind the same gate
        as the slash entry: an admin who unticks the game on the dashboard
        mid-evening must not be overridden by the recap card."""
        refusal = await refuse_launch(self.cog.db, interaction, "price")
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        # Disable buttons on old recap
        disable_all_items(self)
        assert interaction.message  # component interactions always carry their message
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass
        self.stop()
        await interaction.response.defer()
        game_id = await self.cog.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={
                "rounds": self._settings.get("rounds", 5),
                "timer": self._settings.get("timer", 30),
                "vote_timer": self._settings.get("vote_timer", 20),
                "source": self._settings.get("source"),
            },
        )
        # A recap's relaunch button goes straight to the launcher, missing the
        # shared finish_launch_response seam — but a mod restarting a round by
        # hand has run a game. Signed off only on a launch that actually
        # produced a game (a falsy id means it failed, e.g. no send permission),
        # and after it, so a chore is never ticked green for a round that never
        # started.
        if game_id:
            await sign_off_game_chore(
                self.cog.bot, interaction.guild_id, interaction.user.id
            )

    @discord.ui.button(label="🔁 Run Again", style=discord.ButtonStyle.primary, custom_id="price_run_again")
    async def run_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Any member may press it and becomes the new host: same settings, a
        # fresh lobby, the presser at the keyboard.
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await self._relaunch(interaction)


# ── Cog ──────────────────────────────────────────────────────────────────────

class PriceCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot
        self._auto_tasks: set[asyncio.Task] = set()
        # One StickyPanel per *live round's submission board*, not one
        # shared instance — same shape as RushmoreCog._boards (see
        # docs/plans/sticky-panel-extraction.md, Group E), but scoped to a
        # round rather than a whole game: a fresh panel is made each round
        # in _open_board and dropped in _retire_board once that round's
        # submission window closes (disabled frame shown, or reveal posted).
        # Keyed by game_id — only one round is ever live per game at a time.
        self._boards: dict[str, StickyPanel] = {}

    @property
    def db(self):
        return self.bot.games_db

    async def cog_unload(self) -> None:
        # A reload mid-round must not leave a debounced restick armed
        # against a cog that's gone.
        for panel in list(self._boards.values()):
            panel.cancel_all()
        self._boards.clear()

    @commands.Cog.listener("on_message")
    async def _restick_boards(self, message: discord.Message) -> None:
        for panel in list(self._boards.values()):
            await panel.on_message(message)

    @commands.Cog.listener("on_guild_channel_delete")
    async def _forget_deleted_board_channel(
        self, channel: discord.abc.GuildChannel
    ) -> None:
        for panel in list(self._boards.values()):
            await panel.on_channel_delete(channel)

    # ── Sticky board (Games Global Config → Live Game Boards) ─────────
    # Same dial as Mt. Rushmore Draft (games_board_sticky_enabled) — one
    # dial covers every game wired up to it, not a per-game override.

    def _read_board_sticky_dial(self, guild_id: int) -> bool:
        with self.bot.ctx.open_db() as conn:
            return get_board_sticky_enabled(conn, guild_id)

    def _board_ids(self, game_id: str) -> tuple[int, int]:
        """``StickyPanel.load_ids`` for one game — reads the *live* row, not
        a second store, so it can never drift from what ``update_game_message``
        and the busy-check's jump link see."""
        with self.bot.ctx.open_db() as conn:
            row = conn.execute(
                "SELECT channel_id, message_id FROM games_active_games WHERE game_id = ?",
                (game_id,),
            ).fetchone()
        if row is None:
            return 0, 0
        return int(row["channel_id"] or 0), int(row["message_id"] or 0)

    def _save_board_ids(self, game_id: str, channel_id: int, message_id: int) -> None:
        """``StickyPanel.save_ids`` — same table/columns ``update_game_message``
        writes, so a repost keeps the jump link live."""
        with self.bot.ctx.open_db() as conn:
            conn.execute(
                "UPDATE games_active_games SET channel_id = ?, message_id = ? WHERE game_id = ?",
                (channel_id, message_id, game_id),
            )

    async def _board_content(
        self, game_view: "PriceGameView", guild: discord.Guild
    ) -> PanelContent:
        """``StickyPanel.build`` for one round's submission board. Raising is
        the guard against resurrection: a debounced restick that fires after
        this round's board has already retired must not redraw it.
        ``_board_retired`` — not ``_closed`` — is the flag this checks, since
        ``_closed`` is set one legitimate render (the disabled frame) before
        the board is actually retired; see ``_retire_board``."""
        if game_view._board_retired:
            raise RuntimeError(
                f"price board for game {game_view.game_id} round "
                f"{game_view.round_num} is retired"
            )
        return PanelContent(embed=game_view._build_embed(), view=game_view)

    def _make_board_panel(
        self, game_id: str, game_view: "PriceGameView"
    ) -> StickyPanel:
        def _load(_guild_id: int) -> tuple[int, int]:
            return self._board_ids(game_id)

        def _save(_guild_id: int, channel_id: int, message_id: int) -> None:
            self._save_board_ids(game_id, channel_id, message_id)

        async def _build(guild: discord.Guild) -> PanelContent:
            return await self._board_content(game_view, guild)

        return StickyPanel(
            f"price board {game_id}", self.bot,
            load_ids=_load, save_ids=_save, build=_build,
        )

    async def _open_board(
        self,
        game_id: str,
        game_view: "PriceGameView",
        channel,
        guild,
        msg: discord.Message,
    ) -> discord.Message:
        """Post a round's submission board — sticky if the guild has turned
        the dial on, an in-place edit of ``msg`` otherwise (the pre-existing
        behaviour). Read once, here, when the round's board is first posted:
        a mid-game flip of the dial never touches a round already running.

        No explicit delete of the message being replaced: ``_board_ids``
        (this panel's ``load_ids``) reads it out of the game row at this
        point (the previous round's board, or the lobby message at round 1),
        so ``place()`` treats it as "the old panel" and removes it itself —
        post-before-delete. Deleting it first would invert that: a placement
        failure would then leave the round with no board and no way to
        submit a price.
        """
        sticky = await asyncio.to_thread(
            self._read_board_sticky_dial, guild.id if guild else 0
        )
        if sticky and channel is not None:
            panel = self._make_board_panel(game_id, game_view)
            posted = await panel.place(guild, channel)
            if posted is not None:
                self._boards[game_id] = panel
                game_view._panel = panel
                return posted
            # Placement failed — fall through to the ordinary non-sticky
            # path rather than leave the round without a board.

        embed = game_view._build_embed()
        try:
            await msg.edit(embed=embed, view=game_view)
            return msg
        except Exception:
            new_msg = await channel.send(embed=embed, view=game_view)
            await update_game_message(self.db, game_id, new_msg.id)
            return new_msg

    def _retire_board(
        self, game_view: "PriceGameView", channel
    ) -> discord.Message | None:
        """Stop this round's submission board from reposting, and refuse to
        render it again. Called at every point the submission window closes
        (the disabled frame, a host's early End Game, or a forced
        ``/games end``) so the panel is released promptly rather than left
        for a straggling ``on_message`` to restick — same promptness rule as
        RushmoreCog._retire_board's ``forget()`` call. Idempotent and a
        no-op when the dial was never on for this round.

        Returns wherever the board actually ended up (``None`` when the dial
        was off), since the sticky panel may have reposted it since
        ``_open_board`` handed back its message — reveal, the disabled
        frame's own send-fallback, and the next round's own post must all
        target that, not whatever local ``msg`` variable the caller started
        the round with.
        """
        game_view._board_retired = True
        panel = self._boards.pop(game_view.game_id, None)
        if panel is None:
            return None
        panel.cancel_all()
        # Drop the cached ids too, not just the pending restick (Group D /
        # RushmoreCog._retire_board precedent): an on_message call already
        # in-flight when this round ends could otherwise still read the
        # panel's stale cached ids and schedule a restick nothing cancels,
        # reaching _board_content's refusal and logging it as an ERROR
        # traceback for a perfectly ordinary round ending.
        if game_view.guild is not None:
            panel.forget(game_view.guild.id)
        channel_id, message_id = self._board_ids(game_view.game_id)
        if channel is not None and message_id:
            return channel.get_partial_message(message_id)
        return None

    async def recover_game(self, row, payload, channel, message) -> bool:
        """After a restart: re-register a lobby's view, or re-drive the round
        loop from the next un-played round.

        ``completed_rounds`` counts rounds that finished *scoring* (a round's
        entry is written to payload["rounds"] earlier, at submission time, so it
        isn't a safe completion marker). _run_round is recursive, so we re-invoke
        it at completed_rounds+1 and roll scores back to the matching checkpoint,
        so a round interrupted mid-scoring neither double-counts nor is lost.
        """
        settings = payload.get("settings")
        if not settings:
            return False
        game_id = row["game_id"]
        host_id = int(row["host_id"])
        guild = getattr(channel, "guild", None)
        accent = await safe_resolve_accent(self.bot, guild, log_label="price")

        if row["state"] == "joining":
            view = PriceLobbyView(game_id, host_id, self.db, self.bot, self, accent=accent)
            view.message = message
            self.bot.active_views[game_id] = view
            self.bot.add_view(view, message_id=message.id)
            log.info("Recovered price lobby %s in #%s", game_id, getattr(channel, "name", channel.id))
            return True

        host_name = resolve_name(guild, host_id) if guild else "Host"
        total_rounds = payload.get("total_rounds", settings.get("rounds", 0))
        start_round = payload.get("completed_rounds", 0) + 1

        # Roll scores back to the last completed round so the interrupted round
        # (which may have written partial scores) re-runs from a clean base.
        if "scores_checkpoint" in payload:
            payload["scores"] = {k: dict(v) for k, v in payload["scores_checkpoint"].items()}
            await update_game_payload(self.db, game_id, payload)

        try:
            await message.edit(content="↻ Picking up where we left off after a restart…", view=None)
        except discord.HTTPException:
            pass
        if start_round > total_rounds:
            asyncio.create_task(self._show_recap(game_id, host_id, host_name, channel, guild, settings, accent=accent))
        else:
            asyncio.create_task(self._run_round(
                game_id=game_id, host_id=host_id, host_name=host_name,
                channel=channel, guild=guild, round_num=start_round,
                settings=settings, msg=message, accent=accent,
            ))
        log.info(
            "Recovering price game %s (resuming at round %d) in #%s",
            game_id, start_round, getattr(channel, "name", channel.id),
        )
        return True

    async def _advance_round(
        self, game_id, host_id, host_name, channel, guild, round_num, settings, msg,
        *, pre_round_delay: int = 0, accent: discord.Color | None = None,
    ):
        """Finish round_num: checkpoint scores, then go to the next round / recap.

        Called at every round-completion site so the checkpoint (used by
        recover_game) is written in exactly one place, after scoring is final.
        """
        payload = await get_game_payload(self.db, game_id)
        payload["completed_rounds"] = round_num
        scores = payload.get("scores", {})
        payload["scores_checkpoint"] = {k: dict(v) for k, v in scores.items()}
        await update_game_payload(self.db, game_id, payload)

        total_rounds = payload.get("total_rounds", settings["rounds"])
        if round_num < total_rounds:
            if pre_round_delay:
                await asyncio.sleep(pre_round_delay)
            await self._run_round(game_id, host_id, host_name, channel, guild, round_num + 1, settings, msg, accent=accent)
        else:
            await self._show_recap(game_id, host_id, host_name, channel, guild, settings, accent=accent)

    # ── Slash command ────────────────────────────────────────────────

    @app_commands.command(name="price", description=play_description("price"))
    @app_commands.describe(
        source="Where scenarios come from (default: the question bank, or the host if it's empty)",
        start_in="Countdown before the start, in minutes",
    )
    @app_commands.choices(
        source=[
            app_commands.Choice(name="Question bank", value="bank"),
            app_commands.Choice(name="Host writes", value="host"),
            app_commands.Choice(name="Players submit", value="players"),
        ],
    )
    async def price_cmd(
        self,
        interaction: discord.Interaction,
        source: str | None = None,
        start_in: app_commands.Range[int, 1, 60] | None = None,
    ):
        log.info(
            "%s used /games play price in #%s",
            interaction.user.display_name,
            channel_name(interaction.channel),
        )
        # The one launch guard every door shares: allowed channel, enabled
        # dial, and no game already running in this channel.
        refusal = await refuse_launch(self.db, interaction, "price")
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={"source": source, "start_in": start_in},
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
        """Interaction-free launch (slash command + scheduler). Opens the lobby;
        returns game_id, or None."""
        # Pacing knobs come from the per-server dashboard config; an explicit
        # *options* value (e.g. from a saved schedule) still wins.
        game_opts = await get_game_options(self.db, "price", guild_id)
        rounds = max(1, min(int(options.get("rounds", game_opts.get("rounds", 5))), 20))
        timer = max(10, min(int(options.get("timer", game_opts.get("timer", 30))), 120))
        vote_timer = max(10, min(int(options.get("vote_timer", game_opts.get("vote_timer", 20))), 60))
        tags_raw = options.get("tags") or []
        if isinstance(tags_raw, str):
            tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
        else:
            tags = [str(t).strip() for t in tags_raw if str(t).strip()]
        guild = getattr(channel, "guild", None)
        # No choice made → the bank when it holds anything, else the host.
        bank_has_rows = await has_matching_questions(
            self.db, "price", tags, allow_nsfw=channel_allows_nsfw(channel),
        )
        source = resolve_source(options.get("source"), bank_has_rows)
        start_epoch = resolve_start_epoch(options)

        settings = {
            "rounds": rounds,
            "timer": timer,
            "vote_timer": vote_timer,
            "source": source,
            "tags": tags,
        }
        payload: dict = {
            "settings": settings,
            "total_rounds": rounds,
            "rounds": {},
            "scores": {"reasonable_wins": {}, "unhinged_wins": {}},
            "players": [],
        }
        if start_epoch:
            payload["start_epoch"] = start_epoch

        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "price",
            state="joining",
            payload=payload,
            guild_id=guild_id,
        )
        log.info("Game %s (price) created by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))

        # Resolve the guild accent once for the whole game; threaded into every
        # non-winner embed builder below. Never re-resolved per round/guess.
        accent = await safe_resolve_accent(self.bot, guild, log_label="price")
        embed = build_lobby_embed(
            host_name, [], rounds, source, color=accent, start_at=start_epoch, min_players=MIN_PLAYERS,
        )
        view = PriceLobbyView(game_id, host_id, self.db, self.bot, self, accent=accent)
        self.bot.active_views[game_id] = view
        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("price launch lacked send perms in channel %s", channel.id)
            return None
        view.message = msg
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id

    # ── Lobby → play ─────────────────────────────────────────────────

    async def _begin_game(self, game_id: str, payload: dict) -> None:
        """Retire the ``joining`` state. Shared by Start and the countdown
        auto-start so both paths start a game the same way; the caller has
        already checked the roster against the floor."""
        await update_game_state(self.db, game_id, "playing")

    async def _start_rounds(self, game_id: str, host_id: int, channel, msg, payload: dict) -> None:
        """Kick off round 1 as a background task (the caller is a button or
        the sweep, neither of which can be held for the length of a game)."""
        guild = getattr(channel, "guild", None)
        settings = payload["settings"]
        accent = await safe_resolve_accent(self.bot, guild, log_label="price")
        host_name = resolve_name(guild, host_id) if guild else "Host"
        try:
            await msg.edit(embed=build_start_embed(host_name, 1, payload.get("total_rounds", settings["rounds"]), color=accent), view=None)
        except discord.HTTPException:
            pass
        task = asyncio.create_task(self._run_round(
            game_id=game_id, host_id=host_id, host_name=host_name,
            channel=channel, guild=guild, round_num=1,
            settings=settings, msg=msg, accent=accent,
        ))
        self._auto_tasks.add(task)
        task.add_done_callback(self._auto_tasks.discard)

    async def auto_start(self, row, payload: dict, channel) -> bool:
        """Start a countdown lobby without a button press.

        Registered in ``bot.lobby_auto_starters``; the start-ping sweep calls
        it when ``start_epoch`` arrives with at least ``MIN_PLAYERS`` joined.
        Returns False when the lobby can't start as it stands, in which case
        the sweep nudges the host instead.
        """
        game_id = row["game_id"]
        view = self.bot.active_views.get(game_id)
        if not isinstance(view, PriceLobbyView):
            log.debug("auto-start: price lobby %s has no live view", game_id)
            return False
        # Fresh roster: a join may have landed since the sweep read the row.
        payload = await get_game_payload(self.db, game_id)
        if len(lobby_players(payload)) < MIN_PLAYERS:
            return False

        view.stop()
        disable_all_items(view)
        message = view.message
        if message is None and row["message_id"]:
            try:
                message = await channel.fetch_message(int(row["message_id"]))
            except Exception:
                message = None
        if message is None:
            return False
        try:
            await message.edit(view=view)
        except discord.HTTPException:
            pass

        await self._begin_game(game_id, payload)
        await self._start_rounds(game_id, int(row["host_id"]), channel, message, payload)
        log.info("Game %s (price) auto-started at its countdown with %d players", game_id, len(lobby_players(payload)))
        return True

    # ── Round loop ───────────────────────────────────────────────────

    async def _get_scenario(
        self, settings: dict, host_id: int, host_name: str, channel, msg,
        round_num: int, total_rounds: int, accent: discord.Color | None,
    ) -> str | None:
        """Fetch a scenario based on the source setting."""
        source = settings["source"]
        tags = settings.get("tags") or None

        if source in (SOURCE_HOST, SOURCE_PLAYERS):
            written = await self._prompt_scenario(
                host_id, host_name, channel, msg, round_num, total_rounds, accent,
                open_to_all=source == SOURCE_PLAYERS,
            )
            if written:
                return written
            log.info("No %s scenario written for round %d, falling back to question bank", source, round_num)

        # "ai" and "both" were retired with the Prompts & AI studios; a game
        # persisted under either value falls through to the bank rather than
        # erroring out mid-round.
        return await get_price_scenario(self.db, tags=tags, allow_nsfw=channel_allows_nsfw(channel))

    async def _prompt_scenario(
        self, host_id: int, host_name: str, channel, msg, round_num: int,
        total_rounds: int, accent: discord.Color | None, *, open_to_all: bool,
    ) -> str | None:
        """Put the Write Scenario button on the board and wait for text.

        Returns None when nobody wrote one in time, or the host chose the
        bank. No public ping: the board is where the room is looking, and the
        button refuses anyone but the host (or, for ``players``, nobody).
        """
        modal = HostScenarioModal()
        view = ScenarioPromptView(modal, host_id, open_to_all=open_to_all)
        source = SOURCE_PLAYERS if open_to_all else SOURCE_HOST
        embed = build_scenario_wait_embed(host_name, round_num, total_rounds, source, color=accent)
        try:
            await msg.edit(embed=embed, view=view)
        except discord.HTTPException:
            try:
                await channel.send(embed=embed, view=view)
            except discord.HTTPException:
                return None

        result = await modal.wait_for_result(timeout=SCENARIO_WAIT_SECONDS)
        view.stop()
        return result or None

    async def _run_round(
        self,
        game_id: str,
        host_id: int,
        host_name: str,
        channel,
        guild,
        round_num: int,
        settings: dict,
        msg: discord.Message,
        accent: discord.Color | None = None,
    ):
        """Execute one full round: scenario → submit → reveal → vote → results."""
        # Check if game still exists
        if await is_game_expired(self.db, game_id):
            return

        payload = await get_game_payload(self.db, game_id)
        total_rounds = payload.get("total_rounds", settings["rounds"])

        # ── Get scenario ──
        scenario = await self._get_scenario(
            settings, host_id, host_name, channel, msg, round_num, total_rounds, accent,
        )
        if not scenario:
            try:
                await channel.send("❌ Couldn't generate a scenario. Skipping round.")
            except discord.HTTPException:
                pass
            # Advance to next round or end
            await self._advance_round(game_id, host_id, host_name, channel, guild, round_num, settings, msg, pre_round_delay=2, accent=accent)
            return

        if await is_game_expired(self.db, game_id):
            return
        payload = await get_game_payload(self.db, game_id)

        # ── Submission phase ──
        game_view = PriceGameView(
            game_id=game_id,
            host_id=host_id,
            host_name=host_name,
            scenario=scenario,
            round_num=round_num,
            total_rounds=total_rounds,
            timer_secs=settings["timer"],
            db=self.db,
            bot=self.bot,
            cog=self,
            # The lobby roster: the round closes once everyone on it has answered.
            expected_ids=set(lobby_players(payload)),
            accent=accent,
            settings=settings,
            guild=guild,
        )
        self.bot.active_views[game_id] = game_view

        # Post the submission board — sticky (reposts to the bottom of the
        # channel as chat buries it) for the length of this round's
        # submission window when the guild's dial is on, an in-place edit
        # of ``msg`` otherwise.
        msg = await self._open_board(game_id, game_view, channel, guild, msg)
        game_view._msg = msg

        # Start timer
        submission_done = asyncio.Event()

        async def on_submission_timer():
            submission_done.set()

        timer = GameTimer(
            duration=settings["timer"],
            message=msg,
            callback=on_submission_timer,
            timer_field_index=0,
        )
        game_view._timer = timer
        await timer.start()
        await submission_done.wait()

        if game_view._closed:
            # Forced end (/games end, or the host's own End Game button —
            # see end_early) while submissions were open. Whichever fired,
            # the board's retired already or is retiring right now;
            # idempotent either way.
            self._retire_board(game_view, channel)
            return

        # Disable submission view — the board's last render before it
        # retires below, so it goes through the panel too when sticky
        # (same reasoning as refresh_embed: an edit against a possibly
        # stale ``msg`` would silently 404 if a restick moved the board).
        game_view._closed = True
        disable_all_items(game_view)
        if game_view._panel is not None and guild is not None:
            await game_view._panel.refresh(guild.id)
        else:
            try:
                await msg.edit(view=game_view)
            except discord.HTTPException:
                pass

        prices = dict(game_view.prices)

        # Save round data to payload
        round_data = await self._record_round(game_id, round_num, scenario, prices)
        payload = await get_game_payload(self.db, game_id)
        total_rounds = payload.get("total_rounds", settings["rounds"])

        # The submission board's life ends here — everything from this
        # point on (reveal, vote, results, next round) is a fresh edit/send
        # against wherever the board actually ended up, not necessarily
        # ``msg``: the sticky panel may have reposted it since _open_board
        # handed ``msg`` back at the top of this round.
        current_msg = self._retire_board(game_view, channel) or msg

        # ── Handle 0 or 1 submissions ──
        if len(prices) == 0:
            try:
                await channel.send("Nobody submitted a price this round. Moving on…")
            except discord.HTTPException:
                pass
            await self._advance_round(game_id, host_id, host_name, channel, guild, round_num, settings, current_msg, pre_round_delay=3, accent=accent)
            return

        # ── Reveal phase ──
        ladder = build_ladder(prices)
        named_ladder = [(resolve_name(guild, uid), amt) for uid, amt in ladder]
        reveal_embed = build_reveal_embed(host_name, scenario, round_num, total_rounds, named_ladder, color=accent)

        try:
            await current_msg.edit(embed=reveal_embed, view=None)
        except discord.HTTPException:
            pass

        if not vote_possible(len(prices)):
            # One price has nothing to compare; two would be a forced 1-1
            # cross-vote and a guaranteed double tie.
            notice = (
                "Only one price submitted — skipping the vote."
                if len(prices) == 1
                else "Only two prices in — no vote this round. Moving on…"
            )
            try:
                await channel.send(notice)
            except discord.HTTPException:
                pass
            await asyncio.sleep(3)
            await self._advance_round(game_id, host_id, host_name, channel, guild, round_num, settings, current_msg, accent=accent)
            return

        # 5s pause for reactions
        await asyncio.sleep(5)

        if await is_game_expired(self.db, game_id):
            return

        # ── Voting phase ──
        submitters = [uid for uid, _ in ladder]
        vote_view = PriceVoteView(
            game_id=game_id,
            host_id=host_id,
            host_name=host_name,
            submitters=submitters,
            prices=prices,
            round_num=round_num,
            total_rounds=total_rounds,
            timer_secs=settings["vote_timer"],
            guild=guild,
            db=self.db,
            bot=self.bot,
        )
        self.bot.active_views[game_id] = vote_view

        vote_embed = build_vote_embed(host_name, scenario, round_num, total_rounds, settings["vote_timer"], color=accent)
        try:
            vote_msg = await channel.send(embed=vote_embed, view=vote_view)
            vote_view._msg = vote_msg
        except Exception:
            # Can't send vote view — skip voting
            await self._advance_round(game_id, host_id, host_name, channel, guild, round_num, settings, current_msg, accent=accent)
            return

        vote_done = asyncio.Event()

        async def on_vote_timer():
            vote_done.set()

        vote_timer = GameTimer(
            duration=settings["vote_timer"],
            message=vote_msg,
            callback=on_vote_timer,
            timer_field_index=0,
        )
        vote_view._timer = vote_timer
        await vote_timer.start()
        await vote_done.wait()

        # Bail if the game was force-ended (e.g. /games end) while voting.
        if game_id not in self.bot.active_views:
            return

        # Disable vote view
        vote_view._closed = True
        disable_all_items(vote_view)
        try:
            await vote_msg.edit(view=vote_view)
        except discord.HTTPException:
            pass

        # ── Tally votes ──
        r_winners, r_votes = tally_winners(vote_view.reasonable_votes)
        u_winners, u_votes = tally_winners(vote_view.unhinged_votes)

        # Save votes to payload
        payload = await get_game_payload(self.db, game_id)
        total_rounds = payload.get("total_rounds", settings["rounds"])
        rd = payload.setdefault("rounds", {}).setdefault(str(round_num), round_data)
        rd["votes"] = {
            "reasonable": {str(k): str(v) for k, v in vote_view.reasonable_votes.items()},
            "unhinged": {str(k): str(v) for k, v in vote_view.unhinged_votes.items()},
        }

        # Update running scores — all tied winners get a point
        scores = payload.setdefault("scores", {"reasonable_wins": {}, "unhinged_wins": {}})
        for uid in r_winners:
            key = str(uid)
            scores["reasonable_wins"][key] = scores["reasonable_wins"].get(key, 0) + 1
        for uid in u_winners:
            key = str(uid)
            scores["unhinged_wins"][key] = scores["unhinged_wins"].get(key, 0) + 1

        await update_game_payload(self.db, game_id, payload)

        # ── Show round results ──
        r_winner_uid = r_winners[0] if r_winners else None
        u_winner_uid = u_winners[0] if u_winners else None
        r_name = resolve_name(guild, r_winner_uid) if r_winner_uid else "Nobody"
        u_name = resolve_name(guild, u_winner_uid) if u_winner_uid else "Nobody"
        r_price = prices.get(r_winner_uid, 0) if r_winner_uid else 0
        u_price = prices.get(u_winner_uid, 0) if u_winner_uid else 0

        # If ties, list all winners
        if len(r_winners) > 1:
            r_name = " & ".join(resolve_name(guild, uid) for uid in r_winners)
        if len(u_winners) > 1:
            u_name = " & ".join(resolve_name(guild, uid) for uid in u_winners)

        results_embed = build_round_results_embed(
            host_name, round_num, total_rounds,
            r_name, r_price, r_votes,
            u_name, u_price, u_votes,
        )
        try:
            await vote_msg.edit(embed=results_embed, view=None)
        except discord.HTTPException:
            pass

        # ── Next round or recap ──
        await asyncio.sleep(5)
        await self._advance_round(game_id, host_id, host_name, channel, guild, round_num, settings, current_msg, accent=accent)

    async def _record_round(self, game_id: str, round_num: int, scenario: str, prices: dict[int, int]) -> dict:
        """Write a round's prices to the payload (locked): this is what the
        recap and every outside end path pay from."""
        round_data = {
            "scenario": scenario,
            "prices": {str(uid): amt for uid, amt in prices.items()},
            "votes": {"reasonable": {}, "unhinged": {}},
        }

        def _write(payload: dict) -> None:
            payload.setdefault("rounds", {})[str(round_num)] = round_data

        await modify_payload(self.db, game_id, _write)
        return round_data

    # ── Endings ──────────────────────────────────────────────────────

    async def end_early(self, game_view: PriceGameView, channel, guild) -> None:
        """The host's End Game during a round: keep the prices already in,
        stop the round, and post the recap — which pays everyone who
        submitted in any round through the same path a finished game uses."""
        if game_view._closed:
            return
        game_view._closed = True
        disable_all_items(game_view)
        if game_view._panel is not None and guild is not None:
            await game_view._panel.refresh(guild.id)
        elif game_view._msg is not None:
            try:
                await game_view._msg.edit(view=game_view)
            except discord.HTTPException:
                pass
        # Retire the board here rather than waiting for the round loop's own
        # wake-up to notice _closed (it's about to, via skip_timer() below,
        # but that's a second, concurrent coroutine) — same promptness rule
        # as RushmoreCog._retire_board's forget() call: a chat message
        # landing in that window could otherwise still restick a submission
        # board for a round whose recap is already posting. Idempotent
        # against the round loop's own (redundant) call to the same thing.
        self._retire_board(game_view, channel)
        if game_view.prices:
            await self._record_round(game_view.game_id, game_view.round_num, game_view.scenario, dict(game_view.prices))
        # Wake the round loop; it sees _closed and returns without advancing.
        game_view.skip_timer()
        await self._show_recap(
            game_view.game_id, game_view.host_id, game_view.host_name,
            channel, guild, game_view.settings, accent=game_view.accent,
        )

    async def _show_recap(self, game_id: str, host_id: int, host_name: str, channel, guild, settings: dict, accent: discord.Color | None = None):
        payload = await get_game_payload(self.db, game_id)
        if not payload:
            return  # already archived by another end path
        rounds_data = payload.get("rounds", {})
        scores = payload.get("scores", {"reasonable_wins": {}, "unhinged_wins": {}})

        all_players = collect_all_players(rounds_data)
        rounds_played = len(rounds_data)

        # Build awards — logic returns (label, [uids], detail); resolve uids here.
        raw_awards = compute_recap_awards(rounds_data, scores)
        awards: dict[str, tuple[str, str, str]] = {}
        for slug, (label, uids, detail) in raw_awards.items():
            name = " & ".join(resolve_name(guild, uid) for uid in uids)
            awards[slug] = (label, name, detail)

        # Highlight — widest spread round
        highlight: str | None = None
        hi = compute_highlight(rounds_data)
        if hi is not None:
            rnum, lo, hi_amt = hi
            highlight = (
                f"Round {rnum} had the widest spread — "
                f"{format_price(lo)} to {format_price(hi_amt)}"
            )

        recap_embed = build_recap_embed(host_name, rounds_played, len(all_players), awards, highlight, color=accent)
        if guild:
            from bot_modules.economy.game_rewards import append_payout_footer
            await append_payout_footer(self.bot, recap_embed, guild.id, "price")
        recap_view = PriceRecapView(game_id, host_id, self, settings)

        try:
            await channel.send(embed=recap_embed, view=recap_view)
        except discord.HTTPException:
            pass

        # End the game
        await end_game(
            self.db,
            game_id,
            player_count=len(all_players),
            round_count=rounds_played,
            payload=payload,
            bot=self.bot, player_ids=list(all_players),
        )
        if game_id in self.bot.active_views:
            del self.bot.active_views[game_id]


async def setup(bot: "Bot"):
    cog = PriceCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("price")
    play.add_command(cog.price_cmd, override=True)
    bot.game_launchers["price"] = cog.launch
    bot.game_recoverers["price"] = cog.recover_game
    bot.lobby_auto_starters["price"] = cog.auto_start
