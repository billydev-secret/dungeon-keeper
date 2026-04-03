"""Booster cosmetic role picker — persistent panel with mutually exclusive roles."""
from __future__ import annotations

import io
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import discord

from db_utils import open_db

if TYPE_CHECKING:
    pass

log = logging.getLogger("dungeonkeeper.booster_roles")


# ---------------------------------------------------------------------------
# DB schema
# ---------------------------------------------------------------------------

class BoosterRoleRow(TypedDict):
    role_key: str
    label: str
    role_id: int
    image_path: str
    sort_order: int


def init_booster_role_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS booster_roles (
            guild_id   INTEGER NOT NULL,
            role_key   TEXT    NOT NULL,
            label      TEXT    NOT NULL,
            role_id    INTEGER NOT NULL DEFAULT 0,
            image_path TEXT    NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, role_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS booster_panel_messages (
            guild_id   INTEGER NOT NULL PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL
        )
        """
    )


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_booster_roles(
    conn: sqlite3.Connection, guild_id: int,
) -> list[BoosterRoleRow]:
    rows = conn.execute(
        "SELECT role_key, label, role_id, image_path, sort_order "
        "FROM booster_roles WHERE guild_id = ? ORDER BY sort_order, role_key",
        (guild_id,),
    ).fetchall()
    return [
        BoosterRoleRow(
            role_key=r["role_key"], label=r["label"], role_id=r["role_id"],
            image_path=r["image_path"], sort_order=r["sort_order"],
        )
        for r in rows
    ]


def upsert_booster_role(
    conn: sqlite3.Connection,
    guild_id: int,
    role_key: str,
    *,
    label: str,
    role_id: int,
    image_path: str,
    sort_order: int,
) -> None:
    conn.execute(
        """
        INSERT INTO booster_roles
            (guild_id, role_key, label, role_id, image_path, sort_order)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, role_key) DO UPDATE SET
            label=excluded.label, role_id=excluded.role_id,
            image_path=excluded.image_path, sort_order=excluded.sort_order
        """,
        (guild_id, role_key, label, role_id, image_path, sort_order),
    )


def delete_booster_role(
    conn: sqlite3.Connection, guild_id: int, role_key: str,
) -> bool:
    cursor = conn.execute(
        "DELETE FROM booster_roles WHERE guild_id = ? AND role_key = ?",
        (guild_id, role_key),
    )
    return cursor.rowcount > 0


def get_booster_panel_ref(
    conn: sqlite3.Connection, guild_id: int,
) -> tuple[int, int] | None:
    row = conn.execute(
        "SELECT channel_id, message_id FROM booster_panel_messages WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()
    if row is None:
        return None
    return (int(row["channel_id"]), int(row["message_id"]))


def upsert_booster_panel_ref(
    conn: sqlite3.Connection, guild_id: int, channel_id: int, message_id: int,
) -> None:
    conn.execute(
        """
        INSERT INTO booster_panel_messages (guild_id, channel_id, message_id)
        VALUES (?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
            channel_id=excluded.channel_id, message_id=excluded.message_id
        """,
        (guild_id, channel_id, message_id),
    )


# ---------------------------------------------------------------------------
# Persistent button via DynamicItem
# ---------------------------------------------------------------------------

class BoosterRoleDynamicButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"booster_role:(?P<key>.+)",
):
    """Handles all booster_role:* button presses, survives bot restarts."""

    def __init__(self, key: str) -> None:
        super().__init__(
            discord.ui.Button(
                label=key,
                style=discord.ButtonStyle.primary,
                custom_id=f"booster_role:{key}",
            )
        )
        self.key = key

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: discord.utils.MISSING,  # type: ignore[assignment]
        /,
    ) -> "BoosterRoleDynamicButton":
        key = (item.custom_id or "").removeprefix("booster_role:")
        return cls(key)

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "This only works in a server.", ephemeral=True,
            )
            return

        if member.premium_since is None:
            await interaction.response.send_message(
                "Only server boosters can pick a cosmetic role.", ephemeral=True,
            )
            return

        # Look up the DB path from the bot
        db_path: Path = getattr(interaction.client, "db_path", Path("bot.db"))

        with open_db(db_path) as conn:
            roles = get_booster_roles(conn, guild.id)

        target = next((r for r in roles if r["role_key"] == self.key), None)
        if target is None:
            await interaction.response.send_message(
                "This role option no longer exists.", ephemeral=True,
            )
            return

        target_role = guild.get_role(target["role_id"])
        if target_role is None:
            await interaction.response.send_message(
                "The configured role no longer exists in this server.", ephemeral=True,
            )
            return

        all_role_ids = {r["role_id"] for r in roles if r["role_id"] > 0}
        to_remove = [r for r in member.roles if r.id in all_role_ids and r.id != target_role.id]

        if target_role in member.roles and not to_remove:
            await interaction.response.send_message(
                f"You already have {target_role.mention}.", ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        if to_remove:
            await member.remove_roles(*to_remove, reason="Booster cosmetic role switch")
        if target_role not in member.roles:
            await member.add_roles(target_role, reason="Booster cosmetic role pick")

        await interaction.followup.send(
            f"You now have {target_role.mention}!", ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Panel builder
# ---------------------------------------------------------------------------

def _build_panel_view(roles: list[BoosterRoleRow]) -> discord.ui.View:
    """Build a View with one button per booster role."""
    view = discord.ui.View(timeout=None)
    for i, role in enumerate(roles):
        btn: discord.ui.Button[discord.ui.View] = discord.ui.Button(
            label=role["label"],
            style=discord.ButtonStyle.primary,
            custom_id=f"booster_role:{role['role_key']}",
            row=i // 5,
        )
        view.add_item(btn)
    return view


def _safe_filename(name: str, ext: str) -> str:
    """Sanitise a name into a Discord-safe attachment filename."""
    clean = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    return f"{clean}{ext}"


def _build_panel_files(roles: list[BoosterRoleRow]) -> list[discord.File]:
    """Build file attachments for roles that have images."""
    files: list[discord.File] = []
    for role in roles:
        image_path = role["image_path"]
        if image_path and os.path.isfile(image_path):
            ext = os.path.splitext(image_path)[1] or ".png"
            filename = _safe_filename(role["role_key"], ext)
            with open(image_path, "rb") as fp:
                data = fp.read()
            files.append(discord.File(io.BytesIO(data), filename=filename))
            log.info(
                "Booster role %r: attaching %s as %s (%d bytes)",
                role["role_key"], image_path, filename, len(data),
            )
        elif image_path:
            log.warning("Booster role %r image not found: %s", role["role_key"], image_path)
    return files


async def post_or_update_booster_panel(
    db_path: Path,
    guild: discord.Guild,
    channel: discord.TextChannel,
) -> discord.Message | None:
    """Post a new booster panel or edit the existing one. Returns the message."""
    with open_db(db_path) as conn:
        roles = get_booster_roles(conn, guild.id)

    if not roles:
        return None

    content = "**Pick your booster cosmetic role:**"
    files = _build_panel_files(roles)
    view = _build_panel_view(roles)

    with open_db(db_path) as conn:
        ref = get_booster_panel_ref(conn, guild.id)

    # Delete old panel message if it exists
    if ref is not None:
        old_channel_id, old_message_id = ref
        try:
            old_channel = guild.get_channel(old_channel_id)
            if old_channel is not None and isinstance(old_channel, discord.TextChannel):
                old_msg = old_channel.get_partial_message(old_message_id)
                await old_msg.delete()
        except (discord.NotFound, discord.HTTPException):
            pass

    msg = await channel.send(content=content, files=files, view=view)
    with open_db(db_path) as conn:
        upsert_booster_panel_ref(conn, guild.id, channel.id, msg.id)
    log.info("Posted booster panel message %d in #%s", msg.id, channel.name)
    return msg
