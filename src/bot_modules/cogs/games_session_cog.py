import json
from datetime import datetime, timedelta

import discord
from discord.ext import commands
from discord import app_commands

from bot_modules.games_session.embeds import build_session_recap_embed
from bot_modules.games_session.logic import build_highlights, format_duration


class SessionCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(
        name="session-recap",
        description="Show a recap of the current game night session.",
    )
    async def session_recap(self, interaction: discord.Interaction):
        await interaction.response.defer()

        cutoff = (datetime.utcnow() - timedelta(minutes=30)).isoformat()
        session_row = await self.db.fetchone(
            """
            SELECT session_id, started_at, last_game_at, game_ids, player_ids
            FROM games_session_tracker
            WHERE channel_id = ? AND last_game_at >= ?
            ORDER BY last_game_at DESC LIMIT 1
            """,
            (interaction.channel_id, cutoff),
        )

        if not session_row:
            await interaction.followup.send(
                "No active session found in this channel within the last 30 minutes."
            )
            return

        game_ids = json.loads(session_row["game_ids"])
        player_ids = json.loads(session_row["player_ids"])
        duration_str = format_duration(
            session_row["started_at"], session_row["last_game_at"]
        )

        # Fetch game history for these game IDs
        game_histories: list[dict] = []
        for gid in game_ids:
            row = await self.db.fetchone(
                "SELECT game_type, player_count, round_count, payload FROM games_game_history WHERE game_id = ?",
                (gid,),
            )
            if row:
                game_histories.append(
                    {
                        "game_type": row["game_type"],
                        "payload": json.loads(row["payload"]),
                    }
                )

        # Resolve display names against the live guild — fed into logic
        # so the per-game highlight builder stays Discord-free.
        name_lookup: dict[str, str] = {}
        if interaction.guild:
            for history in game_histories:
                payload = history["payload"]
                ids_to_resolve: set[str] = set()
                ids_to_resolve.update(payload.get("guilt_scores", {}).keys())
                ids_to_resolve.update(payload.get("scores", {}).keys())
                for uid_str in ids_to_resolve:
                    try:
                        member = interaction.guild.get_member(int(uid_str))
                    except (TypeError, ValueError):
                        continue
                    if member:
                        name_lookup[uid_str] = member.display_name

        highlights = build_highlights(game_histories, name_lookup)

        embed = build_session_recap_embed(
            game_count=len(game_ids),
            player_ids=player_ids,
            duration_str=duration_str,
            highlights=highlights,
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(SessionCog(bot))
