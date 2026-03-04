from __future__ import annotations

import asyncio
import discord
import logging
import os
import sqlite3

from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, TypeAlias


GuildTextLike: TypeAlias = discord.TextChannel | discord.Thread


def parse_id_set(value: str | None) -> set[int]:
    if not value:
        return set()
    parts = [p.strip() for p in value.replace("\n", ",").split(",")]
    return {int(p) for p in parts if p}


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def get_config_value(conn: sqlite3.Connection, key: str, default: str) -> str:
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def get_config_id_set(conn: sqlite3.Connection, bucket: str) -> set[int]:
    rows = conn.execute(
        "SELECT value FROM config_ids WHERE bucket = ? ORDER BY value",
        (bucket,),
    ).fetchall()
    return {int(row["value"]) for row in rows}


def init_config_db(db_path: Path, log: logging.Logger) -> None:
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

    with open_db(db_path) as conn:
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
            existing = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
            if existing and existing["value"] != value:
                log.warning("Config override from env for %s: db=%s env=%s", key, existing["value"], value)
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
                log.warning("Config override from env for %s: db=%s env=%s", bucket, sorted(existing_values), sorted(values))
            conn.execute("DELETE FROM config_ids WHERE bucket = ?", (bucket,))
            conn.executemany(
                "INSERT INTO config_ids (bucket, value) VALUES (?, ?)",
                [(bucket, value) for value in sorted(values)],
            )


def load_runtime_config(db_path: Path) -> dict[str, object]:
    with open_db(db_path) as conn:
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


class Bot(discord.Client):
    def __init__(self, *, intents: discord.Intents, debug: bool, guild_id: int):
        super().__init__(intents=intents)
        self.tree = discord.app_commands.CommandTree(self)
        self.debug = debug
        self.guild_id = guild_id
        self.startup_task_factories: list[Callable[[], Awaitable[None]]] = []
        self.startup_tasks: list[asyncio.Task] = []

    async def setup_hook(self) -> None:
        if self.debug:
            guild = discord.Object(id=self.guild_id)
            await self.tree.sync(guild=guild)
            print("Synced commands to development guild.")
        else:
            await self.tree.sync()
            print("Synced commands globally.")

        for factory in self.startup_task_factories:
            self.startup_tasks.append(asyncio.create_task(factory()))


@dataclass
class AppContext:
    bot: Bot
    log: logging.Logger
    client: object
    db_path: Path
    guild_id: int
    debug: bool
    model: str
    bigmodel: str
    xp_settings: object
    mod_channel_id: int
    spoiler_required_channels: set[int]
    bypass_role_ids: set[int]
    xp_grant_allowed_user_ids: set[int]
    xp_excluded_channel_ids: set[int]
    level_5_role_id: int
    level_5_log_channel_id: int
    level_up_log_channel_id: int
    greeter_role_id: int
    denizen_role_id: int
    xp_pair_states: dict[int, object] = field(default_factory=dict)

    def open_db(self) -> sqlite3.Connection:
        return open_db(self.db_path)

    def add_config_id_value(self, bucket: str, value: int) -> set[int]:
        with self.open_db() as conn:
            conn.execute("INSERT OR IGNORE INTO config_ids (bucket, value) VALUES (?, ?)", (bucket, value))
            return get_config_id_set(conn, bucket)

    def remove_config_id_value(self, bucket: str, value: int) -> set[int]:
        with self.open_db() as conn:
            conn.execute("DELETE FROM config_ids WHERE bucket = ? AND value = ?", (bucket, value))
            return get_config_id_set(conn, bucket)

    def set_config_value(self, key: str, value: str) -> str:
        with self.open_db() as conn:
            conn.execute(
                """
                INSERT INTO config (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            return get_config_value(conn, key, value)

    def get_interaction_member(self, interaction: discord.Interaction) -> discord.Member | None:
        user = interaction.user
        if isinstance(user, discord.Member):
            return user
        if interaction.guild is None:
            return None
        return interaction.guild.get_member(user.id)

    def get_bot_member(self, guild: discord.Guild) -> discord.Member | None:
        if guild.me is not None:
            return guild.me
        bot_user = guild.client.user
        if bot_user is None:
            return None
        return guild.get_member(bot_user.id)

    def get_guild_channel_or_thread(self, guild: discord.Guild, channel_id: int) -> GuildTextLike | None:
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

    def get_xp_config_target_channel(self, interaction: discord.Interaction) -> GuildTextLike | None:
        channel = interaction.channel
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        return None

    def is_mod(self, interaction: discord.Interaction) -> bool:
        member = self.get_interaction_member(interaction)
        if member is None:
            return False
        perms = member.guild_permissions
        return perms.manage_guild or perms.administrator
