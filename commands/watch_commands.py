"""User watch list commands — DM any public posts from watched users to the watcher.

Watched-user state is persisted in the ``watched_users`` DB table and mirrored
in ``AppContext.watched_users`` for fast lookup on every message event.

Commands (all mod-only):
  /watch_user   — start watching a member
  /unwatch_user — stop watching a member
  /watch_list   — list members you are currently watching
"""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import discord
from discord import app_commands

if TYPE_CHECKING:
    from app_context import AppContext, Bot


def init_watch_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watched_users (
            guild_id       INTEGER NOT NULL,
            watched_user_id INTEGER NOT NULL,
            watcher_user_id INTEGER NOT NULL,
            PRIMARY KEY (guild_id, watched_user_id, watcher_user_id)
        )
        """
    )


def load_watched_users(conn: sqlite3.Connection, guild_id: int) -> dict[int, set[int]]:
    """Return {watched_user_id: {watcher_user_id, ...}} for the given guild."""
    rows = conn.execute(
        "SELECT watched_user_id, watcher_user_id FROM watched_users WHERE guild_id = ?",
        (guild_id,),
    ).fetchall()
    result: dict[int, set[int]] = {}
    for row in rows:
        result.setdefault(row["watched_user_id"], set()).add(row["watcher_user_id"])
    return result


def register_watch_commands(bot: Bot, ctx: AppContext) -> None:
    @bot.tree.command(
        name="watch_user",
        description="Watch a user — their public posts will be DM'd to you.",
    )
    @app_commands.describe(user="The server member to watch.")
    async def watch_user(interaction: discord.Interaction, user: discord.Member):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        if user.bot:
            await interaction.response.send_message("You cannot watch bots.", ephemeral=True)
            return

        if user.id == interaction.user.id:
            await interaction.response.send_message("You cannot watch yourself.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        watcher_id = interaction.user.id
        watched_id = user.id

        with ctx.open_db() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO watched_users (guild_id, watched_user_id, watcher_user_id)
                VALUES (?, ?, ?)
                """,
                (guild_id, watched_id, watcher_id),
            )

        ctx.watched_users.setdefault(watched_id, set()).add(watcher_id)

        await interaction.response.send_message(
            f"Now watching {user.mention}. Their public posts will be DM'd to you.",
            ephemeral=True,
        )

    @bot.tree.command(
        name="unwatch_user",
        description="Stop watching a user.",
    )
    @app_commands.describe(user="The server member to stop watching.")
    async def unwatch_user(interaction: discord.Interaction, user: discord.Member):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        guild_id = interaction.guild_id
        watcher_id = interaction.user.id
        watched_id = user.id

        with ctx.open_db() as conn:
            conn.execute(
                "DELETE FROM watched_users WHERE guild_id = ? AND watched_user_id = ? AND watcher_user_id = ?",
                (guild_id, watched_id, watcher_id),
            )

        if watched_id in ctx.watched_users:
            ctx.watched_users[watched_id].discard(watcher_id)
            if not ctx.watched_users[watched_id]:
                del ctx.watched_users[watched_id]

        await interaction.response.send_message(
            f"Stopped watching {user.mention}.", ephemeral=True
        )

    @bot.tree.command(
        name="watch_list",
        description="Show the users you are currently watching.",
    )
    async def watch_list(interaction: discord.Interaction):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        watcher_id = interaction.user.id
        guild = interaction.guild

        watched_ids = [uid for uid, watchers in ctx.watched_users.items() if watcher_id in watchers]

        if not watched_ids:
            await interaction.response.send_message(
                "You are not watching any users.", ephemeral=True
            )
            return

        labels = []
        for uid in sorted(watched_ids):
            member = guild.get_member(uid) if guild else None
            labels.append(member.mention if member else f"`{uid}`")

        await interaction.response.send_message(
            "You are currently watching: " + ", ".join(labels), ephemeral=True
        )
