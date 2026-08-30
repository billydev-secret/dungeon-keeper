"""Shared fixtures for web route tests."""

from __future__ import annotations

import sys
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bot_modules.core.db_utils import open_db
from tests.db_template import migrated_db
from web_server.auth import DiscordOAuthAuth, OpenAuth, SESSION_COOKIE
from web_server.deps import invalidate_report_cache
from web_server.server import create_app


class FakeCtx:
    """Minimal AppContext substitute for web route tests."""

    def __init__(self, db_path: Path, guild_id: int = 123):
        self.db_path = db_path
        self.guild_id = guild_id
        self.bot = None
        self._guild_config_cache: dict = {}

    def open_db(self):
        return open_db(self.db_path)

    def guild_config(self, guild_id: int):
        from bot_modules.core.app_context import GuildConfig

        cfg = self._guild_config_cache.get(guild_id)
        if cfg is None:
            with self.open_db() as conn:
                cfg = GuildConfig.load(
                    conn, guild_id, allow_legacy_fallback=(guild_id == self.guild_id)
                )
            self._guild_config_cache[guild_id] = cfg
        return cfg

    def invalidate_guild_config(self, guild_id: int) -> None:
        self._guild_config_cache.pop(guild_id, None)

    def set_config_value(self, key: str, value: str, guild_id: int | None = None) -> str:
        """Mirrors AppContext.set_config_value, cache invalidation included.

        Anything reaching a route through core.role_provision persists an id
        this way; without it those routes died on a missing attribute.
        """
        from bot_modules.core.db_utils import set_config_value

        gid = self.guild_id if guild_id is None else guild_id
        with self.open_db() as conn:
            set_config_value(conn, key, value, gid)
            conn.commit()
        self.invalidate_guild_config(gid)
        return value


@pytest.fixture
def web_db(tmp_path) -> Path:
    """A fresh SQLite database with full schema applied."""
    return migrated_db(tmp_path / "web_test.db")


@pytest.fixture
def fake_ctx(web_db) -> FakeCtx:
    return FakeCtx(web_db)


class StubPermissions:
    """discord.Permissions stand-in — only the bits health.py reads.

    ``manage_messages`` is a *separate* argument because it is a separate
    population: Mod Coverage counts everyone who can delete a message, which
    is a wider circle than the kick/ban holders the workload report counts.
    It defaults to ``mod`` so existing callers keep describing one kind of
    moderator, and Discord grants it implicitly with Administrator anyway.
    """

    def __init__(self, *, mod: bool = False, manage_messages: bool | None = None):
        self.administrator = mod
        self.manage_guild = mod
        self.kick_members = mod
        self.ban_members = mod
        self.manage_messages = mod if manage_messages is None else manage_messages


class StubMember:
    def __init__(
        self,
        member_id: int,
        *,
        bot: bool = False,
        mod: bool = False,
        manage_messages: bool | None = None,
        joined_at=None,
        display_name: str | None = None,
    ):
        self.id = member_id
        self.bot = bot
        self.guild_permissions = StubPermissions(
            mod=mod, manage_messages=manage_messages
        )
        self.joined_at = joined_at
        self.display_name = display_name or f"member-{member_id}"


class StubGuild:
    """Minimal ``discord.Guild`` stand-in for ``health._guild_extras``.

    ``FakeCtx.bot`` is ``None`` by default, which the health routes read as
    "the guild isn't available" — the degraded state. Tests that need the
    non-degraded path attach one of these via the ``live_guild`` fixture.
    """

    def __init__(self, guild_id: int, members: list[StubMember] | None = None):
        self.id = guild_id
        self.members = members or []
        self.member_count = len(self.members)
        self.voice_channels: list = []
        self.channels: list = []

    def get_channel(self, channel_id: int):
        return next((c for c in self.channels if c.id == channel_id), None)

    def get_member(self, member_id: int):
        return next((m for m in self.members if m.id == member_id), None)


class StubBot:
    def __init__(self, guild: StubGuild):
        self._guild = guild

    def get_guild(self, guild_id: int):
        return self._guild if self._guild.id == guild_id else None


@pytest.fixture
def live_guild(fake_ctx):
    """Attach a populated guild to ``fake_ctx`` and hand it back.

    Routes resolve ``ctx.bot`` per request, so the returned guild can be
    mutated (or ``fake_ctx.bot`` reset to ``None``) between requests inside a
    single test to simulate the gateway cache going cold.
    """

    def _attach(members: list[StubMember] | None = None) -> StubGuild:
        guild = StubGuild(fake_ctx.guild_id, members)
        fake_ctx.bot = StubBot(guild)
        return guild

    return _attach


@pytest.fixture
def open_client(fake_ctx) -> Generator[TestClient, None, None]:
    """TestClient with no auth (OpenAuth mode)."""
    app = create_app(fake_ctx, auth=OpenAuth())
    client = TestClient(app)
    invalidate_report_cache()
    yield client
    client.close()
    invalidate_report_cache()


@pytest.fixture
def authed_client(fake_ctx) -> Generator[TestClient, None, None]:
    """TestClient with a Discord OAuth session cookie (primary guild)."""
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    app = create_app(fake_ctx, auth=auth)
    client = TestClient(app)
    cookie = auth.create_session_cookie(
        user_id=1,
        username="tester",
        access_token="token",
        permission_bits=0x8,
        guild_id=fake_ctx.guild_id,
        guilds=[{"id": fake_ctx.guild_id, "name": "Test Guild", "icon": None}],
    )
    client.cookies.set(SESSION_COOKIE, cookie)
    invalidate_report_cache()
    yield client
    client.close()
    invalidate_report_cache()
