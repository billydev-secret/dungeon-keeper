from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine, TypeAlias, TypedDict

import discord

from db_utils import (
    GrantRoleConfig,
    can_use_grant,
    get_config_id_set,
    get_config_value,
    get_grant_roles,
    open_db,
    parse_bool,
)

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


def _parse_float_config(raw_value: str, *, key: str, default: float = 0.0) -> float:
    """Parse float config values safely with fallback."""
    normalized = raw_value.strip()
    if not normalized:
        return default
    try:
        return float(normalized)
    except ValueError:
        logging.warning("Invalid float config for %s: %r; using %s.", key, raw_value, default)
        return default


class RuntimeConfig(TypedDict):
    guild_id: int
    mod_channel_id: int
    debug: bool
    xp_level_5_role_id: int
    xp_level_5_log_channel_id: int
    xp_level_up_log_channel_id: int
    greeter_role_id: int
    greeter_chat_channel_id: int
    spoiler_required_channels: set[int]
    bypass_role_ids: set[int]
    xp_grant_allowed_user_ids: set[int]
    xp_excluded_channel_ids: set[int]
    welcome_channel_id: int
    welcome_message: str
    welcome_ping_role_id: int
    leave_channel_id: int
    leave_message: str
    tz_offset_hours: float


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
            "greeter_chat_channel_id": _parse_int_config(
                get_config_value(conn, "greeter_chat_channel_id", "0"), key="greeter_chat_channel_id"
            ),
            "spoiler_required_channels": get_config_id_set(conn, "spoiler_required_channels"),
            "bypass_role_ids": get_config_id_set(conn, "bypass_role_ids"),
            "xp_grant_allowed_user_ids": get_config_id_set(conn, "xp_grant_allowed_user_ids"),
            "xp_excluded_channel_ids": get_config_id_set(conn, "xp_excluded_channel_ids"),
            "welcome_channel_id": _parse_int_config(
                get_config_value(conn, "welcome_channel_id", "0"), key="welcome_channel_id"
            ),
            "welcome_message": get_config_value(conn, "welcome_message", DEFAULT_WELCOME_MESSAGE),
            "welcome_ping_role_id": _parse_int_config(
                get_config_value(conn, "welcome_ping_role_id", "0"), key="welcome_ping_role_id"
            ),
            "leave_channel_id": _parse_int_config(
                get_config_value(conn, "leave_channel_id", "0"), key="leave_channel_id"
            ),
            "leave_message": get_config_value(conn, "leave_message", DEFAULT_LEAVE_MESSAGE),
            "tz_offset_hours": _parse_float_config(
                get_config_value(conn, "tz_offset_hours", "0"), key="tz_offset_hours"
            ),
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
                    self.tree.clear_commands(guild=None)
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
            # Clear any stale guild commands left from a previous debug-mode run.
            if self.guild_id > 0:
                try:
                    guild = discord.Object(id=self.guild_id)
                    self.tree.clear_commands(guild=guild)
                    await self.tree.sync(guild=guild)
                    print(f"Cleared stale guild commands for {self.guild_id}.")
                except discord.HTTPException as exc:
                    print(f"WARNING: could not clear guild commands for {self.guild_id}: {exc}")

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
    greeter_chat_channel_id: int
    welcome_channel_id: int
    welcome_message: str
    welcome_ping_role_id: int
    leave_channel_id: int
    leave_message: str
    tz_offset_hours: float = 0.0
    grant_roles: dict[str, GrantRoleConfig] = field(default_factory=dict)
    xp_pair_states: dict[int, Any] = field(default_factory=dict)
    watched_users: dict[int, set[int]] = field(default_factory=dict)

    def open_db(self) -> contextlib.AbstractContextManager[sqlite3.Connection]:
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
        if perms.manage_guild or perms.administrator:
            return True
        with self.open_db() as conn:
            mod_raw = get_config_value(conn, "mod_role_ids", "")
            admin_raw = get_config_value(conn, "admin_role_ids", "")
        configured = {
            int(x)
            for x in f"{mod_raw},{admin_raw}".split(",")
            if x.strip().isdigit()
        }
        return bool(configured & {r.id for r in member.roles})

    def is_admin(self, interaction: discord.Interaction) -> bool:
        member = self.get_interaction_member(interaction)
        if member is None:
            return False
        if member.guild_permissions.administrator:
            return True
        with self.open_db() as conn:
            raw = get_config_value(conn, "admin_role_ids", "")
        configured = {int(x) for x in raw.split(",") if x.strip().isdigit()}
        return bool(configured & {r.id for r in member.roles})

    def reload_grant_roles(self) -> None:
        with self.open_db() as conn:
            self.grant_roles = get_grant_roles(conn, self.guild_id)

    def can_grant_denizen(self, interaction: discord.Interaction) -> bool:
        """Legacy check — returns True if user can grant ANY grant role."""
        if self.is_mod(interaction):
            return True
        member = self.get_interaction_member(interaction)
        if member is None:
            return False
        role_ids = [r.id for r in member.roles]
        with self.open_db() as conn:
            for grant_name in self.grant_roles:
                if can_use_grant(conn, self.guild_id, grant_name, member.id, role_ids):
                    return True
        return False

    def can_use_grant_role(self, interaction: discord.Interaction, grant_name: str) -> bool:
        if self.is_mod(interaction):
            return True
        member = self.get_interaction_member(interaction)
        if member is None:
            return False
        role_ids = [r.id for r in member.roles]
        with self.open_db() as conn:
            return can_use_grant(conn, self.guild_id, grant_name, member.id, role_ids)

    def can_use_xp_grant(self, interaction: discord.Interaction) -> bool:
        if self.is_mod(interaction):
            return True
        return interaction.user.id in self.xp_grant_allowed_user_ids

    def get_member_last_activity_map(self, conn, guild_id: int, user_ids: list[int]):
        from xp_system import get_member_last_activity_map
        return get_member_last_activity_map(conn, guild_id, user_ids)
