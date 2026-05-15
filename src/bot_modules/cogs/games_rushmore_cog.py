"""
Mt. Rushmore Draft — game cog.

A topic is chosen and players snake-draft their top 4 picks.  No duplicates
across any player.  After 4 rounds everyone's board is revealed, and the room
votes on the best Mt. Rushmore.
"""

import asyncio
import logging
import random
import time as _time

import discord
from discord.ext import commands
from discord import app_commands

from bot_modules.games.constants import GAME_ICONS, HOW_TO_PLAY, PHASE_JOINING, PHASE_PLAYING, PHASE_RESULTS, PHASE_RECAP
from bot_modules.games.utils.game_manager import (
    check_allowed_channel,
    check_game_enabled,
    create_game,
    modify_payload,
    update_game_message,
    update_game_payload,
    update_game_state,
    get_game_payload,
    end_game,
    update_session,
    ConfirmCloseView,
    resolve_name,
)
from bot_modules.games.utils.question_source import get_rushmore_topic
from bot_modules.games.utils.ai_client import generate_text

log = logging.getLogger(__name__)

DRAFT_ROUNDS = 4

# ── AI prompts ───────────────────────────────────────────────────────────────

RUSHMORE_SYSTEM_PROMPT = (
    "You are generating fun, debatable 'Mt. Rushmore' draft topics for an adult "
    "party game in the Golden Meadow Discord community. A Mt. Rushmore topic is "
    "a category where players will draft their top 4 picks. The best topics are "
    "ones where there are many valid options and people will disagree on the best 4."
)

RUSHMORE_USER_PROMPT = (
    "Generate a single Mt. Rushmore topic. "
    "Examples: 'Snacks', 'Movie Villains', 'Excuses to Leave a Party', "
    "'Songs to Play at the End of the World', 'Fast Food Menu Items', "
    "'Things You'd Save in a House Fire', 'Guilty Pleasure TV Shows', "
    "'Underrated Superpowers', 'Worst First Date Ideas'. "
    "Return only the topic — a short noun phrase, no preamble, no quotes."
)


# ── Snake draft helpers ──────────────────────────────────────────────────────

def generate_snake_order(players: list[int], rounds: int = DRAFT_ROUNDS) -> list[list[int]]:
    """Return list of [round_number, player_id] pairs in snake draft order."""
    order = []
    for r in range(rounds):
        if r % 2 == 0:
            order.extend([[r + 1, pid] for pid in players])
        else:
            order.extend([[r + 1, pid] for pid in reversed(players)])
    return order


def is_duplicate(pick: str, all_picks: list[str]) -> bool:
    normalized = pick.strip().lower()
    return normalized in [p.strip().lower() for p in all_picks]


def find_who_picked(pick: str, boards: dict[str, list]) -> str | None:
    """Return user_id string of who already picked this (case-insensitive)."""
    norm = pick.strip().lower()
    for uid_str, board in boards.items():
        for p in board:
            if p and p != "⏭️ Skipped" and p.strip().lower() == norm:
                return uid_str
    return None


# ── Draft board rendering ────────────────────────────────────────────────────

def render_draft_board(
    players: list[tuple[int, str]],
    boards: dict[str, list],
    active_player_id: int | None,
) -> str:
    lines = []
    max_name = max((len(n) for _, n in players), default=6)
    max_name = min(max_name, 16)

    for uid, name in players:
        board = boards.get(str(uid), [None] * DRAFT_ROUNDS)
        picks = []
        for i, pick in enumerate(board):
            if pick is None:
                picks.append(f"{i+1}. —")
            elif pick == "⏭️ Skipped":
                picks.append(f"{i+1}. *Skipped*")
            else:
                display = pick if len(pick) <= 18 else pick[:15] + "..."
                picks.append(f"{i+1}. {display}")

        truncated_name = name if len(name) <= max_name else name[:max_name - 1] + "."
        line = f"`{truncated_name:<{max_name}}` {' | '.join(picks)}"
        if uid == active_player_id:
            line += "  ← \U0001f3af"
        lines.append(line)

    return "**Draft Board:**\n" + "\n".join(lines)


# ── Embed builders ───────────────────────────────────────────────────────────

def _footer(host_name: str) -> str:
    return f"{GAME_ICONS['rushmore']} Mt. Rushmore Draft • Hosted by {host_name}"


def build_join_embed(host_name: str, player_names: list[str], topic: str | None = None) -> discord.Embed:
    title = f"{GAME_ICONS['rushmore']} MT. RUSHMORE DRAFT"
    desc = f"Hosted by: **{discord.utils.escape_markdown(host_name)}**"
    if topic:
        desc += f"\n\nBuild your Mt. Rushmore of **{discord.utils.escape_markdown(topic)}**!"
    else:
        desc += "\n\nJoin up — snake draft, 4 rounds, no duplicate picks."
    embed = discord.Embed(title=title, description=desc, color=PHASE_JOINING)
    pool_str = ", ".join(player_names) if player_names else "(nobody yet)"
    embed.add_field(name=f"Players ({len(player_names)})", value=pool_str, inline=False)
    embed.set_footer(text=_footer(host_name))
    return embed


def build_draft_embed(
    host_name: str,
    topic: str,
    players: list[tuple[int, str]],
    boards: dict[str, list],
    active_player_id: int | None,
    active_player_name: str | None,
    round_num: int,
    timer_secs: int,
) -> discord.Embed:
    from bot_modules.games.utils.timer import format_deadline, now_plus
    embed = discord.Embed(
        title=f"{GAME_ICONS['rushmore']} MT. RUSHMORE OF: {discord.utils.escape_markdown(topic)}",
        color=PHASE_PLAYING,
    )
    embed.add_field(
        name="Timer",
        value=f"Round {round_num}/{DRAFT_ROUNDS} | {format_deadline(now_plus(timer_secs))}",
        inline=False,
    )
    if active_player_name:
        embed.add_field(
            name="Now Picking",
            value=f"\U0001f3af **{discord.utils.escape_markdown(active_player_name)}**'s turn!",
            inline=False,
        )
    board_text = render_draft_board(players, boards, active_player_id)
    embed.add_field(name="​", value=board_text, inline=False)
    embed.set_footer(text=_footer(host_name))
    return embed


def build_final_boards_embed(
    host_name: str,
    topic: str,
    players: list[tuple[int, str]],
    boards: dict[str, list],
) -> discord.Embed:
    embed = discord.Embed(
        title=f"{GAME_ICONS['rushmore']} MT. RUSHMORE OF: {discord.utils.escape_markdown(topic)} — FINAL BOARDS",
        color=PHASE_RESULTS,
    )
    for uid, name in players:
        board = boards.get(str(uid), [None] * DRAFT_ROUNDS)
        lines = []
        for i, pick in enumerate(board):
            if pick is None:
                lines.append(f"{i+1}. —")
            elif pick == "⏭️ Skipped":
                lines.append(f"{i+1}. *Skipped*")
            else:
                lines.append(f"{i+1}. {discord.utils.escape_markdown(pick)}")
        esc_name = discord.utils.escape_markdown(name)
        embed.add_field(
            name=f"{GAME_ICONS['rushmore']} {esc_name}'s Mt. Rushmore",
            value="\n".join(lines),
            inline=True,
        )
    embed.set_footer(text=_footer(host_name))
    return embed


def build_vote_embed(host_name: str, topic: str, timer_secs: int) -> discord.Embed:
    from bot_modules.games.utils.timer import format_deadline, now_plus
    embed = discord.Embed(
        title=f"{GAME_ICONS['rushmore']} VOTE — Best Mt. Rushmore of {discord.utils.escape_markdown(topic)}",
        color=PHASE_PLAYING,
    )
    embed.add_field(name="Timer", value=format_deadline(now_plus(timer_secs)), inline=False)
    embed.add_field(name="Vote", value="Who built the best Mt. Rushmore?", inline=False)
    embed.set_footer(text=_footer(host_name))
    return embed


def build_winner_embed(
    host_name: str,
    topic: str,
    winner_names: list[str],
    winner_votes: int,
    winner_boards: list[list],
    all_results: list[tuple[str, int]],
) -> discord.Embed:
    embed = discord.Embed(
        title=f"{GAME_ICONS['rushmore']} WINNER — Mt. Rushmore of {discord.utils.escape_markdown(topic)}",
        color=PHASE_RESULTS,
    )
    winner_label = " & ".join(discord.utils.escape_markdown(n) for n in winner_names)
    board_lines = []
    for board in winner_boards:
        for i, pick in enumerate(board):
            if pick and pick != "⏭️ Skipped":
                board_lines.append(f"{i+1}. {discord.utils.escape_markdown(pick)}")
            else:
                board_lines.append(f"{i+1}. —")
        if len(winner_boards) > 1:
            board_lines.append("")
    embed.add_field(
        name=f"\U0001f3c6 {winner_label} wins! — {winner_votes} vote{'s' if winner_votes != 1 else ''}",
        value="\n".join(board_lines) or "—",
        inline=False,
    )
    results_lines = []
    for name, votes in all_results:
        results_lines.append(f"**{discord.utils.escape_markdown(name)}** — {votes} vote{'s' if votes != 1 else ''}")
    embed.add_field(name="Full Results", value="\n".join(results_lines) or "—", inline=False)
    embed.set_footer(text=_footer(host_name))
    return embed


def build_recap_embed(
    host_name: str,
    topic: str,
    player_count: int,
    duration_secs: float,
    winner_names: list[str],
    winner_votes: int,
    winner_boards: list[list],
    stats: dict,
) -> discord.Embed:
    mins = int(duration_secs // 60)
    secs = int(duration_secs % 60)
    embed = discord.Embed(
        title=f"{GAME_ICONS['rushmore']} MT. RUSHMORE DRAFT — GAME OVER",
        color=PHASE_RECAP,
    )
    winner_label = " & ".join(discord.utils.escape_markdown(n) for n in winner_names)
    board_lines = []
    for board in winner_boards:
        for i, pick in enumerate(board):
            if pick and pick != "⏭️ Skipped":
                board_lines.append(f"  {i+1}. {discord.utils.escape_markdown(pick)}")
            else:
                board_lines.append(f"  {i+1}. —")

    summary = (
        f"\U0001f4cb Topic: **{discord.utils.escape_markdown(topic)}**\n"
        f"\U0001f465 Players: **{player_count}**\n"
        f"⏱️ Draft duration: **{mins}m {secs}s**\n\n"
        f"\U0001f3c6 Winner: **{winner_label}** — {winner_votes} vote{'s' if winner_votes != 1 else ''}\n"
    )
    summary += "\n".join(board_lines)
    embed.add_field(name="Summary", value=summary, inline=False)

    stat_lines = []
    if stats.get("first_pick"):
        fp = stats["first_pick"]
        stat_lines.append(f"\U0001f947 First Overall Pick: **{discord.utils.escape_markdown(fp['pick'])}** ({discord.utils.escape_markdown(fp['player'])}, Round 1)")
    if stats.get("skipped_count") is not None:
        sc = stats["skipped_count"]
        extra = f" ({', '.join(discord.utils.escape_markdown(n) for n in stats.get('skipped_names', []))})" if stats.get("skipped_names") else ""
        stat_lines.append(f"⏭️ Skipped Picks: **{sc}**{extra}")
    if stats.get("fastest"):
        f = stats["fastest"]
        stat_lines.append(f"⚡ Fastest Pick: **{discord.utils.escape_markdown(f['pick'])}** by {discord.utils.escape_markdown(f['player'])} ({f['time']:.1f}s)")
    if stats.get("slowest"):
        s = stats["slowest"]
        stat_lines.append(f"\U0001f422 Slowest Pick: **{discord.utils.escape_markdown(s['pick'])}** by {discord.utils.escape_markdown(s['player'])} ({s['time']:.1f}s)")
    if stats.get("unanimous"):
        stat_lines.append(f"\U0001f3af Unanimous Vote: Yes — everyone voted for **{discord.utils.escape_markdown(winner_label)}**")
    elif stats.get("vote_split"):
        stat_lines.append(f"\U0001f3af Vote: {stats['vote_split']}-way split")

    if stat_lines:
        embed.add_field(name="\U0001f4ca Draft Stats", value="\n".join(stat_lines), inline=False)

    embed.set_footer(text=_footer(host_name))
    return embed


# ── Modals ───────────────────────────────────────────────────────────────────

class HostTopicModal(discord.ui.Modal, title="Choose a Topic"):
    topic_input = discord.ui.TextInput(
        label="Topic",
        placeholder="e.g. Snacks, Movie Villains, Excuses to Leave a Party",
        required=True,
        max_length=100,
    )

    def __init__(self):
        super().__init__()
        self._result: str | None = None
        self._event = asyncio.Event()

    async def on_submit(self, interaction: discord.Interaction):
        self._result = self.topic_input.value.strip()
        await interaction.response.send_message(
            f"Topic set: **{discord.utils.escape_markdown(self._result)}**", ephemeral=True, delete_after=5
        )
        self._event.set()

    async def wait_for_result(self, timeout: float = 60.0) -> str | None:
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return self._result


class PickModal(discord.ui.Modal):
    pick = discord.ui.TextInput(
        label="Pick",
        required=True,
        max_length=100,
    )

    def __init__(self, round_num: int, topic: str, draft_view: "RushmoreDraftView"):
        title = f"Your Pick — Round {round_num}"
        if len(title) > 45:
            title = f"Round {round_num} Pick"
        super().__init__(title=title)
        self.pick.placeholder = f"Enter your pick for Mt. Rushmore of {topic}"[:100]
        self._draft_view = draft_view

    async def on_submit(self, interaction: discord.Interaction):
        log.info(
            "%s submitted pick modal in #%s",
            interaction.user.display_name,
            interaction.channel.name if interaction.channel else "unknown",
        )
        view = self._draft_view
        if view._closed:
            await interaction.response.send_message("This draft has ended.", ephemeral=True)
            return
        pick_text = self.pick.value.strip()
        if not pick_text:
            await interaction.response.send_message("Pick can't be empty.", ephemeral=True)
            return

        # Duplicate check
        who = find_who_picked(pick_text, view.boards)
        if who:
            who_name = resolve_name(interaction.guild, int(who))
            await interaction.response.send_message(
                f"❌ **{discord.utils.escape_markdown(pick_text)}** was already taken by **{discord.utils.escape_markdown(who_name)}**! Try again.",
                ephemeral=True,
            )
            return

        # Valid pick — store it
        view.accept_pick(pick_text, interaction.user.id)
        await interaction.response.send_message(
            f"✅ Picked **{discord.utils.escape_markdown(pick_text)}**!", ephemeral=True, delete_after=5,
        )


# ── Vote select menu ────────────────────────────────────────────────────────

class RushmoreVoteSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(
            placeholder="\U0001f5f3️ Select the winner",
            options=options,
            custom_id="rushmore_vote_select",
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        view: RushmoreVoteView = self.view
        uid = interaction.user.id
        target = int(self.values[0])
        if target == uid:
            await interaction.response.send_message("You can't vote for yourself!", ephemeral=True)
            return
        changed = uid in view.votes
        view.votes[uid] = target
        target_name = resolve_name(interaction.guild, target)
        msg = f"✅ Voted for **{discord.utils.escape_markdown(target_name)}**"
        if changed:
            msg += " (changed)"
        await interaction.response.send_message(msg, ephemeral=True, delete_after=5)
        if view.all_voted():
            view.skip_timer()


# ── Views ────────────────────────────────────────────────────────────────────

class RushmoreJoinView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, host_name: str, topic: str | None, source: str, db, bot, cog):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.host_name = host_name
        self.topic = topic
        self.source = source
        self.db = db
        self.bot = bot
        self.cog = cog
        self.players: list[int] = []
        self._msg: discord.Message | None = None

    def is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild:
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    def _player_names(self, guild) -> list[str]:
        return [resolve_name(guild, uid) for uid in self.players]

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="rushmore_join", row=0)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        uid = interaction.user.id
        def _add(p):
            if uid not in p.get("players", []):
                p.setdefault("players", []).append(uid)
        payload = await modify_payload(self.db, self.game_id, _add)
        self.players = payload.get("players", [])
        names = self._player_names(interaction.guild)
        embed = build_join_embed(self.host_name, names, self.topic)
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send("✅ You've joined!", ephemeral=True)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, custom_id="rushmore_leave", row=0)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        uid = interaction.user.id
        def _remove(p):
            players = p.get("players", [])
            if uid in players:
                players.remove(uid)
        payload = await modify_payload(self.db, self.game_id, _remove)
        self.players = payload.get("players", [])
        names = self._player_names(interaction.guild)
        embed = build_join_embed(self.host_name, names, self.topic)
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send("You've left.", ephemeral=True)

    @discord.ui.button(label="Start Draft", style=discord.ButtonStyle.primary, custom_id="rushmore_start", row=1)
    async def start_draft(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can start.", ephemeral=True)
            return
        if len(self.players) < 3:
            await interaction.response.send_message("Need at least 3 players to start a Mt. Rushmore draft.", ephemeral=True)
            return

        # Resolve topic if needed
        topic = self.topic
        if not topic:
            if self.source == "host":
                modal = HostTopicModal()
                await interaction.response.send_modal(modal)
                topic = await modal.wait_for_result(timeout=60.0)
                if not topic:
                    try:
                        await interaction.followup.send("Topic entry timed out. Try again.", ephemeral=True)
                    except Exception:
                        pass
                    return
            elif self.source == "ai":
                await interaction.response.defer()
                topic = await generate_text(
                    RUSHMORE_SYSTEM_PROMPT, RUSHMORE_USER_PROMPT,
                    model="gpt-4o-mini", max_tokens=50,
                )
                if not topic:
                    topic = await get_rushmore_topic(self.db)
                if not topic:
                    await interaction.followup.send("Couldn't generate a topic. Try setting one manually with `/rushmore topic:...`.", ephemeral=True)
                    return
            elif self.source == "bank":
                await interaction.response.defer()
                topic = await get_rushmore_topic(self.db)
                if not topic:
                    await interaction.followup.send("No topics in the question bank.", ephemeral=True)
                    return
            else:
                await interaction.response.defer()
        else:
            await interaction.response.defer()

        self.topic = topic
        # Disable join view
        for item in self.children:
            item.disabled = True
        try:
            await self._msg.edit(view=self)
        except Exception:
            pass

        # Start the draft
        await self.cog._start_draft(
            game_id=self.game_id,
            host_id=self.host_id,
            host_name=self.host_name,
            topic=topic,
            players=list(self.players),
            channel=interaction.channel,
            guild=interaction.guild,
            msg=self._msg,
            settings=await self.cog._get_settings(self.game_id),
        )

    @discord.ui.button(label="\U0001f6d1 Cancel", style=discord.ButtonStyle.danger, custom_id="rushmore_cancel", row=1)
    async def cancel_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can cancel.", ephemeral=True)
            return
        game_msg = self._msg

        async def _confirmed(confirm_interaction):
            await end_game(self.db, self.game_id)
            if self.game_id in self.bot.active_views:
                del self.bot.active_views[self.game_id]
            self.stop()
            for item in self.children:
                item.disabled = True
            try:
                await game_msg.edit(
                    embed=discord.Embed(
                        title=f"{GAME_ICONS['rushmore']} MT. RUSHMORE DRAFT — CANCELLED",
                        color=PHASE_RECAP,
                    ),
                    view=self,
                )
            except Exception:
                pass

        view = ConfirmCloseView(_confirmed)
        await interaction.response.send_message("⚠️ Are you sure you want to cancel this game?", view=view, ephemeral=True)

    @discord.ui.button(label="❓ How to Play", style=discord.ButtonStyle.secondary, custom_id="rushmore_htp", row=2)
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        await interaction.response.send_message(HOW_TO_PLAY["rushmore"], ephemeral=True)


class RushmoreDraftView(discord.ui.View):
    """Persistent view during the snake draft."""

    def __init__(self, game_id: str, host_id: int, host_name: str, topic: str,
                 players: list[int], timer_secs: int, guild, db, bot, cog):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.host_name = host_name
        self.topic = topic
        self.players = players
        self.timer_secs = timer_secs
        self.guild = guild
        self.db = db
        self.bot = bot
        self.cog = cog

        # Draft state
        random.shuffle(self.players)
        self.draft_order = generate_snake_order(self.players)
        self.current_pick_index = 0
        self.boards: dict[str, list] = {str(uid): [None] * DRAFT_ROUNDS for uid in self.players}
        self.all_picks: list[str] = []
        self.pick_times: dict[str, float | None] = {}
        self.skipped: list[str] = []
        self._msg: discord.Message | None = None
        self._closed = False
        self._pick_event: asyncio.Event | None = None
        self._pick_start: float = 0.0
        self._draft_start: float = _time.time()
        self._active_player_id: int | None = None

    def _player_tuples(self) -> list[tuple[int, str]]:
        return [(uid, resolve_name(self.guild, uid)) for uid in self.players]

    def accept_pick(self, pick_text: str, user_id: int):
        """Called by PickModal when a valid pick is made."""
        if self._active_player_id != user_id:
            return
        rnd, pid = self.draft_order[self.current_pick_index]
        if pid != user_id:
            return

        self.boards[str(user_id)][rnd - 1] = pick_text
        self.all_picks.append(pick_text)
        elapsed = _time.time() - self._pick_start
        key = f"{user_id}_{rnd}"
        self.pick_times[key] = elapsed

        if self._pick_event:
            self._pick_event.set()

    def is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild:
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    def _build_embed(self) -> discord.Embed:
        if self.current_pick_index < len(self.draft_order):
            rnd, pid = self.draft_order[self.current_pick_index]
            name = resolve_name(self.guild, pid)
        else:
            rnd, pid, name = DRAFT_ROUNDS, None, None
        return build_draft_embed(
            self.host_name, self.topic, self._player_tuples(),
            self.boards, pid, name, rnd, self.timer_secs,
        )

    @discord.ui.button(label="\U0001f5ff Make Your Pick", style=discord.ButtonStyle.success, custom_id="rushmore_pick", row=0)
    async def make_pick(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if self._closed:
            await interaction.response.send_message("The draft is over.", ephemeral=True)
            return
        if interaction.user.id != self._active_player_id:
            active_name = resolve_name(self.guild, self._active_player_id) if self._active_player_id else "someone"
            await interaction.response.send_message(
                f"It's not your turn! Waiting on **{discord.utils.escape_markdown(active_name)}**.",
                ephemeral=True,
            )
            return
        rnd = self.draft_order[self.current_pick_index][0]
        await interaction.response.send_modal(PickModal(rnd, self.topic, self))

    @discord.ui.button(label="\U0001f6d1 Close Game", style=discord.ButtonStyle.danger, custom_id="rushmore_close", row=0)
    async def close_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can close.", ephemeral=True)
            return
        game_msg = self._msg

        async def _confirmed(confirm_interaction):
            self._closed = True
            if self._pick_event:
                self._pick_event.set()
            await end_game(self.db, self.game_id)
            if self.game_id in self.bot.active_views:
                del self.bot.active_views[self.game_id]
            self.stop()
            try:
                await game_msg.edit(
                    embed=discord.Embed(
                        title=f"{GAME_ICONS['rushmore']} MT. RUSHMORE DRAFT — CLOSED",
                        description="This game was closed by the host.",
                        color=PHASE_RECAP,
                    ),
                    view=None,
                )
            except Exception:
                pass

        view = ConfirmCloseView(_confirmed)
        await interaction.response.send_message("⚠️ Are you sure you want to end this game?", view=view, ephemeral=True)


class RushmoreVoteView(discord.ui.View):
    def __init__(self, game_id: str, eligible_players: list[int], guild, db, bot):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.eligible_players = eligible_players
        self.guild = guild
        self.db = db
        self.bot = bot
        self.votes: dict[int, int] = {}
        self._msg: discord.Message | None = None
        self._timer_obj = None
        self._done_event: asyncio.Event | None = None

        options = []
        for uid in eligible_players:
            name = resolve_name(guild, uid)
            options.append(discord.SelectOption(label=name, value=str(uid)))
        self.add_item(RushmoreVoteSelect(options))

    def all_voted(self) -> bool:
        for uid in self.eligible_players:
            if uid not in self.votes:
                return False
        return True

    def skip_timer(self):
        if self._done_event:
            self._done_event.set()


class RushmoreRecapView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, cog: "RushmoreCog", settings: dict):
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

    @discord.ui.button(label="\U0001f501 Run Again", style=discord.ButtonStyle.primary, custom_id="rushmore_run_again")
    async def run_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can restart.", ephemeral=True)
            return
        for item in self.children:
            item.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass
        self.stop()
        await self.cog.start_rushmore(
            interaction,
            topic=None,
            timer=self._settings.get("timer", 30),
            source=self._settings.get("source", "host"),
            vote_timer=self._settings.get("vote_timer", 30),
        )

    @discord.ui.button(label="\U0001f504 Hand Off", style=discord.ButtonStyle.secondary, custom_id="rushmore_hand_off")
    async def hand_off(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can hand off.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Type the **/rushmore** command to start a new game as the new host!",
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

class RushmoreCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    async def _get_settings(self, game_id: str) -> dict:
        payload = await get_game_payload(self.db, game_id)
        return payload.get("settings", {})

    # ── Slash command ────────────────────────────────────────────────

    @app_commands.command(name="rushmore", description="Start a Mt. Rushmore Draft!")
    @app_commands.describe(
        topic="The topic (leave blank for AI/bank/manual entry)",
        timer="Seconds per pick (default 30)",
        source="Where topics come from",
        vote_timer="Seconds for the final vote (default 30)",
    )
    @app_commands.choices(
        source=[
            app_commands.Choice(name="Host picks topic", value="host"),
            app_commands.Choice(name="AI generated", value="ai"),
            app_commands.Choice(name="Question bank", value="bank"),
        ],
    )
    async def rushmore_cmd(
        self,
        interaction: discord.Interaction,
        topic: str = "",
        timer: int = 30,
        source: str = "host",
        vote_timer: int = 30,
    ):
        await self.start_rushmore(interaction, topic or None, timer, source, vote_timer)

    async def start_rushmore(
        self,
        interaction: discord.Interaction,
        topic: str | None = None,
        timer: int = 30,
        source: str = "host",
        vote_timer: int = 30,
    ):
        log.info(
            "%s used /rushmore in #%s",
            interaction.user.display_name,
            interaction.channel.name if interaction.channel else "unknown",
        )
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it with `/games allow-channel`.",
                ephemeral=True,
            )
            return
        if not await check_game_enabled(self.db, "rushmore", interaction.guild_id or 0):
            await interaction.response.send_message("Mt. Rushmore Draft is currently disabled on this server.", ephemeral=True)
            return

        timer = max(10, min(timer, 120))
        vote_timer = max(10, min(vote_timer, 60))
        settings = {"timer": timer, "source": source, "vote_timer": vote_timer}

        game_id = await create_game(
            self.db,
            interaction.channel_id,
            interaction.user.id,
            "rushmore",
            state="joining",
            payload={"settings": settings, "players": [], "topic": topic},
        )
        log.info("Game %s (rushmore) created by %s", game_id, interaction.user.display_name)

        join_view = RushmoreJoinView(
            game_id, interaction.user.id, interaction.user.display_name,
            topic, source, self.db, self.bot, self,
        )
        embed = build_join_embed(interaction.user.display_name, [], topic)
        await interaction.response.send_message(embed=embed, view=join_view)
        msg = await interaction.original_response()
        join_view._msg = msg
        self.bot.active_views[game_id] = join_view
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, interaction.channel_id, game_id, [interaction.user.id])

    # ── Draft execution ──────────────────────────────────────────────

    async def _start_draft(
        self,
        game_id: str,
        host_id: int,
        host_name: str,
        topic: str,
        players: list[int],
        channel,
        guild,
        msg: discord.Message,
        settings: dict,
    ):
        await update_game_state(self.db, game_id, "playing")
        payload = await get_game_payload(self.db, game_id)
        payload["topic"] = topic
        await update_game_payload(self.db, game_id, payload)

        draft_view = RushmoreDraftView(
            game_id, host_id, host_name, topic,
            players, settings.get("timer", 30), guild, self.db, self.bot, self,
        )
        self.bot.active_views[game_id] = draft_view

        # Show initial draft board
        embed = draft_view._build_embed()
        try:
            await msg.edit(embed=embed, view=draft_view)
            draft_view._msg = msg
        except Exception:
            new_msg = await channel.send(embed=embed, view=draft_view)
            draft_view._msg = new_msg
            msg = new_msg
            await update_game_message(self.db, game_id, msg.id)

        # Show draft order announcement
        order_names = [resolve_name(guild, uid) for uid in draft_view.players]
        rev_names = list(reversed(order_names))
        await channel.send(
            f"**Draft order:** {' → '.join(order_names)}\n"
            f"(Round 2 reverses: {' → '.join(rev_names)})"
        )

        # Save draft state
        payload = await get_game_payload(self.db, game_id)
        payload["draft_order"] = draft_view.draft_order
        payload["boards"] = draft_view.boards
        payload["all_picks"] = draft_view.all_picks
        await update_game_payload(self.db, game_id, payload)

        # Run each pick
        await self._run_draft_loop(draft_view, channel, guild, settings)

    async def _run_draft_loop(self, draft_view: RushmoreDraftView, channel, guild, settings: dict):
        timer_secs = settings.get("timer", 30)

        while draft_view.current_pick_index < len(draft_view.draft_order):
            if draft_view._closed:
                return

            rnd, pid = draft_view.draft_order[draft_view.current_pick_index]
            draft_view._active_player_id = pid
            player_name = resolve_name(guild, pid)

            # Update draft board embed
            embed = draft_view._build_embed()
            try:
                await draft_view._msg.edit(embed=embed, view=draft_view)
            except Exception:
                pass

            # Ping the player
            member = guild.get_member(pid) if guild else None
            ping_text = (
                f"{member.mention if member else player_name} It's your turn! "
                f"Pick for Round {rnd} of your Mt. Rushmore of **{discord.utils.escape_markdown(draft_view.topic)}**!"
            )
            try:
                await channel.send(ping_text, delete_after=timer_secs)
            except Exception:
                pass

            # Wait for pick or timeout
            draft_view._pick_event = asyncio.Event()
            draft_view._pick_start = _time.time()

            # Schedule nudge
            nudge_task = None
            if timer_secs > 15:
                async def _nudge():
                    await asyncio.sleep(timer_secs - 10)
                    if not draft_view._pick_event.is_set() and not draft_view._closed:
                        try:
                            m = member.mention if member else player_name
                            await channel.send(f"{m} ⏰ 10 seconds left to pick!", delete_after=10)
                        except Exception:
                            pass
                nudge_task = asyncio.create_task(_nudge())

            try:
                await asyncio.wait_for(draft_view._pick_event.wait(), timeout=timer_secs)
            except asyncio.TimeoutError:
                if not draft_view._closed:
                    # Skipped
                    key = f"{pid}_{rnd}"
                    draft_view.boards[str(pid)][rnd - 1] = "⏭️ Skipped"
                    draft_view.skipped.append(key)
                    draft_view.pick_times[key] = None
                    try:
                        m = member.mention if member else player_name
                        await channel.send(f"{m} ⏱️ Time's up! Your pick was skipped.", delete_after=10)
                    except Exception:
                        pass

            if nudge_task and not nudge_task.done():
                nudge_task.cancel()

            if draft_view._closed:
                return

            # Advance to next pick
            draft_view.current_pick_index += 1

            # Save progress to DB
            payload = await get_game_payload(self.db, draft_view.game_id)
            payload["boards"] = draft_view.boards
            payload["all_picks"] = draft_view.all_picks
            payload["current_pick_index"] = draft_view.current_pick_index
            payload["pick_times"] = draft_view.pick_times
            payload["skipped"] = draft_view.skipped
            await update_game_payload(self.db, draft_view.game_id, payload)

            # Update board after pick
            if not draft_view._closed:
                embed = draft_view._build_embed()
                try:
                    await draft_view._msg.edit(embed=embed, view=draft_view)
                except Exception:
                    pass

        # Draft complete — disable draft view
        draft_view._closed = True
        for item in draft_view.children:
            item.disabled = True
        try:
            await draft_view._msg.edit(view=draft_view)
        except Exception:
            pass

        # Show final boards
        await self._show_final_boards(draft_view, channel, guild, settings)

    async def _show_final_boards(self, draft_view: RushmoreDraftView, channel, guild, settings: dict):
        game_id = draft_view.game_id
        player_tuples = draft_view._player_tuples()

        final_embed = build_final_boards_embed(
            draft_view.host_name, draft_view.topic, player_tuples, draft_view.boards,
        )
        try:
            await channel.send(embed=final_embed)
        except Exception:
            pass

        # Determine eligible voters (players with at least 1 real pick)
        eligible = []
        for uid in draft_view.players:
            board = draft_view.boards.get(str(uid), [])
            has_pick = any(p and p != "⏭️ Skipped" for p in board)
            if has_pick:
                eligible.append(uid)

        if len(eligible) <= 1:
            # Auto-win or no valid boards
            if eligible:
                winner_uid = eligible[0]
                winner_name = resolve_name(guild, winner_uid)
                await channel.send(f"\U0001f3c6 **{discord.utils.escape_markdown(winner_name)}** wins by default!")
            else:
                winner_uid = None
                await channel.send("No valid boards — nobody wins!")
            await self._show_recap(
                draft_view, channel, guild, settings,
                votes={}, winner_uids=[winner_uid] if winner_uid else [],
            )
            return

        await asyncio.sleep(5)

        # Voting phase
        vote_view = RushmoreVoteView(game_id, eligible, guild, self.db, self.bot)
        self.bot.active_views[game_id] = vote_view

        vote_timer = settings.get("vote_timer", 30)
        vote_embed = build_vote_embed(draft_view.host_name, draft_view.topic, vote_timer)

        try:
            vote_msg = await channel.send(embed=vote_embed, view=vote_view)
            vote_view._msg = vote_msg
        except Exception:
            await self._show_recap(draft_view, channel, guild, settings, votes={}, winner_uids=[])
            return

        # Timer
        vote_done = asyncio.Event()
        vote_view._done_event = vote_done

        async def _on_vote_timer():
            vote_done.set()

        from bot_modules.games.utils.timer import GameTimer
        timer = GameTimer(duration=vote_timer, message=vote_msg, callback=_on_vote_timer, timer_field_index=0)
        vote_view._timer_obj = timer
        await timer.start()
        await vote_done.wait()

        # Disable vote view
        for item in vote_view.children:
            item.disabled = True
        try:
            await vote_msg.edit(view=vote_view)
        except Exception:
            pass

        # Tally
        tally: dict[int, int] = {}
        for voter, target in vote_view.votes.items():
            tally[target] = tally.get(target, 0) + 1

        if tally:
            max_votes = max(tally.values())
            winner_uids = [uid for uid, v in tally.items() if v == max_votes]
        else:
            winner_uids = []
            max_votes = 0

        winner_names = [resolve_name(guild, uid) for uid in winner_uids]
        winner_boards_list = [draft_view.boards.get(str(uid), []) for uid in winner_uids]

        # All results sorted by votes
        all_results = []
        for uid in eligible:
            v = tally.get(uid, 0)
            all_results.append((resolve_name(guild, uid), v))
        all_results.sort(key=lambda x: x[1], reverse=True)

        winner_embed = build_winner_embed(
            draft_view.host_name, draft_view.topic,
            winner_names, max_votes, winner_boards_list, all_results,
        )
        try:
            await vote_msg.edit(embed=winner_embed, view=None)
        except Exception:
            pass

        await asyncio.sleep(5)
        await self._show_recap(draft_view, channel, guild, settings, vote_view.votes, winner_uids)

    async def _show_recap(
        self,
        draft_view: RushmoreDraftView,
        channel,
        guild,
        settings: dict,
        votes: dict[int, int],
        winner_uids: list[int],
    ):
        game_id = draft_view.game_id
        duration = _time.time() - draft_view._draft_start

        # Tally votes
        tally: dict[int, int] = {}
        for voter, target in votes.items():
            tally[target] = tally.get(target, 0) + 1
        max_votes = max(tally.values()) if tally else 0

        winner_names = [resolve_name(guild, uid) for uid in winner_uids]
        winner_boards_list = [draft_view.boards.get(str(uid), []) for uid in winner_uids]

        # Build stats
        stats: dict = {}

        # First overall pick
        if draft_view.all_picks:
            first_pick_rnd, first_pick_uid = draft_view.draft_order[0]
            first_board = draft_view.boards.get(str(first_pick_uid), [])
            if first_board and first_board[0] and first_board[0] != "⏭️ Skipped":
                stats["first_pick"] = {
                    "pick": first_board[0],
                    "player": resolve_name(guild, first_pick_uid),
                }

        # Skipped count
        stats["skipped_count"] = len(draft_view.skipped)
        if draft_view.skipped:
            skipped_uids = set()
            for key in draft_view.skipped:
                uid_str = key.rsplit("_", 1)[0]
                skipped_uids.add(int(uid_str))
            stats["skipped_names"] = [resolve_name(guild, uid) for uid in skipped_uids]

        # Fastest / slowest pick
        valid_times = {k: v for k, v in draft_view.pick_times.items() if v is not None}
        if valid_times:
            fastest_key = min(valid_times, key=valid_times.get)
            slowest_key = max(valid_times, key=valid_times.get)

            def _pick_info(key):
                uid_str, rnd_str = key.rsplit("_", 1)
                uid = int(uid_str)
                rnd = int(rnd_str)
                board = draft_view.boards.get(uid_str, [])
                pick_text = board[rnd - 1] if rnd - 1 < len(board) else "?"
                return {"pick": pick_text or "?", "player": resolve_name(guild, uid), "time": valid_times[key]}

            stats["fastest"] = _pick_info(fastest_key)
            stats["slowest"] = _pick_info(slowest_key)

        # Unanimous vote?
        unique_targets = set(tally.keys())
        if len(unique_targets) == 1 and tally:
            stats["unanimous"] = True
        elif len(unique_targets) > 1:
            stats["vote_split"] = len(unique_targets)

        recap_embed = build_recap_embed(
            draft_view.host_name, draft_view.topic, len(draft_view.players),
            duration, winner_names, max_votes, winner_boards_list, stats,
        )
        recap_view = RushmoreRecapView(game_id, draft_view.host_id, self, settings)

        try:
            await channel.send(embed=recap_embed, view=recap_view)
        except Exception:
            pass

        # End game
        payload = await get_game_payload(self.db, game_id)
        payload["votes"] = {str(k): str(v) for k, v in votes.items()}
        await end_game(
            self.db, game_id,
            player_count=len(draft_view.players),
            round_count=DRAFT_ROUNDS,
            payload=payload,
        )
        if game_id in self.bot.active_views:
            del self.bot.active_views[game_id]


async def setup(bot: commands.Bot):
    await bot.add_cog(RushmoreCog(bot))
