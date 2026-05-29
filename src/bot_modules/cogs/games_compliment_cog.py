import asyncio
import logging

import discord
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import GOLDEN_MEADOW_COLOR, GAME_ICONS, HOW_TO_PLAY
from bot_modules.games.utils.game_manager import (
    check_allowed_channel,
    create_game,
    update_game_message,
    get_game_payload,
    modify_payload,
    end_game,
    update_session,
    ConfirmCloseView,
    resolve_names,
)
from bot_modules.games.utils.derangement import random_derangement

log = logging.getLogger(__name__)


def build_compliment_embed(host_name: str, participants: list[str]) -> discord.Embed:
    embed = discord.Embed(
        title=f"{GAME_ICONS['compliment']} SPIN THE COMPLIMENT",
        color=GOLDEN_MEADOW_COLOR,
    )
    embed.add_field(name="Host", value=host_name, inline=True)
    pool_str = ", ".join(participants) if participants else "—"
    embed.add_field(name=f"Pool ({len(participants)})", value=pool_str, inline=False)
    embed.set_footer(text=f"{GAME_ICONS['compliment']} Spin the Compliment")
    return embed


class ComplimentView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, db, bot):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot

    def is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild:
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    @discord.ui.button(label="Add Me!", style=discord.ButtonStyle.success, custom_id="comp_addme")
    async def add_me(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        user_id = interaction.user.id
        action_holder = {}

        def _toggle(payload):
            participants = payload.setdefault("participants", [])
            if user_id in participants:
                participants.remove(user_id)
                action_holder["action"] = "removed from"
            else:
                participants.append(user_id)
                action_holder["action"] = "added to"

        payload = await modify_payload(self.db, self.game_id, _toggle)
        action = action_holder["action"]
        log.info("%s %s game %s in #%s", interaction.user.display_name, action.split()[0], self.game_id, interaction.channel.name if interaction.channel else "unknown")

        names = await self._resolve_names(interaction.guild, payload.get("participants", []))
        host_member = interaction.guild.get_member(self.host_id) if interaction.guild else None
        embed = build_compliment_embed(
            host_member.display_name if host_member else "Host",
            names,
        )
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(
            f"✅ You've been {action} the pool.", ephemeral=True
        )

    @discord.ui.button(label="Close & Generate", style=discord.ButtonStyle.primary, custom_id="comp_generate")
    async def close_generate(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can generate pairings.", ephemeral=True)
            return

        payload = await get_game_payload(self.db, self.game_id)
        participants = payload.get("participants", [])
        if len(participants) < 2:
            await interaction.response.send_message("Need at least 2 players in the pool!", ephemeral=True)
            return

        await interaction.response.defer()

        # Generate pairings
        pairings = random_derangement(participants)

        # Build pairings embed
        embed = discord.Embed(
            title=f"{GAME_ICONS['compliment']} COMPLIMENT PAIRINGS",
            color=GOLDEN_MEADOW_COLOR,
        )
        lines = []
        mentions = []
        for giver_id, receiver_id in pairings.items():
            giver = interaction.guild.get_member(giver_id) if interaction.guild else None
            receiver = interaction.guild.get_member(receiver_id) if interaction.guild else None
            giver_str = giver.mention if giver else str(giver_id)
            receiver_str = receiver.mention if receiver else str(receiver_id)
            lines.append(f"{giver_str} → {receiver_str}")
            if giver:
                mentions.append(giver.mention)
            if receiver:
                mentions.append(receiver.mention)
        embed.description = "\n".join(lines) + "\n\n💛 Reply to deliver your compliment!"
        embed.set_footer(text=f"{GAME_ICONS['compliment']} Spin the Compliment")
        # Ping all participants
        unique_mentions = list(dict.fromkeys(mentions))  # dedupe, preserve order

        self.stop()
        for item in self.children:
            item.disabled = True

        await interaction.edit_original_response(view=self)
        if unique_mentions:
            ping_msg = await interaction.followup.send(content=" ".join(unique_mentions), wait=True)
            async def _delete_ping():
                await asyncio.sleep(15)
                try:
                    await ping_msg.delete()
                except discord.HTTPException:
                    pass
            asyncio.create_task(_delete_ping())
        await interaction.followup.send(embed=embed)

        log.info("Game %s ended — %d players", self.game_id, len(participants))
        await end_game(
            self.db,
            self.game_id,
            player_count=len(participants),
            payload={"pairings": {str(k): v for k, v in pairings.items()}},
        )
        if self.game_id in self.bot.active_views:
            del self.bot.active_views[self.game_id]

    @discord.ui.button(label="🛑 Cancel", style=discord.ButtonStyle.danger, custom_id="comp_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can cancel.", ephemeral=True)
            return
        game_msg = interaction.message

        async def _confirmed(confirm_interaction):
            self.stop()
            for item in self.children:
                item.disabled = True
            try:
                await game_msg.edit(content="Game cancelled.", view=self)
            except Exception:
                pass
            await end_game(self.db, self.game_id)
            if self.game_id in self.bot.active_views:
                del self.bot.active_views[self.game_id]

        view = ConfirmCloseView(_confirmed)
        await interaction.response.send_message("⚠️ Are you sure you want to cancel this game?", view=view, ephemeral=True)

    @discord.ui.button(label="❓ How to Play", style=discord.ButtonStyle.secondary, custom_id="comp_htp")
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        await interaction.response.send_message(HOW_TO_PLAY["compliment"], ephemeral=True)

    async def _resolve_names(self, guild, participants: list[int]) -> list[str]:
        return resolve_names(guild, participants)


class ComplimentCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    async def cog_load(self) -> None:
        rows = await self.db.fetchall(
            "SELECT game_id, host_id FROM games_active_games WHERE game_type = 'compliment'"
        )
        for row in rows:
            view = ComplimentView(row["game_id"], row["host_id"], self.db, self.bot)
            self.bot.add_view(view)
            self.bot.active_views[row["game_id"]] = view
        log.info("compliment: re-registered %d active ComplimentView(s)", len(rows))

    @app_commands.command(name="compliment", description="Start Spin the Compliment — random anonymous pairing!")
    async def compliment(self, interaction: discord.Interaction):
        log.info("%s used /compliment in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it with `/config allow-channel`.",
                ephemeral=True,
            )
            return


        game_id = await create_game(
            self.db,
            interaction.channel_id,
            interaction.user.id,
            "compliment",
            state="joining",
        )

        log.info("Game %s (compliment) created by %s in #%s", game_id, interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        embed = build_compliment_embed(interaction.user.display_name, [])
        view = ComplimentView(game_id, interaction.user.id, self.db, self.bot)
        self.bot.active_views[game_id] = view

        await interaction.response.send_message(embed=embed, view=view)
        msg = await interaction.original_response()
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, interaction.channel_id, game_id, [interaction.user.id])


async def setup(bot: commands.Bot):
    await bot.add_cog(ComplimentCog(bot))
