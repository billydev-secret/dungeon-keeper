import asyncio
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, TypeAlias, TypedDict, cast

import discord
from discord import app_commands
from dotenv import load_dotenv

from post_monitoring import enforce_spoiler_requirement, message_has_qualifying_image
from reports import register_reports
from xp_system import (
    DEFAULT_XP_SETTINGS,
    XP_SOURCE_GRANT,
    XP_SOURCE_IMAGE_REACT,
    XP_SOURCE_REPLY,
    XP_SOURCE_TEXT,
    XP_SOURCE_VOICE,
    AwardResult,
    MessageXpContext,
    PairState,
    apply_xp_award,
    calculate_message_xp,
    completed_voice_intervals,
    count_xp_events,
    delete_voice_session,
    get_member_last_activity_map,
    get_member_xp_state,
    get_oldest_xp_event_timestamp,
    get_user_xp_standing,
    get_voice_session,
    get_xp_distribution_stats,
    get_xp_leaderboard,
    has_any_member_xp,
    has_any_xp_events,
    init_xp_tables,
    is_channel_xp_eligible,
    is_message_processed,
    list_voice_sessions,
    mark_message_processed,
    normalize_message_content,
    record_member_activity,
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
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)

log = logging.getLogger("dungeonkeeper.bot")

TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = Path(__file__).with_name("dungeonkeeper.db")

XP_SETTINGS = DEFAULT_XP_SETTINGS
GuildTextLike: TypeAlias = discord.TextChannel | discord.Thread


class RuntimeConfig(TypedDict):
    guild_id: int
    mod_channel_id: int
    debug: bool
    xp_level_5_role_id: int
    xp_level_5_log_channel_id: int
    xp_level_up_log_channel_id: int
    greeter_role_id: int
    denizen_role_id: int
    spoiler_required_channels: set[int]
    bypass_role_ids: set[int]
    xp_grant_allowed_user_ids: set[int]
    xp_excluded_channel_ids: set[int]

MAX_MESSAGES = 400           # hard cap on messages pulled
MAX_CHARS_PER_MSG = 240      # truncate each message
MAX_TOTAL_CHARS = 40_000     # cap payload size to the model

AUTO_DELETE_RUN_KEYWORDS: dict[str, str] = {
    "once": "once",
    "now": "once",
    "manual": "once",
    "off": "off",
    "disable": "off",
    "none": "off",
}
AUTO_DELETE_NAMED_INTERVALS: dict[str, int] = {
    "hourly": 60 * 60,
    "daily": 24 * 60 * 60,
    "weekly": 7 * 24 * 60 * 60,
}
AUTO_DELETE_DURATION_RE = re.compile(
    r"(\d+)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)",
    re.IGNORECASE,
)
AUTO_DELETE_MIN_AGE_SECONDS = 60
AUTO_DELETE_MIN_INTERVAL_SECONDS = 60
AUTO_DELETE_POLL_SECONDS = 60
AUTO_DELETE_DELETE_PAUSE_SECONDS = 0.35

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


def init_auto_delete_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_delete_rules (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            max_age_seconds INTEGER NOT NULL,
            interval_seconds INTEGER NOT NULL,
            last_run_ts REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, channel_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_delete_messages (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (guild_id, channel_id, message_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_delete_messages_due
        ON auto_delete_messages (guild_id, channel_id, created_at)
        """
    )


def load_runtime_config() -> RuntimeConfig:
    with open_db() as conn:
        return {
            "guild_id": int(get_config_value(conn, "guild_id", "0")),
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
runtime_config: RuntimeConfig = load_runtime_config()

with open_db() as conn:
    init_xp_tables(conn)
    init_auto_delete_tables(conn)

GUILD_ID = runtime_config["guild_id"]
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
        self.auto_delete_task: asyncio.Task | None = None

    async def setup_hook(self):
        if DEBUG and GUILD_ID > 0:
            guild = discord.Object(id=GUILD_ID)
            try:
                self.tree.clear_commands(guild=guild)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                print("Synced commands to development guild.")
            except discord.Forbidden:
                log.warning(
                    "Guild command sync failed for guild_id=%s (Missing Access). Falling back to global sync.",
                    GUILD_ID,
                )
                await self.tree.sync()
                print("Synced commands globally (fallback).")
        else:
            if DEBUG and GUILD_ID <= 0:
                log.warning("DEBUG is enabled but guild_id=%s is invalid; falling back to global sync.", GUILD_ID)
            await self.tree.sync()
            print("Synced commands globally.")

        if self.voice_xp_task is None:
            self.voice_xp_task = asyncio.create_task(voice_xp_loop())
        if self.auto_delete_task is None:
            self.auto_delete_task = asyncio.create_task(auto_delete_loop())

bot = Bot()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.CommandNotFound):
        missing_name = getattr(error, "name", "unknown")
        log.warning(
            "Received unknown slash command '%s' in guild %s (user %s). This is usually stale command registration.",
            missing_name,
            interaction.guild_id,
            interaction.user.id,
        )
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "That command is out of date on this server. Please try again in a moment.",
                ephemeral=True,
            )
        return

    log.exception("Unhandled app command error: %s", error)
    if not interaction.response.is_done():
        await interaction.response.send_message("Command failed. Please try again.", ephemeral=True)

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
        if auto_delete_rule_exists(conn, message.guild.id, message.channel.id):
            track_auto_delete_message(
                conn,
                message.guild.id,
                message.channel.id,
                message.id,
                message_ts,
            )

    await award_message_xp(message)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    await award_image_reaction_xp(payload)


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    if payload.guild_id is None:
        return
    remove_tracked_auto_delete_message(payload.guild_id, payload.channel_id, payload.message_id)


@bot.event
async def on_raw_bulk_message_delete(payload: discord.RawBulkMessageDeleteEvent):
    if payload.guild_id is None:
        return
    remove_tracked_auto_delete_messages(payload.guild_id, payload.channel_id, payload.message_ids)

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

    ref_channel: GuildTextLike | None = None
    if message.guild is not None and message.reference.channel_id is not None:
        candidate_channel = message.guild.get_channel(message.reference.channel_id)
        if isinstance(candidate_channel, discord.TextChannel):
            ref_channel = candidate_channel
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

    channel = message.channel
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return

    if not channel_is_xp_allowed(channel):
        log.debug(
            "XP skipped for %s in #%s: channel excluded.",
            format_user_for_log(message.author),
            getattr(channel, "id", "unknown"),
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
    pair_state = bot.xp_pair_states.get(channel.id)
    next_pair_state, pair_streak = update_pair_state(pair_state, message.author.id)
    bot.xp_pair_states[channel.id] = next_pair_state

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


async def process_auto_delete_tick() -> None:
    now_ts = time.time()
    rules = list_auto_delete_rules()
    if not rules:
        return

    for rule in rules:
        guild_id = int(rule["guild_id"])
        channel_id = int(rule["channel_id"])
        max_age_seconds = int(rule["max_age_seconds"])
        interval_seconds = int(rule["interval_seconds"])
        last_run_ts = float(rule["last_run_ts"])
        if now_ts - last_run_ts < interval_seconds:
            continue

        guild = bot.get_guild(guild_id)
        if guild is None:
            log.warning("Auto-delete skipped: guild %s is unavailable for channel %s.", guild_id, channel_id)
            touch_auto_delete_rule_run(guild_id, channel_id, now_ts)
            continue

        channel = get_guild_channel_or_thread(guild, channel_id)
        if channel is None:
            log.warning("Auto-delete removed stale rule for missing channel %s in guild %s.", channel_id, guild_id)
            remove_auto_delete_rule(guild_id, channel_id)
            continue

        cutoff_ts = now_ts - max_age_seconds
        interval_label = "hourly" if interval_seconds <= 60 * 60 else "daily"
        try:
            queued, deleted, failed = await delete_tracked_messages_older_than(
                guild_id,
                channel,
                cutoff_ts,
                reason=f"Scheduled auto-delete ({interval_label})",
            )
            log.info(
                "Auto-delete ran in %s (%s): deleted=%s queued=%s failed=%s age>%ss.",
                channel.mention,
                channel.id,
                deleted,
                queued,
                failed,
                max_age_seconds,
            )
        except discord.Forbidden:
            log.warning("Auto-delete missing permissions in %s (%s).", channel.mention, channel.id)
        except Exception:
            log.exception("Auto-delete failed in channel %s for guild %s.", channel_id, guild_id)
        finally:
            touch_auto_delete_rule_run(guild_id, channel_id, now_ts)


async def auto_delete_loop() -> None:
    await bot.wait_until_ready()

    while not bot.is_closed():
        try:
            await process_auto_delete_tick()
        except asyncio.CancelledError:
            raise
        except sqlite3.OperationalError:
            log.exception("Auto-delete tick hit a SQLite operational error.")
        except Exception:
            log.exception("Auto-delete tick failed.")

        await asyncio.sleep(AUTO_DELETE_POLL_SECONDS)



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


def upsert_auto_delete_rule(
    guild_id: int,
    channel_id: int,
    max_age_seconds: int,
    interval_seconds: int,
    *,
    last_run_ts: float | None = None,
) -> None:
    run_ts = time.time() if last_run_ts is None else last_run_ts
    with open_db() as conn:
        conn.execute(
            """
            INSERT INTO auto_delete_rules (
                guild_id,
                channel_id,
                max_age_seconds,
                interval_seconds,
                last_run_ts
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, channel_id) DO UPDATE SET
                max_age_seconds = excluded.max_age_seconds,
                interval_seconds = excluded.interval_seconds,
                last_run_ts = excluded.last_run_ts
            """,
            (guild_id, channel_id, max_age_seconds, interval_seconds, run_ts),
        )


def remove_auto_delete_rule(guild_id: int, channel_id: int) -> bool:
    with open_db() as conn:
        cursor = conn.execute(
            "DELETE FROM auto_delete_rules WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        )
        conn.execute(
            "DELETE FROM auto_delete_messages WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        )
        return cursor.rowcount > 0


def touch_auto_delete_rule_run(guild_id: int, channel_id: int, run_ts: float) -> None:
    with open_db() as conn:
        conn.execute(
            "UPDATE auto_delete_rules SET last_run_ts = ? WHERE guild_id = ? AND channel_id = ?",
            (run_ts, guild_id, channel_id),
        )


def list_auto_delete_rules() -> list[sqlite3.Row]:
    with open_db() as conn:
        return conn.execute(
            """
            SELECT guild_id, channel_id, max_age_seconds, interval_seconds, last_run_ts
            FROM auto_delete_rules
            ORDER BY guild_id, channel_id
            """
        ).fetchall()


def list_auto_delete_rules_for_guild(guild_id: int) -> list[sqlite3.Row]:
    with open_db() as conn:
        return conn.execute(
            """
            SELECT guild_id, channel_id, max_age_seconds, interval_seconds, last_run_ts
            FROM auto_delete_rules
            WHERE guild_id = ?
            ORDER BY channel_id
            """,
            (guild_id,),
        ).fetchall()


def auto_delete_rule_exists(conn: sqlite3.Connection, guild_id: int, channel_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM auto_delete_rules
        WHERE guild_id = ? AND channel_id = ?
        LIMIT 1
        """,
        (guild_id, channel_id),
    ).fetchone()
    return row is not None


def track_auto_delete_message(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    message_id: int,
    created_at: float,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO auto_delete_messages (guild_id, channel_id, message_id, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (guild_id, channel_id, message_id, created_at),
    )


def remove_tracked_auto_delete_message(guild_id: int, channel_id: int, message_id: int) -> None:
    with open_db() as conn:
        conn.execute(
            """
            DELETE FROM auto_delete_messages
            WHERE guild_id = ? AND channel_id = ? AND message_id = ?
            """,
            (guild_id, channel_id, message_id),
        )


def remove_tracked_auto_delete_messages(
    guild_id: int,
    channel_id: int,
    message_ids: set[int],
) -> None:
    if not message_ids:
        return
    with open_db() as conn:
        conn.executemany(
            """
            DELETE FROM auto_delete_messages
            WHERE guild_id = ? AND channel_id = ? AND message_id = ?
            """,
            [(guild_id, channel_id, message_id) for message_id in message_ids],
        )


def pop_due_auto_delete_message_ids(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    cutoff_ts: float,
    *,
    limit: int = 500,
) -> list[int]:
    rows = conn.execute(
        """
        SELECT message_id
        FROM auto_delete_messages
        WHERE guild_id = ? AND channel_id = ? AND created_at <= ?
        ORDER BY created_at, message_id
        LIMIT ?
        """,
        (guild_id, channel_id, cutoff_ts, limit),
    ).fetchall()
    return [int(row["message_id"]) for row in rows]


async def delete_tracked_messages_older_than(
    guild_id: int,
    channel: GuildTextLike,
    cutoff_ts: float,
    *,
    reason: str,
) -> tuple[int, int, int]:
    queued = 0
    deleted = 0
    failed = 0
    next_delete_at = 0.0

    with open_db() as conn:
        message_ids = pop_due_auto_delete_message_ids(conn, guild_id, channel.id, cutoff_ts)

    if not message_ids:
        return queued, deleted, failed

    for message_id in message_ids:
        queued += 1
        now_monotonic = time.monotonic()
        if now_monotonic < next_delete_at:
            await asyncio.sleep(next_delete_at - now_monotonic)

        partial = channel.get_partial_message(message_id)
        try:
            delete_call = cast(Any, partial.delete)
            try:
                await delete_call(reason=reason)
            except TypeError:
                await partial.delete()
            deleted += 1
            remove_tracked_auto_delete_message(guild_id, channel.id, message_id)
            next_delete_at = time.monotonic() + AUTO_DELETE_DELETE_PAUSE_SECONDS
        except discord.NotFound:
            remove_tracked_auto_delete_message(guild_id, channel.id, message_id)
        except discord.Forbidden:
            failed += 1
            break
        except discord.HTTPException:
            failed += 1

    return queued, deleted, failed


def format_duration_seconds(seconds: int) -> str:
    if seconds <= 0:
        return "0s"
    units = (
        (24 * 60 * 60, "day"),
        (60 * 60, "hour"),
        (60, "minute"),
    )
    for unit_seconds, unit_label in units:
        if seconds % unit_seconds == 0:
            amount = seconds // unit_seconds
            suffix = "" if amount == 1 else "s"
            return f"{amount} {unit_label}{suffix}"
    return f"{seconds} seconds"


def parse_duration_seconds(value: str) -> int | None:
    text = value.strip().lower()
    if not text:
        return None
    if text in AUTO_DELETE_NAMED_INTERVALS:
        return AUTO_DELETE_NAMED_INTERVALS[text]

    total = 0
    cursor = 0
    for match in AUTO_DELETE_DURATION_RE.finditer(text):
        separator = text[cursor:match.start()]
        if separator.strip():
            return None

        amount = int(match.group(1))
        unit = match.group(2).lower()
        if unit.startswith("w"):
            multiplier = 7 * 24 * 60 * 60
        elif unit.startswith("d"):
            multiplier = 24 * 60 * 60
        elif unit.startswith("h"):
            multiplier = 60 * 60
        elif unit.startswith("m"):
            multiplier = 60
        else:
            multiplier = 1
        total += amount * multiplier
        cursor = match.end()

    if cursor == 0:
        return None

    if text[cursor:].strip():
        return None

    return total if total > 0 else None


async def send_ephemeral_text_chunks(interaction: discord.Interaction, text: str, chunk_size: int = 1900) -> None:
    remaining = text
    while remaining:
        if len(remaining) <= chunk_size:
            await interaction.followup.send(remaining, ephemeral=True)
            return
        split_at = remaining.rfind("\n", 0, chunk_size + 1)
        if split_at <= 0:
            split_at = chunk_size
        chunk = remaining[:split_at]
        remaining = remaining[split_at:].lstrip("\n")
        await interaction.followup.send(chunk, ephemeral=True)


async def delete_messages_older_than(
    channel: GuildTextLike,
    cutoff: datetime,
    *,
    reason: str,
) -> tuple[int, int, int, int]:
    scanned = 0
    deleted = 0
    skipped_pinned = 0
    failed = 0
    next_delete_at = 0.0

    async for message in channel.history(limit=None, before=cutoff, oldest_first=True):
        scanned += 1
        if message.pinned:
            skipped_pinned += 1
            continue
        now_monotonic = time.monotonic()
        if now_monotonic < next_delete_at:
            await asyncio.sleep(next_delete_at - now_monotonic)
        try:
            delete_call = cast(Any, message.delete)
            try:
                await delete_call(reason=reason)
            except TypeError:
                await message.delete()
            deleted += 1
            next_delete_at = time.monotonic() + AUTO_DELETE_DELETE_PAUSE_SECONDS
        except discord.Forbidden:
            failed += 1
            break
        except discord.HTTPException:
            failed += 1

    return scanned, deleted, skipped_pinned, failed


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

    rank_icons = ["ðŸ¥‡", "ðŸ¥ˆ", "ðŸ¥‰", "4.", "5."]
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


def format_help_lines(command_specs: list[tuple[str, str]]) -> str:
    return "\n".join(f"`{name}` - {description}" for name, description in command_specs)


def build_help_embed(interaction: discord.Interaction) -> discord.Embed:
    embed = discord.Embed(
        title="Dungeon Keeper Help",
        description="Command guide for this server. Use the examples as templates and change the values.",
        color=discord.Color.blurple(),
    )

    embed.add_field(
        name="General",
        value=format_help_lines(
            [
                ("/help", "Show this guide."),
                ("/xp_leaderboards timescale:week", "View top XP and your rank for a time window."),
            ]
        ),
        inline=False,
    )

    if can_grant_denizen(interaction):
        embed.add_field(
            name="Greeter",
            value=format_help_lines(
                [
                    ("/grant_denizen member:@user", "Give the configured Denizen role to one member."),
                ]
            ),
            inline=False,
        )

    if can_use_xp_grant(interaction):
        embed.add_field(
            name="XP Grant",
            value=format_help_lines(
                [
                    ("/xp_give member:@user", "Give 20 XP manually to one member."),
                ]
            ),
            inline=False,
        )

    if is_mod(interaction):
        embed.add_field(
            name="Moderation",
            value=format_help_lines(
                [
                    ("/listrole role:@Role", "List members who currently have a role."),
                    ("/inactive_role role:@Role days:7", "Show role members inactive in the last N days."),
                ]
            ),
            inline=False,
        )
        embed.add_field(
            name="Configuration",
            value=format_help_lines(
                [
                    ("/set_greeter_role role:@Role", "Choose who can run /grant_denizen."),
                    ("/set_denizen_role role:@Role", "Choose which role /grant_denizen gives."),
                    ("/xp_give_allow member:@user", "Allow a member to run /xp_give."),
                    ("/xp_give_disallow member:@user", "Remove /xp_give access from a member."),
                    ("/xp_give_allowed", "Show current /xp_give allowlist."),
                    ("/xp_set_levelup_log_here", "Run in a channel/thread to receive all level-up posts."),
                    ("/xp_set_level5_log_here", "Run in a channel/thread for level 5 alerts."),
                    ("/auto_delete del_age:30d run:1d", "Delete old posts now and schedule repeats."),
                    ("/auto_delete_configs", "List active auto-delete schedules in this server."),
                    ("/spoiler_guard_add_here", "Enable spoiler guard in this channel/thread."),
                    ("/spoiler_guard_remove_here", "Disable spoiler guard in this channel/thread."),
                    ("/spoiler_guarded_channels", "List channels/threads with spoiler guard enabled."),
                    ("/xp_exclude_here", "Disable XP gain in this channel/thread."),
                    ("/xp_include_here", "Re-enable XP gain in this channel/thread."),
                    ("/xp_excluded_channels", "List channels/threads where XP is off."),
                    ("/xp_backfill_history days:30", "Import historical message XP for the last N days."),
                ]
            ),
            inline=False,
        )
        embed.add_field(
            name="Auto-Delete Notes",
            value=(
                "`del_age` accepts values like `15m`, `2h`, `30d`, `1h30m`.\n"
                "`run` accepts `once`, `off`, or a duration like `30m`, `1h`, `1d`.\n"
                "Recurring runs delete tracked messages posted after the rule is enabled."
            ),
            inline=False,
        )

    embed.set_footer(text="Tip: Discord command prompts show parameter hints while you type.")
    return embed


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
        ("Text", "ðŸ’¬", XP_SOURCE_TEXT, "No text XP yet."),
        ("Replies", "â†©ï¸", XP_SOURCE_REPLY, "No reply XP yet."),
        ("Voice", "ðŸŽ™ï¸", XP_SOURCE_VOICE, "No voice XP yet."),
        ("Image Reacts", "ðŸ–¼ï¸", XP_SOURCE_IMAGE_REACT, "No image react XP yet."),
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
    name="help",
    description="Show command reference and examples."
)
async def help_command(interaction: discord.Interaction):
    await interaction.response.send_message(embed=build_help_embed(interaction), ephemeral=True)


@bot.tree.command(
    name="xp_backfill_history",
    description="Backfill message XP history into the database."
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
    description="Grant the Denizen role to a member."
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
    description="Set the role allowed to run /grant_denizen."
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
    description="Set the role that /grant_denizen assigns."
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
    description="Give a member 20 XP."
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
    description="Allow a user to use /xp_give."
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
    description="Remove a user from /xp_give access."
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
    description="List users allowed to use /xp_give."
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
    description="Send level-up announcements to this channel or thread."
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
    description="Send level 5 XP announcements to this channel or thread."
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
    name="auto_delete",
    description="Delete old posts now and optionally schedule recurring cleanup."
)
@app_commands.describe(
    del_age="Delete posts older than this duration (examples: 30d, 2h, 15m, 1h30m).",
    run="Run once, disable schedule, or set interval (examples: once, off, 1h, 30m, 1d).",
)
async def auto_delete(
    interaction: discord.Interaction,
    del_age: str = "30d",
    run: str = "once",
):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    channel = get_xp_config_target_channel(interaction)
    if channel is None:
        await interaction.response.send_message("This command only works in text channels or threads.", ephemeral=True)
        return

    bot_member = get_bot_member(guild)
    if bot_member is None:
        await interaction.response.send_message("Bot member context is unavailable right now.", ephemeral=True)
        return
    if not channel.permissions_for(bot_member).manage_messages:
        await interaction.response.send_message(
            "I need the Manage Messages permission in this channel to delete posts.",
            ephemeral=True,
        )
        return

    age_seconds = parse_duration_seconds(del_age)
    if age_seconds is None:
        await interaction.response.send_message(
            "Invalid `del_age`. Use durations like `30d`, `2h`, `15m`, or `1h30m`.",
            ephemeral=True,
        )
        return
    if age_seconds < AUTO_DELETE_MIN_AGE_SECONDS:
        await interaction.response.send_message(
            f"`del_age` must be at least {format_duration_seconds(AUTO_DELETE_MIN_AGE_SECONDS)}.",
            ephemeral=True,
        )
        return

    run_token = run.strip().lower()
    schedule_mode = AUTO_DELETE_RUN_KEYWORDS.get(run_token)
    interval_seconds: int | None = None
    if schedule_mode is None:
        interval_seconds = parse_duration_seconds(run_token)
        if interval_seconds is None:
            await interaction.response.send_message(
                "Invalid `run`. Use `once`, `off`, or a duration like `30m`, `1h`, `1d`.",
                ephemeral=True,
            )
            return
        if interval_seconds < AUTO_DELETE_MIN_INTERVAL_SECONDS:
            await interaction.response.send_message(
                f"`run` interval must be at least {format_duration_seconds(AUTO_DELETE_MIN_INTERVAL_SECONDS)}.",
                ephemeral=True,
            )
            return
        schedule_mode = "schedule"

    await interaction.response.defer(ephemeral=True, thinking=True)

    cutoff = discord.utils.utcnow() - timedelta(seconds=age_seconds)
    actor = get_interaction_member(interaction)
    reason = f"Auto-delete requested by {format_user_for_log(actor, interaction.user.id)}"

    try:
        scanned, deleted, skipped_pinned, failed = await delete_messages_older_than(
            channel,
            cutoff,
            reason=reason,
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "I couldn't delete messages in this channel due to missing permissions.",
            ephemeral=True,
        )
        return

    schedule_status = "Recurring cleanup unchanged."
    if schedule_mode == "schedule" and interval_seconds is not None:
        upsert_auto_delete_rule(
            guild.id,
            channel.id,
            age_seconds,
            interval_seconds,
            last_run_ts=time.time(),
        )
        schedule_status = (
            f"Recurring cleanup enabled: every `{format_duration_seconds(interval_seconds)}` "
            f"(age `{format_duration_seconds(age_seconds)}`)."
        )
    elif schedule_mode == "off":
        removed = remove_auto_delete_rule(guild.id, channel.id)
        schedule_status = (
            "Recurring cleanup disabled for this channel."
            if removed
            else "No recurring cleanup rule was set for this channel."
        )

    log.info(
        "Auto-delete run by %s in %s (%s): age=%s deleted=%s scanned=%s pinned=%s failed=%s schedule=%s.",
        format_user_for_log(actor, interaction.user.id),
        channel.mention,
        channel.id,
        age_seconds,
        deleted,
        scanned,
        skipped_pinned,
        failed,
        run_token,
    )

    await interaction.followup.send(
        (
            f"Deleted **{deleted}** messages older than `{format_duration_seconds(age_seconds)}` in {channel.mention}.\n"
            f"Scanned: `{scanned}` | Pinned skipped: `{skipped_pinned}` | Failed: `{failed}`\n"
            f"{schedule_status}"
        ),
        ephemeral=True,
    )


@bot.tree.command(
    name="auto_delete_configs",
    description="List auto-delete schedules configured for this server."
)
async def auto_delete_configs(interaction: discord.Interaction):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    rules = list_auto_delete_rules_for_guild(guild.id)
    if not rules:
        await interaction.response.send_message("No active auto-delete schedules are configured in this server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    lines = [f"**Active Auto-Delete Schedules ({len(rules)})**", ""]
    for index, rule in enumerate(rules, start=1):
        channel_id = int(rule["channel_id"])
        channel = get_guild_channel_or_thread(guild, channel_id)
        channel_label = channel.mention if channel is not None else f"<#{channel_id}> (missing)"

        age_seconds = int(rule["max_age_seconds"])
        interval_seconds = int(rule["interval_seconds"])
        age_label = format_duration_seconds(age_seconds)
        interval_label = format_duration_seconds(interval_seconds)

        last_run_ts = float(rule["last_run_ts"])
        if last_run_ts > 0:
            last_run_display = f"<t:{int(last_run_ts)}:R>"
            next_run_ts = int(last_run_ts + interval_seconds)
            next_run_display = f"<t:{next_run_ts}:R>"
        else:
            last_run_display = "never"
            next_run_display = "as soon as the scheduler runs"

        lines.append(
            f"{index}. {channel_label} | age `{age_label}` | every `{interval_label}`\n"
            f"Last run: {last_run_display} | Next run: {next_run_display}"
        )

    await send_ephemeral_text_chunks(interaction, "\n\n".join(lines))


@bot.tree.command(
    name="spoiler_guard_add_here",
    description="Enable spoiler guard in this channel or thread."
)
async def spoiler_guard_add_here(interaction: discord.Interaction):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    channel = get_xp_config_target_channel(interaction)
    if channel is None:
        await interaction.response.send_message("This command only works in text channels or threads.", ephemeral=True)
        return

    global SPOILER_REQUIRED_CHANNELS
    SPOILER_REQUIRED_CHANNELS = add_config_id_value("spoiler_required_channels", channel.id)
    await interaction.response.send_message(
        f"Spoiler guard enabled for {channel.mention}. Guarded channel IDs: {sorted(SPOILER_REQUIRED_CHANNELS)}",
        ephemeral=True,
    )


@bot.tree.command(
    name="spoiler_guard_remove_here",
    description="Disable spoiler guard in this channel or thread."
)
async def spoiler_guard_remove_here(interaction: discord.Interaction):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    channel = get_xp_config_target_channel(interaction)
    if channel is None:
        await interaction.response.send_message("This command only works in text channels or threads.", ephemeral=True)
        return

    global SPOILER_REQUIRED_CHANNELS
    SPOILER_REQUIRED_CHANNELS = remove_config_id_value("spoiler_required_channels", channel.id)
    await interaction.response.send_message(
        f"Spoiler guard disabled for {channel.mention}. Guarded channel IDs: {sorted(SPOILER_REQUIRED_CHANNELS)}",
        ephemeral=True,
    )


@bot.tree.command(
    name="spoiler_guarded_channels",
    description="List channels and threads where spoiler guard is enabled."
)
async def spoiler_guarded_channels(interaction: discord.Interaction):
    if not is_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    if not SPOILER_REQUIRED_CHANNELS:
        await interaction.response.send_message("Spoiler guard is currently disabled in all channels.", ephemeral=True)
        return

    guild = interaction.guild
    labels = []
    for channel_id in sorted(SPOILER_REQUIRED_CHANNELS):
        channel = get_guild_channel_or_thread(guild, channel_id) if guild else None
        labels.append(channel.mention if channel else f"`{channel_id}`")

    await interaction.response.send_message(
        "Spoiler guard enabled in: " + ", ".join(labels),
        ephemeral=True,
    )


@bot.tree.command(
    name="xp_exclude_here",
    description="Disable XP gain in this channel or thread."
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
    description="Re-enable XP gain in this channel or thread."
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
    description="List channels and threads where XP is currently disabled."
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
    description="Show top 5 XP earners for a selected timescale, plus your standing."
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
            embed.add_field(name="ðŸ’¬ Text", value="No tracked text XP yet.", inline=True)
            embed.add_field(name="â†©ï¸ Replies", value="No tracked reply XP yet.", inline=True)
            embed.add_field(name="ðŸŽ™ï¸ Voice", value="No tracked voice XP yet.", inline=True)
            embed.add_field(name="ðŸ–¼ï¸ Image Reacts", value="No tracked image react XP yet.", inline=True)
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
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set.")
    bot.run(TOKEN, log_handler=None)

