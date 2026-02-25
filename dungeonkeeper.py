import asyncio
import datetime
import discord
import logging
import os
import json

from typing import NamedTuple
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
MONITORED_CHANNEL_IDS: set[int] = set()

MODEL = "gpt-5-nano"
BIGMODEL = "gpt-5.2-2025-12-11"

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
# User Classes
# ==============================
class UserMsg(NamedTuple):
    created_at: datetime
    channel_id: int
    channel_mention: str
    jump_url: str
    content: str
    mentions: list[str]
    reply_to: str | None
    reply_content: str | None


# ==============================
# Events
# ==============================
@bot.event
async def on_ready():
    # load_monitored_channels()
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    log.info(f"In Guild {GUILD_ID} (Guarding: {SPOILER_REQUIRED_CHANNELS})")
    log.info("------")

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
                    log.info(f"Deleting message {message.author}: {message.content}")
                    await message.delete()
                    await message.channel.send(
                        f"Beep Boop - friendly bot helper: Images in this channel must be marked as spoiler.",
                        delete_after=5,
                    )
                except discord.Forbidden:
                    pass
                return

# ==============================
# Logic
# ==============================
def extract_json_object(s: str):
    """
    Best-effort extraction of a JSON object from model output.
    Handles cases where the model wraps JSON in markdown or extra text.
    """
    s = s.strip()

    # Direct parse attempt
    try:
        return json.loads(s)
    except Exception:
        pass

    # Try extracting the first {...} block
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(s[start:end + 1])
        except Exception:
            return None

    return None

def load_monitored_channels() -> None:
    global MONITORED_CHANNEL_IDS
    if MONITOR_FILE.exists():
        data = json.loads(MONITOR_FILE.read_text(encoding="utf-8"))
        MONITORED_CHANNEL_IDS = set(int(x) for x in data.get("channel_ids", []))

def save_monitored_channels() -> None:
    MONITOR_FILE.write_text(
        json.dumps({"channel_ids": sorted(MONITORED_CHANNEL_IDS)}, indent=2),
        encoding="utf-8"
    )

def is_mod(interaction: discord.Interaction) -> bool:
    perms = interaction.user.guild_permissions
    return perms.manage_guild or perms.administrator

async def llm_user_review(member: discord.Member, transcript: str, stats: dict):
    prompt = f"""
        You are helping moderators review a user for promotion.

        User: {member} (id {member.id})
        Window: last {stats['hours']} hours
        Messages included: {stats['found']}
        Channels posted in: {stats['unique_channels_posted']}

        You will receive a transcript where each message line is numbered.

        Your job:
        1. Write a promotion candidate report

        Write a concise MOD-ONLY report in Markdown:
            ## Activity snapshot
            - posting frequency (based on transcript), breadth of channels, consistency

            ## Themes & participation style
            - what they talk about, how they engage (questions, support, jokes, etc.)

            ## Consent / BDSM rules & boundaries (if applicable)
            - flag any patterns that suggest consent issues, coercion, DM pressure, boundary pushing, unsafe framing
            - be careful: consensual flirting and kink discussion is allowed
            - if there’s insufficient evidence, say so

            ## Tone & community fit
            - respectful? supportive? chronic conflict? chronic negativity? (only if supported)

            ## Recommendation
            - “Looks good for promotion” / “Needs mod check-in” / “Insufficient data”
            - 1–2 sentences why

        2. Identify up to:
           - 5 messages that indicate poor conduct around consent and boundary respect. Let's look for negative sentament as well.
           - 5 messages that demonstrate positive conduct

        Return ONLY valid JSON in this format:

        {{
          "summary": "markdown summary",
          "poor_indices": [3, 18],
          "good_indices": [5, 7, 22]
        }}

        Rules:
        - Only select messages that truly stand out.
        - If none exist, return empty arrays.
        - Do not invent anything.
        """

    numbered_lines = []
    for idx, line in enumerate(transcript.splitlines(), start=1):
        numbered_lines.append(f"{idx}. {line}")
    numbered_transcript = "\n".join(numbered_lines)

    resp = await asyncio.to_thread(
        client.chat.completions.create,
        model=BIGMODEL,
        messages=[
            {"role": "system", "content": "You are a careful moderation analyst. Output valid JSON only."},
            {"role": "user", "content": prompt + "\n\nTRANSCRIPT:\n" + numbered_transcript},
        ],
        temperature=0.2,
    )

    raw = resp.choices[0].message.content.strip()
    data = extract_json_object(raw)
    return data

async def llm_summarize(channel_name: str, transcript: str, hours: int) -> str:
    SUMMARY_PROMPT = f"""
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
            {"role": "user", "content": SUMMARY_PROMPT + "\n\nTRANSCRIPT:\n" + transcript},
        ],
        temperature=0.2,
    )
    return resp.choices[0].message.content.strip()

async def collect_user_messages(
    guild: discord.Guild,
    member: discord.Member,
    hours: int = 168,
    max_msgs: int = 200,
    per_channel_limit: int = 300,
    use_monitored_channels: bool = False,
) -> tuple[list[UserMsg], dict]:
    """
    Collect recent messages by `member` across a set of channels.
    Returns (messages, stats). No persistence.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    # Choose channels to scan
    channels: list[discord.TextChannel] = []
    if use_monitored_channels:
        for cid in sorted(MONITORED_CHANNEL_IDS):
            log.info("Scanning:", ch.name)

            ch = guild.get_channel(cid)
            if isinstance(ch, discord.TextChannel):
                channels.append(ch)
    else:
        channels = list(guild.text_channels)

    found: list[UserMsg] = []
    scanned_channels = 0
    skipped_no_access = 0
    scanned_msgs_total = 0
    per_channel_hits = Counter()

    for ch in channels:
        if len(found) >= max_msgs:
            break

        scanned_channels += 1

        # Skip channels the bot can't read history for
        me = guild.me or guild.get_member(guild.client.user.id)  # best-effort
        if me and not ch.permissions_for(me).read_message_history:
            skipped_no_access += 1
            continue

        try:
            async for msg in ch.history(limit=per_channel_limit, after=cutoff, oldest_first=False):
                scanned_msgs_total += 1
                if msg.author.id != member.id:
                    continue
                if not msg.content:
                    continue

                content = msg.content.replace("\n", " ").strip()
                if not content:
                    continue

                jump = f"https://discord.com/channels/{guild.id}/{ch.id}/{msg.id}"
                mentions = [m.display_name for m in msg.mentions]

                reply_to = None
                reply_content = None

                if msg.reference:
                    ref = msg.reference

                    # If cached
                    if isinstance(ref.resolved, discord.Message):
                        reply_to = ref.resolved.author.display_name
                        if ref.resolved.content:
                            reply_content = ref.resolved.content[:120]

                    # If not cached, fetch manually
                    elif ref.message_id:
                        try:
                            ref_channel = guild.get_channel(ref.channel_id)
                            if ref_channel:
                                fetched = await ref_channel.fetch_message(ref.message_id)
                                reply_to = fetched.author.display_name
                                if fetched.content:
                                    reply_content = fetched.content[:120]
                        except (discord.NotFound, discord.Forbidden):
                            pass

                found.append(
                    UserMsg(
                        created_at=msg.created_at,
                        channel_id=ch.id,
                        channel_mention=ch.mention,
                        jump_url=jump,
                        content=content[:MAX_CHARS_PER_MSG],
                        mentions=mentions,
                        reply_to=reply_to,
                        reply_content=reply_content,
                    )
                )
                per_channel_hits[ch.id] += 1

                if len(found) >= max_msgs:
                    break

        except discord.Forbidden:
            skipped_no_access += 1
            continue

    # Sort chronologically for the transcript
    found.sort(key=lambda m: m.created_at)

    stats = {
        "hours": hours,
        "max_msgs": max_msgs,
        "found": len(found),
        "scanned_channels": scanned_channels,
        "skipped_no_access": skipped_no_access,
        "scanned_msgs_total": scanned_msgs_total,
        "unique_channels_posted": len({m.channel_id for m in found}),
        "top_channels": per_channel_hits.most_common(5),
        "cutoff": cutoff,
    }
    return found, stats

def format_user_transcript(items: list[UserMsg]) -> str:
    lines = []

    for m in items:
        ts = m.created_at.strftime("%Y-%m-%d %H:%M")

        meta_parts = []

        if m.reply_to:
            meta_parts.append(f"reply_to={m.reply_to}")

        if m.reply_content:
            meta_parts.append(f"reply_excerpt='{m.reply_content}'")

        if m.mentions:
            meta_parts.append(f"mentions={','.join(m.mentions)}")

        meta = f" ({' | '.join(meta_parts)})" if meta_parts else ""

        lines.append(
            f"[{ts}] {m.channel_mention}{meta}: {m.content}"
        )

    return build_transcript(lines)

async def send_markdown(channel, text):
    MAX = 1800
    chunks = []

    while text:
        chunk = text[:MAX]
        text = text[MAX:]
        chunks.append(chunk)

    for c in chunks:
        await channel.send(f"```markdown\n{c}\n```")

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
    DAILY_DIGEST_PROMPT = f"""
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
        await send_markdown(mod_channel, summary)
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

@bot.tree.command(
    name="user_review",
    description="Review a user's recent message history (for promotions/mod check-ins).",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
@app_commands.describe(
    member="User to review",
    hours="How many hours back (default 168 = 7 days)",
    max_msgs="Max messages to include (default 200)",
    use_monitored="If true, only scan monitored channels (recommended)"
)
async def user_review(
    interaction: discord.Interaction,
    member: discord.Member,
    hours: app_commands.Range[int, 1, 720] = 168,
    max_msgs: app_commands.Range[int, 20, 500] = 200,
    use_monitored: bool = False
):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    if not guild:
        await interaction.followup.send("Guild context missing.", ephemeral=True)
        return

    items, stats = await collect_user_messages(
        guild=guild,
        member=member,
        hours=hours,
        max_msgs=max_msgs,
        per_channel_limit=400,
        use_monitored_channels=use_monitored,
    )

    if not items:
        await interaction.followup.send("No messages found in that window (or I lack access).", ephemeral=True)
        return
    
    transcript = format_user_transcript(items)
    analysis = await llm_user_review(member, transcript, stats)

    if not analysis:
        await interaction.followup.send("LLM analysis failed.", ephemeral=True)
        return

    summary = analysis.get("summary", "No summary provided.")
    poor_indices = analysis.get("poor_indices", [])
    good_indices = analysis.get("good_indices", [])

    mod_channel = guild.get_channel(MOD_CHANNEL_ID)

    await mod_channel.send(
        f"**User Review — {member.mention}**\n"
        f"Window: last {hours}h | Messages: {stats['found']} | Channels: {stats['unique_channels_posted']}\n"
        f"Requested by {interaction.user.mention}"
    )

    await mod_channel.send(f"```markdown\n{summary}\n```")

    # Helper to safely fetch transcript lines
    lines = transcript.splitlines()

    def build_quote_block(index_list, label):
        blocks = []
        blocks.append(f"**{label}") 
        for i in index_list:
            if 1 <= i <= len(items):
                msg = items[i - 1]
                snippet = msg.content if len(msg.content) < 400 else msg.content[:400] + "…"
                blocks.append(
                    f"**{msg.channel_mention} [{msg.created_at.strftime('%Y-%m-%d %H:%M')}]**\n"
                    f"> {snippet}\n"
                )
        return blocks

    poor_blocks = build_quote_block(poor_indices, "⚠️ Needs Review")
    good_blocks = build_quote_block(good_indices, "✅ Positive Conduct")

    if poor_blocks:
        await mod_channel.send("\n\n".join(poor_blocks))

    if good_blocks:
        await mod_channel.send("\n\n".join(good_blocks))

    await interaction.followup.send("Posted user review to the mod channel ✅", ephemeral=True)

# ==============================
# Run
# ==============================

bot.run(TOKEN)
