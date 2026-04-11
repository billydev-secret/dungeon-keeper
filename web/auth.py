"""Pluggable authentication backends for the dashboard.

Ships with two backends:

* ``OpenAuth`` — no-op, grants full permissions. For trusted LAN deployments.
* ``DiscordOAuthAuth`` — session-cookie auth backed by Discord OAuth2.
  Resolves permissions per-request from the bot's guild member cache (live)
  or from stored OAuth data (standalone fallback).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from fastapi import Request

_log = logging.getLogger("dungeonkeeper.web.auth")

SESSION_COOKIE = "dk_session"
SESSION_MAX_AGE = 30 * 86400  # 30 days

# Discord permission bits used for dashboard access mapping
_ADMINISTRATOR = 0x8
_MANAGE_GUILD = 0x20
_KICK_MEMBERS = 0x2
_BAN_MEMBERS = 0x4
_MANAGE_MESSAGES = 0x2000
_MANAGE_ROLES = 0x10000000


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: int
    username: str
    perms: frozenset[str]

    def has_perm(self, perm: str) -> bool:
        return perm in self.perms


class AuthBackend(Protocol):
    async def authenticate(self, request: Request) -> AuthenticatedUser | None: ...


class OpenAuth:
    """No-auth backend: every request is treated as a full-permission admin.

    Appropriate for a trusted LAN deployment. Do not use this if the bot host
    is reachable from an untrusted network.
    """

    _ALL_PERMS = frozenset({"manage_guild", "manage_roles"})

    async def authenticate(self, request: Request) -> AuthenticatedUser:
        return AuthenticatedUser(
            user_id=0,
            username="anonymous",
            perms=self._ALL_PERMS,
        )


def resolve_discord_perms(permission_bits: int) -> frozenset[str]:
    """Map a Discord permission bitfield to dashboard permission strings."""
    perms: set[str] = set()
    if permission_bits & _ADMINISTRATOR:
        perms.update({"manage_guild", "manage_roles"})
    else:
        if permission_bits & (_MANAGE_GUILD | _KICK_MEMBERS | _BAN_MEMBERS | _MANAGE_MESSAGES):
            perms.add("manage_guild")
        if permission_bits & _MANAGE_ROLES:
            perms.add("manage_roles")
    return frozenset(perms)


class DiscordOAuthAuth:
    """Discord OAuth2 session-based authentication.

    On every request the backend resolves permissions live from the bot's
    guild member cache when available, guaranteeing that role changes in
    Discord are reflected immediately. When the bot cache is unavailable
    (standalone mode), it falls back to permissions stored in the session
    at login time.
    """

    def __init__(self, session_secret: str, guild_id: int) -> None:
        from itsdangerous import URLSafeTimedSerializer
        self._serializer = URLSafeTimedSerializer(session_secret)
        self._guild_id = guild_id

    # ── Session cookie helpers ──────────────────────────────────────

    def create_session_cookie(
        self,
        user_id: int,
        username: str,
        access_token: str,
        permission_bits: int = 0,
    ) -> str:
        """Create a signed, timestamped session cookie value."""
        return self._serializer.dumps({
            "uid": user_id,
            "name": username,
            "token": access_token,
            "perms_bits": permission_bits,
        })

    def read_session(self, cookie: str) -> dict | None:
        """Decode and verify a session cookie. Returns None on failure."""
        from itsdangerous import BadSignature
        try:
            return self._serializer.loads(cookie, max_age=SESSION_MAX_AGE)  # type: ignore[no-any-return]
        except (BadSignature, Exception):
            return None

    # ── Per-request authentication ──────────────────────────────────

    async def authenticate(self, request: Request) -> AuthenticatedUser | None:
        cookie = request.cookies.get(SESSION_COOKIE)
        if not cookie:
            return None
        session = self.read_session(cookie)
        if not session:
            return None

        user_id: int = session["uid"]
        username: str = session["name"]

        # Prefer bot guild cache — instant, always reflects current roles
        ctx = request.app.state.ctx
        bot = getattr(ctx, "bot", None)
        guild = bot.get_guild(self._guild_id) if bot else None

        if guild:
            member = guild.get_member(user_id)
            if not member:
                return None  # User no longer in guild
            perms = resolve_discord_perms(member.guild_permissions.value)
            return AuthenticatedUser(
                user_id=user_id,
                username=member.display_name,
                perms=perms,
            )

        # Fallback: use permission bits stored at login time
        perms_bits: int = session.get("perms_bits", 0)
        return AuthenticatedUser(
            user_id=user_id,
            username=username,
            perms=resolve_discord_perms(perms_bits),
        )
