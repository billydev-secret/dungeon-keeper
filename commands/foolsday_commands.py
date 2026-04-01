"""April Fools name-shuffle command.

Records current display names, then shuffles nicknames among members who
have been active in at least 3 of the last 5 days.  A restore option sets
everyone back to their original name.  While active, names are reshuffled
every hour via a background loop.
"""
from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
import time as _time
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands

if TYPE_CHECKING:
    from app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.foolsday")

DAY_SECONDS = 86400
RESHUFFLE_INTERVAL = 3600  # seconds between automatic reshuffles


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _init_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS foolsday_names (
            guild_id   INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            original   TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )


def _save_names(
    conn: sqlite3.Connection,
    guild_id: int,
    names: dict[int, str],
) -> None:
    """Store original display names, overwriting any previous snapshot."""
    conn.execute("DELETE FROM foolsday_names WHERE guild_id = ?", (guild_id,))
    conn.executemany(
        "INSERT INTO foolsday_names (guild_id, user_id, original) VALUES (?, ?, ?)",
        [(guild_id, uid, name) for uid, name in names.items()],
    )


def _load_names(
    conn: sqlite3.Connection,
    guild_id: int,
) -> dict[int, str]:
    rows = conn.execute(
        "SELECT user_id, original FROM foolsday_names WHERE guild_id = ?",
        (guild_id,),
    ).fetchall()
    return {int(r[0]): r[1] for r in rows}


def _clear_names(conn: sqlite3.Connection, guild_id: int) -> None:
    conn.execute("DELETE FROM foolsday_names WHERE guild_id = ?", (guild_id,))


def _active_user_ids(
    conn: sqlite3.Connection,
    guild_id: int,
    min_days: int = 3,
    window_days: int = 5,
) -> set[int]:
    """Return user IDs that posted on at least *min_days* of the last *window_days*."""
    cutoff = int(_time.time()) - window_days * DAY_SECONDS
    rows = conn.execute(
        """
        SELECT user_id
        FROM (
            SELECT user_id,
                   COUNT(DISTINCT CAST(created_at / ? AS INTEGER)) AS active_days
            FROM processed_messages
            WHERE guild_id = ? AND created_at >= ?
            GROUP BY user_id
        )
        WHERE active_days >= ?
        """,
        (DAY_SECONDS, guild_id, cutoff, min_days),
    ).fetchall()
    return {int(r[0]) for r in rows}


# ---------------------------------------------------------------------------
# Reshuffle helper (used by both the command and the background loop)
# ---------------------------------------------------------------------------

async def _reshuffle_guild(guild: discord.Guild, conn: sqlite3.Connection, bot_user_id: int) -> None:
    """Reshuffle nicknames among members that have saved originals."""
    saved = _load_names(conn, guild.id)
    if not saved:
        return

    bot_member = guild.get_member(bot_user_id)
    candidates: list[discord.Member] = []
    for uid in saved:
        m = guild.get_member(uid)
        if m is None or m.bot:
            continue
        if m.id == guild.owner_id:
            continue
        if bot_member and m.top_role >= bot_member.top_role:
            continue
        candidates.append(m)

    if len(candidates) < 2:
        log.info("Foolsday reshuffle: not enough renameable members (%d), skipping", len(candidates))
        return

    # Collect current display names and shuffle
    names = [m.display_name for m in candidates]
    random.shuffle(names)
    if len(candidates) > 2:
        for _ in range(20):
            if all(names[i] != candidates[i].display_name for i in range(len(candidates))):
                break
            random.shuffle(names)

    renamed = 0
    failed = 0
    for member, new_name in zip(candidates, names):
        try:
            log.debug("Reshuffle %s (%d): %r -> %r", member, member.id, member.display_name, new_name)
            await member.edit(nick=new_name, reason="April Fools hourly reshuffle")
            renamed += 1
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Reshuffle could not rename %s (%d): %s", member, member.id, exc)
            failed += 1

    log.info("Foolsday reshuffle complete: %d renamed, %d failed", renamed, failed)


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------

async def foolsday_loop(bot: discord.Client, db_path: Path) -> None:
    """Reshuffle names every hour while the foolsday shuffle is active."""
    from db_utils import open_db as _open_db

    await bot.wait_until_ready()

    while not bot.is_closed():
        await asyncio.sleep(RESHUFFLE_INTERVAL)
        try:
            with _open_db(db_path) as conn:
                _init_table(conn)
                rows = conn.execute(
                    "SELECT DISTINCT guild_id FROM foolsday_names"
                ).fetchall()
                if not rows:
                    continue
                for (guild_id,) in rows:
                    guild = bot.get_guild(guild_id)
                    if guild is None:
                        continue
                    log.info("Foolsday hourly reshuffle for guild %s (%d)", guild.name, guild.id)
                    await _reshuffle_guild(guild, conn, bot.user.id if bot.user else 0)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Foolsday reshuffle loop iteration failed")


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

def register_foolsday_commands(bot: "Bot", ctx: "AppContext") -> None:

    @bot.tree.command(
        name="foolsday",
        description="April Fools name shuffle — randomise or restore member nicknames.",
    )
    @app_commands.describe(
        action="shuffle = randomise names, restore = set names back to original.",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="shuffle", value="shuffle"),
        app_commands.Choice(name="restore", value="restore"),
    ])
    async def foolsday(
        interaction: discord.Interaction,
        action: str,
    ) -> None:
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        with ctx.open_db() as conn:
            _init_table(conn)

            if action == "shuffle":
                # Find active members
                active_ids = _active_user_ids(conn, guild.id)
                log.info("Foolsday shuffle initiated by %s — %d active user(s) found", interaction.user, len(active_ids))
                if len(active_ids) < 2:
                    await interaction.followup.send(
                        "Not enough active members to shuffle (need at least 2).",
                        ephemeral=True,
                    )
                    return

                # Resolve to guild members the bot can rename
                bot_member = guild.get_member(bot.user.id) if bot.user else None
                candidates: list[discord.Member] = []
                skipped_bot = 0
                skipped_owner = False
                skipped_role = 0
                for uid in active_ids:
                    m = guild.get_member(uid)
                    if m is None or m.bot:
                        skipped_bot += 1
                        continue
                    if m.id == guild.owner_id:
                        skipped_owner = True
                        continue
                    if bot_member and m.top_role >= bot_member.top_role:
                        skipped_role += 1
                        continue
                    candidates.append(m)

                log.info(
                    "Foolsday candidates: %d renameable, %d bots/missing, %d above bot role, owner skipped=%s",
                    len(candidates), skipped_bot, skipped_role, skipped_owner,
                )

                if len(candidates) < 2:
                    await interaction.followup.send(
                        "Not enough renameable active members (need at least 2). "
                        "Members above my role or the server owner can't be renamed.",
                        ephemeral=True,
                    )
                    return

                # Save originals
                originals = {m.id: m.display_name for m in candidates}
                _save_names(conn, guild.id, originals)

                # Shuffle names
                names = list(originals.values())
                random.shuffle(names)
                # Make sure nobody keeps their own name if possible
                if len(candidates) > 2:
                    for _ in range(20):
                        if all(names[i] != candidates[i].display_name for i in range(len(candidates))):
                            break
                        random.shuffle(names)

                # Apply
                renamed = 0
                failed = 0
                for member, new_name in zip(candidates, names):
                    try:
                        log.debug("Renaming %s (%d): %r -> %r", member, member.id, member.display_name, new_name)
                        await member.edit(nick=new_name, reason="April Fools name shuffle")
                        renamed += 1
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        log.warning("Could not rename %s (%d): %s", member, member.id, exc)
                        failed += 1

                log.info("Foolsday shuffle complete: %d renamed, %d failed", renamed, failed)
                msg = f"Shuffled **{renamed}** member nicknames."
                if failed:
                    msg += f"\nFailed to rename **{failed}** members (permission issues)."
                msg += "\nNames will reshuffle automatically every hour."
                msg += "\nUse `/foolsday action:restore` to stop and undo."
                await interaction.followup.send(msg, ephemeral=True)

            else:
                # Restore
                saved = _load_names(conn, guild.id)
                log.info("Foolsday restore initiated by %s — %d saved name(s)", interaction.user, len(saved))
                if not saved:
                    await interaction.followup.send(
                        "No saved names found — nothing to restore.",
                        ephemeral=True,
                    )
                    return

                restored = 0
                failed = 0
                for uid, original_name in saved.items():
                    m = guild.get_member(uid)
                    if m is None:
                        continue
                    # Set nick to None if original matches their username
                    # (i.e. they had no nickname before)
                    nick = None if original_name == m.name else original_name
                    try:
                        log.debug("Restoring %s (%d): %r -> %r", m, m.id, m.display_name, original_name)
                        await m.edit(nick=nick, reason="April Fools restore")
                        restored += 1
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        log.warning("Could not restore %s (%d): %s", m, m.id, exc)
                        failed += 1

                log.info("Foolsday restore complete: %d restored, %d failed", restored, failed)
                _clear_names(conn, guild.id)

                msg = f"Restored **{restored}** member nicknames."
                if failed:
                    msg += f"\nFailed to restore **{failed}** members."
                await interaction.followup.send(msg, ephemeral=True)
