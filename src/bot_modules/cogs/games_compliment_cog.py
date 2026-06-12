import asyncio
import logging

import discord
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import HOW_TO_PLAY
from bot_modules.games.command_groups import play
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
from bot_modules.games_compliment.embeds import (
    build_lobby_embed,
    build_pairings_embed,
    format_pairing_line,
)
from bot_modules.games_compliment.logic import (
    generate_pairings,
    pairing_ids,
    serialize_pairings,
    toggle_participant,
)

log = logging.getLogger(__name__)


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
        action_holder: dict[str, str] = {}

        def _toggle(payload):
            action_holder["action"] = toggle_participant(payload, user_id)

        payload = await modify_payload(self.db, self.game_id, _toggle)
        action = action_holder["action"]
        log.info("%s %s game %s in #%s", interaction.user.display_name, action.split()[0], self.game_id, interaction.channel.name if interaction.channel else "unknown")

        names = resolve_names(interaction.guild, payload.get("participants", []))
        host_member = interaction.guild.get_member(self.host_id) if interaction.guild else None
        embed = build_lobby_embed(
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
        pairings = generate_pairings(participants)

        # Build pairings embed
        lines: list[str] = []
        mention_lookup: dict[int, str] = {}
        for giver_id, receiver_id in pairings.items():
            giver = interaction.guild.get_member(giver_id) if interaction.guild else None
            receiver = interaction.guild.get_member(receiver_id) if interaction.guild else None
            giver_str = giver.mention if giver else str(giver_id)
            receiver_str = receiver.mention if receiver else str(receiver_id)
            lines.append(format_pairing_line(giver_str, receiver_str))
            mention_lookup[giver_id] = giver_str
            mention_lookup[receiver_id] = receiver_str
        embed = build_pairings_embed(lines)
        # Ping all participants (preserve order from pairings dict)
        unique_mentions = [mention_lookup[uid] for uid in pairing_ids(pairings) if uid in mention_lookup]

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
            payload={"pairings": serialize_pairings(pairings)},
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
        log.info("%s used /games play compliment in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it from the web dashboard.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={},
        )
        if game_id is None:
            try:
                await interaction.followup.send(
                    "I don't have access to send messages in that channel. "
                    "Please grant me **View Channel**, **Send Messages**, and **Embed Links**.",
                    ephemeral=True,
                )
            except Exception:
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
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "compliment",
            state="joining",
        )

        log.info("Game %s (compliment) created by %s in #%s", game_id, host_name, getattr(channel, "name", channel.id))
        embed = build_lobby_embed(host_name, [])
        view = ComplimentView(game_id, host_id, self.db, self.bot)
        self.bot.active_views[game_id] = view

        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("compliment launch lacked send perms in channel %s", channel.id)
            return None
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id


async def setup(bot: commands.Bot):
    cog = ComplimentCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("compliment")
    play.add_command(cog.compliment)
    bot.game_launchers["compliment"] = cog.launch
