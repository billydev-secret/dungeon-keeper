import logging

import discord
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import GOLDEN_MEADOW_COLOR, GAME_ICONS, GAME_NAMES

log = logging.getLogger(__name__)

SUPPORT_INVITE_URL = "https://discord.gg/7gfbYYkH"

# Slash command name for each game
GAME_COMMANDS = {
    'ffa': '/ffa',
    'traditional': '/traditional',
    'compliment': '/compliment',
    'mfk': '/mfk',
    'wyr': '/wyr',
    'nhie': '/nhie',
    'mlt': '/mlt',
    'ttl': '/twotruths',
    'hottakes': '/hottakes',
    'story': '/story',

    'ama': '/ama',
    'fantasies': '/fantasies',
    'price': '/price',
    'rushmore': '/rushmore',
    'clapback': '/clapback',
    'legitlibs': '/legitlibs',
}

# Short one-line descriptions for the help list
GAME_DESCRIPTIONS = {
    'ffa': 'Ask the server a question — everyone replies.',
    'traditional': 'Classic truth or dare with SFW/NSFW categories.',
    'compliment': 'Random pairings — give your match a compliment.',
    'mfk': 'Assign three names to each player. You know the rest.',
    'wyr': 'Vote between two options each round.',
    'nhie': 'Guilty or innocent? Find out who has done what.',
    'mlt': 'Vote on who fits each prompt the best.',
    'ttl': 'Submit two truths and one lie. Fool the group.',
    'hottakes': 'Submit anonymous opinions, rate them 🧊 to 🔥.',
    'story': 'Take turns writing one sentence to build a story.',

    'ama': 'Ask the hot seat player anything — anonymously.',
    'fantasies': 'Submit anonymously, then vote Same or Not for me.',
    'price': 'Name your price for absurd scenarios — vote on the most unhinged.',
    'rushmore': 'Snake-draft your top 4 picks — no duplicates allowed.',
    'clapback': 'Write the funniest answer head-to-head, vote for the best.',
    'legitlibs': 'Fill in the blanks to complete a story — everyone gets their own unhinged version.',
}


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="games-help", description="List all game modes and how to use them.")
    async def help_command(self, interaction: discord.Interaction):
        log.info("%s used /games-help in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")

        embed = discord.Embed(
            title="🌸 Golden Meadow Games",
            description="All available game modes. Use the slash command to start a game.",
            color=GOLDEN_MEADOW_COLOR,
        )

        for key in GAME_ICONS:
            icon = GAME_ICONS[key]
            name = GAME_NAMES.get(key, key)
            cmd = GAME_COMMANDS.get(key, f'/{key}')
            desc = GAME_DESCRIPTIONS.get(key, '')
            embed.add_field(
                name=f"{icon} {name}",
                value=f"`{cmd}` — {desc}",
                inline=False,
            )

        embed.add_field(
            name="⚙️ Other Commands",
            value=(
                "`/consent` — Manage your opt-in/opt-out settings\n"
                "`/consent-status` — Check your current consent status\n"
                "`/session-recap` — Recap of the current game night\n"
                "`/games` — Admin channel/game management\n"
                "`/games-support` — Join the support Discord server"
            ),
            inline=False,
        )

        embed.set_footer(text="Golden Meadow Games • /games-help")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="games-support", description="Get a link to the support Discord server.")
    async def support_command(self, interaction: discord.Interaction):
        log.info("%s used /games-support in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")

        embed = discord.Embed(
            title="🛟 Support Server",
            description=f"Need help, want to report a bug, or share feedback?\nJoin us here: {SUPPORT_INVITE_URL}",
            color=GOLDEN_MEADOW_COLOR,
        )
        embed.set_footer(text="Golden Meadow Games • /games-support")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
