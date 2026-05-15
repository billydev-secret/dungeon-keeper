"""
Name Your Price — game cog.

A scenario is posed and everyone secretly submits a dollar amount for how much
money it would take for them to do it.  Prices are revealed sorted lowest to
highest, then the room votes on "Most Reasonable" and "Most Unhinged."
"""

import asyncio
import logging
import random
import statistics

import discord
from discord.ext import commands
from discord import app_commands

from bot_modules.games.constants import GAME_ICONS, HOW_TO_PLAY, PHASE_JOINING, PHASE_PLAYING, PHASE_RESULTS, PHASE_RECAP
from bot_modules.games.utils.game_manager import (
    check_allowed_channel,
    check_game_enabled,
    create_game,
    update_game_message,
    update_game_payload,
    get_game_payload,
    end_game,
    update_session,
    is_game_expired,
    ConfirmCloseView,
    resolve_name,
)
from bot_modules.games.utils.question_source import get_price_scenario
from bot_modules.games.utils.ai_client import generate_text
from bot_modules.games.utils.timer import GameTimer

log = logging.getLogger(__name__)

# ── Price parsing / formatting ───────────────────────────────────────────────

def parse_price(raw: str) -> int | None:
    """Parse user input into an integer dollar amount (0 – 999,999,999)."""
    cleaned = raw.strip().replace("$", "").replace(",", "").strip()
    if not cleaned:
        return None

    multipliers = {
        "k": 1_000, "m": 1_000_000, "million": 1_000_000,
        "b": 1_000_000_000, "billion": 1_000_000_000,
    }
    lower = cleaned.lower()
    for suffix, mult in multipliers.items():
        if lower.endswith(suffix):
            num_part = cleaned[: len(cleaned) - len(suffix)].strip()
            try:
                return max(0, min(int(float(num_part) * mult), 999_999_999))
            except ValueError:
                return None

    try:
        value = int(float(cleaned))
        return max(0, min(value, 999_999_999))
    except ValueError:
        return None


def format_price(amount: int) -> str:
    if amount >= 1_000_000_000:
        return f"${amount / 1_000_000_000:.1f}B"
    elif amount >= 1_000_000:
        return f"${amount / 1_000_000:.1f}M"
    elif amount >= 1_000:
        return f"${amount:,}"
    else:
        return f"${amount}"


def _price_label(amount: int) -> str:
    """Price string with optional flavour for extremes."""
    base = format_price(amount)
    if amount == 0:
        return f"{base} (free?!)"
    if amount >= 999_000_000:
        return f"{base} (absolutely not)"
    return base


# ── AI prompts ───────────────────────────────────────────────────────────────

PRICE_SYSTEM_PROMPT = (
    "You are generating 'Name Your Price' scenarios for an adult party game "
    "in the Golden Meadow Discord community. Each scenario poses a situation "
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


# ── Embed builders ───────────────────────────────────────────────────────────

def _footer(host_name: str) -> str:
    return f"{GAME_ICONS['price']} Name Your Price • Hosted by {host_name}"


def build_start_embed(host_name: str, round_num: int, total_rounds: int) -> discord.Embed:
    embed = discord.Embed(
        title=f"{GAME_ICONS['price']} NAME YOUR PRICE",
        description=f"Hosted by: **{discord.utils.escape_markdown(host_name)}** | "
                    f"Round {round_num}/{total_rounds}",
        color=PHASE_JOINING,
    )
    embed.add_field(name="Status", value="Starting up — first scenario incoming...", inline=False)
    embed.set_footer(text=_footer(host_name))
    return embed


def build_scenario_embed(
    host_name: str,
    scenario: str,
    round_num: int,
    total_rounds: int,
    timer_secs: int,
    submitted: int,
    total_players: int | None = None,
) -> discord.Embed:
    from bot_modules.games.utils.timer import format_deadline, now_plus
    embed = discord.Embed(
        title=f"{GAME_ICONS['price']} NAME YOUR PRICE — Round {round_num}/{total_rounds}",
        color=PHASE_PLAYING,
    )
    embed.add_field(name="Timer", value=format_deadline(now_plus(timer_secs)), inline=False)
    embed.add_field(
        name="Scenario",
        value=f'# "{discord.utils.escape_markdown(scenario)}"',
        inline=False,
    )
    sub_text = f"💵 Submitted: **{submitted}**"
    if total_players is not None:
        sub_text += f"/{total_players}"
    embed.add_field(name="Submissions", value=sub_text, inline=False)
    embed.set_footer(text=_footer(host_name))
    return embed


def build_reveal_embed(
    host_name: str,
    scenario: str,
    round_num: int,
    total_rounds: int,
    ladder: list[tuple[str, int]],
    guild,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"{GAME_ICONS['price']} REVEAL — Round {round_num}/{total_rounds}",
        color=PHASE_RESULTS,
    )
    embed.add_field(
        name="Scenario",
        value=f'"{discord.utils.escape_markdown(scenario)}"',
        inline=False,
    )
    lines = []
    for name, amount in ladder:
        lines.append(f"`{format_price(amount):>12}` — **{discord.utils.escape_markdown(name)}**"
                      + (" (free?!)" if amount == 0 else "")
                      + (" (absolutely not)" if amount >= 999_000_000 else ""))
    embed.add_field(name="💵 Price Ladder", value="\n".join(lines) or "—", inline=False)

    amounts = [a for _, a in ladder]
    if amounts:
        spread = f"{format_price(min(amounts))} — {format_price(max(amounts))}"
        median = format_price(int(statistics.median(amounts)))
        avg = format_price(int(statistics.mean(amounts)))
        embed.add_field(name="📊 Stats", value=f"Spread: {spread}\nMedian: {median}\nAverage: {avg}", inline=False)

    embed.set_footer(text=_footer(host_name))
    return embed


def build_vote_embed(
    host_name: str,
    scenario: str,
    round_num: int,
    total_rounds: int,
    timer_secs: int,
) -> discord.Embed:
    from bot_modules.games.utils.timer import format_deadline, now_plus
    embed = discord.Embed(
        title=f"{GAME_ICONS['price']} VOTE — Round {round_num}/{total_rounds}",
        color=PHASE_PLAYING,
    )
    embed.add_field(name="Timer", value=format_deadline(now_plus(timer_secs)), inline=False)
    embed.add_field(
        name="Scenario",
        value=f'"{discord.utils.escape_markdown(scenario)}"',
        inline=False,
    )
    embed.add_field(
        name="Vote",
        value="Who had the **Most Reasonable** price? Who was the **Most Unhinged**?",
        inline=False,
    )
    embed.set_footer(text=_footer(host_name))
    return embed


def build_round_results_embed(
    host_name: str,
    round_num: int,
    total_rounds: int,
    reasonable_winner: str,
    reasonable_price: int,
    reasonable_votes: int,
    unhinged_winner: str,
    unhinged_price: int,
    unhinged_votes: int,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"{GAME_ICONS['price']} ROUND {round_num} RESULTS",
        color=PHASE_RESULTS,
    )
    embed.add_field(
        name="🎯 Most Reasonable",
        value=f"**{discord.utils.escape_markdown(reasonable_winner)}** ({format_price(reasonable_price)}) — {reasonable_votes} vote{'s' if reasonable_votes != 1 else ''}",
        inline=False,
    )
    embed.add_field(
        name="🤯 Most Unhinged",
        value=f"**{discord.utils.escape_markdown(unhinged_winner)}** ({format_price(unhinged_price)}) — {unhinged_votes} vote{'s' if unhinged_votes != 1 else ''}",
        inline=False,
    )
    embed.set_footer(text=_footer(host_name))
    return embed


def build_recap_embed(
    host_name: str,
    rounds_played: int,
    player_count: int,
    awards: dict,
    highlight: str | None,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"{GAME_ICONS['price']} NAME YOUR PRICE — GAME OVER",
        color=PHASE_RECAP,
    )
    embed.add_field(
        name="Summary",
        value=f"🎮 Rounds played: **{rounds_played}**\n👥 Players: **{player_count}**",
        inline=False,
    )
    award_lines = []
    for key, (label, name, detail) in awards.items():
        if name:
            award_lines.append(f"{label} **{discord.utils.escape_markdown(name)}** — {detail}")
    if award_lines:
        embed.add_field(name="🏆 Awards", value="\n".join(award_lines), inline=False)
    if highlight:
        embed.add_field(name="💡 Highlight", value=highlight, inline=False)
    embed.set_footer(text=_footer(host_name))
    return embed


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
        except Exception:
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
        except Exception:
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

    @discord.ui.button(label="⏭️ Skip Round", style=discord.ButtonStyle.secondary, custom_id="price_skip", row=1)
    async def skip_round(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can skip.", ephemeral=True)
            return
        await interaction.response.defer()
        self.skip_timer()

    @discord.ui.button(label="➕ Add Rounds", style=discord.ButtonStyle.secondary, custom_id="price_add_rounds", row=1)
    async def add_rounds(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can add rounds.", ephemeral=True)
            return
        await interaction.response.send_modal(AddRoundsModal(self.cog, self.game_id))

    @discord.ui.button(label="🛑 Close Game", style=discord.ButtonStyle.danger, custom_id="price_close", row=2)
    async def close_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can close.", ephemeral=True)
            return
        game_msg = self._msg
        channel = interaction.channel

        async def _confirmed(confirm_interaction):
            self._closed = True
            if self._timer:
                self._timer.cancel()
            await self.cog._end_game(self.game_id, game_msg=game_msg, channel=channel)

        view = ConfirmCloseView(_confirmed)
        await interaction.response.send_message("⚠️ Are you sure you want to end this game?", view=view, ephemeral=True)

    @discord.ui.button(label="❓ How to Play", style=discord.ButtonStyle.secondary, custom_id="price_htp", row=2)
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
        except Exception:
            pass
        self.stop()
        await self.cog.start_price(
            interaction,
            rounds=self._settings.get("rounds", 5),
            timer=self._settings.get("timer", 30),
            vote_timer=self._settings.get("vote_timer", 20),
            source=self._settings.get("source", "host"),
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
        except Exception:
            pass
        self.stop()


# ── Cog ──────────────────────────────────────────────────────────────────────

class PriceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    # ── Slash command ────────────────────────────────────────────────

    @app_commands.command(name="price", description="Start a Name Your Price game!")
    @app_commands.describe(
        rounds="Number of rounds (1-20, default 5)",
        timer="Seconds for price submission per round (default 30)",
        vote_timer="Seconds for voting per round (default 20)",
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
        rounds: int = 5,
        timer: int = 30,
        vote_timer: int = 20,
        source: str = "host",
    ):
        await self.start_price(interaction, rounds, timer, vote_timer, source=source)

    async def start_price(
        self,
        interaction: discord.Interaction,
        rounds: int = 5,
        timer: int = 30,
        vote_timer: int = 20,
        source: str = "host",
    ):
        log.info(
            "%s used /price in #%s",
            interaction.user.display_name,
            interaction.channel.name if interaction.channel else "unknown",
        )
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it with `/games allow-channel`.",
                ephemeral=True,
            )
            return
        if not await check_game_enabled(self.db, "price", interaction.guild_id or 0):
            await interaction.response.send_message("Name Your Price is currently disabled on this server.", ephemeral=True)
            return

        rounds = max(1, min(rounds, 20))
        timer = max(10, min(timer, 120))
        vote_timer = max(10, min(vote_timer, 60))

        settings = {
            "rounds": rounds,
            "timer": timer,
            "vote_timer": vote_timer,
            "source": source,
        }

        game_id = await create_game(
            self.db,
            interaction.channel_id,
            interaction.user.id,
            "price",
            state="playing",
            payload={
                "settings": settings,
                "total_rounds": rounds,
                "rounds": {},
                "scores": {"reasonable_wins": {}, "unhinged_wins": {}},
            },
        )
        log.info(
            "Game %s (price) created by %s in #%s",
            game_id,
            interaction.user.display_name,
            interaction.channel.name if interaction.channel else "unknown",
        )

        embed = build_start_embed(interaction.user.display_name, 1, rounds)
        await interaction.response.send_message(embed=embed)
        msg = await interaction.original_response()
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, interaction.channel_id, game_id, [interaction.user.id])

        # Start the first round
        asyncio.create_task(self._run_round(
            game_id=game_id,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            channel=interaction.channel,
            guild=interaction.guild,
            round_num=1,
            settings=settings,
            msg=msg,
        ))

    # ── Round loop ───────────────────────────────────────────────────

    async def _get_scenario(self, settings: dict, host_id: int, channel, interaction_or_msg) -> str | None:
        """Fetch a scenario based on the source setting."""
        source = settings["source"]

        if source == "host":
            return await self._host_scenario(host_id, channel, interaction_or_msg)

        if source == "players":
            return await self._player_scenario(channel, interaction_or_msg)

        if source == "ai":
            return await self._ai_scenario()

        if source == "bank":
            return await get_price_scenario(self.db)

        if source == "both":
            if random.random() < 0.5:
                result = await get_price_scenario(self.db)
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

    async def _host_scenario(self, host_id: int, channel, msg) -> str | None:
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
        except Exception:
            pass

        if not result:
            log.info("Host scenario timed out, falling back to question bank")
            return await get_price_scenario(self.db)

        return result

    async def _player_scenario(self, channel, msg) -> str | None:
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
        except Exception:
            pass

        if not result:
            log.info("Player scenario timed out, falling back to question bank")
            return await get_price_scenario(self.db)

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
            scenario = await get_price_scenario(self.db)
        if not scenario:
            scenario = await self._ai_scenario()
        if not scenario:
            try:
                await channel.send("❌ Couldn't generate a scenario. Skipping round.")
            except Exception:
                pass
            # Advance to next round or end
            if round_num < total_rounds:
                await asyncio.sleep(2)
                await self._run_round(game_id, host_id, host_name, channel, guild, round_num + 1, settings, msg)
            else:
                await self._show_recap(game_id, host_id, host_name, channel, guild, settings)
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
        except Exception:
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
            except Exception:
                pass
            if round_num < total_rounds:
                await asyncio.sleep(3)
                await self._run_round(game_id, host_id, host_name, channel, guild, round_num + 1, settings, msg)
            else:
                await self._show_recap(game_id, host_id, host_name, channel, guild, settings)
            return

        # ── Reveal phase ──
        ladder = sorted(prices.items(), key=lambda x: x[1])
        named_ladder = [(resolve_name(guild, uid), amt) for uid, amt in ladder]
        reveal_embed = build_reveal_embed(host_name, scenario, round_num, total_rounds, named_ladder, guild)

        try:
            await msg.edit(embed=reveal_embed, view=None)
        except Exception:
            pass

        if len(prices) == 1:
            try:
                await channel.send("Only one price submitted — skipping the vote.")
            except Exception:
                pass
            await asyncio.sleep(3)
            if round_num < total_rounds:
                await self._run_round(game_id, host_id, host_name, channel, guild, round_num + 1, settings, msg)
            else:
                await self._show_recap(game_id, host_id, host_name, channel, guild, settings)
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
            if round_num < total_rounds:
                await self._run_round(game_id, host_id, host_name, channel, guild, round_num + 1, settings, msg)
            else:
                await self._show_recap(game_id, host_id, host_name, channel, guild, settings)
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

        # Disable vote view
        vote_view._closed = True
        for item in vote_view.children:
            item.disabled = True
        try:
            await vote_msg.edit(view=vote_view)
        except Exception:
            pass

        # ── Tally votes ──
        reasonable_tally: dict[int, int] = {}
        for voter, target in vote_view.reasonable_votes.items():
            reasonable_tally[target] = reasonable_tally.get(target, 0) + 1

        unhinged_tally: dict[int, int] = {}
        for voter, target in vote_view.unhinged_votes.items():
            unhinged_tally[target] = unhinged_tally.get(target, 0) + 1

        # Winners (handle ties by picking all with max votes)
        r_winner_uid, r_votes = None, 0
        if reasonable_tally:
            max_r = max(reasonable_tally.values())
            r_winners = [uid for uid, v in reasonable_tally.items() if v == max_r]
            r_winner_uid = r_winners[0]
            r_votes = max_r

        u_winner_uid, u_votes = None, 0
        if unhinged_tally:
            max_u = max(unhinged_tally.values())
            u_winners = [uid for uid, v in unhinged_tally.items() if v == max_u]
            u_winner_uid = u_winners[0]
            u_votes = max_u

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
        if reasonable_tally:
            for uid in r_winners:
                key = str(uid)
                scores["reasonable_wins"][key] = scores["reasonable_wins"].get(key, 0) + 1
        if unhinged_tally:
            for uid in u_winners:
                key = str(uid)
                scores["unhinged_wins"][key] = scores["unhinged_wins"].get(key, 0) + 1

        await update_game_payload(self.db, game_id, payload)

        # ── Show round results ──
        r_name = resolve_name(guild, r_winner_uid) if r_winner_uid else "Nobody"
        u_name = resolve_name(guild, u_winner_uid) if u_winner_uid else "Nobody"
        r_price = prices.get(r_winner_uid, 0) if r_winner_uid else 0
        u_price = prices.get(u_winner_uid, 0) if u_winner_uid else 0

        # If ties, list all winners
        if reasonable_tally and len(r_winners) > 1:
            r_name = " & ".join(resolve_name(guild, uid) for uid in r_winners)
        if unhinged_tally and len(u_winners) > 1:
            u_name = " & ".join(resolve_name(guild, uid) for uid in u_winners)

        results_embed = build_round_results_embed(
            host_name, round_num, total_rounds,
            r_name, r_price, r_votes,
            u_name, u_price, u_votes,
        )
        try:
            await vote_msg.edit(embed=results_embed, view=None)
        except Exception:
            pass

        # ── Next round or recap ──
        await asyncio.sleep(5)

        # Re-read total_rounds in case host added rounds
        payload = await get_game_payload(self.db, game_id)
        total_rounds = payload.get("total_rounds", settings["rounds"])

        if round_num < total_rounds:
            await self._run_round(game_id, host_id, host_name, channel, guild, round_num + 1, settings, msg)
        else:
            await self._show_recap(game_id, host_id, host_name, channel, guild, settings)

    # ── Recap ────────────────────────────────────────────────────────

    async def _show_recap(self, game_id: str, host_id: int, host_name: str, channel, guild, settings: dict):
        payload = await get_game_payload(self.db, game_id)
        rounds_data = payload.get("rounds", {})
        scores = payload.get("scores", {"reasonable_wins": {}, "unhinged_wins": {}})

        # Gather all prices per player across all rounds
        player_prices: dict[int, list[int]] = {}
        for rnd in rounds_data.values():
            for uid_str, amt in rnd.get("prices", {}).items():
                uid = int(uid_str)
                player_prices.setdefault(uid, []).append(amt)

        all_players = set(player_prices.keys())
        rounds_played = len(rounds_data)

        # Build awards
        awards = {}

        # Most Reasonable overall
        rw = scores.get("reasonable_wins", {})
        if rw:
            max_r = max(rw.values())
            winners = [uid for uid, v in rw.items() if v == max_r]
            name = " & ".join(resolve_name(guild, int(uid)) for uid in winners)
            awards["reasonable"] = ("🎯 Most Reasonable (overall):", name, f"won {max_r} round{'s' if max_r != 1 else ''}")

        # Most Unhinged overall
        uw = scores.get("unhinged_wins", {})
        if uw:
            max_u = max(uw.values())
            winners = [uid for uid, v in uw.items() if v == max_u]
            name = " & ".join(resolve_name(guild, int(uid)) for uid in winners)
            awards["unhinged"] = ("🤯 Most Unhinged (overall):", name, f"won {max_u} round{'s' if max_u != 1 else ''}")

        # Biggest Spender (highest average)
        if player_prices:
            avg_prices = {uid: statistics.mean(p) for uid, p in player_prices.items()}
            max_avg_uid = max(avg_prices, key=avg_prices.get)
            awards["spender"] = (
                "💸 Biggest Spender:",
                resolve_name(guild, max_avg_uid),
                f"avg {format_price(int(avg_prices[max_avg_uid]))}",
            )

            # Cheapest Date (lowest average)
            min_avg_uid = min(avg_prices, key=avg_prices.get)
            awards["cheapest"] = (
                "🆓 Cheapest Date:",
                resolve_name(guild, min_avg_uid),
                f"avg {format_price(int(avg_prices[min_avg_uid]))}",
            )

        # Consistency / Wildest Swings (need at least 2 rounds of data)
        multi_round_players = {uid: p for uid, p in player_prices.items() if len(p) >= 2}
        if multi_round_players:
            std_devs = {uid: statistics.stdev(p) for uid, p in multi_round_players.items()}
            most_consistent = min(std_devs, key=std_devs.get)
            awards["consistent"] = (
                "📏 Most Consistent:",
                resolve_name(guild, most_consistent),
                f"std dev {format_price(int(std_devs[most_consistent]))}",
            )
            wildest = max(std_devs, key=std_devs.get)
            awards["wildest"] = (
                "🎢 Wildest Swings:",
                resolve_name(guild, wildest),
                f"std dev {format_price(int(std_devs[wildest]))}",
            )

        # Highlight — widest spread round
        highlight = None
        if rounds_data:
            widest_round = None
            widest_spread = -1
            for rnum, rnd in rounds_data.items():
                p = rnd.get("prices", {})
                if len(p) >= 2:
                    amounts = list(p.values())
                    spread = max(amounts) - min(amounts)
                    if spread > widest_spread:
                        widest_spread = spread
                        widest_round = rnum
            if widest_round is not None:
                rnd_prices = list(rounds_data[widest_round]["prices"].values())
                highlight = (
                    f"Round {widest_round} had the widest spread — "
                    f"{format_price(min(rnd_prices))} to {format_price(max(rnd_prices))}"
                )

        recap_embed = build_recap_embed(host_name, rounds_played, len(all_players), awards, highlight)
        recap_view = PriceRecapView(game_id, host_id, self, settings)

        try:
            await channel.send(embed=recap_embed, view=recap_view)
        except Exception:
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
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(PriceCog(bot))
