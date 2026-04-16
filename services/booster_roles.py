"""Booster cosmetic role picker — persistent panel with mutually exclusive roles."""

from __future__ import annotations

import io
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import TypedDict

import discord

from db_utils import get_config_value, open_db

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

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
    # Migrate old single-row table to multi-row table
    info = conn.execute("PRAGMA table_info(booster_panel_messages)").fetchall()
    pk_cols = [r["name"] for r in info if r["pk"] > 0]
    if info and pk_cols == ["guild_id"]:
        conn.execute("DROP TABLE booster_panel_messages")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS booster_panel_messages (
            guild_id   INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            PRIMARY KEY (guild_id, message_id)
        )
        """
    )


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def get_booster_roles(
    conn: sqlite3.Connection,
    guild_id: int,
) -> list[BoosterRoleRow]:
    rows = conn.execute(
        "SELECT role_key, label, role_id, image_path, sort_order "
        "FROM booster_roles WHERE guild_id = ? ORDER BY sort_order, role_key",
        (guild_id,),
    ).fetchall()
    return [
        BoosterRoleRow(
            role_key=r["role_key"],
            label=r["label"],
            role_id=r["role_id"],
            image_path=r["image_path"],
            sort_order=r["sort_order"],
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
    conn: sqlite3.Connection,
    guild_id: int,
    role_key: str,
) -> bool:
    cursor = conn.execute(
        "DELETE FROM booster_roles WHERE guild_id = ? AND role_key = ?",
        (guild_id, role_key),
    )
    return cursor.rowcount > 0


def get_booster_panel_refs(
    conn: sqlite3.Connection,
    guild_id: int,
) -> list[tuple[int, int]]:
    rows = conn.execute(
        "SELECT channel_id, message_id FROM booster_panel_messages WHERE guild_id = ?",
        (guild_id,),
    ).fetchall()
    return [(int(r["channel_id"]), int(r["message_id"])) for r in rows]


def replace_booster_panel_refs(
    conn: sqlite3.Connection,
    guild_id: int,
    refs: list[tuple[int, int]],
) -> None:
    conn.execute("DELETE FROM booster_panel_messages WHERE guild_id = ?", (guild_id,))
    conn.executemany(
        "INSERT INTO booster_panel_messages (guild_id, channel_id, message_id) VALUES (?, ?, ?)",
        [(guild_id, ch, msg) for ch, msg in refs],
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
                style=discord.ButtonStyle.secondary,
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
    ) -> BoosterRoleDynamicButton:
        key = (item.custom_id or "").removeprefix("booster_role:")
        return cls(key)

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "This only works in a server.",
                ephemeral=True,
            )
            return

        if member.premium_since is None:
            await interaction.response.send_message(
                "Only server boosters can pick a cosmetic role.",
                ephemeral=True,
            )
            return

        # Look up the DB path from the bot
        db_path: Path = getattr(interaction.client, "db_path", Path("bot.db"))

        with open_db(db_path) as conn:
            roles = get_booster_roles(conn, guild.id)

        target = next((r for r in roles if r["role_key"] == self.key), None)
        if target is None:
            await interaction.response.send_message(
                "This role option no longer exists.",
                ephemeral=True,
            )
            return

        target_role = guild.get_role(target["role_id"])
        if target_role is None:
            await interaction.response.send_message(
                "The configured role no longer exists in this server.",
                ephemeral=True,
            )
            return

        all_role_ids = {r["role_id"] for r in roles if r["role_id"] > 0}
        to_remove = [
            r for r in member.roles if r.id in all_role_ids and r.id != target_role.id
        ]

        if target_role in member.roles and not to_remove:
            await interaction.response.send_message(
                f"You already have {target_role.mention}.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            if to_remove:
                await member.remove_roles(
                    *to_remove, reason="Booster cosmetic role switch"
                )
            if target_role not in member.roles:
                await member.add_roles(target_role, reason="Booster cosmetic role pick")
        except (discord.Forbidden, discord.HTTPException) as exc:
            await interaction.followup.send(
                f"Failed to update roles: {exc}",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"You now have {target_role.mention}!",
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Panel builder
# ---------------------------------------------------------------------------


def _safe_filename(name: str, ext: str) -> str:
    """Sanitise a name into a Discord-safe attachment filename."""
    clean = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    return f"{clean}{ext}"


def _make_role_file(role: BoosterRoleRow) -> discord.File | None:
    """Return a discord.File for a role's image, or None."""
    image_path = role["image_path"]
    if not image_path:
        return None
    if not os.path.isfile(image_path):
        log.warning("Booster role %r image not found: %s", role["role_key"], image_path)
        return None
    ext = os.path.splitext(image_path)[1] or ".png"
    filename = _safe_filename(role["role_key"], ext)
    with open(image_path, "rb") as fp:
        data = fp.read()
    log.info(
        "Booster role %r: attaching %s as %s (%d bytes)",
        role["role_key"],
        image_path,
        filename,
        len(data),
    )
    return discord.File(io.BytesIO(data), filename=filename)


async def post_or_update_booster_panel(
    db_path: Path,
    guild: discord.Guild,
    channel: discord.TextChannel,
) -> list[discord.Message]:
    """Post one message per booster role (image + button). Returns the messages."""
    with open_db(db_path) as conn:
        roles = get_booster_roles(conn, guild.id)

    if not roles:
        return []

    # Delete old panel messages
    with open_db(db_path) as conn:
        old_refs = get_booster_panel_refs(conn, guild.id)
    for old_channel_id, old_message_id in old_refs:
        try:
            old_ch = guild.get_channel(old_channel_id)
            if old_ch is not None and isinstance(old_ch, discord.TextChannel):
                await old_ch.get_partial_message(old_message_id).delete()
        except (discord.NotFound, discord.HTTPException):
            pass

    # Header message
    header = await channel.send("**Pick your booster cosmetic role:**")

    # One message per role: image file + single button, with spacers between
    messages: list[discord.Message] = [header]
    for i, role in enumerate(roles):
        if i > 0:
            spacer = await channel.send("\u200b")  # zero-width space as visual gap
            messages.append(spacer)

        view = discord.ui.View(timeout=None)
        btn: discord.ui.Button[discord.ui.View] = discord.ui.Button(
            label=role["label"],
            style=discord.ButtonStyle.secondary,
            custom_id=f"booster_role:{role['role_key']}",
        )
        view.add_item(btn)

        file = _make_role_file(role)
        kwargs: dict = {"view": view}
        if file is not None:
            kwargs["file"] = file
        msg = await channel.send(**kwargs)
        messages.append(msg)

    # Store all message refs for cleanup later
    with open_db(db_path) as conn:
        replace_booster_panel_refs(
            conn,
            guild.id,
            [(channel.id, m.id) for m in messages],
        )
    log.info("Posted %d booster panel messages in #%s", len(messages), channel.name)
    return messages


# ---------------------------------------------------------------------------
# Swatch sync — scan directory, create/delete roles to match files
# ---------------------------------------------------------------------------


def _parse_swatch_filename(filename: str) -> tuple[str, str, str] | None:
    """Parse ``ColorName_HEX1_HEX2.ext`` → (label, hex1, hex2) or None."""
    stem = os.path.splitext(filename)[0]
    parts = stem.split("_")
    if len(parts) < 3:
        return None
    hex2 = parts[-1]
    hex1 = parts[-2]
    if not (
        re.fullmatch(r"[0-9A-Fa-f]{6}", hex1) and re.fullmatch(r"[0-9A-Fa-f]{6}", hex2)
    ):
        return None
    label = " ".join(parts[:-2])
    return label, hex1, hex2


def _hex_sort_key(hex1: str, hex2: str) -> int:
    """Return an integer sort key: hue of hex1 primary, hue of hex2 secondary.

    Uses HSV hue (0-359) so colours order by visual gradient.
    """
    import colorsys

    def _hue(h: str) -> int:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        hue, _s, _v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        return int(hue * 3600)  # 0-3600 for extra precision

    return _hue(hex1) * 10000 + _hue(hex2)


def get_swatch_directory(db_path: Path) -> str:
    with open_db(db_path) as conn:
        return get_config_value(conn, "booster_swatch_dir", "")


async def sync_swatches(
    db_path: Path,
    guild: discord.Guild,
) -> tuple[list[str], list[str]]:
    """Sync booster roles from swatch files on disk.

    Returns (created_labels, removed_labels).
    """
    swatch_dir = get_swatch_directory(db_path)
    if not swatch_dir or not os.path.isdir(swatch_dir):
        raise ValueError(f"Swatch directory not configured or missing: `{swatch_dir}`")

    # Discover swatch files
    # role_key → (label, hex1, hex2, file_path, sort_order)
    found: dict[str, tuple[str, str, str, int]] = {}
    for entry in os.listdir(swatch_dir):
        ext = os.path.splitext(entry)[1].lower()
        if ext not in _IMAGE_EXTS:
            continue
        parsed = _parse_swatch_filename(entry)
        if parsed is None:
            log.warning("Skipping swatch file with unexpected name format: %s", entry)
            continue
        label, hex1, hex2 = parsed
        role_key = label.lower().replace(" ", "_")
        found[role_key] = (
            label,
            hex1,
            hex2,
            os.path.join(swatch_dir, entry),
            _hex_sort_key(hex1, hex2),
        )

    with open_db(db_path) as conn:
        existing = get_booster_roles(conn, guild.id)
    existing_keys = {r["role_key"]: r for r in existing}

    # Find the anchor role to position new roles below
    anchor = discord.utils.get(guild.roles, name="#### Cosmetics")

    created: list[str] = []
    removed: list[str] = []
    new_roles: list[discord.Role] = []

    async def _apply_gradient(role: discord.Role, h1: str, h2: str) -> None:
        """Set two-color gradient on a role via raw HTTP (discord.py has no high-level API)."""
        route = discord.http.Route(
            "PATCH",
            "/guilds/{guild_id}/roles/{role_id}",
            guild_id=guild.id,
            role_id=role.id,
        )
        try:
            await guild._state.http.request(
                route,
                json={"colors": [int(h1, 16), int(h2, 16)]},
                reason="Booster swatch sync",
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not set gradient for role %r: %s", role.name, exc)

    # Create roles for new swatches and update sort order for all
    for key, (label, hex_color, hex_color2, file_path, skey) in sorted(found.items()):
        if key in existing_keys:
            old = existing_keys[key]
            if old["image_path"] != file_path or old["sort_order"] != skey:
                with open_db(db_path) as conn:
                    upsert_booster_role(
                        conn,
                        guild.id,
                        key,
                        label=old["label"],
                        role_id=old["role_id"],
                        image_path=file_path,
                        sort_order=skey,
                    )
            # Update the Discord role's gradient colors without deleting it
            if old["role_id"] > 0:
                discord_role = guild.get_role(old["role_id"])
                if discord_role is not None:
                    await _apply_gradient(discord_role, hex_color, hex_color2)
            continue
        role = await guild.create_role(
            name=label,
            color=discord.Color(int(hex_color, 16)),
            reason="Booster swatch sync",
        )
        await _apply_gradient(role, hex_color, hex_color2)
        new_roles.append(role)
        with open_db(db_path) as conn:
            upsert_booster_role(
                conn,
                guild.id,
                key,
                label=label,
                role_id=role.id,
                image_path=file_path,
                sort_order=skey,
            )
        created.append(label)
        log.info(
            "Created booster role %r (id=%d, color=#%s)", label, role.id, hex_color
        )

    # Position new roles right under the anchor
    if anchor and new_roles:
        target_pos = max(anchor.position - 1, 1)
        positions: dict[discord.abc.Snowflake, int] = {r: target_pos for r in new_roles}
        try:
            await guild.edit_role_positions(positions=positions)
            log.info(
                "Moved %d new booster roles below %r (pos %d)",
                len(new_roles),
                anchor.name,
                target_pos,
            )
        except discord.HTTPException as exc:
            log.warning("Could not reposition booster roles: %s", exc)

    # Remove roles whose swatch files are gone
    for key, row in existing_keys.items():
        if key in found:
            continue
        if row["role_id"] > 0:
            discord_role = guild.get_role(row["role_id"])
            if discord_role is not None:
                try:
                    await discord_role.delete(reason="Booster swatch removed")
                    log.info(
                        "Deleted booster role %r (id=%d)", row["label"], row["role_id"]
                    )
                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.warning(
                        "Could not delete booster role %r: %s", row["label"], exc
                    )
        with open_db(db_path) as conn:
            delete_booster_role(conn, guild.id, key)
        removed.append(row["label"])

    return created, removed
