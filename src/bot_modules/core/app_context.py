from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeAlias, TypedDict
from collections.abc import Callable, Coroutine

import discord
from discord.ext import commands

from bot_modules.core.db_utils import (
    GrantRoleConfig,
    add_config_id,
    can_use_grant,
    clear_config_id_bucket,
    delete_config_value,
    get_config_id_set,
    get_config_value,
    get_grant_roles,
    open_db,
    parse_bool,
    remove_config_id,
    set_config_value as _db_set_config_value,
)
from bot_modules.core.xp_system import DEFAULT_XP_SETTINGS, XpSettings, load_xp_settings

GuildTextLike: TypeAlias = discord.TextChannel | discord.Thread


def _parse_int_config(raw_value: str, *, key: str, default: int = 0) -> int:
    """Parse integer config values safely with fallback."""
    normalized = raw_value.strip()
    if not normalized:
        return default
    try:
        return int(normalized)
    except ValueError:
        logging.warning(
            "Invalid integer config for %s: %r; using %s.", key, raw_value, default
        )
        return default


def _parse_float_config(raw_value: str, *, key: str, default: float = 0.0) -> float:
    """Parse float config values safely with fallback."""
    normalized = raw_value.strip()
    if not normalized:
        return default
    try:
        return float(normalized)
    except ValueError:
        logging.warning(
            "Invalid float config for %s: %r; using %s.", key, raw_value, default
        )
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
    join_leave_log_channel_id: int
    spoiler_required_channels: set[int]
    bypass_role_ids: set[int]
    xp_grant_allowed_user_ids: set[int]
    xp_excluded_channel_ids: set[int]
    recorded_bot_user_ids: set[int]
    welcome_channel_id: int
    welcome_message: str
    welcome_ping_role_id: int
    leave_channel_id: int
    leave_message: str
    tz_offset_hours: float


def load_runtime_config(db_path: Path, *, debug: bool, default_guild_id: int = 0) -> RuntimeConfig:
    from bot_modules.services.welcome_service import DEFAULT_LEAVE_MESSAGE, DEFAULT_WELCOME_MESSAGE

    with open_db(db_path) as conn:
        guild_id = _parse_int_config(
            get_config_value(conn, "guild_id", "0"), key="guild_id"
        )
        if guild_id == 0:
            guild_id = default_guild_id or _parse_int_config(
                os.environ.get("GUILD_ID", "0"), key="GUILD_ID"
            )
        leave_channel_id = _parse_int_config(
            get_config_value(conn, "leave_channel_id", "0"), key="leave_channel_id"
        )

        return {
            "guild_id": guild_id,
            "debug": debug,
            "mod_channel_id": _parse_int_config(
                get_config_value(conn, "mod_channel_id", "0"), key="mod_channel_id"
            ),
            "xp_level_5_role_id": _parse_int_config(
                get_config_value(conn, "xp_level_5_role_id", "0"),
                key="xp_level_5_role_id",
            ),
            "xp_level_5_log_channel_id": _parse_int_config(
                get_config_value(conn, "xp_level_5_log_channel_id", "0"),
                key="xp_level_5_log_channel_id",
            ),
            "xp_level_up_log_channel_id": _parse_int_config(
                get_config_value(conn, "xp_level_up_log_channel_id", "0"),
                key="xp_level_up_log_channel_id",
            ),
            "greeter_role_id": _parse_int_config(
                get_config_value(conn, "greeter_role_id", "0"), key="greeter_role_id"
            ),
            "greeter_chat_channel_id": _parse_int_config(
                get_config_value(conn, "greeter_chat_channel_id", "0"),
                key="greeter_chat_channel_id",
            ),
            "join_leave_log_channel_id": _parse_int_config(
                get_config_value(
                    conn,
                    "join_leave_log_channel_id",
                    str(leave_channel_id),
                ),
                key="join_leave_log_channel_id",
                default=leave_channel_id,
            ),
            "spoiler_required_channels": get_config_id_set(
                conn, "spoiler_required_channels"
            ),
            "bypass_role_ids": get_config_id_set(conn, "bypass_role_ids"),
            "xp_grant_allowed_user_ids": get_config_id_set(
                conn, "xp_grant_allowed_user_ids"
            ),
            "xp_excluded_channel_ids": get_config_id_set(
                conn, "xp_excluded_channel_ids"
            ),
            "recorded_bot_user_ids": get_config_id_set(
                conn, "recorded_bot_user_ids"
            ),
            "welcome_channel_id": _parse_int_config(
                get_config_value(conn, "welcome_channel_id", "0"),
                key="welcome_channel_id",
            ),
            "welcome_message": get_config_value(
                conn, "welcome_message", DEFAULT_WELCOME_MESSAGE
            ),
            "welcome_ping_role_id": _parse_int_config(
                get_config_value(conn, "welcome_ping_role_id", "0"),
                key="welcome_ping_role_id",
            ),
            "leave_channel_id": leave_channel_id,
            "leave_message": get_config_value(
                conn, "leave_message", DEFAULT_LEAVE_MESSAGE
            ),
            "tz_offset_hours": _parse_float_config(
                get_config_value(conn, "tz_offset_hours", "0"), key="tz_offset_hours"
            ),
        }


class Bot(commands.Bot):
    ctx: AppContext  # set by the entry point before bot.run()

    def __init__(self, *, intents: discord.Intents, debug: bool, guild_id: int | str):
        super().__init__(intents=intents, command_prefix=commands.when_mentioned)
        self.debug = debug
        self.guild_id = _parse_int_config(str(guild_id), key="guild_id")
        self.startup_task_factories: list[Callable[[], Coroutine[Any, Any, None]]] = []
        self.startup_tasks: list[asyncio.Task[None]] = []
        self.extension_names: list[str] = []

    async def setup_hook(self) -> None:
        from bot_modules.services.command_sync import sync_if_changed

        for ext in self.extension_names:
            await self.load_extension(ext)

        if self.debug:
            if self.guild_id <= 0:
                print(
                    "WARNING: debug=True but guild_id is not configured; skipping guild command sync."
                )
            else:
                db_path = self.ctx.db_path
                guild = discord.Object(id=self.guild_id)
                try:
                    self.tree.copy_global_to(guild=guild)
                    synced, did = await sync_if_changed(
                        self.tree, db_path, guild=guild
                    )
                    if did:
                        print(
                            f"Synced {len(synced)} commands to development guild {self.guild_id}."
                        )
                    else:
                        print(
                            f"Command tree unchanged for guild {self.guild_id} — skipping sync."
                        )
                    # Clear any stale global commands so they don't appear alongside guild commands.
                    self.tree.clear_commands(guild=None)
                    _, did_global = await sync_if_changed(
                        self.tree, db_path, guild=None
                    )
                    if did_global:
                        print("Cleared global commands (debug mode).")
                except discord.Forbidden as exc:
                    print(
                        "WARNING: missing access while syncing commands to "
                        f"guild {self.guild_id}: {exc}. Ensure the bot is in this guild and has applications.commands "
                        f"scope."
                    )
        else:
            db_path = self.ctx.db_path
            synced, did = await sync_if_changed(self.tree, db_path, guild=None)
            if did:
                print(f"Synced {len(synced)} commands globally.")
            else:
                print("Command tree unchanged — skipping global sync.")
            # Clear any stale guild commands left from a previous debug-mode run.
            if self.guild_id > 0:
                try:
                    guild = discord.Object(id=self.guild_id)
                    self.tree.clear_commands(guild=guild)
                    _, did_guild = await sync_if_changed(
                        self.tree, db_path, guild=guild
                    )
                    if did_guild:
                        print(f"Cleared stale guild commands for {self.guild_id}.")
                except discord.HTTPException as exc:
                    print(
                        f"WARNING: could not clear guild commands for {self.guild_id}: {exc}"
                    )

        for factory in self.startup_task_factories:
            self.startup_tasks.append(asyncio.create_task(
                self._resilient_task(factory)
            ))

    @staticmethod
    async def _resilient_task(
        factory: Callable[[], Coroutine[Any, Any, None]],
        restart_delay: float = 30.0,
        max_restarts: int = 10,
    ) -> None:
        """Run a background task with automatic restart on failure.

        Respects CancelledError (shutdown) but restarts on other exceptions,
        with a delay between restarts and a cap on total restarts.
        """
        _log = logging.getLogger("dungeonkeeper.tasks")
        name = getattr(factory, "__name__", repr(factory))
        restarts = 0

        while restarts <= max_restarts:
            try:
                await factory()
                return  # Exited cleanly (one-shot task like backfill)
            except asyncio.CancelledError:
                raise  # Propagate — shutdown is happening
            except Exception:
                restarts += 1
                _log.exception(
                    "Background task %s crashed (restart %d/%d) — "
                    "restarting in %.0fs",
                    name, restarts, max_restarts, restart_delay,
                )
                if restarts > max_restarts:
                    _log.error(
                        "Background task %s exceeded max restarts — giving up",
                        name,
                    )
                    return
                await asyncio.sleep(restart_delay)


def _parse_id_csv(raw: str) -> frozenset[int]:
    """Parse a comma-separated string of integer IDs into a frozenset."""
    return frozenset(int(x) for x in raw.split(",") if x.strip().isdigit())


@dataclass(frozen=True)
class GuildConfig:
    """Immutable snapshot of one guild's config (core-slice fields).

    Built lazily and cached per guild on :class:`AppContext` via
    ``ctx.guild_config(guild_id)``. For the home guild, reads its own
    ``guild_id`` with the legacy ``guild_id=0`` fallback; for other guilds,
    reads strictly (``allow_legacy_fallback=False``) so an unconfigured guild
    gets real defaults instead of silently inheriting the home/legacy values.
    """

    guild_id: int
    # welcome / leave
    welcome_channel_id: int
    welcome_message: str
    welcome_ping_role_id: int
    welcome_ping_member: bool
    welcome_trigger: str
    unverified_role_id: int
    greeter_chat_channel_id: int
    leave_channel_id: int
    leave_message: str
    join_leave_log_channel_id: int
    # moderation / permissions
    mod_channel_id: int
    mod_role_ids: frozenset[int]
    admin_role_ids: frozenset[int]
    # message archival / spoiler enforcement
    spoiler_required_channels: frozenset[int]
    bypass_role_ids: frozenset[int]
    recorded_bot_user_ids: frozenset[int]
    # message-content storage level: "none" (default — keep derivations only)
    # or "all" (full content archive). See bot_modules.services.message_store.
    message_storage_level: str
    # XP / leveling
    xp_excluded_channel_ids: frozenset[int]
    xp_grant_allowed_user_ids: frozenset[int]
    level_5_role_id: int
    level_5_log_channel_id: int
    level_up_log_channel_id: int
    xp_settings: XpSettings
    # role-grant definitions (member self-assignable roles)
    grant_roles: dict[str, GrantRoleConfig]
    # roles automatically applied to every new member on join
    auto_role_ids: frozenset[int]

    @classmethod
    def load(
        cls,
        conn: sqlite3.Connection,
        guild_id: int,
        *,
        allow_legacy_fallback: bool,
    ) -> "GuildConfig":
        from bot_modules.services.welcome_service import (
            DEFAULT_LEAVE_MESSAGE,
            DEFAULT_WELCOME_MESSAGE,
        )

        def _val(key: str, default: str = "") -> str:
            return get_config_value(
                conn,
                key,
                default,
                guild_id,
                allow_legacy_fallback=allow_legacy_fallback,
            )

        def _int(key: str, default: int = 0) -> int:
            return _parse_int_config(_val(key, str(default)), key=key, default=default)

        def _ids(bucket: str) -> frozenset[int]:
            return frozenset(
                get_config_id_set(
                    conn,
                    bucket,
                    guild_id,
                    allow_legacy_fallback=allow_legacy_fallback,
                )
            )

        leave_channel_id = _int("leave_channel_id")
        return cls(
            guild_id=guild_id,
            welcome_channel_id=_int("welcome_channel_id"),
            welcome_message=_val("welcome_message", DEFAULT_WELCOME_MESSAGE),
            welcome_ping_role_id=_int("welcome_ping_role_id"),
            welcome_ping_member=parse_bool(_val("welcome_ping_member", "false")),
            welcome_trigger=_val("welcome_trigger", "join"),
            unverified_role_id=_int("unverified_role_id"),
            greeter_chat_channel_id=_int("greeter_chat_channel_id"),
            leave_channel_id=leave_channel_id,
            leave_message=_val("leave_message", DEFAULT_LEAVE_MESSAGE),
            join_leave_log_channel_id=_parse_int_config(
                _val("join_leave_log_channel_id", str(leave_channel_id)),
                key="join_leave_log_channel_id",
                default=leave_channel_id,
            ),
            mod_channel_id=_int("mod_channel_id"),
            mod_role_ids=_parse_id_csv(_val("mod_role_ids")),
            admin_role_ids=_parse_id_csv(_val("admin_role_ids")),
            spoiler_required_channels=_ids("spoiler_required_channels"),
            bypass_role_ids=_ids("bypass_role_ids"),
            recorded_bot_user_ids=_ids("recorded_bot_user_ids"),
            message_storage_level=_val("message_storage_level", "none"),
            xp_excluded_channel_ids=_ids("xp_excluded_channel_ids"),
            xp_grant_allowed_user_ids=_ids("xp_grant_allowed_user_ids"),
            level_5_role_id=_int("xp_level_5_role_id"),
            level_5_log_channel_id=_int("xp_level_5_log_channel_id"),
            level_up_log_channel_id=_int("xp_level_up_log_channel_id"),
            # XP coefficients keep the legacy guild_id=0 fallback intentionally:
            # they are global algorithm tuning, not guild identity, and home's
            # coeffs live under home's real id (guild_id=0 holds only ai/tree-hash
            # rows), so a second guild falls back to DEFAULT_XP_SETTINGS, never
            # home's tuning. Do not "fix" this to strict.
            xp_settings=load_xp_settings(conn, guild_id),
            grant_roles=get_grant_roles(conn, guild_id),
            auto_role_ids=_ids("auto_role_ids"),
        )

    def member_is_mod(self, member: discord.Member) -> bool:
        """True if the member holds a configured mod or admin role.

        The Discord ``manage_guild``/``administrator`` short-circuit is the
        caller's responsibility (see ``AppContext.is_mod``).
        """
        configured = self.mod_role_ids | self.admin_role_ids
        return bool(configured & {r.id for r in member.roles})

    def member_is_admin(self, member: discord.Member) -> bool:
        """True if the member holds a configured admin role."""
        return bool(self.admin_role_ids & {r.id for r in member.roles})

    @property
    def retains_content(self) -> bool:
        """True if this guild archives raw message content (storage level "all").

        The single home for the level→retain predicate; ingest call sites read
        this instead of comparing the level string themselves.
        """
        return self.message_storage_level == "all"


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
    recorded_bot_user_ids: set[int]
    level_5_role_id: int
    level_5_log_channel_id: int
    level_up_log_channel_id: int
    greeter_role_id: int
    greeter_chat_channel_id: int
    join_leave_log_channel_id: int
    welcome_channel_id: int
    welcome_message: str
    welcome_ping_role_id: int
    leave_channel_id: int
    leave_message: str
    tz_offset_hours: float = 0.0
    xp_settings: XpSettings = field(default_factory=lambda: DEFAULT_XP_SETTINGS)
    grant_roles: dict[str, GrantRoleConfig] = field(default_factory=dict)
    xp_pair_states: dict[int, Any] = field(default_factory=dict)
    watched_users: dict[int, set[int]] = field(default_factory=dict)
    mod_role_ids: set[int] = field(default_factory=set)
    admin_role_ids: set[int] = field(default_factory=set)
    _guild_config_cache: dict[int, GuildConfig] = field(default_factory=dict)

    def open_db(self) -> contextlib.AbstractContextManager[sqlite3.Connection]:
        return open_db(self.db_path)

    def guild_config(self, guild_id: int) -> GuildConfig:
        """Return the cached per-guild config snapshot, loading it on first use.

        The home guild reads with the legacy ``guild_id=0`` fallback; other
        guilds read strictly so an unconfigured guild gets real defaults.
        """
        cfg = self._guild_config_cache.get(guild_id)
        if cfg is None:
            with self.open_db() as conn:
                cfg = GuildConfig.load(
                    conn,
                    guild_id,
                    allow_legacy_fallback=(guild_id == self.guild_id),
                )
            self._guild_config_cache[guild_id] = cfg
        return cfg

    def invalidate_guild_config(self, guild_id: int) -> None:
        """Drop the cached snapshot for a guild so the next read reloads it.

        Call after any config write so runtime readers pick up the change.
        """
        self._guild_config_cache.pop(guild_id, None)

    def reload_xp_settings(self) -> None:
        """Reload XP algorithm coefficients from the config DB."""
        with self.open_db() as conn:
            self.xp_settings = load_xp_settings(conn, self.guild_id)

    def reload_permission_roles(self) -> None:
        """Reload the home guild's mod/admin role ID caches from the config DB.

        Scoped to ``self.guild_id`` (with legacy ``guild_id=0`` fallback) so the
        flat caches agree with ``guild_config(self.guild_id)``; other guilds are
        resolved per-event via ``guild_config``.
        """
        with self.open_db() as conn:
            mod_raw = get_config_value(conn, "mod_role_ids", "", self.guild_id)
            admin_raw = get_config_value(conn, "admin_role_ids", "", self.guild_id)
        self.mod_role_ids = {int(x) for x in mod_raw.split(",") if x.strip().isdigit()}
        self.admin_role_ids = {int(x) for x in admin_raw.split(",") if x.strip().isdigit()}

    def add_config_id_value(self, bucket: str, value: int) -> set[int]:
        with self.open_db() as conn:
            add_config_id(conn, bucket, value, self.guild_id)
            result = get_config_id_set(conn, bucket, self.guild_id)
        # Always the home guild — refresh the per-guild snapshot so guild_config()
        # readers (spoiler/bypass/xp-excluded/…) see the change without a restart.
        self.invalidate_guild_config(self.guild_id)
        return result

    def remove_config_id_value(self, bucket: str, value: int) -> set[int]:
        with self.open_db() as conn:
            remove_config_id(conn, bucket, value, self.guild_id)
            result = get_config_id_set(conn, bucket, self.guild_id)
        self.invalidate_guild_config(self.guild_id)
        return result

    def clear_config_id_bucket(self, bucket: str) -> None:
        with self.open_db() as conn:
            clear_config_id_bucket(conn, bucket, self.guild_id)
        self.invalidate_guild_config(self.guild_id)

    def set_config_value(
        self, key: str, value: str, guild_id: int | None = None
    ) -> str:
        """Write a config value for ``guild_id`` (defaults to the home guild).

        Keeps the home flat caches consistent and invalidates the per-guild
        snapshot so ``guild_config()`` readers see the write.
        """
        gid = self.guild_id if guild_id is None else guild_id
        with self.open_db() as conn:
            _db_set_config_value(conn, key, value, gid)
            result = get_config_value(conn, key, value, gid)
        if gid == self.guild_id:
            if key == "mod_role_ids":
                self.mod_role_ids = {int(x) for x in result.split(",") if x.strip().isdigit()}
            elif key == "admin_role_ids":
                self.admin_role_ids = {int(x) for x in result.split(",") if x.strip().isdigit()}
        self.invalidate_guild_config(gid)
        return result

    def delete_config_value(self, key: str) -> None:
        with self.open_db() as conn:
            delete_config_value(conn, key, self.guild_id)
        if key == "mod_role_ids":
            self.mod_role_ids = set()
        elif key == "admin_role_ids":
            self.admin_role_ids = set()
        self.invalidate_guild_config(self.guild_id)

    def get_interaction_member(
        self, interaction: discord.Interaction
    ) -> discord.Member | None:
        from bot_modules.core.utils import get_interaction_member as _impl

        return _impl(interaction)

    def get_bot_member(self, guild: discord.Guild) -> discord.Member | None:
        return guild.me

    def get_guild_channel_or_thread(
        self, guild: discord.Guild, channel_id: int
    ) -> GuildTextLike | None:
        from bot_modules.core.utils import get_guild_channel_or_thread as _impl

        return _impl(guild, channel_id)

    def get_xp_config_target_channel(
        self, interaction: discord.Interaction
    ) -> GuildTextLike | None:
        channel = interaction.channel
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        return None

    def is_mod(self, interaction: discord.Interaction) -> bool:
        _log = logging.getLogger("dungeonkeeper.perms")
        perms = interaction.permissions
        member = self.get_interaction_member(interaction)
        _log.info(
            "is_mod user=%s perms_value=%d manage_guild=%s administrator=%s member=%s member_perms=%s",
            interaction.user.id,
            perms.value,
            perms.manage_guild,
            perms.administrator,
            member,
            member.guild_permissions.value if member else None,
        )
        if perms.manage_guild or perms.administrator:
            return True
        if member is None or interaction.guild_id is None:
            return False
        return self.guild_config(interaction.guild_id).member_is_mod(member)

    def is_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.permissions.administrator:
            return True
        member = self.get_interaction_member(interaction)
        if member is None or interaction.guild_id is None:
            return False
        return self.guild_config(interaction.guild_id).member_is_admin(member)

    def reload_grant_roles(self) -> None:
        with self.open_db() as conn:
            self.grant_roles = get_grant_roles(conn, self.guild_id)

    def can_grant_any_role(self, interaction: discord.Interaction) -> bool:
        """Returns True if user can grant ANY configured grant role."""
        if self.is_mod(interaction):
            return True
        member = self.get_interaction_member(interaction)
        if member is None or interaction.guild_id is None:
            return False
        gid = interaction.guild_id
        role_ids = [r.id for r in member.roles]
        with self.open_db() as conn:
            for grant_name in self.guild_config(gid).grant_roles:
                if can_use_grant(conn, gid, grant_name, member.id, role_ids):
                    return True
        return False

    def can_use_grant_role(
        self, interaction: discord.Interaction, grant_name: str
    ) -> bool:
        if self.is_mod(interaction):
            return True
        member = self.get_interaction_member(interaction)
        if member is None or interaction.guild_id is None:
            return False
        role_ids = [r.id for r in member.roles]
        with self.open_db() as conn:
            return can_use_grant(
                conn, interaction.guild_id, grant_name, member.id, role_ids
            )

    def can_use_xp_grant(self, interaction: discord.Interaction) -> bool:
        if self.is_mod(interaction):
            return True
        if interaction.guild_id is None:
            return False
        allowed = self.guild_config(interaction.guild_id).xp_grant_allowed_user_ids
        return interaction.user.id in allowed

    def get_member_last_activity_map(self, conn, guild_id: int, user_ids: list[int]):
        from bot_modules.core.xp_system import get_member_last_activity_map

        return get_member_last_activity_map(conn, guild_id, user_ids)
