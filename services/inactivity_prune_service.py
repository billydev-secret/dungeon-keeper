"""Inactivity prune service - removes a role from members inactive for N days, running at midnight."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import discord

from db_utils import open_db
from settings import AUTO_DELETE_SETTINGS

if TYPE_CHECKING:
    pass

log = logging.getLogger("dungeonkeeper.inactivity_prune")


# ---------------------------------------------------------------------------
# Table init
# ---------------------------------------------------------------------------

def init_inactivity_prune_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS inactivity_prune_rules (
            guild_id INTEGER PRIMARY KEY,
            role_id  INTEGER NOT NULL,
            inactivity_days INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS inactivity_prune_exceptions (
            guild_id INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )


# ---------------------------------------------------------------------------
# Rule CRUD
# ---------------------------------------------------------------------------

def upsert_prune_rule(db_path: Path, guild_id: int, role_id: int, inactivity_days: int) -> None:
    with open_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO inactivity_prune_rules (guild_id, role_id, inactivity_days)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                role_id = excluded.role_id,
                inactivity_days = excluded.inactivity_days
            """,
            (guild_id, role_id, inactivity_days),
        )


def remove_prune_rule(db_path: Path, guild_id: int) -> bool:
    with open_db(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM inactivity_prune_rules WHERE guild_id = ?", (guild_id,)
        )
        return cursor.rowcount > 0


def get_prune_rule(db_path: Path, guild_id: int) -> sqlite3.Row | None:
    with open_db(db_path) as conn:
        row: sqlite3.Row | None = conn.execute(
            "SELECT guild_id, role_id, inactivity_days FROM inactivity_prune_rules WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        return row


def list_all_prune_rules(db_path: Path) -> list[sqlite3.Row]:
    with open_db(db_path) as conn:
        return conn.execute(
            "SELECT guild_id, role_id, inactivity_days FROM inactivity_prune_rules"
        ).fetchall()


# ---------------------------------------------------------------------------
# Exception list CRUD
# ---------------------------------------------------------------------------

def add_prune_exception(db_path: Path, guild_id: int, user_id: int) -> None:
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO inactivity_prune_exceptions (guild_id, user_id) VALUES (?, ?)",
            (guild_id, user_id),
        )


def remove_prune_exception(db_path: Path, guild_id: int, user_id: int) -> bool:
    with open_db(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM inactivity_prune_exceptions WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        return cursor.rowcount > 0


def get_prune_exception_ids(db_path: Path, guild_id: int) -> set[int]:
    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT user_id FROM inactivity_prune_exceptions WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
        return {int(row["user_id"]) for row in rows}


# ---------------------------------------------------------------------------
# Prune logic
# ---------------------------------------------------------------------------

async def run_prune_for_guild(
    bot: discord.Client,
    db_path: Path,
    guild_id: int,
    role_id: int,
    inactivity_days: int,
) -> None:
    from xp_system import get_member_last_activity_map

    guild = bot.get_guild(guild_id)
    if guild is None:
        log.warning("Inactivity prune: guild %s not found; skipping.", guild_id)
        return

    role = guild.get_role(role_id)
    if role is None:
        log.warning("Inactivity prune: role %s not found in guild %s; skipping.", role_id, guild.name)
        return

    exceptions = get_prune_exception_ids(db_path, guild_id)
    candidates = [m for m in role.members if not m.bot and m.id not in exceptions]
    if not candidates:
        return

    cutoff_ts = (discord.utils.utcnow().timestamp()) - inactivity_days * 86400
    candidate_ids = [m.id for m in candidates]

    with open_db(db_path) as conn:
        activity_map = get_member_last_activity_map(conn, guild_id, candidate_ids)

    pruned: list[discord.Member] = []
    next_action_at = 0.0
    for member in candidates:
        activity = activity_map.get(member.id)
        if activity is None:
            continue
        if activity.created_at < cutoff_ts:
            now = time.monotonic()
            if now < next_action_at:
                await asyncio.sleep(next_action_at - now)
            try:
                await member.remove_roles(role, reason=f"Inactivity prune: no activity in {inactivity_days} days")
                pruned.append(member)
            except discord.Forbidden:
                log.warning("Inactivity prune: missing permission to remove role from %s (%s).", member, member.id)
            except discord.HTTPException as exc:
                log.warning("Inactivity prune: HTTP error removing role from %s: %s", member, exc)
            next_action_at = time.monotonic() + AUTO_DELETE_SETTINGS.role_modify_pause_seconds

    if pruned:
        log.info(
            "Inactivity prune: removed @%s from %d member(s) in guild %s: %s",
            role.name,
            len(pruned),
            guild.name,
            ", ".join(m.display_name for m in pruned),
        )


# ---------------------------------------------------------------------------
# Midnight scheduling loop
# ---------------------------------------------------------------------------

def _seconds_until_next_midnight_utc() -> float:
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (next_midnight - now).total_seconds()


async def inactivity_prune_loop(bot: discord.Client, db_path: Path) -> None:
    await bot.wait_until_ready()

    while not bot.is_closed():
        sleep_secs = _seconds_until_next_midnight_utc()
        log.info("Inactivity prune: next run in %.0f seconds (midnight UTC).", sleep_secs)
        await asyncio.sleep(sleep_secs)

        rules = list_all_prune_rules(db_path)
        for rule in rules:
            try:
                await run_prune_for_guild(
                    bot,
                    db_path,
                    int(rule["guild_id"]),
                    int(rule["role_id"]),
                    int(rule["inactivity_days"]),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "Inactivity prune: unhandled error for guild %s.", rule["guild_id"]
                )
