from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine, TypeAlias, TypedDict

import discord

from db_utils import get_config_id_set, get_config_value, open_db, parse_bool

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


def load_runtime_config(db_path: Path) -> RuntimeConfig:
    with open_db(db_path) as conn:
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


class Bot(discord.Client):
    def __init__(self, *, intents: discord.Intents, debug: bool, guild_id: int):
        super().__init__(intents=intents)
        self.tree = discord.app_commands.CommandTree(self)
        self.debug = debug
        self.guild_id = guild_id
        self.startup_task_factories: list[Callable[[], Coroutine[Any, Any, None]]] = []
        self.startup_tasks: list[asyncio.Task[None]] = []

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
    xp_pair_states: dict[int, Any] = field(default_factory=dict)

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
