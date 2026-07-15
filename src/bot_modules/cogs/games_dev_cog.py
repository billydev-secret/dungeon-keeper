"""Dev tools for game testing — fill lobbies with fake players, submit fake answers."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot
    from bot_modules.cogs.games_clapback_cog import ClapbackCog

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.games.command_groups import games
from bot_modules.games.utils.game_manager import (
    _FAKE_BASE,
    _FAKE_NAMES,
    get_active_game,
    get_game_payload,
    modify_payload,
    resolve_name,
)

log = logging.getLogger(__name__)

# NOTE: no `parent=games` here — that auto-registers into games._children at
# import time, which collides (CommandAlreadyRegistered) on hot-reload since the
# old `dev` still lives in the restored `games` group. Attach it in setup() with
# override=True instead.
dev = app_commands.Group(
    name="dev",
    description="Developer tools for testing games.",
)

# Games whose lobby keeps its joined-player list under "participants"
# instead of the "players" key most game payloads use.
_PARTICIPANTS_KEY_GAMES = {"compliment", "mfk", "traditional"}

# Games that also need a "prefs" entry per player or a fake join looks
# like a no-op to the game's own logic (toggle_pref populates both).
_PREFS_GAMES = {"traditional"}
_DEFAULT_PREF_CATEGORY = "sfw_truth"

# Games with a "joining" lobby state but no simple joined-player list at
# all — they're submission-based (a modal writes straight into a
# statements/takes structure keyed by user id), so "add N fake players"
# doesn't map onto their payload shape.
_NO_PLAYER_LIST_GAMES = {"ttl", "hottakes"}

_FAKE_ANSWERS = [
    "My entire personality",
    "The audacity of this question",
    "A questionable life choice",
    "Whatever gets the most votes",
    "I have no idea but I'm confident",
    "The thing we don't talk about",
    "Unfiltered chaos",
    "Someone else's problem",
    "A deeply concerning amount",
    "The vibe, honestly",
    "Absolute nonsense",
    "Peak fiction",
]


class GamesDevCog(commands.Cog):
    def __init__(self, bot: Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(name="fill", description="Add fake players to the active game lobby.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(count="Number of fake players to add (1–12, default 5)")
    async def dev_fill(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, 1, 12] = 5,
    ):
        row = await get_active_game(self.db, interaction.channel_id)
        if not row:
            await interaction.response.send_message(
                "No active game in this channel.", ephemeral=True
            )
            return

        game_id = row["game_id"]
        game_type = row["game_type"]

        if row["state"] != "joining":
            await interaction.response.send_message(
                f"Game is in `{row['state']}` state — `/games dev fill` only works during the lobby.",
                ephemeral=True,
            )
            return

        if game_type in _NO_PLAYER_LIST_GAMES:
            await interaction.response.send_message(
                f"`/games dev fill` doesn't support `{game_type}` — it has no "
                "joined-player list (submissions are written directly by a "
                "modal, one per player).",
                ephemeral=True,
            )
            return

        players_key = "participants" if game_type in _PARTICIPANTS_KEY_GAMES else "players"
        added = 0

        def _add_fakes(payload):
            nonlocal added
            players = payload.setdefault(players_key, [])
            prefs = payload.setdefault("prefs", {}) if game_type in _PREFS_GAMES else None
            for i in range(len(_FAKE_NAMES)):
                if added >= count:
                    break
                uid = _FAKE_BASE + i
                if uid not in players:
                    players.append(uid)
                    if prefs is not None:
                        prefs.setdefault(str(uid), []).append(_DEFAULT_PREF_CATEGORY)
                    added += 1

        payload = await modify_payload(self.db, game_id, _add_fakes)
        players = payload.get(players_key, [])

        embed_note = ""
        if game_type == "clapback":
            if not row["message_id"]:
                embed_note = (
                    "\n⚠️ Lobby message isn't posted yet — the on-screen list "
                    "will look stale until you retry in a moment."
                )
            else:
                try:
                    from bot_modules.games_clapback.embeds import build_lobby_embed
                    from bot_modules.core.branding import resolve_accent_color
                    config = payload.get("config", {})
                    guild = interaction.guild
                    host_member = guild.get_member(row["host_id"]) if guild else None
                    colour = (
                        await resolve_accent_color(self.bot.ctx.db_path, guild)
                        if guild
                        else None
                    )
                    embed = build_lobby_embed(
                        host_name=host_member.display_name if host_member else "Host",
                        config=config,
                        players=players,
                        name_resolver=lambda uid: resolve_name(guild, uid),
                        start_at=config.get("start_epoch"),
                        colour=colour,
                    )
                    channel = interaction.channel
                    # A channel hosting an active game is always sendable.
                    assert isinstance(channel, discord.abc.Messageable)
                    msg = await channel.fetch_message(row["message_id"])
                    await msg.edit(embed=embed)
                except Exception as e:
                    log.warning("dev fill: embed update failed: %s", e)
                    embed_note = (
                        f"\n⚠️ Lobby embed did **not** update ({e}). The fake "
                        "players were added to game state, but the on-screen "
                        "list is stale until something else refreshes it."
                    )

        await interaction.response.send_message(
            f"Added {added} fake player(s). Lobby: **{len(players)}** player(s)."
            f"{embed_note}",
            ephemeral=True,
        )

    @app_commands.command(name="answer", description="Submit fake answers for all fake players in a Clapback round.")
    @app_commands.default_permissions(manage_guild=True)
    async def dev_answer(self, interaction: discord.Interaction):
        row = await get_active_game(self.db, interaction.channel_id)
        if not row:
            await interaction.response.send_message(
                "No active game in this channel.", ephemeral=True
            )
            return

        game_id = row["game_id"]

        if row["game_type"] != "clapback":
            await interaction.response.send_message(
                f"`/games dev answer` only supports clapback (game is `{row['game_type']}`).",
                ephemeral=True,
            )
            return

        payload = await get_game_payload(self.db, game_id)

        if payload.get("phase") != "submitting":
            await interaction.response.send_message(
                f"Game is in `{payload.get('phase')}` phase, not `submitting`.",
                ephemeral=True,
            )
            return

        players = payload.get("players", [])
        fake_players = [uid for uid in players if _FAKE_BASE <= uid < _FAKE_BASE + len(_FAKE_NAMES)]

        if not fake_players:
            await interaction.response.send_message(
                "No fake players in this game. Run `/games dev fill` first.",
                ephemeral=True,
            )
            return

        filled = 0

        def _fill_answers(p):
            nonlocal filled
            answers = p.setdefault("answers", {})
            for i, uid in enumerate(fake_players):
                if str(uid) not in answers:
                    answers[str(uid)] = _FAKE_ANSWERS[i % len(_FAKE_ANSWERS)]
                    filled += 1

        await modify_payload(self.db, game_id, _fill_answers)

        # Wake up the submit phase loop so it sees all answers
        for cog in self.bot.cogs.values():
            if hasattr(cog, "_submit_events"):
                events = cast("ClapbackCog", cog)._submit_events
                if game_id in events:
                    events[game_id].set()
                    break

        await interaction.response.send_message(
            f"Submitted fake answers for {filled} fake player(s).",
            ephemeral=True,
        )


async def setup(bot: Bot):
    cog = GamesDevCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("fill")
    bot.tree.remove_command("answer")
    games.add_command(dev, override=True)
    dev.add_command(cog.dev_fill, override=True)
    dev.add_command(cog.dev_answer, override=True)
