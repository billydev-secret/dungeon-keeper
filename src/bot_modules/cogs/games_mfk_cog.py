import logging
import random
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

log = logging.getLogger(__name__)

DEFAULT_LABELS = ["Marry", "Fornicate", "Kiss"]


def build_mfk_embed(host_name: str, participants: list[str], labels: list[str] | None = None) -> discord.Embed:
    labels = labels or DEFAULT_LABELS
    title_str = ", ".join(labels)
    embed = discord.Embed(
        title=f"{GAME_ICONS['mfk']} {title_str.upper()}",
        color=GOLDEN_MEADOW_COLOR,
    )
    embed.add_field(name="Host", value=host_name, inline=True)
    embed.add_field(name="Categories", value=" · ".join(f"**{lbl}**" for lbl in labels), inline=True)
    pool_str = ", ".join(participants) if participants else "—"
    embed.add_field(name=f"Pool ({len(participants)})", value=pool_str, inline=False)
    embed.set_footer(text=f"{GAME_ICONS['mfk']} {title_str}")
    return embed


class MFKView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, db, bot, labels: list[str] | None = None):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self.labels = labels or DEFAULT_LABELS

    def is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild:
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    @discord.ui.button(label="Join the Pool", style=discord.ButtonStyle.success, custom_id="mfk_join")
    async def join_pool(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        user_id = interaction.user.id
        action_holder = {}

        def _toggle(payload):
            participants = payload.setdefault("participants", [])
            if user_id in participants:
                participants.remove(user_id)
                action_holder["action"] = "left"
            else:
                participants.append(user_id)
                action_holder["action"] = "joined"

        payload = await modify_payload(self.db, self.game_id, _toggle)
        action = action_holder["action"]
        log.info("%s %s game %s in #%s", interaction.user.display_name, action, self.game_id, interaction.channel.name if interaction.channel else "unknown")

        host_member = interaction.guild.get_member(self.host_id) if interaction.guild else None
        names = resolve_names(interaction.guild, payload.get("participants", []))
        embed = build_mfk_embed(
            host_member.display_name if host_member else "Host",
            names,
            labels=self.labels,
        )
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(
            f"You've {action} the pool.", ephemeral=True
        )

    @discord.ui.button(label="Close & Assign", style=discord.ButtonStyle.primary, custom_id="mfk_assign")
    async def close_assign(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can assign roles.", ephemeral=True)
            return

        payload = await get_game_payload(self.db, self.game_id)
        participants = payload.get("participants", [])
        if len(participants) < 4:
            await interaction.response.send_message(
                "Need at least 4 players in the pool!", ephemeral=True
            )
            return

        await interaction.response.defer()

        # Each player gets 3 random names from the pool (not themselves)
        assignments = {}
        for player_id in participants:
            others = [p for p in participants if p != player_id]
            assignments[player_id] = random.sample(others, 3)

        title_str = ", ".join(self.labels)
        embed = discord.Embed(
            title=f"{GAME_ICONS['mfk']} {title_str.upper()} — YOUR THREE NAMES",
            description=f"Reply with your {title_str} picks!",
            color=GOLDEN_MEADOW_COLOR,
        )
        mentions = []
        for player_id, trio in assignments.items():
            player = interaction.guild.get_member(player_id) if interaction.guild else None
            player_str = player.mention if player else str(player_id)
            if player:
                mentions.append(player.mention)
            names = []
            for uid in trio:
                m = interaction.guild.get_member(uid) if interaction.guild else None
                names.append(m.display_name if m else str(uid))
            embed.add_field(
                name=player_str,
                value=f"**{names[0]}** · **{names[1]}** · **{names[2]}**",
                inline=False,
            )
        embed.set_footer(text=f"{GAME_ICONS['mfk']} {title_str}")

        self.stop()
        for item in self.children:
            item.disabled = True

        await interaction.edit_original_response(view=self)

        unique_mentions = list(dict.fromkeys(mentions))
        await interaction.followup.send(
            content=" ".join(unique_mentions),
            embed=embed,
        )

        log.info("Game %s ended — %d players", self.game_id, len(participants))
        await end_game(
            self.db,
            self.game_id,
            player_count=len(participants),
            payload={"assignments": {str(k): v for k, v in assignments.items()}},
        )
        if self.game_id in self.bot.active_views:
            del self.bot.active_views[self.game_id]

    @discord.ui.button(label="🛑 Cancel", style=discord.ButtonStyle.danger, custom_id="mfk_cancel")
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

    @discord.ui.button(label="❓ How to Play", style=discord.ButtonStyle.secondary, custom_id="mfk_htp")
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        await interaction.response.send_message(HOW_TO_PLAY["mfk"], ephemeral=True)

    def _resolve_names(self, guild, participants: list[int]) -> list[str]:
        return resolve_names(guild, participants)


class MFKCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(name="mfk", description="Start a Marry, Fornicate, Kiss game!")
    @app_commands.describe(
        options='Custom categories (comma-separated, exactly 3). e.g. "Cruise, Wedding, Vacation"',
    )
    async def mfk(self, interaction: discord.Interaction, options: str | None = None):
        log.info("%s used /mfk in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it with `/config allow-channel`.",
                ephemeral=True,
            )
            return

        # Parse custom labels
        labels = None
        if options:
            parts = [p.strip() for p in options.split(",") if p.strip()]
            if len(parts) != 3:
                await interaction.response.send_message(
                    f"Need exactly 3 comma-separated options (got {len(parts)}). "
                    'Example: `Cruise, Wedding, Vacation`',
                    ephemeral=True,
                )
                return
            labels = parts

        game_id = await create_game(
            self.db,
            interaction.channel_id,
            interaction.user.id,
            "mfk",
            state="joining",
            payload={"labels": labels or DEFAULT_LABELS},
        )

        log.info("Game %s (mfk) created by %s in #%s", game_id, interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        embed = build_mfk_embed(interaction.user.display_name, [], labels=labels)
        view = MFKView(game_id, interaction.user.id, self.db, self.bot, labels=labels)
        self.bot.active_views[game_id] = view

        await interaction.response.send_message(embed=embed, view=view)
        msg = await interaction.original_response()
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, interaction.channel_id, game_id, [interaction.user.id])


async def setup(bot: commands.Bot):
    await bot.add_cog(MFKCog(bot))
