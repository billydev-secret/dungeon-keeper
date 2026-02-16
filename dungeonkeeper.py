import os
import datetime
import discord
from discord import app_commands
from dotenv import load_dotenv

# ==============================
# Configuration
# ==============================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
SPOILER_REQUIRED_CHANNELS = os.getenv("SPOILER_REQUIRED_CHANNELS")

DEBUG = True  # Set False to go global

# Roles that bypass spoiler enforcement
BYPASS_ROLE_IDS = set()

# ==============================
# Intents
# ==============================

intents = discord.Intents.default()
intents.members = True
intents.message_content = True  # Required for attachment enforcement

# ==============================
# Bot Class
# ==============================

class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if DEBUG:
            guild = discord.Object(id=GUILD_ID)
            await self.tree.sync(guild=guild)
            print("Synced commands to development guild.")
        else:
            await self.tree.sync()
            print("Synced commands globally.")

bot = Bot()

# ==============================
# Events
# ==============================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")

@bot.event
async def on_message(message: discord.Message):

    if message.author.bot:
        return

    if message.channel.id not in SPOILER_REQUIRED_CHANNELS:
        return

    if any(role.id in BYPASS_ROLE_IDS for role in message.author.roles):
        return

    if not message.attachments:
        return

    for attachment in message.attachments:
        filename = attachment.filename.lower()

        if filename.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            if not attachment.is_spoiler():
                try:
                    await message.delete()
                    await message.channel.send(
                        f"{message.author.mention} — 🚨 Images in this channel must be marked as spoiler.",
                        delete_after=15
                    )
                except discord.Forbidden:
                    pass
                return

# ==============================
# Slash Commands
# ==============================

@bot.tree.command(
    name="listrole",
    description="List all members in a role",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
@app_commands.describe(role="The role to inspect")
async def listrole(interaction: discord.Interaction, role: discord.Role):

    members = role.members

    if not members:
        await interaction.response.send_message(
            f"No members found in **{role.name}**.",
            ephemeral=True
        )
        return

    output = "\n".join(member.display_name for member in members)

    if len(output) > 1900:
        output = output[:1900] + "\n... (truncated)"

    await interaction.response.send_message(
        f"**Members in {role.name}:**\n{output}"
    )

@bot.tree.command(
    name="inactive_role",
    description="Report inactivity for a role",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
@app_commands.describe(
    role="Role to analyze",
    days="Number of days to check (default 7)"
)
async def inactive_role(
    interaction: discord.Interaction,
    role: discord.Role,
    days: app_commands.Range[int, 1, 60] = 7
):

    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message(
            "You do not have permission to use this command.",
            ephemeral=True
        )
        return

    await interaction.response.defer()

    guild = interaction.guild
    cutoff = discord.utils.utcnow() - datetime.timedelta(days=days)

    role_members = set(role.members)
    active_members = set()

    for channel in guild.text_channels:
        if not channel.permissions_for(guild.me).read_message_history:
            continue

        try:
            async for message in channel.history(after=cutoff, limit=None):
                if message.author in role_members:
                    active_members.add(message.author)

                if active_members == role_members:
                    break
        except discord.Forbidden:
            continue

    inactive_members = role_members - active_members

    total = len(role_members)
    inactive_count = len(inactive_members)
    percent = (inactive_count / total * 100) if total else 0

    summary = (
        f"**Role Activity Report — {role.name} ({days} days)**\n"
        f"Total Members: {total}\n"
        f"Inactive: {inactive_count} ({percent:.1f}%)\n"
        f"----------------------------------\n"
    )

    if inactive_members:
        names = "\n".join(m.display_name for m in inactive_members)
        if len(names) > 1800:
            names = names[:1800] + "\n... (truncated)"
        summary += "\n**Inactive Members:**\n" + names
    else:
        summary += "\nAll members active in this period."

    await interaction.followup.send(summary)

# ==============================
# Run
# ==============================

bot.run(TOKEN)
