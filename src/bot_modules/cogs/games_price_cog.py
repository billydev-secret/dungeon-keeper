"""
Name Your Price — game cog.

A scenario is posed and everyone secretly submits a dollar amount for how much
money it would take for them to do it.  Prices are revealed sorted lowest to
highest, then the room votes on "Most Reasonable" and "Most Unhinged."

Pure logic and embed builders live in
``bot_modules/games_price/{logic,embeds}.py``; this module keeps only
the Discord glue (slash command, modals, views, round loop).
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord
from discord.ext import commands
from discord import app_commands

from bot_modules.games.constants import GAME_ICONS, HOW_TO_PLAY, PHASE_RECAP
from bot_modules.games.command_groups import play
from bot_modules.games.utils.game_manager import (
    check_allowed_channel,
    check_game_enabled,
    create_game,
    update_game_message,
    update_game_payload,
    get_game_payload,
    get_game_options,
    end_game,
    update_session,
    is_game_expired,
    resolve_name,
)
from bot_modules.games.utils.question_source import get_price_scenario, channel_allows_nsfw
from bot_modules.games.utils.ai_client import generate_text
from bot_modules.games.utils.timer import GameTimer
from bot_modules.games_price.embeds import (
    build_recap_embed,
    build_reveal_embed,
    build_round_results_embed,
    build_scenario_embed,
    build_start_embed,
    build_vote_embed,
)
from bot_modules.games_price.logic import (
    build_ladder,
    collect_all_players,
    compute_highlight,
    compute_recap_awards,
    format_price,
    parse_price,
    tally_winners,
)

log = logging.getLogger(__name__)


# ── AI prompts ───────────────────────────────────────────────────────────────

PRICE_SYSTEM_PROMPT = (
    "You are generating 'Name Your Price' scenarios for an adult party game "
    "in this Discord community. Each scenario poses a situation "
    "where players must decide how much money it would take for them to do it. "
    "Scenarios should be funny, creative, slightly uncomfortable, or absurd. "
    "They should be things where different people would genuinely price differently."
)

PRICE_USER_PROMPT = (
    "Generate a single 'Name Your Price' scenario. "
    "Start with 'How much money would it cost for you to...' or similar phrasing. "
    "Examples:\n"
    "- How much money would it cost for you to let a stranger pick your next haircut?\n"
    "- How much to give up your phone for an entire month?\n"
    "- How much to eat nothing but gas station food for a week?\n"
    "- How much to let your ex write your dating profile?\n"
    "Return only the scenario text, no preamble."
)


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
            interaction.channel.name if interaction.channel else "unknown",
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

        # Auto-advance if all players submitted
        if view.expected_players and len(view.prices) >= view.expected_players:
            view.skip_timer()


class HostScenarioModal(discord.ui.Modal, title="Write a Scenario"):
    scenario = discord.ui.TextInput(
        label="Scenario",
        placeholder="How much money would it cost for you to...",
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
            interaction.channel.name if interaction.channel else "unknown",
        )
        self._result = self.scenario.value.strip()
        await interaction.response.send_message("✅ Scenario submitted!", ephemeral=True, delete_after=5)
        self._event.set()

    async def wait_for_result(self, timeout: float = 120.0) -> str | None:
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
        if view and hasattr(view, "total_rounds"):
            view.total_rounds += n

        await interaction.response.send_message(
            f"✅ Added **{n}** rounds! New total: **{payload['total_rounds']}**.",
            ephemeral=True,
        )


# ── Select Menus for voting ──────────────────────────────────────────────────

class ReasonableSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(
            placeholder="🎯 Most Reasonable — Select a player",
            options=options,
            custom_id="price_vote_reasonable",
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        view: PriceVoteView = self.view
        uid = interaction.user.id
        target = int(self.values[0])
        if uid == target:
            await interaction.response.send_message("You can't vote for yourself!", ephemeral=True)
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
            placeholder="🤯 Most Unhinged — Select a player",
            options=options,
            custom_id="price_vote_unhinged",
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        view: PriceVoteView = self.view
        uid = interaction.user.id
        target = int(self.values[0])
        if uid == target:
            await interaction.response.send_message("You can't vote for yourself!", ephemeral=True)
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

class HostWriteView(discord.ui.View):
    """Ephemeral view sent to host to open the scenario modal."""

    def __init__(self, modal: HostScenarioModal):
        super().__init__(timeout=120)
        self._modal = modal

    @discord.ui.button(label="📝 Write Scenario", style=discord.ButtonStyle.primary)
    async def write(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(self._modal)
        button.disabled = True
        try:
            await interaction.edit_original_response(view=self)
        except discord.HTTPException:
            pass


class PlayerWriteView(discord.ui.View):
    """View that lets any player submit a scenario. First submission wins."""

    def __init__(self, modal: HostScenarioModal):
        super().__init__(timeout=120)
        self._modal = modal
        self._submitted = False

    @discord.ui.button(label="📝 Write Scenario", style=discord.ButtonStyle.primary)
    async def write(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._submitted:
            await interaction.response.send_message("Someone already submitted a scenario!", ephemeral=True)
            return
        await interaction.response.send_modal(self._modal)
        self._submitted = True
        button.disabled = True
        try:
            await interaction.edit_original_response(view=self)
        except discord.HTTPException:
            pass


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
        expected_players: int | None = None,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.host_name = host_name
        self.scenario = scenario
        self.round_num = round_num
        self.total_rounds = total_rounds
        self.timer_secs = timer_secs
        self.db = db
        self.bot = bot
        self.cog = cog
        self.expected_players = expected_players
        self.prices: dict[int, int] = {}
        self._msg: discord.Message | None = None
        self._timer: GameTimer | None = None
        self._closed = False

    def is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild:
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    def _build_embed(self) -> discord.Embed:
        return build_scenario_embed(
            self.host_name,
            self.scenario,
            self.round_num,
            self.total_rounds,
            self._timer.remaining if self._timer else self.timer_secs,
            len(self.prices),
            self.expected_players,
        )

    async def refresh_embed(self):
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
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if self._closed:
            await interaction.response.send_message("This round is over.", ephemeral=True)
            return
        await interaction.response.send_modal(PriceModal(self))

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary, custom_id="price_skip", row=1)
    async def skip_round(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can skip.", ephemeral=True)
            return
        await interaction.response.defer()
        self.skip_timer()

    @discord.ui.button(label="➕ Add", style=discord.ButtonStyle.secondary, custom_id="price_add_rounds", row=1)
    async def add_rounds(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can add rounds.", ephemeral=True)
            return
        await interaction.response.send_modal(AddRoundsModal(self.cog, self.game_id))

    @discord.ui.button(label="❓ Help", style=discord.ButtonStyle.secondary, custom_id="price_htp", row=2)
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        await interaction.response.send_message(HOW_TO_PLAY["price"], ephemeral=True)


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
    """Shown on the game-over recap embed."""

    def __init__(self, game_id: str, host_id: int, cog: "PriceCog", settings: dict):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.cog = cog
        self._settings = settings

    def is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild:
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    @discord.ui.button(label="🔁 Run Again", style=discord.ButtonStyle.primary, custom_id="price_run_again")
    async def run_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can restart.", ephemeral=True)
            return
        # Disable buttons on old recap
        for item in self.children:
            item.disabled = True
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass
        self.stop()
        await interaction.response.defer()
        await self.cog.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={
                "rounds": self._settings.get("rounds", 5),
                "timer": self._settings.get("timer", 30),
                "vote_timer": self._settings.get("vote_timer", 20),
                "source": self._settings.get("source", "host"),
            },
        )

    @discord.ui.button(label="🔄 Hand Off", style=discord.ButtonStyle.secondary, custom_id="price_hand_off")
    async def hand_off(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can hand off.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Type the **/price** command to start a new game as the new host!",
            ephemeral=True,
        )
        for item in self.children:
            item.disabled = True
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass
        self.stop()


# ── Cog ──────────────────────────────────────────────────────────────────────

class PriceCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Re-drive the round loop from the next un-played round after a restart.

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
            asyncio.create_task(self._show_recap(game_id, host_id, host_name, channel, guild, settings))
        else:
            asyncio.create_task(self._run_round(
                game_id=game_id, host_id=host_id, host_name=host_name,
                channel=channel, guild=guild, round_num=start_round,
                settings=settings, msg=message,
            ))
        log.info(
            "Recovering price game %s (resuming at round %d) in #%s",
            game_id, start_round, getattr(channel, "name", channel.id),
        )
        return True

    async def _advance_round(
        self, game_id, host_id, host_name, channel, guild, round_num, settings, msg,
        *, pre_round_delay: int = 0,
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
            await self._run_round(game_id, host_id, host_name, channel, guild, round_num + 1, settings, msg)
        else:
            await self._show_recap(game_id, host_id, host_name, channel, guild, settings)

    # ── Slash command ────────────────────────────────────────────────

    @app_commands.command(name="price", description="Start a Name Your Price game!")
    @app_commands.describe(
        source="Where scenarios come from",
    )
    @app_commands.choices(
        source=[
            app_commands.Choice(name="Host writes", value="host"),
            app_commands.Choice(name="Players submit", value="players"),
            app_commands.Choice(name="AI generated", value="ai"),
            app_commands.Choice(name="Question bank", value="bank"),
            app_commands.Choice(name="AI + Bank mix", value="both"),
        ],
    )
    async def price_cmd(
        self,
        interaction: discord.Interaction,
        source: str = "host",
    ):
        log.info(
            "%s used /games play price in #%s",
            interaction.user.display_name,
            interaction.channel.name if interaction.channel else "unknown",
        )
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it from the web dashboard.",
                ephemeral=True,
            )
            return
        if not await check_game_enabled(self.db, "price", interaction.guild_id or 0):
            await interaction.response.send_message("Name Your Price is currently disabled on this server.", ephemeral=True)
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={"source": source},
        )
        if game_id is None:
            try:
                await interaction.followup.send(
                    "I don't have access to send messages in that channel. "
                    "Please grant me **View Channel**, **Send Messages**, and **Embed Links**.",
                    ephemeral=True,
                )
            except discord.HTTPException:
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
        # Pacing knobs come from the per-server dashboard config; an explicit
        # *options* value (e.g. from a saved schedule) still wins.
        game_opts = await get_game_options(self.db, "price", guild_id)
        rounds = max(1, min(int(options.get("rounds", game_opts.get("rounds", 5))), 20))
        timer = max(10, min(int(options.get("timer", game_opts.get("timer", 30))), 120))
        vote_timer = max(10, min(int(options.get("vote_timer", game_opts.get("vote_timer", 20))), 60))
        source = options.get("source", "host")
        guild = getattr(channel, "guild", None)

        settings = {
            "rounds": rounds,
            "timer": timer,
            "vote_timer": vote_timer,
            "source": source,
            "tags": options.get("tags") or [],
        }

        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "price",
            state="playing",
            payload={
                "settings": settings,
                "total_rounds": rounds,
                "rounds": {},
                "scores": {"reasonable_wins": {}, "unhinged_wins": {}},
            },
        )
        log.info("Game %s (price) created by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))

        embed = build_start_embed(host_name, 1, rounds)
        try:
            msg = await channel.send(embed=embed)
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("price launch lacked send perms in channel %s", channel.id)
            return None
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])

        # Start the first round
        asyncio.create_task(self._run_round(
            game_id=game_id,
            host_id=host_id,
            host_name=host_name,
            channel=channel,
            guild=guild,
            round_num=1,
            settings=settings,
            msg=msg,
        ))
        return game_id

    # ── Round loop ───────────────────────────────────────────────────

    async def _get_scenario(self, settings: dict, host_id: int, channel, interaction_or_msg) -> str | None:
        """Fetch a scenario based on the source setting."""
        source = settings["source"]
        tags = settings.get("tags") or None

        if source == "host":
            return await self._host_scenario(host_id, channel, interaction_or_msg, tags=tags)

        if source == "players":
            return await self._player_scenario(channel, interaction_or_msg, tags=tags)

        if source == "ai":
            return await self._ai_scenario()

        if source == "bank":
            return await get_price_scenario(self.db, tags=tags, allow_nsfw=channel_allows_nsfw(channel))

        if source == "both":
            if random.random() < 0.5:
                result = await get_price_scenario(self.db, tags=tags, allow_nsfw=channel_allows_nsfw(channel))
                if result:
                    return result
            return await self._ai_scenario()

        return None

    async def _ai_scenario(self) -> str | None:
        return await generate_text(
            PRICE_SYSTEM_PROMPT,
            PRICE_USER_PROMPT,
            model="gpt-4o-mini",
            max_tokens=150,
        )

    async def _host_scenario(self, host_id: int, channel, msg, tags: list[str] | None = None) -> str | None:
        """Prompt the host to write a scenario. Returns text or None on timeout."""
        modal = HostScenarioModal()
        write_view = HostWriteView(modal)
        try:
            prompt_msg = await channel.send(
                f"<@{host_id}> — write this round's scenario!",
                view=write_view,
            )
        except Exception:
            return None

        result = await modal.wait_for_result(timeout=120.0)

        # Clean up prompt message
        try:
            await prompt_msg.delete()
        except discord.HTTPException:
            pass

        if not result:
            log.info("Host scenario timed out, falling back to question bank")
            return await get_price_scenario(self.db, tags=tags, allow_nsfw=channel_allows_nsfw(channel))

        return result

    async def _player_scenario(self, channel, msg, tags: list[str] | None = None) -> str | None:
        """Let any player submit a scenario. First submission wins."""
        modal = HostScenarioModal()
        write_view = PlayerWriteView(modal)
        try:
            prompt_msg = await channel.send(
                "Anyone can write this round's scenario! First submission wins.",
                view=write_view,
            )
        except Exception:
            return None

        result = await modal.wait_for_result(timeout=120.0)

        try:
            await prompt_msg.delete()
        except discord.HTTPException:
            pass

        if not result:
            log.info("Player scenario timed out, falling back to question bank")
            return await get_price_scenario(self.db, tags=tags, allow_nsfw=channel_allows_nsfw(channel))

        return result

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
    ):
        """Execute one full round: scenario → submit → reveal → vote → results."""
        # Check if game still exists
        if await is_game_expired(self.db, game_id):
            return

        payload = await get_game_payload(self.db, game_id)
        total_rounds = payload.get("total_rounds", settings["rounds"])

        # ── Get scenario ──
        scenario = await self._get_scenario(settings, host_id, channel, msg)
        if not scenario:
            # Fall back to question bank, then AI as last resort
            scenario = await get_price_scenario(
                self.db, tags=settings.get("tags") or None,
                allow_nsfw=channel_allows_nsfw(channel),
            )
        if not scenario:
            scenario = await self._ai_scenario()
        if not scenario:
            try:
                await channel.send("❌ Couldn't generate a scenario. Skipping round.")
            except discord.HTTPException:
                pass
            # Advance to next round or end
            await self._advance_round(game_id, host_id, host_name, channel, guild, round_num, settings, msg, pre_round_delay=2)
            return

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
        )
        self.bot.active_views[game_id] = game_view

        embed = game_view._build_embed()
        try:
            await msg.edit(embed=embed, view=game_view)
            game_view._msg = msg
        except Exception:
            new_msg = await channel.send(embed=embed, view=game_view)
            game_view._msg = new_msg
            msg = new_msg
            await update_game_message(self.db, game_id, msg.id)

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
            return

        # Disable submission view
        game_view._closed = True
        for item in game_view.children:
            item.disabled = True
        try:
            await msg.edit(view=game_view)
        except discord.HTTPException:
            pass

        prices = dict(game_view.prices)

        # Save round data to payload
        payload = await get_game_payload(self.db, game_id)
        total_rounds = payload.get("total_rounds", settings["rounds"])
        round_data = {
            "scenario": scenario,
            "prices": {str(uid): amt for uid, amt in prices.items()},
            "votes": {"reasonable": {}, "unhinged": {}},
        }
        payload.setdefault("rounds", {})[str(round_num)] = round_data
        await update_game_payload(self.db, game_id, payload)

        # ── Handle 0 or 1 submissions ──
        if len(prices) == 0:
            try:
                await channel.send("Nobody submitted a price this round. Moving on...")
            except discord.HTTPException:
                pass
            await self._advance_round(game_id, host_id, host_name, channel, guild, round_num, settings, msg, pre_round_delay=3)
            return

        # ── Reveal phase ──
        ladder = build_ladder(prices)
        named_ladder = [(resolve_name(guild, uid), amt) for uid, amt in ladder]
        reveal_embed = build_reveal_embed(host_name, scenario, round_num, total_rounds, named_ladder)

        try:
            await msg.edit(embed=reveal_embed, view=None)
        except discord.HTTPException:
            pass

        if len(prices) == 1:
            try:
                await channel.send("Only one price submitted — skipping the vote.")
            except discord.HTTPException:
                pass
            await asyncio.sleep(3)
            await self._advance_round(game_id, host_id, host_name, channel, guild, round_num, settings, msg)
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

        vote_embed = build_vote_embed(host_name, scenario, round_num, total_rounds, settings["vote_timer"])
        try:
            vote_msg = await channel.send(embed=vote_embed, view=vote_view)
            vote_view._msg = vote_msg
        except Exception:
            # Can't send vote view — skip voting
            await self._advance_round(game_id, host_id, host_name, channel, guild, round_num, settings, msg)
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
        for item in vote_view.children:
            item.disabled = True
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
        await self._advance_round(game_id, host_id, host_name, channel, guild, round_num, settings, msg)

    # ── Recap ────────────────────────────────────────────────────────

    async def _show_recap(self, game_id: str, host_id: int, host_name: str, channel, guild, settings: dict):
        payload = await get_game_payload(self.db, game_id)
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

        recap_embed = build_recap_embed(host_name, rounds_played, len(all_players), awards, highlight)
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
        )
        if game_id in self.bot.active_views:
            del self.bot.active_views[game_id]

    async def _end_game(self, game_id: str, game_msg=None, channel=None):
        """Force-close the game (from close button)."""
        await end_game(self.db, game_id)
        if game_id in self.bot.active_views:
            del self.bot.active_views[game_id]
        if game_msg:
            try:
                embed = discord.Embed(
                    title=f"{GAME_ICONS['price']} NAME YOUR PRICE — CLOSED",
                    description="This game was closed by the host.",
                    color=PHASE_RECAP,
                )
                await game_msg.edit(embed=embed, view=None)
            except Exception:
                log.exception("price: failed to edit closed-game message")


async def setup(bot: "Bot"):
    cog = PriceCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("price")
    play.add_command(cog.price_cmd, override=True)
    bot.game_launchers["price"] = cog.launch
    bot.game_recoverers["price"] = cog.recover_game
