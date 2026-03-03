import asyncio
import datetime
import discord
import logging
import os
import json
import sqlite3
import time

from typing import Literal, NamedTuple
from collections import Counter
from datetime import datetime, timedelta, timezone
from discord import app_commands
from dotenv import load_dotenv
from openai import OpenAI
from pathlib import Path
from xp_system import (
    DEFAULT_XP_SETTINGS,
    MessageXpContext,
    PairState,
    XP_SOURCE_IMAGE_REACT,
    XP_SOURCE_REPLY,
    XP_SOURCE_TEXT,
    XP_SOURCE_VOICE,
    apply_xp_award,
    calculate_message_xp,
    completed_voice_intervals,
    count_xp_events,
    delete_voice_session,
    get_oldest_xp_event_timestamp,
    has_any_member_xp,
    has_any_xp_events,
    get_member_xp_state,
    get_xp_leaderboard,
    get_user_xp_standing,
    get_voice_session,
    init_xp_tables,
    is_channel_xp_eligible,
    is_message_processed,
    list_voice_sessions,
    mark_message_processed,
    normalize_message_content,
    record_xp_event,
    set_voice_session,
    update_pair_state,
)


# ==============================
# Configuration
# ==============================
load_dotenv()

logging.basicConfig(
    level=logging.DEBUG,
)

log = logging.getLogger("Dungeon Keeper")

TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DB_PATH = Path(__file__).with_name("dungeonkeeper.db")

MODEL = "gpt-5-nano"
BIGMODEL = "gpt-5.2-2025-12-11"
XP_SETTINGS = DEFAULT_XP_SETTINGS

MAX_MESSAGES = 400           # hard cap on messages pulled
MAX_CHARS_PER_MSG = 240      # truncate each message
MAX_TOTAL_CHARS = 40_000     # cap payload size to the model

def parse_id_set(value: str | None) -> set[int]:
    if not value:
        return set()
    # supports "1,2,3" (with optional spaces/newlines)
    parts = [p.strip() for p in value.replace("\n", ",").split(",")]
    return {int(p) for p in parts if p}

def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn

def init_config_db() -> None:
    bootstrap: dict[str, str] = {}
    bootstrap_sets: dict[str, set[int]] = {}

    mod_channel_id = os.getenv("MOD_CHANNEL_ID")
    if mod_channel_id is not None:
        bootstrap["mod_channel_id"] = mod_channel_id

    debug_env = os.getenv("DEBUG")
    if debug_env is not None:
        bootstrap["debug"] = "1" if parse_bool(debug_env, default=True) else "0"

    level_5_role_id = os.getenv("XP_LEVEL_5_ROLE_ID")
    if level_5_role_id is not None:
        bootstrap["xp_level_5_role_id"] = level_5_role_id

    spoiler_channels = os.getenv("SPOILER_REQUIRED_CHANNELS")
    if spoiler_channels is not None:
        bootstrap_sets["spoiler_required_channels"] = parse_id_set(spoiler_channels)

    bypass_role_ids = os.getenv("BYPASS_ROLE_IDS")
    if bypass_role_ids is not None:
        bootstrap_sets["bypass_role_ids"] = parse_id_set(bypass_role_ids)

    xp_excluded_channels = os.getenv("XP_EXCLUDED_CHANNEL_IDS")
    if xp_excluded_channels is not None:
        bootstrap_sets["xp_excluded_channel_ids"] = parse_id_set(xp_excluded_channels)

    with open_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS config_ids (
                bucket TEXT NOT NULL,
                value INTEGER NOT NULL,
                PRIMARY KEY (bucket, value)
            )
            """
        )

        for key, value in bootstrap.items():
            existing = conn.execute(
                "SELECT value FROM config WHERE key = ?",
                (key,),
            ).fetchone()
            if existing and existing["value"] != value:
                log.warning(
                    "Config override from env for %s: db=%s env=%s",
                    key,
                    existing["value"],
                    value,
                )
            conn.execute(
                """
                INSERT INTO config (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

        for bucket, values in bootstrap_sets.items():
            existing_rows = conn.execute(
                "SELECT value FROM config_ids WHERE bucket = ? ORDER BY value",
                (bucket,),
            ).fetchall()
            existing_values = {int(row["value"]) for row in existing_rows}
            if existing_values and existing_values != values:
                log.warning(
                    "Config override from env for %s: db=%s env=%s",
                    bucket,
                    sorted(existing_values),
                    sorted(values),
                )
            conn.execute(
                "DELETE FROM config_ids WHERE bucket = ?",
                (bucket,),
            )
            conn.executemany(
                "INSERT INTO config_ids (bucket, value) VALUES (?, ?)",
                [(bucket, value) for value in sorted(values)],
            )

def get_config_value(conn: sqlite3.Connection, key: str, default: str) -> str:
    row = conn.execute(
        "SELECT value FROM config WHERE key = ?",
        (key,),
    ).fetchone()
    return row["value"] if row else default

def get_config_id_set(conn: sqlite3.Connection, bucket: str) -> set[int]:
    rows = conn.execute(
        "SELECT value FROM config_ids WHERE bucket = ? ORDER BY value",
        (bucket,),
    ).fetchall()
    return {int(row["value"]) for row in rows}


def add_config_id_value(bucket: str, value: int) -> set[int]:
    with open_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO config_ids (bucket, value) VALUES (?, ?)",
            (bucket, value),
        )
        return get_config_id_set(conn, bucket)


def remove_config_id_value(bucket: str, value: int) -> set[int]:
    with open_db() as conn:
        conn.execute(
            "DELETE FROM config_ids WHERE bucket = ? AND value = ?",
            (bucket, value),
        )
        return get_config_id_set(conn, bucket)

def load_runtime_config() -> dict:
    with open_db() as conn:
        return {
            "mod_channel_id": int(get_config_value(conn, "mod_channel_id", "0")),
            "debug": parse_bool(get_config_value(conn, "debug", "1"), default=True),
            "xp_level_5_role_id": int(get_config_value(conn, "xp_level_5_role_id", "0")),
            "spoiler_required_channels": get_config_id_set(conn, "spoiler_required_channels"),
            "bypass_role_ids": get_config_id_set(conn, "bypass_role_ids"),
            "xp_excluded_channel_ids": get_config_id_set(conn, "xp_excluded_channel_ids"),
        }

init_config_db()
runtime_config = load_runtime_config()

with open_db() as conn:
    init_xp_tables(conn)

GUILD_ID = int(os.getenv("GUILD_ID", "0"))
MOD_CHANNEL_ID = runtime_config["mod_channel_id"]
SPOILER_REQUIRED_CHANNELS = runtime_config["spoiler_required_channels"]
DEBUG = runtime_config["debug"]
BYPASS_ROLE_IDS = runtime_config["bypass_role_ids"]
XP_EXCLUDED_CHANNEL_IDS = runtime_config["xp_excluded_channel_ids"]
LEVEL_5_ROLE_ID = runtime_config["xp_level_5_role_id"]

client = OpenAI(api_key=OPENAI_API_KEY)


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
        self.xp_pair_states: dict[int, PairState] = {}
        self.voice_xp_task: asyncio.Task | None = None

    async def setup_hook(self):
        if DEBUG:
            guild = discord.Object(id=GUILD_ID)
            await self.tree.sync(guild=guild)
            print("Synced commands to development guild.")
        else:
            await self.tree.sync()
            print("Synced commands globally.")

        if self.voice_xp_task is None:
            self.voice_xp_task = asyncio.create_task(voice_xp_loop())

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
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    log.info(f"In Guild {GUILD_ID} (Guarding: {SPOILER_REQUIRED_CHANNELS})")
    log.debug("XP excluded channels: %s", sorted(XP_EXCLUDED_CHANNEL_IDS))
    if GUILD_ID:
        with open_db() as conn:
            log.debug("XP event rows for guild %s: %s", GUILD_ID, count_xp_events(conn, GUILD_ID))
    log.info("------")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    if await enforce_spoiler_requirement(message):
        return

    await award_message_xp(message)

# ==============================
# Logic
# ==============================
def channel_is_xp_allowed(channel) -> bool:
    channel_id = getattr(channel, "id", None)
    if channel_id is None:
        return False
    parent_id = getattr(channel, "parent_id", None)
    return is_channel_xp_eligible(channel_id, parent_id, XP_EXCLUDED_CHANNEL_IDS)


async def enforce_spoiler_requirement(message: discord.Message) -> bool:
    if message.channel.id not in SPOILER_REQUIRED_CHANNELS:
        return False

    if not isinstance(message.author, discord.Member):
        return False

    if any(role.id in BYPASS_ROLE_IDS for role in message.author.roles):
        return False

    if not message.attachments:
        return False

    for attachment in message.attachments:
        filename = attachment.filename.lower()
        if not filename.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            continue
        if attachment.is_spoiler():
            continue

        try:
            log.info("Deleting spoilerless image from %s: %s", message.author, message.content)
            await message.delete()
            await message.channel.send(
                "Beep Boop - friendly bot helper: Images in this channel must be marked as spoiler.",
                delete_after=5,
            )
        except discord.Forbidden:
            pass
        return True

    return False


async def resolve_reply_target(message: discord.Message) -> discord.Message | None:
    if not message.reference:
        return None

    if isinstance(message.reference.resolved, discord.Message):
        return message.reference.resolved

    if not message.reference.message_id:
        return None

    ref_channel = message.guild.get_channel(message.reference.channel_id) if message.guild else None
    if ref_channel is None and hasattr(message.channel, "fetch_message"):
        ref_channel = message.channel

    if ref_channel is None or not hasattr(ref_channel, "fetch_message"):
        return None

    try:
        return await ref_channel.fetch_message(message.reference.message_id)
    except (discord.NotFound, discord.Forbidden):
        return None


async def maybe_grant_level_role(member: discord.Member, new_level: int) -> None:
    if LEVEL_5_ROLE_ID <= 0 or new_level < XP_SETTINGS.role_grant_level:
        return

    role = member.guild.get_role(LEVEL_5_ROLE_ID)
    if role is None:
        log.warning("Level %s reward role %s was not found.", XP_SETTINGS.role_grant_level, LEVEL_5_ROLE_ID)
        return

    if role in member.roles:
        return

    try:
        await member.add_roles(role, reason=f"Reached level {XP_SETTINGS.role_grant_level}")
    except discord.Forbidden:
        log.warning("Missing permission to grant level reward role %s to %s.", role.id, member)


async def award_message_xp(message: discord.Message) -> None:
    if not message.guild or not isinstance(message.author, discord.Member):
        return

    if not channel_is_xp_allowed(message.channel):
        log.debug("XP skipped for %s in #%s: channel excluded.", message.author.id, getattr(message.channel, "id", "unknown"))
        return

    reply_target = await resolve_reply_target(message)
    is_reply_to_human = bool(
        reply_target
        and not reply_target.author.bot
        and reply_target.author.id != message.author.id
    )

    now_ts = message.created_at.timestamp() if message.created_at else time.time()
    normalized_content = normalize_message_content(message.content)
    pair_state = bot.xp_pair_states.get(message.channel.id)
    next_pair_state, pair_streak = update_pair_state(pair_state, message.author.id)
    bot.xp_pair_states[message.channel.id] = next_pair_state

    with open_db() as conn:
        state = get_member_xp_state(conn, message.guild.id, message.author.id)
        is_duplicate = bool(normalized_content) and normalized_content == state.last_message_norm
        breakdown = calculate_message_xp(
            MessageXpContext(
                content=message.content,
                seconds_since_last_message=None if state.last_message_at is None else now_ts - state.last_message_at,
                is_duplicate=is_duplicate,
                is_reply_to_human=is_reply_to_human,
                pair_streak=pair_streak,
            ),
            XP_SETTINGS,
        )
        award = apply_xp_award(
            conn,
            message.guild.id,
            message.author.id,
            breakdown.awarded_xp,
            message_timestamp=now_ts,
            message_norm=breakdown.normalized_content,
            settings=XP_SETTINGS,
        )
        reply_award = 0.0
        if breakdown.reply_bonus_xp > 0:
            reply_award = round(
                breakdown.reply_bonus_xp
                * breakdown.cooldown_multiplier
                * breakdown.duplicate_multiplier
                * breakdown.pair_multiplier,
                2,
            )
        text_award = round(max(0.0, award.awarded_xp - reply_award), 2)
        record_xp_event(
            conn,
            message.guild.id,
            message.author.id,
            XP_SOURCE_TEXT,
            text_award,
            now_ts,
        )
        record_xp_event(
            conn,
            message.guild.id,
            message.author.id,
            XP_SOURCE_REPLY,
            reply_award,
            now_ts,
        )
        mark_message_processed(
            conn,
            message.guild.id,
            message.id,
            message.channel.id,
            message.author.id,
            now_ts,
        )

    if award.awarded_xp <= 0:
        log.debug(
            "XP skipped for %s in #%s: zero award (words=%s duplicate=%s cooldown=%.2f pair=%.2f reply_bonus=%.2f).",
            message.author.id,
            getattr(message.channel, "id", "unknown"),
            breakdown.qualified_words,
            is_duplicate,
            breakdown.cooldown_multiplier,
            breakdown.pair_multiplier,
            breakdown.reply_bonus_xp,
        )
        return

    log.debug(
        "Awarded %.2f text XP to %s in #%s (words=%s total=%.2f level=%s).",
        award.awarded_xp,
        message.author.id,
        getattr(message.channel, "id", "unknown"),
        breakdown.qualified_words,
        award.total_xp,
        award.new_level,
    )

    if award.new_level >= XP_SETTINGS.role_grant_level:
        await maybe_grant_level_role(message.author, award.new_level)


def is_qualifying_voice_channel(channel: discord.VoiceChannel) -> bool:
    afk_channel = channel.guild.afk_channel
    if afk_channel and channel.id == afk_channel.id:
        return False

    human_count = sum(1 for member in channel.members if not member.bot)
    return human_count >= XP_SETTINGS.voice_min_humans


async def process_voice_xp_tick() -> None:
    role_grant_members: dict[tuple[int, int], discord.Member] = {}
    active_members: set[tuple[int, int]] = set()
    now_ts = time.time()

    with open_db() as conn:
        for guild in bot.guilds:
            for channel in guild.voice_channels:
                human_members = [member for member in channel.members if not member.bot]
                if not human_members:
                    continue

                qualifies = is_qualifying_voice_channel(channel)
                for member in human_members:
                    active_members.add((guild.id, member.id))
                    session = get_voice_session(conn, guild.id, member.id)

                    if session is None or session.channel_id != channel.id:
                        set_voice_session(
                            conn,
                            guild.id,
                            member.id,
                            channel.id,
                            session_started_at=now_ts,
                            qualified_since=now_ts if qualifies else None,
                            awarded_intervals=0,
                        )
                        continue

                    if not qualifies:
                        if session.qualified_since is not None or session.awarded_intervals != 0:
                            set_voice_session(
                                conn,
                                guild.id,
                                member.id,
                                channel.id,
                                session_started_at=now_ts,
                                qualified_since=None,
                                awarded_intervals=0,
                            )
                        continue

                    if session.qualified_since is None:
                        set_voice_session(
                            conn,
                            guild.id,
                            member.id,
                            channel.id,
                            session_started_at=now_ts,
                            qualified_since=now_ts,
                            awarded_intervals=0,
                        )
                        continue

                    intervals_due = completed_voice_intervals(session, now_ts, XP_SETTINGS)
                    if intervals_due <= 0:
                        continue

                    set_voice_session(
                        conn,
                        guild.id,
                        member.id,
                        channel.id,
                        session_started_at=session.session_started_at,
                        qualified_since=session.qualified_since,
                        awarded_intervals=session.awarded_intervals + intervals_due,
                    )
                    award = apply_xp_award(
                        conn,
                        guild.id,
                        member.id,
                        intervals_due * XP_SETTINGS.voice_award_xp,
                        event_source=XP_SOURCE_VOICE,
                        event_timestamp=now_ts,
                        settings=XP_SETTINGS,
                    )
                    if award.awarded_xp > 0:
                        log.debug(
                            "Awarded %.2f voice XP to %s in voice channel %s (total=%.2f level=%s).",
                            award.awarded_xp,
                            member.id,
                            channel.id,
                            award.total_xp,
                            award.new_level,
                        )
                    if award.new_level >= XP_SETTINGS.role_grant_level:
                        role_grant_members[(guild.id, member.id)] = member

        for session in list_voice_sessions(conn):
            if (session.guild_id, session.user_id) not in active_members:
                delete_voice_session(conn, session.guild_id, session.user_id)

    for member in role_grant_members.values():
        await maybe_grant_level_role(member, XP_SETTINGS.role_grant_level)


async def voice_xp_loop() -> None:
    await bot.wait_until_ready()

    while not bot.is_closed():
        try:
            await process_voice_xp_tick()
        except asyncio.CancelledError:
            raise
        except sqlite3.OperationalError:
            log.exception("Voice XP tick hit a SQLite operational error.")
        except Exception:
            log.exception("Voice XP tick failed.")

        await asyncio.sleep(XP_SETTINGS.voice_poll_seconds)


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

def is_mod(interaction: discord.Interaction) -> bool:
    perms = interaction.user.guild_permissions
    return perms.manage_guild or perms.administrator


def get_xp_config_target_channel(interaction: discord.Interaction):
    channel = interaction.channel
    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        return channel
    return None


def iter_backfill_channels(guild: discord.Guild) -> list:
    channels = list(guild.text_channels)
    active_thread_ids = {thread.id for thread in guild.threads}
    for thread in guild.threads:
        if thread.id not in active_thread_ids:
            continue
        channels.append(thread)
    return channels


def resolve_leaderboard_timescale(timescale: str) -> tuple[str, str, discord.Color, float | None]:
    now_ts = time.time()
    mapping = {
        "hour": ("Hourly", "Last 60 minutes", discord.Color.dark_teal(), now_ts - 60 * 60),
        "day": ("Daily", "Last 24 hours", discord.Color.blue(), now_ts - 24 * 60 * 60),
        "week": ("Weekly", "Last 7 days", discord.Color.teal(), now_ts - 7 * 24 * 60 * 60),
        "month": ("Monthly", "Last 30 days", discord.Color.orange(), now_ts - 30 * 24 * 60 * 60),
        "year": ("Yearly", "Last 365 days", discord.Color.brand_green(), now_ts - 365 * 24 * 60 * 60),
        "alltime": ("All-Time", "Since tracking began", discord.Color.gold(), None),
    }
    return mapping[timescale]


def format_xp_leaderboard_lines(
    guild: discord.Guild | None,
    entries,
    empty_text: str,
    user_line: str,
) -> str:
    if not entries:
        return f"{empty_text}\n\n{user_line}"

    rank_icons = ["🥇", "🥈", "🥉", "4.", "5."]
    lines = []
    for idx, entry in enumerate(entries, start=1):
        member = guild.get_member(entry.user_id) if guild else None
        label = member.mention if member else f"<@{entry.user_id}>"
        rank = rank_icons[idx - 1] if idx <= len(rank_icons) else f"{idx}."
        lines.append(f"{rank} {label}\n`{entry.xp:.2f} XP`")

    lines.append("")
    lines.append(user_line)
    return "\n".join(lines)


def build_xp_leaderboard_embed(
    guild: discord.Guild,
    caller: discord.Member,
    window_name: str,
    subtitle: str,
    color: discord.Color,
    cutoff: float | None,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"{window_name} XP Leaders",
        description=subtitle,
        color=color,
    )

    source_specs = [
        ("Text", "💬", XP_SOURCE_TEXT, "No text XP yet."),
        ("Replies", "↩️", XP_SOURCE_REPLY, "No reply XP yet."),
        ("Voice", "🎙️", XP_SOURCE_VOICE, "No voice XP yet."),
        ("Image Reacts", "🖼️", XP_SOURCE_IMAGE_REACT, "No image react XP yet."),
    ]

    with open_db() as conn:
        for field_name, icon, source_key, empty_text in source_specs:
            entries = get_xp_leaderboard(
                conn,
                guild.id,
                source_key,
                since_ts=cutoff,
                limit=5,
            )
            standing = get_user_xp_standing(
                conn,
                guild.id,
                source_key,
                caller.id,
                since_ts=cutoff,
            )
            if standing.rank is None:
                user_line = f"Your standing: {caller.mention} has no tracked XP here."
            else:
                user_line = f"Your standing: #{standing.rank} {caller.mention} with `{standing.xp:.2f} XP`"
            embed.add_field(
                name=f"{icon} {field_name}",
                value=format_xp_leaderboard_lines(guild, entries, empty_text, user_line),
                inline=True,
            )

    embed.set_footer(text="Top 5 by XP source with your standing")
    return embed

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
) -> tuple[list[UserMsg], dict]:
    """
    Collect recent messages by `member` across a set of channels.
    Returns (messages, stats). No persistence.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    # Choose channels to scan
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
    name="xp_backfill_history",
    description="Backfill historical message XP into the guild database.",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
@app_commands.describe(days="How many days back to scan. Use 0 for all available history.")
async def xp_backfill_history(
    interaction: discord.Interaction,
    days: app_commands.Range[int, 0, 3650] = 0,
):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    now_dt = datetime.now(timezone.utc)
    after_dt = None if days == 0 else now_dt - timedelta(days=days)
    granted_members: dict[int, discord.Member] = {}
    backfill_user_state: dict[int, tuple[float, str]] = {}
    pair_states: dict[int, PairState] = {}
    stats = {
        "channels_scanned": 0,
        "messages_seen": 0,
        "messages_processed": 0,
        "messages_skipped_processed": 0,
        "messages_awarded": 0,
        "xp_awarded": 0.0,
    }

    with open_db() as conn:
        cutoff_ts = get_oldest_xp_event_timestamp(conn, guild.id, (XP_SOURCE_TEXT, XP_SOURCE_REPLY))
        before_dt = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc) if cutoff_ts is not None else None

        if after_dt and before_dt and after_dt >= before_dt:
            await interaction.followup.send(
                "Nothing to backfill. The selected window is already covered by tracked text/reply XP events.",
                ephemeral=True,
            )
            return

        me = guild.me or guild.get_member(guild.client.user.id)
        for channel in iter_backfill_channels(guild):
            if not channel_is_xp_allowed(channel):
                continue

            if me and not channel.permissions_for(me).read_message_history:
                continue

            stats["channels_scanned"] += 1
            channel_pair_state = pair_states.get(channel.id)

            try:
                async for message in channel.history(limit=None, after=after_dt, before=before_dt, oldest_first=True):
                    stats["messages_seen"] += 1

                    if not message.guild or message.author.bot:
                        continue

                    if is_message_processed(conn, guild.id, message.id):
                        stats["messages_skipped_processed"] += 1
                        continue

                    reply_target = await resolve_reply_target(message)
                    is_reply_to_human = bool(
                        reply_target
                        and not reply_target.author.bot
                        and reply_target.author.id != message.author.id
                    )

                    now_ts = message.created_at.timestamp() if message.created_at else time.time()
                    normalized_content = normalize_message_content(message.content)
                    channel_pair_state, pair_streak = update_pair_state(channel_pair_state, message.author.id)
                    pair_states[channel.id] = channel_pair_state

                    prior_ts = None
                    prior_norm = None
                    if message.author.id in backfill_user_state:
                        prior_ts, prior_norm = backfill_user_state[message.author.id]

                    breakdown = calculate_message_xp(
                        MessageXpContext(
                            content=message.content,
                            seconds_since_last_message=None if prior_ts is None else now_ts - prior_ts,
                            is_duplicate=bool(normalized_content) and normalized_content == prior_norm,
                            is_reply_to_human=is_reply_to_human,
                            pair_streak=pair_streak,
                        ),
                        XP_SETTINGS,
                    )

                    award = apply_xp_award(
                        conn,
                        guild.id,
                        message.author.id,
                        breakdown.awarded_xp,
                        settings=XP_SETTINGS,
                    )

                    reply_award = 0.0
                    if breakdown.reply_bonus_xp > 0:
                        reply_award = round(
                            breakdown.reply_bonus_xp
                            * breakdown.cooldown_multiplier
                            * breakdown.duplicate_multiplier
                            * breakdown.pair_multiplier,
                            2,
                        )
                    text_award = round(max(0.0, award.awarded_xp - reply_award), 2)
                    record_xp_event(conn, guild.id, message.author.id, XP_SOURCE_TEXT, text_award, now_ts)
                    record_xp_event(conn, guild.id, message.author.id, XP_SOURCE_REPLY, reply_award, now_ts)
                    mark_message_processed(
                        conn,
                        guild.id,
                        message.id,
                        message.channel.id,
                        message.author.id,
                        now_ts,
                    )

                    backfill_user_state[message.author.id] = (now_ts, normalized_content)
                    stats["messages_processed"] += 1
                    if award.awarded_xp > 0:
                        stats["messages_awarded"] += 1
                        stats["xp_awarded"] += award.awarded_xp
                        member = message.author if isinstance(message.author, discord.Member) else guild.get_member(message.author.id)
                        if member and award.new_level >= XP_SETTINGS.role_grant_level:
                            granted_members[member.id] = member
            except discord.Forbidden:
                continue

    for member in granted_members.values():
        await maybe_grant_level_role(member, XP_SETTINGS.role_grant_level)

    window_label = "all available history" if days == 0 else f"last {days} days"
    cutoff_note = ""
    if cutoff_ts is not None:
        cutoff_note = "\nSkipped messages on or after the earliest tracked live text/reply XP to avoid double counting."

    await interaction.followup.send(
        (
            f"Backfill complete for {window_label}.\n"
            f"Channels scanned: {stats['channels_scanned']}\n"
            f"Messages seen: {stats['messages_seen']}\n"
            f"Messages processed: {stats['messages_processed']}\n"
            f"Already processed: {stats['messages_skipped_processed']}\n"
            f"Messages awarding XP: {stats['messages_awarded']}\n"
            f"XP added: {stats['xp_awarded']:.2f}"
            f"{cutoff_note}"
        ),
        ephemeral=True,
    )


@bot.tree.command(
    name="xp_exclude_here",
    description="Disable XP gain in this channel or thread.",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
async def xp_exclude_here(interaction: discord.Interaction):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    channel = get_xp_config_target_channel(interaction)
    if channel is None:
        await interaction.response.send_message("This command only works in text channels or threads.", ephemeral=True)
        return

    global XP_EXCLUDED_CHANNEL_IDS
    XP_EXCLUDED_CHANNEL_IDS = add_config_id_value("xp_excluded_channel_ids", channel.id)
    await interaction.response.send_message(
        f"XP excluded for {channel.mention}. Excluded channel IDs: {sorted(XP_EXCLUDED_CHANNEL_IDS)}",
        ephemeral=True,
    )


@bot.tree.command(
    name="xp_include_here",
    description="Re-enable XP gain in this channel or thread.",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
async def xp_include_here(interaction: discord.Interaction):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    channel = get_xp_config_target_channel(interaction)
    if channel is None:
        await interaction.response.send_message("This command only works in text channels or threads.", ephemeral=True)
        return

    global XP_EXCLUDED_CHANNEL_IDS
    XP_EXCLUDED_CHANNEL_IDS = remove_config_id_value("xp_excluded_channel_ids", channel.id)
    await interaction.response.send_message(
        f"XP enabled for {channel.mention}. Excluded channel IDs: {sorted(XP_EXCLUDED_CHANNEL_IDS)}",
        ephemeral=True,
    )


@bot.tree.command(
    name="xp_excluded_channels",
    description="List channels and threads where XP is currently disabled.",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
async def xp_excluded_channels(interaction: discord.Interaction):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    if not XP_EXCLUDED_CHANNEL_IDS:
        await interaction.response.send_message("XP is currently enabled in all channels.", ephemeral=True)
        return

    guild = interaction.guild
    labels = []
    for channel_id in sorted(XP_EXCLUDED_CHANNEL_IDS):
        channel = guild.get_channel(channel_id) if guild else None
        labels.append(channel.mention if channel else f"`{channel_id}`")

    await interaction.response.send_message(
        "XP excluded in: " + ", ".join(labels),
        ephemeral=True,
    )


@bot.tree.command(
    name="xp_leaderboards",
    description="Show top 5 XP earners for a selected timescale, plus your standing.",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
@app_commands.describe(timescale="Choose the leaderboard window.")
async def xp_leaderboards(
    interaction: discord.Interaction,
    timescale: Literal["hour", "day", "week", "month", "year", "alltime"] = "alltime",
):
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    caller = interaction.user if isinstance(interaction.user, discord.Member) else guild.get_member(interaction.user.id)
    window_name, subtitle, color, cutoff = resolve_leaderboard_timescale(timescale)

    with open_db() as conn:
        if not has_any_xp_events(conn, guild.id):
            description = (
                "Existing XP totals predate the event ledger. New text and voice XP will appear here going forward."
                if has_any_member_xp(conn, guild.id)
                else "No XP recorded yet."
            )
            embed = discord.Embed(
                title="XP Leaderboards",
                description=description,
                color=discord.Color.blurple(),
            )
            embed.add_field(name="💬 Text", value="No tracked text XP yet.", inline=True)
            embed.add_field(name="↩️ Replies", value="No tracked reply XP yet.", inline=True)
            embed.add_field(name="🎙️ Voice", value="No tracked voice XP yet.", inline=True)
            embed.add_field(name="🖼️ Image Reacts", value="No tracked image react XP yet.", inline=True)
            embed.set_footer(text="Top 5 by XP source and time window")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

    if caller is None:
        await interaction.response.send_message("Could not resolve your member record in this guild.", ephemeral=True)
        return

    embed = build_xp_leaderboard_embed(guild, caller, window_name, subtitle, color, cutoff)
    await interaction.response.send_message(embed=embed, ephemeral=True)


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
    max_msgs="Max messages to include (default 200)"
)
async def user_review(
    interaction: discord.Interaction,
    member: discord.Member,
    hours: app_commands.Range[int, 1, 720] = 168,
    max_msgs: app_commands.Range[int, 20, 500] = 200,
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

if __name__ == "__main__":
    bot.run(TOKEN)
