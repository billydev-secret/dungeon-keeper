import asyncio
import discord
import logging
import os
import sqlite3
import time

from typing import Literal, TypeAlias
from datetime import datetime, timedelta, timezone
from discord import app_commands
from dotenv import load_dotenv
from openai import OpenAI
from pathlib import Path
from types import SimpleNamespace
from post_monitoring import message_has_qualifying_image, enforce_spoiler_requirement
from reports import register_reports
from xp_system import (
    AwardResult,
    DEFAULT_XP_SETTINGS,
    MessageXpContext,
    PairState,
    XP_SOURCE_GRANT,
    XP_SOURCE_IMAGE_REACT,
    XP_SOURCE_REPLY,
    XP_SOURCE_TEXT,
    XP_SOURCE_VOICE,
    apply_xp_award,
    calculate_message_xp,
    completed_voice_intervals,
    count_xp_events,
    delete_voice_session,
    get_xp_distribution_stats,
    get_oldest_xp_event_timestamp,
    has_any_member_xp,
    has_any_xp_events,
    get_member_xp_state,
    get_member_last_activity_map,
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
    record_member_activity,
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
GuildTextLike: TypeAlias = discord.TextChannel | discord.Thread

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

    level_5_log_channel_id = os.getenv("XP_LEVEL_5_LOG_CHANNEL_ID")
    if level_5_log_channel_id is not None:
        bootstrap["xp_level_5_log_channel_id"] = level_5_log_channel_id

    level_up_log_channel_id = os.getenv("XP_LEVEL_UP_LOG_CHANNEL_ID")
    if level_up_log_channel_id is not None:
        bootstrap["xp_level_up_log_channel_id"] = level_up_log_channel_id

    greeter_role_id = os.getenv("GREETER_ROLE_ID")
    if greeter_role_id is not None:
        bootstrap["greeter_role_id"] = greeter_role_id

    denizen_role_id = os.getenv("DENIZEN_ROLE_ID")
    if denizen_role_id is not None:
        bootstrap["denizen_role_id"] = denizen_role_id

    spoiler_channels = os.getenv("SPOILER_REQUIRED_CHANNELS")
    if spoiler_channels is not None:
        bootstrap_sets["spoiler_required_channels"] = parse_id_set(spoiler_channels)

    bypass_role_ids = os.getenv("BYPASS_ROLE_IDS")
    if bypass_role_ids is not None:
        bootstrap_sets["bypass_role_ids"] = parse_id_set(bypass_role_ids)

    xp_grant_allowed_user_ids = os.getenv("XP_GRANT_ALLOWED_USER_IDS")
    if xp_grant_allowed_user_ids is not None:
        bootstrap_sets["xp_grant_allowed_user_ids"] = parse_id_set(xp_grant_allowed_user_ids)

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


def set_config_value(key: str, value: str) -> str:
    with open_db() as conn:
        conn.execute(
            """
            INSERT INTO config (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        return get_config_value(conn, key, value)


def load_runtime_config() -> dict[str, object]:
    with open_db() as conn:
        return {
            "mod_channel_id": int(get_config_value(conn, "mod_channel_id", "0")),
            "debug": parse_bool(get_config_value(conn, "debug", "1"), default=True),
            "xp_level_5_role_id": int(get_config_value(conn, "xp_level_5_role_id", "0")),
            "xp_level_5_log_channel_id": int(get_config_value(conn, "xp_level_5_log_channel_id", "0")),
            "xp_level_up_log_channel_id": int(get_config_value(conn, "xp_level_up_log_channel_id", "0")),
            "greeter_role_id": int(get_config_value(conn, "greeter_role_id", "0")),
            "denizen_role_id": int(get_config_value(conn, "denizen_role_id", "0")),
            "spoiler_required_channels": get_config_id_set(conn, "spoiler_required_channels"),
            "bypass_role_ids": get_config_id_set(conn, "bypass_role_ids"),
            "xp_grant_allowed_user_ids": get_config_id_set(conn, "xp_grant_allowed_user_ids"),
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
XP_GRANT_ALLOWED_USER_IDS = runtime_config["xp_grant_allowed_user_ids"]
XP_EXCLUDED_CHANNEL_IDS = runtime_config["xp_excluded_channel_ids"]
LEVEL_5_ROLE_ID = runtime_config["xp_level_5_role_id"]
LEVEL_5_LOG_CHANNEL_ID = runtime_config["xp_level_5_log_channel_id"]
LEVEL_UP_LOG_CHANNEL_ID = runtime_config["xp_level_up_log_channel_id"]
GREETER_ROLE_ID = runtime_config["greeter_role_id"]
DENIZEN_ROLE_ID = runtime_config["denizen_role_id"]

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
# Events
# ==============================
@bot.event
async def on_ready():
    if bot.user is None:
        log.warning("Bot user was not available during on_ready.")
        return

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

    if await enforce_spoiler_requirement(
        message,
        spoiler_required_channels=SPOILER_REQUIRED_CHANNELS,
        bypass_role_ids=BYPASS_ROLE_IDS,
        log=log,
    ):
        return

    message_ts = message.created_at.timestamp() if message.created_at else time.time()
    with open_db() as conn:
        record_member_activity(
            conn,
            message.guild.id,
            message.author.id,
            message.channel.id,
            message.id,
            message_ts,
        )

    await award_message_xp(message)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    await award_image_reaction_xp(payload)

# ==============================
# Logic
# ==============================
def channel_is_xp_allowed(channel: GuildTextLike) -> bool:
    channel_id = getattr(channel, "id", None)
    if channel_id is None:
        return False
    parent_id = getattr(channel, "parent_id", None)
    return is_channel_xp_eligible(channel_id, parent_id, XP_EXCLUDED_CHANNEL_IDS)


def get_interaction_member(interaction: discord.Interaction) -> discord.Member | None:
    user = interaction.user
    if isinstance(user, discord.Member):
        return user
    guild = interaction.guild
    if guild is None:
        return None
    return guild.get_member(user.id)


def get_bot_member(guild: discord.Guild) -> discord.Member | None:
    if guild.me is not None:
        return guild.me

    bot_user = guild.client.user
    if bot_user is None:
        return None

    return guild.get_member(bot_user.id)


def format_user_for_log(user: discord.abc.User | discord.Member | None = None, user_id: int | None = None) -> str:
    if user is not None:
        resolved_id = getattr(user, "id", user_id)
        display_name = getattr(user, "display_name", None)
        username = getattr(user, "name", None)
        if display_name and username and display_name != username:
            return f"{display_name} [{username}] ({resolved_id})"
        label = display_name or username or str(user)
        return f"{label} ({resolved_id})" if resolved_id is not None else label

    if user_id is None:
        return "unknown user"

    return f"user {user_id}"


def resolve_user_for_log(guild: discord.Guild | None, user_id: int) -> str:
    member = guild.get_member(user_id) if guild is not None else None
    return format_user_for_log(member, user_id)


async def resolve_reply_target(message: discord.Message) -> discord.Message | None:
    if not message.reference:
        return None

    if isinstance(message.reference.resolved, discord.Message):
        return message.reference.resolved

    if not message.reference.message_id:
        return None

    ref_channel = message.guild.get_channel(message.reference.channel_id) if message.guild else None
    if ref_channel is None and isinstance(message.channel, (discord.TextChannel, discord.Thread)):
        ref_channel = message.channel

    if not isinstance(ref_channel, (discord.TextChannel, discord.Thread)):
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


def get_guild_channel_or_thread(guild: discord.Guild, channel_id: int) -> GuildTextLike | None:
    resolver = getattr(guild, "get_channel_or_thread", None)
    if callable(resolver):
        channel = resolver(channel_id)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        return None

    channel = guild.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        return channel

    thread = guild.get_thread(channel_id)
    if isinstance(thread, discord.Thread):
        return thread

    return None


async def maybe_log_level_5(member: discord.Member, total_xp: float) -> None:
    if LEVEL_5_LOG_CHANNEL_ID <= 0:
        return

    channel = get_guild_channel_or_thread(member.guild, LEVEL_5_LOG_CHANNEL_ID)
    if channel is None:
        log.warning(
            "Level %s log channel %s was not found.",
            XP_SETTINGS.role_grant_level,
            LEVEL_5_LOG_CHANNEL_ID,
        )
        return

    reward_role = member.guild.get_role(LEVEL_5_ROLE_ID) if LEVEL_5_ROLE_ID > 0 else None
    embed = discord.Embed(
        title=f"Level {XP_SETTINGS.role_grant_level} reached",
        description=f"{member.mention} just reached level {XP_SETTINGS.role_grant_level}.",
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Total XP", value=f"{total_xp:.2f}", inline=True)
    if reward_role is not None:
        embed.add_field(name="Reward Role", value=reward_role.mention, inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)

    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        log.warning(
            "Missing permission to send level %s announcements in channel %s.",
            XP_SETTINGS.role_grant_level,
            LEVEL_5_LOG_CHANNEL_ID,
        )


async def maybe_log_level_ups(member: discord.Member, old_level: int, new_level: int, total_xp: float) -> None:
    if LEVEL_UP_LOG_CHANNEL_ID <= 0 or new_level <= old_level:
        return

    channel = get_guild_channel_or_thread(member.guild, LEVEL_UP_LOG_CHANNEL_ID)
    if channel is None:
        log.warning("Level-up log channel %s was not found.", LEVEL_UP_LOG_CHANNEL_ID)
        return

    skip_special_level = LEVEL_UP_LOG_CHANNEL_ID == LEVEL_5_LOG_CHANNEL_ID
    for level in range(old_level + 1, new_level + 1):
        if skip_special_level and level == XP_SETTINGS.role_grant_level:
            continue

        embed = discord.Embed(
            title=f"Level {level} reached",
            description=f"{member.mention} leveled up to level {level}.",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Total XP", value=f"{total_xp:.2f}", inline=True)
        embed.set_thumbnail(url=member.display_avatar.url)

        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            log.warning("Missing permission to send level-up announcements in channel %s.", LEVEL_UP_LOG_CHANNEL_ID)
            return


async def handle_level_progress(member: discord.Member, award: AwardResult) -> None:
    if award.new_level >= XP_SETTINGS.role_grant_level:
        await maybe_grant_level_role(member, award.new_level)

    if award.new_level > award.old_level:
        await maybe_log_level_ups(member, award.old_level, award.new_level, award.total_xp)
        if award.role_grant_due:
            await maybe_log_level_5(member, award.total_xp)


async def award_message_xp(message: discord.Message) -> None:
    if not message.guild or not isinstance(message.author, discord.Member):
        return

    if not channel_is_xp_allowed(message.channel):
        log.debug(
            "XP skipped for %s in #%s: channel excluded.",
            format_user_for_log(message.author),
            getattr(message.channel, "id", "unknown"),
        )
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
        state = get_member_xp_state(conn, message.guild.id, message.author.id, XP_SETTINGS)
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
            format_user_for_log(message.author),
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
        format_user_for_log(message.author),
        getattr(message.channel, "id", "unknown"),
        breakdown.qualified_words,
        award.total_xp,
        award.new_level,
    )

    await handle_level_progress(message.author, award)


async def award_image_reaction_xp(payload: discord.RawReactionActionEvent) -> None:
    bot_user = bot.user
    if payload.guild_id is None or bot_user is None or payload.user_id == bot_user.id:
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return

    channel = get_guild_channel_or_thread(guild, payload.channel_id)
    if channel is None or not channel_is_xp_allowed(channel):
        return

    member = guild.get_member(payload.user_id)
    if member is not None and member.bot:
        return

    try:
        message = await channel.fetch_message(payload.message_id)
    except (discord.Forbidden, discord.NotFound):
        return

    if not isinstance(message.author, discord.Member):
        author = guild.get_member(message.author.id)
        if author is None:
            return
    else:
        author = message.author

    if author.bot or author.id == payload.user_id:
        return

    if not message_has_qualifying_image(message):
        return

    with open_db() as conn:
        award = apply_xp_award(
            conn,
            guild.id,
            author.id,
            XP_SETTINGS.image_reaction_received_xp,
            event_source=XP_SOURCE_IMAGE_REACT,
            settings=XP_SETTINGS,
        )

    log.debug(
        "Awarded %.2f image reaction XP to %s for message %s from reaction by %s.",
        award.awarded_xp,
        format_user_for_log(author),
        message.id,
        resolve_user_for_log(guild, payload.user_id),
    )

    await handle_level_progress(author, award)


def is_qualifying_voice_channel(channel: discord.VoiceChannel) -> bool:
    afk_channel = channel.guild.afk_channel
    if afk_channel and channel.id == afk_channel.id:
        return False

    human_count = sum(1 for member in channel.members if not member.bot)
    return human_count >= XP_SETTINGS.voice_min_humans


async def process_voice_xp_tick() -> None:
    leveled_members: dict[tuple[int, int], tuple[discord.Member, AwardResult]] = {}
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
                            format_user_for_log(member),
                            channel.id,
                            award.total_xp,
                            award.new_level,
                        )
                        leveled_members[(guild.id, member.id)] = (member, award)

        for session in list_voice_sessions(conn):
            if (session.guild_id, session.user_id) not in active_members:
                delete_voice_session(conn, session.guild_id, session.user_id)

    for member, award in leveled_members.values():
        await handle_level_progress(member, award)


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



def is_mod(interaction: discord.Interaction) -> bool:
    member = get_interaction_member(interaction)
    if member is None:
        return False
    perms = member.guild_permissions
    return perms.manage_guild or perms.administrator


def can_grant_denizen(interaction: discord.Interaction) -> bool:
    if is_mod(interaction):
        return True

    member = get_interaction_member(interaction)
    if member is None or GREETER_ROLE_ID <= 0:
        return False

    return any(role.id == GREETER_ROLE_ID for role in member.roles)


def can_use_xp_grant(interaction: discord.Interaction) -> bool:
    if is_mod(interaction):
        return True
    return interaction.user.id in XP_GRANT_ALLOWED_USER_IDS


def get_xp_config_target_channel(interaction: discord.Interaction) -> GuildTextLike | None:
    channel = interaction.channel
    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        return channel
    return None


def iter_backfill_channels(guild: discord.Guild) -> list[GuildTextLike]:
    channels: list[GuildTextLike] = list(guild.text_channels)
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
    stats_line: str,
    empty_text: str,
    user_line: str,
) -> str:
    if not entries:
        return f"{stats_line}\n\n{empty_text}\n\n{user_line}"

    rank_icons = ["🥇", "🥈", "🥉", "4.", "5."]
    lines = [stats_line, ""]
    for idx, entry in enumerate(entries, start=1):
        member = guild.get_member(entry.user_id) if guild else None
        label = member.mention if member else f"<@{entry.user_id}>"
        rank = rank_icons[idx - 1] if idx <= len(rank_icons) else f"{idx}."
        lines.append(f"{rank} {label}\n`{entry.xp:.2f} XP`")

    lines.append("")
    lines.append(user_line)
    return "\n".join(lines)


def format_xp_distribution_summary(member_count: int, median_xp: float, stddev_xp: float) -> str:
    return (
        "**Distribution**\n"
        f"Members: **{member_count}**\n"
        f"Median: `{median_xp:.2f} XP`\n"
        f"Std Dev: `{stddev_xp:.2f} XP`"
    )


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
            distribution = get_xp_distribution_stats(
                conn,
                guild.id,
                source_key,
                since_ts=cutoff,
            )
            standing = get_user_xp_standing(
                conn,
                guild.id,
                source_key,
                caller.id,
                since_ts=cutoff,
            )
            stats_line = format_xp_distribution_summary(
                distribution.member_count,
                distribution.median_xp,
                distribution.stddev_xp,
            )
            if standing.rank is None:
                user_line = f"Your standing: {caller.mention} has no tracked XP here."
            else:
                user_line = f"Your standing: #{standing.rank} {caller.mention} with `{standing.xp:.2f} XP`"
            embed.add_field(
                name=f"{icon} {field_name}",
                value=format_xp_leaderboard_lines(guild, entries, stats_line, empty_text, user_line),
                inline=True,
            )

    embed.set_footer(text="Top 5 by XP source with your standing")
    return embed

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

        me = get_bot_member(guild)
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
    name="grant_denizen",
    description="Grant the Denizen role to a member.",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
@app_commands.describe(member="Member to receive the Denizen role.")
async def grant_denizen(interaction: discord.Interaction, member: discord.Member):
    if not can_grant_denizen(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    actor = get_interaction_member(interaction)
    if member.bot:
        await interaction.response.send_message("Bots can't receive the Denizen role.", ephemeral=True)
        return

    if actor is not None and member.id == actor.id and not is_mod(interaction):
        await interaction.response.send_message("You can't grant the Denizen role to yourself.", ephemeral=True)
        return

    if DENIZEN_ROLE_ID <= 0:
        await interaction.response.send_message("The Denizen role is not configured yet.", ephemeral=True)
        return

    denizen_role = guild.get_role(DENIZEN_ROLE_ID)
    if denizen_role is None:
        await interaction.response.send_message("The configured Denizen role no longer exists.", ephemeral=True)
        return

    if denizen_role in member.roles:
        await interaction.response.send_message(f"{member.mention} already has {denizen_role.mention}.", ephemeral=True)
        return

    bot_member = get_bot_member(guild)
    if bot_member is None:
        await interaction.response.send_message("Bot member context is unavailable right now.", ephemeral=True)
        return

    if not bot_member.guild_permissions.manage_roles:
        await interaction.response.send_message("I need the Manage Roles permission to do that.", ephemeral=True)
        return

    if denizen_role >= bot_member.top_role:
        await interaction.response.send_message(
            f"I can't grant {denizen_role.mention} because it is above my highest role.",
            ephemeral=True,
        )
        return

    try:
        await member.add_roles(denizen_role, reason=f"Granted by {interaction.user} via /grant_denizen")
    except discord.Forbidden:
        await interaction.response.send_message(
            f"I couldn't grant {denizen_role.mention}. Check my role hierarchy and permissions.",
            ephemeral=True,
        )
        return

    log.info(
        "%s granted %s to %s.",
        format_user_for_log(actor, interaction.user.id),
        denizen_role.name,
        format_user_for_log(member),
    )
    await interaction.response.send_message(
        f"{member.mention} has been granted {denizen_role.mention}.",
        ephemeral=False,
    )


@bot.tree.command(
    name="set_greeter_role",
    description="Set which role can use /grant_denizen.",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
@app_commands.describe(role="Role allowed to grant Denizen.")
async def set_greeter_role(interaction: discord.Interaction, role: discord.Role):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    global GREETER_ROLE_ID
    GREETER_ROLE_ID = int(set_config_value("greeter_role_id", str(role.id)))
    await interaction.response.send_message(
        f"Members with {role.mention} can now use /grant_denizen.",
        ephemeral=True,
    )


@bot.tree.command(
    name="set_denizen_role",
    description="Set which role /grant_denizen will assign.",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
@app_commands.describe(role="Role to grant with /grant_denizen.")
async def set_denizen_role(interaction: discord.Interaction, role: discord.Role):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    global DENIZEN_ROLE_ID
    DENIZEN_ROLE_ID = int(set_config_value("denizen_role_id", str(role.id)))
    await interaction.response.send_message(
        f"/grant_denizen will now assign {role.mention}.",
        ephemeral=True,
    )


@bot.tree.command(
    name="xp_give",
    description="Give a member 20 XP.",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
@app_commands.describe(member="Member to receive the XP.")
async def xp_give(interaction: discord.Interaction, member: discord.Member):
    if not can_use_xp_grant(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    if member.bot:
        await interaction.response.send_message("Bots cannot receive XP grants.", ephemeral=True)
        return

    if member.id == interaction.user.id:
        await interaction.response.send_message("You can't grant XP to yourself.", ephemeral=True)
        return

    now_ts = time.time()
    with open_db() as conn:
        award = apply_xp_award(
            conn,
            guild.id,
            member.id,
            XP_SETTINGS.manual_grant_xp,
            event_source=XP_SOURCE_GRANT,
            event_timestamp=now_ts,
            settings=XP_SETTINGS,
        )

    await handle_level_progress(member, award)

    await interaction.response.send_message(
        f"{interaction.user.mention} granted {XP_SETTINGS.manual_grant_xp:.0f} XP to {member.mention}. "
        f"They now have {award.total_xp:.2f} XP and are level {award.new_level}.",
        ephemeral=False,
    )


@bot.tree.command(
    name="xp_give_allow",
    description="Allow a user to use /xp_give.",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
@app_commands.describe(member="User to add to the /xp_give allowlist.")
async def xp_give_allow(interaction: discord.Interaction, member: discord.Member):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    global XP_GRANT_ALLOWED_USER_IDS
    XP_GRANT_ALLOWED_USER_IDS = add_config_id_value("xp_grant_allowed_user_ids", member.id)
    await interaction.response.send_message(
        f"{member.mention} can now use /xp_give. Allowed user IDs: {sorted(XP_GRANT_ALLOWED_USER_IDS)}",
        ephemeral=True,
    )


@bot.tree.command(
    name="xp_give_disallow",
    description="Remove a user from /xp_give access.",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
@app_commands.describe(member="User to remove from the /xp_give allowlist.")
async def xp_give_disallow(interaction: discord.Interaction, member: discord.Member):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    global XP_GRANT_ALLOWED_USER_IDS
    XP_GRANT_ALLOWED_USER_IDS = remove_config_id_value("xp_grant_allowed_user_ids", member.id)
    await interaction.response.send_message(
        f"{member.mention} can no longer use /xp_give. Allowed user IDs: {sorted(XP_GRANT_ALLOWED_USER_IDS)}",
        ephemeral=True,
    )


@bot.tree.command(
    name="xp_give_allowed",
    description="List users allowed to use /xp_give.",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
async def xp_give_allowed(interaction: discord.Interaction):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    if not XP_GRANT_ALLOWED_USER_IDS:
        await interaction.response.send_message("No regular users are currently allowed to use /xp_give.", ephemeral=True)
        return

    guild = interaction.guild
    labels = []
    for user_id in sorted(XP_GRANT_ALLOWED_USER_IDS):
        member = guild.get_member(user_id) if guild else None
        labels.append(member.mention if member else f"`{user_id}`")

    await interaction.response.send_message(
        "Users allowed to use /xp_give: " + ", ".join(labels),
        ephemeral=True,
    )


@bot.tree.command(
    name="xp_set_levelup_log_here",
    description="Send level-up announcements to this channel or thread.",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
async def xp_set_levelup_log_here(interaction: discord.Interaction):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    channel = get_xp_config_target_channel(interaction)
    if channel is None:
        await interaction.response.send_message("This command only works in text channels or threads.", ephemeral=True)
        return

    global LEVEL_UP_LOG_CHANNEL_ID
    LEVEL_UP_LOG_CHANNEL_ID = int(set_config_value("xp_level_up_log_channel_id", str(channel.id)))
    await interaction.response.send_message(
        f"Level-up announcements will be posted in {channel.mention}.",
        ephemeral=True,
    )


@bot.tree.command(
    name="xp_set_level5_log_here",
    description="Send level 5 XP announcements to this channel or thread.",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
async def xp_set_level5_log_here(interaction: discord.Interaction):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    channel = get_xp_config_target_channel(interaction)
    if channel is None:
        await interaction.response.send_message("This command only works in text channels or threads.", ephemeral=True)
        return

    global LEVEL_5_LOG_CHANNEL_ID
    LEVEL_5_LOG_CHANNEL_ID = int(set_config_value("xp_level_5_log_channel_id", str(channel.id)))
    await interaction.response.send_message(
        f"Level {XP_SETTINGS.role_grant_level} announcements will be posted in {channel.mention}.",
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


report_ctx = SimpleNamespace(
    guild_id=GUILD_ID,
    debug=DEBUG,
    client=client,
    model=MODEL,
    bigmodel=BIGMODEL,
    mod_channel_id=MOD_CHANNEL_ID,
    open_db=open_db,
    get_bot_member=get_bot_member,
    get_guild_channel_or_thread=get_guild_channel_or_thread,
    get_interaction_member=get_interaction_member,
    get_member_last_activity_map=get_member_last_activity_map,
    is_mod=is_mod,
)
register_reports(bot, report_ctx)

# ==============================
# Run
# ==============================
if __name__ == "__main__":
    bot.run(TOKEN)
