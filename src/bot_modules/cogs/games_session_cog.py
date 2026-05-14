import json
from datetime import datetime, timedelta
import discord
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import GOLDEN_MEADOW_COLOR, GAME_NAMES, GAME_ICONS


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
        started_at = session_row["started_at"]
        last_game_at = session_row["last_game_at"]

        # Calculate duration
        try:
            start_dt = datetime.fromisoformat(started_at)
            end_dt = datetime.fromisoformat(last_game_at)
            duration = end_dt - start_dt
            hours, remainder = divmod(int(duration.total_seconds()), 3600)
            minutes, _ = divmod(remainder, 60)
            duration_str = f"{hours}h {minutes}m" if hours else f"{minutes}m"
        except Exception:
            duration_str = "unknown"

        # Fetch game history for these game IDs
        game_histories = []
        for gid in game_ids:
            row = await self.db.fetchone(
                "SELECT game_type, player_count, round_count, payload FROM games_game_history WHERE game_id = ?",
                (gid,),
            )
            if row:
                game_histories.append(row)

        embed = discord.Embed(
            title="📋 GAME NIGHT SESSION RECAP",
            color=GOLDEN_MEADOW_COLOR,
        )
        embed.add_field(name="🎮 Games Played", value=str(len(game_ids)), inline=True)
        embed.add_field(name="👥 Unique Players", value=str(len(player_ids)), inline=True)
        embed.add_field(name="⏱️ Total Duration", value=duration_str, inline=True)

        # Most active player (tracked in player_ids across all games)
        # (We only have unique player IDs, not per-game participation — best effort)
        if player_ids:
            embed.add_field(
                name="🏆 Players",
                value=", ".join(f"<@{uid}>" for uid in player_ids[:10]),
                inline=False,
            )

        # Game highlights
        highlights = []
        for history in game_histories:
            gt = history["game_type"]
            payload = json.loads(history["payload"])
            icon = GAME_ICONS.get(gt, "")
            name = GAME_NAMES.get(gt, gt)

            highlight = f"**{icon} {name}**"

            if gt == "wyr":
                rounds = payload.get("rounds", {})
                if rounds:
                    most_div = min(
                        rounds.values(),
                        key=lambda r: abs(len(r.get("a", [])) - len(r.get("b", []))),
                    )
                    highlight += f": Most divisive — {most_div.get('q', '')[:50]}"

            elif gt == "nhie":
                guilt_scores = payload.get("guilt_scores", {})
                if guilt_scores:
                    guiltiest_id = max(guilt_scores, key=guilt_scores.get)
                    member = interaction.guild.get_member(int(guiltiest_id)) if interaction.guild else None
                    name_g = member.display_name if member else guiltiest_id
                    highlight += f": Guiltiest — {name_g} ({guilt_scores[guiltiest_id]} guilty)"

            elif gt == "ttl":
                scores = payload.get("scores", {})
                if scores:
                    best = max(scores.items(), key=lambda x: x[1].get("fooled", 0))
                    member = interaction.guild.get_member(int(best[0])) if interaction.guild else None
                    name_b = member.display_name if member else best[0]
                    highlight += f": Best Liar — {name_b}"

            elif gt == "hottakes":
                results = payload.get("results", [])
                if results:
                    hottest = max(results, key=lambda x: x.get("avg", 0))
                    highlight += f": Hottest — \"{hottest['text'][:40]}...\" (avg {hottest.get('avg', 0):.1f}/4)"

            highlights.append(highlight)

        if highlights:
            embed.add_field(
                name="Game Highlights",
                value="\n".join(f"• {h}" for h in highlights[:8]),
                inline=False,
            )

        embed.set_footer(text="Golden Meadow Games • Session Recap")
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(SessionCog(bot))
