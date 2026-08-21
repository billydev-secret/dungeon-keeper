"""Shared helpers for web route handlers."""

from __future__ import annotations

import asyncio
import os

import discord
from fastapi import HTTPException

from bot_modules.services.embeds import build_admin_mirror_embed
from bot_modules.services.message_store import get_known_users_bulk


def parse_time_of_day(raw: str, *, field: str = "time") -> int:
    """Parse ``'HH:MM'`` into minutes since local midnight (0..1439).

    Three scheduling panels take a time-of-day off a form and store it as a
    minute offset — announcements, the photo challenge, and scheduled games.
    ``field`` names the offending input in the 400 so the dashboard can point
    at the right box; everything else about the answer is the same.

    Both components are range-checked, not just the total. The three copies
    this replaces checked only the total, so "10:75" was accepted and quietly
    stored as 11:15 — an admin who typoed a post time got a success toast and
    a job that fired 75 minutes off, with nothing anywhere to explain it. The
    dashboard's own ``<input type="time">`` can't produce that, so this only
    changes what a hand-rolled API call gets: a 400 instead of a silent
    reinterpretation.
    """
    try:
        hh, mm = raw.split(":")
        hours, mins = int(hh), int(mm)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail=f"{field} must be 'HH:MM'")
    if not (0 <= hours < 24 and 0 <= mins < 60):
        raise HTTPException(status_code=400, detail=f"{field} out of range")
    return hours * 60 + mins


async def mirror_admin_action_to_mod_log(
    ctx,
    guild_id: int,
    *,
    domain: str,
    action: str,
    summary: str,
    user,
    log,
) -> None:
    """Mirror a web admin action to the guild's Discord mod-log channel.

    One copy of the plumbing every dashboard-managed feature needs (grown out
    of voice_master's ``_post_mod_log_mirror``, 2026-08-17): resolve the
    configured mod channel, post an orange audit embed titled
    ``"{domain} — {action}"`` with a ``by {user} (web)`` footer, and swallow
    send failures with a log line — a mirror must never fail the action it
    mirrors. ``domain`` is the feature's branded prefix (``"🛡️ Voice
    Control"``, ``"🏈 Survivor"``); ``user`` is the AuthenticatedUser;
    ``log`` is the caller's logger so failures land in its feature's feed.
    """
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is None:
        return
    mod_channel_id = ctx.guild_config(guild_id).mod_channel_id
    channel = guild.get_channel(mod_channel_id) if mod_channel_id else None
    if not isinstance(channel, discord.TextChannel):
        return
    embed = build_admin_mirror_embed(
        domain=domain,
        action=action,
        summary=summary,
        actor_name=f"{user.username} (web)",
        actor_id=int(user.user_id),
    )
    try:
        await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        log.exception("failed to mirror web admin action to mod-log")


def public_base_url() -> str:
    """Public origin of the dashboard (for building absolute asset URLs).

    Mirrors ``routes.oauth._base_url`` — reads ``DASHBOARD_BASE_URL`` (the
    Cloudflare-tunnelled https origin in production). Trailing slash stripped.
    """
    return os.getenv("DASHBOARD_BASE_URL", "http://localhost:8080").strip().rstrip("/")


async def resolve_names(ctx, guild, entries, *id_name_pairs):
    """Resolve user IDs to display names in a list of dicts.

    Each pair is (id_field, name_field). Tries the live guild cache first,
    then falls back to the known_users DB table, then "User <id>" as a
    last resort so the frontend never renders a raw integer ID. The DB
    fallback runs off the event loop.
    """
    if not entries:
        return
    guild_id = guild.id if guild else 0
    unresolved: set[int] = set()
    for entry in entries:
        for id_field, name_field in id_name_pairs:
            uid = entry.get(id_field)
            if uid:
                if guild:
                    member = guild.get_member(int(uid))
                    if member:
                        entry[name_field] = member.display_name
                        continue
                unresolved.add(int(uid))
    if unresolved:
        def _db_lookup() -> dict[int, str]:
            with ctx.open_db() as conn:
                return get_known_users_bulk(conn, guild_id, list(unresolved))

        known = await asyncio.to_thread(_db_lookup)
        for entry in entries:
            for id_field, name_field in id_name_pairs:
                if entry.get(name_field):
                    continue
                uid = entry.get(id_field)
                if uid and int(uid) in known:
                    entry[name_field] = known[int(uid)]
    for entry in entries:
        for id_field, name_field in id_name_pairs:
            if entry.get(name_field):
                continue
            uid = entry.get(id_field)
            if uid:
                entry[name_field] = f"User {uid}"
