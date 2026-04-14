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
    role_ids: tuple[int, ...] = ()
    role_names: tuple[str, ...] = ()
    avatar_url: str | None = None

    def has_perm(self, perm: str) -> bool:
        return perm in self.perms

    def has_role(self, role_id: int) -> bool:
        return role_id in self.role_ids

    def has_role_named(self, name: str) -> bool:
        return any(n.lower() == name.lower() for n in self.role_names)


class AuthBackend(Protocol):
    async def authenticate(self, request: Request) -> AuthenticatedUser | None: ...


class OpenAuth:
    """No-auth backend: every request is treated as a full-permission admin.

    Appropriate for a trusted LAN deployment. Do not use this if the bot host
    is reachable from an untrusted network.
    """

    _ALL_PERMS = frozenset({"admin", "moderator"})

    async def authenticate(self, request: Request) -> AuthenticatedUser:
        return AuthenticatedUser(
            user_id=0,
            username="anonymous",
            perms=self._ALL_PERMS,
        )


_MOD_BITS = (
    _MANAGE_GUILD | _KICK_MEMBERS | _BAN_MEMBERS | _MANAGE_MESSAGES | _MANAGE_ROLES
)


def resolve_discord_perms(permission_bits: int) -> frozenset[str]:
    """Map a Discord permission bitfield to dashboard permission strings.

    * ``admin``         — user has the Discord ADMINISTRATOR bit.
    * ``moderator``     — user has ADMINISTRATOR *or* any of MANAGE_GUILD,
      KICK_MEMBERS, BAN_MEMBERS, MANAGE_MESSAGES, MANAGE_ROLES.
    * ``manage_server`` — user has ADMINISTRATOR or MANAGE_GUILD specifically.
      Used by the wellness panel admin pages (spec §10).

    Admin implies moderator AND manage_server.
    """
    perms: set[str] = set()
    if permission_bits & _ADMINISTRATOR:
        perms.update({"admin", "moderator", "manage_server"})
    else:
        if permission_bits & _MOD_BITS:
            perms.add("moderator")
        if permission_bits & _MANAGE_GUILD:
            perms.add("manage_server")
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
        role_ids: list[int] | None = None,
        role_names: list[str] | None = None,
        guild_id: int | None = None,
        guilds: list[dict] | None = None,
        avatar_url: str | None = None,
    ) -> str:
        """Create a signed, timestamped session cookie value."""
        return self._serializer.dumps(
            {
                "uid": user_id,
                "name": username,
                "token": access_token,
                "perms_bits": permission_bits,
                "role_ids": role_ids or [],
                "role_names": role_names or [],
                "guild_id": guild_id or self._guild_id,
                "guilds": guilds or [],
                "avatar_url": avatar_url,
            }
        )

    def read_session(self, cookie: str) -> dict | None:
        """Decode and verify a session cookie. Returns None on failure."""
        from itsdangerous import BadSignature

        try:
            return self._serializer.loads(cookie, max_age=SESSION_MAX_AGE)  # type: ignore[no-any-return]
        except (BadSignature, Exception):
            return None

    def update_session_guild(self, cookie: str, new_guild_id: int) -> str | None:
        """Re-sign the session with a different active guild. Returns new cookie or None."""
        session = self.read_session(cookie)
        if not session:
            return None
        guild_ids = {g["id"] for g in session.get("guilds", [])}
        if new_guild_id not in guild_ids:
            return None
        session["guild_id"] = new_guild_id
        return self._serializer.dumps(session)

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
        avatar_url: str | None = session.get("avatar_url")

        # Use the active guild from session, falling back to the primary guild
        active_guild_id = session.get("guild_id", self._guild_id)

        # Prefer bot guild cache — instant, always reflects current roles
        ctx = request.app.state.ctx
        bot = getattr(ctx, "bot", None)
        guild = bot.get_guild(active_guild_id) if bot else None

        if guild:
            member = guild.get_member(user_id)
            if not member:
                return None  # User no longer in guild
            perms = resolve_discord_perms(member.guild_permissions.value)
            rids = tuple(r.id for r in member.roles if not r.is_default())
            rnames = tuple(r.name for r in member.roles if not r.is_default())
            return AuthenticatedUser(
                user_id=user_id,
                username=member.display_name,
                perms=perms,
                role_ids=rids,
                role_names=rnames,
                avatar_url=avatar_url,
            )

        # Fallback: use permission bits and roles stored at login time
        perms_bits: int = session.get("perms_bits", 0)
        stored_rids = tuple(int(r) for r in session.get("role_ids", []))
        stored_rnames = tuple(str(r) for r in session.get("role_names", []))
        return AuthenticatedUser(
            user_id=user_id,
            username=username,
            perms=resolve_discord_perms(perms_bits),
            role_ids=stored_rids,
            role_names=stored_rnames,
            avatar_url=avatar_url,
        )
