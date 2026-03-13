from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine, TypeAlias, TypedDict

import discord

from db_utils import get_config_id_set, get_config_value, open_db, parse_bool

GuildTextLike: TypeAlias = discord.TextChannel | discord.Thread


def _parse_int_config(raw_value: str, *, key: str, default: int = 0) -> int:
    """Parse integer config values safely with fallback."""
    normalized = raw_value.strip()
    if not normalized:
        return default
    try:
        return int(normalized)
    except ValueError:
        logging.warning("Invalid integer config for %s: %r; using %s.", key, raw_value, default)
        return default


class RuntimeConfig(TypedDict):
    guild_id: int
    mod_channel_id: int
    debug: bool
    xp_level_5_role_id: int
    xp_level_5_log_channel_id: int
    xp_level_up_log_channel_id: int
    greeter_role_id: int
    denizen_role_id: int
    denizen_log_channel_id: int
    denizen_grant_message: str
    nsfw_role_id: int
    nsfw_log_channel_id: int
    nsfw_grant_message: str
    veteran_role_id: int
    veteran_log_channel_id: int
    veteran_grant_message: str
    spoiler_required_channels: set[int]
    bypass_role_ids: set[int]
    xp_grant_allowed_user_ids: set[int]
    xp_excluded_channel_ids: set[int]
    welcome_channel_id: int
    welcome_message: str
    leave_channel_id: int
    leave_message: str


def load_runtime_config(db_path: Path) -> RuntimeConfig:
    from services.welcome_service import DEFAULT_LEAVE_MESSAGE, DEFAULT_WELCOME_MESSAGE

    with open_db(db_path) as conn:
        guild_id = _parse_int_config(get_config_value(conn, "guild_id", "0"), key="guild_id")
        if guild_id == 0:
            guild_id = _parse_int_config(os.environ.get("GUILD_ID", "0"), key="GUILD_ID")

        db_debug = get_config_value(conn, "debug", "")
        if db_debug:
            debug = parse_bool(db_debug, default=True)
        else:
            debug = parse_bool(os.environ.get("DEBUG", "1"), default=True)

        return {
            "guild_id": guild_id,
            "debug": debug,
            "mod_channel_id": _parse_int_config(get_config_value(conn, "mod_channel_id", "0"),
                                                key="mod_channel_id"),
            "xp_level_5_role_id": _parse_int_config(get_config_value(conn, "xp_level_5_role_id", "0"),
                                                    key="xp_level_5_role_id"),
            "xp_level_5_log_channel_id": _parse_int_config(
                get_config_value(conn, "xp_level_5_log_channel_id", "0"), key="xp_level_5_log_channel_id"
            ),
            "xp_level_up_log_channel_id": _parse_int_config(
                get_config_value(conn, "xp_level_up_log_channel_id", "0"), key="xp_level_up_log_channel_id"
            ),
            "greeter_role_id": _parse_int_config(get_config_value(conn, "greeter_role_id", "0"),
                                                 key="greeter_role_id"),
            "denizen_role_id": _parse_int_config(get_config_value(conn, "denizen_role_id", "0"),
                                                 key="denizen_role_id"),
            "denizen_log_channel_id": _parse_int_config(
                get_config_value(conn, "denizen_log_channel_id", "0"), key="denizen_log_channel_id"
            ),
            "denizen_grant_message": get_config_value(conn, "denizen_grant_message", ""),
            "nsfw_role_id": _parse_int_config(get_config_value(conn, "nsfw_role_id", "0"), key="nsfw_role_id"),
            "nsfw_log_channel_id": _parse_int_config(
                get_config_value(conn, "nsfw_log_channel_id", "0"), key="nsfw_log_channel_id"
            ),
            "nsfw_grant_message": get_config_value(conn, "nsfw_grant_message", ""),
            "veteran_role_id": _parse_int_config(get_config_value(conn, "veteran_role_id", "0"), key="veteran_role_id"),
            "veteran_log_channel_id": _parse_int_config(
                get_config_value(conn, "veteran_log_channel_id", "0"), key="veteran_log_channel_id"
            ),
            "veteran_grant_message": get_config_value(conn, "veteran_grant_message", ""),
            "spoiler_required_channels": get_config_id_set(conn, "spoiler_required_channels"),
            "bypass_role_ids": get_config_id_set(conn, "bypass_role_ids"),
            "xp_grant_allowed_user_ids": get_config_id_set(conn, "xp_grant_allowed_user_ids"),
            "xp_excluded_channel_ids": get_config_id_set(conn, "xp_excluded_channel_ids"),
            "welcome_channel_id": _parse_int_config(
                get_config_value(conn, "welcome_channel_id", "0"), key="welcome_channel_id"
            ),
            "welcome_message": get_config_value(conn, "welcome_message", DEFAULT_WELCOME_MESSAGE),
            "leave_channel_id": _parse_int_config(
                get_config_value(conn, "leave_channel_id", "0"), key="leave_channel_id"
            ),
            "leave_message": get_config_value(conn, "leave_message", DEFAULT_LEAVE_MESSAGE),
        }


class Bot(discord.Client):
    def __init__(self, *, intents: discord.Intents, debug: bool, guild_id: int):
        super().__init__(intents=intents)
        self.tree = discord.app_commands.CommandTree(self)
        self.debug = debug
        self.guild_id = _parse_int_config(str(guild_id), key="guild_id")
        self.startup_task_factories: list[Callable[[], Coroutine[Any, Any, None]]] = []
        self.startup_tasks: list[asyncio.Task[None]] = []

    async def setup_hook(self) -> None:
        if self.debug:
            if self.guild_id <= 0:
                print("WARNING: debug=True but guild_id is not configured; skipping guild command sync.")
            else:
                guild = discord.Object(id=self.guild_id)
                try:
                    self.tree.copy_global_to(guild=guild)
                    synced = await self.tree.sync(guild=guild)
                    print(f"Synced {len(synced)} commands to development guild {self.guild_id}.")
                    # Clear any stale global commands so they don't appear alongside guild commands.
                    await self.tree.sync()
                    print("Cleared global commands (debug mode).")
                except discord.Forbidden as exc:
                    print(
                        "WARNING: missing access while syncing commands to "
                        f"guild {self.guild_id}: {exc}. Ensure the bot is in this guild and has applications.commands "
                        f"scope."
                    )
        else:
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} commands globally.")

        for factory in self.startup_task_factories:
            self.startup_tasks.append(asyncio.create_task(factory()))


@dataclass
class AppContext:
    bot: Bot
    log: logging.Logger
    db_path: Path
    guild_id: int
    debug: bool
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
    denizen_log_channel_id: int
    denizen_grant_message: str
    nsfw_role_id: int
    nsfw_log_channel_id: int
    nsfw_grant_message: str
    veteran_role_id: int
    veteran_log_channel_id: int
    veteran_grant_message: str
    welcome_channel_id: int
    welcome_message: str
    leave_channel_id: int
    leave_message: str
    xp_pair_states: dict[int, Any] = field(default_factory=dict)
    watched_users: dict[int, set[int]] = field(default_factory=dict)

    def open_db(self) -> sqlite3.Connection:
        return open_db(self.db_path)

    def add_config_id_value(self, bucket: str, value: int) -> set[int]:
        with self.open_db() as conn:
            conn.execute("INSERT OR IGNORE INTO config_ids (bucket, value) VALUES (?, ?)", (bucket,
                                                                                            value))
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

    def can_grant_denizen(self, interaction: discord.Interaction) -> bool:
        if self.is_mod(interaction):
            return True
        member = self.get_interaction_member(interaction)
        if member is None or self.greeter_role_id <= 0:
            return False
        return any(role.id == self.greeter_role_id for role in member.roles)

    def can_use_xp_grant(self, interaction: discord.Interaction) -> bool:
        if self.is_mod(interaction):
            return True
        return interaction.user.id in self.xp_grant_allowed_user_ids

    def get_member_last_activity_map(self, conn, guild_id: int, user_ids: list[int]):
        from xp_system import get_member_last_activity_map
        return get_member_last_activity_map(conn, guild_id, user_ids)
