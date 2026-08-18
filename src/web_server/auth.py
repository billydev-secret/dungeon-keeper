"""Pluggable authentication backends for the dashboard.

Ships with two backends:

* ``OpenAuth`` — no-op, grants full permissions. For trusted LAN deployments.
* ``DiscordOAuthAuth`` — session-cookie auth backed by Discord OAuth2.
  Resolves permissions per-request from the bot's guild member cache (live)
  or from stored OAuth data (standalone fallback).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

_log = logging.getLogger("dungeonkeeper.web.auth")

SESSION_COOKIE = "dk_session"
SESSION_MAX_AGE = 30 * 86400  # 30 days

# Per-user session generation counter. Logging out bumps it; a cookie carrying
# an older generation is refused. Stored in the ``config`` KV table (guild_id 0)
# so no migration is needed and the revocation survives a restart.
SESSION_GEN_KEY_PREFIX = "session_gen:"

# Verbs that can change state. Everything else is safe to serve cross-origin.
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Discord permission bits used for dashboard access mapping
_ADMINISTRATOR = 0x8
_MANAGE_GUILD = 0x20
_KICK_MEMBERS = 0x2
_BAN_MEMBERS = 0x4
_MANAGE_MESSAGES = 0x2000
_MANAGE_ROLES = 0x10000000
_MODERATE_MEMBERS = 0x10000000000  # "Timeout Members" — Discord's modern mod perm


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

    _ALL_PERMS = frozenset({"admin", "moderator", "manage_server"})

    async def authenticate(self, request: Request) -> AuthenticatedUser:
        check_request_origin(request)
        return AuthenticatedUser(
            user_id=0,
            username="anonymous",
            perms=self._ALL_PERMS,
        )


_MOD_BITS = (
    _MANAGE_GUILD
    | _KICK_MEMBERS
    | _BAN_MEMBERS
    | _MANAGE_MESSAGES
    | _MANAGE_ROLES
    | _MODERATE_MEMBERS
)


def resolve_discord_perms(permission_bits: int) -> frozenset[str]:
    """Map a Discord permission bitfield to dashboard permission strings.

    **Bits only** — no knowledge of the guild's configured staff roles, so it
    no longer decides the ``moderator`` tier by itself. It stays as the bit half
    of :func:`resolve_guild_perms` and for the login log line, which has nothing
    but a bitfield in hand. Nothing that gates a request calls it directly.

    * ``admin``         — user has the Discord ADMINISTRATOR bit.
    * ``moderator``     — user has ADMINISTRATOR *or* any of MANAGE_GUILD,
      KICK_MEMBERS, BAN_MEMBERS, MANAGE_MESSAGES, MANAGE_ROLES,
      MODERATE_MEMBERS (Timeout Members).
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


def staff_role_ids(ctx, guild_id: int) -> tuple[frozenset[int], frozenset[int]]:
    """``(mod_role_ids, admin_role_ids)`` for a guild, or empty sets.

    Reads the same cached ``GuildConfig`` snapshot the bot gates on, so the two
    surfaces can never drift out of sync on a config edit. Defensive because
    the dashboard also runs against a context stub in standalone mode, where an
    unreadable config must mean "no configured roles" (bits-only, today's
    behaviour) rather than a 500 on every authenticated request.
    """
    try:
        cfg = ctx.guild_config(int(guild_id))
        return (frozenset(cfg.mod_role_ids), frozenset(cfg.admin_role_ids))
    except Exception:  # pragma: no cover - defensive
        _log.debug("Could not read staff roles for guild %s", guild_id, exc_info=True)
        return (frozenset(), frozenset())


def resolve_guild_perms(
    permission_bits: int,
    *,
    role_ids: Iterable[int] = (),
    mod_role_ids: Iterable[int] = (),
    admin_role_ids: Iterable[int] = (),
) -> frozenset[str]:
    """Dashboard permission strings for a member of a specific guild.

    The ``moderator`` tier is **the guild's configured staff roles**, not a
    permission bit — deliberately the same rule the bot enforces in Discord
    (``AppContext.is_mod``: administrator/manage_guild short-circuit, then
    ``mod_role_ids | admin_role_ids``). Before this existed the two surfaces
    disagreed in both directions: a mod with Timeout Members but no Manage
    Server passed here and was refused by the todo board's buttons, while
    Manage Channels opened those buttons and granted nothing here.

    A configured role is what a server *means* by "moderator"; the permission
    bits are an implementation detail of what that role can do, and a bot or a
    category-manager holding Manage Messages is not on the mod team. Guilds
    that have configured no staff roles at all fall back to the two elevated
    bits, so they are never locked out of their own dashboard.

    ``admin`` and ``manage_server`` are unchanged and stay bit-only:
    administrator is Discord's own ceiling, and widening it through a config
    row would let anyone who can edit that row hand themselves the keys.
    """
    perms = set(resolve_discord_perms(permission_bits))
    staff = set(mod_role_ids) | set(admin_role_ids)
    if staff:
        held = staff & set(role_ids)
        # Rebuild rather than mutate: "moderator" is now earned by the role or
        # by the two elevated bits, and the wider bit set no longer counts.
        perms.discard("moderator")
        if held or permission_bits & (_ADMINISTRATOR | _MANAGE_GUILD):
            perms.add("moderator")
    return frozenset(perms)


def _allowed_origin_hosts() -> set[str]:
    """Hostnames a state-changing request may legitimately originate from.

    The dashboard's own host is authoritative; ``DASHBOARD_BASE_URL`` and the
    ``DASHBOARD_RETURN_TO_URLS`` safelist (sibling apps such as the wellness
    panel on another port) are added because they are already trusted by the
    OAuth return-to check.
    """
    hosts: set[str] = set()
    raw_urls = [os.getenv("DASHBOARD_BASE_URL", "")]
    raw_urls += os.getenv("DASHBOARD_RETURN_TO_URLS", "").split(",")
    for raw in raw_urls:
        raw = raw.strip()
        if not raw:
            continue
        host = urlsplit(raw).hostname
        if host:
            hosts.add(host.lower())
    return hosts


def check_request_origin(request: Request) -> None:
    """Reject a cross-origin state-changing request (cheap CSRF backstop).

    ``SameSite=Lax`` on the session cookie already blocks the classic
    cross-site POST, but it is a single point of failure (browser quirks,
    a future ``SameSite=None``). This adds a second, independent check.

    Deliberately **permissive when the header is absent**: curl, the test
    client, and anything non-browser send neither ``Origin`` nor ``Referer``,
    and those callers cannot be CSRF'd (there is no ambient cookie). Only a
    header that is present *and* points somewhere else is refused.
    """
    if request.method.upper() not in _UNSAFE_METHODS:
        return
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return  # No browser context — nothing to forge from.

    origin_host = urlsplit(source).hostname
    if not origin_host:
        # "null" (sandboxed iframe / opaque origin) or a malformed header.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cross-origin request rejected.",
        )

    allowed = _allowed_origin_hosts()
    own_host = urlsplit(f"//{request.headers.get('host', '')}").hostname
    if own_host:
        allowed.add(own_host.lower())
    if request.url.hostname:
        allowed.add(request.url.hostname.lower())

    if origin_host.lower() not in allowed:
        _log.warning(
            "Blocked cross-origin %s %s from %s",
            request.method,
            request.url.path,
            origin_host,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cross-origin request rejected.",
        )


class DiscordOAuthAuth:
    """Discord OAuth2 session-based authentication.

    On every request the backend resolves permissions live from the bot's
    guild member cache when available, guaranteeing that role changes in
    Discord are reflected immediately. When the bot cache is unavailable
    (standalone mode, the startup window, a guild the bot was removed from),
    it falls back to permissions stored in the session — but only the ones
    captured for the guild that is active now; see ``perms_guild_id``.

    Two other seams are closed here rather than in a middleware, because this
    is the one place every guarded request already passes through: server-side
    session revocation (``revoke_sessions``, bumped by ``/logout``) and the
    cross-origin check on state-changing verbs (``check_request_origin``).
    """

    def __init__(self, session_secret: str, guild_id: int, support_user_id: int = 0) -> None:
        from itsdangerous import URLSafeTimedSerializer

        self._serializer = URLSafeTimedSerializer(session_secret)
        self._guild_id = guild_id
        self._support_user_id = support_user_id
        # user_id → session generation. Read through to the config KV table on
        # first use so the per-request check costs one dict lookup, not a query.
        self._gen_cache: dict[int, int] = {}

    # ── Session revocation (logout) ─────────────────────────────────

    def current_generation(self, ctx, user_id: int) -> int:
        """Current session generation for a user (0 until they log out once)."""
        cached = self._gen_cache.get(user_id)
        if cached is not None:
            return cached
        gen = 0
        try:
            from bot_modules.core.db_utils import get_config_value
            with ctx.open_db() as conn:
                gen = int(
                    get_config_value(
                        conn, f"{SESSION_GEN_KEY_PREFIX}{user_id}", "0", 0,
                        allow_legacy_fallback=False,
                    )
                    or 0
                )
        except Exception:
            gen = 0
        self._gen_cache[user_id] = gen
        return gen

    def revoke_sessions(self, ctx, user_id: int) -> int:
        """Invalidate every outstanding cookie for this user. Returns the new gen.

        Logging out has to mean something server-side: without this a stolen
        cookie stays usable for the full 30-day ``max_age``.
        """
        gen = self.current_generation(ctx, user_id) + 1
        try:
            from bot_modules.core.db_utils import set_config_value
            with ctx.open_db() as conn:
                set_config_value(conn, f"{SESSION_GEN_KEY_PREFIX}{user_id}", str(gen), 0)
        except Exception:
            _log.warning("Could not persist session revocation for %s", user_id)
        self._gen_cache[user_id] = gen
        return gen

    def _support_access_enabled(self, ctx, guild_id: int) -> bool:
        """Return True if the given guild has opted in to support access."""
        try:
            from bot_modules.core.db_utils import get_config_value
            with ctx.open_db() as conn:
                val = get_config_value(
                    conn, "support_access_enabled", "0", guild_id,
                    allow_legacy_fallback=False,
                )
                return val == "1"
        except Exception:
            return False

    # ── Session cookie helpers ──────────────────────────────────────

    def create_session_cookie(
        self,
        user_id: int,
        username: str,
        access_token: str = "",  # deprecated, ignored — see docstring
        permission_bits: int = 0,
        role_ids: list[int] | None = None,
        role_names: list[str] | None = None,
        guild_id: int | None = None,
        guilds: list[dict] | None = None,
        avatar_url: str | None = None,
        generation: int = 0,
    ) -> str:
        """Create a signed, timestamped session cookie value.

        ``access_token`` is accepted for call-site compatibility and
        deliberately **not stored**: the cookie is signed but not encrypted, so
        anything in it is readable by whoever holds it, and nothing ever read
        the token back (B-SEC4).

        ``perms_guild_id`` records which guild ``permission_bits`` were
        captured for, so the cache-miss fallback in :meth:`authenticate` can
        refuse to apply guild A's bits while guild B is active (B-SEC1).
        """
        active_guild = guild_id or self._guild_id
        return self._serializer.dumps(
            {
                "uid": user_id,
                "name": username,
                "perms_bits": permission_bits,
                "perms_guild_id": active_guild,
                "role_ids": role_ids or [],
                "role_names": role_names or [],
                "guild_id": active_guild,
                "guilds": guilds or [],
                "avatar_url": avatar_url,
                "gen": generation,
            }
        )

    def read_session(self, cookie: str) -> dict | None:
        """Decode and verify a session cookie. Returns None on failure."""
        from itsdangerous import BadSignature

        try:
            return self._serializer.loads(cookie, max_age=SESSION_MAX_AGE)  # type: ignore[no-any-return]
        except (BadSignature, Exception):
            return None

    def update_session_guild(
        self,
        cookie: str,
        new_guild_id: int,
        *,
        permission_bits: int | None = None,
        role_ids: list[int] | None = None,
        role_names: list[str] | None = None,
    ) -> str | None:
        """Re-sign the session with a different active guild.

        The stored permission bits/roles are **re-captured for the target
        guild**, not carried over: they described the guild the user logged in
        to. When the caller can't resolve them (bot offline, not a member),
        they are cleared, so the cache-miss fallback grants nothing rather than
        replaying guild A's admin bits in guild B.

        Returns the new cookie value, or None if the switch isn't allowed.
        """
        session = self.read_session(cookie)
        if not session:
            return None
        guild_ids = {g["id"] for g in session.get("guilds", [])}
        if new_guild_id not in guild_ids:
            return None
        session["guild_id"] = new_guild_id
        session["perms_guild_id"] = new_guild_id
        session["perms_bits"] = permission_bits or 0
        session["role_ids"] = role_ids or []
        session["role_names"] = role_names or []
        session.pop("token", None)  # legacy sessions carried the OAuth token
        return self._serializer.dumps(session)

    # ── Per-request authentication ──────────────────────────────────

    async def authenticate(self, request: Request) -> AuthenticatedUser | None:
        check_request_origin(request)
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

        # Server-side revocation: a logout bumps the user's generation, which
        # retires every cookie minted before it.
        if int(session.get("gen", 0) or 0) < self.current_generation(ctx, user_id):
            return None

        bot = getattr(ctx, "bot", None)
        guild = bot.get_guild(active_guild_id) if bot else None

        _SUPPORT_PERMS = frozenset({"admin", "moderator", "manage_server"})
        is_support = self._support_user_id != 0 and user_id == self._support_user_id

        if guild:
            member = guild.get_member(user_id)
            if not member:
                # Support user in an opted-in guild they haven't joined
                if is_support and self._support_access_enabled(ctx, active_guild_id):
                    return AuthenticatedUser(
                        user_id=user_id,
                        username=username,
                        perms=_SUPPORT_PERMS,
                        avatar_url=avatar_url,
                    )
                return None  # User no longer in guild
            rids = tuple(r.id for r in member.roles if not r.is_default())
            staff = staff_role_ids(ctx, active_guild_id)
            perms = resolve_guild_perms(
                member.guild_permissions.value,
                role_ids=rids,
                mod_role_ids=staff[0],
                admin_role_ids=staff[1],
            )
            # Elevate support user to full admin when the guild has opted in
            if is_support and self._support_access_enabled(ctx, active_guild_id):
                perms = _SUPPORT_PERMS
            rnames = tuple(r.name for r in member.roles if not r.is_default())
            return AuthenticatedUser(
                user_id=user_id,
                username=member.display_name,
                perms=perms,
                role_ids=rids,
                role_names=rnames,
                avatar_url=avatar_url,
            )

        # Fallback: use permission bits and roles stored at login time.
        #
        # Only when they were captured for the guild that is active *now*.
        # A session records the guild its bits came from; switching guilds
        # re-captures or clears them. Sessions minted before that field
        # existed are treated as unqualified and get nothing — a cache miss
        # (startup window, bot kicked, standalone mode) must never hand an
        # admin of guild A admin rights in guild B (B-SEC1).
        perms_guild_id = session.get("perms_guild_id")
        perms_qualified = (
            perms_guild_id is not None and int(perms_guild_id) == int(active_guild_id)
        )
        perms_bits: int = session.get("perms_bits", 0) if perms_qualified else 0
        stored_rids = (
            tuple(int(r) for r in session.get("role_ids", [])) if perms_qualified else ()
        )
        stored_rnames = (
            tuple(str(r) for r in session.get("role_names", []))
            if perms_qualified
            else ()
        )
        if not perms_qualified:
            _log.debug(
                "Session perms not qualified for guild %s (captured for %s) — no perms granted",
                active_guild_id,
                perms_guild_id,
            )
        # The stored role ids carry the same guild qualification as the bits
        # (both are cleared on a guild switch), so the configured-role rule
        # applies here too — a mod whose only claim is the role must not lose
        # the dashboard the moment the bot's guild cache is cold.
        fallback_staff = staff_role_ids(ctx, active_guild_id)
        fallback_perms = resolve_guild_perms(
            perms_bits,
            role_ids=stored_rids,
            mod_role_ids=fallback_staff[0],
            admin_role_ids=fallback_staff[1],
        )
        if is_support and self._support_access_enabled(ctx, active_guild_id):
            fallback_perms = _SUPPORT_PERMS
        return AuthenticatedUser(
            user_id=user_id,
            username=username,
            perms=fallback_perms,
            role_ids=stored_rids,
            role_names=stored_rnames,
            avatar_url=avatar_url,
        )
