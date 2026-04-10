"""Pluggable authentication backends for the dashboard.

Phase 1 ships with ``OpenAuth`` — a no-op backend that grants every request
full permissions. The bot runs on a LAN the user trusts. The protocol is
designed so a ``LocalhostAuth`` (127.0.0.1 + shared-secret header) or
``DiscordOAuth2Auth`` can be swapped in later by changing one line in
``web/server.py``, without touching any route handler.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fastapi import Request


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
