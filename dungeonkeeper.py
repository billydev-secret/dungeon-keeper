import asyncio
import datetime
import discord
import logging
import os

from collections import Counter
from datetime import datetime, timedelta, timezone
from discord import app_commands
from dotenv import load_dotenv
from openai import OpenAI


# ==============================
# Configuration
# ==============================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MOD_CHANNEL_ID = int(os.environ["MOD_CHANNEL_ID"])

MODEL = "gpt-4o-mini"
MAX_MESSAGES = 400           # hard cap on messages pulled
MAX_CHARS_PER_MSG = 240      # truncate each message
MAX_TOTAL_CHARS = 40_000     # cap payload size to the model

def parse_id_set(value: str | None) -> set[int]:
    if not value:
        return set()
    # supports "1,2,3" (with optional spaces/newlines)
    parts = [p.strip() for p in value.replace("\n", ",").split(",")]
    return {int(p) for p in parts if p}

GUILD_ID = int(os.getenv("GUILD_ID", "0"))
SPOILER_REQUIRED_CHANNELS = parse_id_set(os.getenv("SPOILER_REQUIRED_CHANNELS"))

DEBUG = True  # Set False to go global

# Roles that bypass spoiler enforcement
BYPASS_ROLE_IDS = set()

client = OpenAI(api_key=OPENAI_API_KEY)

logging.basicConfig(
    level=logging.INFO,
)

log = logging.getLogger("accord")  # your bot namespace


# ==============================
# Intents
# ==============================

intents = discord.Intents.default()
intents.members = True
intents.message_content = True  # Required for attachment enforcement
intents = discord.Intents.default()
intents.message_content = True

DAILY_DIGEST_PROMPT = """
You are writing a “Daily Highlights” recap for users to remember the previous day fondly

Channel: #{channel_name}
Window: last {hours} hours
Top posters (approx counts): {author_counts}

Write in Markdown with these sections:

## 🌿 Daily Highlights (5–10 bullets)
- Short, specific, concrete.

## 🧠 What people were into (3–6 bullets)

## 🤝 Shout-outs (3–6 bullets)
- Credit helpful, kind, or funny contributions.

## 📌 Coming up / next steps (0–5 bullets)
- Only include items explicitly mentioned.

## 🎭 Vibe check (1–2 sentences)
- Overall tone. If venting happened, acknowledge gently without spotlighting individuals.

Constraints:
- Do not invent details. If unsure, say “insufficient data.”
- No moderation language, no callouts.
- Avoid quoting >12 words; paraphrase.
"""

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
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    log.info("------")
    log.info(f"In Guild {GUILD_ID} (Guarding: {SPOILER_REQUIRED_CHANNELS})")

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
                        f"Beep Boop - friendly bot helper: Images in this channel must be marked as spoiler.",
                        delete_after=5,
                    )
                except discord.Forbidden:
                    pass
                return

async def llm_summarize(channel_name: str, transcript: str, hours: int) -> str:
    prompt = f"""
You are summarizing a Discord channel for moderators.

Channel: #{channel_name}
Time window: last {hours} hours

Output in Markdown with these sections:

## Themes (3–6 bullets)
## Notable moments (bullets)
## Participation
- Activity level: low/medium/high
- Top participants (approx counts if possible)
- Threading pattern: (few long threads / many short exchanges)

## Tone & climate
- Overall vibe (1–2 sentences)
- Venting present? (yes/no + neutral note)
- If negativity appears: classify as normal venting vs targeted negativity vs repeated downer framing

## Potential friction / discomfort (ranked)
For each item: category tag, confidence 0–1, and suggested soft mod action.
Only include if supported by transcript.

## Action items / follow-ups
Only list explicit decisions or asks. If none, say “None observed.”

Rules:
- Do not moralize; keep neutral.
- Do not invent facts; if unsure, say “insufficient data.”
- No long quotes; paraphrase.
"""
    resp = await asyncio.to_thread(
        client.chat.completions.create,
        model=MODEL,
        messages=[
            {"role": "system", "content": "You are a helpful, careful moderation summarizer."},
            {"role": "user", "content": prompt + "\n\nTRANSCRIPT:\n" + transcript},
        ],
        temperature=0.2,
    )
    return resp.choices[0].message.content.strip()

# ==============================
# Slash Commands
# ==============================
def build_transcript(lines):
    out = []
    total = 0
    for line in lines:
        if total + len(line) > MAX_TOTAL_CHARS:
            break
        out.append(line)
        total += len(line)
    return "\n".join(out)
@bot.tree.command(
    name="daily_digest",
    description="Post a Daily Highlights digest for this channel (manual trigger).",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
@app_commands.describe(hours="How many hours back to summarize (default 24).")
async def daily_digest(interaction: discord.Interaction, hours: int = 24):
    await interaction.response.defer(ephemeral=True)

    channel = interaction.channel
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        await interaction.followup.send("This command only works in text channels/threads.", ephemeral=True)
        return

    after_dt = datetime.now(timezone.utc) - timedelta(hours=hours)

    lines = []
    author_counter = Counter()
    count = 0

    try:
        async for msg in channel.history(limit=None, after=after_dt, oldest_first=True):
            if msg.author.bot or msg.webhook_id is not None:
                continue
            if not msg.content:
                continue

            content = msg.content.replace("\n", " ").strip()
            if not content:
                continue

            author_counter[msg.author.display_name] += 1
            content = content[:MAX_CHARS_PER_MSG]
            lines.append(f"[{msg.created_at.strftime('%Y-%m-%d %H:%M')}] {msg.author.display_name}: {content}")

            count += 1
            if count >= MAX_MESSAGES:
                break

    except discord.Forbidden:
        await interaction.followup.send(
            "I can’t read this channel’s history. Please grant me **View Channel** + **Read Message History** here.",
            ephemeral=True
        )
        return

    if not lines:
        await interaction.followup.send(f"No messages found in the last {hours}h.", ephemeral=True)
        return

    transcript = build_transcript(lines)

    top_authors = author_counter.most_common(8)
    author_counts = ", ".join([f"{name} ({n})" for name, n in top_authors]) or "none"

    prompt = DAILY_DIGEST_PROMPT.format(
        channel_name=getattr(channel, "name", "thread"),
        hours=hours,
        author_counts=author_counts
    )

    resp = await asyncio.to_thread(
        client.chat.completions.create,
        model=MODEL,
        messages=[
            {"role": "system", "content": "You write accurate, warm community digests."},
            {"role": "user", "content": prompt + "\n\nTRANSCRIPT:\n" + transcript},
        ],
        temperature=0.3,
    )
    digest = resp.choices[0].message.content.strip()

    # Post to mod channel
    mod_channel = interaction.guild.get_channel(MOD_CHANNEL_ID) if interaction.guild else None
    if not mod_channel:
        await interaction.followup.send("Mod channel not found (check MOD_CHANNEL_ID).", ephemeral=True)
        return

    await mod_channel.send(f"**Daily Digest — {channel.mention} (last {hours}h)**\n\n{digest}")
    await interaction.followup.send("Posted the daily digest to the mod channel ✅", ephemeral=True)

@bot.tree.command(name="summarize", 
    description="Summarize this channel over a time window.", 
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
@app_commands.describe(hours="How many hours back to summarize (e.g., 24, 72).")
async def summarize(interaction: discord.Interaction, hours: int = 24):
    await interaction.response.defer(ephemeral=True)

    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.followup.send("This command only works in text channels.", ephemeral=True)
        return

    after_dt = datetime.now(timezone.utc) - timedelta(hours=hours)

    lines = []
    count = 0
    async for msg in channel.history(limit=None, after=after_dt, oldest_first=True):
        if msg.author.bot:
            continue
        if not msg.content:
            continue
        content = msg.content.replace("\n", " ").strip()
        if not content:
            continue
        content = content[:MAX_CHARS_PER_MSG]
        lines.append(f"[{msg.created_at.strftime('%Y-%m-%d %H:%M')}] {msg.author.display_name}: {content}")
        count += 1
        if count >= MAX_MESSAGES:
            break

    if not lines:
        await interaction.followup.send(f"No messages found in the last {hours}h.", ephemeral=True)
        return

    transcript = build_transcript(lines)
    summary = await llm_summarize(channel.name, transcript, hours)

    mod_channel = interaction.guild.get_channel(MOD_CHANNEL_ID) if interaction.guild else None
    if mod_channel:
        await mod_channel.send(f"Summary requested by {interaction.user.mention} for {channel.mention}:")
        await mod_channel.send(f"```markdown\n{summary}\n```")
        await interaction.followup.send("Posted summary to the mod channel.", ephemeral=True)
    else:
        await interaction.followup.send(f"```markdown\n{summary}\n```", ephemeral=True)


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
