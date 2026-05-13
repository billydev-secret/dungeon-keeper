"""Hash-based slash command sync.

Computes a stable signature of the local command tree and compares it to a
previously stored hash before calling Discord's bulk-overwrite API. If the
hash matches, the sync is skipped — saving an API round-trip and avoiding
the global-sync rate limit on no-op deploys / reloads.

Hash storage uses the existing ``config`` table:
  - global scope:  key=``command_tree_hash_global``, guild_id=0
  - per-guild:     key=``command_tree_hash_guild``,  guild_id=<gid>
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands

from bot_modules.core.db_utils import get_config_value, open_db, set_config_value

if TYPE_CHECKING:
    pass

log = logging.getLogger("dungeonkeeper.command_sync")

_KEY_GLOBAL = "command_tree_hash_global"
_KEY_GUILD = "command_tree_hash_guild"


def _param_signature(p: app_commands.Parameter) -> dict[str, Any]:
    sig: dict[str, Any] = {
        "name": p.name,
        "description": getattr(p, "description", "") or "",
        "required": bool(getattr(p, "required", True)),
        "type": str(getattr(p, "type", "")),
    }
    choices = getattr(p, "choices", None) or []
    if choices:
        sig["choices"] = sorted(
            ({"name": c.name, "value": str(c.value)} for c in choices),
            key=lambda x: x["name"],
        )
    if getattr(p, "autocomplete", None):
        sig["autocomplete"] = True
    return sig


def _command_signature(
    cmd: app_commands.Command | app_commands.Group | app_commands.ContextMenu,
) -> dict[str, Any]:
    sig: dict[str, Any] = {
        "name": cmd.name,
        "description": getattr(cmd, "description", "") or "",
    }
    perms = getattr(cmd, "default_permissions", None)
    if perms is not None:
        sig["default_permissions"] = (
            perms.value if hasattr(perms, "value") else int(perms)
        )
    if getattr(cmd, "nsfw", False):
        sig["nsfw"] = True
    if getattr(cmd, "guild_only", False):
        sig["guild_only"] = True

    cmd_type = getattr(cmd, "type", None)
    if cmd_type is not None:
        sig["type"] = str(cmd_type)

    sub = getattr(cmd, "commands", None)
    if sub:
        sig["subcommands"] = sorted(
            (_command_signature(s) for s in sub), key=lambda x: x["name"]
        )

    params = getattr(cmd, "parameters", None)
    if params:
        sig["options"] = sorted(
            (_param_signature(p) for p in params), key=lambda x: x["name"]
        )
    return sig


def compute_tree_hash(
    tree: app_commands.CommandTree, *, guild: discord.abc.Snowflake | None
) -> str:
    """Compute a stable SHA256 of the tree's commands for the given scope."""
    cmds: list[Any] = []
    for cmd_type in (
        discord.AppCommandType.chat_input,
        discord.AppCommandType.user,
        discord.AppCommandType.message,
    ):
        cmds.extend(tree.get_commands(guild=guild, type=cmd_type))
    payload = json.dumps(
        sorted(
            (_command_signature(c) for c in cmds),
            key=lambda x: (x.get("type", ""), x["name"]),
        ),
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_stored_hash(db_path: Path, *, guild_id: int, key: str) -> str:
    with open_db(db_path) as conn:
        return get_config_value(conn, key, "", guild_id=guild_id) or ""


def _write_stored_hash(
    db_path: Path, *, guild_id: int, key: str, value: str
) -> None:
    with open_db(db_path) as conn:
        set_config_value(conn, key, value, guild_id=guild_id)


async def sync_if_changed(
    tree: app_commands.CommandTree,
    db_path: Path,
    *,
    guild: discord.abc.Snowflake | None,
) -> tuple[list[app_commands.AppCommand], bool]:
    """Sync the tree iff the local signature differs from the stored hash.

    Returns ``(synced_commands, did_sync)``. When the hash already matches
    the previously-synced state, returns ``([], False)`` without calling
    Discord.
    """
    new_hash = compute_tree_hash(tree, guild=guild)

    if guild is None:
        key, gid = _KEY_GLOBAL, 0
        scope_label = "global"
    else:
        key, gid = _KEY_GUILD, int(guild.id)
        scope_label = f"guild={gid}"

    old_hash = _read_stored_hash(db_path, guild_id=gid, key=key)
    if old_hash == new_hash:
        log.info(
            "Command tree hash unchanged for %s — skipping sync", scope_label
        )
        return [], False

    if guild is None:
        synced = await tree.sync()
    else:
        synced = await tree.sync(guild=guild)

    _write_stored_hash(db_path, guild_id=gid, key=key, value=new_hash)
    log.info(
        "Command tree synced (%d commands) for %s", len(synced), scope_label
    )
    return synced, True


def invalidate_stored_hash(
    db_path: Path, *, guild: discord.abc.Snowflake | None
) -> None:
    """Force the next sync_if_changed to push, regardless of local state."""
    if guild is None:
        key, gid = _KEY_GLOBAL, 0
    else:
        key, gid = _KEY_GUILD, int(guild.id)
    _write_stored_hash(db_path, guild_id=gid, key=key, value="")
